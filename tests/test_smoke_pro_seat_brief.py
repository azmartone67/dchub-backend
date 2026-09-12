"""scripts/smoke_pro_seat_brief.py — the Pro-seat smoke must be able to fail.

The probe runs against production from the post-deploy smoke. These tests drive
its verdicts and its whole run() against a fake edge, so each defect it is named
for is shown to turn it RED here, and a missing or non-Pro key is shown to be
BLIND (never a pass, never a false conviction).
"""
import importlib.util
import pathlib

import pytest
import requests

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "smoke_pro_seat_brief", REPO / "scripts" / "smoke_pro_seat_brief.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

# What every brief emits above </body> since #4433: the refresh self-heal
# FIRST (both are `defer`, so document order is execution order), then nav.
HEAL_TAIL = ('<script src="/js/dchub-api-base.js" defer></script>'
             '<script src="/js/dchub-nav.js" defer></script>')
NO_HEAL_TAIL = '<script src="/js/dchub-nav.js" defer></script>'

PAID_PAGE = ('<h2>Power &amp; Grid</h2><table></table>'
             '<a href="https://dchub.cloud/markets/dallas/brief.pdf" class="pdf-btn pro" '
             'download="dchub-market-brief-dallas.pdf">Download PDF</a>') + HEAL_TAIL
ANON_PAGE = ('<div class="blur-title">PRO unlocks all sections</div>' * 6
             + '<a href="/pricing?utm_source=market_brief_pdf" class="pdf-btn upgrade">'
               'Upgrade to download PDF</a>') + HEAL_TAIL
GOOD_PDF = b"%PDF-1.7\n" + b"x" * 4096 + b"\n%%EOF\n"


class FakeResponse:
    def __init__(self, status=200, text="", content=None, headers=None):
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode()
        self.headers = headers or {}


class FakeEdge:
    """A tiny model of the edge: a URL-keyed cache that either respects
    credentials (healthy) or ignores them (the 2026-09-11 defect)."""

    def __init__(self, leak=False, key_is_pro=True, pdf=None, pdf_headers=None,
                 can_heal=True):
        self.leak, self.key_is_pro, self.can_heal = leak, key_is_pro, can_heal
        self.cache = {}
        self.pdf = pdf if pdf is not None else FakeResponse(
            200, content=GOOD_PDF,
            headers={"Content-Type": "application/pdf", "Cache-Control": "private, no-store"})
        self.seen_keys = []

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        keyed = "X-API-Key" in (headers or {})
        self.seen_keys.append(keyed)
        path = url.split("?")[0]
        if path.endswith(".pdf"):
            if not keyed:
                return FakeResponse(402, text='{"error":"pdf_requires_pro"}')
            return self.pdf
        paid = keyed and self.key_is_pro
        if self.leak and url in self.cache:
            return self.cache[url]
        body = PAID_PAGE if paid else ANON_PAGE
        if not self.can_heal:
            body = body.replace(HEAL_TAIL, NO_HEAL_TAIL)
        resp = FakeResponse(200, text=body)
        if self.leak or not keyed:
            self.cache[url] = resp
        return resp


def verdicts(results):
    return {name: verdict for name, verdict, _ in results}


def test_the_control_fires_on_the_real_verdicts():
    assert smoke.control_fires()


def test_a_healthy_edge_passes_every_check():
    res = smoke.run("https://dchub.cloud", "dallas", "k", session=FakeEdge(), pause_s=0)
    v = verdicts(res)
    assert set(v.values()) == {smoke.PASS}, res
    assert smoke.report(res) == 0


def test_the_2026_09_11_leak_is_red_in_both_directions():
    res = smoke.run("https://dchub.cloud", "dallas", "k", session=FakeEdge(leak=True), pause_s=0)
    v = verdicts(res)
    assert v["anonymous-never-gets-the-paid-copy"] == smoke.RED
    assert v["paid-never-gets-the-anonymous-copy"] == smoke.RED
    assert smoke.report(res) == 1


