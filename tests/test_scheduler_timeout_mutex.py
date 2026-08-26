"""What `_run_with_guard` must do when the hard timeout fires.

WHY THIS EXISTS
---------------
`_run_with_guard` holds TWO mutual-exclusion mechanisms:

  * `_active_crawler` — the in-process singleton, one crawl at a time per replica;
  * a `crawler_run_claims` row — the cross-replica singleton, one of the two
    Railway replicas runs each crawl.

Its `finally` released BOTH unconditionally. On a TIMEOUT that is exactly
backwards. Python cannot kill a thread, so `t.join(HARD_TIMEOUT_SECONDS)`
returning does not stop the crawler — the abandoned thread is STILL RUNNING and
still writing when the finally hands both locks back, so a second copy of the
same crawler could start on top of it.

`_CLAIM_TTL_SECONDS = HARD_TIMEOUT_SECONDS + 120` was written precisely so a
claim OUTLIVES a run that overran. The unconditional
`DELETE FROM crawler_run_claims` threw that design away.

Measured 2026-08-26 on the public dead-man ledger: `worker:intl_infra_ingest`
reached `status=timeout` at 1800s (`last_run` 2026-08-26T05:30:09Z, started
05:00:09) — SCHEDULE #8, hour 5.

THE SHAPE OF THE FIX, AND THE TRAP IN IT
----------------------------------------
"Stop clearing `_active_crawler`" is NOT the fix, and
`test_active_crawler_is_still_cleared_on_timeout` is here to stop anyone
"tightening" it into one. `_active_crawler` gates EVERY lane, so leaving it set
would wedge the whole replica permanently on one hung crawl — strictly worse
than the duplicate. It stays fail-open; the duplicate is stopped per-NAME by
`_abandoned_runs` instead.
"""
import importlib
import threading

import pytest

cs = importlib.import_module("crawler_scheduler")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Env:
    """What the guard did, captured instead of executed."""
    def __init__(self):
        self.released = []      # names passed to _release_crawler_run
        self.beats = []         # {name, status, note} per _beat_deadman call
        self.gate = threading.Event()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """No DB, no ledger, no leaked threads.

    `_beat_deadman` reaches routes.ingest_runs.record_beat (a real write) and
    `_claim_crawler_run` opens psycopg2, so both become recorders. Every
    blocking crawler a test starts is released here, or pytest hangs waiting on
    a daemon thread that never returns.
    """
    e = _Env()
    monkeypatch.setattr(cs, "_claim_crawler_run", lambda name: True)
    monkeypatch.setattr(cs, "_release_crawler_run", lambda name: e.released.append(name))
    monkeypatch.setattr(
        cs, "_beat_deadman",
        lambda name, status, duration_s=None, cadence_h=None, note=None:
            bool(e.beats.append({"name": name, "status": status, "note": note})) or True)

    cs._active_crawler = None
    cs._abandoned_runs.clear()
    try:
        yield e
    finally:
        e.gate.set()
        for t in list(cs._abandoned_runs.values()):
            t.join(timeout=5)
        cs._abandoned_runs.clear()
        cs._active_crawler = None


@pytest.fixture
def guard(monkeypatch, env):
    """Run a crawler under a millisecond hard timeout.

    Returns (run, gate, started): `run(name)` invokes the real
    `_run_with_guard` with a crawler that blocks on the gate until the test
    releases it — i.e. a run that WILL time out and WILL still be alive after.
    """
    monkeypatch.setattr(cs, "HARD_TIMEOUT_SECONDS", 0.05)
    # _run_with_guard ends with time.sleep(OVERLAP_GUARD_SECONDS) — 30s of real
    # wall clock per run, which is also why the five cascaded hour-5 lanes were
    # spaced ~30s apart. Not the behaviour under test; zero it or this file
    # takes ~8 minutes.
    monkeypatch.setattr(cs, "OVERLAP_GUARD_SECONDS", 0)
    started = []

    def _blocker():
        started.append(1)
        env.gate.wait(timeout=30)

    def run(name="testlane", func=_blocker):
        cs._run_with_guard(name, func)

    return run, env.gate, started


# ---------------------------------------------------------------------------
# ★ THE BUG: a timeout must not hand back the cross-replica claim.
# ---------------------------------------------------------------------------

def test_timeout_keeps_the_run_claim(guard, env):
    """★ THE BUG. The finally ran `_release_crawler_run` unconditionally, so the
    instant a crawl was abandoned the other replica could claim and start a
    SECOND copy of it — while the first was still writing.
    """
    run, _gate, _ = guard
    run("intl_infra_ingest")
    assert env.released == [], (
        "released the run claim for a crawler whose thread is STILL RUNNING — "
        "the other replica can now start a second copy on top of it")


def test_success_still_releases_the_claim(guard, env):
    """The fix must be scoped to the timeout. A clean run still releases
    immediately, so the next scheduled cycle does not wait out the 1920s TTL."""
    run, _gate, _ = guard
    run("cleanlane", func=lambda: None)
    assert env.released == ["cleanlane"]


def test_crawler_error_still_releases_the_claim(guard, env):
    """A crawler that RAISES has no surviving thread — nothing to protect, so
    the claim goes back. Only `timeout` is special."""
    def _boom():
        raise RuntimeError("crawler blew up")

    run, _gate, _ = guard
    run("errorlane", func=_boom)
    assert env.released == ["errorlane"]


