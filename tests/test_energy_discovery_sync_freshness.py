"""Guard: a freshness timestamp must describe the table its figure came from.

WHAT THIS PINS
──────────────
GET /api/energy-discovery/status published, MEASURED LIVE 2026-07-29 before the
fix:

    total_power_plants          = 13446                        <- power_plants_eia
    recent_syncs[power_plants]  = "2026-03-30 07:30:25.382534"  <- power_plants

Those describe DIFFERENT TABLES. The count is the real US EIA-860 fleet
(power_plants_eia, 13,446 rows). The timestamp was MAX(created_at) over the bare
`power_plants` table — 66 rows for the entire United States, the same EIA
population loaded to ~0.5% because the crawler's dedup step keyed on
rec['plantid'], a spelling the EIA v2 facility-fuel response does not return, so
54,934 of 55,000 records were dropped while the run reported errors=0
(land_power_crawler.py crawl_power_plants; the drop counter landed in #1923).

A reader therefore saw a fresh, correct 13,446 welded to a four-month-old date
and concluded the EIA fleet was four months stale. That is a WRONG answer to
"how current is this?", not a missing one — which is why the repair removes the
member rather than annotating it.

THE CONTRACT
────────────
  F1. No freshness member is sourced from the bare `power_plants` stub, and the
      endpoint issues no query against it at all.
  F2. A table with no timestamp column reports at=None WITH a reason. It never
      borrows another table's date, and it is never silently omitted — an
      omission leaves the caller unable to tell "not tracked" from "never
      synced". (This is deliberately stricter than the sibling
      /api/v1/energy/discovery/status in main.py, which drops such a table
      silently.)
  F3. A table that DOES carry a timestamp still reports it, with the column
      named — the fix must not blind the endpoint.
  F4. An absent table is unmeasured + a reason, never dropped and never 0.
  F5. One failing table cannot empty the whole list. The old code bound three
      tables into a single UNION ALL, so a single missing table or renamed
      column failed the entire statement and left recent_syncs as the seed `[]`
      — an empty list published as though it meant "no syncs have happened".
  F6. Every published plant figure carries a basis naming its table and scope.

★ NOTE ON SCHEMA DEPENDENCE: this repair does NOT hardcode "power_plants_eia has
  no timestamp column". It probes information_schema at request time and adapts,
  so it stays correct whether or not that table later gains one. F2 and F3 pin
  both branches of that probe. (The repo DDL at scripts/load_power_plants.py:38
  carries no timestamp column, and routes/energy_discovery_routes.py already
  passed ts=None there with a comment recording that created_at and inserted_at
  both failed against the live table in Railway logs — but per the house
  LIVE≠repo-DDL rule that is corroboration, not proof, so the code does not rely
  on it.)

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ beadfa48, extracted with `git archive origin/main`):
    8 failed, 4 passed, 1 xfailed
    The 4 that pass unpatched must pass in BOTH states, and are there to prove
    the harness works rather than to pin the patch:
        test_extraction_parsed_and_free_vars_resolve
        test_endpoint_runs_against_the_stub_at_all
        test_harness_can_reproduce_the_original_bug   (positive control)
        test_a_table_that_has_a_timestamp_still_reports_it   (F3)
    The other 8 are this change's actual contribution.
PATCHED (this branch):
    12 passed, 1 xfailed, 0 failed
`1 xfailed` appears on BOTH runs -- the must-fail control is collected in each.

★ FIRST DRAFT WAS A FALSE GREEN, recorded because it is the reusable lesson: the
  two headline F1 tests PASSED against unpatched main, because the stub cursor
  did not understand UNION ALL. The old query raised, the endpoint's `except`
  swallowed it, recent_syncs stayed the seed `[]`, and "the stub table is not in
  recent_syncs" was trivially true of an empty list. The assertions were right
  and the harness was too weak to falsify them. Fixed by teaching the stub
  UNION ALL and adding test_harness_can_reproduce_the_original_bug, which fails
  if the stub ever again cannot emit the buggy row. Asserting an absence is only
  meaningful once you have proven the presence was reachable.

MUST-FAIL CONTROL
─────────────────
test_zzz_must_fail_control is xfail(strict=True) asserting a falsehood. pytest
reports it as `xfailed`. If a conftest or collection accident ever makes this
file silently not run, the control vanishes from the summary, exposing a fake
"0 failed" instead of letting it read as a pass. Never delete it.

Run:  python3 -m pytest tests/test_energy_discovery_sync_freshness.py -v
"""
import ast
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENERGY = os.path.join(ROOT, "routes", "energy_discovery_routes.py")

