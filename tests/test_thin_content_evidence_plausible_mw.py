"""LANE 3's evidence test counted a capacity the page refuses to print.

r-mw-one-owner (2026-09-12), the half PR #4483 did not reach. That change gave
util.facility_headline.MW_PLAUSIBLE_MAX a single owner — `plausible_mw` — and
routed the six surfaces that PRINT a capacity through it: the SERP title, the
body stat tile, the narrative, thin_content.context_block's LANE-2 row, the
comparables peer annotation, and facility_measures (the inline Dataset JSON-LD
and the /facilities/<slug>.json twin).

util.thin_content.evidence was not one of them, and it is not a surface — it is
the VERDICT about the surfaces. It asked `_has(fac["power_mw"])`: "is there a
value", which is the question the six stopped asking.

★★★ THE MEASUREMENT (2026-09-12, re-derived from scratch, not inherited).

Rows: the exact UNION contentless_slug_set reads — discovered_facilities
(non-duplicate) plus facilities, 47,695 rows over 27,903 distinct slugs.
URLs: the published sitemap, fetched shard by shard — sitemap-facilities-1
(6,840, the family GSC and Bing read) and sitemap-ai-facilities-1/2 (18,880);
gated minus ai = 0, so the gated family is a strict subset, 18,880 distinct.

    rows whose ONLY evidence is power_mw               126
    ... and whose power_mw is above the cap             17
    slugs contentless BEFORE                        1,462
    slugs contentless AFTER                         1,479
    NEWLY contentless                                  17
    slugs that LEAVE the contentless set                0

All 17 are published in BOTH families. All 17 have city NULL, address NULL,
latitude NULL, longitude NULL. Their live pages, fetched one by one the same
day, render EXACTLY three stat tiles — Power / Status / Country — carry no geo
and no Address tile, say `<meta name="robots" content="index, follow">`, and
their LANE-2 block's only row is "Reported capacity". The rendered tile agreed
with the row 17/17. Suppress the capacity and Status + Country is the whole
page: precisely what LANE 3 exists to noindex.

    aep-none-0dc136e7            63,000 MW   nextera-…-none-77c70307  130,000 MW
    switch-none-f4215ae0         55,000 MW   dominion-…-none-402400e0  48,000 MW
    google-google-none-fb8621cf  30,000 MW   digitalbridge-none-…      20,800 MW
    … 11 more, down to stark-power-none-cc5795ce at 5,600 MW

★ A SAMPLE CANNOT MEASURE THIS. The base rate is 17/18,880 = 0.090%. Three
  independent 600-URL live draws returned 0, 0 and 0 hits — a per-draw rate of
  0.0000% against a true rate of 0.090%, i.e. the sample says the class is
  EMPTY. The number in this docstring comes from the full population, and the
  17 pages were then confirmed one at a time against the live HTML. Nothing
  here is extrapolated from a draw.

★ WHAT THIS FILE DOES NOT ASSERT. Not "17", and not 1,462/1,479. Those move
  with the corpus — a row gaining a city, an ingest run, a corrected capacity —
  and a test that pins them fails on a day when nothing is wrong. What is
  pinned is the INVARIANT: the verdict and the surfaces answer the same
  question, and no page with any other fact leaves the index.
"""
import ast
import importlib
import io
import pathlib

import pytest

from util.facility_headline import MW_PLAUSIBLE_MAX, display_mw, plausible_mw
from util.thin_content import (contentless_slug_set, context_block, evidence,
                               is_contentless)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The measured shape of all 17, field for field, as contentless_slug_set reads
# them: nothing but a fleet-sized capacity. aep-none-0dc136e7's own row.
FLEET_ONLY = {"city": None, "address": None, "latitude": None,
              "longitude": None, "power_mw": 63000.0,
              "status": "Planned", "country": "US"}
# A real single site. 350 MW is under the cap and always was.
REAL_SITE = dict(FLEET_ONLY, power_mw=350.0)


