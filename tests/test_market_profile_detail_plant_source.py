"""Guard: /api/land-power/market-profile/<market> — plant source, and scope.

WHAT THIS PINS
──────────────
GET /api/land-power/market-profile/<market> had THREE faults. All were measured
live on production (https://dchub-backend-production.up.railway.app) and against
the live Neon schema on 2026-07-29, before the fix.

1. WRONG TABLE. `large_power_plants` was built from

       SELECT name, operator, capacity_mw, fuel_category, lat, lon
       FROM power_plants WHERE state = %s AND capacity_mw > 100 LIMIT 20

   `power_plants` holds 66 rows for the entire United States. It is NOT a
   differently-scoped population — it is the SAME EIA-860 plant population as
   the healthy 13,446-row `power_plants_eia`, loaded to ~0.5%: the 2026-03-30
   `eia-860-plants` run FETCHED 55,000 records and UPSERTED 66 with errors=0,
   because the dedup step keyed on rec['plantid'] (a spelling the EIA v2
   facility-fuel response does not return) and silently `continue`d the rest.
   errors is only incremented by the INSERT handler, which the 54,934 dropped
   records never reached. Fixed in #1923; the 66 rows are that pre-fix build and
   are what is live today.

   Live measurement of what that published, per market:

       markets in market_power_profiles                      42
       markets whose large_power_plants list was EMPTY        34
       markets that got 1-2 rows                              8
       states with ANY row in power_plants                    13  (of 52)
       rows in power_plants with capacity_mw > 100            17  nationwide
       Austin / Dallas-Fort Worth / Houston / San Antonio    [] each
       TX rows in power_plants_eia with >100 MW nameplate    437

   So the modal published answer was an empty list, indistinguishable from "no
   large plants in this market". Not zero — unmeasured.

2. HARD 500, EVERY MARKET. The substations query selected `lon`. The live
   `substations` table has no `lon` column; it is `lng`. Measured directly
   against the Railway origin, bypassing the CF worker:

       GET /api/land-power/market-profile/Houston      -> HTTP 500
       {"error": "column \\"lon\\" does not exist\\nLINE 2: ... lat, lon ..."}

   Identical for Austin and San Antonio. Through dchub.cloud the worker turns
   that into a 503 "Backend unreachable", which is why this read as an
   infrastructure blip rather than a query bug. Note the ORDER: the plants query
   runs first and succeeded, which is how we know `power_plants` really does
   carry `lon` while `substations` does not. Fixing fault 1 alone would have
   changed nothing a caller could see — the response was never built.

3. STATE ROLL-UP UNDER A MARKET NAME — DISCLOSED, NOT FIXED. Both queries filter
   `WHERE state = %s`, so all four Texas markets return byte-identical lists.
   Live collisions: OR 4 (Hillsboro, Portland, Prineville, The Dalles), CA 4,
   TX 4, WA 3, NC/NE/NV/MO/IA/FL 2 each — 42 markets over 25 states.
   This is NOT re-derived at metro scope, because there is nothing to re-derive
   FROM: market_power_profiles carries no geography but `state` (20 columns; no
   centroid, no county, no CBSA), and the database has no market->metro mapping
   (dcpi_markets is 31 CITY rows on a different key). So the scope is now stated
   in the response and the colliding markets are named. That is the same call
   the plural /market-profiles endpoint made in #1923, one step further: it
   names the specific siblings rather than warning generically.

LIVE SCHEMA (information_schema, 2026-07-29) — authoritative over the repo DDL:
    power_plants       19 cols   lat ✓  lon ✓  lng ✗   66 rows
    power_plants_eia   35 cols   lat ✓  lon ✗  lng ✓   13,446 rows   NO `status`
    substations        36 cols   lat ✓  lon ✗  lng ✓   126,841 rows
The stub cursor below reproduces exactly that lon/lng split. That is what makes
this guard non-vacuous: unpatched, the substations query raises here for the same
reason it raises in production.

★ `power_plants_eia` has no `status` column, so "operating" is an upstream
  property we cannot assert per row, and the response says so. The old 66-row
  source DID carry `status` (live values: OP 61, SB 4, OS 1) and never filtered
  on it, so no assertion is lost — it was never made.
★ nameplate_capacity_mw (real) is used, matching the canonical
  /api/v1/power-plants query. The live table also carries a `capacity_mw`
  (numeric) twin absent from the repo DDL; measured, 0 of 13,446 rows disagree
  by more than 0.5 MW, so the choice is about consistency, not about numbers.

THE CONTRACT
────────────
  M1. No query in this endpoint reads the bare `power_plants` stub.
  M2. The plant list comes from power_plants_eia and is populated for a state
      that has plants — the repair must not merely stop being wrong.
  M3. The endpoint returns a payload, not a 500. No `lon` against a table whose
      live column is `lng`.
  M4. The response discloses that its scope is the STATE, naming the state.
  M5. The sibling markets that return an identical payload are named.
  M6. Operating status is explicitly NOT asserted, and the plant basis names
      power_plants_eia.
  M7. The sibling-markets lookup is best-effort: if it fails, the endpoint still
      answers. It must not become fault 2 in a new spelling.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
Measured by extracting origin/main @ d85b287d with `git archive`, dropping this
file into that tree, and running it there.

UNPATCHED (origin/main @ d85b287d):   7 failed, 3 passed, 1 xfailed
    The 3 that pass unpatched pass in BOTH states and exist to prove the harness
    works rather than to pin the patch:
        test_extraction_parsed_and_free_vars_resolve
        test_harness_reproduces_the_live_500          (positive control)
        test_harness_reproduces_the_empty_plant_list  (positive control)
    The other 7 are this change's actual contribution. The unpatched failure
    text is the production 500 verbatim:
        column "lon" does not exist
        LINE 2: ...  name, voltage_kv, max_voltage_kv, lat, lon
PATCHED (this branch):                0 failed, 10 passed, 1 xfailed

`1 xfailed` appears on BOTH runs — the must-fail control is collected in each.
That is the point: a conftest-level abort or a collection error yields rc 0 with
0 tests, which renders as an ordinary green. If the control is missing from the
summary, the run did not happen, whatever the exit code says.

★ WHY TWO POSITIVE CONTROLS. Asserting an absence ("no query reads
  power_plants") is only meaningful once the presence is proven reachable. The
  unpatched endpoint dies at the SUBSTATIONS query, i.e. AFTER the plants query
  — so a stub too weak to reach either would satisfy M1 vacuously against buggy
  code. test_harness_reproduces_the_empty_plant_list pins that the stub really
  does make the old plants query run and return [] for Texas, and
  test_harness_reproduces_the_live_500 pins the UndefinedColumn. If either
  control ever fails, this whole file has gone vacuous — fix the harness, do not
  delete the control.

MUST-FAIL CONTROL
─────────────────
test_zzz_must_fail_control is xfail(strict=True) asserting a falsehood. pytest
reports it as `xfailed`. Never delete it.

Run:  python3 -m pytest tests/test_market_profile_detail_plant_source.py -v
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAND = os.path.join(ROOT, "land_power_crawler.py")

OUTER = "register_land_power_routes"
INNER = "market_profile_detail"

# Live, measured 2026-07-29. Keep these two apart: the whole bug is that they
# are the same population at two different loadings.
STUB_ROWS = 66
EIA_ROWS = 13446
TX_EIA_OVER_100 = 437


# ── extraction ────────────────────────────────────────────────────────────────
def _extract():
    """AST-extract the nested route function, decorators stripped.

    House rule: assert the parse produced a Module with a non-empty body, and
    that the extracted function's own body is non-empty. An empty parse
    satisfies every downstream assertion silently, so it must be caught here and
    never by an isinstance filter that quietly matches nothing.
    """
    src = open(LAND).read()
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module), "parse did not produce a Module"
    assert tree.body, "parsed module body is EMPTY — extraction read nothing"

    outer = next((n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == OUTER), None)
    assert outer is not None, f"{OUTER} not found in {LAND}"
    assert outer.body, f"{OUTER} parsed with an EMPTY body"

    fn = next((n for n in ast.walk(outer)
               if isinstance(n, ast.FunctionDef) and n.name == INNER), None)
    assert fn is not None, f"{INNER} not found inside {OUTER}"
    assert fn.body, f"{INNER} parsed with an EMPTY body"
    fn.decorator_list = []
    return fn, outer, tree


def _free_vars(fn):
    """Names the extracted function loads without binding them itself."""
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
            # lambda params bind too — without this a `key=lambda x: ...` reports
            # `x` as an unresolved free variable (hit for real in the sibling
            # tests/test_geothermal_nearby_plants_source.py).
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


def _sql_strings(fn):
    """Every string literal in the function that looks like SQL."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if re.search(r"(?i)\bselect\b.*\bfrom\b", n.value, re.S):
                out.append(" ".join(n.value.split()))
    return out


