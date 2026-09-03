#!/usr/bin/env python3
"""The two pages that tell you to configure Cloudflare must hand you a Cloudflare URL.

NO NETWORK, NO DB — source-shape test, the same house pattern as
tests/test_integrations_pages_are_discoverable.py.

WHY THIS GUARD EXISTS (2026-09-03)
==================================
/integrations/cloudflare shipped a step-by-step recipe whose first instruction was
"In the Cloudflare dashboard open Zero Trust > Access controls > AI controls" — with
nothing to click. /integrations/mcp listed "Cloudflare MCP Server Portal" in its
platform run. Between them the two pages contained ZERO outbound Cloudflare URLs,
while every peer quickstart card on the same page carried a vendor deep link
(Claude -> claude.ai/settings/connectors). Proven by:

    curl -s https://dchub.cloud/integrations/{mcp,cloudflare} \
      | grep -oE 'href="[^"]*cloudflare\\.com[^"]*"'      # -> no output, both

★ THE LINK IS THE PLAIN CONSOLE URL ON PURPOSE. Cloudflare's own docs for this
feature link https://dash.cloudflare.com/ and give the nav path as prose — they
publish no ?to= deep link. Zero Trust also moved off one.dash.cloudflare.com, and
that host 301s to dash.cloudflare.com/one/ DROPPING the ?to= target, so a deep link
written the old way silently lands on the wrong screen. Matching Cloudflare beats
inventing a link we cannot verify.

★ EACH BLOB IS SLICED, NOT GREPPED WHOLE-FILE. A bare `"dash.cloudflare.com" in
source` would pass forever on one page's link while the other page has none — which
is the exact shape of the bug. The negative assertions below prove each slice really
is bounded to its own page.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES = os.path.join(ROOT, "routes", "integrations_landing.py")

CF_DASH = 'href="https://dash.cloudflare.com/"'
CF_DOCS = "developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/"

# Marks the end of a top-level HTML constant: the next module-level assignment
# or decorator in the file.
_NEXT_TOP_LEVEL = re.compile(r"(?m)^(?:[A-Z_]+ = |_[a-z_]+ = |@|def )")


def _source():
    with open(ROUTES, encoding="utf-8") as fh:
        return fh.read()


def _blob(name):
    """The source text of one top-level HTML constant, and nothing after it."""
    src = _source()
    starts = [m.start() for m in re.finditer(r"(?m)^%s = " % re.escape(name), src)]
    assert len(starts) == 1, f"{name} is assigned {len(starts)}x — slice is ambiguous"
    start = starts[0]
    nxt = _NEXT_TOP_LEVEL.search(src, start + len(name) + 4)
    assert nxt, f"no top-level statement follows {name} — slice would run to EOF"
    blob = src[start:nxt.start()]
    assert len(blob) > 2000, f"{name} slice is only {len(blob)} bytes — boundary is wrong"
    return blob


def test_the_cloudflare_recipe_links_the_cloudflare_dashboard():
    blob = _blob("CLOUDFLARE_PORTAL_RECIPE_HTML")
    assert CF_DASH in blob, (
        "/integrations/cloudflare tells the reader to open the Cloudflare dashboard "
        "but gives them no link to it"
    )
    assert CF_DOCS in blob, (
        "/integrations/cloudflare documents Cloudflare's MCP server portals without "
        "citing Cloudflare's own reference for them"
    )


def test_the_main_mcp_page_links_the_cloudflare_dashboard():
    blob = _blob("MCP_LANDING_HTML")
    assert CF_DASH in blob, (
        "/integrations/mcp names the Cloudflare MCP Server Portal but carries no "
        "Cloudflare-side URL, unlike every peer quickstart card on the same page"
    )


def test_the_two_slices_are_actually_different_pages():
    """Non-vacuity: proves neither assertion above is reading the other page."""
    mcp = _blob("MCP_LANDING_HTML")
    cf = _blob("CLOUDFLARE_PORTAL_RECIPE_HTML")
    assert CF_DOCS not in mcp, "MCP_LANDING_HTML slice is bleeding into the recipe page"
    assert "CLOUDFLARE_PORTAL_RECIPE_HTML" not in mcp
    assert "60-second quickstarts" not in cf, "recipe slice is bleeding into the landing page"


def test_no_stale_one_dash_deep_links():
    """one.dash.cloudflare.com 301s to dash.cloudflare.com/one/ and drops ?to=."""
    src = _source()
    bad = re.findall(r'href="https://one\.dash\.cloudflare\.com[^"]*"', src)
    assert not bad, f"stale Zero Trust deep link(s) that lose their target: {bad}"

def _quickstart_grid():
    """The <div class="qs"> card grid near the TOP of MCP_LANDING_HTML."""
    blob = _blob("MCP_LANDING_HTML")
    i = blob.index('<div class="qs">')
    j = blob.index("<h2>Agent recipes", i)
    grid = blob[i:j]
    assert grid.count('<div class="qs-card">') >= 6, (
        "quickstart grid lost its cards — slice boundary is wrong"
    )
    return grid


def test_cloudflare_has_a_quickstart_card_not_just_a_tail_link():
    """PRESENT IS NOT VISIBLE (2026-09-03).

    The first fix put the Cloudflare links on the page and every assertion
    passed — but the entry rendered at 44,889px down a 49,689px page (90%),
    as the last of ~20 undifferentiated text links, while the quickstart
    cards sit at 4,291px. Measured in the live DOM:

        [...document.querySelectorAll('a')]
          .find(x => x.href.includes('integrations/cloudflare'))
          .getBoundingClientRect().top + scrollY   // -> 44889 of 49689

    A guard that only asks "is the href in the file" cannot tell those two
    outcomes apart. This one pins it to the card grid.
    """
    grid = _quickstart_grid()
    assert "integrations/cloudflare" in grid, (
        "Cloudflare is missing from the quickstart card grid — it is back to "
        "being a tail link 90% down the page, which is why nobody sees it"
    )
    assert "Cloudflare Zero Trust" in grid, "the Cloudflare card lost its heading"


def test_the_quickstart_pane_does_not_miscount_its_own_cards():
    """The pane claimed 'the six biggest agent platforms'; there are now seven
    cards. A stale hard-coded count is the cheapest kind of lie on the page."""
    blob = _blob("MCP_LANDING_HTML")
    n = _quickstart_grid().count('<div class="qs-card">')
    assert "six biggest agent platforms" not in blob, (
        f"heading still claims six platforms but the grid renders {n} cards"
    )
    assert "These six platforms drive" not in blob, (
        f"sub-heading still says 'These six platforms' but the grid renders {n} cards"
    )