def test_a_brief_that_cannot_heal_a_lapsed_seat_is_red():
    """The 2026-09-12 report: the render a lapsed Pro seat gets carries no
    /js/dchub-api-base.js, so the 401 on /api/auth/me is never refreshed."""
    res = smoke.run("https://dchub.cloud", "dallas", "k",
                    session=FakeEdge(can_heal=False), pause_s=0)
    assert verdicts(res)["brief-can-heal-a-lapsed-paid-seat"] == smoke.RED
    assert smoke.report(res) == 1


def test_the_heal_loaded_after_nav_is_red():
    """Order is the invariant: api-base after nav never wraps nav's call."""
    assert smoke.verdict_lapsed_token_heal(
        '<script src="/js/dchub-nav.js" defer></script>'
        '<script src="/js/dchub-api-base.js" defer></script>')[0] == smoke.RED


@pytest.mark.parametrize("pdf", [
    pytest.param(FakeResponse(503, text='{"error":"pdf_render_failed"}',
                              headers={"Content-Type": "application/json"}), id="the-weasyprint-5xx"),
    pytest.param(FakeResponse(200, content=b"<html>not a pdf</html>",
                              headers={"Content-Type": "text/html"}), id="html-under-a-pdf-name"),
])
def test_a_broken_pro_pdf_is_red(pdf):
    res = smoke.run("https://dchub.cloud", "dallas", "k", session=FakeEdge(pdf=pdf), pause_s=0)
    assert verdicts(res)["pro-pdf-downloads"] == smoke.RED
    assert smoke.report(res) == 1


def test_a_publicly_cacheable_pro_pdf_is_red():
    pdf = FakeResponse(200, content=GOOD_PDF, headers={
        "Content-Type": "application/pdf", "Cache-Control": "public, max-age=3600, s-maxage=3600"})
    res = smoke.run("https://dchub.cloud", "dallas", "k", session=FakeEdge(pdf=pdf), pause_s=0)
    v = verdicts(res)
    assert v["pro-pdf-downloads"] == smoke.PASS
    assert v["pro-pdf-never-publicly-cacheable"] == smoke.RED


def test_no_key_is_blind_and_touches_nothing():
    class Exploding:
        def get(self, *a, **k):
            raise AssertionError("the probe made a request without a key")
    res = smoke.run("https://dchub.cloud", "dallas", "", session=Exploding(), pause_s=0)
    assert verdicts(res) == {"paid-seat": smoke.BLIND}
    assert smoke.report(res) == 0


def test_a_key_that_is_not_a_pro_seat_is_blind_not_red():
    res = smoke.run("https://dchub.cloud", "dallas", "k",
                    session=FakeEdge(key_is_pro=False), pause_s=0)
    v = verdicts(res)
    assert v["pro-brief-renders-paid"] == smoke.BLIND
    assert v["anonymous-never-gets-the-paid-copy"] == smoke.BLIND
    assert v["anonymous-pdf-is-402"] == smoke.PASS
    assert smoke.RED not in v.values()


def test_an_unreachable_edge_is_blind_not_a_crash():
    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("edge down")
    res = smoke.run("https://dchub.cloud", "dallas", "k", session=Down(), pause_s=0)
    assert set(verdicts(res).values()) == {smoke.BLIND}


def test_markup_the_probe_does_not_recognise_is_never_a_verdict():
    assert smoke.render_kind(PAID_PAGE + ANON_PAGE) == "unrecognised"
    assert smoke.render_kind("<html></html>") == "unrecognised"
    assert smoke.verdict_anonymous_after_paid(200, "unrecognised")[0] == smoke.BLIND


def test_main_refuses_to_report_when_the_control_cannot_fire(monkeypatch):
    monkeypatch.setattr(smoke, "verdict_anonymous_after_paid", lambda status, kind: (smoke.PASS, "mutant"))
    monkeypatch.delenv("DCHUB_API_KEY", raising=False)
    assert smoke.main(["--base", "https://example.invalid"]) == 2
