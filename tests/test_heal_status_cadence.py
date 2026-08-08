"""The cadence /api/v1/heal/status publishes must be the cadence the healer runs.

The bug (2026-08-08). `dchub_self_heal.get_status()` returned a literal
`"next_cycle_minutes": 5`. The healer's interval was loosened four times
underneath it — 5 min → 30 min (FF+25-followup) → 60 min (r71) → 6 h (r72)
— and r78 then added a 5.5 h DB-backed gate on top, and the literal never
moved. /api/v1/heal/status is public and unauthenticated; confirmed live
2026-08-08 it served `"next_cycle_minutes":5` while the measured cadence in
`self_heal_events WHERE endpoint = '__cycle__'` was 5.57–11.48 h, median
~6 h. The module docstring documented the real cadence correctly the whole
time, so the file contradicted itself and the wrong half was the published
one.

Why this test is shaped the way it is: re-hardcoding a corrected literal
(360, or 330) would pass any test that asserts a number, and would go
stale on the fifth loosening exactly as 5 did. So nothing here asserts a
cadence VALUE. It asserts the wiring:

  1. DERIVED  — the cadence fields in get_status()'s payload must trace,
                through local assignments, to HEAL_INTERVAL_HOURS /
                HEAL_MIN_INTERVAL_HOURS. A bare numeric literal fails.
  2. SAME NUMBER — those constants must be the ones start_scheduler()
                schedules with and heal_cycle()'s gate compares against.
                Two literals that happen to agree today is the state this
                fix removes; a constant nothing uses would be the vacuous
                version of it.
  3. BEHAVIOUR— drive the real get_status() and check the published wait
                counts down against the floor, floors at 0 rather than
                going negative, and is None (not 0) when the last-cycle
                read is unavailable. Expected values are computed FROM the
                constants, so raising the interval never edits this file.
  4. GATE TIE — the floor status advertises is the floor heal_cycle()
                actually enforces: skip just under it, probe just over it.

House rules: no DB, never import main (it opens pools and registers ~200
blueprints), nothing at module scope. dchub_self_heal is stdlib-only at
import time, so it is driven for real with its DB edges stubbed.

Run:  python3 -m pytest tests/test_heal_status_cadence.py -v
"""

import ast
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SELF_HEAL = _ROOT / "dchub_self_heal.py"

# The two module constants the published cadence must be derived from.
_INTERVAL = "HEAL_INTERVAL_HOURS"
_FLOOR = "HEAL_MIN_INTERVAL_HOURS"

# Fields in the /heal/status payload that describe the schedule. Each one
# is a number a reader will act on, so each one must be derived.
_CADENCE_KEYS = ("interval_hours", "min_interval_hours", "next_cycle_minutes")


# ── helpers ───────────────────────────────────────────────────────────

def _sh():
    """Import the real module (function scope: nothing at module scope)."""
    import dchub_self_heal as sh
    return sh


def _func(name):
    tree = ast.parse(_SELF_HEAL.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        "%s() is not in dchub_self_heal.py any more — this fence is pointing "
        "at a function that no longer exists and would pass vacuously" % name)


def _returned_dict_fields(fn):
    """{key: value-node} over every dict literal `fn` returns."""
    out = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out[k.value] = v
    return out


def _derived_names(fn):
    """Local names in `fn` whose value traces back to a cadence constant.

    A cadence field is allowed to be `HEAL_MIN_INTERVAL_HOURS` directly or
    a local computed from it (the published wait goes through
    `gate_opens_in_h`). Transitive, so an extra intermediate step does not
    trip the fence — only a value that never touches the constants does.
    """
    tainted = {_INTERVAL, _FLOOR}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(n, ast.Name) and n.id in tainted
                       for n in ast.walk(node.value)):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id not in tainted:
                    tainted.add(tgt.id)
                    changed = True
    return tainted


