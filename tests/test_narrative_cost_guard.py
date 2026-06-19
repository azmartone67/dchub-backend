"""
tests/test_narrative_cost_guard.py — COST SAFETY for the cron-fired brain
narrative refresh (mirror of tests/test_brain_self_director.py guard tests).

The narrative refresh endpoint is hit by a cron ~4x/day and used to call Claude
with ONLY an admin + ANTHROPIC_API_KEY check — guaranteed model calls, no
opt-out. It now ships DARK behind BRAIN_NARRATIVE_ENABLED (default OFF) plus a
server-side daily cap, both enforced cost-first BEFORE any model call.

THE WHOLE POINT: prove the cost guards short-circuit BEFORE _call_claude. Every
guard test patches _call_claude with a boom-sentinel that RAISES if reached — so
a passing test PROVES zero model calls. No real Anthropic API, no network.
"""
import pytest

nb = pytest.importorskip("routes.brain_narrative")


# A sentinel model-call that BLOWS UP if ever reached — proves the cost guards
# short-circuit BEFORE any model work.
def _boom_call_claude(*a, **k):
    raise AssertionError("_call_claude must NOT be called — a cost guard should "
                         "have short-circuited before any model work")


@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(nb.brain_narrative_bp)
    # No admin key configured -> the admin check is a no-op (we test the COST
    # guards here, not auth). The self-director suite covers admin gating.
    monkeypatch.setattr(nb, "_ADMIN_KEY", "", raising=False)
    return app.test_client()


@pytest.fixture()
def _reset_counter(monkeypatch):
    """Start every test with a clean in-memory daily counter."""
    monkeypatch.setitem(nb._DAILY_COUNTER, "date", None)
    monkeypatch.setitem(nb._DAILY_COUNTER, "count", 0)


# ════════════════════════════════════════════════════════════════════
#  GUARD ORDER — the cost ceiling. Each guard makes ZERO model calls.
# ════════════════════════════════════════════════════════════════════
def test_refresh_flag_off_is_noop_zero_model_calls(client, monkeypatch, _reset_counter):
    """Flag OFF (default) -> skipped:disabled and _call_claude is NEVER called.
    Ships dark — the cron fires but makes ZERO model calls until an operator
    flips BRAIN_NARRATIVE_ENABLED."""
    monkeypatch.setattr(nb, "_enabled", lambda: False)
    monkeypatch.setattr(nb, "_ANTHROPIC_KEY", "sk-present", raising=False)
    # Boom-sentinel: raising if reached proves zero model calls.
    monkeypatch.setattr(nb, "_call_claude", _boom_call_claude)
    # If a guard were skipped we'd still fail fast before network anyway.
    monkeypatch.setattr(nb, "_fetch_findings", lambda: [])
    monkeypatch.setattr(nb, "_recent_commits", lambda days=1: [])

    resp = client.post("/api/v1/brain/narrative/refresh")  # must NOT raise
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["skipped_reason"] == "disabled"


def test_refresh_no_api_key_is_noop_zero_model_calls(client, monkeypatch, _reset_counter):
    """Flag on but no ANTHROPIC_API_KEY -> 503, NO _call_claude call (degrades
    gracefully)."""
    monkeypatch.setattr(nb, "_enabled", lambda: True)
    monkeypatch.setattr(nb, "_ANTHROPIC_KEY", "", raising=False)
    monkeypatch.setattr(nb, "_call_claude", _boom_call_claude)
    monkeypatch.setattr(nb, "_fetch_findings", lambda: [])
    monkeypatch.setattr(nb, "_recent_commits", lambda days=1: [])

    resp = client.post("/api/v1/brain/narrative/refresh")
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


def test_refresh_over_daily_cap_is_noop_zero_model_calls(client, monkeypatch, _reset_counter):
    """At/over the daily cap -> skipped:daily_cap, NO _call_claude call. The cap
    is enforced SERVER-SIDE in the handler, not trusted to the cron schedule."""
    monkeypatch.setattr(nb, "_enabled", lambda: True)
    monkeypatch.setattr(nb, "_ANTHROPIC_KEY", "sk-present", raising=False)
    monkeypatch.setattr(nb, "_daily_cap", lambda: 4)
    monkeypatch.setattr(nb, "_today_count", lambda: 4)  # already at cap
    monkeypatch.setattr(nb, "_call_claude", _boom_call_claude)
    monkeypatch.setattr(nb, "_fetch_findings", lambda: [])
    monkeypatch.setattr(nb, "_recent_commits", lambda days=1: [])

    resp = client.post("/api/v1/brain/narrative/refresh")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["skipped_reason"] == "daily_cap"
    assert data["used_today"] == 4
    assert data["daily_cap"] == 4


