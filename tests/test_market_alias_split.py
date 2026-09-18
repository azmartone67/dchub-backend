"""r-market-alias-split (2026-09-18) — one metro rendered as two markets.

`discovered_facilities.market` is free text from ingestion, not a slug
vocabulary, and `_section_market_concentration` / `operators.top_markets`
both `GROUP BY COALESCE(market, city, '')`. So the same metro arrives under
several spellings and is published as several rows. Measured live against
production on 2026-09-18 with the route's own `is_duplicate = 0` predicate:

    Equinix          'Frankfurt'          20 facilities  240.0 MW
    Equinix          'Frankfurt Am Main'   4 facilities   36.0 MW
    Digital Realty   'Frankfurt'          17 facilities   80.0 MW
    Digital Realty   'Frankfurt Am Main'  12 facilities  132.0 MW
    Digital Realty   'Frankfurt am Main'   1 facility      0.0 MW

Digital Realty's Frankfurt position — 30 facilities / 212 MW — was published
as THREE rows, two differing only in the case of "am". Each row's share_pct
is taken against the operator's whole footprint, so each understates the
metro, and each mints a different `market_slug`.

★ THE SLUGS WERE THE WORSE HALF. `/markets/<slug>/brief` answers **HTTP 200
for any slug at all**, rendering a shell whose only market-specific text is
the slug echoed into the <h1>. Measured live the same day:

    /markets/frankfurt/brief          <title>Frankfurt Market Brief · DC Hub</title>
    /markets/frankfurt-am-main/brief  <title>Market Brief · DC Hub</title>
    /markets/las-cruces/brief         <title>Market Brief · DC Hub</title>
    /markets/pennsylvania/brief       <title>Market Brief · DC Hub</title>

A status code therefore cannot tell a market page from a dead one. Of the
2,267 distinct slugs the operator briefs linked to, 2,002 (88.3%) had no
published `market_power_scores` row — 35,696 MW of linked capacity pointing
at empty shells.

★ NO DERIVED RULE IS SAFE, which is why the alias map is hand-curated and
why the refusals below are pinned. A leading-whole-token containment rule
folds Frankfurt correctly but, measured over the same published values,
also folds a state into a city and two countries into each other.
"""
import re

import pytest


def _flatten_sql(sql: str) -> str:
    """Flatten a SQL literal for matching, with `-- ...` comments removed.

    Matching the RAW text makes a comment that QUOTES the statement
    indistinguishable from the statement itself. routes/operator_brief.py
    documents the 2-arg `GROUP BY COALESCE(market, city)` it used to carry
    (it raised GroupingError, so the section published nothing); an
    unstripped match dispatched on that PROSE while the real statement had
    already moved to the 3-arg form — the fake cursor answered a query the
    route no longer sends, and the test passed for the wrong reason.
    """
    bare = "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())
    return " ".join(bare.split())

from util.market_aliases import (
    FACILITY_MARKET_ALIASES,
    canonical_market_slug,
    market_group_key,
)

# Every target of the curated map was confirmed `published = true` in
# market_power_scores on 2026-09-18. An alias pointing at an unpublished
# slug moves a metro's facilities onto a page that renders empty.
PUBLISHED_ON_2026_09_18 = frozenset({
    'frankfurt', 'dc', 'minneapolis', 'raleigh', 'sydney', 'piscataway',
    'jakarta', 'osaka', 'hong kong', 'quincy', 'birmingham', 'ashburn',
    'santa-clara', 'sao-paulo', 'dallas', 'columbus', 'mexico-city',
})


# ── the split itself ──────────────────────────────────────────────────
@pytest.mark.parametrize("spelling", [
    "Frankfurt", "Frankfurt Am Main", "Frankfurt am Main",
    "frankfurt-am-main", "FRANKFURT AM MAIN", "Frankfurt  Am  Main",
])
def test_every_frankfurt_spelling_folds_to_one_market(spelling):
    assert market_group_key(spelling) == "frankfurt"


def test_the_three_measured_digital_realty_rows_become_one():
    """The exact rows production published, folded."""
    rows = [("Frankfurt", 17, 80.0), ("Frankfurt Am Main", 12, 132.0),
            ("Frankfurt am Main", 1, 0.0)]
    keys = {market_group_key(m) for m, _, _ in rows}
    assert keys == {"frankfurt"}, "three spellings must collapse to one key"
    assert sum(n for _, n, _ in rows) == 30
    assert sum(mw for _, _, mw in rows) == 212.0


