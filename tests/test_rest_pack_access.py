"""REST /api/v1/facilities honours what /pricing sells (frontend#1534, 2026-09-21).

Before: any key below Pro got a 403 with no rows while a keyless caller got the
5-row preview. That included the trial key auto_issue_key_for_ai_agents injects
for Claude/ChatGPT/Perplexity/Gemini/Cursor user agents, and the Developer
plan /pricing sells as full result sets.

Now: Developer and above get the full list. A valid key below Developer gets
the full list for one $10-pack credit, burned only on a delivered 200, or else
exactly the keyless preview. An unknown key still gets 401.

The route cases run main.py's REAL list_facilities (compiled out of main.py,
which is too heavy to import), behind the REAL require_plan and protect_data.
Only the key lookup, the credit ledger and the two list bodies are stubbed.
The ledger's SQL is pinned against a real Postgres in
tests/test_key_hash_format_sql.py.
"""
import ast
import functools
import pathlib

import pytest
from flask import Flask, jsonify, make_response, request

import api_data_protection
import api_tier_gating
import routes.mcp_conversion_plays as ledger
import util.location_meter as location_meter
from util import rest_pack_access as rpa

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROWS = 40

KEYS = {
    "k-developer": {"plan": "developer"},
    "k-pro": {"plan": "pro"},
    "k-free-pack": {"plan": "free"},
    "k-free-dry": {"plan": "free"},
    "k-trial-injected": {"plan": "identified"},
    "k-free-racing": {"plan": "free"},
}


@pytest.fixture
def app():
    return Flask(__name__)


@pytest.fixture
def world(monkeypatch):
    """Key lookup + credit ledger at their module boundaries, with a call log."""
    credits = {"k-free-pack": 42, "k-free-racing": 1}
    calls = []
    monkeypatch.setattr(api_tier_gating, "validate_api_key", lambda k: KEYS.get(k))
    monkeypatch.delenv("DCHUB_AI_WARS_KEYS", raising=False)

    def pack_active(api_key=None, mcp_session=None):
        calls.append(("pack_active", api_key))
        return credits.get(api_key, 0) > 0

    def consume_credits(api_key, mcp_session_id, count=1):
        calls.append(("burn", api_key, mcp_session_id, count))
        if api_key == "k-free-racing":          # a concurrent call took the last credit
            credits[api_key] = 0
            return {"ok": False, "error": "insufficient_credits", "remaining": 0}
        credits[api_key] -= count
        return {"ok": True, "remaining": credits[api_key], "burned": count}

    monkeypatch.setattr(location_meter, "pack_active", pack_active)
    monkeypatch.setattr(ledger, "consume_credits", consume_credits)
    return {"credits": credits, "calls": calls}


def _full():
    return jsonify({"success": True,
                    "data": [{"id": i, "lat": 38.9, "lng": -77.4} for i in range(ROWS)]})


def _preview():
    return jsonify({"success": True, "tier": "free", "data": [{"id": 0}], "preview": True})


@functools.lru_cache(maxsize=1)
def _list_facilities_code():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "list_facilities"]
    assert len(fns) == 1, "expected exactly one top-level list_facilities in main.py"
    fn = fns[0]
    fn.decorator_list = []                      # drop @app.route; keep the body as written
    return compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec")


def _route():
    ns = {
        "request": request, "jsonify": jsonify,
        "get_ai_wars_key_info": lambda: None,
        "_real_require_plan": api_tier_gating.require_plan,
        "protect_data": api_data_protection.protect_data,
        "_list_facilities_full": _full,
        "_list_facilities_free": _preview,
    }
    exec(_list_facilities_code(), ns)
    return ns["list_facilities"]


def _get(app, key):
    headers = {"X-API-Key": key} if key else {}
    with app.test_request_context("/api/v1/facilities?limit=100", headers=headers):
        resp = make_response(_route()())
        return resp.status_code, resp.get_json(silent=True) or {}, resp.headers


def _burns(world):
    return [c for c in world["calls"] if c[0] == "burn"]


# ── the route ──────────────────────────────────────────────────────────────

def test_a_developer_key_gets_the_full_list_and_spends_nothing(app, world):
    status, body, headers = _get(app, "k-developer")
    assert status == 200 and not body.get("preview")
    assert len(body["data"]) == 25 and body["truncated"] is True   # Developer's per-response cap
    assert headers["X-Plan-Tier"] == "developer"
    assert world["calls"] == []                                    # no ledger read, no burn