def _tables_read(fn):
    tables = set()
    for sql in _sql_strings(fn):
        for m in re.finditer(r"(?i)\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)", sql):
            tables.add(m.group(1).lower())
    return tables


# ── stub DB — reproduces the live lon/lng split exactly ───────────────────────
# cols are the column names each table really has. A SELECT naming anything else
# raises, the way psycopg2 raises UndefinedColumn against the live database.
SPEC = {
    "power_plants": {
        "cols": {"id", "eia_plant_id", "name", "operator", "state", "county",
                 "city", "lat", "lon", "capacity_mw", "fuel_type",
                 "fuel_category", "prime_mover", "status", "operating_year",
                 "sector", "source", "last_updated", "created_at"},
        # 66 rows over 13 states; ZERO Texas rows clear capacity_mw > 100.
        "rows_for": {},
    },
    "power_plants_eia": {
        "cols": {"id", "plant_id", "name", "utility_name", "sector", "street",
                 "city", "county", "state", "zipcode", "primary_fuel",
                 "fuel_sources", "technology", "nameplate_capacity_mw",
                 "max_output_mw", "geothermal_mw", "natural_gas_mw",
                 "nuclear_mw", "solar_mw", "wind_mw", "coal_mw",
                 "reporting_period", "lat", "lng", "source", "plant_name",
                 "capacity_mw", "energy_source"},
        # Real Texas plants, live values.
        "rows_for": {
            "TX": [
                ("W A Parish", "NRG Texas Power LLC", 3632.0, "coal",
                 29.4828, -95.6311),
                ("South Texas Project", "STP Nuclear Operating Co", 2580.0,
                 "nuclear", 28.795, -96.0481),
                ("Martin Lake", "Luminant Generation Company LLC", 2410.0,
                 "coal", 32.2606, -94.5706),
            ],
        },
    },
    "substations": {
        "cols": {"id", "name", "operator", "substation_type", "voltage_kv",
                 "capacity_mva", "lat", "lng", "city", "state", "country",
                 "status", "source", "max_voltage_kv", "min_voltage_kv",
                 "county", "owner", "lines_count"},
        "rows_for": {
            "TX": [
                ("HARTBURG", 500.0, 0.0, 30.266668, -93.73866),
                ("CYPRESS", 500.0, 0.0, 30.303635, -94.2572),
            ],
        },
    },
    # 20 columns, market at index 1 and state at index 2 — live ordinal order.
    "market_power_profiles": {
        "cols": {"id", "market", "state", "substation_count", "avg_voltage_kv",
                 "max_voltage_kv", "transmission_line_count",
                 "total_transmission_miles", "gas_pipeline_count",
                 "power_plant_count", "total_generation_mw", "solar_mw",
                 "wind_mw", "natural_gas_mw", "nuclear_mw", "coal_mw",
                 "battery_storage_mw", "renewable_pct",
                 "power_readiness_score", "last_updated"},
        "rows_for": {},
    },
}

