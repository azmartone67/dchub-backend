"""
tests/test_causal_cost_guard.py — Brain L14 causal/analyze COST SAFETY (2026-06-19).

L14's POST/GET /api/v1/brain/causal/analyze is cron-fired ~4x/day and, with
ANTHROPIC_API_KEY set, fired a GUARANTEED Claude call every time with NO
off-switch and NO daily cap. This locks in the fix that mirrors
routes/brain_self_director.py: the endpoint now ships DARK behind a default-OFF
flag (BRAIN_CAUSAL_ENABLED) + a server-side daily cap, with the guards enforced
COST-FIRST so a dark / keyless / capped call makes ZERO model calls.

THE LOAD-BEARING TEST: with the flag OFF the handler must short-circuit and make
ZERO model calls — proven by a boom-sentinel patched over BOTH the model-call
function (_call_claude) AND threading.Thread (the background worker that would
make the call), so neither can fire without raising.
"""
import os

import pytest

cz = pytest.importorskip("routes.brain_layer14_causal")
flask = pytest.importorskip("flask")


def _boom_call_claude(*a, **k):
    raise AssertionError(
        "_call_claude() must NOT be reached — the cost guard should have "
        "short-circuited before any model work")


class _BoomThread:
    """Stand-in for threading.Thread that explodes if the background model
    worker is ever spawned — proves flag-off never even starts the call."""
    def __init__(self, *a, **k):
        raise AssertionError(
            "background analyze thread must NOT be spawned when the cost guard "
            "short-circuits (flag off / no key / capped)")


@pytest.fixture()
def client(monkeypatch):
    app = flask.Flask(__name__)
    app.register_blueprint(cz.brain_layer14_bp)
    # Admin gate off for the test (no admin key configured) so we exercise the
    # cost guards, not auth.
    monkeypatch.setattr(cz, "_ADMIN_KEY", "")
    return app.test_client()


@pytest.fixture()
def _no_model(monkeypatch):
    """Patch BOTH the model-call function and the background-thread constructor
    with boom-sentinels: if EITHER is reached the test fails. This is how we
    prove a guarded call makes ZERO model calls."""
    monkeypatch.setattr(cz, "_call_claude", _boom_call_claude)
    # The handler does `import threading as _th` locally, so patch the stdlib
    # symbol the local import resolves to.
    import threading
    monkeypatch.setattr(threading, "Thread", _BoomThread)


# ════════════════════════════════════════════════════════════════════
#  THE COST CEILING — each guard makes ZERO model calls
# ════════════════════════════════════════════════════════════════════
def test_analyze_flag_off_is_noop_zero_model_calls(client, monkeypatch, _no_model):
    """Flag OFF (default) -> handler short-circuits with skipped:disabled and
    NEVER reaches _call_claude or spawns the background thread (ships dark — a
    cron hit is a no-op that makes ZERO model calls)."""
    monkeypatch.setattr(cz, "_enabled", lambda: False)
    # Even with a key present, flag-off wins (flag is the FIRST guard).
    monkeypatch.setattr(cz, "_ANTHROPIC_KEY", "sk-present")

    resp = client.post("/api/v1/brain/causal/analyze")  # must NOT raise
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["skipped"] is True
    assert data["skipped_reason"] == "disabled"


def test_analyze_default_flag_is_off(monkeypatch):
    """The flag DEFAULTS OFF — with no env var set, _enabled() is False so the
    endpoint ships dark until an operator flips it."""
    monkeypatch.delenv("BRAIN_CAUSAL_ENABLED", raising=False)
    assert cz._enabled() is False


def test_analyze_no_api_key_is_noop_zero_model_calls(client, monkeypatch, _no_model):
    """Flag ON but no ANTHROPIC_API_KEY -> 503, NO model call (degrades
    gracefully). The no-key guard runs before any model work."""
    monkeypatch.setattr(cz, "_enabled", lambda: True)
    monkeypatch.setattr(cz, "_ANTHROPIC_KEY", "")

    resp = client.post("/api/v1/brain/causal/analyze")  # must NOT raise
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


def test_analyze_over_daily_cap_is_noop_zero_model_calls(client, monkeypatch, _no_model):
    """At/over the daily cap -> skipped:daily_cap, NO model call. The cap is
    enforced SERVER-SIDE in the handler (not trusted to the cron schedule)."""
    monkeypatch.setattr(cz, "_enabled", lambda: True)
    monkeypatch.setattr(cz, "_ANTHROPIC_KEY", "sk-present")
    monkeypatch.setattr(cz, "_daily_cap", lambda: 4)
    monkeypatch.setattr(cz, "_today_count", lambda: 4)   # already at cap

    resp = client.post("/api/v1/brain/causal/analyze")  # must NOT raise
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["skipped"] is True
    assert data["skipped_reason"] == "daily_cap"
    assert data["used_today"] == 4
    assert data["daily_cap"] == 4


def test_today_count_fails_closed_on_error(monkeypatch):
    """A broken counter must fail CLOSED — return a huge number so the loop never
    runs uncapped when something is off."""
    monkeypatch.setattr(cz, "_utc_today",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cz._today_count() >= 10 ** 9


# ════════════════════════════════════════════════════════════════════
#  HAPPY PATH — flag ON + key + under cap spawns the analysis ONCE,
#  behavior UNCHANGED, and one daily slot is consumed.
# ════════════════════════════════════════════════════════════════════
def test_analyze_enabled_spawns_once_and_consumes_a_slot(client, monkeypatch):
    """Flag ON + key + under cap -> the existing 202 fire-and-forget behavior is
    preserved (the model call is spawned exactly once) and the daily counter is
    bumped so the cap can bind on the next cron tick."""
    monkeypatch.setattr(cz, "_enabled", lambda: True)
    monkeypatch.setattr(cz, "_ANTHROPIC_KEY", "sk-present")
    monkeypatch.setattr(cz, "_daily_cap", lambda: 4)
    monkeypatch.setattr(cz, "_today_count", lambda: 0)

    spawned = {"n": 0}

    class _CountingThread:
        def __init__(self, *a, **k):
            spawned["n"] += 1

        def start(self):
            pass  # do NOT actually run the model call in the test

    import threading
    monkeypatch.setattr(threading, "Thread", _CountingThread)

    bumped = {"n": 0}
    monkeypatch.setattr(cz, "_bump_today_count",
                        lambda: bumped.__setitem__("n", bumped["n"] + 1))

    resp = client.post("/api/v1/brain/causal/analyze")
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["ok"] is True
    assert data["accepted"] is True
    assert spawned["n"] == 1          # behavior unchanged: spawned exactly once
    assert bumped["n"] == 1           # one daily slot consumed
