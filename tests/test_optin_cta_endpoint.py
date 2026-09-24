"""GET /api/v1/opt-in/cta — the paywall opt-in card for one keyed caller.

The Node MCP server (dchub-mcp-server#519) builds the same `optin_cta` card as
mcp_gatekeeper._optin_cta_block but cannot read the suppression list or resolve
a key's tier, so it skips every keyed caller. This endpoint answers for one
key with the gatekeeper's own card or null.

Guards: internal-key gated; the key is read from the X-API-Key header only;
the card is byte-for-byte _optin_cta_block's; null for flag off, a non-FREE
tier (a valid trial key is IDENTIFIED), an off-set tool, a suppressed email,
or any error; never cacheable; never returns the email; never writes.
"""
import flask
import pytest

import mcp_gatekeeper as g
import routes.marketing_opt_in as moi

INTERNAL = "test-internal-key"
KEY = "dchub_free_abc123"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL)
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    state = {"tier": g.Tier.FREE, "suppressed": False, "seen": []}

    def tier(k):
        state["seen"].append(("tier", k))
        return state["tier"]

    def suppressed(k):
        state["seen"].append(("suppressed", k))
        if isinstance(state["suppressed"], Exception):
            raise state["suppressed"]
        return state["suppressed"]

    monkeypatch.setattr(g, "resolve_tier", tier)
    monkeypatch.setattr(g, "_optin_recipient_suppressed", suppressed)
    monkeypatch.setattr(moi, "request_opt_in",
                        lambda *a, **k: pytest.fail("the cta read must never request an opt-in"))
    app = flask.Flask("optin-cta-endpoint-test")
    app.register_blueprint(moi.marketing_opt_in_bp)
    return {"client": app.test_client(), "state": state}


def _get(env, tool="get_grid_intelligence", key=KEY, internal=INTERNAL, **kw):
    h = {}
    if internal:
        h["X-Internal-Key"] = internal
    if key:
        h["X-API-Key"] = key
    return env["client"].get("/api/v1/opt-in/cta", query_string={"tool": tool, **kw}, headers=h)


def test_free_unsuppressed_key_gets_the_gatekeepers_own_card(env):
    r = _get(env)
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["reason"] == "ok"
    assert j["optin_cta"] == g._optin_cta_block("get_grid_intelligence", g.Tier.FREE, KEY)
    assert j["optin_cta"]["optin_url"] == (
        "https://dchub.cloud/api/v1/opt-in/request?source=paywall_optin_cta&tool=get_grid_intelligence")
    assert ("suppressed", KEY) in env["state"]["seen"]


def test_never_cacheable(env):
    for r in (_get(env), _get(env, internal=None), _get(env, tool="BAD TOOL")):
        assert "no-store" in r.headers.get("Cache-Control", "")


@pytest.mark.parametrize("internal", [None, "", "wrong"])
def test_internal_key_required(env, internal):
    r = _get(env, internal=internal)
    assert r.status_code == 403
    assert "optin_cta" not in r.get_json()
    assert env["state"]["seen"] == []


def test_the_key_is_read_from_the_header_only(env):
    r = env["client"].get("/api/v1/opt-in/cta",
                          query_string={"tool": "get_grid_intelligence", "api_key": KEY, "key": KEY},
                          headers={"X-Internal-Key": INTERNAL})
    assert r.get_json()["reason"] == "ok"
    assert ("tier", None) in env["state"]["seen"]
    assert all(k != KEY for _, k in env["state"]["seen"])


@pytest.mark.parametrize("tier", [g.Tier.IDENTIFIED, g.Tier.STARTER, g.Tier.DEVELOPER, g.Tier.PRO, g.Tier.ENTERPRISE])
def test_non_free_tier_gets_no_card(env, tier):
    env["state"]["tier"] = tier
    j = _get(env).get_json()
    assert j["optin_cta"] is None and j["reason"] == "tier"


def test_suppressed_email_gets_no_card(env):
    env["state"]["suppressed"] = True
    j = _get(env).get_json()
    assert j["optin_cta"] is None and j["reason"] == "suppressed"


def test_any_error_answers_no_card(env):
    env["state"]["suppressed"] = RuntimeError("db down")
    r = _get(env)
    assert r.status_code == 200
    j = r.get_json()
    assert j["optin_cta"] is None and j["ok"] is False


@pytest.mark.parametrize("flag", ["", "1", "false", "yes"])
def test_flag_off_gets_no_card(env, monkeypatch, flag):
    monkeypatch.setenv("OPTIN_CTA_ENABLED", flag)
    j = _get(env).get_json()
    assert j["optin_cta"] is None and j["reason"] == "flag_off"


def test_off_set_tool_gets_no_card(env):
    j = _get(env, tool="rank_markets").get_json()
    assert j["optin_cta"] is None and j["reason"] == "tool"


@pytest.mark.parametrize("tool", ["", "BAD TOOL", "a" * 65, "x;drop"])
def test_bad_tool_is_refused(env, tool):
    assert _get(env, tool=tool).status_code == 400


def test_answer_never_carries_the_key_or_an_email(env):
    body = _get(env).get_data(as_text=True)
    assert KEY not in body and "@" not in body
