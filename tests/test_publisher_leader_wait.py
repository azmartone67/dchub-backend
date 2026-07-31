"""Publisher leadership-race fix (r-leaderwait) — 2026-07-31 deploy-storm freeze.

Five consecutive LinkedIn fires went silent on 2026-07-31: on every Railway
zero-downtime rollover the NEW worker's first leadership check landed while
the OLD worker still held the session advisory lock (SIGTERM ~60-90s after
the new one boots), so each publisher loop skipped into its long non-leader
sleep — LinkedIn sleep(1800), X/Bluesky a bare `continue` into the next 6h
cadence sleep. With deploys landing every ~3min, every worker died before it
ever rechecked, and the only log line was DEBUG, so nothing published all
evening and nothing surfaced. _wait_for_publish_leadership now parks on a
120s recheck (an in-memory main._LEADERSHIP read — no DB cost) and logs INFO
once per non-leader stretch + once on acquisition.

DB-free and pure per house style: monkeypatches cp._is_publish_leader and
time.sleep; the loop-level tests capture each loop's thread target via a fake
threading.Thread and break out with a BaseException, which the loops' broad
`except Exception` guards must NOT swallow. Never imports main (pre-merge CI
has no DB/JWT_SECRET).
"""
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402


class _Breaker(BaseException):
    """Escapes the publisher loops' `except Exception` guards."""


class _SleepRecorder:
    def __init__(self, break_on_call=None, break_when_ge=None):
        self.calls = []
        self.break_on_call = break_on_call
        self.break_when_ge = break_when_ge

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.break_on_call is not None and len(self.calls) >= self.break_on_call:
            raise _Breaker()
        if self.break_when_ge is not None and seconds >= self.break_when_ge:
            raise _Breaker()


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Hard guarantee the suite never touches a DB, even when the dev shell
    has DATABASE_URL set — the loops' credential branches try _db_conn()."""
    def _raise(*a, **k):
        raise RuntimeError("DB forbidden in pure tests")
    monkeypatch.setattr(cp, "_db_conn", _raise)
    monkeypatch.setattr(cp, "_get_db", _raise, raising=False)