class TestTheVerdictAsksThePredicate:
    """`power` means "a capacity this page will print", not "a column with
    something in it"."""

    def test_an_implausible_capacity_is_not_evidence(self):
        assert evidence(FLEET_ONLY)["power"] is False

    def test_a_plausible_capacity_is_still_evidence(self):
        assert evidence(REAL_SITE)["power"] is True

    def test_the_page_that_renders_only_status_and_country_is_contentless(self):
        """The whole point: LANE 3's verdict now matches the rendered page."""
        assert is_contentless(FLEET_ONLY) is True

    def test_the_lane_2_block_that_page_would_render_is_empty(self):
        """Confirms "nothing but Status + Country" end to end. context_block
        returns '' rather than a header over an empty body, so the suppressed
        capacity does not leave a "Market & grid context" section behind."""
        assert context_block(FLEET_ONLY, None) == ""

    def test_a_real_site_is_still_indexable_on_its_capacity_alone(self):
        assert is_contentless(REAL_SITE) is False

    @pytest.mark.parametrize("mw", [5000.0, 4999.9, 650.0, 350.0, 36, 2.5, 0.4])
    def test_capacities_at_and_under_the_cap_keep_their_page(self, mw):
        assert is_contentless(dict(FLEET_ONLY, power_mw=mw)) is False

    @pytest.mark.parametrize("mw", [5000.1, 5600.0, 8000, 17000, 63000.0,
                                    130000.0, 150000.0])
    def test_capacities_above_the_cap_no_longer_hold_a_page_open(self, mw):
        assert is_contentless(dict(FLEET_ONLY, power_mw=mw)) is True


class TestNothingWithRealContentLeaves:
    """A page dropping out that still has content is a regression, not a win.
    One implausible capacity plus ANY other fact keeps the page."""

    @pytest.mark.parametrize("extra", [
        {"city": "Columbus"},
        {"address": "1 Riverside Plaza"},
        {"latitude": 39.96, "longitude": -83.0},
    ])
    def test_any_other_fact_outranks_a_suppressed_capacity(self, extra):
        assert is_contentless(dict(FLEET_ONLY, **extra)) is False

    def test_a_placeholder_city_is_still_not_a_fact(self):
        """'None'/'Regional' were never evidence and this change must not
        promote them into some — they are the reason the 17 have no city."""
        assert is_contentless(dict(FLEET_ONLY, city="None")) is True
        assert is_contentless(dict(FLEET_ONLY, city="Regional")) is True

    def test_the_other_three_evidence_fields_are_untouched(self):
        """Only `power` moved. A change that also tightened city, address or
        coordinates would de-index pages this measurement never looked at."""
        rich = {"city": "Columbus", "address": "1 Riverside Plaza",
                "latitude": 39.96, "longitude": -83.0, "power_mw": 63000.0}
        assert evidence(rich) == {"power": False, "coords": True,
                                  "address": True, "city": True}

    def test_a_page_with_no_capacity_at_all_is_unchanged(self):
        """The 408-row LANE-3 population this module was built for."""
        assert is_contentless(dict(FLEET_ONLY, power_mw=None)) is True
        assert is_contentless({"city": "Columbus", "power_mw": None}) is False


