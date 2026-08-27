"""'UNK' is a sentinel meaning "no operator", and it was being printed.

r-iso-unk (2026-08-27).

Every SQL predicate in this codebase that counts markets without a grid
operator reads `iso IS NULL OR iso = '' OR iso = 'UNK'` — main.py in three
places, dchub_self_heal.py, and the note at the top of util/iso_taxonomy.py.
The RENDERERS were the exception: a bare truthy test, so four scored markets
carrying the literal string put it straight into a page title.

    Universal Telecom Unitelco SP — Sao Paulo, SP, BR  | UNK grid | DC Hub
    Lepida S.c.p.A. BOIX — Bologna, IT, IT             | UNK grid | DC Hub
    Bunker One — Midrand, GP, ZA                       | UNK grid | DC Hub
    Vodacom — Midrand, GP, ZA                          | UNK grid | DC Hub

Measured 2026-08-27 over a random 500 of the 9,095 sitemap facility pages:
5 pages, ~91 across the sitemap, later 4/~73 once market resolution settled.
The four markets are barueri, bologna, midrand and osasco.

★ THE FIX IS NOT TO INVENT A LABEL. The first attempt added ONS (Brazil) and
  ESKOM (South Africa) to MARKET_ISO_OVERRIDES and broke five existing tests,
  which was the codebase saying no in the correct places:

    - MARKET_ISO_OVERRIDES values must each declare a taxonomy class in
      ISO_TYPE, and must CONTRADICT their state default. It is a map for US
      split-state metros, not a place to name foreign operators.
    - tests/test_dcpi_orphan_geography.py pins johannesburg to iso == ""
      with the reason "midrand convention: no registered grid-operator
      label". An empty iso here is a DECISION, and johannesburg already
      renders with no grid clause at all.

  So the defect was never the missing label — it was 'UNK' not being treated
  the way '' already is. This makes them behave identically.

★★ is_registered_label() is deliberately NOT iso_type_of(). A market can carry
   a real operator with no taxonomy class — EirGrid, TNB, EGAT, ENTSOE-IT are
   all absent from ISO_TYPE — and every one of them must still display.
"""
import os
import re

import pytest

from util.iso_taxonomy import is_registered_label

FPP = "routes/facility_profile_page.py"
DCPI = "routes/dcpi.py"


def _src(rel):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, rel), encoding="utf-8") as fh:
        return fh.read()


# ── the sentinel itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["UNK", "unk", " Unk ", "", "   ", None])
def test_sentinel_and_empty_are_both_hidden(value):
    assert is_registered_label(value) is False, (
        f"{value!r} is not an operator — rendering it puts a placeholder in a "
        "page title, which is what this rule exists to stop")


@pytest.mark.parametrize("value", [
    "PJM", "MISO", "ERCOT", "CAISO", "SERC",        # US, in ISO_TYPE
    "ENTSOE-IT", "ENTSOE-DE", "EirGrid", "TNB",     # real, absent from ISO_TYPE
    "EGAT", "AEMO", "POSOCO", "TEPCO", "CENACE",
])
def test_real_operators_still_display(value):
    assert is_registered_label(value) is True, (
        f"{value!r} is a real operator that happens to have no taxonomy class; "
        "hiding it would strip the grid from thousands of correct pages")


def test_it_is_not_a_synonym_for_having_a_taxonomy_class():
    """The two questions must stay separate.

    If someone 'simplifies' this to iso_type_of(iso) != '', every non-US label
    disappears from every title at once — 60-odd markets.
    """
    from util.iso_taxonomy import iso_type_of

    assert iso_type_of("EirGrid") == "", "premise: EirGrid has no class here"
    assert is_registered_label("EirGrid") is True, (
        "is_registered_label collapsed into iso_type_of — every non-US "
        "operator would vanish from every page title")


def test_the_midrand_convention_is_untouched():
    """An EMPTY iso stays hidden, and no label was invented for those markets."""
    from util.iso_taxonomy import MARKET_ISO_OVERRIDES

    assert is_registered_label("") is False
    for slug in ("johannesburg", "midrand", "barueri", "osasco", "bologna"):
        assert slug not in MARKET_ISO_OVERRIDES, (
            f"{slug} was given an invented operator label in "
            "MARKET_ISO_OVERRIDES — that map is for US split-state metros, and "
            "tests/test_dcpi_orphan_geography.py pins the empty-iso decision")


# ── the render sites ──────────────────────────────────────────────────────

def test_the_facility_page_gates_every_iso_render_on_the_helper():
    """A bare truthy test on iso is how 'UNK grid' reached the <title>."""
    src = _src(FPP)

    assert "is_registered_label" in src, f"{FPP} no longer imports the helper"

    bare = re.findall(r'if\s+_?dcpi\.get\("iso"\)\s*:', src)
    assert not bare, (
        f"{FPP} has {len(bare)} bare truthy test(s) on iso again — the "
        "sentinel 'UNK' passes those and lands in the page title")


def test_the_dcpi_page_title_excludes_the_sentinel():
    src = _src(DCPI)

    assert "{% if s.iso %} · {{ s.iso }} grid{% endif %}" not in src, (
        "the DCPI page title is back to a bare truthy test on iso, so the four "
        "UNK markets print '· UNK grid' in their own titles")
    assert "s.iso != 'UNK'" in src, (
        "the DCPI title template no longer excludes the sentinel")
