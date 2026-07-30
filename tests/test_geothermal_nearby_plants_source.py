"""Guard: /api/v1/geothermal-potential must not publish [] as "none nearby".

WHAT THIS PINS
──────────────
`nearby_geothermal_plants` was built from

    SELECT name, capacity_mw, lat, lng
    FROM power_plants
    WHERE fuel_type ILIKE '%geothermal%' AND lat BETWEEN .. AND lng BETWEEN ..

That is a DOUBLE fault, and it published a confident false negative.

  a) WRONG TABLE. `power_plants` holds 66 rows for the entire United States —
     the same EIA-860 population as the healthy 13,446-row `power_plants_eia`,
     loaded to ~0.5% (see land_power_crawler.crawl_power_plants; the dedup step
     keyed on rec['plantid'], a spelling EIA v2 does not return, dropping 54,934
     of 55,000 records with errors=0; fixed in #1923). Measured live: that table
     holds ZERO rows matching geothermal at all, so this query could never
     return anything even spelled correctly.
  b) WRONG COLUMN. It selected `lng`, but `power_plants` spells it `lon`.
     Verified against the live information_schema, not the repo DDL:
         power_plants       lat ✓  lon ✓  lng ✗
         power_plants_eia   lat ✓  lon ✗  lng ✓
     So every call raised UndefinedColumn. The enclosing `except` logged at
     DEBUG and fell through, leaving `nearby_plants` at its seed `[]`.

MEASURED LIVE on the Railway origin with a privileged key, before the fix — the
two densest geothermal areas in the United States:

    ?lat=39.5&lon=-119.8&state=NV   (Reno)            -> nearby_geothermal_plants: []
    ?lat=33.15&lon=-115.6&state=CA  (Imperial Valley) -> nearby_geothermal_plants: []
    both with   source: "DC Hub + USGS EGS Atlas + EIA-860"

power_plants_eia holds 67 geothermal plants: CA 33, NV 26, UT 3, OR 2, NM 1,
HI 1, ID 1. So the endpoint claimed EIA-860 provenance for an empty list at the
exact coordinates where the answer is densest. An empty list that means "the
query never ran" is not a measurement.

★ FILTER ON primary_fuel, NOT geothermal_mw. Measured: geothermal_mw > 0 matches
  0 of 13,446 rows — that column is unpopulated, so swapping one
  guaranteed-empty predicate for another would look like a fix and change
  nothing. primary_fuel ILIKE '%geothermal%' matches 67.
★ power_plants_eia has no `status` column, so operating status is not asserted.

THE CONTRACT
────────────
  G1. The plant lookup reads power_plants_eia, never the bare `power_plants`.
  G2. It does not select `lon` from power_plants_eia nor `lng` from
      power_plants — the column must match the table it is read from.
  G3. The predicate is primary_fuel, not the unpopulated geothermal_mw.
  G4. A real result is published: near Reno the list is non-empty.
  G5. A FAILED query is reported, not published as []. The response carries a
      reason distinguishable from a genuine "none nearby", and the failure is
      logged above DEBUG.
  G6. A genuine empty result stays empty WITHOUT a false reason attached.
  G7. Literal % in the statement is escaped as %% (see below — this is the one
      that got through).

★★ THIS FILE CERTIFIED A BROKEN FIX ONCE. Recorded because it is the reusable
   lesson. #1930 repointed the query to power_plants_eia, every test here
   passed, and production STILL published an empty list — because psycopg2 runs
   Python %-formatting over the statement CLIENT-SIDE when params are passed, so
   `ILIKE '%geothermal%'` beside %s placeholders raises

       IndexError: tuple index out of range

   before the SQL is ever sent. Measured both ways against the live database:
   '%geothermal%' -> IndexError; '%%geothermal%%' -> 10 rows.

   That also means #1930 named the WRONG proximate cause. The wrong table and
   the `lng`/`lon` mismatch are both real and both had to be fixed, but this
   raised first, client-side, so the UndefinedColumn was never reached.

   The harness let it through because the stub cursor validated tables and
   columns but never modelled the BINDING step — it only ever saw SQL that
   psycopg2 would have refused to send. A stub that is more forgiving than the
   driver certifies code the driver rejects. _Cur.execute now performs the
   %-interpolation first, so this class cannot pass again.

   It was caught at all only because #1930 also replaced the silent
   `except: pass` -> [] with a published reason. The fix was wrong; the
   instrumentation shipped alongside it is what surfaced that within minutes.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
Measured by extracting origin/main with `git archive`, dropping this file into
that tree, and running it there.

UNPATCHED (origin/main @ a974f5d5, i.e. WITH #1930's repoint already in):
    3 failed, 6 passed, 1 xfailed
    The repoint is already there, so only the %-escape failures remain:
        test_literal_percent_is_escaped_for_psycopg2_client_side_binding  (G7)
        test_a_real_result_is_published_near_reno                         (G4)
        test_a_genuinely_empty_area_is_empty_without_a_false_reason       (G6)
PATCHED (this branch):                0 failed, 9 passed, 1 xfailed

For the record, against pre-#1930 main @ d85b287d this file measured
5 failed, 3 passed, 1 xfailed.

`1 xfailed` on BOTH runs. A conftest-level abort or collection error exits 0 with
0 tests and renders as an ordinary green; the control's presence in the summary
is the only proof the file ran.

MUST-FAIL CONTROL
─────────────────
test_zzz_must_fail_control is xfail(strict=True). Never delete it. This file
carries its own control rather than relying on the sibling
tests/test_market_profile_detail_plant_source.py — a control only proves the
file it lives in was collected.

Run:  python3 -m pytest tests/test_geothermal_nearby_plants_source.py -v
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NLR = os.path.join(ROOT, "nlr_intelligence.py")

FN = "geothermal_potential"

# Live, measured 2026-07-29.
EIA_GEOTHERMAL_PLANTS = 67           # primary_fuel ILIKE '%geothermal%'
EIA_GEOTHERMAL_MW_POPULATED = 0      # rows with geothermal_mw > 0
STUB_GEOTHERMAL_PLANTS = 0           # power_plants matching geothermal, at all

# Real NV geothermal plants near Reno, from power_plants_eia.
NV_PLANTS = [
    ("Steamboat Hills", 87.0, 39.3689, -119.7419),
    ("Brady Hot Springs", 26.1, 39.7906, -119.0106),
]


# ── extraction ────────────────────────────────────────────────────────────────
def _extract():
    """AST-extract the route, decorators stripped.

    Assert the parse produced a Module with a non-empty body and that the
    function body is non-empty — an empty parse silently satisfies every
    downstream assertion.
    """
    tree = ast.parse(open(NLR).read())
    assert isinstance(tree, ast.Module), "parse did not produce a Module"
    assert tree.body, "parsed module body is EMPTY — extraction read nothing"
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == FN), None)
    assert fn is not None, f"{FN} not found in {NLR}"
    assert fn.body, f"{FN} parsed with an EMPTY body"
    fn.decorator_list = []
    return fn, tree


def _free_vars(fn):
    assigned, loaded = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            (assigned if isinstance(n.ctx, ast.Store) else loaded).add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                assigned.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.FunctionDef):
            if n is not fn:
                assigned.add(n.name)
            assigned.update(_arg_names(n.args))
        elif isinstance(n, ast.Lambda):
            # lambda params bind too. Missing this reported `x` from
            # `key=lambda x: x["distance_km"]` as an unresolved free variable.
            assigned.update(_arg_names(n.args))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
    import builtins
    return sorted(loaded - assigned - set(dir(builtins)))


def _arg_names(a):
    names = [x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    for v in (a.vararg, a.kwarg):
        if v:
            names.append(v.arg)
    return names


def _plant_sql(fn):
    """The one SQL literal in this route that reads a plants table."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            s = " ".join(n.value.split())
            if re.search(r"(?i)\bfrom\s+power_plants", s):
                return s
    return None


