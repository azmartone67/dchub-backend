"""
tests/test_orchestrator_cost_guard.py — Brain L8 Orchestrator COST GUARD.

The orchestrator's POST /api/v1/brain/orchestrator/refresh is fired by a ~4x/day
cron and, with ANTHROPIC_API_KEY set, would otherwise call Claude on EVERY hit
with no off-switch and no daily cap. This mirrors the brain_self_director
EXEMPLAR: the refresh now ships DARK behind BRAIN_ORCHESTRATOR_ENABLED (default
OFF) plus a SERVER-SIDE daily cap (BRAIN_ORCHESTRATOR_DAILY_CAP), both enforced
BEFORE any model call in cost-first guard order.

THE WHOLE POINT is the SAFETY of a cron-fired LLM loop. Covers:
  · flag OFF -> ZERO model calls (asserted via a boom-sentinel on _call_claude
    AND on the whole _gather_context/_bg_refresh path that would precede it);
  · no api key -> 503, ZERO model calls;
  · over the daily cap -> skipped, ZERO model calls;
  · the L20 durability fallback now FAILS CLOSED (429) instead of silently
    proceeding unguarded when the guard import is unavailable;
  · the in-memory daily counter fails CLOSED (huge number) on error;
  · happy path (flag ON + key + under cap + L20 ok) DOES dispatch one refresh —
    proving the flag only gates WHETHER it runs, not WHAT it does.

NO real Anthropic API, NO network: _call_claude is replaced everywhere with a
boom-sentinel that raises if a model call is ever reached.
"""
import pytest

orch = pytest.importorskip("routes.brain_layer8_orchestrator")


# A sentinel Claude call that BLOWS UP if it is ever reached — used to PROVE the
# cost guards short-circuit BEFORE any model work. We patch it on every test so
# even an accidental background dispatch would surface.
def _boom_call_claude(*a, **k):
    raise AssertionError("_call_claude() must NOT be reached — the cost guard "
                         "should have short-circuited before any model work")


@pytest.fixture()
def client(monkeypatch):
    """Flask test client with the orchestrator blueprint and admin disabled
    (no DCHUB_ADMIN_KEY set in the module -> POST passes the admin gate)."""
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(orch.brain_layer8_bp)
    # The admin gate compares against module-level _ADMIN_KEY; with no key set
    # the gate is open. Force it open so we isolate the COST guards under test.
    monkeypatch.setattr(orch, "_ADMIN_KEY", "")
    return app.test_client()


@pytest.fixture(autouse=True)
def _boom_everywhere(monkeypatch):
    """Wire the boom-sentinel onto the model call AND the context gather it
    depends on, so ANY path toward a model call fails loudly. Tests that WANT a
    dispatch override these locally."""
    monkeypatch.setattr(orch, "_call_claude", _boom_call_claude)

    def _boom_gather(*a, **k):
        raise AssertionError("_gather_context() must NOT run when the refresh "
                             "is short-circuited before any model work")
    monkeypatch.setattr(orch, "_gather_context", _boom_gather)
    # Always provide an api key by default; flag-off / cap tests must still make
    # ZERO calls even WITH a key present.
    monkeypatch.setattr(orch, "_ANTHROPIC_KEY", "sk-test-key")


# ════════════════════════════════════════════════════════════════════
#  GUARD 1 — flag OFF ships DARK: ZERO model calls (the headline test)
# ════════════════════════════════════════════════════════════════════
def test_refresh_flag_off_is_noop_zero_model_calls(client, monkeypatch):
    """BRAIN_ORCHESTRATOR_ENABLED OFF -> 200 {ran:false, skipped:disabled} and
    NEITHER _gather_context NOR _call_claude is ever reached (boom-sentinels
    would raise). The endpoint ships DARK — a cron hit is a NO-OP with ZERO
    model calls. The handler must NOT raise."""
    monkeypatch.setattr(orch, "_enabled", lambda: False)

    resp = client.post("/api/v1/brain/orchestrator/refresh")  # must NOT raise

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["ran"] is False
    assert data["skipped_reason"] == "disabled"


def test_refresh_flag_off_even_with_default_env(monkeypatch):
    """With NO env var set at all, _enabled() is OFF by default (ships dark)."""
    monkeypatch.delenv("BRAIN_ORCHESTRATOR_ENABLED", raising=False)
    assert orch._enabled() is False
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("BRAIN_ORCHESTRATOR_ENABLED", v)
        assert orch._enabled() is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("BRAIN_ORCHESTRATOR_ENABLED", v)
        assert orch._enabled() is False


# ════════════════════════════════════════════════════════════════════
#  GUARD 2 — no api key: ZERO model calls
# ════════════════════════════════════════════════════════════════════
def test_refresh_no_api_key_zero_model_calls(client, monkeypatch):
    monkeypatch.setattr(orch, "_enabled", lambda: True)
    monkeypatch.setattr(orch, "_ANTHROPIC_KEY", "")

    resp = client.post("/api/v1/brain/orchestrator/refresh")  # must NOT raise

    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


# ════════════════════════════════════════════════════════════════════
#  GUARD 3 — daily cap: ZERO model calls
# ════════════════════════════════════════════════════════════════════
def test_refresh_over_daily_cap_zero_model_calls(client, monkeypatch):
    monkeypatch.setattr(orch, "_enabled", lambda: True)
    monkeypatch.setattr(orch, "_daily_cap", lambda: 4)
    monkeypatch.setattr(orch, "_today_count", lambda: 4)  # already at cap

    resp = client.post("/api/v1/brain/orchestrator/refresh")  # must NOT raise

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ran"] is False
    assert data["skipped_reason"] == "daily_cap"
    assert data["used_today"] == 4
    assert data["daily_cap"] == 4