def test_refresh_counter_fails_closed_zero_model_calls(client, monkeypatch, _reset_counter):
    """If the daily counter blows up, the handler fails CLOSED (huge used count)
    and skips the model call rather than running uncapped."""
    monkeypatch.setattr(nb, "_enabled", lambda: True)
    monkeypatch.setattr(nb, "_ANTHROPIC_KEY", "sk-present", raising=False)
    monkeypatch.setattr(nb, "_daily_cap", lambda: 4)

    def _boom_count():
        raise RuntimeError("counter exploded")
    monkeypatch.setattr(nb, "_today_count", _boom_count)
    monkeypatch.setattr(nb, "_call_claude", _boom_call_claude)
    monkeypatch.setattr(nb, "_fetch_findings", lambda: [])
    monkeypatch.setattr(nb, "_recent_commits", lambda days=1: [])

    resp = client.post("/api/v1/brain/narrative/refresh")  # must NOT raise
    assert resp.status_code == 200
    assert resp.get_json()["skipped_reason"] == "daily_cap"


# ════════════════════════════════════════════════════════════════════
#  Flag default OFF — the primary control ships dark.
# ════════════════════════════════════════════════════════════════════
def test_flag_defaults_off(monkeypatch):
    """With the env var unset, _enabled() is False — the endpoint ships dark."""
    monkeypatch.delenv("BRAIN_NARRATIVE_ENABLED", raising=False)
    assert nb._enabled() is False


def test_flag_truthy_values(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("BRAIN_NARRATIVE_ENABLED", v)
        assert nb._enabled() is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("BRAIN_NARRATIVE_ENABLED", v)
        assert nb._enabled() is False


# ════════════════════════════════════════════════════════════════════
#  Daily counter — UTC-date keyed, resets across days.
# ════════════════════════════════════════════════════════════════════
def test_daily_counter_increments_and_resets(monkeypatch, _reset_counter):
    monkeypatch.setattr(nb, "_utc_today", lambda: "2026-06-19")
    assert nb._today_count() == 0
    nb._bump_today_count()
    nb._bump_today_count()
    assert nb._today_count() == 2
    # New UTC day -> counter resets to 0 (no carry-over).
    monkeypatch.setattr(nb, "_utc_today", lambda: "2026-06-20")
    assert nb._today_count() == 0


# ════════════════════════════════════════════════════════════════════
#  HAPPY PATH — flag ON keeps the existing behavior intact.
# ════════════════════════════════════════════════════════════════════
def test_refresh_happy_path_calls_claude_once_when_enabled(client, monkeypatch, _reset_counter):
    """Flag on + key + under cap -> the existing single Claude call runs and the
    narrative is returned UNCHANGED (gating WHETHER it runs, not WHAT it does)."""
    monkeypatch.setattr(nb, "_enabled", lambda: True)
    monkeypatch.setattr(nb, "_ANTHROPIC_KEY", "sk-present", raising=False)
    monkeypatch.setattr(nb, "_daily_cap", lambda: 4)
    monkeypatch.setattr(nb, "_fetch_findings", lambda: [])
    monkeypatch.setattr(nb, "_recent_commits", lambda days=1: ["fix: x"])

    calls = {"n": 0}

    def _one(prompt):
        calls["n"] += 1
        return "Everything is calm. Fix the iso_metrics cron. Uptime is up."
    monkeypatch.setattr(nb, "_call_claude", _one)

    resp = client.post("/api/v1/brain/narrative/refresh")
    assert resp.status_code == 200
    data = resp.get_json()
    assert calls["n"] == 1
    assert data["ok"] is True
    assert "iso_metrics" in data["narrative"]
    # The recompute consumed exactly one daily-cap slot.
    assert nb._today_count() == 1
