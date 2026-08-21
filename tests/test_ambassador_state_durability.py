"""data/ambassador_state.json is TRACKED state, and a boot used to shred it.

register_ambassador_routes() ran two full cycles — start_scheduler(3600)'s
thread runs one immediately, and an explicit run_ambassador_cycle() ran a second
on the main thread — against ONE AgenticAmbassador and ONE state file. Both
cycles call _save_state() several times, and _save_state was
`open(STATE_FILE, 'w')` + json.dump: truncate first, write second. Two of those
overlapping leave a torn file, and _load_state's bare `except: pass` then
returned the EMPTY DEFAULT, which the next save persisted over the history.

Observed 2026-08-21 in a working checkout: 12,989 lines / total_outreach 1473
collapsed to 537 / 60. origin/main was never affected, so nothing was lost for
good — but only because no one committed the gutted copy.

These are behaviour tests. They drive the real functions from the shipped
module against a redirected state path (DCHUB_AMBASSADOR_STATE_FILE, the knob
added in #3014); none of them import main.py.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_GOOD = {
    "outreach_sent": [{"partner": "kept", "n": i} for i in range(50)],
    "ai_registrations": [],
    "citations_detected": [],
    "stories_generated": [],
    "partner_responses": [],
    "total_outreach": 1473,
    "total_citations": 0,
    "last_cycle": "2026-08-20T00:00:00",
}


@pytest.fixture
def amb(tmp_path, monkeypatch):
    """The real module, with its state path pointed at tmp_path."""
    state = tmp_path / "ambassador_state.json"
    monkeypatch.setenv("DCHUB_AMBASSADOR_STATE_FILE", str(state))
    import agentic_ambassador as A
    monkeypatch.setattr(A, "STATE_FILE", str(state))
    return A, state


def test_a_failed_save_leaves_the_previous_file_intact(amb, monkeypatch):
    """★ The data-loss guard. The old truncate-then-write meant ANY failure
    mid-write — including json.dumps raising because another thread appended to
    a list — left a gutted file on disk. Publishing atomically means a failed
    save is a no-op, not a shredder."""
    A, state = amb
    state.write_text(json.dumps(_GOOD), encoding="utf-8")
    before = state.read_bytes()

    a = A.AgenticAmbassador()
    assert a.state["total_outreach"] == 1473, "fixture did not load"

    boom = RuntimeError("list changed size during iteration")
    monkeypatch.setattr(A.json, "dumps",
                        lambda *args, **kw: (_ for _ in ()).throw(boom))
    a.state["outreach_sent"].append({"partner": "doomed"})
    a._save_state()  # must swallow and log, never truncate

    assert state.read_bytes() == before, (
        "a failed save destroyed the previous state file — this is the "
        "truncate-then-write bug that collapsed 12,989 lines to 537")
    assert json.loads(state.read_text())["total_outreach"] == 1473


def test_concurrent_savers_never_publish_invalid_json(amb):
    """Two cycles used to run at once. Whatever ends up on disk must always be
    a complete document, and no temp files may be left behind."""
    A, state = amb
    a = A.AgenticAmbassador()
    a.state = json.loads(json.dumps(_GOOD))

    errors: list = []

    def hammer(tag):
        try:
            for i in range(25):
                a.state["outreach_sent"].append({"partner": tag, "n": i})
                a._save_state()
        except Exception as exc:                      # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"savers raised: {errors}"
    loaded = json.loads(state.read_text(encoding="utf-8"))
    assert loaded["total_outreach"] == 1473
    assert len(loaded["outreach_sent"]) >= 50

    leftovers = [p.name for p in state.parent.iterdir()
                 if p.name.startswith(".ambassador_state-")]
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_unreadable_state_is_preserved_not_silently_blanked(amb, caplog):
    """★ The amplifier. A torn file only became DATA LOSS because _load_state
    swallowed the parse error, returned the empty default, and let the next save
    persist it. An existing-but-unreadable file must survive somewhere."""
    A, state = amb
    state.write_text('{"outreach_sent": [{"partner": "irrepla', encoding="utf-8")

    with caplog.at_level("ERROR"):
        a = A.AgenticAmbassador()

    quarantine = pathlib.Path(str(state) + ".corrupt")
    assert quarantine.exists(), (
        "the unreadable state file was dropped on the floor; its bytes are the "
        "only copy of whatever had accumulated")
    assert "irrepla" in quarantine.read_text(encoding="utf-8")
    assert a.state["total_outreach"] == 0, "should start from empty, loudly"
    assert any("unreadable" in r.message.lower() or "unreadable" in r.getMessage().lower()
               for r in caplog.records), "the failure was not logged"

    # And the fresh state must be publishable without touching the quarantine.
    a._save_state()
    assert json.loads(state.read_text())["total_outreach"] == 0
    assert "irrepla" in quarantine.read_text(encoding="utf-8")


def test_absent_state_file_is_still_an_ordinary_fresh_start(amb):
    """The quarantine path must not turn a first-ever boot into an error."""
    A, state = amb
    assert not state.exists()
    a = A.AgenticAmbassador()
    assert a.state["total_outreach"] == 0
    assert not pathlib.Path(str(state) + ".corrupt").exists()


def test_registration_runs_exactly_one_cycle(amb, monkeypatch):
    """★ The racer itself. start_scheduler's thread already runs a cycle before
    its first sleep, so the explicit call that used to follow it made two cycles
    run CONCURRENTLY against one instance. One boot, one cycle."""
    flask = pytest.importorskip("flask")
    A, _state = amb

    calls: list = []
    monkeypatch.setattr(A.ambassador, "run_ambassador_cycle",
                        lambda: calls.append(1) or {})
    monkeypatch.setattr(A.ambassador, "running", False)

    app = flask.Flask(__name__ + "_amb_reg")
    try:
        A.register_ambassador_routes(app)
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)  # give a second (buggy) cycle time to show up
        assert calls, "registration ran NO cycle — the boot pass was lost"
        assert len(calls) == 1, (
            f"registration ran {len(calls)} cycles concurrently against one "
            "instance and one state file — that race tore the state file")
    finally:
        A.ambassador.stop_scheduler()
