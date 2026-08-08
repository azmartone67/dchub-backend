"""r-sat-dedup (2026-08-08) — the DCPI market saturation FOOTPRINT must count a
twinned building once, and both branches must count it the same way.

THE BUG: gather_metrics_for_market measures how dense a market already is —
COUNT(*) facilities, SUM(power_mw) operational, SUM(power_mw) pipeline,
COUNT(DISTINCT provider) — over discovered_facilities with NO
duplicate-visibility predicate. 9,459 of 24,676 rows (38%) carry a
duplicate_of_id, so a metro with heavy duplication read denser than it is.
Boardman OR measured 53 facilities against 6 real ones.

That number is not cosmetic. It becomes `_saturation_index`, which multiplies
queue_wait_months by (0.90 + 0.45·sat), adds 4.0·sat points of
demand_growth_yoy_pct, and scales btm_headroom_mw by (1 − 0.40·sat). Those feed
compute_constraint_score / compute_excess_power_score and therefore the
published verdict. Double-counted buildings made markets look more contested,
slower to interconnect and shorter on behind-the-meter headroom than the real
fleet supports.

★ VISIBILITY IS `duplicate_of_id` ALONE, NEVER `is_duplicate` — the same rule
tests/test_dcpi_facility_list_dedup.py pins for the rendered list. `is_duplicate`
is a suppression bit that also drops the row from counts and the sitemap, and
3,188 twin rows carry a pointer while staying UNflagged; scoping the footprint on
the flag would leave exactly those double-counted while removing rows that are
not twins at all.

★ WHY THE PREDICATE IS A SHARED CONSTANT AND THIS TEST INSISTS ON IT.
The two branches (US city+state, intl city-pooled) sit 14 lines apart and have
diverged twice, both times shipping a wrong published score:
  - r-declone-2 (2026-07-17): the country predicate was on the US branch only,
    so every international market matched 0 facilities and stayed an ISO clone.
  - r-status-taxonomy (2026-07-29): the unfiltered-op_mw bug was in BOTH and had
    to be fixed in both places.
A hand-copied literal in each branch is how that keeps happening, so the
predicate has ONE definition (routes.dcpi._SQL_FOOTPRINT_DEDUP) and the
structural tests below fail if a branch stops referencing it.

SCOPE: the aggregate footprint. The row-rendering list is pinned by the sibling
test. The third call site — the /api/v1/dcpi/lite-recompute loop — carries the
predicate but is DEAD (MARKETS holds 6-tuples, so `slug.replace` raises into its
own per-market `except`), and is deliberately left dead: making it run would let
a lite score overwrite the full path's row.
"""
import ast
import re
import sqlite3

import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402

# One AST reader, not a second copy of one. The sibling test owns it; a
# hand-copied extractor is the same bug class this module is about.
from tests.test_dcpi_facility_list_dedup import _sql_nodes  # noqa: E402

_SRC = dcpi.__file__.replace(".pyc", ".py")

# The saturation footprint pair, and nothing else: only these two queries
# aggregate discovered_facilities with a DISTINCT provider count. The lite
# loop's aggregate (COUNT/SUM/MAX(state), no provider term) does not match.
_FOOTPRINT_MARKER = "count(distinct provider)"
_DEDUPED = re.compile(r"duplicate_of_id\s+is\s+null", re.I)


def _footprint_queries():
    out = [(ln, sql) for ln, sql in _sql_nodes(_SRC)
           if "from discovered_facilities" in sql.lower()
           and _FOOTPRINT_MARKER in sql.lower()]
    assert out, "could not locate the saturation footprint queries in routes/dcpi.py"
    return out


# --------------------------------------------------------------------------
# Structural: the predicate is present, in BOTH branches, from ONE definition.
# --------------------------------------------------------------------------
def test_the_shared_predicate_is_the_pointer_not_the_flag():
    """The single definition says `duplicate_of_id IS NULL` and nothing else.
    A 'fix' that swaps in is_duplicate would suppress non-twins while leaving
    the 3,188 pointer-carrying unflagged twins counted twice."""
    pred = dcpi._SQL_FOOTPRINT_DEDUP
    assert _DEDUPED.search(pred), (
        f"_SQL_FOOTPRINT_DEDUP does not scope on the pointer: {pred!r}")
    assert not re.search(r"is_duplicate", pred, re.I), (
        f"_SQL_FOOTPRINT_DEDUP scopes visibility on is_duplicate: {pred!r}. "
        "Visibility is duplicate_of_id ALONE — the flag also removes the row "
        "from counts and the sitemap, and does not consolidate.")


