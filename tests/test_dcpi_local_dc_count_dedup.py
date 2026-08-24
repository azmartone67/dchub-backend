"""r-radius-dedup (2026-08-08) — the 25 km local-competition COUNT must scope
duplicate visibility on the POINTER, not on the suppression flag.

THE BUG: `_local_infra_metrics` computed `local_dc_count` with
`COALESCE(is_duplicate, 0) = 0`. That is the wrong key by this repo's own rule
(see tests/test_dcpi_facility_list_dedup.py): visibility is `duplicate_of_id`
ALONE. Two measured consequences on the live table (2026-08-08, 24,859 rows):

  * 3,286 rows carry a `duplicate_of_id` while staying UNflagged. The flag
    predicate counted every one of those a second time.
  * 1,510 rows are flagged with NO pointer. That is a keeperless suppression,
    not a twin — a real facility, silently dropped from the count.

WHY IT IS NOT COSMETIC: `local_dc_count` feeds compute_constraint_score as the
local-competition term, 0.06 x clip(count / 40 x 100) — bounded <= +6 points.
Replaying the real scorer over all 316 markets with only this predicate toggled
(one gather per market, so no live feed can move between the two reads) moved
constraint_score for 147 markets: 132 DOWN (twins removed) and 15 UP (flagged
but unpointed real facilities restored), mean |delta| 0.47 points, max 1.5.
It flipped ZERO verdicts — nearest miss tulsa, already 0.2 points inside the
BUILD boundary and moved further inside it. So this corrects a published
competition signal without relabelling any market.

NOT COVERED BY THE EXISTING DEDUP TESTS, BY CONSTRUCTION: this is a RADIUS
(bbox) aggregate. test_dcpi_facility_list_dedup only asserts over queries that
are market-name-scoped AND row-rendering; a bbox COUNT(*) matches neither of its
gates. That is why the wrong key survived that PR.

WHAT IS PINNED HERE:
  1. STATIC — the bbox facility COUNT carries `duplicate_of_id IS NULL` and does
     NOT key visibility on `is_duplicate`. Read out of the AST, so a predicate
     that only exists in a comment cannot satisfy it. The predicate reaches that
     query through the shared `_SQL_FOOTPRINT_DEDUP` constant, and `_sql_strings`
     RESOLVES that constant to its value — so this also fails if the constant
     itself is redefined to the flag, which would silently break the saturation
     footprint (#2403) too.
  2. BEHAVIORAL — `_local_infra_metrics` itself is executed against planted
     rows, through its own SQL and its own 16 positional parameters. A twin
     counts once; a flagged-but-unpointed row still counts; the bbox still
     bounds the count, and the dc parameters still reach the dc subquery.
  3. SCORE-BEARING — the measured miami shape (40 -> 30 facilities) moves
     constraint_score, so a regression here is a published-number regression.
"""
import ast
import re
import sqlite3

import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402

_SRC = dcpi.__file__.replace(".pyc", ".py")

# The pointer, and ONLY the pointer.
_DEDUPED = re.compile(r"duplicate_of_id\s+is\s+null", re.I)
# A RADIUS predicate: latitude bounded by BIND PARAMETERS, in the facilities
# table's coordinate spelling. Deliberately not a bare `latitude BETWEEN` —
# _load_markets_dynamic bounds latitude by the literals -90 AND 90 as a sanity
# filter, and that query is a per-city aggregate, not a radius read. It is
# undeduped too, but it belongs to the saturation-footprint class (see the SCOPE
# note in tests/test_dcpi_facility_list_dedup.py), not to this change.
_BBOX = re.compile(r"latitude\s+between\s*%s", re.I)