def test_a_pro_key_is_unchanged(app, world):
    status, body, headers = _get(app, "k-pro")
    assert status == 200 and len(body["data"]) == ROWS and headers["X-Plan-Tier"] == "pro"
    assert world["calls"] == []


def test_a_free_key_with_pack_credits_gets_the_full_list_for_one_credit(app, world):
    status, body, headers = _get(app, "k-free-pack")
    assert status == 200 and not body.get("preview")
    assert len(body["data"]) == 25 and headers["X-Plan-Tier"] == "pack"
    assert headers["X-DCHub-Access"] == "pack"
    assert headers["X-DCHub-Credits-Remaining"] == "41"
    assert _burns(world) == [("burn", "k-free-pack", None, 1)]


def test_a_free_key_without_credits_gets_exactly_the_keyless_preview(app, world):
    keyed = _get(app, "k-free-dry")
    keyless = _get(app, None)
    assert keyed[0] == keyless[0] == 200
    assert keyed[1] == keyless[1] and keyed[1]["preview"] is True
    assert _burns(world) == []


def test_the_trial_key_injected_for_an_ai_agent_gets_the_preview_not_a_403(app, world):
    status, body, _ = _get(app, "k-trial-injected")
    assert status == 200 and body["preview"] is True


def test_a_burn_lost_to_a_concurrent_call_serves_the_preview_not_the_list(app, world):
    status, body, headers = _get(app, "k-free-racing")
    assert status == 200 and body["preview"] is True
    assert "X-DCHub-Access" not in headers
    assert _burns(world) == [("burn", "k-free-racing", None, 1)]


def test_an_unknown_key_is_still_refused_as_invalid(app, world):
    status, body, _ = _get(app, "k-nobody")
    assert status == 401 and body["error"] == "invalid_api_key"
    assert world["calls"] == []


# ── the helper's own rules ─────────────────────────────────────────────────

@pytest.mark.parametrize("plan,refused", [("free", True), ("identified", True),
                                          ("starter", True), ("developer", False),
                                          ("pro", False)])
def test_only_a_plan_below_the_gate_reads_as_a_plan_refusal(app, monkeypatch, plan, refused):
    """Read off what the REAL require_plan('developer') answers a key on `plan`."""
    monkeypatch.setattr(api_tier_gating, "validate_api_key", lambda k: {"plan": plan})
    with app.test_request_context("/x", headers={"X-API-Key": "k"}):
        resp = api_tier_gating.require_plan("developer")(lambda: ("opened", 200))()
        assert rpa.is_plan_upgrade_refusal(resp) is refused


def test_only_a_403_plan_upgrade_required_is_a_plan_refusal(app):
    with app.test_request_context("/x"):
        assert rpa.is_plan_upgrade_refusal((jsonify(error="plan_upgrade_required"), 403))
        # no credential at all (the AI Wars path reaches the gate without a key)
        assert not rpa.is_plan_upgrade_refusal((jsonify(error="plan_required"), 403))
        assert not rpa.is_plan_upgrade_refusal((jsonify(error="invalid_api_key"), 401))
        assert not rpa.is_plan_upgrade_refusal((jsonify(error="plan_upgrade_required"), 503))
        assert not rpa.is_plan_upgrade_refusal(jsonify(error="plan_upgrade_required"))  # a 200


def test_no_credit_is_burned_for_an_answer_that_is_not_a_200(app, world):
    with app.test_request_context("/x"):
        resp = rpa.serve_below_plan("k-free-pack", lambda: (jsonify(error="daily_limit_reached"), 429),
                                    _preview)
        assert make_response(resp).status_code == 429
    assert _burns(world) == []
    assert world["credits"]["k-free-pack"] == 42


def test_the_full_handler_runs_as_tier_pack(app, world):
    seen = {}

    def full():
        from flask import g
        seen["tier"] = g.user_tier
        return _full()

    with app.test_request_context("/x"):
        rpa.serve_below_plan("k-free-pack", full, _preview)
    assert seen == {"tier": "pack"}


def test_the_pack_rows_carry_developer_s_caps():
    cfg = api_data_protection.PROTECTION_CONFIG
    for table in ("daily_record_caps", "max_results_per_response"):
        assert cfg[table]["pack"] == cfg[table]["developer"], table
