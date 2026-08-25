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


# ═══════════════════════════════════════════════════════════════════════════
# the tick reports what it cost — 2026-08-23
# ═══════════════════════════════════════════════════════════════════════════
# #3086 fixed the blind-spot query that had /admin/brain-autonomy answering
# Cloudflare's 502 on 13 of 13 probes at a flat ~12.2s while the tick behind
# it ran 4.2s-26.7s. This adds the part that made it hard to SEE rather than
# hard to fix: the tick reported no duration, so attributing it meant probing
# the JSON route by hand and correlating wall-clock against which check
# happened to fail. #3086's own finding was that both callers swallowed the
# timeout and reported UNMEASURED, "so nothing ever went red" — a shell can
# be minutes slow and look identical to a healthy one.
#
# The guards are behavioural, not static: a constant that exists but never
# reaches the payload measures nothing, and that is the exact shape of the
# defect above.

def _shell():
    import importlib
    return importlib.import_module("routes.brain_autonomy_master_shell")


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        return None

    def fetchone(self):
        return None


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _tick_without_a_db(monkeypatch, *, act=False):
    """Run _tick with the database stubbed out (CI has no DATABASE_URL)."""
    m = _shell()
    monkeypatch.setattr(m, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(m, "_ensure_tables", lambda c: None)
    monkeypatch.setattr(m, "_lane_actuators", lambda c, a: ([], False))
    monkeypatch.setattr(m, "_lane_proposals", lambda c, a: [])
    monkeypatch.setattr(m, "_lane_activation", lambda c: [])
    monkeypatch.setattr(m, "_stamp_heartbeat", lambda c, ok, ms: None)
    return m._tick(act=act)


def test_the_tick_reports_its_total_cost(monkeypatch):
    """MUTATION: drop `"ms": ms` from the _tick payload -> this fails."""
    out = _tick_without_a_db(monkeypatch)
    assert isinstance(out.get("ms"), int), (
        "the tick payload lost its total duration — a shell that does not "
        "say how long it took looks identical whether it ran in 1s or 26s")


def test_every_lane_reports_its_own_cost(monkeypatch):
    """The total alone cannot say WHICH lane spent the time.

    MUTATION: drop `"ms"` from the dict _lane() returns -> this fails.
    """
    out = _tick_without_a_db(monkeypatch)
    lanes = out.get("lanes") or []
    assert len(lanes) == 3, f"expected 3 lanes, got {len(lanes)}"
    for l in lanes:
        assert isinstance(l.get("ms"), int), (
            f"lane {l.get('lane')!r} does not report its own duration")


def test_the_heartbeat_is_stamped_with_the_same_number_the_payload_carries(
        monkeypatch):
    """CONTROL — must stay GREEN.

    cron_last_run.last_duration_ms is what brain_consistency_radar watches.
    Computing the payload's `ms` separately from the heartbeat's would let the
    board and the radar disagree about how slow this loop is, which is worse
    than neither having the number.
    """
    m = _shell()
    seen = {}
    monkeypatch.setattr(m, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(m, "_ensure_tables", lambda c: None)
    monkeypatch.setattr(m, "_lane_actuators", lambda c, a: ([], False))
    monkeypatch.setattr(m, "_lane_proposals", lambda c, a: [])
    monkeypatch.setattr(m, "_lane_activation", lambda c: [])
    monkeypatch.setattr(m, "_stamp_heartbeat",
                        lambda c, ok, ms: seen.update(ms=ms, ok=ok))
    out = m._tick(act=False)
    assert seen.get("ms") == out.get("ms"), (
        f"heartbeat stamped {seen.get('ms')!r} while the payload reports "
        f"{out.get('ms')!r} — the radar and the board would disagree")


def test_the_lane_verdicts_are_unchanged_by_the_timing_wrapper(monkeypatch):
    """CONTROL — must stay GREEN.

    _lane() wraps three calls that used to be made inline. The wrapper must
    pass the same arguments and return the same verdicts; a lane that reads
    `pass` from the wrong place is a silent board.
    """
    m = _shell()
    calls = []
    monkeypatch.setattr(m, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(m, "_ensure_tables", lambda c: None)
    monkeypatch.setattr(m, "_stamp_heartbeat", lambda c, ok, ms: None)

    # critical=True — _lane_pass only reaches a True/False verdict from
    # CRITICAL checks; a lane of non-critical checks is always None and the
    # assertions below could not tell a working wrapper from a broken one.
    ok_check = m._check("x", "x", True, "d", critical=True)
    bad_check = m._check("y", "y", False, "d", critical=True)
    monkeypatch.setattr(m, "_lane_actuators",
                        lambda c, a: (calls.append(("act", a)), ([ok_check], False))[1])
    monkeypatch.setattr(m, "_lane_proposals",
                        lambda c, a: (calls.append(("prop", a)), [bad_check])[1])
    monkeypatch.setattr(m, "_lane_activation",
                        lambda c: (calls.append(("activ", None)), [ok_check])[1])

    out = m._tick(act=True)
    assert [c[0] for c in calls] == ["act", "prop", "activ"], (
        f"lane order or arity changed: {calls}")
    assert calls[0][1] is True and calls[1][1] is True, (
        "the act flag stopped reaching the lanes that branch on it")
    lanes = out.get("lanes") or []
    assert [l["pass"] for l in lanes] == [True, False, True], (
        f"the wrapper changed the verdicts: {[l['pass'] for l in lanes]}")
    assert out["lanes_pass"] == 2 and out["lanes_total"] == 3
