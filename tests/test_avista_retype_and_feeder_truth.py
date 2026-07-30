"""Avista retype + feeder-capacity truth (2026-07-30).

What this guards, and why it exists:

  The one `bus_headroom` territory (Avista) was, per its publisher's own AGOL
  metadata ("2026.GenerationInterconnectionHeatMap.R1"), a GENERATION-
  interconnection study — "can I inject", not what a load can draw. Its
  MW_Available field is a pass/fail flag over nine discrete study sizes, and
  the point-only feeder_key + capacity-DESC paging + keep-first dedup stored
  each bus at its MAXIMUM passing size (bus 'Huetter': stored 200 MW while
  failing 2/10 constraints at 20 MW — confirmed against the live table
  2026-07-30). Meanwhile /api/v1/sites/cross-layer declared ALL feeder
  capacity "wrong physical quantity" in a hardcoded string, although 9 of 24
  live territories are capacity_type='load'.

These are BEHAVIOUR tests in the house pattern: functions are pulled out of
the shipped source with `ast` and EXECUTED against stubs. No test imports
main.py, Flask app state, or the database, and nothing runs at module scope.
Every extraction asserts the file parsed to a non-empty Module, the wanted
names were found, and the free variables the functions need actually resolved
— an empty parse must never pass vacuously.

EXPECTED, unpatched (measured against `git archive origin/main`, a974f5d5):
  test_must_fail_control                                   XFAIL (control)
  test_gen_field_guard_covers_bus_headroom                 FAIL
  test_avista_is_retyped_gen_with_the_agol_evidence        FAIL
  test_avista_is_registered_row_not_feeder                 FAIL
  test_avista_constraint_rows_keep_their_own_keys          FAIL
  test_retraction_deletes_the_old_mislabelled_rows         FAIL
  test_retraction_runs_before_the_weekly_gate              FAIL
  test_load_value_never_inherits_a_gen_row                 FAIL (no helper)
  test_mva_unit_rides_with_a_peco_value                    FAIL (no helper)
  test_gen_only_scope_keeps_the_exclusion_reason           FAIL (no helper)
  test_no_rows_is_unknown_never_zero                       FAIL (no helper)
  test_negative_load_allowance_survives                    FAIL (no helper)
  test_headroom_reason_records_the_retype                  FAIL
  → 12 failed, 1 xfailed
EXPECTED, patched: 12 passed, 1 xfailed.

Run:  python3 -m pytest tests/test_avista_retype_and_feeder_truth.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_INGEST = "routes/hosting_capacity_ingest.py"
_XLAYER = "routes/cross_layer_sites.py"


def _root():
    return pathlib.Path(__file__).resolve().parents[1]


def _parse(relpath):
    """→ (tree, source). Asserts the file exists AND parsed to a NON-EMPTY
    Module — the filed AST-guard trap is an empty parse passing everything."""
    p = _root() / relpath
    assert p.exists(), "missing source file: %s" % relpath
    src = p.read_text(encoding="utf-8")
    assert src.strip(), "%s is empty — nothing to guard" % relpath
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module), "ast.parse did not yield a Module"
    assert len(tree.body) > 0, (
        "%s parsed to an EMPTY module — every test would pass vacuously"
        % relpath)
    return tree, src


def _extract(relpath, fn_names, assign_names=(), namespace=None):
    """Compile named top-level functions + assignments into a namespace.

    Asserts every requested name was found, that every function compiled to a
    callable, and that every requested free-variable assignment resolved —
    a missing name is either a NameError at call time or a silently untested
    function (the third repeat of that trap in this repo)."""
    tree, _ = _parse(relpath)
    keep, found = [], set()
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in fn_names:
            assert stmt.body, "%s.%s has an EMPTY body" % (relpath, stmt.name)
            keep.append(stmt)
            found.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in assign_names:
                    keep.append(stmt)
                    found.add(tgt.id)
    missing = (set(fn_names) | set(assign_names)) - found
    assert not missing, "not found in %s: %s" % (relpath, sorted(missing))
    ns = dict(namespace or {})
    mod = ast.Module(body=keep, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), relpath, "exec"), ns)
    for n in fn_names:
        assert callable(ns.get(n)), "%s did not compile to a callable" % n
    for n in assign_names:
        assert n in ns, "free variable %r did not resolve — extract broken" % n
    return ns


def _tolerant_eval(node):
    """literal_eval with a marker for non-literal values (env lookups)."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        pass
    if isinstance(node, ast.Dict):
        return {_tolerant_eval(k): _tolerant_eval(v)
                for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_tolerant_eval(e) for e in node.elts]
    return "<dynamic>"


