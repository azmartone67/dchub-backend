"""Named HUMAN customer testimonials come from ONE source and reach agents labelled.

NO NETWORK, NO DB. tests/conftest.py sets DCHUB_CUSTOMER_TESTIMONIALS_FETCH=0
for the whole run; tests here replace _fetch_raw with a canned document, or
patch requests.get when exercising the shipped _fetch_raw itself.

What this pins:
  * the loader: User-Agent (Cloudflare 403s Python's default), validation drops
    incomplete rows, last good kept on error, [] when never loaded, no raise;
  * why_dchub carries the list, the page URL and a people-not-AI label;
  * the SERVED /llms.txt and /llms-full.txt carry the section, with the quote
    when loaded and pointer-only when not;
  * ai-agents.json (main.py handler, read from source) carries the pointer.
"""
import os

import pytest

from util import customer_testimonials as ct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RICH = {
    "id": "rich-bray-lpi",
    "name": "Rich Bray",
    "title": "Development Manager",
    "company": "LPI Group",
    "quote": "DC Hub is now integral to how we evaluate every site.",
    "featured": True,
    "approved_for_public_use": "2026-09-24",
}
DOC = {
    "about": "x",
    "page": "https://dchub.cloud/testimonials",
    "customer_testimonials": [RICH],
    "ai_agent_quotes": {"url": "https://dchub.cloud/api/v1/testimonials"},
}


def _serve(monkeypatch, doc):
    calls = []

    def fake():
        calls.append(1)
        if isinstance(doc, Exception):
            raise doc
        return doc

    monkeypatch.setattr(ct, "_fetch_raw", fake)
    return calls


# ── the loader ────────────────────────────────────────────────────────────

def test_fetch_sends_the_backend_user_agent(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"customer_testimonials": []}

    def fake_get(url, headers=None, timeout=None, **kw):
        seen["ua"] = (headers or {}).get("User-Agent")
        seen["url"] = url
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(ct.requests, "get", fake_get)
    monkeypatch.setenv(ct.FETCH_ENV, "1")
    assert ct._fetch_raw() == {"customer_testimonials": []}
    assert seen["ua"] == "DCHub-Backend/1.0 (+https://dchub.cloud)"
    assert seen["url"] == "https://dchub.cloud/testimonials.json"
    assert seen["timeout"] and max(seen["timeout"]) <= 5


def test_fetch_switch_off_never_opens_a_socket(monkeypatch):
    called = []
    monkeypatch.setattr(ct.requests, "get", lambda *a, **k: called.append(1))
    monkeypatch.setenv(ct.FETCH_ENV, "0")
    with pytest.raises(RuntimeError):
        ct._fetch_raw()
    assert ct.get_customer_testimonials() == []
    assert called == [], "requests.get was reached with the fetch switched off"


def test_loader_returns_valid_rows(monkeypatch):
    _serve(monkeypatch, DOC)
    rows = ct.get_customer_testimonials()
    assert [r["name"] for r in rows] == ["Rich Bray"]
    assert ct.public_fields(rows[0]) == {
        "name": "Rich Bray", "title": "Development Manager",
        "company": "LPI Group",
        "quote": "DC Hub is now integral to how we evaluate every site.",
    }


@pytest.mark.parametrize("missing", ["name", "title", "company", "quote"])
def test_rows_missing_a_required_field_are_dropped(monkeypatch, missing):
    bad = dict(RICH)
    bad[missing] = "  "
    other = dict(RICH, name="Kept Person")
    _serve(monkeypatch, dict(DOC, customer_testimonials=[bad, other]))
    assert [r["name"] for r in ct.get_customer_testimonials()] == ["Kept Person"]


def test_never_loaded_is_empty_and_does_not_raise(monkeypatch):
    _serve(monkeypatch, OSError("boom"))
    assert ct.get_customer_testimonials() == []
    assert ct.featured_testimonial() is None


def test_last_good_is_kept_when_a_refresh_fails(monkeypatch):
    _serve(monkeypatch, DOC)
    assert ct.get_customer_testimonials()
    # Force the cache stale, then make the origin fail.
    ct._cache["next_fetch_at"] = 0.0
    calls = _serve(monkeypatch, OSError("origin down"))
    rows = ct.get_customer_testimonials()
    assert calls, "the stale cache did not attempt a refresh"
    assert [r["name"] for r in rows] == ["Rich Bray"]