# The four Texas markets that share one state roll-up, live.
TX_MARKETS = ["Austin", "Dallas-Fort Worth", "Houston", "San Antonio"]


class _Cur:
    def __init__(self, log, fail_siblings=False):
        self.log = log
        self.fail_siblings = fail_siblings
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append(s)
        low = s.lower()
        params = params or ()

        m = re.search(r"(?i)\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)", s)
        if not m:
            raise RuntimeError(f"stub cursor could not find a table in: {s}")
        tbl = m.group(1).lower()
        if tbl not in SPEC:
            raise RuntimeError(f'relation "{tbl}" does not exist')
        entry = SPEC[tbl]

        # Column validation, exactly as the live database does it.
        sel = s[s.lower().index("select") + 6:s.lower().index(" from ")]
        if sel.strip() != "*":
            for tok in re.split(r",\s*", sel):
                name = tok.strip().split()[-1].strip()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
                    if name not in entry["cols"]:
                        raise RuntimeError(
                            f'column "{name}" does not exist\n'
                            f'LINE 2: ...  {sel.strip()}')

        if tbl == "market_power_profiles":
            if sel.strip() == "*":
                mk = params[0] if params else "Houston"
                if mk not in TX_MARKETS:
                    self._result = []
                    return
                row = [None] * 20
                row[1], row[2] = mk, "TX"
                self._result = [tuple(row)]
                return
            # the sibling-markets lookup
            if self.fail_siblings:
                raise RuntimeError("simulated failure on the siblings lookup")
            self_market = params[1] if len(params) > 1 else None
            self._result = [(m2,) for m2 in TX_MARKETS if m2 != self_market]
            return

        state = params[0] if params else None
        self._result = list(entry["rows_for"].get(state, []))

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _run(market="Houston", fail_siblings=False):
    """Execute the extracted route against the stub. Returns (result, sql_log)."""
    fn, _, _ = _extract()
    log = []
    conn = _Conn(_Cur(log, fail_siblings=fail_siblings))
    ns = {
        "get_db": lambda: conn,
        # jsonify is identity here so the test reads the payload the endpoint
        # built, not a Flask Response it would have to unwrap.
        "jsonify": lambda payload: payload,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), LAND, "exec"), ns)
    return ns[INNER](market), log


