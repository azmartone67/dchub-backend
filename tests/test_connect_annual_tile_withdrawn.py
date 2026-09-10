#!/usr/bin/env python3
"""The Pro Annual tile must not be advertised while it is not a saving.

NO NETWORK, NO DB — renders the page and reads what it serves.

MEASURED LIVE 2026-09-10, cache-busted, anonymous, on /connect/cursor:

    Pro Annual          Pro Monthly
    $1,188 /yr          $99 /mo

$1,188 is 12 x $99 EXACTLY. r-price-collapse (2026-09-05) had already
withdrawn Pro Annual for precisely this — both annual SKUs were priced off
the $299 monthly list, and tier_registry's own note calls selling one beside
a $99/mo button "an offer that punishes the buyer for taking it".

★ THE WITHDRAWAL ONLY REACHED HALF THE SURFACES. It landed in
ANNUAL_OPTIONS, so /api/v1/tiers stopped advertising annual, and never
reached routes/mcp_connect.py — so all 12 install pages kept rendering the
tile for five days. One decision, two consumers, one of them missed.

★ WHAT THIS PINS IS DERIVATION, NOT ABSENCE. A guard asserting "no annual
tile" would be satisfied forever by deleting the feature, and would fight
the owner the day they mint a real annual link. So it asserts BOTH
directions: gone at 12x monthly, back at $990 with a badge.

★ NOT A PRICE CHANGE. Nothing here creates, renames or re-prices a tier.
The Stripe link is untouched: an annual subscription already provisioned
keeps renewing, its URL keeps resolving, and /api/v1/connect/click still
honours plan=pro_annual. The offer is only un-advertised.
"""
import pathlib
import re
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_connect as mc  # noqa: E402


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(mc.mcp_connect_bp)
    return app.test_client()


def _page():
    return mc._render_page("cursor", 4242)


# ── the tile is gone at the price actually on the link ───────────────────

def test_no_annual_tile_at_twelve_times_monthly():
    html = _page()
    assert 'id="upg-annual"' not in html, (
        "the Pro Annual tile is advertised at 12 x the monthly price — a 0% "
        "discount sold beside the monthly button")
    assert "1,188" not in html


def test_the_monthly_tile_survives_and_is_derived():
    """FLOOR. Every assertion above is an absence, and an upgrade section
    that lost BOTH tiles would satisfy all of them while quietly deleting the
    only paid CTA on the install pages."""
    html = _page()
    assert 'id="upg-monthly"' in html
    assert "plan=pro_monthly" in html
    from tier_registry import price
    assert ">$%d<" % int(price("pro")) in html.replace(
        '<span style="font-size:.7em;color:var(--muted)">/mo</span>', "<")


def test_the_grid_collapses_to_one_column():
    """A 1fr 1fr grid with one tile leaves an empty column."""
    assert "upgrade-grid solo" in _page()
    assert ".upgrade-grid.solo{grid-template-columns:1fr}" in _page()


# ── …and comes BACK on its own at a real annual price ────────────────────

def test_a_real_annual_price_restores_the_tile(monkeypatch):
    """DERIVED, NOT DELETED — the restore path tier_registry documents is
    'mint a $990/yr link and put it back', with no code change beyond the
    number. If this fails, that promise is false."""
    monkeypatch.setattr(mc, "_ANNUAL_PRICE_USD", 990)
    html = _page()

    assert 'id="upg-annual"' in html, (
        "a genuine annual saving did not bring the tile back — the tile was "
        "deleted rather than derived, and the documented restore path is dead")
    assert re.search(r'\$990<span[^>]*>/yr', html)
    assert "plan=pro_annual" in html
    pct = re.search(r'<span class="save">(\d+)% off</span>', html)
    assert pct and int(pct.group(1)) == 17, (
        "the saving badge is computed against the live monthly price; "
        "$990 against 12 x $99 is 17%%, got %s" % (pct and pct.group(1)))
    assert "upgrade-grid solo" not in html, "two tiles — grid must not be solo"


def test_the_grid_modifier_tracks_the_tile(monkeypatch):
    """Non-vacuity: proves 'solo' is not simply always present."""
    assert "upgrade-grid solo" in _page()
    monkeypatch.setattr(mc, "_ANNUAL_PRICE_USD", 990)
    assert "upgrade-grid solo" not in _page()


# ── the parts that must NOT change ───────────────────────────────────────

def test_the_click_proxy_still_honours_plan_pro_annual(client):
    """An annual subscriber's live URL must keep resolving. Un-advertising an
    offer is not the same as revoking it, and this is the difference."""
    r = client.get("/api/v1/connect/click?platform=cursor&plan=pro_annual")
    assert r.status_code in (302, 303), r.status_code
    assert "stripe.com" in r.headers.get("Location", "")


def test_the_annual_stripe_link_is_left_intact():
    assert mc._STRIPE_ANNUAL and "stripe.com" in mc._STRIPE_ANNUAL


def test_the_mint_javascript_survives_a_missing_tile():
    """The JS re-points both hrefs after minting a key. Unguarded, a missing
    #upg-annual throws a TypeError and takes the REST of mintKey() with it —
    the ref-note and the mint-update POST that attributes the trial key back
    to this view. That would be a silent attribution regression, not a
    visible one."""
    src = mc._PAGE_TEMPLATE_RAW
    assert 'document.getElementById("upg-annual").href' not in src, (
        "unguarded href assignment on a tile that may not be rendered")
    assert "const annualEl" in src and "if (annualEl)" in src