def test_today_count_fails_closed_on_error(monkeypatch):
    """A broken in-memory counter must fail CLOSED (huge number) so the cap
    holds and the loop never runs uncapped."""
    def _boom_today():
        raise RuntimeError("clock blew up")
    monkeypatch.setattr(orch, "_utc_today", _boom_today)
    assert orch._today_count() >= 10 ** 9


# ════════════════════════════════════════════════════════════════════
#  GUARD 4 — L20 durability now FAILS CLOSED (was silently unguarded)
# ════════════════════════════════════════════════════════════════════
def test_refresh_l20_unavailable_fails_closed_429(client, monkeypatch):
    """When the L20 durability guard import is unavailable, the refresh now
    FAILS CLOSED (429) instead of silently proceeding with no-op lambdas — so a
    missing guard can never let an uncapped Claude call through. ZERO model
    calls (boom-sentinels prove _gather_context/_call_claude never run)."""
    monkeypatch.setattr(orch, "_enabled", lambda: True)
    monkeypatch.setattr(orch, "_daily_cap", lambda: 4)
    monkeypatch.setattr(orch, "_today_count", lambda: 0)

    # Force the late `from routes.brain_layer20_durability import ...` to fail.
    import builtins
    real_import = builtins.__import__

    def _blocked_import(name, *a, **k):
        if name == "routes.brain_layer20_durability":
            raise ImportError("simulated: L20 durability module unavailable")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    resp = client.post("/api/v1/brain/orchestrator/refresh")  # must NOT raise

    assert resp.status_code == 429
    data = resp.get_json()
    assert data["ok"] is False
    assert data["throttled"] is True
    assert data["reason"] == "durability_guard_unavailable"


# ════════════════════════════════════════════════════════════════════
#  HAPPY PATH — flag ON + key + under cap + L20 ok -> ONE dispatch
#  (proves the flag only gates WHETHER it runs, not WHAT it does)
# ════════════════════════════════════════════════════════════════════
def test_refresh_happy_path_dispatches_when_flag_on(client, monkeypatch):
    """Flag ON + key + under cap + L20 permits -> 202 accepted and the daily
    counter is bumped. The background thread is patched so no real Claude call
    fires, but reaching 202 (not a guard skip) proves the endpoint's behavior is
    UNCHANGED when the operator opts in — the flag only gates WHETHER it runs."""
    monkeypatch.setattr(orch, "_enabled", lambda: True)
    monkeypatch.setattr(orch, "_daily_cap", lambda: 4)

    # Independent counter we control so the test is deterministic.
    state = {"count": 0}
    monkeypatch.setattr(orch, "_today_count", lambda: state["count"])
    monkeypatch.setattr(orch, "_bump_today_count",
                        lambda: state.__setitem__("count", state["count"] + 1))

    # L20 permits the call.
    import sys
    import types as _types
    fake_l20 = _types.ModuleType("routes.brain_layer20_durability")
    fake_l20.can_start_claude_call = lambda layer: (True, "ok")
    fake_l20.register_claude_call_start = lambda layer: "call-1"
    fake_l20.register_claude_call_end = lambda call_id: None
    monkeypatch.setitem(sys.modules, "routes.brain_layer20_durability", fake_l20)

    # Stop the background thread from actually doing anything (no real model
    # work in a unit test). Patching Thread.start to a no-op means _bg_refresh —
    # and the boom _gather_context/_call_claude inside it — never run.
    import threading
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    resp = client.post("/api/v1/brain/orchestrator/refresh")

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["ok"] is True
    assert data["accepted"] is True
    # The committed model dispatch was counted against today's cap up front.
    assert state["count"] == 1


# ════════════════════════════════════════════════════════════════════
#  Helpers — the cap math + flag default
# ════════════════════════════════════════════════════════════════════
def test_daily_cap_default_is_four(monkeypatch):
    monkeypatch.delenv("BRAIN_ORCHESTRATOR_DAILY_CAP", raising=False)
    assert orch._daily_cap() == 4
    monkeypatch.setenv("BRAIN_ORCHESTRATOR_DAILY_CAP", "10")
    assert orch._daily_cap() == 10
    monkeypatch.setenv("BRAIN_ORCHESTRATOR_DAILY_CAP", "garbage")
    assert orch._daily_cap() == 4  # bad value -> safe default
    monkeypatch.setenv("BRAIN_ORCHESTRATOR_DAILY_CAP", "0")
    assert orch._daily_cap() == 0  # operator can hard-disable via cap=0


def test_counter_rolls_on_new_utc_day(monkeypatch):
    """The in-memory counter is UTC-date keyed: a new day resets the window."""
    monkeypatch.setattr(orch, "_utc_today", lambda: "2026-06-19")
    orch._DAILY["date"] = None
    orch._DAILY["count"] = 0
    orch._bump_today_count()
    orch._bump_today_count()
    assert orch._today_count() == 2
    # Advance to the next UTC day -> counter resets to 0.
    monkeypatch.setattr(orch, "_utc_today", lambda: "2026-06-20")
    assert orch._today_count() == 0
