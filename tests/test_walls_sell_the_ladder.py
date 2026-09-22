"""REST walls sell the ladder /pricing sells, and mint no key a wall cannot open.

NO NETWORK, NO DB.

Measured keyless on production, 2026-09-22 ~03:30Z:

  /api/v1/transactions/export.csv   402  stripe_alternates: starter_monthly_9,
                                         pro_monthly_199, pro_monthly_299;
                                         stripe_checkout = the Starter link;
                                         auto_trial_key + X-Trial-Key minted,
                                         "Retry ... and this call will succeed"
  /api/v1/lp/export.csv|geojson,
  /api/v1/bots/whales|dormant       402  the same stripe_alternates map
  /api/v1/carbon                    402  the same map, and "200 calls/day"
                                         beside auto_trial_daily_calls 15
  /api/v1/capacity/heatmap/public   403  recommended_upgrade_tier "starter",
                                         plans.enterprise at a monthly price
  401/403/429 hint (_upgrade_hint)       starter_url and a Starter rung

The ladder is the $10 pack, Developer and Pro; every price is read from the
registries. A trial key resolves IDENTIFIED, so it is minted only for a gate it
opens. Every test drives the code that serves the response, not a copy of it.
"""
import ast
import base64
import json
import pathlib
import re
import sys

import pytest

flask = pytest.importorskip("flask")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "walls-ladder-test-key-not-a-secret"

# The retired rungs and prices. "starter" as a substring so a starter_url key
# is caught as well as the word.
_RETIRED = re.compile(r"\$\s?9(?![\d.,])|\$\s?199\b|\$\s?299\b|\$\s?699\b|starter|founding",
                      re.IGNORECASE)


def _retired_in(obj):
    return sorted(set(m.group(0).lower() for m in _RETIRED.finditer(json.dumps(obj))))


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")[0]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)   # /go/c links are signed with it


@pytest.fixture
def mints(monkeypatch):
    """Count trial-key mints instead of writing one."""
    import routes.auto_trial as auto_trial
    calls = []

    def _fake(req=None, tool_name="", *a, **k):
        calls.append(tool_name)
        return {"ok": True, "api_key": "dch_trial_TESTKEY", "expires_at": "2026-10-21T00:00:00Z",
                "daily_calls": 15}
    monkeypatch.setattr(auto_trial, "mint_trial_for_request", _fake)
    return calls


def _wall(current, required, gate="t_gate"):
    from routes import tier_gate
    with flask.Flask("walls-ladder").test_request_context("/api/v1/x"):
        resp, status = tier_gate._gate_response(current, required, gate, {"n": 1})
        return status, resp.get_json(), dict(resp.headers)


# ── routes.tier_gate._gate_response ──────────────────────────────────────

@pytest.mark.parametrize("required,plan", [("DEVELOPER", "developer"), ("PRO", "pro")])
def test_a_paid_wall_offers_only_the_plan_its_gate_admits(mints, required, plan):
    status, body, headers = _wall("FREE", required)
    assert status == 402
    assert not _retired_in(body), _retired_in(body)
    assert "stripe_alternates" not in body
    assert _plan_of(body["upgrade_url"]) == plan
    assert _plan_of(body["stripe_checkout"]) == plan
    assert [o["plan"] for o in body["upgrade_options"]] == [plan]
    assert body["upgrade_options"][0]["opens"] == "rest"
    assert body["claim_free_key_first"] is None
    assert "keys/claim" not in headers.get("WWW-Authenticate", "")


@pytest.mark.parametrize("required", ["DEVELOPER", "PRO"])
def test_a_wall_a_trial_key_cannot_open_mints_none(mints, required):
    status, body, headers = _wall("FREE", required)
    assert mints == [], "a %s wall minted a trial key it cannot open" % required
    assert "auto_trial_key" not in body
    assert "X-Trial-Key" not in headers and "Retry-After" not in headers