class _FakeCursor:
    """Enough cursor for get_status()'s two aggregate reads. No DB."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return None  # PGCursorWrapper.execute returns None in prod too

    def fetchone(self):
        return (7, 5, 2, None)  # total, ok, fail, MAX(ts)

    def fetchall(self):
        return [("noneround", 3)]


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor()


def _status(monkeypatch, age_hours):
    """Drive the REAL get_status() with only its DB edges stubbed."""
    sh = _sh()
    monkeypatch.setattr(sh, "DATABASE_URL", "postgres://stub")
    monkeypatch.setattr(sh, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(sh, "_last_cycle_age_hours", lambda: age_hours)
    out = sh.get_status()
    assert out.get("healer") == "alive", (
        "the stubs did not drive get_status() to its real payload: %r" % (out,))
    return sh, out


# ── 1. DERIVED — no literal cadence in the payload ────────────────────

@pytest.mark.parametrize("key", _CADENCE_KEYS)
def test_cadence_field_is_derived_from_the_schedule_constants(key):
    """This is the fence the original bug walks into.

    `"next_cycle_minutes": 5` is an ast.Constant that references neither
    constant, so it fails here — and so does a corrected literal like 330,
    which is the point: the next loosening must not need this file edited.
    """
    fn = _func("get_status")
    fields = _returned_dict_fields(fn)
    assert key in fields, (
        "get_status() no longer publishes %r. Dropping the field is a valid "
        "fix, but it is a deliberate API change — update this fence on "
        "purpose rather than letting the cadence go unfenced." % key)

    value = fields[key]
    derived = _derived_names(fn)
    refs = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
    assert refs & derived, (
        "get_status() publishes %r as %s — a value that never reads %s or %s. "
        "That is how it came to advertise a 5-minute healer across four "
        "loosenings of the interval; a fresh literal goes stale the same way."
        % (key, ast.dump(value), _INTERVAL, _FLOOR))


def test_status_still_derives_probe_count_from_probes():
    """Control: `probe_count` was already derived (`len(PROBES)`) and must
    stay that way. It also proves the check above discriminates — the taint
    walk finds no cadence constant here, and this field is not asked to."""
    value = _returned_dict_fields(_func("get_status"))["probe_count"]
    assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == "len", (
        "probe_count is no longer len(PROBES): %s" % ast.dump(value))


# ── 2. SAME NUMBER — the constants are the ones that actually run ─────

def test_scheduler_schedules_with_the_constant_status_publishes():
    """A constant that nothing schedules with is the vacuous version of
    this fix: status would read from one number while APScheduler ran on
    another, which is the bug again with an extra step."""
    fn = _func("start_scheduler")
    jobs = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "add_job"
            and any(isinstance(a, ast.Constant) and a.value == "interval"
                    for a in n.args)]
    assert len(jobs) == 1, (
        "expected exactly one APScheduler interval job in start_scheduler(), "
        "found %d — status can only publish one interval" % len(jobs))
    hours = [kw.value for kw in jobs[0].keywords if kw.arg == "hours"]
    assert hours, "the heal_cycle interval job no longer takes hours="
    assert isinstance(hours[0], ast.Name) and hours[0].id == _INTERVAL, (
        "the interval job is scheduled with %s instead of %s, so "
        "/heal/status publishes a number the scheduler does not use"
        % (ast.dump(hours[0]), _INTERVAL))


def test_interval_gate_compares_against_the_constant_status_publishes():
    """The DB gate is the one that actually binds (it survives worker
    recycles and is shared across replicas), so it is the floor a caller
    reading `min_interval_hours` is really being told about."""
    fn = _func("heal_cycle")
    comparators = [c for n in ast.walk(fn) if isinstance(n, ast.Compare)
                   for c in n.comparators]
    assert any(isinstance(c, ast.Name) and c.id == _FLOOR for c in comparators), (
        "heal_cycle()'s interval gate no longer compares against %s — the "
        "floor published by /heal/status is not the floor being enforced"
        % _FLOOR)


def test_the_constants_are_a_coherent_schedule():
    sh = _sh()
    interval = getattr(sh, _INTERVAL)
    floor = getattr(sh, _FLOOR)
    assert isinstance(interval, (int, float)) and interval > 0
    assert isinstance(floor, (int, float)) and floor > 0
    assert floor <= interval, (
        "the DB floor (%s h) is above the scheduled interval (%s h): the "
        "scheduler would be gated out of most of its own ticks and the "
        "published interval would be a fiction" % (floor, interval))


# ── 3. BEHAVIOUR — drive the real get_status() ────────────────────────

def test_status_publishes_the_schedule_it_runs_on(monkeypatch):
    sh, status = _status(monkeypatch, age_hours=1.0)
    assert status["interval_hours"] == getattr(sh, _INTERVAL)
    assert status["min_interval_hours"] == getattr(sh, _FLOOR)


def test_next_cycle_counts_down_to_the_gate(monkeypatch):
    """Expected value computed from the constant, never restated."""
    sh, status = _status(monkeypatch, age_hours=1.0)
    expected = int(round((getattr(sh, _FLOOR) - 1.0) * 60))
    assert status["next_cycle_minutes"] == expected, (
        "published wait %r does not count down to the %s h floor (expected %d)"
        % (status["next_cycle_minutes"], _FLOOR, expected))
    assert status["last_cycle_hours_ago"] == 1.0


def test_a_fresh_cycle_advertises_the_whole_floor(monkeypatch):
    """The shape the old literal got wrong: right after a cycle, the next
    one is a floor away — not 5 minutes."""
    sh, status = _status(monkeypatch, age_hours=0.0)
    assert status["next_cycle_minutes"] == int(round(getattr(sh, _FLOOR) * 60))


def test_an_overdue_cycle_reports_zero_not_a_negative_wait(monkeypatch):
    sh, status = _status(monkeypatch, age_hours=getattr(_sh(), _FLOOR) + 3.0)
    assert status["next_cycle_minutes"] == 0, (
        "an overdue healer published %r minutes" % status["next_cycle_minutes"])


def test_unknown_last_cycle_is_none_not_zero(monkeypatch):
    """`_last_cycle_age_hours()` returns None when the read fails. Zero
    would read as 'eligible now' — a measurement failure must not be
    published as a measurement."""
    sh, status = _status(monkeypatch, age_hours=None)
    assert status["next_cycle_minutes"] is None
    assert status["last_cycle_hours_ago"] is None
    # …and the schedule itself is still known: those two come from the
    # constants, not from the DB.
    assert status["interval_hours"] == getattr(sh, _INTERVAL)


def test_published_wait_never_exceeds_the_floor(monkeypatch):
    sh = _sh()
    floor_minutes = getattr(sh, _FLOOR) * 60
    for age in (0.0, 0.25, 1.0, 3.0, 5.4, 12.0, 400.0):
        _, status = _status(monkeypatch, age_hours=age)
        assert 0 <= status["next_cycle_minutes"] <= floor_minutes, (
            "age %sh published %r minutes, outside [0, %s]"
            % (age, status["next_cycle_minutes"], floor_minutes))


# ── 4. GATE TIE — the advertised floor is the enforced floor ──────────

def _stub_cycle(monkeypatch, age_hours):
    """heal_cycle() with its DB/network edges stubbed. No DB, no main."""
    sh = _sh()
    calls = {"events": []}

    fake_main = types.ModuleType("main")
    fake_main.is_current_leader = lambda: True
    monkeypatch.setitem(sys.modules, "main", fake_main)

    monkeypatch.setattr(sh, "_last_cycle_age_hours", lambda: age_hours)
    monkeypatch.setattr(sh, "ensure_log_table", lambda: None)
    monkeypatch.setattr(sh, "acquire_lock", lambda: object())
    monkeypatch.setattr(sh, "release_lock", lambda c: None)
    monkeypatch.setattr(sh, "probe", lambda path, kind: (200, ""))
    monkeypatch.setattr(sh, "detect", lambda body, status: [])
    monkeypatch.setattr(sh, "log_event",
                        lambda cid, ep, pat, fix, ok, det:
                        calls["events"].append(ep))
    return sh, calls


def test_the_gate_fires_just_under_the_published_floor(monkeypatch):
    sh, calls = _stub_cycle(monkeypatch, age_hours=getattr(_sh(), _FLOOR) - 0.01)
    result = sh.heal_cycle()
    assert result.get("reason") == "interval_gate", (
        "a cycle 0.01 h under the published floor was allowed to probe: %r"
        % (result,))
    assert calls["events"] == []


def test_the_gate_opens_just_over_the_published_floor(monkeypatch):
    """The other half — without this, a gate that refused everything would
    also 'pass', and /heal/status would be advertising a floor that is
    really a wall."""
    sh, calls = _stub_cycle(monkeypatch, age_hours=getattr(_sh(), _FLOOR) + 0.01)
    result = sh.heal_cycle()
    assert result.get("skipped") is not True, (
        "a cycle past the published floor was still gated: %r" % (result,))
    assert result["probes"] == len(sh.PROBES)
    assert "__cycle__" in calls["events"], (
        "the cycle ran without writing the __cycle__ marker the gate reads")