class TestOneOwner:
    """The comparison lives in plausible_mw and nowhere else. A second copy of
    `<= MW_PLAUSIBLE_MAX` is the defect this whole rune is named for."""

    def _evidence_src(self):
        """evidence's CODE, with the docstring dropped.

        ★ The docstring NAMES `MW_PLAUSIBLE_MAX` — it has to, to say where the
          comparison lives — and the first draft of this guard matched its own
          prose and went red on a correct implementation. Prose that explains a
          rule is not the rule. Dropped by AST node, not by a regex over the
          text, so a docstring that grows a code example cannot re-arm it.
        """
        src = io.open(ROOT / "util" / "thin_content.py", encoding="utf-8").read()
        fn = [n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "evidence"]
        assert fn, "util.thin_content.evidence is gone or was renamed"
        body = list(fn[0].body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        assert body, "evidence has a docstring and no code"
        return "\n".join(ast.unparse(n) for n in body)

    def test_evidence_does_not_re_spell_the_cap(self):
        assert "MW_PLAUSIBLE_MAX" not in self._evidence_src(), (
            "evidence compares against the cap itself — a second spelling is "
            "exactly how the cap came to be applied on one surface of six")

    def test_evidence_asks_the_predicate(self):
        assert "plausible_mw" in self._evidence_src()

    def test_power_is_no_longer_a_bare_presence_check(self):
        """`_has` answers 'is there a value'. The six printing surfaces stopped
        asking that; the verdict about them must stop too."""
        src = self._evidence_src()
        assert "_has(fac.get('power_mw'))" not in src
        assert '_has(fac.get("power_mw"))' not in src

    def test_the_verdict_and_the_printed_capacity_cannot_disagree(self):
        """THE COHERENCE INVARIANT, checked across the boundary and not just
        near it: evidence counts a capacity exactly when a surface prints one.
        display_mw is the independent witness — it reached the cap first and by
        a different route."""
        for v in (0, 0.0, 0.4, 1, 36, 350, 4999.9, MW_PLAUSIBLE_MAX,
                  MW_PLAUSIBLE_MAX + 0.1, 5600, 8000, 17000, 63000.0, 150000,
                  None, "", "x", True, False, float("nan"), float("inf")):
            assert evidence({"power_mw": v})["power"] is bool(display_mw(v)), v
            assert (evidence({"power_mw": v})["power"]
                    is (plausible_mw(v) is not None)), v


class _Cursor:
    """The two-column-family cursor contentless_slug_set executes against:
    (canonical_slug, city, address, latitude, longitude, power_mw)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return None

    def fetchall(self):
        return list(self._rows)


def _row(slug, *, city=None, address=None, lat=None, lon=None, mw=None):
    return (slug, city, address, lat, lon, mw)


class TestTheSitemapDelta:
    """contentless_slug_set is what the sitemap builder and the page's robots
    tag both read, so this is where the delta is actually spent."""

    def test_the_fleet_row_joins_the_noindex_set(self):
        rows = [_row("aep-none-0dc136e7", mw=63000.0)]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(40)]
        assert "aep-none-0dc136e7" in contentless_slug_set(_Cursor(rows))

    def test_a_real_capacity_keeps_its_slug_out_of_the_set(self):
        rows = [_row("site-350", mw=350.0)]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(40)]
        assert "site-350" not in contentless_slug_set(_Cursor(rows))

    def test_a_slug_kept_alive_by_a_SIBLING_row_is_not_dropped(self):
        """One URL, several rows: the richest row serves the page, and that
        page is not noindexed. `out -= has_content` is the line under test —
        the 17 measured are single-row, so this case has no live member today
        and would otherwise ship unexercised."""
        rows = [_row("twin", mw=63000.0), _row("twin", city="Columbus")]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(40)]
        assert "twin" not in contentless_slug_set(_Cursor(rows))

    def test_the_refusal_floor_still_refuses_an_implausible_result(self):
        """Returning an empty set means 'emit everything'. A corpus where the
        evidence columns went missing must trip it, unchanged by this work."""
        rows = [_row("gone-%d" % i, mw=63000.0) for i in range(40)]
        assert contentless_slug_set(_Cursor(rows)) == set()

    def test_the_floor_is_not_tripped_at_the_measured_ratio(self):
        """Live 2026-09-12: 1,479 of 47,695 rows, ~3%. The floor refuses above
        25%, so this change (17 slugs, +0.04pp) is nowhere near it."""
        rows = [_row("thin-%d" % i, mw=63000.0) for i in range(30)]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(970)]
        out = contentless_slug_set(_Cursor(rows))
        assert len(out) == 30

    def test_a_dead_cursor_still_means_emit_everything(self):
        class _Boom:
            def execute(self, *_a, **_k):
                raise RuntimeError("no db")

            def fetchall(self):
                raise RuntimeError("no db")

        assert contentless_slug_set(_Boom()) == set()


@pytest.mark.parametrize("mod,name", [
    ("util.facility_headline", "plausible_mw"),
    ("util.thin_content", "evidence"),
    ("util.thin_content", "is_contentless"),
])
def test_the_predicate_is_importable_where_the_callers_look(mod, name):
    """evidence imports plausible_mw at call time (util.facility_headline
    imports this module back, at its own function level). A NameError there
    surfaces as a request-time 500 on the facility page, not an import error
    at boot."""
    assert callable(getattr(importlib.import_module(mod), name))