def _payload(result):
    """The endpoint returns a dict on success, or (dict, status) on error."""
    if isinstance(result, tuple):
        pytest.fail(f"endpoint returned HTTP {result[1]}: {result[0]}")
    assert isinstance(result, dict), f"endpoint returned {type(result)}"
    return result


# ── extraction sanity (passes in both states) ─────────────────────────────────
def test_extraction_parsed_and_free_vars_resolve():
    fn, outer, tree = _extract()
    assert len(tree.body) > 1, "module parsed to a single node — that is not this file"
    assert len(outer.body) > 1, f"{OUTER} parsed to a single node"
    # Every free variable must be one this harness supplies. An unresolved name
    # is either a NameError at exec time or, worse, a branch that silently never
    # runs — which is how a guard goes green without testing anything.
    supplied = {"get_db", "jsonify"}     # exactly what _run() puts in `ns`
    unresolved = set(_free_vars(fn)) - supplied
    assert not unresolved, f"unresolved free vars in {INNER}: {sorted(unresolved)}"


# ── positive controls: prove the harness can reproduce BOTH original faults ───
def test_harness_reproduces_the_live_500():
    """Unpatched, the substations query must raise UndefinedColumn on `lon`.

    Patched, it must not. Either way this test passes — it asserts the harness
    faithfully models the live lon/lng split, not which state the tree is in.
    """
    assert "lon" not in SPEC["substations"]["cols"], \
        "stub substations has a `lon` column — live does not; harness is wrong"
    assert "lng" in SPEC["substations"]["cols"]
    assert "lon" in SPEC["power_plants"]["cols"], \
        "stub power_plants lacks `lon` — live has it; harness is wrong"
    assert "lng" not in SPEC["power_plants"]["cols"]
    assert "lng" in SPEC["power_plants_eia"]["cols"]
    assert "lon" not in SPEC["power_plants_eia"]["cols"]

    fn, _, _ = _extract()
    sqls = " || ".join(_sql_strings(fn))
    reads_stub = bool(re.search(r"(?i)\bfrom\s+power_plants\b(?!_)", sqls))
    result, log = _run()
    if reads_stub:
        # unpatched tree: this MUST be the 500 production returns
        assert isinstance(result, tuple) and result[1] == 500, (
            "unpatched code did not 500 — the stub is not reproducing the live "
            "`column \"lon\" does not exist`, so M1/M3 below would pass "
            "vacuously")
        assert "lon" in str(result[0]), f"500 for the wrong reason: {result[0]}"
    else:
        assert not isinstance(result, tuple), \
            f"patched code still errors: {result}"