def test_an_identified_wall_still_mints_the_key_that_opens_it(mints):
    status, body, headers = _wall("FREE", "IDENTIFIED")
    assert status == 402 and mints == ["t_gate"]
    assert body["auto_trial_key"] == "dch_trial_TESTKEY"
    assert headers.get("X-Trial-Key") == "dch_trial_TESTKEY"
    assert "no-store" in headers.get("Cache-Control", "")
    # The allowance is the minted row's own: this said 200 beside a 15.
    assert "15 calls/day" in body["message"] and "200" not in body["message"]
    assert not _retired_in(body), _retired_in(body)


def test_the_export_route_serves_the_wall_without_a_trial_key(mints, monkeypatch):
    """/api/v1/transactions/export.csv, through its own view function."""
    from routes import tier_gate
    import routes.transactions_browser as tb
    monkeypatch.setattr(tier_gate, "_resolve_caller_tier", lambda: ("FREE", {}))
    monkeypatch.setattr(tb, "_fetch_deals", lambda **k: ([], 0))
    with flask.Flask("walls-ladder").test_request_context("/api/v1/transactions/export.csv"):
        resp, status = tb.transactions_export_csv()
        body = resp.get_json()
    assert status == 402 and mints == []
    assert "auto_trial_key" not in body and "X-Trial-Key" not in resp.headers
    assert _plan_of(body["upgrade_url"]) == "developer"
    assert not _retired_in(body), _retired_in(body)


def test_the_export_route_still_serves_a_developer(monkeypatch):
    from routes import tier_gate
    import routes.transactions_browser as tb
    monkeypatch.setattr(tier_gate, "_resolve_caller_tier", lambda: ("DEVELOPER", {}))
    monkeypatch.setattr(tb, "_fetch_deals", lambda **k: ([{"id": "d1", "buyer": "B"}], 1))
    with flask.Flask("walls-ladder").test_request_context("/api/v1/transactions/export.csv"):
        resp, status = tb.transactions_export_csv()
    assert status == 200 and resp.mimetype == "text/csv"
    assert resp.get_data(as_text=True).splitlines()[1].startswith("d1,")


# ── main.py: the AI-agent auto-issue hook skips bulk exports ─────────────

def _hook():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    fns = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)
           and n.name in ("_is_bulk_export_path", "auto_issue_key_for_ai_agents")]
    assert any(f.name == "auto_issue_key_for_ai_agents" for f in fns)
    ns = {"request": flask.request, "g": flask.g,
          "_identify_ai_platform": lambda ua: "ChatGPT"}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "main.py", "exec"), ns)
    return ns["auto_issue_key_for_ai_agents"]


@pytest.mark.parametrize("path", ["/api/v1/transactions/export.csv", "/api/v1/lp/export.geojson",
                                  "/api/v1/mcp/tools/export_facility_csv",
                                  "/api/v1/data/dcpi-history.csv"])
def test_the_ai_agent_hook_mints_nothing_on_an_export(mints, path):
    hook = _hook()
    with flask.Flask("walls-ladder").test_request_context(
            path, headers={"User-Agent": "Mozilla/5.0; compatible; ChatGPT-User/1.0"}):
        hook()
        assert flask.request.environ.get("HTTP_X_API_KEY") is None
    assert mints == [], "the auto-issue hook minted a trial key for %s" % path


def test_the_ai_agent_hook_still_mints_elsewhere(mints):
    """Control: without it the export test above could pass on a hook that
    never mints at all."""
    hook = _hook()
    with flask.Flask("walls-ladder").test_request_context(
            "/api/v1/stats", headers={"User-Agent": "Mozilla/5.0; compatible; ChatGPT-User/1.0"}):
        hook()
        assert flask.request.environ.get("HTTP_X_API_KEY") == "dch_trial_TESTKEY"
    assert len(mints) == 1


# ── the 401/403/429 _upgrade_hint ────────────────────────────────────────

