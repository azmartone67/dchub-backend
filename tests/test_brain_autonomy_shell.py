"""Brain-autonomy shell: the acting surface keeps its safety properties.

This is the first surface allowed to CLOSE part of the thinking→acting gap,
so its guards pin the properties that make acting safe:
  · rollback is collected BEFORE the mutation (deals actuator),
  · every fire is budgeted (per-actuator + global daily caps),
  · the triage lane moves STATUSES only — it may never DELETE a proposal,
  · the re-resolve actuator uses the scan's own matcher, no private variant,
  · GET never acts (only POST reaches the firing paths),
  · the blueprint is actually registered in main.py (the 0730 loader trap:
    4/11 registered loaders did NOTHING — registration must be pinned).

All static/AST — CI has no DATABASE_URL. Helpers assert they FOUND their
target first: an empty parse satisfies every "not in".
"""

import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "routes", "brain_autonomy_master_shell.py")


def _tree():
    with open(SRC, encoding="utf-8") as f:
        return ast.parse(f.read())


def _func(tree, name):
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == name]
    assert fns, f"{name} not found in {SRC}"
    return fns[0]


def _calls(fn, callee):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == callee) or \
               (isinstance(f, ast.Attribute) and f.attr == callee):
                out.append(n)
    return out


def _consts(node):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_deals_actuator_collects_rollback_before_the_update():
    fn = _func(_tree(), "_fire_deal_dupe_quarantine")
    collect = [n.lineno for n in _consts(fn)
               if "SELECT id, data_flag" in n.value]
    update = [n.lineno for n in _consts(fn)
              if "UPDATE deals" in n.value]
    assert collect and update, "rollback-collect or UPDATE not found"
    assert min(collect) < min(update), (
        "rollback must be collected BEFORE the mutation — after is a"
        " snapshot of the damage, not a way back")
    assert _calls(fn, "deals_ok"), (
        "the quarantine UPDATE lost its served-predicate guard — replays"
        " and races would re-flip restored rows")


def test_every_fire_is_budgeted():
    fn = _func(_tree(), "_lane_actuators")
    assert _calls(fn, "_budget_ok"), (
        "_lane_actuators no longer checks _budget_ok — unbudgeted autonomy"
        " is exactly what this shell exists to prevent")
    tree = _tree()
    caps = {n.targets[0].id: n.value.value for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id.startswith("ACTUATOR_DAILY_CAP")
            and isinstance(n.value, ast.Constant)}
    assert set(caps) == {"ACTUATOR_DAILY_CAP_EACH",
                         "ACTUATOR_DAILY_CAP_GLOBAL"}, caps
    assert all(isinstance(v, int) and 1 <= v <= 5 for v in caps.values()), (
        f"daily caps drifted out of the sane band: {caps}")


def test_triage_moves_statuses_and_never_deletes():
    tree = _tree()
    # SQL constants only — the module DOCSTRING legitimately says "never
    # DELETE" beside the table name (the grep-hit-a-comment trap, again).
    table_sql = [n.value for n in _consts(tree)
                 if "brain_enhancement_proposals" in n.value
                 and ("SELECT" in n.value or "UPDATE" in n.value
                      or "DELETE" in n.value.upper().replace("NEVER DELETE", ""))]
    assert len(table_sql) >= 3, "proposal SQL not found — vacuous guard"
    for s in table_sql:
        assert "DELETE" not in s.upper(), (
            "the triage lane may only move statuses — a DELETE destroys the"
            " brain's own history: " + s[:80])


def test_reresolve_actuator_uses_the_scans_matcher():
    fn = _func(_tree(), "_fire_entity_reresolve")
    imported = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
                and any(a.name == "_reresolve_unmatched" for a in n.names)]
    assert imported and _calls(fn, "_reresolve_unmatched"), (
        "the actuator must fire the scan's own _reresolve_unmatched — a"
        " private variant is how the lane and resolver drift")


def test_get_never_acts():
    fn = _func(_tree(), "brain_autonomy_tick")
    src_ok = False
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute) \
           and n.left.attr == "method":
            comps = [c.value for c in n.comparators
                     if isinstance(c, ast.Constant)]
            src_ok = comps == ["POST"]
    assert src_ok, (
        "act must be request.method == 'POST' — a GET that acts turns every"
        " cache-warm and dashboard view into an actuation")


def test_blueprint_is_registered_in_main():
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        main_src = f.read()
    assert "register_blueprint(brain_autonomy_master_shell_bp" in main_src, (
        "main.py no longer registers the shell — the 0730 trap: a loader"
        " that exists but is never wired does NOTHING, silently")