def test_harness_reproduces_the_empty_plant_list():
    """The old plants query must actually RUN and return [] for Texas.

    The unpatched endpoint dies at the substations query, which comes AFTER the
    plants query. If the stub could not reach the plants query, M1 ("no query
    reads power_plants") would be trivially true of code that reads it.
    """
    _, log = _run()
    plant_sql = [s for s in log
                 if re.search(r"(?i)\bfrom\s+power_plants\b(?!_)", s)]
    eia_sql = [s for s in log if "power_plants_eia" in s.lower()]
    assert plant_sql or eia_sql, (
        "no plant query reached the stub at all — the harness never exercises "
        "the line this file exists to pin")
    if plant_sql:
        # unpatched: prove the query ran and yielded nothing for TX, which is
        # the published-empty-list fault, measured.
        assert not SPEC["power_plants"]["rows_for"].get("TX"), (
            "stub gives Texas rows in the 66-row table — live gives zero over "
            "100 MW; harness is wrong")
        assert "capacity_mw > 100" in plant_sql[0].lower().replace("  ", " ")


# ── M1 ────────────────────────────────────────────────────────────────────────
def test_no_query_reads_the_66_row_stub():
    fn, _, _ = _extract()
    tables = _tables_read(fn)
    assert "power_plants" not in tables, (
        f"still reads the bare `power_plants` table ({STUB_ROWS} rows for the "
        f"whole US; 34 of 42 markets got an empty list from it). "
        f"Tables read: {sorted(tables)}")
    _, log = _run()
    offenders = [s for s in log
                 if re.search(r"(?i)\bfrom\s+power_plants\b(?!_)", s)]
    assert not offenders, f"executed a query against the stub: {offenders}"


# ── M2 ────────────────────────────────────────────────────────────────────────
def test_plant_list_comes_from_the_eia_table_and_is_populated():
    fn, _, _ = _extract()
    assert "power_plants_eia" in _tables_read(fn), (
        f"plant list is not sourced from power_plants_eia ({EIA_ROWS} rows)")
    payload = _payload(_run()[0])
    plants = payload.get("large_power_plants")
    assert isinstance(plants, list), f"large_power_plants is {type(plants)}"
    assert plants, (
        f"Texas returned an EMPTY plant list. power_plants_eia holds "
        f"{TX_EIA_OVER_100} Texas plants over 100 MW — the repair has to stop "
        f"being wrong AND start being right")
    p = plants[0]
    for key in ("name", "operator", "mw", "fuel", "lat", "lon"):
        assert key in p, f"published plant dropped the `{key}` field: {p}"
    assert p["mw"] and p["mw"] > 100, f"a sub-100 MW plant leaked in: {p}"
    assert p["lon"] is not None and p["lon"] < 0, (
        f"longitude did not survive the lng->lon rename: {p}")