def _sources():
    tree, _ = _parse(_INGEST)
    node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "SOURCES":
                    node = stmt.value
    assert node is not None, "SOURCES assignment not found"
    sources = _tolerant_eval(node)
    assert isinstance(sources, list) and sources, (
        "SOURCES extracted EMPTY — every assertion would be vacuous")
    return sources


def _contract_ns():
    return _extract(
        _INGEST, ["check_source_contract"],
        assign_names=("_ALLOWED_CAPACITY_TYPES", "_GEN_ONLY_FIELDS",
                      "_ROW_NOT_FEEDER_SOURCES"))


def _avista():
    hits = [s for s in _sources() if s.get("key") == "avista_bus"]
    assert hits, "avista_bus source disappeared from SOURCES"
    return hits[0]


# ── control ────────────────────────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason=(
    "MUST-FAIL control: an undeclared capacity_type is refused by every "
    "shipped version of check_source_contract(), so this assertion must fail "
    "on patched AND unpatched runs. If it ever passes, the harness is broken "
    "and every green result in this file means nothing."))
def test_must_fail_control():
    check = _contract_ns()["check_source_contract"]
    assert check({"key": "control", "capacity_type": "banana",
                  "fields": {}}) is None


# ── contract gap (a): the gen-field guard must cover bus_headroom ─────────
def test_gen_field_guard_covers_bus_headroom():
    """A known GENERATION field mapped into a bus_headroom source must be
    refused exactly as it is for load — both types assert 'the right physical
    quantity', so neither may carry a gen-only capacity field. The old check
    fired only `if ct == "load"`, exempting bus_headroom entirely."""
    check = _contract_ns()["check_source_contract"]
    probe = {"key": "probe_bus", "capacity_type": "bus_headroom",
             "fields": {"mw_max": ("ica_overall_pv", 1.0), "mw_min": None}}
    assert check(probe), (
        "a bus_headroom source mapping gen-only field 'ica_overall_pv' into "
        "mw_max was ACCEPTED — the gen-field guard does not cover "
        "bus_headroom, the exact gap that shipped a generation study as "
        "transmission headroom")
    # regression control: the load branch keeps its guard
    assert check(dict(probe, key="probe_load", capacity_type="load")), (
        "the load branch of the gen-field guard regressed")


# ── the Avista entry itself ────────────────────────────────────────────────
def test_avista_is_retyped_gen_with_the_agol_evidence():
    s = _avista()
    assert s.get("capacity_type") == "gen", (
        "avista_bus is typed %r — it must be 'gen': the publisher's own AGOL "
        "metadata labels the layer a Generation Interconnection study"
        % s.get("capacity_type"))
    assert "bus headroom" not in (s.get("utility") or "").lower(), (
        "the utility label still claims bus headroom: %r" % s.get("utility"))
    basis = s.get("capacity_basis") or ""
    assert "GenerationInterconnectionHeatMap" in basis, (
        "capacity_basis must name the publisher's own evidence "
        "(GenerationInterconnectionHeatMap); got %r" % basis[:120])
    assert "34c5773cf6dc44a798b300d0ebab0ecb" in basis, (
        "capacity_basis must cite the AGOL item id so the retype is auditable")


def test_avista_is_registered_row_not_feeder():
    """19,764 source rows → 197 buses (100.3x, measured). The registry that
    exists for 15x Ameren and 781x NV Energy must cover the 100x case, and
    the declared knob must be one the entry actually carries."""
    ns = _contract_ns()
    reg = ns["_ROW_NOT_FEEDER_SOURCES"]
    assert "avista_bus" in reg, (
        "avista_bus is absent from _ROW_NOT_FEEDER_SOURCES despite a "
        "measured ~100x row:bus ratio")
    _gran, knob = reg["avista_bus"]
    s = _avista()
    if knob == "feeder_field":
        assert s.get("fields", {}).get("feeder"), "knob demands a feeder field"
    else:
        assert s.get(knob), (
            "avista_bus declares knob %r in the registry but the SOURCES "
            "entry does not carry it — the contract would refuse the source "
            "on every run (permanent silent no-op)" % knob)
    # and the shipped entry passes the real contract with the registration
    assert ns["check_source_contract"](s) is None, (
        "the shipped avista_bus entry fails its own runtime contract: %r"
        % ns["check_source_contract"](s))