# The stale date the endpoint used to publish beside a fresh 13,446.
STALE_STUB_DATE = "2026-03-30 07:30:25.382534"


# ── extraction ────────────────────────────────────────────────────────────────
def _extract(fn_name):
    """AST-extract one top-level function, decorators stripped.

    House rule: assert the parse produced a Module with a non-empty body, and
    that the function's own body is non-empty — an empty parse would otherwise
    satisfy every downstream assertion silently.
    """
    src = open(ENERGY).read()
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module), "parse did not produce a Module"
    assert tree.body, "parsed module body is EMPTY — extraction read nothing"
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    assert fn is not None, f"{fn_name} not found in {ENERGY}"
    assert fn.body, f"{fn_name} parsed with an EMPTY body"
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
        elif isinstance(n, ast.FunctionDef) and n is not fn:
            assigned.add(n.name)
            assigned.update(a.arg for a in n.args.args)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
    import builtins
    return sorted(loaded - assigned - set(dir(builtins)))


# ── stub DB ───────────────────────────────────────────────────────────────────
class _Cur:
    """Minimal psycopg2-ish cursor driven by a table->schema/rowcount spec."""

    def __init__(self, spec, log, fail_max_for=()):
        self.spec = spec          # {table: {"cols": {...}, "rows": n, "max": {col: val}}}
        self.log = log            # every SQL string executed
        self.fail_max_for = set(fail_max_for)
        self._result = []

    def execute(self, sql, params=None):
        self.log.append(sql)
        s = " ".join(sql.split())
        low = s.lower()

        if "to_regclass" in low:
            tbl = (params[0] if params else "").split(".")[-1]
            self._result = [(tbl if tbl in self.spec else None,)]
            return

        if "information_schema.columns" in low:
            tbl = params[0] if params else None
            cols = self.spec.get(tbl, {}).get("cols", set())
            self._result = [(c,) for c in sorted(cols)]
            return

        # The UNPATCHED endpoint asks for all three freshness rows in ONE
        # UNION ALL statement. The stub must emulate that faithfully, or the
        # old query raises here, the endpoint's except swallows it, and
        # recent_syncs stays the seed `[]` -- which would make the F1
        # assertions below pass VACUOUSLY against the buggy code. A guard that
        # cannot make the old code produce its bug is not a guard.
        if " union all " in low:
            rows = []
            for branch in re.split(r"(?i)\bunion\s+all\b", s):
                lit = re.search(r"'([^']+)'", branch)
                mx = re.search(r"(?i)MAX\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", branch)
                tb = self._table_of(branch)
                if not (lit and mx and tb):
                    raise RuntimeError(f"unparsed UNION branch: {branch}")
                if tb not in self.spec:
                    raise RuntimeError(f'relation "{tb}" does not exist')
                col = mx.group(1)
                if col not in self.spec[tb].get("cols", set()):
                    raise RuntimeError(f'column "{col}" does not exist')
                rows.append((lit.group(1), self.spec[tb].get("max", {}).get(col)))
            self._result = rows
            return

        tbl = self._table_of(s)
        if tbl is None or tbl not in self.spec:
            raise RuntimeError(f"relation does not exist / unparsed: {s}")
        entry = self.spec[tbl]

        if low.startswith("select max("):
            col = s[len("SELECT MAX("):s.index(")")]
            if tbl in self.fail_max_for:
                raise RuntimeError(f"simulated failure on MAX({col}) FROM {tbl}")
            if col not in entry.get("cols", set()):
                raise RuntimeError(f'column "{col}" does not exist')
            self._result = [(entry.get("max", {}).get(col),)]
            return

        if "count(*)" in low and "max(" in low:
            col = s[s.index("MAX(") + 4:s.index(")", s.index("MAX("))]
            if col not in entry.get("cols", set()):
                raise RuntimeError(f'column "{col}" does not exist')
            self._result = [(entry.get("rows", 0), entry.get("max", {}).get(col))]
            return

        if "coalesce(sum(" in low:
            self._result = [(entry.get("capacity", 0),)]
            return

        if "count(*)" in low and "ilike" in low:
            self._result = [(entry.get("wind", 0),)]
            return

        if "count(*)" in low:
            self._result = [(entry.get("rows", 0),)]
            return

        raise RuntimeError(f"stub cursor got unexpected SQL: {s}")

    @staticmethod
    def _table_of(s):
        toks = s.replace("(", " ").replace(")", " ").split()
        for i, t in enumerate(toks):
            if t.lower() == "from" and i + 1 < len(toks):
                return toks[i + 1].strip(",")
        return None

    def fetchone(self):
        return self._result[0] if self._result else None

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