def test_both_footprint_branches_are_duplicate_scoped():
    """THE invariant. Each branch of the footprint carries the predicate."""
    offenders = [(ln, " ".join(sql.split())[:170])
                 for ln, sql in _footprint_queries()
                 if not _DEDUPED.search(sql)
                 and "_SQL_FOOTPRINT_DEDUP" not in sql]
    assert not offenders, (
        "market saturation footprint query with no duplicate-visibility "
        "predicate — this is how boardman measured 53 facilities against 6 "
        "real ones and published a saturation of 0.556:\n"
        + "\n".join(f"  routes/dcpi.py:{ln}  {s}" for ln, s in offenders))


def test_there_are_exactly_two_branches_and_both_use_the_shared_constant():
    """Anti-divergence. Both branches must interpolate the SAME module-level
    name, so the predicate cannot be changed in one place and not the other —
    the failure mode that shipped r-declone-2 and r-status-taxonomy."""
    qs = _footprint_queries()
    assert len(qs) == 2, (
        f"expected exactly 2 saturation footprint branches (US city+state, "
        f"intl city-pooled), found {len(qs)} at lines "
        f"{[ln for ln, _ in qs]}. A third copy of this aggregate is a third "
        "place for the definition to drift.")
    for ln, sql in qs:
        assert "_SQL_FOOTPRINT_DEDUP" in sql, (
            f"routes/dcpi.py:{ln} hand-copies the duplicate predicate instead "
            "of interpolating _SQL_FOOTPRINT_DEDUP. Two literals are two "
            "definitions, and these two branches have already drifted twice.")


def test_both_branches_measure_THE_SAME_THING():
    """The branches may differ ONLY in how they select the market's rows (the
    US one also filters state). What they MEASURE — the SELECT list — must be
    byte-identical, or 'the facilities in this market' means two different
    things depending on which side of the border the market sits."""
    selects = []
    for ln, sql in _footprint_queries():
        m = re.search(r"\bselect\b(.*?)\bfrom\b", sql, re.I | re.S)
        assert m, f"routes/dcpi.py:{ln}: unparseable footprint query"
        selects.append((ln, " ".join(m.group(1).split())))
    (ln_a, sel_a), (ln_b, sel_b) = selects
    assert sel_a == sel_b, (
        f"routes/dcpi.py:{ln_a} and :{ln_b} measure different things:\n"
        f"  {ln_a}: {sel_a}\n  {ln_b}: {sel_b}")


# --------------------------------------------------------------------------
# Behavioral: EXECUTE both production queries over planted twins.
# --------------------------------------------------------------------------
_SQLITE_DDL = """
CREATE TABLE discovered_facilities (
  id INTEGER PRIMARY KEY, name TEXT, provider TEXT, power_mw REAL,
  city TEXT, market TEXT, state TEXT, country TEXT, status TEXT,
  latitude REAL, longitude REAL, duplicate_of_id INTEGER, is_duplicate INTEGER
)
"""

_COLS = ("id,name,provider,power_mw,city,market,state,country,status,"
         "latitude,longitude,duplicate_of_id,is_duplicate")


def _render(sql, ctry_sql):
    """Fill the f-string holes with the REAL fragments production interpolates,
    so this executes production's predicate rather than a retyped one."""
    for name, frag in (("_SQL_OP_STATUS", dcpi._SQL_OP_STATUS),
                       ("_SQL_PIPE_STATUS", dcpi._SQL_PIPE_STATUS),
                       ("_SQL_UNK_STATUS", dcpi._SQL_UNK_STATUS),
                       ("_SQL_FOOTPRINT_DEDUP", dcpi._SQL_FOOTPRINT_DEDUP),
                       ("_ctry_sql", ctry_sql)):
        sql = sql.replace(f" {name} ", " " + frag + " ")
    return sql.replace("%s", "?")


