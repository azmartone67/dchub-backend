"""The Market Brief PDF export: rendered by Gotenberg, never publicly cacheable,
never a 500.

2026-09-11. A Pro user clicked "Download PDF" on /markets/midland-tx/brief three
times and the browser reported "Couldn't download - Something went wrong" each
time. Railway recorded three 5xx on that path between 08:00Z and 08:30Z, and the
deploy log printed "WeasyPrint could not import some external libraries" at
08:13:55Z and 08:15:47Z.

The route imported weasyprint, which cannot load on the Railway image: the live
service config says builder RAILPACK, and Railpack does not read nixpacks.toml,
so the Pango/Cairo packages declared there never reach the runtime.
routes/pdf_render.py has recorded that since 2026-06-24, and the Deal Desk Brief
and the Site Analysis PDF already render through it. The same route also sent the
Pro-only file as `public, max-age=3600, s-maxage=3600`.

Pinned here:
  * anonymous / FREE get 402 and nothing is rendered;
  * PRO gets the application/pdf that routes.pdf_render.html_to_pdf produced;
  * that PDF is private + no-store, never public, s-maxage or max-age;
  * a renderer failure (unreachable, slow, a non-PDF body, a broken URL, or no
    time left in the request) is a 503 with Retry-After that does not publish
    the renderer's internal host, never a 500;
  * a failed render is not remembered, a repeat download reuses a good one;
  * the renderer only gets the time left in the request budget;
  * the module never imports weasyprint (AST, with a must-fail control);
  * the cover's padding sits inside its height (measured with Chrome 152:
    without it the cover's meta block printed on a page of its own).
"""
import ast
import pathlib
import re

import pytest
import requests
from flask import Flask

import routes.market_brief as mb
import routes.pdf_render as pdf_render

REPO = pathlib.Path(__file__).resolve().parent.parent
URL = "/markets/midland-tx/brief.pdf"
FAKE_PDF = b"%PDF-1.7\n% test body\n" + b"0" * 128

BRIEF = {
    "ok": True, "slug": "midland-tx",
    "hero": {"name": "Midland-Odessa", "verdict": "BUILD", "composite_score": None,
             "computed_at": "2026-09-11T06:37:04Z"},
    "live_as_of": {"iso": "2026-09-11T06:37:04Z"},
    "kpis": {}, "power_grid": {}, "pipeline": [], "operators": [], "ma": [],
    "comps": {}, "risk": {}, "outlook": {},
}


class Renderer:
    """Stands in for routes.pdf_render.html_to_pdf and records every call.
    `plan` is consumed one entry per call: bytes are returned, exceptions raised."""

    def __init__(self, *plan):
        self.plan = list(plan) or [FAKE_PDF]
        self.calls = []

    def __call__(self, html, **kw):
        self.calls.append({"html": html, **kw})
        step = self.plan[min(len(self.calls), len(self.plan)) - 1]
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def make_client(monkeypatch):
    def make(tier="PRO", renderer=None, build=None):
        mb._PDF_CACHE.clear()
        r = renderer or Renderer()
        monkeypatch.setattr(pdf_render, "html_to_pdf", r)
        monkeypatch.setattr(mb, "_caller_tier", lambda: tier)
        monkeypatch.setattr(mb, "_build_brief",
                            build or (lambda slug, tier: dict(BRIEF, slug=slug)))
        app = Flask(__name__)
        app.register_blueprint(mb.market_brief_bp)
        return app.test_client(), r
    yield make
    mb._PDF_CACHE.clear()


def test_anonymous_and_free_get_402_and_nothing_is_rendered(make_client):
    for tier in ("FREE", "IDENTIFIED", "DEVELOPER"):
        client, r = make_client(tier=tier)
        resp = client.get(URL)
        assert resp.status_code == 402, tier
        assert resp.get_json()["error"] == "pdf_requires_pro"
        assert r.calls == [], f"{tier} caller started a render"


def test_pro_gets_the_pdf_the_renderer_produced(make_client):
    client, r = make_client(tier="PRO")
    resp = client.get(URL)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.mimetype == "application/pdf"
    assert resp.data == FAKE_PDF
    assert len(r.calls) == 1
    printed = r.calls[0]["html"]
    assert 'class="cover"' in printed and "Midland-Odessa" in printed
    assert re.fullmatch(
        r'attachment; filename="dchub-market-brief-midland-tx-\d{4}-\d{2}-\d{2}\.pdf"',
        resp.headers["Content-Disposition"])


