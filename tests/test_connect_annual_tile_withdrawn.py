#!/usr/bin/env python3
"""Install pages tile the plans agents buy, and never a Pro tile.

NO NETWORK, NO DB — renders the page and reads what it serves.

HISTORY. MEASURED LIVE 2026-09-10, cache-busted, anonymous, on /connect/cursor:

    Pro Annual          Pro Monthly
    $1,188 /yr          $99 /mo

$1,188 is 12 x $99 EXACTLY, a 0% "discount" sold beside the monthly button.
This file then pinned "derived, not deleted": the annual tile gone at 12 x
monthly, back on its own at a real annual price.

★ 2026-09-21 (P0-D, frontend#1535): OWNER DECISION. Agents buy the $10 pack or
Developer; Pro is the plan for a human screening sites and lives on /pricing.
Step 4 of every install page now tiles the pack and Developer, and no Pro tile,
monthly or annual. So the old restore promise ("mint a $990/yr link and the tile
comes back") is WITHDRAWN for this surface on purpose, and this file pins the new
rule in both directions: no Pro tile at 12 x monthly AND none at a real annual
price, with the paid tiles as the floor so an absence can never pass vacuously.

★ STILL UNCHANGED, and pinned below: the click proxy keeps honouring
plan=pro_annual and plan=pro_monthly, so a link already handed out keeps
resolving. The offer is un-advertised, not revoked.
"""
import pathlib
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


# ── no Pro tile, at any annual price ─────────────────────────────────────

def test_no_annual_tile_at_twelve_times_monthly():
    html = _page()
    assert 'id="upg-annual"' not in html
    assert "1,188" not in html


def test_no_pro_tile_even_at_a_real_annual_price(monkeypatch):
    """The other direction: a genuine annual saving no longer brings a Pro tile
    back to an install page (P0-D). /pricing is where Pro is sold."""
    monkeypatch.setattr(mc, "_ANNUAL_PRICE_USD", 990)
    html = _page()
    assert 'id="upg-annual"' not in html and 'id="upg-monthly"' not in html
    assert "plan=pro_annual" not in html and "plan=pro_monthly" not in html


def test_the_paid_tiles_survive_and_are_derived():
    """FLOOR. Every assertion above is an absence, and an upgrade section that
    lost every tile would satisfy all of them while deleting the only paid CTAs
    on the install pages."""
    html = _page()
    assert 'id="upg-pack"' in html and "plan=pack" in html
    assert 'id="upg-developer"' in html and "plan=developer" in html
    from routes.mcp_conversion_plays import PACK10_PRICE_CENTS, PACK10_CREDITS
    from tier_registry import price
    assert "$%d<span" % (int(PACK10_PRICE_CENTS) // 100) in html
    assert "%s API calls" % format(int(PACK10_CREDITS), ",") in html
    assert "$%d<span" % int(price("developer")) in html


# ── the parts that must NOT change ───────────────────────────────────────

@pytest.mark.parametrize("plan,link", [("pro_annual", "_STRIPE_ANNUAL"),
                                       ("pro_monthly", "_STRIPE_MONTHLY")])
def test_the_click_proxy_still_honours_the_pro_plans(client, plan, link):
    """A Pro link already handed out must keep resolving to ITS Payment Link.
    Un-advertising an offer is not the same as revoking it."""
    r = client.get("/api/v1/connect/click?platform=cursor&plan=" + plan)
    assert r.status_code in (302, 303), r.status_code
    assert r.headers.get("Location", "") == getattr(mc, link)


def test_the_annual_stripe_link_is_left_intact():
    assert mc._STRIPE_ANNUAL and "stripe.com" in mc._STRIPE_ANNUAL


def test_the_mint_javascript_survives_a_missing_tile():
    """The JS re-points the tiles' hrefs after minting a key. Unguarded, a
    missing tile throws a TypeError and takes the REST of mintKey() with it:
    the ref-note and the mint-update POST that attributes the trial key back to
    this view. That would be a silent attribution regression, not a visible one."""
    src = mc._PAGE_TEMPLATE_RAW
    for tile in ("upg-pack", "upg-developer", "upg-monthly", "upg-annual"):
        assert 'document.getElementById("%s").href' % tile not in src, tile
    assert "if (tileEl)" in src