def _enriched(monkeypatch, status, variant):
    import routes.paywall_hint_middleware as m
    monkeypatch.setattr(m, "_log_ab_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "_personal_hit_pitch", lambda *a, **k: "")
    monkeypatch.setattr(m, "_safe_caller_id", lambda: ("203.0.113.9", "t", "h"))
    monkeypatch.setattr(m, "_pick_variant", lambda ip, ua: variant)
    app = flask.Flask("walls-ladder")
    m.register_paywall_hint_middleware(app)
    enrich = app.after_request_funcs[None][-1]
    with app.test_request_context("/api/v1/some/gated/path"):
        resp = flask.jsonify({"error": "nope"})
        resp.status_code = status
        return json.loads(enrich(resp).get_data(as_text=True))["_upgrade_hint"]


@pytest.mark.parametrize("variant", ["A", "B", "C", "D"])
@pytest.mark.parametrize("status", [401, 403, 429])
def test_the_hint_names_no_retired_rung(monkeypatch, status, variant):
    hint = _enriched(monkeypatch, status, variant)
    assert not _retired_in(hint), (variant, status, _retired_in(hint))
    assert _plan_of(hint["pack_url"]) == "metered"


def test_the_hint_sells_the_ladder_at_registry_prices(monkeypatch):
    import tier_registry
    hint = _enriched(monkeypatch, 403, "A")
    for plan in ("developer", "pro"):
        assert tier_registry.price_display(plan, "") in hint["pricing_quick"], plan
        assert tier_registry.price_display(plan) in hint["what_you_get"], plan
    assert "API credits" in hint["what_you_get"] and "API credits" in hint["pricing_quick"]


# ── api_tier_gating._rich_gate_response (require_plan walls) ─────────────

def _rich(monkeypatch, min_plan):
    import mcp_signal_canonical
    import routes.pair_code as pair_code
    import api_tier_gating
    import utils.paywall_response as pr
    # Production configures the Developer link (DCHUB_STRIPE_DEVELOPER_LINK);
    # without it the builder's one-click and recommended fields never render.
    monkeypatch.setattr(pr, "STRIPE_DEVELOPER_LINK", "https://buy.stripe.com/test_developer")
    monkeypatch.setattr(pair_code, "get_or_create_code",
                        lambda *a, **k: {"code": "DCM-TEST", "expires_at": None})
    monkeypatch.setattr(mcp_signal_canonical, "_compute_caller_id", lambda **k: "anon:test")
    with flask.Flask("walls-ladder").test_request_context(
            "/api/v1/capacity/heatmap/public", headers={"User-Agent": "t"}):
        return api_tier_gating._rich_gate_response(path="/api/v1/capacity/heatmap/public",
                                                   min_plan=min_plan)


@pytest.mark.parametrize("min_plan", ["developer", "pro"])
def test_a_require_plan_wall_recommends_the_plan_it_requires(monkeypatch, min_plan):
    body = _rich(monkeypatch, min_plan)
    assert not _retired_in(body), _retired_in(body)
    assert body["recommended_upgrade_tier"] == min_plan
    assert _plan_of(body["recommended_upgrade_url"]) == min_plan
    assert _plan_of(body["upgrade_url"]) == min_plan
    assert all(o["opens"] == "rest" for o in body["upgrade_options"])


@pytest.mark.parametrize("min_plan", ["identified", "developer", "pro"])
def test_no_require_plan_wall_names_a_retired_rung(monkeypatch, min_plan):
    """Separately from the override above: an identified wall keeps the
    builder's own fields, so this is what guards the builder."""
    body = _rich(monkeypatch, min_plan)
    assert not _retired_in(body), (min_plan, _retired_in(body))


def test_a_pro_wall_carries_no_developer_offer(monkeypatch):
    body = _rich(monkeypatch, "pro")
    assert _plan_of(body["one_click_upgrade_url"]) == "pro"
    assert "tier=developer" not in json.dumps(body)


def test_the_gate_plans_quote_registry_prices(monkeypatch):
    import api_tier_gating
    import tier_registry
    plans = api_tier_gating._build_gate_plans()
    assert tier_registry.price_display("pro") in plans["pro"]
    assert not _retired_in(plans), _retired_in(plans)