def test_the_pro_only_pdf_is_never_publicly_cacheable(make_client):
    client, _ = make_client(tier="PRO")
    resp = client.get(URL)
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    directives = {d.strip().split("=")[0] for d in cc.lower().split(",") if d.strip()}
    assert {"private", "no-store"} <= directives, cc
    assert not directives & {"public", "s-maxage", "max-age"}, cc
    assert resp.headers.get("CDN-Cache-Control", "").lower() == "no-store"


@pytest.mark.parametrize("failure", [
    pytest.param(requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='render-pdf.railway.internal', port=3000): "
        "Max retries exceeded"), id="renderer-unreachable"),
    pytest.param(requests.exceptions.ReadTimeout("Read timed out."), id="renderer-slow"),
    pytest.param(b"<html><body>502 Bad Gateway</body></html>", id="renderer-answers-html"),
    pytest.param(requests.exceptions.InvalidURL("Invalid URL 'render-pdf'"),
                 id="renderer-url-broken-which-is-a-ValueError-too"),
])
def test_a_renderer_failure_is_a_503_with_retry_after_never_a_500(make_client, failure):
    client, _ = make_client(tier="PRO", renderer=Renderer(failure))
    resp = client.get(URL)
    text = resp.get_data(as_text=True)
    assert resp.status_code == 503, (resp.status_code, text[:300])
    assert resp.mimetype == "application/json"
    assert resp.get_json()["error"] == "pdf_engine_unavailable"
    assert resp.headers.get("Retry-After")
    assert "railway.internal" not in text


def test_a_market_that_is_not_covered_is_404_not_503(make_client):
    client, r = make_client(
        tier="PRO", build=lambda slug, tier: {"ok": False, "error": "market_not_found"})
    assert client.get(URL).status_code == 404
    assert r.calls == []


def test_a_failed_render_is_retried_and_a_good_one_is_reused(make_client):
    client, r = make_client(
        tier="PRO",
        renderer=Renderer(requests.exceptions.ReadTimeout("Read timed out."), FAKE_PDF))
    assert client.get(URL).status_code == 503
    assert client.get(URL).status_code == 200, "a failure was remembered"
    assert client.get(URL).status_code == 200
    assert len(r.calls) == 2, "the good render was not reused"


def test_the_renderer_only_gets_the_time_left_in_the_request(make_client, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mb, "_monotonic", lambda: clock["t"])

    def slow_build(slug, tier):
        clock["t"] += 4.0
        return dict(BRIEF, slug=slug)

    client, r = make_client(tier="PRO", build=slow_build)
    assert client.get(URL).status_code == 200
    assert r.calls[0]["timeout"] == pytest.approx(mb._PDF_REQUEST_BUDGET_S - 4.0)


def test_no_render_starts_once_the_budget_is_spent(make_client, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mb, "_monotonic", lambda: clock["t"])

    def slower_build(slug, tier):
        clock["t"] += mb._PDF_REQUEST_BUDGET_S - 0.5
        return dict(BRIEF, slug=slug)

    client, r = make_client(tier="PRO", build=slower_build)
    assert client.get(URL).status_code == 503
    assert r.calls == []


def _imports_weasyprint(source):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] == "weasyprint" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "weasyprint":
            return True
    return False


def test_the_import_detector_finds_the_deferred_form_that_shipped():
    # The removed import sat inside a function body; a string mentioning it is not one.
    assert _imports_weasyprint("def f():\n    from weasyprint import HTML\n    return HTML\n")
    assert _imports_weasyprint("import weasyprint.css\n")
    assert not _imports_weasyprint("NOTE = 'from weasyprint import HTML'\n")


def test_the_brief_module_never_imports_weasyprint():
    source = (REPO / "routes" / "market_brief.py").read_text(encoding="utf-8")
    assert not _imports_weasyprint(source)


def test_the_cover_padding_sits_inside_its_height():
    rule = re.search(r"\.cover-inner\s*\{([^}]*)\}", mb._PDF_BASE_CSS_TMPL)
    assert rule, "no .cover-inner rule in the print CSS"
    assert re.search(r"(^|;)\s*box-sizing:\s*border-box\s*(;|$)", rule.group(1)), rule.group(1)