# ── M3 ────────────────────────────────────────────────────────────────────────
def test_endpoint_answers_instead_of_500ing():
    result, _ = _run()
    payload = _payload(result)
    assert payload.get("market") == "Houston"
    subs = payload.get("high_voltage_substations")
    assert isinstance(subs, list) and subs, (
        "substations list is empty — the query that used to 500 on `lon` still "
        "is not returning rows")
    assert subs[0].get("lon") is not None, \
        f"substation longitude lost in the lng->lon rename: {subs[0]}"
    fn, _, _ = _extract()
    for sql in _sql_strings(fn):
        if re.search(r"(?i)\bfrom\s+(substations|power_plants_eia)\b", sql):
            assert not re.search(r"(?i)(^|[\s,])lon([\s,]|$)", sql), (
                f"selects `lon` from a table whose live column is `lng`: {sql}")


# ── M4 ────────────────────────────────────────────────────────────────────────
def test_state_scope_is_disclosed_not_implied():
    payload = _payload(_run()[0])
    basis = payload.get("basis") or {}
    scope = (basis.get("geographic_scope") or "")
    assert scope.strip(), (
        "no geographic_scope published — a STATE roll-up is still labelled as a "
        "market with nothing telling the caller so")
    assert "STATE" in scope.upper(), f"scope does not say it is state-level: {scope}"
    assert "TX" in scope, f"scope does not name the state it rolled up: {scope}"
    assert payload.get("state") == "TX", \
        "the state behind the figures is not published as a field"


# ── M5 ────────────────────────────────────────────────────────────────────────
def test_the_markets_sharing_this_rollup_are_named():
    payload = _payload(_run()[0])
    same = (payload.get("basis") or {}).get("identical_for_markets")
    assert isinstance(same, list), (
        f"identical_for_markets is {type(same)} — the caller cannot tell which "
        f"other markets return this same payload")
    assert set(same) == {"Austin", "Dallas-Fort Worth", "San Antonio"}, (
        f"wrong sibling set for Houston: {same}")


# ── M6 ────────────────────────────────────────────────────────────────────────
def test_operating_status_is_not_asserted_and_basis_names_the_table():
    payload = _payload(_run()[0])
    basis = payload.get("basis") or {}
    plants_basis = basis.get("large_power_plants") or ""
    assert "power_plants_eia" in plants_basis, \
        f"plant basis does not name its table: {plants_basis!r}"
    status = basis.get("operating_status") or ""
    assert status.strip(), (
        "no operating_status note. power_plants_eia has no `status` column, so "
        "'operating' is an upstream property this endpoint cannot assert")
    assert "status" in status.lower() and "not" in status.lower(), \
        f"operating_status does not disclaim the assertion: {status!r}"
    assert "status" not in ", ".join(SPEC["power_plants_eia"]["cols"]).split(", "), \
        "stub gave power_plants_eia a `status` column — live has none"


# ── M7 ────────────────────────────────────────────────────────────────────────
def test_a_failing_siblings_lookup_cannot_take_the_endpoint_down():
    result, _ = _run(fail_siblings=True)
    payload = _payload(result)
    assert payload.get("large_power_plants"), \
        "a failed siblings lookup emptied the plant list"
    assert payload.get("high_voltage_substations"), \
        "a failed siblings lookup emptied the substation list"
    same = (payload.get("basis") or {}).get("identical_for_markets")
    assert same is None, (
        f"a FAILED siblings lookup published {same!r} — an empty list would read "
        f"as 'no other market shares this roll-up', which is the exact "
        f"unmeasured-as-zero confusion this change exists to remove")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
