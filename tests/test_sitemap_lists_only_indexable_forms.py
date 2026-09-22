"""The sitemap may only list the form of a page Google is asked to index.

2026-09-22, measured live against https://dchub.cloud (135-URL stratified
sample of all eight sitemap shards, redirects not followed):

  sitemap-press.xml  165 URLs, every one /press-release/<slug>. The page there
                     declares rel=canonical /news/<slug>, and GSC's stored page
                     grain shows /news/* in results and no /press-release/*.
                     So all 165 were "Alternate page with proper canonical tag".
  sitemap-static.xml /grid/caiso /grid/miso /grid/nyiso /grid/isone /grid/spp:
                     the Pro interstitial, `X-Robots-Tag: noindex, follow`.
                     A noindex URL in a sitemap is "Submitted URL marked
                     noindex".

Both lists are hand-typed or hand-composed, so each is pinned to the thing that
decides the answer: the press <loc> to the canonical every other emitter
already uses (media hub, dcpi_auto_press, marketing_engine's IndexNow call),
and the /grid/<iso> entries to routes.grid_public_routes.FREE_TIER_ISOS — the
set the paywall itself reads. If a tier changes, this test fails and names the
sitemap line to add or drop; the noindex follows the gate on its own.

CI-SAFETY: source reads only (ast + text), no flask, no database.
"""
import ast
import os
import re

from tests.test_sitemap_covers_linked_pages import MAIN, _static_pages

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID = os.path.join(REPO, "routes", "grid_public_routes.py")


def _grid_sets():
    """(all ISO codes, free ISO codes) as grid_public_routes declares them."""
    with open(GRID, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    isos = free = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name == "ISOS":
                isos = {k.value for k in node.value.keys}
            elif name == "FREE_TIER_ISOS":
                free = {e.value for e in node.value.elts}
    return isos, free


def test_grid_sets_are_actually_parsed():
    """NON-VACUITY: a renamed or reshaped literal would make every assertion
    below compare empty sets and pass."""
    isos, free = _grid_sets()
    assert isos and len(isos) >= 7, f"ISOS not parsed from {GRID}: {isos}"
    assert free and free < isos, f"FREE_TIER_ISOS not parsed or not a subset: {free}"


def test_sitemap_lists_exactly_the_free_grid_isos():
    isos, free = _grid_sets()
    listed = {p.rsplit("/", 1)[1].upper() for p in _static_pages()
              if re.fullmatch(r"/grid/[a-z]+", p) and p.rsplit("/", 1)[1].upper() in isos}
    gated = sorted(listed - free)
    missing = sorted(free - listed)
    assert not gated, (
        f"static_pages lists /grid/<iso> for {gated}, which anonymous callers get "
        f"as the noindex Pro interstitial. Drop the line, or make the ISO free "
        f"in FREE_TIER_ISOS first.")
    assert not missing, (
        f"{missing} are free (they render the full page) but static_pages does "
        f"not list /grid/<iso> for them.")


def _press_block():
    with open(MAIN, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("sections['press'].append(")
    return src[i:src.index("_press_added += 1", i)]


def test_press_sitemap_lists_the_news_canonical():
    block = _press_block()
    locs = re.findall(r"<loc>([^<]+)</loc>", block)
    assert locs == ["https://dchub.cloud/news/{_pslug}"], (
        f"sitemap-press.xml <loc> is {locs}. /press-release/<slug> declares "
        f"rel=canonical /news/<slug>; list the canonical, not the alternate.")