def test_avista_constraint_rows_keep_their_own_keys():
    """THE FLATTERING-FOLD FIX, tested as behaviour: two constraint rows on
    the same bus at the same point (one passing 200 MW, one FAILING at
    20 MW) must map to DIFFERENT feeder_keys. With the old point-only key
    they collided, and capacity-DESC paging + keep-first dedup kept the
    200 and silently discarded the binding constraint."""
    ns = _extract(_INGEST,
                  ["map_feature", "_num", "_num_lo", "_num_all", "_rep_point"])
    src = _avista()

    def feat(mw_avail, mw_input, elem, ctg):
        return {"attributes": {"Bus_Name": "Huetter", "Bus_Voltage": 115,
                               "MW_Available": mw_avail, "MW_Input": mw_input,
                               "Limiting_Element": elem, "Limiting_CTG": ctg},
                "geometry": {"x": -116.95, "y": 47.72}}

    r_pass = ns["map_feature"](feat(200, 200, "Line A-B 115kV", "N-1: X"), src)
    r_fail = ns["map_feature"](feat(0, 20, "Line C-D 115kV", "N-1: Y"), src)
    assert r_pass is not None and r_fail is not None, (
        "map_feature dropped a constraint row — a failed constraint "
        "(MW_Available=0) is a real measured result and must survive")
    assert r_pass["feeder_key"] != r_fail["feeder_key"], (
        "two different constraint rows on one bus collapsed onto ONE key — "
        "the capacity-DESC crawl would keep the flattering 200 and delete "
        "the constraint that binds at 20 MW (the stored-Huetter defect)")
    assert r_pass["capacity_type"] == "gen", (
        "rows map with capacity_type %r — the retype did not reach "
        "map_feature output" % r_pass["capacity_type"])
    assert r_fail["capacity_mw_max"] == 0.0, (
        "a failed study row must store its measured 0, never be dropped or "
        "inflated")


# ── the retraction of the already-stored mislabelled rows ─────────────────
class _StubCursor:
    def __init__(self, log):
        self._log = log
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._log.append((sql, params))
        self.rowcount = 197 if "DELETE" in sql else 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubConn:
    def __init__(self):
        self.log = []
        self.committed = False

    def cursor(self):
        return _StubCursor(self.log)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class _StubLogger:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def test_retraction_deletes_the_old_mislabelled_rows():
    """The upsert has no delete path, so a retype alone strands the 197 old
    'Avista (bus headroom)' rows (new key_extra keys → new rows; ON CONFLICT
    never touches the orphans — the stale-twin trap). Executed against a stub
    connection: the retraction must DELETE exactly the old utility label and
    report what it deleted."""
    conn = _StubConn()
    ns = _extract(_INGEST, ["run_capacity_retractions"],
                  assign_names=("_RETRACTED_UTILITIES",),
                  namespace={"_conn": lambda: conn, "logger": _StubLogger()})
    assert "Avista (bus headroom)" in ns["_RETRACTED_UTILITIES"], (
        "_RETRACTED_UTILITIES does not name the old mislabelled Avista label")
    out = ns["run_capacity_retractions"]()
    deletes = [(sql, params) for sql, params in conn.log if "DELETE" in sql]
    assert deletes, "run_capacity_retractions executed no DELETE"
    assert any("hosting_capacity_feeders" in sql for sql, _ in deletes), (
        "the DELETE does not target hosting_capacity_feeders")
    assert any(params == ("Avista (bus headroom)",) for _, params in deletes), (
        "no DELETE was parameterised with the exact old utility label — "
        "a broader predicate could eat honest rows, a narrower one nothing")
    assert conn.committed, "the retraction never committed"
    assert out.get("deleted", {}).get("Avista (bus headroom)") == 197, (
        "the retraction must report what it deleted; got %r" % (out,))