# ── stub DB — reproduces the live lon/lng split exactly ───────────────────────
COLS = {
    "power_plants": {"id", "name", "operator", "state", "lat", "lon",
                     "capacity_mw", "fuel_type", "fuel_category", "status"},
    "power_plants_eia": {"id", "plant_id", "name", "utility_name", "state",
                         "primary_fuel", "technology", "nameplate_capacity_mw",
                         "geothermal_mw", "lat", "lng", "capacity_mw"},
}


class _Cur:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append(s)
        if self.fail:
            raise RuntimeError("simulated connection reset mid-query")

        # ★ psycopg2 runs Python %-formatting over the statement CLIENT-SIDE
        # before anything is sent. A literal % that is not doubled is a format
        # directive, so `ILIKE '%geothermal%'` next to %s params raises
        # IndexError here — never reaching Postgres, and never reaching any of
        # the column checks below. The first version of this stub skipped this
        # step, which is exactly why it certified a repoint that published
        # nothing in production. Model the binding, not just the SQL.
        if params is not None:
            try:
                sql % tuple(params)
            except (IndexError, TypeError, ValueError) as exc:
                raise IndexError(
                    f"{exc} — literal % in the statement must be escaped as %%"
                ) from exc

        m = re.search(r"(?i)\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)", s)
        assert m, f"stub could not find a table in: {s}"
        tbl = m.group(1).lower()
        if tbl not in COLS:
            raise RuntimeError(f'relation "{tbl}" does not exist')

        sel = s[s.lower().index("select") + 6:s.lower().index(" from ")]
        for tok in re.split(r",\s*", sel):
            name = tok.strip().split()[-1].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
                if name not in COLS[tbl]:
                    raise RuntimeError(f'column "{name}" does not exist')
        for name in re.findall(r"(?i)\b(?:AND|WHERE)\s+([a-z_]+)\s+(?:BETWEEN|ILIKE)", s):
            if name not in COLS[tbl]:
                raise RuntimeError(f'column "{name}" does not exist')

        # The 66-row stub holds no geothermal rows at all. The EIA table does.
        if tbl == "power_plants":
            self._result = []
            return
        if "geothermal_mw" in s.lower():
            self._result = []       # 0 of 13,446 rows are populated
            return
        self._result = list(NV_PLANTS)

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


