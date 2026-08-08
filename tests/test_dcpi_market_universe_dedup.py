"""r-universe-dedup (2026-08-08) — the DCPI market UNIVERSE must be built from
real buildings, not from twin rows.

THE BUG: _load_markets_dynamic's `city_stats` CTE aggregated
discovered_facilities with NO duplicate-visibility predicate at all. 9,459 of
24,859 rows (38%, measured live 2026-08-08) carry a `duplicate_of_id`, so a
twin-heavy city read as much as 10x its real size (ashburn 308 rows -> 163 real
buildings; boardman 51 -> 5).

Unlike the sibling saturation-footprint bug, this one does not merely mis-score
a market. It decides WHICH CITIES ARE MARKETS, in three distinct ways:

  1. ADMISSION.  `HAVING COUNT(*) >= 3` is the published bar for becoming a
     scored DCPI market. goose-creek SC counted 12 rows against fewer than 3
     real buildings — admitted on twins alone. Six markets in the served 200
     were admitted this way.

  2. CROWD-OUT.  `ORDER BY facility_count DESC LIMIT 200` is a FIXED cap, so
     padding is zero-sum: every twin-inflated city displaces a real one.
     Measured on the live replica, 22 real markets were pushed off the 200,
     including mount-pleasant WI (3,600 MW) and abilene TX (3,100 MW) — two of
     the largest AI-era campuses in the country. Same shape as r-list-dedup,
     where twins ate a LIMIT 50 and pushed 8 real facilities off a page.

  3. CENTROID.  The percentile_cont median runs over the SAME rows, so
     duplicated coordinates re-weight it. 36 markets move >0.5 km and
     chattanooga moves 21.4 km. That centroid is what gather_metrics_for_market
     hands to _local_infra_metrics, whose 25/40/60 km boxes feed constraint
     (<= +6) and excess (<= +8) — a scored INPUT, not a label. Measured at
     west-texas' two centroids, local_gen_mw reads 584 MW vs 145 MW.

  (op_mw / pipeline_mw are double-counted too, but the tuple branch discards
  them — dead output today. Pinned below anyway so a future reader is safe.)

★ VISIBILITY IS `duplicate_of_id` ALONE, NEVER `is_duplicate` — the rule
tests/test_dcpi_facility_list_dedup.py pins for the rendered list and
tests/test_dcpi_saturation_footprint_dedup.py pins for the footprint.
`is_duplicate` is a suppression bit: 3,286 twin rows carry a pointer while
staying UNflagged (scoping on the flag leaves exactly those double-counted) and
1,510 rows are flagged with NO pointer (scoping on the flag drops real
facilities that are not twins at all).

NOT A REGRESSION IN THE UNIVERSE: every slug displaced by this fix is already
in market_power_scores, and _load_scored_orphans re-adopts anything ever scored,
so no market leaves the scored set. Verified against the live replica before
shipping rather than assumed — see the PR body.
"""
import ast
import re
import sqlite3

import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402

# One AST reader, not a second copy of one. The facility-list test owns it.
from tests.test_dcpi_facility_list_dedup import _sql_nodes  # noqa: E402

_SRC = dcpi.__file__.replace(".pyc", ".py")

_DEDUPED = re.compile(r"duplicate_of_id\s+is\s+null", re.I)


def _universe_query():
    """The city_stats CTE, read out of the AST so a query that exists only in a
    comment cannot satisfy this test. f-string holes render as the interpolated
    NAME, so `{_SQL_FOOTPRINT_DEDUP}` reads as present."""
    hits = [(ln, sql) for ln, sql in _sql_nodes(_SRC)
            if "city_stats" in sql and "discovered_facilities" in sql.lower()
            and "having" in sql.lower()]
    assert len(hits) == 1, (
        f"expected exactly 1 city_stats market-universe query in routes/dcpi.py, "
        f"found {len(hits)} at lines {[ln for ln, _ in hits]}. A second copy is "
        "a second place for the market universe to be defined.")
    return hits[0]


# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------
def test_the_market_universe_query_is_duplicate_scoped():
    """THE invariant. Without this predicate, `HAVING COUNT(*) >= 3` admits
    cities that do not have 3 facilities and `LIMIT 200` ranks on padding."""
    ln, sql = _universe_query()
    assert _DEDUPED.search(sql) or "_SQL_FOOTPRINT_DEDUP" in sql, (
        f"routes/dcpi.py:{ln} builds the DCPI market universe from "
        "discovered_facilities with NO duplicate-visibility predicate. This is "
        "how goose-creek SC became a scored market on 12 rows covering fewer "
        "than 3 real buildings, and how mount-pleasant WI (3,600 MW) and "
        "abilene TX (3,100 MW) were crowded off the LIMIT 200:\n"
        f"  {' '.join(sql.split())[:200]}")


def test_it_interpolates_the_shared_constant_not_a_hand_copied_literal():
    """The predicate has ONE definition in this module. A hand-copied literal
    is how the two saturation-footprint branches drifted twice (r-declone-2,
    r-status-taxonomy) — this is now the third call site."""
    ln, sql = _universe_query()
    assert "_SQL_FOOTPRINT_DEDUP" in sql, (
        f"routes/dcpi.py:{ln} hand-copies the duplicate predicate instead of "
        "interpolating _SQL_FOOTPRINT_DEDUP. Two literals are two definitions.")


def test_visibility_is_the_pointer_never_the_flag():
    """Scoping the universe on is_duplicate would admit the 3,286 pointer-
    carrying UNflagged twins anyway, while dropping 1,510 flagged-but-unpointed
    rows that are real facilities — wrong in both directions at once."""
    ln, sql = _universe_query()
    assert not re.search(r"is_duplicate", sql, re.I), (
        f"routes/dcpi.py:{ln} scopes market admission on is_duplicate. "
        "Visibility is duplicate_of_id ALONE.")
    assert _DEDUPED.search(dcpi._SQL_FOOTPRINT_DEDUP), (
        f"_SQL_FOOTPRINT_DEDUP is not the pointer: {dcpi._SQL_FOOTPRINT_DEDUP!r}")


def test_the_admission_bar_and_the_cap_are_still_what_the_docs_claim():
    """The two clauses this bug corrupts. If either changes, the numbers in
    this module's docstring and in the PR body stop describing production."""
    _ln, sql = _universe_query()
    flat = " ".join(sql.split()).lower()
    assert "having count(*) >= 3" in flat, "the >=3 admission bar moved"
    assert "order by facility_count desc" in flat, "the ranking key moved"
    assert "limit 200" in flat, "the 200-market cap moved"