def _us_query():
    for ln, sql in _footprint_queries():
        if "upper(coalesce(state,''))" in sql.lower():
            return sql
    pytest.fail("no US city+state footprint branch found")


def _intl_query():
    for ln, sql in _footprint_queries():
        if "upper(coalesce(state,''))" not in sql.lower():
            return sql
    pytest.fail("no intl city-pooled footprint branch found")


def _run(sql, rows, params):
    con = sqlite3.connect(":memory:")
    con.execute(_SQLITE_DDL)
    con.executemany(
        f"INSERT INTO discovered_facilities ({_COLS}) "
        f"VALUES ({','.join('?' * 13)})", rows)
    return con.execute(sql, params).fetchone()


def _row(i, name, dup_of=None, flag=0, city="Boardman", state="OR",
         country="US", status="operating", mw=10.0, lat=45.84, lon=-119.70,
         provider="Amazon"):
    return (i, name, provider, mw, city, city, state, country, status,
            lat, lon, dup_of, flag)


def _run_us(rows, city="Boardman", state="OR", iso="WECC",
            lat=45.84, lon=-119.70):
    ctry_sql, ctry_params = dcpi._market_country_scope(iso, state, lat, lon)
    return _run(_render(_us_query(), ctry_sql), rows,
                [city, state.upper()] + list(ctry_params))


def _run_intl(rows, city="Sydney", state="NSW", iso="AEMO",
              lat=-33.87, lon=151.21):
    ctry_sql, ctry_params = dcpi._market_country_scope(iso, state, lat, lon)
    return _run(_render(_intl_query(), ctry_sql), rows, [city] + list(ctry_params))


def test_us_branch_counts_a_twinned_building_once():
    """The measured Boardman shape, scaled down: three real facilities, each
    with a twin row pointing at it. Every term must see three, not six."""
    rows = [
        _row(1, "AWS Boardman PDX-1", mw=100.0),
        _row(2, "AWS Boardman PDX-2", mw=50.0, provider="AWS"),
        _row(3, "Umatilla Electric DC", mw=25.0, provider="Umatilla",
             status="planned"),
        _row(4, "AWS Boardman PDX-1", dup_of=1, mw=100.0),
        _row(5, "AWS Boardman PDX-2", dup_of=2, mw=50.0, provider="AWS"),
        _row(6, "Umatilla Electric DC", dup_of=3, mw=25.0,
             provider="Umatilla", status="planned"),
    ]
    fac, op_mw, pipe_mw, unk_mw, prov = _run_us(rows)
    assert fac == 3, f"twins double-counted the facility term: {fac}"
    assert op_mw == 150.0, f"twins double-counted operational MW: {op_mw}"
    assert pipe_mw == 25.0, f"twins double-counted pipeline MW: {pipe_mw}"
    assert unk_mw == 0.0, f"unclassified must stay 0 here: {unk_mw}"
    assert prov == 3, f"provider diversity counted twins: {prov}"


def test_intl_branch_counts_a_twinned_building_once():
    """The intl branch pools by city. It must dedupe identically — the US
    branch having a filter the intl one lacks is precisely r-declone-2."""
    rows = [
        _row(1, "Equinix SY1", city="Sydney", state="NSW", country="AU",
             mw=40.0, lat=-33.87, lon=151.21),
        _row(2, "NEXTDC S1", city="Sydney", state="", country="AU",
             mw=30.0, provider="NEXTDC", lat=-33.87, lon=151.21),
        _row(3, "Equinix SY1", city="Sydney", state="NSW", country="AU",
             mw=40.0, dup_of=1, lat=-33.87, lon=151.21),
        _row(4, "NEXTDC S1", city="Sydney", state="", country="AU",
             mw=30.0, provider="NEXTDC", dup_of=2, lat=-33.87, lon=151.21),
    ]
    fac, op_mw, pipe_mw, unk_mw, prov = _run_intl(rows)
    assert fac == 2, f"twins double-counted the facility term: {fac}"
    assert op_mw == 70.0, f"twins double-counted operational MW: {op_mw}"
    assert prov == 2, f"provider diversity counted twins: {prov}"