# A spec close to live: the 66-row stub EXISTS and has a real (stale)
# created_at, which is exactly what made the old bug possible.
def _live_like_spec():
    return {
        "substations": {
            "cols": {"id", "name", "updated_at", "created_at"},
            "rows": 127000,
            "max": {"updated_at": "2026-07-29 16:06:11.778193"},
        },
        "fiber_routes": {
            "cols": {"id", "updated_at"},
            "rows": 55000,
            "max": {"updated_at": "2026-07-30 02:30:01.933935"},
        },
        # The dead stub: present, populated, timestamped, and WRONG.
        "power_plants": {
            "cols": {"id", "capacity_mw", "created_at", "last_updated"},
            "rows": 66,
            "capacity": 9609,
            "max": {"created_at": STALE_STUB_DATE},
        },
        # The real fleet: no timestamp column at all.
        "power_plants_eia": {
            "cols": {"id", "plant_id", "nameplate_capacity_mw", "primary_fuel"},
            "rows": 13446,
            "capacity": 1241974,
            "wind": 1352,
        },
        "gas_pipelines": {"cols": {"id", "updated_at"}, "rows": 30918,
                          "max": {"updated_at": "2026-07-28 00:00:00"}},
        "transmission_lines": {"cols": {"id", "created_at"}, "rows": 94626,
                               "max": {"created_at": "2026-07-27 00:00:00"}},
        "gas_compressor_stations": {"cols": {"id", "loaded_at"}, "rows": 1700,
                                    "max": {"loaded_at": "2026-06-01 00:00:00"}},
        "gas_processing_plants": {"cols": {"id", "loaded_at"}, "rows": 478,
                                  "max": {"loaded_at": "2026-06-01 00:00:00"}},
    }


def _run(spec=None, fail_max_for=()):
    """Execute the extracted endpoint against the stub. Returns (payload, sql_log)."""
    fn, _ = _extract("energy_discovery_status")
    spec = _live_like_spec() if spec is None else spec
    log = []
    conn = _Conn(_Cur(spec, log, fail_max_for=fail_max_for))

    fake_db_utils = types.ModuleType("db_utils")
    fake_db_utils.try_get_db = lambda: conn
    saved = sys.modules.get("db_utils")
    sys.modules["db_utils"] = fake_db_utils
    try:
        import time as _time
        ns = {
            "jsonify": lambda payload: payload,
            "time": _time,
            # force a cache miss so the body actually runs
            "_STATUS_CACHE": {"data": None, "ts": 0},
            "_STATUS_TTL_S": 0,
        }
        exec(compile(ast.Module(body=[fn], type_ignores=[]), ENERGY, "exec"), ns)
        payload = ns["energy_discovery_status"]()
    finally:
        if saved is None:
            sys.modules.pop("db_utils", None)
        else:
            sys.modules["db_utils"] = saved
    assert isinstance(payload, dict), f"endpoint returned {type(payload)}"
    assert not payload.get("data", {}).get("_error"), \
        f"endpoint errored: {payload.get('data', {}).get('_error')}"
    return payload, log


def _syncs(payload):
    return payload["data"]["recent_syncs"]


# ── extraction sanity (passes in both states) ─────────────────────────────────
def test_extraction_parsed_and_free_vars_resolve():
    fn, tree = _extract("energy_discovery_status")
    assert len(tree.body) > 1
    # Every free variable must be one this harness supplies, or the test would
    # NameError at exec time -- or worse, silently not exercise the code.
    supplied = {"jsonify", "time", "_STATUS_CACHE", "_STATUS_TTL_S",
                "energy_discovery_bp"}
    unresolved = set(_free_vars(fn)) - supplied
    assert not unresolved, f"unresolved free vars: {sorted(unresolved)}"


def test_endpoint_runs_against_the_stub_at_all():
    payload, log = _run()
    assert payload["data"]["total_substations"] == 127000
    assert log, "no SQL was issued -- the stub never exercised the endpoint"


def test_harness_can_reproduce_the_original_bug():
    """Positive control on the STUB, not on the endpoint.

    The first draft of this file passed its headline F1 assertions against
    unpatched main for the wrong reason: the stub did not understand UNION ALL,
    so the old query raised, the endpoint's `except` swallowed it, and
    recent_syncs stayed the seed `[]` -- vacuously free of the stub table. This
    pins the stub's ability to emit the buggy row, so that failure mode cannot
    come back silently.
    """
    log = []
    cur = _Cur(_live_like_spec(), log)
    cur.execute(
        "SELECT 'substations' AS source, MAX(updated_at) AS at FROM substations "
        "UNION ALL SELECT 'fiber_routes', MAX(updated_at) FROM fiber_routes "
        "UNION ALL SELECT 'power_plants', MAX(created_at) FROM power_plants")
    rows = cur.fetchall()
    assert ("power_plants", STALE_STUB_DATE) in rows, (
        "the stub cannot reproduce the original defect, so any test asserting "
        f"its absence proves nothing; got {rows}")