def test_accent_and_case_only_variants_fold():
    """São Paulo/Sao Paulo was the largest pure-spelling split: 195 rows."""
    assert market_group_key("São Paulo") == market_group_key("Sao Paulo")
    assert market_group_key("Zurich") == market_group_key("Zürich")
    assert market_group_key("Dallas") == market_group_key("DALLAS")


# ── the refusals: measured false merges a derived rule would have made ──
@pytest.mark.parametrize("a,b,why", [
    ("Colorado", "Colorado Springs", "a state and a city inside it"),
    ("Mexico", "Mexico City", "a country and its capital"),
    ("Porto", "Porto Alegre", "Portugal vs Brazil"),
    ("Santiago", "Santiago De Chile", "Dominican Republic vs Chile"),
    ("Santiago", "Santiago de Cali", "Dominican Republic vs Colombia"),
    ("Texas", "Texas Regional", "'Texas Regional' is a state rollup"),
    ("Manchester", "Manchester UK", "New Hampshire vs England"),
    ("Washington", "Washington State", "the DC metro vs the state"),
    ("Ashburn", "Ashburn III", "a facility name, not a market"),
    ("Lagos", "Lagos Island", "Lagos Island is not the Lagos metro row"),
])
def test_containment_lookalikes_stay_separate_markets(a, b, why):
    """A leading-token containment rule folds all of these. It must not.

    These are the pairs that rule actually produced over the published
    values on 2026-09-18 — not invented cases. Country gating does not
    save it: Colorado/Colorado Springs and Mexico/Mexico City are both
    same-country, which is why folding is curated rather than derived.
    """
    assert market_group_key(a) != market_group_key(b), why


# ── publication gating: the link must be earned ───────────────────────
def test_unpublished_metro_yields_no_slug():
    """'' is the signal the renderer uses to drop the <a> entirely."""
    for raw in ("Frankfurt Am Main", "Frankfurt"):
        assert canonical_market_slug(raw, PUBLISHED_ON_2026_09_18) == "frankfurt"
    for raw in ("Las Cruces", "Pennsylvania", "Texas Regional", "One"):
        assert canonical_market_slug(raw, PUBLISHED_ON_2026_09_18) == "", raw


def test_alias_targets_are_all_published():
    """An alias pointing at an unpublished slug is worse than the split."""
    for key, target in FACILITY_MARKET_ALIASES.items():
        assert target in PUBLISHED_ON_2026_09_18, f"{key} -> {target}"


def test_northern_virginia_resolves_to_the_published_twin():
    """Direction check. `northern-virginia` is published=false and
    `ashburn` published=true in market_power_scores, so the existing
    DCPI_METRO_ALIASES direction is the one that reaches a real page."""
    assert market_group_key("Northern Virginia") == "ashburn"
    assert market_group_key("Silicon Valley") == "santa-clara"


def test_blank_market_yields_no_key_and_no_slug():
    for blank in (None, "", "   ", "-", "  -  "):
        assert market_group_key(blank) == ""
        assert canonical_market_slug(blank, PUBLISHED_ON_2026_09_18) == ""


# ══════════════════════════════════════════════════════════════════════
# ROUTE LEVEL — the two mechanisms the split actually travelled through
# ══════════════════════════════════════════════════════════════════════
from routes import operator_brief as ob  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_published_cache():
    """_PUBLISHED_SLUGS is a module global; a value cached by one test
    would decide the next one's answer."""
    ob._PUBLISHED_SLUGS = None
    yield
    ob._PUBLISHED_SLUGS = None


class _Cur:
    """Minimal cursor. `rows` is what the next fetchall() returns; `boom`
    raises instead, so the read-failure path is exercised rather than
    asserted about."""
    def __init__(self, rows=(), boom=False):
        self.rows, self.boom = list(rows), boom

    def execute(self, *a, **k):
        if self.boom:
            raise RuntimeError("connection reset")

    def fetchall(self):
        return list(self.rows)


def test_digital_realtys_three_measured_rows_fold_to_one():
    folded = ob._fold_markets([("Frankfurt", 17, 80.0, 3),
                               ("Frankfurt Am Main", 12, 132.0, 4),
                               ("Frankfurt am Main", 1, 0.0, 0)])
    assert len(folded) == 1
    label, n, mw, mw_n = folded[0]
    assert (n, mw, mw_n) == (30, 212.0, 7)
    assert label == "Frankfurt", "keep the spelling with the most facilities"