# --------------------------------------------------------------------------
# Behavioral: EXECUTE the production query over planted twin rows.
#
# The shipped SQL is Postgres. sqlite runs neither percentile_cont/WITHIN
# GROUP, nor aggregate FILTER, nor the POSIX `~` regex, and this repo has no
# in-process Postgres. So the query is put through an EXPLICIT, NAMED shim —
# and _shim_is_faithful() below asserts the shim rewrote only those three
# constructs and left the predicate, the HAVING bar, the GROUP BY and the
# ORDER BY byte-identical. A shim that silently no-ops (and so tests nothing)
# fails its own substitution counters.
# --------------------------------------------------------------------------
class _Median:
    """percentile_cont(0.5) — mean of the two middle values on even counts."""

    def __init__(self):
        self.vals = []

    def step(self, v):
        if v is not None:
            self.vals.append(float(v))

    def finalize(self):
        if not self.vals:
            return None
        s = sorted(self.vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


_SHIM_RULES = (
    # percentile_cont(0.5) WITHIN GROUP (ORDER BY col) FILTER (WHERE <bounds>)
    # -> _median(col). The FILTER here is a coordinate sanity bound
    # (-90..90 / -180..180); the planted rows are all in range, so dropping it
    # cannot change a result. _Median ignores NULLs, as percentile_cont does.
    (r"percentile_cont\(0\.5\)\s*WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+(\w+)\s*\)"
     r"\s*FILTER\s*\(\s*WHERE\s+[^)]*?\)", r"_median(\1)", 2),
    # SUM(x) FILTER (WHERE cond) -> SUM(CASE WHEN cond THEN x END)
    (r"SUM\((\w+)\)\s*FILTER\s*\(\s*WHERE\s+(.*?)\)\s*,\s*0\)",
     r"SUM(CASE WHEN \2 THEN \1 END), 0)", 1),
    # POSIX regex -> GLOB. Same meaning for a 2-char uppercase code.
    (r"state\s*~\s*'\^\[A-Z\]\{2\}\$'", "state GLOB '[A-Z][A-Z]'", 1),
)

# Clauses the shim must NOT touch — the things actually under test.
_MUST_SURVIVE = (
    "AND duplicate_of_id IS NULL",
    "HAVING COUNT(*) >= 3",
    "GROUP BY LOWER(city), city, state",
    "ORDER BY facility_count DESC",
)


def _rendered_sql():
    """Production's query with the real constant substituted in."""
    _ln, sql = _universe_query()
    return sql.replace(" _SQL_FOOTPRINT_DEDUP ",
                       " " + dcpi._SQL_FOOTPRINT_DEDUP + " ")


def _shim(sql):
    for pat, repl, expected in _SHIM_RULES:
        sql, n = re.subn(pat, repl, sql, flags=re.I | re.S)
        assert n == expected, (
            f"sqlite shim rule {pat!r} applied {n}x, expected {expected}x. The "
            "shipped query changed shape — re-check this shim before trusting "
            "any green below.")
    return sql


def test_the_shim_is_faithful_to_the_clauses_under_test():
    """Guard on the guard. The behavioral tests are only evidence about
    production if the shim left the dedup predicate, the admission bar and the
    ranking key exactly as shipped."""
    shimmed = _shim(_rendered_sql())
    for clause in _MUST_SURVIVE:
        assert clause in shimmed, (
            f"the sqlite shim altered or dropped {clause!r} — the behavioral "
            "tests below would be testing a query production does not run")
    assert "percentile_cont" not in shimmed.lower()
    assert "filter" not in shimmed.lower()


_DDL = """
CREATE TABLE discovered_facilities (
  id INTEGER PRIMARY KEY, name TEXT, provider TEXT, power_mw REAL,
  city TEXT, state TEXT, country TEXT, status TEXT,
  latitude REAL, longitude REAL, duplicate_of_id INTEGER, is_duplicate INTEGER
)
"""
_COLS = ("id,name,provider,power_mw,city,state,country,status,"
         "latitude,longitude,duplicate_of_id,is_duplicate")


def _row(i, city, dup_of=None, flag=0, state="VA", country="US",
         status="operating", mw=10.0, lat=39.0, lon=-77.0, name=None,
         provider="Amazon"):
    return (i, name or f"{city} DC {i}", provider, mw, city, state, country,
            status, lat, lon, dup_of, flag)


def _run(rows, sql=None):
    con = sqlite3.connect(":memory:")
    con.create_aggregate("_median", 1, _Median)
    con.execute(_DDL)
    con.executemany(
        f"INSERT INTO discovered_facilities ({_COLS}) "
        f"VALUES ({','.join('?' * 12)})", rows)
    return con.execute(_shim(sql or _rendered_sql())).fetchall()


# ---- 1. ADMISSION --------------------------------------------------------
def test_admission_two_real_buildings_plus_a_twin_is_not_a_market():
    """THE admission bug. `HAVING COUNT(*) >= 3` must count buildings, not
    rows — otherwise a 2-facility city is published as a scored DCPI market."""
    rows = [
        _row(1, "Twinsville"),
        _row(2, "Twinsville"),
        _row(3, "Twinsville", dup_of=1),   # twin of #1, not a third building
    ]
    got = _run(rows)
    assert got == [], (
        "a city with 2 real buildings + 1 twin row was admitted as a DCPI "
        f"market — this is the goose-creek SC shape: {got}")


def test_admission_three_real_buildings_still_qualify():
    """The other direction. The fix must not suppress a market that genuinely
    clears the bar — including when it ALSO carries twins."""
    rows = [
        _row(1, "Realtown"), _row(2, "Realtown"), _row(3, "Realtown"),
        _row(4, "Realtown", dup_of=1), _row(5, "Realtown", dup_of=2),
    ]
    got = _run(rows)
    assert len(got) == 1 and got[0][3] == 3, (
        f"a real 3-facility market was dropped or miscounted: {got}")


def test_a_flagged_but_unpointed_row_still_counts_toward_admission():
    """1,510 live rows are is_duplicate=1 with NO pointer. They are real
    facilities under a suppression bit, not twins. Scoping admission on the
    flag would silently un-market cities that do clear the bar."""
    rows = [
        _row(1, "Flagville"), _row(2, "Flagville"),
        _row(3, "Flagville", dup_of=None, flag=1),
    ]
    got = _run(rows)
    assert len(got) == 1 and got[0][3] == 3, (
        "a flagged-but-unpointed row was dropped from market admission — "
        f"visibility must key on the pointer alone: {got}")


# ---- 2. CROWD-OUT --------------------------------------------------------
def test_crowd_out_a_twin_padded_city_cannot_outrank_a_real_one():
    """`ORDER BY facility_count DESC` feeds a FIXED `LIMIT 200`, so rank IS
    admission at the margin. A 3-building city padded to 23 rows must not
    outrank a real 5-building city."""
    rows = ([_row(i, "Padded") for i in range(1, 4)]
            + [_row(100 + i, "Padded", dup_of=1) for i in range(20)]
            + [_row(200 + i, "Genuine", state="TX") for i in range(5)])
    got = _run(rows)
    ranked = [(r[0], r[3]) for r in got]
    assert ranked[0][0] == "genuine", (
        "a twin-padded city outranked a larger real one; under LIMIT 200 that "
        f"is a real market displaced off the scored set: {ranked}")
    assert ranked == [("genuine", 5), ("padded", 3)], ranked


def test_the_cap_truncates_by_that_rank():
    """Demonstrates the displacement directly: with the cap lowered to 1 (the
    ONLY edit — asserted below), the surviving market must be the real one."""
    sql = _rendered_sql()
    capped, n = re.subn(r"LIMIT\s+200", "LIMIT 1", sql)
    assert n == 1, "could not lower the cap; the LIMIT clause changed shape"
    rows = ([_row(i, "Padded") for i in range(1, 4)]
            + [_row(100 + i, "Padded", dup_of=1) for i in range(20)]
            + [_row(200 + i, "Genuine", state="TX") for i in range(5)])
    got = _run(rows, sql=capped)
    assert [r[0] for r in got] == ["genuine"], (
        f"the twin-padded city took the last slot from the real market: {got}")


# ---- 3. CENTROID ---------------------------------------------------------
def test_the_centroid_is_the_median_of_real_buildings_only():
    """percentile_cont runs over the same rows, so duplicated coordinates drag
    the market centroid. That centroid feeds _local_infra_metrics' 25/40/60 km
    boxes, which adjust constraint (<= +6) and excess (<= +8)."""
    rows = [
        _row(1, "Dragtown", lat=10.0, lon=10.0),
        _row(2, "Dragtown", lat=20.0, lon=20.0),
        _row(3, "Dragtown", lat=30.0, lon=30.0),
    ] + [_row(10 + i, "Dragtown", dup_of=1, lat=10.0, lon=10.0)
         for i in range(5)]
    got = _run(rows)
    assert len(got) == 1, got
    _slug, _name, _st, fac, _op, _pipe, lat, lon = got[0]
    assert fac == 3, fac
    assert (lat, lon) == (20.0, 20.0), (
        "the centroid was dragged onto the duplicated building — over all 8 "
        f"rows the median is 10.0, over the 3 real ones it is 20.0: {(lat, lon)}")


# ---- MW (dead output today, pinned so it stays correct if ever read) -----
def test_megawatts_are_not_summed_across_twins():
    rows = [
        _row(1, "Mwville", mw=100.0),
        _row(2, "Mwville", mw=50.0, status="planned"),
        _row(3, "Mwville", mw=25.0),
        _row(4, "Mwville", mw=100.0, dup_of=1),
        _row(5, "Mwville", mw=50.0, dup_of=2, status="planned"),
    ]
    got = _run(rows)
    assert len(got) == 1, got
    _slug, _name, _st, fac, op_mw, pipe_mw, _lat, _lon = got[0]
    assert (fac, op_mw, pipe_mw) == (3, 175.0, 50.0), (
        f"twins double-counted the market's megawatts: {(fac, op_mw, pipe_mw)}")