# ── F1 ────────────────────────────────────────────────────────────────────────
def test_no_freshness_member_is_sourced_from_the_66_row_stub():
    payload, _ = _run()
    sources = [e.get("source") for e in _syncs(payload)]
    assert "power_plants" not in sources, (
        "recent_syncs still publishes the bare `power_plants` stub as a source; "
        f"got {sources}")


def test_the_stale_stub_date_is_published_nowhere():
    payload, _ = _run()
    blob = repr(payload)
    assert STALE_STUB_DATE not in blob, (
        f"the 66-row stub's {STALE_STUB_DATE} is still reaching the payload -- "
        "a stale date welded to a fresh figure")


def test_endpoint_issues_no_query_against_the_bare_stub_table():
    _, log = _run()
    offenders = [s for s in log
                 if "power_plants" in s
                 and "power_plants_eia" not in s
                 and "information_schema" not in s]
    assert not offenders, (
        "the endpoint still queries the dead `power_plants` table: "
        f"{offenders}")


# ── F2 ────────────────────────────────────────────────────────────────────────
def test_table_without_timestamp_reports_none_plus_a_reason():
    payload, _ = _run()
    entry = next((e for e in _syncs(payload)
                  if e.get("source") == "power_plants_eia"), None)
    assert entry is not None, (
        "power_plants_eia is silently OMITTED from recent_syncs -- the caller "
        "cannot tell 'not tracked' from 'never synced'")
    assert entry.get("at") is None, (
        "power_plants_eia has no timestamp column in this spec, so it must not "
        f"report a date; got {entry.get('at')!r}")
    reason = entry.get("unmeasured") or ""
    assert reason.strip(), "at=None was published with no reason"
    assert "0" != str(entry.get("at")), "unmeasured must never be 0"


# ── F3 (passes in both states: the fix must not blind the endpoint) ───────────
def test_a_table_that_has_a_timestamp_still_reports_it():
    payload, _ = _run()
    subs = next(e for e in _syncs(payload) if e.get("source") == "substations")
    assert subs.get("at") == "2026-07-29 16:06:11.778193"


def test_a_reported_timestamp_names_the_column_it_came_from():
    payload, _ = _run()
    subs = next(e for e in _syncs(payload) if e.get("source") == "substations")
    assert "updated_at" in (subs.get("basis") or ""), (
        "a published timestamp carries no basis naming its column; got "
        f"{subs!r}")


# ── F4 ────────────────────────────────────────────────────────────────────────
def test_an_absent_table_is_unmeasured_not_dropped():
    spec = _live_like_spec()
    del spec["power_plants_eia"]
    payload, _ = _run(spec=spec)
    entry = next((e for e in _syncs(payload)
                  if e.get("source") == "power_plants_eia"), None)
    assert entry is not None, "an absent table vanished instead of reporting"
    assert entry.get("at") is None
    assert (entry.get("unmeasured") or "").strip(), \
        "absent table reported without a reason"


# ── F5 ────────────────────────────────────────────────────────────────────────
def test_one_failing_table_cannot_empty_the_whole_list():
    payload, _ = _run(fail_max_for=("fiber_routes",))
    syncs = _syncs(payload)
    assert syncs, (
        "one failing table emptied recent_syncs -- the UNION ALL fragility that "
        "published `[]` as 'no syncs have happened'")
    subs = next((e for e in syncs if e.get("source") == "substations"), None)
    assert subs and subs.get("at"), \
        "a healthy table lost its timestamp because a sibling failed"
    fib = next((e for e in syncs if e.get("source") == "fiber_routes"), None)
    assert fib is not None and (fib.get("unmeasured") or "").strip(), \
        "the failing table was dropped silently instead of reporting a reason"


# ── F6 ────────────────────────────────────────────────────────────────────────
def test_every_published_plant_figure_names_its_table_and_scope():
    payload, _ = _run()
    data = payload["data"]
    # unchanged: still the real fleet, not the stub
    assert data["total_power_plants"] == 13446
    basis = data.get("basis") or {}
    for key in ("total_power_plants", "total_capacity_mw",
                "total_wind_projects", "recent_syncs"):
        assert (basis.get(key) or "").strip(), f"no basis published for {key}"
    assert "power_plants_eia" in basis["total_power_plants"], \
        "the count's basis does not name the table it comes from"


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