class _SectionCur:
    """Cursor that DISPATCHES ON THE SQL IT IS GIVEN.

    A fake that returns a canned list regardless of the query would keep
    passing if the route stopped grouping, stopped filtering duplicates, or
    asked for something else entirely — it would be measuring the fixture.
    Each branch below is keyed to a fragment of the real statement, and an
    unrecognised query raises rather than returning [].
    """
    def __init__(self, market_rows, total_mw, published):
        self.market_rows, self.total_mw = market_rows, total_mw
        self.published = published
        self._pending = None
        self.limited_in_sql = None

    def execute(self, sql, params=None):
        flat = _flatten_sql(sql)
        if "FROM market_power_scores" in flat:
            self._pending = ("many", [(s,) for s in self.published])
        elif "GROUP BY COALESCE(market, city" in flat:
            # The route must NOT limit in SQL — that is the defect.
            self.limited_in_sql = "LIMIT" in flat.upper()
            self._pending = ("many", list(self.market_rows))
        elif "SELECT COALESCE(SUM(power_mw), 0)" in flat:
            self._pending = ("one", (self.total_mw, 1))
        else:
            raise AssertionError(f"unexpected query: {flat[:90]}")

    def fetchall(self):
        kind, val = self._pending
        assert kind == "many"
        return val

    def fetchone(self):
        kind, val = self._pending
        assert kind == "one"
        return val


def test_the_route_folds_before_it_limits():
    """★ THE ORDERING IS THE FIX, and this drives the ROUTE, not the helper.

    Asserting on `_fold_markets(rows)[:5]` in the test would re-implement
    the very expression under test: the route could limit first and the
    assertion would still pass. So `_section_market_concentration` is
    called, and the fake cursor also records whether the SQL carried a
    LIMIT at all.

    Here the two Frankfurt halves rank 6th and 7th by MW. Limit-then-fold
    never sees them, so the operator's second-largest metro once whole is
    absent from its own top-5 table.
    """
    cur = _SectionCur(
        market_rows=[("Alpha", 1, 500.0, 1), ("Beta", 1, 400.0, 1),
                     ("Gamma", 1, 300.0, 1), ("Delta", 1, 250.0, 1),
                     ("Epsilon", 1, 200.0, 1),
                     ("Frankfurt", 17, 120.0, 3),
                     ("Frankfurt Am Main", 12, 130.0, 4)],
        total_mw=2000.0, published=["frankfurt", "alpha"])
    out = ob._section_market_concentration(cur, "Test Operator")

    assert cur.limited_in_sql is False, (
        "the markets query must not LIMIT — folding after a LIMIT takes "
        "the top five SPELLINGS, not the top five markets")
    assert len(out) == 5
    names = [r["market"] for r in out]
    assert "Frankfurt" in names, f"folded Frankfurt (250 MW) missing: {names}"
    assert "Frankfurt Am Main" not in names, "the metro must appear once"
    fr = next(r for r in out if r["market"] == "Frankfurt")
    assert fr["facility_count"] == 29
    assert fr["total_mw"] == 250.0
    # share is taken against the operator footprint, and is now the WHOLE
    # metro's share rather than each half understating it
    assert fr["share_pct"] == 12.5
    assert fr["market_slug"] == "frankfurt"


def test_the_route_suppresses_the_slug_for_an_unpublished_metro():
    cur = _SectionCur(market_rows=[("Las Cruces", 1, 4500.0, 1)],
                      total_mw=4500.0, published=["frankfurt"])
    out = ob._section_market_concentration(cur, "Test Operator")
    assert out[0]["market"] == "Las Cruces"
    assert out[0]["market_slug"] == "", (
        "/markets/las-cruces/brief answers 200 with an empty shell")


def test_fold_is_deterministic_when_two_spellings_tie_on_facilities():
    a = ob._fold_markets([("Frankfurt Am Main", 5, 10.0, 1),
                          ("Frankfurt am Main", 5, 10.0, 1)])
    b = ob._fold_markets([("Frankfurt am Main", 5, 10.0, 1),
                          ("Frankfurt Am Main", 5, 10.0, 1)])
    assert a == b, "input order must not decide the rendered label"


def test_unreadable_published_set_is_unknown_not_empty():
    """An empty set would strip every link in every brief at once. A read
    failure must degrade to the pre-existing unvalidated slug instead."""
    assert ob._published_market_slugs(_Cur(boom=True)) is None
    assert ob._published_market_slug(_Cur(boom=True), "Frankfurt") == "frankfurt"