def _leader_seq(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr(cp, "_is_publish_leader", lambda: next(it))


def _cp_records(caplog):
    return [r for r in caplog.records if r.name == cp.logger.name]


# ── the wait helper itself ──────────────────────────────────────────

def test_wait_noop_when_already_leader(monkeypatch, caplog):
    monkeypatch.setattr(cp, "_is_publish_leader", lambda: True)
    rec = _SleepRecorder()
    monkeypatch.setattr(cp.time, "sleep", rec)
    with caplog.at_level(logging.DEBUG, logger=cp.logger.name):
        cp._wait_for_publish_leadership("linkedin")
    assert rec.calls == []          # a leader replica pays zero latency
    assert _cp_records(caplog) == []  # and emits zero log noise


def test_wait_rechecks_on_short_cadence_until_leader(monkeypatch):
    _leader_seq(monkeypatch, [False, False, False, True])
    rec = _SleepRecorder()
    monkeypatch.setattr(cp.time, "sleep", rec)
    cp._wait_for_publish_leadership("linkedin")
    # Never the old 1800s/6h — the recheck must beat the ~3min deploy cadence.
    assert rec.calls == [cp._NONLEADER_RECHECK_SECONDS] * 3
    assert cp._NONLEADER_RECHECK_SECONDS == 120


def test_info_once_per_nonleader_stretch_then_debug(monkeypatch, caplog):
    _leader_seq(monkeypatch, [False, False, False, True])
    monkeypatch.setattr(cp.time, "sleep", _SleepRecorder())
    with caplog.at_level(logging.DEBUG, logger=cp.logger.name):
        cp._wait_for_publish_leadership("twitter")
    records = _cp_records(caplog)
    infos = [r for r in records if r.levelno == logging.INFO]
    debugs = [r for r in records if r.levelno == logging.DEBUG]
    not_leader = [r for r in infos if "not leader" in r.getMessage()]
    assert len(not_leader) == 1              # once per stretch, not per tick
    assert "twitter" in not_leader[0].getMessage()
    assert "120" in not_leader[0].getMessage()  # says when it will retry
    assert len(debugs) == 2                  # ticks 2..3 stay quiet
    acquired = [r for r in infos if "acquired" in r.getMessage()]
    assert len(acquired) == 1                # recovery is observable, once


def test_new_stretch_logs_info_again(monkeypatch, caplog):
    """Each call spans one stretch; losing leadership later must re-fire the
    INFO line (state is per-wait, not process-global)."""
    monkeypatch.setattr(cp.time, "sleep", _SleepRecorder())
    with caplog.at_level(logging.INFO, logger=cp.logger.name):
        _leader_seq(monkeypatch, [False, True])
        cp._wait_for_publish_leadership("bluesky")
        _leader_seq(monkeypatch, [False, True])
        cp._wait_for_publish_leadership("bluesky")
    not_leader = [r for r in _cp_records(caplog)
                  if r.levelno == logging.INFO and "not leader" in r.getMessage()]
    assert len(not_leader) == 2


def test_sleep_exception_does_not_kill_the_wait(monkeypatch):
    _leader_seq(monkeypatch, [False, True])

    def _boom(_seconds):
        raise RuntimeError("sleep broke")

    monkeypatch.setattr(cp.time, "sleep", _boom)
    cp._wait_for_publish_leadership("bluesky")  # must return, not raise


# ── the three loops actually engage the wait ────────────────────────

def _capture_loop(monkeypatch, starter, running_flag):
    captured = []

    class _FakeThread:
        def __init__(self, target=None, **kwargs):
            captured.append(target)

        def start(self):
            pass

    monkeypatch.setattr(cp.threading, "Thread", _FakeThread)
    monkeypatch.setattr(cp, running_flag, False)
    starter()
    assert len(captured) == 1 and captured[0] is not None
    return captured[0]


def test_linkedin_loop_uses_short_nonleader_cadence(monkeypatch, caplog):
    loop = _capture_loop(monkeypatch, cp.start_auto_publisher,
                         "_auto_publisher_running")
    _leader_seq(monkeypatch, [False, True])
    rec = _SleepRecorder(break_on_call=2)
    monkeypatch.setattr(cp.time, "sleep", rec)
    with caplog.at_level(logging.DEBUG, logger=cp.logger.name):
        with pytest.raises(_Breaker):
            loop()
    # Call 1 = the non-leader recheck (120, not the old 1800); call 2 = the
    # leader path's first-fire sleep — proof the loop UNBLOCKED on the flip.
    assert rec.calls == [120, 120]
    not_leader = [r for r in _cp_records(caplog)
                  if r.levelno == logging.INFO and "not leader" in r.getMessage()]
    assert len(not_leader) == 1


def test_twitter_loop_holds_due_fire_until_leadership(monkeypatch, caplog):
    monkeypatch.setenv("TWITTER_PUBLISHER_ENABLED", "true")
    for var in ("TWITTER_BEARER_TOKEN", "TWITTER_API_KEY", "TWITTER_API_SECRET",
                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"):
        monkeypatch.delenv(var, raising=False)
    loop = _capture_loop(monkeypatch, cp.start_twitter_publisher,
                         "_twitter_publisher_running")
    monkeypatch.setattr(cp, "run_publisher_deadman_check", lambda *a, **k: None)
    _leader_seq(monkeypatch, [False, False, True])
    rec = _SleepRecorder(break_when_ge=3600)
    monkeypatch.setattr(cp.time, "sleep", rec)
    with caplog.at_level(logging.DEBUG, logger=cp.logger.name):
        with pytest.raises(_Breaker):
            loop()
    # Cadence sleep (150 first-fire) lands BEFORE the leader gate; the due
    # fire then HOLDS on 120s rechecks instead of skipping a full 6h out.
    # The 6h sleep (the breaker) is only reached on the NEXT iteration.
    assert rec.calls == [150, 120, 120, 6 * 3600]
    not_leader = [r for r in _cp_records(caplog)
                  if r.levelno == logging.INFO and "not leader" in r.getMessage()]
    assert len(not_leader) == 1


def test_bluesky_loop_holds_due_fire_until_leadership(monkeypatch, caplog):
    for var in ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    loop = _capture_loop(monkeypatch, cp.start_bluesky_publisher,
                         "_bluesky_publisher_running")
    monkeypatch.setattr(cp, "run_publisher_deadman_check", lambda *a, **k: None)
    _leader_seq(monkeypatch, [False, True])
    rec = _SleepRecorder(break_when_ge=3600)
    monkeypatch.setattr(cp.time, "sleep", rec)
    with caplog.at_level(logging.DEBUG, logger=cp.logger.name):
        with pytest.raises(_Breaker):
            loop()
    assert rec.calls == [180, 120, 6 * 3600]
    not_leader = [r for r in _cp_records(caplog)
                  if r.levelno == logging.INFO and "not leader" in r.getMessage()]
    assert len(not_leader) == 1
