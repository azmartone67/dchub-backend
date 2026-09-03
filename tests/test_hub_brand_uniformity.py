"""tests/test_hub_brand_uniformity.py — two hub pages shipped off-brand.

MEASURED LIVE 2026-09-03 (browser UA + cache-buster):

    page        dchub-brand.css   dchub-nav.js   hand-rolled <header>
    /markets    no                no             yes
    /facilities no                no             yes
    /dcpi       YES               YES            no

/dcpi is the canonical shell. /markets and /facilities render through
facilities_hub._shell, which links neither the brand sheet nor the shared nav
and hand-rolls its own header instead — so the two pages carry a different bar,
different link set and different type than the rest of the site.

/markets only started rendering here on 2026-09-01 (a5291907f, #3571) when it
moved off a static file that HAD both assets onto this renderer, which has
neither. That is why the finding appeared when it did.

★ THE COUPLING IS THE POINT. /js/dchub-nav.js:885 is
      document.body.insertBefore(navEl.firstChild, document.body.firstChild)
  — an UNCONDITIONAL prepend with no existing-header guard (verified by reading
  the served asset). So:
     · loading nav.js while the hand-rolled header is still there = TWO bars;
     · removing the header without loading nav.js = NO bar at all.
  Neither edit is safe alone. The invariant test below is written on the pair
  rather than on either line, so a future edit cannot half-apply it.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_hub_brand_uniformity.py -v
"""
from __future__ import annotations

import re

import pytest

import facilities_hub as fh


def _render(canonical="/markets"):
    return fh._shell("T", "D", canonical, "<nav>bc</nav>", "<p>body</p>")


def test_the_shell_links_the_canonical_brand_sheet():
    assert '/static/dchub-brand.css' in _render()


def test_the_shell_loads_the_shared_nav():
    h = _render()
    assert '<script src="/js/dchub-nav.js" defer></script>' in h
    assert h.index("dchub-nav.js") < h.index("</body>")


def test_the_hand_rolled_header_is_gone():
    h = _render()
    assert '<header class="header">' not in h, (
        "nav.js prepends unconditionally — leaving this produces two bars")


def test_the_pair_is_all_or_nothing():
    """★ THE INVARIANT, not the two lines. Exactly one of these states is
    shippable: shared nav loaded AND no hand-rolled header. The other three
    combinations are a double bar, no bar, or the off-brand page we started
    with."""
    h = _render()
    has_nav = '<script src="/js/dchub-nav.js"' in h
    has_own = '<header class="header">' in h
    assert has_nav and not has_own, (
        "nav.js loaded=%s, hand-rolled header present=%s — the only correct "
        "combination is (True, False)" % (has_nav, has_own))


def test_the_nav_is_loaded_exactly_once():
    """A second <script> tag would run the unconditional prepend twice."""
    h = _render()
    assert len(re.findall(r'<script[^>]+dchub-nav\.js', h)) == 1


def test_the_brand_sheet_is_linked_exactly_once():
    h = _render()
    assert len(re.findall(r'<link[^>]+dchub-brand\.css', h)) == 1


def test_the_brand_sheet_loads_after_the_font_sheet():
    """Brand tokens must be able to override the font sheet's defaults."""
    h = _render()
    assert h.index("fonts.googleapis.com/css2") < h.index("dchub-brand.css")


@pytest.mark.parametrize("canonical", ["/markets", "/facilities", "/facilities/us"])
def test_every_page_through_this_shell_gets_the_same_bar(canonical):
    """The finding was 'brand uniformity' — the fix is worthless if it only
    lands on the page that happened to be reported."""
    h = _render(canonical)
    assert "dchub-brand.css" in h and "dchub-nav.js" in h
    assert '<header class="header">' not in h


def test_the_page_still_renders_its_own_content():
    """CONTROL: removing the header must not have eaten the body."""
    h = _render()
    assert "<p>body</p>" in h and "<nav>bc</nav>" in h
    assert h.strip().startswith("<!doctype html>") and h.rstrip().endswith("</html>")
