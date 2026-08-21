"""data/ai_ecosystem_state.json is TRACKED 2.1 MB state behind the same trap
that shredded data/ambassador_state.json in #3018.

ai_ecosystem_agent.py carried the identical shape: a hardcoded relative
AGENT_STATE_FILE, save_state() as open(...,'w') + json.dump (truncate first,
write second), and a load_state() that collapsed "file absent" and "file
unreadable" into the same empty default — the step that turns one torn write
into permanent loss, because the next save persists the blank over the history.

It has one mitigating difference and one aggravating one. register_ai_ecosystem_
routes() is NEVER called — `git log -S` finds no reference in main.py anywhere
in history — so unlike the ambassador this has never run at boot, and the suite
only escaped dirtying the file because run_cycle() raises before reaching the
save. But POST /api/ai-ecosystem/run calls run_cycle() on a request thread while
the scheduler thread runs its own, against one module-level `agent` and one
file, so the concurrency is there waiting for whoever wires it up.

Behaviour tests against the shipped module, path redirected to tmp_path. None
import main.py.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_GOOD = {
    "created_at": "2026-01-01T00:00:00",
    "total_discoveries": 4211,
    "total_enrichments": 900,
    "total_outreach": 1473,
    "platforms_registered": ["claude", "openai"],
    "last_run": "2026-08-20T00:00:00",
    "discovered_companies": [{"id": f"co-{i}", "keep": True} for i in range(80)],
    "outreach_log": [],
    "learning_insights": [],
}


@pytest.fixture
def eco(tmp_path, monkeypatch):
    """The real module, with its state path pointed at tmp_path."""
    state = tmp_path / "ai_ecosystem_state.json"
    monkeypatch.setenv("DCHUB_AI_ECOSYSTEM_STATE_FILE", str(state))
    import ai_ecosystem_agent as E
    monkeypatch.setattr(E, "AGENT_STATE_FILE", str(state))
    return E, state


def _agent(E, state_dict=None):
    a = E.AIEcosystemAgent.__new__(E.AIEcosystemAgent)
    a.state = state_dict if state_dict is not None else dict(_GOOD)
    return a


def test_the_state_path_is_redirectable(eco):
    """★ The whole reason the suite could dirty a tracked file: the path was a
    module constant with no way to point it elsewhere."""
    E, state = eco
    _agent(E).save_state()
    assert state.exists(), "save ignored the redirected path"
    assert json.loads(state.read_text())["total_outreach"] == 1473


def test_the_env_knob_alone_moves_the_default_path(tmp_path):
    """★ The test above cannot prove the knob works: the fixture also
    monkeypatches AGENT_STATE_FILE directly, so it stays green even if the
    env lookup is deleted. Import the module in a SUBPROCESS with nothing but
    the environment variable set.

    The sibling module has a second line of defence here — deleting
    DCHUB_AMBASSADOR_STATE_FILE reddens test_app_contract_gate.py, whose digest
    check notices the boot dirtying data/. That backstop cannot cover THIS
    module: register_ai_ecosystem_routes() is never called, so no boot touches
    this file and the digest never moves. This test is the only thing standing
    between a refactor and a tracked 2.1 MB file getting rewritten again.
    """
    state = tmp_path / "redirected.json"
    env = {**os.environ, "DCHUB_AI_ECOSYSTEM_STATE_FILE": str(state)}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import ai_ecosystem_agent as E; print(E.AGENT_STATE_FILE)"],
        cwd=str(_ROOT), env=env, capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, f"import failed: {proc.stderr[-600:]}"
    assert proc.stdout.strip() == str(state), (
        "AGENT_STATE_FILE ignored DCHUB_AI_ECOSYSTEM_STATE_FILE — the path is "
        f"hardcoded again (got {proc.stdout.strip()!r})")


def test_serialisation_happens_while_the_lock_is_held(eco, monkeypatch):
    """★ os.replace() alone keeps readers from seeing a torn file, so the
    concurrency test below passes with or without the lock. The lock guards a
    different failure: json.dumps walking a list that another thread is
    appending to, which raises and loses that save. Assert the invariant
    directly rather than racing for it."""
    E, _state = eco
    seen = {}
    real_dumps = E.json.dumps

    def spy(*a, **k):
        seen["locked"] = E._STATE_LOCK.locked()
        return real_dumps(*a, **k)

    monkeypatch.setattr(E.json, "dumps", spy)
    _agent(E).save_state()

    assert seen.get("locked") is True, (
        "state was serialised outside _STATE_LOCK — a concurrent append can "
        "make json.dumps raise mid-save")


def test_a_failed_save_leaves_the_previous_file_intact(eco, monkeypatch):
    """★ The data-loss guard. Truncate-then-write meant ANY failure mid-write
    left a gutted file. Publishing atomically makes a failed save a no-op."""
    E, state = eco
    _agent(E).save_state()
    before = state.read_text()

    boom = _agent(E)
    boom.state = {"discovered_companies": {1, 2, 3}}   # a set: json.dumps raises
    boom.save_state()                                   # must not raise

    assert state.read_text() == before, (
        "a failed save damaged the previously-good file")
    leftovers = [p.name for p in state.parent.iterdir()
                 if p.name.startswith(".ai_ecosystem_state.")]
    assert not leftovers, f"temp file left behind: {leftovers}"


def test_an_unreadable_file_is_preserved_not_silently_blanked(eco):
    """★ The step that made the loss permanent. Returning the empty default for
    a file that EXISTS but will not parse means the next save overwrites real
    history with nothing."""
    E, state = eco
    state.write_text('{"total_outreach": 1473, "truncated": ')   # torn write

    loaded = E.AIEcosystemAgent.load_state(_agent(E))

    assert loaded["total_outreach"] == 0, "expected a fresh-start skeleton"
    quarantine = pathlib.Path(str(state) + ".corrupt")
    assert quarantine.exists(), (
        "the unreadable state was discarded — the bytes must be moved aside")
    assert "1473" in quarantine.read_text(), "quarantine lost the original bytes"
    assert not state.exists(), "the torn file was left in place to be re-read"


def test_an_absent_file_is_an_ordinary_fresh_start(eco):
    """The control: absent must NOT be treated as corruption."""
    E, state = eco
    loaded = E.AIEcosystemAgent.load_state(_agent(E))
    assert loaded["total_discoveries"] == 0
    assert not pathlib.Path(str(state) + ".corrupt").exists(), (
        "a first run quarantined a file that never existed")


def test_concurrent_saves_never_publish_a_torn_file(eco):
    """The scheduler thread and POST /api/ai-ecosystem/run both call run_cycle()
    against one shared agent. Every reader must see a complete document."""
    E, state = eco
    a = _agent(E)
    _agent(E).save_state()
    errors = []

    def writer(n):
        for _ in range(12):
            a.state["total_outreach"] = n
            a.save_state()

    def reader():
        for _ in range(40):
            try:
                if state.exists():
                    json.loads(state.read_text())
            except Exception as e:      # noqa: BLE001
                errors.append(repr(e))

    threads = ([threading.Thread(target=writer, args=(i,)) for i in range(4)]
               + [threading.Thread(target=reader) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a reader saw a torn file: {errors[:3]}"
    assert json.loads(state.read_text())["total_discoveries"] == 4211