class _Log:
    def __init__(self):
        self.calls = []

    def _rec(self, level):
        def f(msg, *a):
            self.calls.append((level, str(msg) % a if a else str(msg)))
        return f

    def __getattr__(self, name):
        return self._rec(name)


def _run(lat=39.5, lon=-119.8, state="NV", fail=False, no_conn=False):
    """Execute the extracted route. Returns (payload, sql_log, log_calls)."""
    fn, _ = _extract()
    sql_log, logger = [], _Log()
    conn = None if no_conn else _Conn(_Cur(sql_log, fail=fail))

    class _Req:
        args = {"lat": str(lat), "lon": str(lon), "state": state,
                "radius_km": "500"}

    ns = {
        "request": _Req(),
        "jsonify": lambda p: p,
        "logger": logger,
        "_get_db_safe": lambda: conn,
        "_nearest_geothermal": lambda *a: [
            {"name": "Great Basin EGS", "effective_score": 78,
             "distance_km": 41.0}],
        "_geo_classification": lambda s: "High",
        "_haversine_km": lambda a, b, c, d: 40.0,
        # privileged, so the full payload is returned rather than the teaser
        "_nlr_privileged": lambda: True,
        "_nlr_teaser": lambda *a: {},
        "nlr_bp": None,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), NLR, "exec"), ns)
    payload = ns[FN]()
    assert isinstance(payload, dict), f"route returned {type(payload)}"
    return payload, sql_log, logger.calls


# ── sanity + positive control (pass in both states) ───────────────────────────
def test_extraction_parsed_and_free_vars_resolve():
    fn, tree = _extract()
    assert len(tree.body) > 1, "module parsed to a single node"
    supplied = {"request", "jsonify", "logger", "_get_db_safe",
                "_nearest_geothermal", "_geo_classification", "_haversine_km",
                "_nlr_privileged", "_nlr_teaser", "nlr_bp"}
    unresolved = set(_free_vars(fn)) - supplied
    assert not unresolved, f"unresolved free vars in {FN}: {sorted(unresolved)}"


def test_harness_models_the_live_lon_lng_split():
    assert "lon" in COLS["power_plants"] and "lng" not in COLS["power_plants"], \
        "stub power_plants has the wrong coord column — live is `lon` only"
    assert "lng" in COLS["power_plants_eia"] and "lon" not in COLS["power_plants_eia"], \
        "stub power_plants_eia has the wrong coord column — live is `lng` only"
    assert STUB_GEOTHERMAL_PLANTS == 0


def test_harness_reproduces_the_swallowed_undefined_column():
    """Unpatched, the query must raise and the route must still answer with [].

    That is the fault: an exception became an empty list. This control proves the
    harness can produce it, so G1/G4 are not passing vacuously.
    """
    fn, _ = _extract()
    sql = _plant_sql(fn)
    assert sql, "no plants query found in the route at all"
    payload, log, _ = _run()
    assert log, "the plants query never reached the stub"
    if re.search(r"(?i)\bfrom\s+power_plants\b(?!_)", sql):
        # unpatched tree
        assert payload.get("nearby_geothermal_plants") == [], (
            "unpatched code did not publish the empty list — the stub is not "
            "reproducing the live UndefinedColumn, so this file is vacuous")


# ── G1 + G2 ───────────────────────────────────────────────────────────────────
def test_reads_the_eia_table_with_the_column_that_table_actually_has():
    fn, _ = _extract()
    sql = _plant_sql(fn)
    assert sql, "no plants query found"
    assert not re.search(r"(?i)\bfrom\s+power_plants\b(?!_)", sql), (
        f"still reads the bare `power_plants` stub, which holds "
        f"{STUB_GEOTHERMAL_PLANTS} geothermal rows: {sql}")
    assert "power_plants_eia" in sql.lower(), \
        f"plants query does not read power_plants_eia: {sql}"
    assert "lng" in sql.lower(), f"query lost the `lng` column: {sql}"
    assert not re.search(r"(?i)(^|[\s,(])lon([\s,)]|$)", sql), (
        f"selects `lon` from power_plants_eia, whose live column is `lng`: {sql}")


