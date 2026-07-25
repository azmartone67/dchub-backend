"""Platform-Doors master shell (#27) test suite (2026-07-25).

Mocked (no DB, no network, never imports main). Pins the SECURITY + HONESTY
properties, not a live count:
  1. the recipe lane flags a published PRO comp key (critical) and reads clean
     when none is present.
  2. the Grok dead-key recipe is detected.
  3. agent-card honesty: handoff advertised True is flagged; False/absent is fine.
  4. lane verdict is critical-aware ('?' not green when a load-bearing check is
     undetermined).
  5. kill switch -> 404 (never 5xx); admin gate -> 403; empty key never opens.
"""
import importlib.util
import os

import flask
import pytest

_MOD = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "routes",
                                    "platform_doors_master_shell.py"))


def _load():
    spec = importlib.util.spec_from_file_location("platform_doors_master_shell_t", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


shell = _load()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("PLATFORM_DOORS_SHELL_DISABLE", "DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── 1-2 recipe / comp-key security lane ───────────────────────────────

def test_recipe_lane_flags_published_comp_key(monkeypatch):
    def _fake_read(rel):
        if "mcp-config.json" in rel:
            return '{"apiKey": "dchub_copilot_2026_verify"}'
        return None
    monkeypatch.setattr(shell, "_read_static", _fake_read)
    checks = shell._lane_recipes(None)
    exp = [c for c in checks if c["id"] == "rc_compkey_exposure"][0]
    assert exp["pass"] is False
    assert exp["critical"] is True


def test_recipe_lane_clean_when_no_comp_key(monkeypatch):
    monkeypatch.setattr(shell, "_read_static",
                        lambda rel: '{"note":"use claim_free_key, no embedded key"}'
                        if "config" in rel or "README" in rel or "script" in rel else None)
    checks = shell._lane_recipes(None)
    exp = [c for c in checks if c["id"] == "rc_compkey_exposure"][0]
    assert exp["pass"] is True


def test_grok_dead_key_detected(monkeypatch):
    def _fake_read(rel):
        if rel.endswith(os.path.join("grok", "mcp-config.json")):
            return '{"headers": {"X-API-Key": "dchub_grok_2026_verify"}}'
        return None
    monkeypatch.setattr(shell, "_read_static", _fake_read)
    checks = shell._lane_recipes(None)
    grok = [c for c in checks if c["id"] == "rc_grok_live"][0]
    assert grok["pass"] is False


# ── 3 agent-card honesty ──────────────────────────────────────────────

def test_card_flags_false_handoff_claim(monkeypatch):
    monkeypatch.setattr(shell, "_read_static",
                        lambda rel: '    "supports_a2a_handoff": True,' if "agent_a2a" in rel else None)
    checks = shell._lane_card(None)
    assert checks[0]["pass"] is False


def test_card_ok_when_handoff_false(monkeypatch):
    monkeypatch.setattr(shell, "_read_static",
                        lambda rel: '    "supports_a2a_handoff": False,' if "agent_a2a" in rel else None)
    checks = shell._lane_card(None)
    assert checks[0]["pass"] is True


# ── 4 lane verdict ────────────────────────────────────────────────────

def test_undetermined_critical_is_not_green():
    checks = [shell._check("a", "a", True, "ok"),
              shell._check("b", "b", None, "unknown", critical=True)]
    assert shell._lane_verdict(checks) is None


def test_gauge_does_not_fail_a_lane():
    checks = [shell._check("a", "a", True, "ok"), shell._check("g", "g", None, "gauge")]
    assert shell._lane_verdict(checks) is True


# ── 5 gating ──────────────────────────────────────────────────────────

def _app():
    app = flask.Flask(__name__)
    app.register_blueprint(shell.platform_doors_master_shell_bp)
    return app


def test_kill_switch_is_404(monkeypatch):
    monkeypatch.setenv("PLATFORM_DOORS_SHELL_DISABLE", "1")
    with _app().test_client() as cl:
        assert cl.get("/api/v1/admin/platform-doors/master-tick").status_code == 404
        assert cl.get("/admin/platform-doors").status_code == 404


def test_admin_gate(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    with _app().test_client() as cl:
        assert cl.get("/api/v1/admin/platform-doors/master-tick").status_code == 403
        assert cl.get("/api/v1/admin/platform-doors/master-tick",
                      headers={"X-Admin-Key": "no"}).status_code == 403


def test_empty_admin_key_never_opens(monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    with _app().test_client() as cl:
        assert cl.get("/api/v1/admin/platform-doors/master-tick").status_code == 403