def test_empty_published_table_is_also_unknown_and_is_not_cached():
    cur = _Cur(rows=[])
    assert ob._published_market_slugs(cur) is None
    assert ob._PUBLISHED_SLUGS is None, "an empty read must not be cached"


def test_published_set_gates_the_slug():
    cur = _Cur(rows=[("frankfurt",), ("dallas",)])
    assert ob._published_market_slug(cur, "Frankfurt Am Main") == "frankfurt"
    assert ob._published_market_slug(cur, "Las Cruces") == ""


def test_renderer_drops_the_anchor_when_the_slug_did_not_resolve():
    """The '' contract only pays off if the renderer honours it."""
    import re
    src = open(ob.__file__, encoding="utf-8").read()
    block = src[src.index("mc_rows = "):src.index("mc_html = ")]
    assert '/markets/{m["market_slug"]}/brief' in block
    assert re.search(r'if m\.get\("market_slug"\)', block), (
        "the <a> must be conditional on a resolved slug")


# ══════════════════════════════════════════════════════════════════════
# THE RANKING COLUMN — a self-inflicted regression, pinned
# ══════════════════════════════════════════════════════════════════════
from util.market_aliases import fold_market_rows  # noqa: E402


def test_concentration_ranks_by_mw_not_by_the_mw_denominator():
    """`(m, n, mw, mw_n)` — ranking by the LAST numeric column ranks by
    mw_n, the sparse-MW denominator #4714 added, not by MW. Caught in
    review of this change: a 10 MW market with 9 reporting rows outranked
    a 500 MW market with 1."""
    rows = [("Alpha", 1, 500.0, 1), ("Beta", 1, 400.0, 1),
            ("Zeta", 9, 10.0, 9)]
    assert [r[0] for r in fold_market_rows(rows, sort_idx=2)] == [
        "Alpha", "Beta", "Zeta"]
    assert [r[0] for r in ob._fold_markets(rows)] == ["Alpha", "Beta", "Zeta"]


def test_chip_list_ranks_by_facility_count():
    assert [r[0] for r in fold_market_rows([("Alpha", 1), ("Zeta", 9)])] == [
        "Zeta", "Alpha"]


def test_sort_idx_outside_the_numeric_columns_is_refused():
    """Silently falling back to a default would re-hide the bug above."""
    with pytest.raises(ValueError):
        fold_market_rows([("Alpha", 1, 500.0, 1)], sort_idx=9)


# ══════════════════════════════════════════════════════════════════════
# /operators chip list — same split, same fold
# ══════════════════════════════════════════════════════════════════════
from routes import operators as ops_mod  # noqa: E402


class _OpsCur:
    """Dispatches on the SQL, like _SectionCur. The summary query is
    answered with a minimal row so the route reaches top_markets."""
    def __init__(self, market_rows):
        self.market_rows, self._pending = market_rows, None
        self.limited_in_sql = None

    def execute(self, sql, params=None):
        flat = _flatten_sql(sql)
        if "GROUP BY COALESCE(market, city" in flat:
            self.limited_in_sql = "LIMIT" in flat.upper()
            self._pending = ("many", list(self.market_rows))
        elif "COUNT(*) AS facility_count" in flat:
            self._pending = ("one", (42, 100.0, 5, 40, 2, 3, 1))
        else:
            self._pending = ("many", [])

    def fetchall(self):
        kind, val = self._pending
        return val if kind == "many" else []

    def fetchone(self):
        kind, val = self._pending
        return val if kind == "one" else None


def test_operators_chip_list_folds_the_metro_before_limiting():
    """Drives _operator_summary itself — no fallback branch. A test that
    quietly re-implements the fold when it cannot reach the route measures
    the fixture, and would stay green if /operators stopped folding."""
    cur = _OpsCur([("Frankfurt", 17), ("Frankfurt Am Main", 12),
                   ("Frankfurt am Main", 1), ("Dallas", 40)])
    summary = ops_mod._operator_summary(cur, "Test Operator")
    assert summary is not None
    assert cur.limited_in_sql is False, (
        "the chip query must not LIMIT — folding after a LIMIT 10 takes "
        "the top ten SPELLINGS")
    tm = summary["top_markets"]
    assert [m["market"] for m in tm] == ["Dallas", "Frankfurt"]
    assert tm[1]["facilities"] == 30, "three chips became one: 17+12+1"