def test_a_flagged_but_unpointed_row_still_counts():
    """is_duplicate alone must NOT remove a facility from the footprint. A row
    flagged with no pointer is a keeperless suppression, not a twin — dropping
    it would understate a real market's density."""
    rows = [
        _row(1, "Real Facility A", mw=100.0),
        _row(2, "Real Facility B", dup_of=None, flag=1, mw=50.0,
             provider="Meta"),
    ]
    fac, op_mw, _pipe, _unk, prov = _run_us(rows)
    assert (fac, op_mw, prov) == (2, 150.0, 2), (
        "a flagged-but-unpointed row was dropped from the footprint — "
        f"visibility must key on the pointer alone, got {(fac, op_mw, prov)}")


def test_a_twin_row_is_dropped_even_when_it_is_the_only_flagged_state():
    """The common live shape: pointer set, flag never set (3,188 rows). The
    pointer alone must be enough to drop it."""
    rows = [
        _row(1, "Solo Building", mw=80.0),
        _row(2, "Solo Building", dup_of=1, flag=0, mw=80.0),
    ]
    fac, op_mw, _pipe, _unk, _prov = _run_us(rows)
    assert (fac, op_mw) == (1, 80.0), (
        f"an unflagged twin survived the footprint: {(fac, op_mw)}")


# --------------------------------------------------------------------------
# The consequence: a smaller footprint cannot raise the saturation index.
# --------------------------------------------------------------------------
def test_log_sat_is_monotonic_so_removing_twins_cannot_raise_saturation():
    """_saturation_index is a positively-weighted sum of _log_sat terms over
    the four footprint values. Dropping twin rows can only shrink or hold each
    value, so the index can only fall or hold. Pinning the monotonicity of
    _log_sat is what makes that argument true of the real function rather than
    of a formula retyped into a test."""
    for ceiling in (400.0, 8000.0, 5000.0, 40.0):
        prev = None
        for v in (0, 1, 2, 5, 6, 11, 16, 43, 53, 400, 8000, 20000):
            cur = dcpi._log_sat(float(v), ceiling)
            assert 0.0 <= cur <= 1.0, f"_log_sat({v},{ceiling}) = {cur}"
            if prev is not None:
                assert cur >= prev, (
                    f"_log_sat is not monotonic at ceiling {ceiling}: "
                    f"{v} gave {cur}, previous gave {prev}")
            prev = cur


def test_the_measured_boardman_case_falls():
    """The worst measured market. 53 rows → 6 real buildings, and the published
    saturation must move down, not up. Weights/ceilings read from the source so
    this reproduces the shipped formula rather than a copy of it."""
    src = open(_SRC, encoding="utf-8").read()
    m = re.search(
        r"_sat\s*=\s*_clip\(\s*"
        r"([\d.]+)\s*\*\s*_log_sat\(_fac,\s*([\d.]+)\)\s*\+\s*"
        r"([\d.]+)\s*\*\s*_log_sat\(_op_mw,\s*([\d.]+)\)\s*\+\s*"
        r"([\d.]+)\s*\*\s*_log_sat\(_pipe_mw,\s*([\d.]+)\)\s*\+\s*"
        r"([\d.]+)\s*\*\s*_log_sat\(_prov,\s*([\d.]+)\)", src)
    assert m, "could not read the _sat formula out of routes/dcpi.py"
    w = [float(g) for g in m.groups()]

    def sat(fac, op_mw, pipe_mw, prov):
        return dcpi._clip(w[0] * dcpi._log_sat(fac, w[1])
                          + w[2] * dcpi._log_sat(op_mw, w[3])
                          + w[4] * dcpi._log_sat(pipe_mw, w[5])
                          + w[6] * dcpi._log_sat(prov, w[7]), 0.0, 1.0)

    # measured live 2026-08-08 (replica), boardman OR
    before = sat(53, 322.0, 0.0, 6)
    after = sat(6, 322.0, 0.0, 3)
    assert after < before, (
        f"removing 47 twin rows did not lower saturation: {before} -> {after}")
    # and the published queue-wait multiplier must ease, not tighten
    assert (0.90 + 0.45 * after) < (0.90 + 0.45 * before)