def _sql_strings(path):
    """(lineno, sql) for every string literal / f-string in the module, read out
    of the AST. A query that exists only inside a `#` comment is invisible here,
    which is the point — a grep-based version of this test has been satisfied by
    a comment before (see the r-daily count-gap postmortem).

    ★ An f-string hole whose expression is a module-level STRING constant is
    rendered as that constant's VALUE, resolved from the imported module. The
    predicate here arrives via {_SQL_FOOTPRINT_DEDUP}, so rendering the hole as
    the NAME (what tests/test_dcpi_facility_list_dedup.py does, correctly, for
    its own purposes) would make this file assert on the spelling of a variable
    instead of on the SQL that executes — and would keep passing if that shared
    constant were redefined to the suppression flag. Non-string and non-module
    expressions still fall back to the name."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    claimed, out = set(), []

    def _render_hole(node):
        name = ast.unparse(node.value)
        val = getattr(dcpi, name, None) if name.isidentifier() else None
        return val if isinstance(val, str) else f" {name} "

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            buf = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    claimed.add(id(v))
                    buf.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    claimed.add(id(v))
                    buf.append(_render_hole(v))
            out.append((node.lineno, "".join(buf)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in claimed):
            out.append((node.lineno, node.value))
    return out


def _radius_facility_counts():
    """Every bbox-scoped COUNT over discovered_facilities in routes/dcpi.py."""
    found = []
    for lineno, sql in _sql_strings(_SRC):
        low = sql.lower()
        if "from discovered_facilities" not in low:
            continue
        if "count(" not in low or not _BBOX.search(sql):
            continue
        found.append((lineno, sql))
    return found


def test_the_radius_count_query_exists():
    """Guard the guard: if the query is renamed or restructured out of AST
    reach, the two tests below would pass vacuously over an empty list."""
    found = _radius_facility_counts()
    assert found, (
        "no bbox-scoped COUNT over discovered_facilities found in "
        "routes/dcpi.py — this test can no longer see the query it guards. "
        "Re-point _radius_facility_counts, do not delete the assertion.")


def test_radius_count_scopes_on_the_pointer_not_the_flag():
    """THE invariant. `is_duplicate` is a suppression bit: it double-counts the
    3,286 pointed-but-unflagged twins and drops the 1,510 flagged-but-unpointed
    real facilities. Both directions are wrong, and both move a published
    constraint_score."""
    offenders = []
    for lineno, sql in _radius_facility_counts():
        flat = " ".join(sql.split())
        if not _DEDUPED.search(sql):
            offenders.append((lineno, "no duplicate_of_id predicate", flat))
        elif re.search(r"is_duplicate", sql, re.I):
            offenders.append((lineno, "still keys on is_duplicate", flat))
    assert not offenders, (
        "radius facility COUNT with the wrong duplicate-visibility key — "
        "visibility is duplicate_of_id ALONE:\n"
        + "\n".join(f"  routes/dcpi.py:{ln}  [{why}]  {s[:150]}"
                    for ln, why, s in offenders))


# --------------------------------------------------------------------------
# Behavioral: run _local_infra_metrics itself over planted rows.
#
# _conn is swapped for a sqlite-backed shim, so the function's OWN sql and its
# OWN 16 positional parameters are what execute. Nothing about the bbox math or
# the parameter order is restated here — a reordering that fed the substation
# bbox to the facility subquery would fail test_bbox_still_bounds_the_count.
# --------------------------------------------------------------------------
_DDL = (
    "CREATE TABLE substations (lat REAL, lng REAL, voltage_kv REAL)",
    "CREATE TABLE gem_power (status TEXT, lat REAL, lng REAL, capacity_mw REAL)",
    "CREATE TABLE discovered_facilities (id INTEGER PRIMARY KEY, name TEXT, "
    "latitude REAL, longitude REAL, duplicate_of_id INTEGER, is_duplicate INTEGER)",
)

_MIA_LAT, _MIA_LON = 25.7617, -80.1918


class _Cur:
    def __init__(self, con):
        self._con = con
        self._cur = con.cursor()

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("SET"):
            return                        # SET LOCAL statement_timeout — no-op
        self._cur.execute(sql.replace("%s", "?"), params)

    def fetchone(self):
        return self._cur.fetchone()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._con = sqlite3.connect(":memory:")
        for ddl in _DDL:
            self._con.execute(ddl)
        self._con.executemany(
            "INSERT INTO discovered_facilities (id,name,latitude,longitude,"
            "duplicate_of_id,is_duplicate) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", rows)

    def cursor(self):
        return _Cur(self._con)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _dc_count(monkeypatch, rows, lat=_MIA_LAT, lon=_MIA_LON):
    monkeypatch.setattr(dcpi, "_conn", lambda: _Conn(rows))
    dcpi._LOCAL_INFRA_CACHE.clear()       # cache keys on rounded coords
    try:
        return dcpi._local_infra_metrics(lat, lon)["local_dc_count"]
    finally:
        dcpi._LOCAL_INFRA_CACHE.clear()


def _fac(i, name, dup_of=None, flag=0, lat=_MIA_LAT, lon=_MIA_LON):
    return (i, name, lat, lon, dup_of, flag)


def test_a_pointed_but_unflagged_twin_is_counted_once(monkeypatch):
    """The 3,286-row shape: pointer set, flag never set. Under the old flag
    predicate both rows counted, so the market looked twice as contested."""
    rows = [
        _fac(1, "Equinix MI1"),
        _fac(2, "Equinix MI1", dup_of=1),          # twin, UNflagged
        _fac(3, "Digital Realty MIA"),
        _fac(4, "Digital Realty MIA", dup_of=3),   # twin, UNflagged
    ]
    assert _dc_count(monkeypatch, rows) == 2, (
        "pointed-but-unflagged twins are still inflating local_dc_count")


def test_a_flagged_but_unpointed_row_is_still_counted(monkeypatch):
    """The 1,510-row shape: flagged with no pointer. That is a keeperless
    suppression, not a twin — it is a real facility and it must count. The old
    predicate dropped it."""
    rows = [
        _fac(1, "Real Facility A"),
        _fac(2, "Real Facility B", dup_of=None, flag=1),
    ]
    assert _dc_count(monkeypatch, rows) == 2, (
        "a flagged-but-unpointed facility was dropped from local_dc_count — "
        "the flag is a suppression bit, not a twin marker")


def test_flagged_and_pointed_counts_once(monkeypatch):
    """The 6,173-row shape: both set. One building, one count — under either
    key. Pinned so the two predicates cannot be confused as interchangeable
    on the strength of this majority case alone."""
    rows = [_fac(1, "Keeper"), _fac(2, "Keeper", dup_of=1, flag=1)]
    assert _dc_count(monkeypatch, rows) == 1


def test_bbox_still_bounds_the_count(monkeypatch):
    """The dedup predicate must not be the only thing left in the WHERE clause,
    and the dc bbox parameters must still reach the dc subquery. A facility on
    another continent is not local competition."""
    rows = [
        _fac(1, "Miami DC"),
        _fac(2, "London DC", lat=51.5074, lon=-0.1278),
        _fac(3, "Sydney DC", lat=-33.8688, lon=151.2093),
    ]
    assert _dc_count(monkeypatch, rows) == 1, (
        "out-of-radius facilities are being counted as local competition")


def test_no_rows_is_zero_not_an_error(monkeypatch):
    """Absence must never penalize — the r-local-granularity honesty rule."""
    assert _dc_count(monkeypatch, []) == 0


# --------------------------------------------------------------------------
# The consequence: this count is a published number, not a diagnostic.
# --------------------------------------------------------------------------
_BASE = {
    "queue_wait_months": 30, "reserve_margin_pct": 15,
    "emergency_count_30d": 1, "demand_growth_yoy_pct": 4,
}


def test_the_inflated_count_moved_a_published_constraint_score():
    """miami/doral, measured 2026-08-08: 40 facilities under the flag predicate,
    30 under the pointer. If constraint_score does not move between those two
    counts, local_dc_count has stopped feeding the score and the bug above was
    unobservable — which would mean this whole test file guards nothing."""
    inflated = dcpi.compute_constraint_score(dict(_BASE, local_dc_count=40))
    deduped = dcpi.compute_constraint_score(dict(_BASE, local_dc_count=30))
    assert inflated > deduped, (
        "local_dc_count no longer moves constraint_score — the local-competition "
        f"term is dead (both {inflated})")
    assert round(inflated - deduped, 1) == 1.5, (
        "the measured miami delta changed shape; re-measure before re-pinning "
        f"(got {inflated - deduped:+.2f} points, expected +1.5)")


def test_published_methodology_names_the_predicate_that_actually_runs():
    """/api/v1/dcpi/methodology PUBLISHES the source query for this term. It
    read `discovered_facilities WHERE COALESCE(is_duplicate,0)=0` in production
    on 2026-08-08. Fixing the SQL alone would leave that string describing a
    query the scorer no longer runs — the exact drift the r-ws3-methodology
    refactor exists to prevent ("one object, two consumers, so a published
    weight and a scoring weight cannot silently disagree")."""
    from util.dcpi_method import LOCAL_INFRA_TERMS
    term = next((t for t in LOCAL_INFRA_TERMS
                 if t["name"] == "local_dc_count"), None)
    assert term is not None, "local_dc_count dropped out of LOCAL_INFRA_TERMS"
    src = term["source"]
    assert _DEDUPED.search(src), (
        f"published methodology does not name the pointer predicate: {src!r}")
    assert "is_duplicate" not in src.lower(), (
        f"published methodology still advertises the suppression flag: {src!r}")
    # And it must agree with the SQL the module really executes.
    assert any(_DEDUPED.search(sql) for _, sql in _radius_facility_counts()), (
        "methodology claims duplicate_of_id but no executed radius COUNT uses it")


def test_double_counting_cannot_be_hidden_by_the_ceiling():
    """The term saturates at 40 facilities, so inflation is invisible in the
    biggest markets (ashburn counts 409). The defect lives BELOW the ceiling —
    exactly where a 25 km radius puts most markets. Pin that a sub-ceiling
    doubling is score-bearing, so nobody dismisses this as clipped away."""
    real = dcpi.compute_constraint_score(dict(_BASE, local_dc_count=8))
    doubled = dcpi.compute_constraint_score(dict(_BASE, local_dc_count=16))
    assert doubled > real, (
        "a sub-ceiling doubling of local_dc_count does not move the score")
