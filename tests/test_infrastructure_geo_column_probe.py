#!/usr/bin/env python3
"""tests/test_infrastructure_geo_column_probe.py — /api/v1/infrastructure must
count through a coordinate pair that HOLDS DATA, not the first one that parses.

NO NETWORK, NO DB, NO main.py IMPORT. The real `cf_stub_infrastructure` body is
lifted out of main.py with `ast` and executed against stubs, per this repo's
testing rule.

WHAT WENT WRONG (measured live 2026-09-07)
──────────────────────────────────────────
    /api/v1/infrastructure                             -> gas_pipelines 33,771
    /api/v1/infrastructure?lat=0&lon=0&radius_km=20000 -> gas_pipelines 0
        (radius 20,000 km around 0,0 = a bbox covering the whole planet)

while substations / transmission_lines_eia / discovered_power_plants each came
back with their near-total inside that same global box. `unmeasured` was empty,
so the 0 published as a measurement. Meanwhile the v2 handler
(expanded_infrastructure_api.get_hifld_gas_pipelines) returned real rows off the
SAME TABLE at the same coordinates — ONEOK Westex at lat 31.8925 near Midland.

THE MECHANISM. The probe took the first (lat_col, lon_col) pair whose query DID
NOT RAISE. Measured columns on gas_pipelines (via /api/v1/admin/schema):

    id name operator pipeline_type diameter_inches capacity_mcf status
    lat(real) lng(real) city state country source source_id
    created_at updated_at commodity diameter_in length_miles
    lon(double precision) last_updated

It is the ONLY one of the four tables carrying a third coordinate column — the
other three have exactly `lat` and `lng` — and the shipped LON_COLS orders
`lon` BEFORE `lng`. `lon` is entirely NULL, so `lat`/`lon` parsed, counted 0,
and `lat`/`lng` was never reached. Nothing raised, so nothing was recorded as
unmeasured.

"The query parsed" is not the same question as "the query can answer". That
distinction is the whole fix, and it is the same defect class the endpoint's own
docstring already names: a silent 0 standing in for a measurement that never
happened.

THE CONTRACT
────────────
  C1. A pair that exists and is entirely NULL is NOT the answer — keep probing.
  C2. The pair that carries data wins, whatever its position in LON_COLS.
  C3. `geo_columns` publishes the pair each count was measured through, so a 0
      is readable as "none nearby" instead of "read the wrong column".
  C4. A rejected all-NULL pair is NAMED, not silently dropped.
  C5. When NO pair on a table carries data, the count is null + `unmeasured` —
      never 0.
  C6. A table with no coordinate columns at all stays null + `unmeasured`
      (pre-existing behaviour, pinned so the fix does not erode it).
  C7. A genuinely empty bbox STILL reports 0. The fix must not launder every 0
      into null — that would trade a wrong number for a missing one.
  C8. Global (no lat/lon) mode is untouched: plain COUNT(*), no geo_columns —
      and it resolves no columns, so it must not pay for a catalog read.
  C9. Which columns exist is read ONCE from information_schema, not discovered
      by firing statements at missing columns. Measured on the four real
      schemas: 24 probes of which 19 RAISED (each aborting the transaction and
      needing a rollback) becomes 1 catalog read + 5 probes + 0 rollbacks.
      A catalog read that FAILS falls back to discover-by-failing and must
      still produce the same answer — otherwise the cleanup trades wasted
      statements for an outage.

EXPECTED PASS/FAIL — MEASURED, not predicted, by checking each tree out into a
clean worktree with __pycache__ purged and dropping this file in:

    origin/main, no fix at all            10 failed,  6 passed
    the fix without the C9 cleanup         3 failed, 13 passed
    this branch                            0 failed, 16 passed

The 3 in the middle row are exactly C9's guards. Re-measure rather than
trusting this comment.

Nothing here runs at module scope.

Run standalone:   python3 -m pytest tests/test_infrastructure_geo_column_probe.py -v
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"

HANDLER = "cf_stub_infrastructure"

# Midland TX — the coordinate the live v2 handler answers with ONEOK Westex.
MIDLAND = (31.9, -102.3)


# ── a Postgres-shaped fake ───────────────────────────────────────────────────

class UndefinedColumn(Exception):
    """psycopg2.errors.UndefinedColumn, near enough for this handler."""


class _Table:
    def __init__(self, columns, rows):
        self.columns = list(columns)
        self.rows = list(rows)


class _FakeCursor:
    """Answers exactly the three query shapes this handler issues.

    Two real Postgres behaviours are modelled on purpose, because the bug lives
    in the gap between them:
      * a missing column raises, and
      * an all-NULL column does NOT raise — it returns no rows.
    A failed statement also aborts the transaction until rollback(), so the
    handler's rollback discipline is exercised rather than assumed.
    """

    _PROBE = re.compile(
        r"^SELECT 1 FROM (\w+) WHERE (\w+) IS NOT NULL AND (\w+) IS NOT NULL LIMIT 1$")
    _BBOX = re.compile(
        r"^SELECT COUNT\(\*\) FROM (\w+) WHERE (\w+) BETWEEN %s AND %s "
        r"AND (\w+) BETWEEN %s AND %s$")
    _TOTAL = re.compile(r"^SELECT COUNT\(\*\) FROM (\w+)$")
    _CATALOG = re.compile(
        r"^SELECT table_name, column_name FROM information_schema\.columns "
        r"WHERE table_name = ANY\(%s\) AND column_name = ANY\(%s\) "
        r"AND table_schema = ANY\(current_schemas\(false\)\)$")

    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    # -- helpers ----------------------------------------------------------
    def _table(self, name):
        assert name in self._conn.tables, "handler queried unknown table %r" % name
        return self._conn.tables[name]

    def _require(self, table, *cols):
        for c in cols:
            if c not in table.columns:
                raise UndefinedColumn('column "%s" does not exist' % c)

    @staticmethod
    def _between(value, lo, hi):
        # NULL BETWEEN ... is NULL, which never satisfies a WHERE.
        return value is not None and lo <= value <= hi

    # -- DB-API -----------------------------------------------------------
    def execute(self, sql, params=None):
        if self._conn.aborted:
            raise UndefinedColumn(
                "current transaction is aborted, commands ignored until "
                "end of transaction block")
        q = " ".join(sql.split())
        self._conn.queries.append(q)

        m = self._CATALOG.match(q)
        if m:
            self._conn.catalog_reads += 1
            if self._conn.catalog_fails:
                self._conn.aborted = True
                raise UndefinedColumn("simulated catalog read failure")
            wanted_tables, wanted_cols = params
            self._rows = [
                (name, col)
                for name in wanted_tables if name in self._conn.tables
                for col in self._conn.tables[name].columns if col in wanted_cols
            ]
            return

        m = self._PROBE.match(q)
        if m:
            name, lat_col, lon_col = m.groups()
            t = self._table(name)
            try:
                self._require(t, lat_col, lon_col)
            except UndefinedColumn:
                self._conn.aborted = True
                raise
            hit = any(r.get(lat_col) is not None and r.get(lon_col) is not None
                      for r in t.rows)
            self._rows = [(1,)] if hit else []
            return

        m = self._BBOX.match(q)
        if m:
            name, lat_col, lon_col = m.groups()
            t = self._table(name)
            try:
                self._require(t, lat_col, lon_col)
            except UndefinedColumn:
                self._conn.aborted = True
                raise
            lat_lo, lat_hi, lon_lo, lon_hi = params
            n = sum(1 for r in t.rows
                    if self._between(r.get(lat_col), lat_lo, lat_hi)
                    and self._between(r.get(lon_col), lon_lo, lon_hi))
            self._rows = [(n,)]
            return

        m = self._TOTAL.match(q)
        if m:
            self._rows = [(len(self._table(m.group(1)).rows),)]
            return

        raise AssertionError(
            "handler issued SQL this fake does not model, so the test would be "
            "measuring nothing: %r" % q)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, tables, catalog_fails=False):
        self.tables = tables
        self.queries = []
        self.aborted = False
        self.rollbacks = 0
        self.returned = 0
        self.catalog_reads = 0
        self.catalog_fails = catalog_fails

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False


# ── fixtures shaped like the real schema ─────────────────────────────────────

def _gas_rows(n=40):
    """Populated `lat`/`lng`, entirely NULL `lon` — the measured shape."""
    return [{"id": i, "lat": 31.85 + i * 0.002, "lng": -102.35 + i * 0.002,
             "lon": None}
            for i in range(n)]


def _plain_rows(lat0, lng0, n):
    return [{"id": i, "lat": lat0 + i * 0.002, "lng": lng0 + i * 0.002}
            for i in range(n)]


def _schema(gas_columns=("id", "lat", "lng", "lon"), gas_rows=None,
            tl_columns=("id", "lat", "lng"), tl_rows=None,
            dpp_columns=("id", "lat", "lng"), dpp_rows=None):
    return {
        "substations": _Table(("id", "lat", "lng"), _plain_rows(31.88, -102.32, 12)),
        "transmission_lines_eia": _Table(
            tl_columns,
            _plain_rows(31.88, -102.32, 7) if tl_rows is None else tl_rows),
        "gas_pipelines": _Table(
            gas_columns, _gas_rows() if gas_rows is None else gas_rows),
        "discovered_power_plants": _Table(
            dpp_columns,
            _plain_rows(31.88, -102.32, 5) if dpp_rows is None else dpp_rows),
    }


# ── extraction ───────────────────────────────────────────────────────────────

class _Args(dict):
    def get(self, key, default=None, type=None):
        if key not in self:
            return default
        v = self[key]
        return type(v) if type is not None else v


class _Req:
    def __init__(self, args):
        self.args = _Args(args)


def _fn():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == HANDLER), None)
    assert fn is not None, "%s not found in main.py" % HANDLER
    assert fn.body, "%s parsed with an EMPTY body" % HANDLER
    return fn


def _shipped_local_list(name):
    """Literal-eval a list assigned inside the handler body."""
    for node in ast.walk(_fn()):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == name for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("%s not assigned inside %s" % (name, HANDLER))


def _call(tables, catalog_fails=False, **args):
    fn = _fn()
    fn.decorator_list = []
    conn = _FakeConn(tables, catalog_fails=catalog_fails)

    def jsonify(*a, **kw):
        return dict(kw) if kw else (a[0] if a else {})

    def _return(c):
        conn.returned += 1

    ns = {
        "jsonify": jsonify,
        "request": _Req(args),
        "get_pg_connection": lambda: conn,
        "return_pg_connection": _return,
        "_INFRA_COUNT_BASIS": {},
        "_INFRA_KNOWN_LAYERS": ("substations",),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<main>", "exec"), ns)
    body = ns[HANDLER]()
    body = body[0] if isinstance(body, tuple) else body
    assert body.get("success") is True, "handler errored: %r" % (body,)
    return body, conn


def _near_midland(tables, radius_km=40, catalog_fails=False):
    lat, lon = MIDLAND
    return _call(tables, catalog_fails=catalog_fails,
                 lat=lat, lon=lon, radius_km=radius_km)


# ── guard the guard ──────────────────────────────────────────────────────────

def test_the_shipped_probe_order_is_what_makes_this_a_trap():
    """If LON_COLS ever stopped putting an all-NULL candidate ahead of the
    populated one, every assertion below would pass on any tree — vacuously.
    Re-point this at whatever the new trap is; do not delete it."""
    lon_cols = _shipped_local_list("LON_COLS")
    assert "lon" in lon_cols and "lng" in lon_cols, lon_cols
    assert lon_cols.index("lon") < lon_cols.index("lng"), (
        "LON_COLS no longer probes the all-NULL `lon` before the populated "
        "`lng`; this file is no longer testing the 2026-09-07 defect: %r"
        % (lon_cols,))


def test_the_fixture_reproduces_the_ambiguity():
    """The fake gas table must genuinely offer both pairs, or C1/C2 are moot."""
    gas = _schema()["gas_pipelines"]
    assert {"lat", "lng", "lon"} <= set(gas.columns)
    assert all(r["lon"] is None for r in gas.rows), "fixture `lon` is not all-NULL"
    assert any(r["lng"] is not None for r in gas.rows), "fixture `lng` is empty"


# ── C1 + C2: the regression ──────────────────────────────────────────────────

def test_gas_pipelines_counts_through_the_populated_pair():
    """THE REGRESSION. Rows exist at this coordinate; the answer must not be 0
    because an all-NULL column sorted first."""
    body, _ = _near_midland(_schema())
    n = body["counts"]["gas_pipelines"]
    assert n is not None, "gas_pipelines came back unmeasured: %r" % (body.get("unmeasured"),)
    assert n > 0, (
        "gas_pipelines counted 0 near Midland while the table holds rows there "
        "— the probe took the all-NULL `lon` and never reached `lng`. "
        "counts=%r geo_columns=%r" % (body["counts"], body.get("geo_columns")))


def test_a_whole_planet_bbox_cannot_return_zero_for_a_populated_table():
    """The live probe, as a test: 0 over every coordinate on earth is a
    measurement of the schema, not of the world."""
    body, _ = _call(_schema(), lat=0.0, lon=0.0, radius_km=20000.0)
    tables = _schema()
    for name, count in body["counts"].items():
        assert count == len(tables[name].rows), (
            "%s: %r of %d rows inside a bbox covering the planet"
            % (name, count, len(tables[name].rows)))


# ── C3 + C4: a 0 must be distinguishable from a bad probe ────────────────────

def test_geo_columns_publishes_the_pair_each_count_came_through():
    body, _ = _near_midland(_schema())
    geo = body.get("geo_columns")
    assert geo, "no geo_columns published — a 0 here is unattributable"
    assert geo["gas_pipelines"]["lat_col"] == "lat"
    assert geo["gas_pipelines"]["lon_col"] == "lng", (
        "counted through %r, not the populated `lng`" % (geo["gas_pipelines"],))
    for name in ("substations", "transmission_lines_eia", "discovered_power_plants"):
        assert geo[name] == {"lat_col": "lat", "lon_col": "lng"}, (name, geo[name])


def test_the_rejected_all_null_pair_is_named():
    body, _ = _near_midland(_schema())
    skipped = body["geo_columns"]["gas_pipelines"].get("skipped_all_null")
    assert skipped == ["lat/lon"], (
        "the all-NULL pair was dropped silently: %r" % (skipped,))


def test_only_the_table_that_has_a_decoy_reports_one():
    """The other three tables have no all-NULL pair, so nothing to skip."""
    body, _ = _near_midland(_schema())
    for name in ("substations", "transmission_lines_eia", "discovered_power_plants"):
        assert "skipped_all_null" not in body["geo_columns"][name], name


# ── C5: no pair carries data -> null, not 0 ──────────────────────────────────

def test_a_table_whose_every_pair_is_all_null_is_unmeasured_not_zero():
    tables = _schema(gas_rows=[{"id": i, "lat": None, "lng": None, "lon": None}
                               for i in range(9)])
    body, _ = _near_midland(tables)
    assert body["counts"]["gas_pipelines"] is None, (
        "published %r for a table with no usable coordinates"
        % (body["counts"]["gas_pipelines"],))
    why = body["unmeasured"]["gas_pipelines"]
    assert "no non-null" in why, why
    assert "NOT a count of zero assets nearby" in why, why


# ── C6: no coordinate columns at all (pre-existing behaviour) ────────────────

def test_a_table_with_no_coordinate_columns_is_unmeasured_not_zero():
    tables = _schema(dpp_columns=("id", "name"),
                     dpp_rows=[{"id": i, "name": "p%d" % i} for i in range(4)])
    body, _ = _near_midland(tables)
    assert body["counts"]["discovered_power_plants"] is None
    why = body["unmeasured"]["discovered_power_plants"]
    assert "no latitude/longitude column pair" in why, why
    assert "discovered_power_plants" not in body.get("geo_columns", {})


# ── C7: a real 0 must survive ────────────────────────────────────────────────

def test_a_genuinely_empty_bbox_still_reports_zero():
    """The fix must not launder every 0 into null. Antarctica has no rows in
    any fixture, and 0 is the correct, measured answer there."""
    body, _ = _call(_schema(), lat=-82.0, lon=25.0, radius_km=30.0)
    for name, count in body["counts"].items():
        assert count == 0, (name, count)
    assert not body.get("unmeasured"), body.get("unmeasured")
    assert body["geo_columns"]["gas_pipelines"]["lon_col"] == "lng", (
        "a 0 that cannot name the column it was measured through is the bug")


# ── C8: global mode untouched ────────────────────────────────────────────────

def test_global_mode_is_a_plain_count_with_no_geo_columns():
    tables = _schema()
    body, conn = _call(tables)
    for name, count in body["counts"].items():
        assert count == len(tables[name].rows), (name, count)
    assert "geo_columns" not in body
    assert "filter" not in body
    assert not any("BETWEEN" in q for q in conn.queries), conn.queries


def test_the_connection_is_returned_to_the_pool():
    _, conn = _near_midland(_schema())
    assert conn.returned == 1, "connection not returned (%d)" % conn.returned


# ── C9: the catalog replaces discover-by-failing ─────────────────────────────

def test_no_statement_is_fired_at_a_column_that_does_not_exist():
    """THE CLEANUP. Which columns exist is read once from information_schema.
    Discovering it by firing queries and catching the failures cost ~20
    deliberately-failing statements per geo request, each aborting the
    transaction and needing a rollback before the next could run."""
    _, conn = _near_midland(_schema())
    assert conn.catalog_reads == 1, (
        "expected exactly one catalog read, got %d" % conn.catalog_reads)
    assert conn.rollbacks == 0, (
        "%d rollback(s) — a statement was still fired at a missing column: %r"
        % (conn.rollbacks, conn.queries))
    # `latitude`/`longitude` exist on none of the four tables. They may appear
    # only as catalog PARAMETERS, never interpolated into a table query.
    fired = [q for q in conn.queries if not _FakeCursor._CATALOG.match(q)]
    assert fired, "no table queries issued at all — the test measures nothing"
    for q in fired:
        assert "latitude" not in q and "longitude" not in q, q


def test_one_probe_per_table_not_one_per_candidate_pair():
    """Only the pairs the catalog says exist are probed: `lat`/`lng` on three
    tables, and `lat`/`lon` then `lat`/`lng` on the gas decoy = 5 probes."""
    _, conn = _near_midland(_schema())
    probes = [q for q in conn.queries if _FakeCursor._PROBE.match(q)]
    assert len(probes) == 5, "%d probes: %r" % (len(probes), probes)


def test_a_failed_catalog_read_falls_back_to_discover_by_failing():
    """The degraded path must still produce the RIGHT answer, not an error.
    Without this the cleanup would trade ~20 wasted statements for an outage
    whenever information_schema is unreadable."""
    body, conn = _near_midland(_schema(), catalog_fails=True)
    assert conn.catalog_reads == 1
    assert conn.rollbacks > 0, "fallback did not fire any exploratory statement"
    assert body["counts"]["gas_pipelines"] > 0, (
        "fallback lost the answer: %r" % (body["counts"],))
    assert body["geo_columns"]["gas_pipelines"]["lon_col"] == "lng"
    assert body["geo_columns"]["gas_pipelines"]["skipped_all_null"] == ["lat/lon"]


def test_the_catalog_is_not_read_at_all_in_global_mode():
    """No bbox, no column resolution — global mode is a plain COUNT(*)."""
    _, conn = _call(_schema())
    assert conn.catalog_reads == 0, conn.queries