def test_claim_ttl_outlives_the_hard_timeout():
    """The INVARIANT the fix leans on. Keeping the claim buys cover only because
    the TTL is longer than the run that overran; set them equal and the claim
    expires at the same instant the guard gives up, and this fix silently buys
    nothing while still looking present."""
    assert cs._CLAIM_TTL_SECONDS > cs.HARD_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# ★ The in-process half: refuse a duplicate, but never wedge.
# ---------------------------------------------------------------------------

def test_second_run_refused_while_abandoned_thread_is_alive(guard, env):
    """The same replica must not start a second copy either. With 3204's widened
    firing window this is not theoretical: the window is now the whole hour, so a
    restart at 05:35 meets a fresh `last_run_hours` and re-fires the hour-5 slot
    while the 05:00 thread is still running."""
    run, _gate, started = guard
    run("intl_infra_ingest")
    assert started == [1], "sanity: the first run must actually have started"

    run("intl_infra_ingest")
    assert started == [1], (
        "a SECOND copy of intl_infra_ingest started while the abandoned thread "
        "was still running")


def test_refusal_is_not_silent(guard, env):
    """A skipped slot that leaves no trace reads healthy-but-stale — it keeps
    reporting its last SUCCESSFUL run. The refusal beats with a non-success
    status so the board shows it."""
    run, _gate, _ = guard
    run("intl_infra_ingest")
    env.beats.clear()
    run("intl_infra_ingest")

    assert len(env.beats) == 1, "the refusal left no trace on the ledger"
    beat = env.beats[0]
    assert beat["status"] == cs._SKIPPED_ABANDONED_STATUS
    assert beat["status"] != "success", "a skipped slot laundered into a success"


def test_beat_status_survives_the_ledger_clamp():
    """routes.ingest_runs treats anything not exactly `success` as overdue, and
    `_beat_status` truncates to _BEAT_STATUS_MAX. A status clipped mid-word
    still reads overdue, but stops being readable — assert it fits whole."""
    assert len(cs._SKIPPED_ABANDONED_STATUS) <= cs._BEAT_STATUS_MAX
    assert cs._beat_status(cs._SKIPPED_ABANDONED_STATUS) == cs._SKIPPED_ABANDONED_STATUS


def test_lane_is_not_wedged_once_the_thread_finishes(guard, env):
    """★ THE ANTI-WEDGE. The registry is reaped, so a lane that overran ONCE is
    refused only while that thread lives — not forever. Without reaping this fix
    would trade a rare double-run for a permanent outage of the lane."""
    run, gate, started = guard
    run("intl_infra_ingest")
    gate.set()
    for t in list(cs._abandoned_runs.values()):
        t.join(timeout=5)

    run("intl_infra_ingest", func=lambda: None)
    assert cs._abandoned_runs == {}, "registry still holds a finished thread"
    assert env.released == ["intl_infra_ingest"], (
        "the lane never ran again after its abandoned thread finished")


def test_other_crawlers_are_not_blocked(guard, env):
    """The registry is per-NAME. A hung intl_infra_ingest must not stop the five
    hour-5 lanes behind it — they were already 31 minutes late."""
    run, _gate, _ = guard
    run("intl_infra_ingest")
    run("peeringdb_network_sync", func=lambda: None)
    assert env.released == ["peeringdb_network_sync"]


def test_active_crawler_is_still_cleared_on_timeout(guard, env):
    """★ DO NOT "FIX" THIS INTO A WEDGE. Clearing `_active_crawler` is what lets
    the duplicate happen, so the tempting tightening is to stop clearing it —
    but it gates EVERY lane, and leaving it set strands the whole replica's
    scheduler on one hung crawl forever. It stays fail-open ON PURPOSE; the
    per-name registry is what stops the duplicate."""
    run, _gate, _ = guard
    run("intl_infra_ingest")
    assert cs._active_crawler is None, (
        "a hung crawler left _active_crawler set — every OTHER lane on this "
        "replica is now blocked permanently")


def test_both_writes_land_together(guard, env):
    """Postcondition of the single-acquisition write. Split across two `with
    _lock:` blocks there is a window where neither mutex is held; the observable
    requirement is that once the finally returns, the crawler is registered as
    abandoned AND `_active_crawler` is clear."""
    run, _gate, _ = guard
    run("intl_infra_ingest")
    assert "intl_infra_ingest" in cs._abandoned_runs
    assert cs._abandoned_runs["intl_infra_ingest"].is_alive()
    assert cs._active_crawler is None


# ---------------------------------------------------------------------------
# The abandoned run must be visible from outside the process.
# ---------------------------------------------------------------------------

def test_status_endpoint_names_the_abandoned_run(guard, env):
    """Before this, a timed-out crawler kept running with NOTHING naming it:
    `active_crawler` was None and the lane read idle."""
    run, _gate, _ = guard
    run("intl_infra_ingest")
    status = cs.get_scheduler_status()
    assert status["abandoned_runs"] == ["intl_infra_ingest"]
    assert status["active_crawler"] is None


def test_manual_trigger_refuses_instead_of_lying(guard, env):
    """`run_crawler_now` checks `_active_crawler`, which is None during an
    abandonment — so without its own check the admin gets "Started ..." (HTTP
    200) and `_run_with_guard` then silently refuses it."""
    run, _gate, _ = guard
    run("intl_infra_ingest")

    ok, message = cs.run_crawler_now("intl_infra_ingest")
    assert ok is False, "admin trigger reported a start it was about to refuse"
    assert "still running" in message
