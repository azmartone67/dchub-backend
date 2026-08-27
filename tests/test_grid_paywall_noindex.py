"""The sitemap asked Google to index five 38-word paywall interstitials.

r-grid-paywall-noindex (2026-08-27).

/grid/<iso> renders a full page for the free-tier ISOs (PJM, ERCOT) and a
paywall interstitial for the other five. All seven are listed in the sitemap,
so Google was being asked to index five pages that differ only in the ISO name
and carried NO canonical at all. Measured live:

    /grid/pjm     6,433 b   106 words   canonical present
    /grid/ercot   6,531 b   109 words   canonical present
    /grid/caiso   2,939 b    38 words   canonical NONE
    /grid/isone   /grid/miso   /grid/nyiso   /grid/spp   — same stub

A near-identical cluster with no self-selected representative is exactly the
"Duplicate without user-selected canonical" shape, and at 38 words each they
are also "Crawled — currently not indexed" candidates. /grid was the thinnest
page class on the whole site: median 3,294 bytes against 16,782 for /markets
and 40,294 for /dcpi.

★ THE FIX IS noindex, NOT A CANONICAL AND NOT A SITEMAP EDIT.
  - A canonical would only move the cluster to "Google chose a different
    canonical". The interstitial has nothing to represent.
  - Pruning the sitemap would DRIFT: FREE_TIER_ISOS is the single source of
    truth for which ISOs are paid, and a hand-maintained sitemap exclusion
    would go stale the moment a tier changes. noindex flips automatically
    with the gate, and Google reports "Excluded by noindex" — the honest
    state for a page you deliberately do not want indexed.

★★ `follow`, not `nofollow`. The interstitial exists to send the reader to
   /pricing; that link should still carry equity.

★★★ THE DANGEROUS DIRECTION IS THE FREE PAGES. A noindex that leaks onto
    /grid/pjm or /grid/ercot would delist two real, substantive pages. That is
    what test_the_free_tier_pages_are_never_noindexed exists to catch, and it
    is the test to keep if any other is ever dropped.
"""
import pytest

pytest.importorskip("flask")


def _client(monkeypatch, tier="free"):
    from flask import Flask

    import routes.grid_public_routes as g

    monkeypatch.setattr(g, "_user_tier", lambda req: tier)
    monkeypatch.setattr(g, "_fetch_live", lambda iso: {}, raising=False)
    app = Flask(__name__)
    app.register_blueprint(g.grid_public_bp)
    return app.test_client(), g


PAID = ["caiso", "isone", "miso", "nyiso", "spp"]


@pytest.mark.parametrize("iso", PAID)
def test_the_paywall_interstitial_is_noindexed(monkeypatch, iso):
    cl, _ = _client(monkeypatch)

    r = cl.get(f"/grid/{iso}")
    body = r.data.decode("utf-8", "replace")

    assert r.status_code == 200, f"/grid/{iso} answered {r.status_code}"
    assert "Pro Tier Required" in body, (
        f"/grid/{iso} did not render the paywall — this test is not exercising "
        "the gated path and proves nothing")
    assert 'content="noindex, follow"' in body, (
        f"/grid/{iso} is a 38-word interstitial asking to be indexed again; "
        "five of these differ only in the ISO name")


@pytest.mark.parametrize("iso", PAID)
def test_the_header_carries_it_too(monkeypatch, iso):
    """Belt and braces: the meta needs the body parsed, the header does not."""
    cl, _ = _client(monkeypatch)

    r = cl.get(f"/grid/{iso}")

    assert r.headers.get("X-Robots-Tag") == "noindex, follow", (
        f"/grid/{iso} X-Robots-Tag is {r.headers.get('X-Robots-Tag')!r}")


@pytest.mark.parametrize("iso", ["pjm", "ercot"])
def test_the_free_tier_pages_are_never_noindexed(monkeypatch, iso):
    """★ The direction that would actually cost traffic.

    PJM and ERCOT are substantive pages a free visitor is meant to reach and
    Google is meant to index. If the noindex ever leaks past the gate, two
    real pages go dark and nothing else in this file would notice.
    """
    cl, _ = _client(monkeypatch)

    r = cl.get(f"/grid/{iso}")
    body = r.data.decode("utf-8", "replace")

    assert "Pro Tier Required" not in body, (
        f"/grid/{iso} is free-tier but rendered the paywall — FREE_TIER_ISOS "
        "no longer covers it")
    assert "noindex" not in body.lower(), (
        f"/grid/{iso} carries a noindex — a real, indexable page just went dark")
    assert "noindex" not in (r.headers.get("X-Robots-Tag") or "").lower(), (
        f"/grid/{iso} carries an X-Robots-Tag noindex header")


def test_the_gate_is_still_the_single_source_of_truth(monkeypatch):
    """A paid visitor must get the real page, not the interstitial."""
    cl, g = _client(monkeypatch, tier="pro")

    r = cl.get("/grid/caiso")
    body = r.data.decode("utf-8", "replace")

    assert "Pro Tier Required" not in body, (
        "a pro caller got the paywall — the gate, not the noindex, decides")
    assert g.FREE_TIER_ISOS == {"PJM", "ERCOT"}, (
        f"FREE_TIER_ISOS is now {g.FREE_TIER_ISOS} — the paid list above and "
        "the sitemap's expectations both move with it")