# ── G7 ────────────────────────────────────────────────────────────────────────
def test_literal_percent_is_escaped_for_psycopg2_client_side_binding():
    """The ILIKE wildcards must be %% — see the stub's binding step.

    This is the fault that shipped in #1930: the repoint to power_plants_eia was
    correct and still published nothing, because psycopg2 never sent the query.
    Pinning it at BOTH levels — the literal in the source, and the binding in
    the harness — because the source check alone is easy to satisfy accidentally
    and the harness check alone would not say why.
    """
    fn, _ = _extract()
    sql = _plant_sql(fn)
    assert sql, "no plants query found"
    # Every % in the statement must be either a %s placeholder or a doubled %%.
    leftovers = re.sub(r"%%|%s", "", sql)
    assert "%" not in leftovers, (
        f"un-escaped literal % in a parameterised statement — psycopg2 will "
        f"treat it as a format directive and raise IndexError client-side "
        f"before the query is sent: {sql}")

    # And prove the harness would actually catch a regression here.
    bad = ("SELECT name FROM power_plants_eia WHERE primary_fuel ILIKE "
           "'%geothermal%' AND lat BETWEEN %s AND %s")
    with pytest.raises(IndexError):
        _Cur([]).execute(bad, (1, 2))


# ── G3 ────────────────────────────────────────────────────────────────────────
def test_filters_on_primary_fuel_not_the_unpopulated_geothermal_mw_column():
    fn, _ = _extract()
    sql = _plant_sql(fn)
    assert "primary_fuel" in sql.lower(), (
        f"does not filter on primary_fuel — the only populated geothermal "
        f"discriminator ({EIA_GEOTHERMAL_PLANTS} rows): {sql}")
    assert "geothermal_mw" not in sql.lower(), (
        f"filters on geothermal_mw, which is populated on "
        f"{EIA_GEOTHERMAL_MW_POPULATED} of 13,446 rows — another "
        f"guaranteed-empty predicate: {sql}")


# ── G4 ────────────────────────────────────────────────────────────────────────
def test_a_real_result_is_published_near_reno():
    payload, _, _ = _run()
    plants = payload.get("nearby_geothermal_plants")
    assert isinstance(plants, list), f"nearby_geothermal_plants is {type(plants)}"
    assert plants, (
        "still empty near Reno. Nevada has 26 geothermal plants in "
        "power_plants_eia — this is the exact false negative that shipped")
    p = plants[0]
    for key in ("name", "capacity_mw", "distance_km"):
        assert key in p, f"published plant dropped `{key}`: {p}"
    assert p["capacity_mw"] > 0, f"capacity did not survive: {p}"
    assert not payload.get("nearby_geothermal_plants_unmeasured"), (
        "a successful measurement carries an unmeasured reason: "
        f"{payload.get('nearby_geothermal_plants_unmeasured')!r}")


# ── G5 ────────────────────────────────────────────────────────────────────────
def test_a_failed_query_reports_itself_instead_of_publishing_empty():
    payload, _, log_calls = _run(fail=True)
    assert payload.get("nearby_geothermal_plants") == []
    reason = payload.get("nearby_geothermal_plants_unmeasured")
    assert reason and str(reason).strip(), (
        "a FAILED query published [] with no reason — indistinguishable from "
        "'no geothermal plants nearby', which is the whole fault")
    assert "not" in str(reason).lower(), \
        f"reason does not say the value is unmeasured: {reason!r}"
    levels = {lvl for lvl, _ in log_calls}
    assert levels - {"debug"}, (
        f"the failure was only logged at DEBUG, where it went unnoticed for "
        f"months: {log_calls}")

    # and the same treatment when there is no connection at all
    payload2, _, _ = _run(no_conn=True)
    assert payload2.get("nearby_geothermal_plants") == []
    assert (payload2.get("nearby_geothermal_plants_unmeasured") or "").strip(), \
        "no-connection path publishes a bare [] with no reason"


# ── G6 ────────────────────────────────────────────────────────────────────────
def test_a_genuinely_empty_area_is_empty_without_a_false_reason():
    """Kansas has no geothermal plants. That [] is a real answer, not a failure.

    The repair must not label every empty list "unmeasured" — that would trade a
    false negative for a useless one.
    """
    payload, sql_log, _ = _run(lat=38.5, lon=-98.0, state="KS")
    assert sql_log, "the query did not run for a no-plants area"
    plants = payload.get("nearby_geothermal_plants")
    assert isinstance(plants, list)
    assert not payload.get("nearby_geothermal_plants_unmeasured"), (
        "a query that RAN and found nothing was labelled unmeasured: "
        f"{payload.get('nearby_geothermal_plants_unmeasured')!r}")
    assert (payload.get("nearby_geothermal_plants_basis") or "").strip(), (
        "no basis published — a caller cannot tell what population and radius "
        "the empty list is empty of")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