def test_retraction_runs_before_the_weekly_gate():
    """run_hosting_capacity_ingest() must call run_capacity_retractions()
    BEFORE _ran_recently(): the weekly gate reads MAX(ingested_at), which the
    mislabelled ingest itself set — gated behind it, the retraction could
    wait ~6 days while the wrong rows keep serving."""
    tree, _ = _parse(_INGEST)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == "run_hosting_capacity_ingest"]
    assert fns, "run_hosting_capacity_ingest not found"
    lines = {}
    for node in ast.walk(fns[0]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            lines.setdefault(node.func.id, node.lineno)
    assert "run_capacity_retractions" in lines, (
        "run_hosting_capacity_ingest never calls run_capacity_retractions — "
        "the retraction would fire on no schedule at all")
    assert "_ran_recently" in lines, "_ran_recently call disappeared"
    assert lines["run_capacity_retractions"] < lines["_ran_recently"], (
        "the retraction is gated BEHIND the weekly _ran_recently gate")


# ── cross-layer: the live feeder_capacity_mw clause ───────────────────────
def _clause_ns():
    return _extract(_XLAYER, ["_feeder_capacity_clause"],
                    assign_names=("_FEEDER_GEN_REASON", "_FEEDER_PAD_KM"))


def test_load_value_never_inherits_a_gen_row():
    """A gen row LARGER than every load row must not leak into the value:
    the answer is the best LOAD-typed feeder, with the gen exclusion counted
    and its reason attached, and the mandatory single-feeder scale caveat."""
    clause = _clause_ns()["_feeder_capacity_clause"]([
        ("AEP Ohio & I&M (load)", 7.5, "load", "2026-06-01"),
        ("Dominion Energy VA (binned)", 24.0, "gen", None),
    ])
    assert clause["status"] == "validated"
    assert clause["value"] == 7.5, (
        "the 24.0 gen row leaked into the load value: %r" % (clause,))
    assert clause["unit"] == "MW"
    assert clause["utility"] == "AEP Ohio & I&M (load)"
    assert clause["as_of"] == "2026-06-01"
    assert "NOT campus-scale" in clause["basis"], (
        "the mandatory scale caveat is missing from the basis")
    assert clause.get("gen_rows_excluded") == 1
    assert "can I inject" in (clause.get("gen_exclusion_reason") or "")


def test_mva_unit_rides_with_a_peco_value():
    """PECO stores MVA, not MW (its own legend's unit). If the best load row
    is PECO's, the unit must say so — an MVA relayed as MW errs high."""
    clause = _clause_ns()["_feeder_capacity_clause"]([
        ("PECO (Philadelphia) — MVA, not MW", 19.9, "load", None),
    ])
    assert clause["status"] == "validated"
    assert clause["value"] == 19.9
    assert clause["unit"].startswith("MVA"), (
        "PECO's magnitude is MVA and the unit field must carry it; got %r"
        % clause["unit"])


def test_gen_only_scope_keeps_the_exclusion_reason():
    """Where only gen-typed feeders exist, the clause stays unavailable and
    keeps the wrong-physical-quantity reason — it is correct for gen."""
    ns = _clause_ns()
    clause = ns["_feeder_capacity_clause"]([
        ("Con Edison NY", 4.2, "gen", None),
        ("PHI (Pepco/Delmarva/ACE)", 2.0, "gen", None),
    ])
    assert clause["status"] == "unavailable"
    assert "can I inject" in clause["reason"]
    assert "excluded by design" in clause["reason"]
    assert "value" not in clause, "an unavailable clause must carry no figure"


def test_no_rows_is_unknown_never_zero():
    """No feeder coverage near the scope is UNKNOWN — unavailable + reason,
    never 0 and never a value key at all."""
    clause = _clause_ns()["_feeder_capacity_clause"]([])
    assert clause["status"] == "unavailable"
    assert "value" not in clause
    assert "/api/v1/grid/hosting-capacity/coverage" in clause["reason"], (
        "the reason must point at where load coverage IS listed instead of "
        "hardcoding a territory count that will rot")


def test_negative_load_allowance_survives():
    """PSE&G's negative allowances are real (over-subscribed circuits) and
    must never be clamped, dropped, or hidden behind a positive sibling."""
    clause = _clause_ns()["_feeder_capacity_clause"]([
        ("PSE&G New Jersey (EV load allowance)", -1.322, "load", None),
    ])
    assert clause["status"] == "validated"
    assert clause["value"] == -1.322, (
        "a negative load value was altered: %r" % clause["value"])


def test_headroom_reason_records_the_retype():
    """headroom_mw stays unavailable, and its reason must now record WHY the
    scoreboard's one transmission-voltage source no longer counts — while the
    old whole-table 'DER/generation' claim about feeder capacity (factually
    wrong since load territories shipped) must be gone."""
    tree, _ = _parse(_XLAYER)
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    headroom = [s for s in consts if "no measured MW headroom exists" in s]
    assert headroom, "the headroom_mw reason string disappeared"
    joined = " ".join(consts)
    assert "GENERATION-interconnection" in joined, (
        "the headroom ledger no longer records that the one transmission-"
        "voltage source was a generation-interconnection study")
    assert "hosting_capacity_feeders.capacity_mw_max is DER/generation" \
        not in joined, (
            "the old OVER-BROAD claim (the whole table is gen) is still "
            "published — it is factually wrong for the load territories")
    # the live read must order load rows first so gen rows cannot crowd them
    # out of the LIMIT
    assert any("(capacity_type = 'load') DESC" in s for s in consts), (
        "the feeder query does not order load rows first — a gen-dense scope "
        "would crowd every load row out of the row cap")