def test_cache_serves_without_refetching(monkeypatch):
    calls = _serve(monkeypatch, DOC)
    ct.get_customer_testimonials()
    ct.get_customer_testimonials()
    assert len(calls) == 1


def test_bad_shape_counts_as_a_failed_refresh(monkeypatch):
    _serve(monkeypatch, DOC)
    ct.get_customer_testimonials()
    ct._cache["next_fetch_at"] = 0.0
    _serve(monkeypatch, {"customer_testimonials": "not a list"})
    assert [r["name"] for r in ct.get_customer_testimonials()] == ["Rich Bray"]


def test_featured_prefers_flag_then_first():
    a = dict(RICH, name="A", featured=False)
    b = dict(RICH, name="B", featured=True)
    assert ct.featured_testimonial([a, b])["name"] == "B"
    assert ct.featured_testimonial([a])["name"] == "A"
    assert ct.featured_testimonial([]) is None


# ── why_dchub ─────────────────────────────────────────────────────────────

def _why(monkeypatch, doc):
    flask = pytest.importorskip("flask")
    ci = pytest.importorskip("routes.competitive_intel")
    _serve(monkeypatch, doc)
    app = flask.Flask(__name__)
    app.register_blueprint(ci.competitive_intel_bp)
    r = app.test_client().get("/api/v1/competitive/why-dchub")
    assert r.status_code == 200
    return r.get_json()


def test_why_dchub_carries_named_human_customers(monkeypatch):
    body = _why(monkeypatch, DOC)
    assert body["customer_testimonials"] == [{
        "name": "Rich Bray", "title": "Development Manager",
        "company": "LPI Group",
        "quote": "DC Hub is now integral to how we evaluate every site.",
    }]
    assert body["testimonials_page"] == "https://dchub.cloud/testimonials"
    note = body["customer_testimonials_note"].lower()
    assert "human" in note and "not ai" in note
    # Human quotes are not smuggled into the AI-positioning fields.
    assert "Rich Bray" not in str(body["edges"]) and "Rich Bray" not in body["pitch"]


def test_why_dchub_empty_source_is_an_empty_list(monkeypatch):
    body = _why(monkeypatch, OSError("down"))
    assert body["customer_testimonials"] == []
    assert body["testimonials_page"] == "https://dchub.cloud/testimonials"


# ── llms.txt / llms-full.txt, the SERVED renderers ───────────────────────

def _door(monkeypatch, doc, path):
    flask = pytest.importorskip("flask")
    adr = pytest.importorskip("ai_discovery_routes")
    _serve(monkeypatch, doc)
    app = flask.Flask(__name__)
    adr.register_discovery_routes(app)
    r = app.test_client().get(path)
    assert r.status_code == 200
    return r.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_llms_doors_carry_the_featured_quote_with_attribution(monkeypatch, path):
    body = _door(monkeypatch, DOC, path)
    assert "## Customer testimonials (named human customers)" in body
    assert "https://dchub.cloud/testimonials.json" in body
    assert "DC Hub is now integral to how we evaluate every site." in body
    assert "Rich Bray, Development Manager, LPI Group" in body


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_llms_doors_are_pointer_only_when_nothing_loaded(monkeypatch, path):
    body = _door(monkeypatch, OSError("down"), path)
    assert "## Customer testimonials (named human customers)" in body
    assert "https://dchub.cloud/testimonials" in body
    assert "Rich Bray" not in body


# ── ai-agents.json (main.py cannot be imported under the suite) ──────────

def test_ai_agents_json_handler_carries_the_pointer():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    start = src.index("if path == '/.well-known/ai-agents.json'")
    end = src.index('"provider": {', start)
    handler = src[start:end]
    assert '"customer_testimonials": _ai_customer_testimonials' in handler
    assert "testimonials_pointer" in handler
    ptr = ct.testimonials_pointer()
    assert ptr["page"] == "https://dchub.cloud/testimonials"
    assert ptr["json"] == "https://dchub.cloud/testimonials.json"
    assert "not ai assistants" in ptr["note"].lower()
