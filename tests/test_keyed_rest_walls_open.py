"""The keyed operations of the public spec open for what their walls sell
(frontend#1534, "REST honours /pricing", 2026-09-21).

WHY. /openapi.json marks four operations keyed. A hosted catalogue generates
tools from it, so their 403 bodies reach its customers' agents verbatim.
Measured on origin/main before this change:

  operation                  gate over REST            its wall sold
  /api/grid/fuel-mix         require_plan('pro')       Starter $9, Developer $49, a free key
  /api/energy/prices/<st>    require_plan('pro')       Starter $9, Developer $49, a free key
  /api/v1/pipeline           require_plan('identified') Starter $9, Developer $49, a free key
  /api/site-score            dchub_ keys on Developer+ Developer $49

None of the free-key offers opened anything; on fuel-mix and energy prices
neither did Starter or Developer; the $10 pack, which /pricing sells as
full-depth credits and which opens the MCP tools behind all three, opened
none of them; and site-score refused every dch_live_ key, which is what a
self-serve Developer buyer holds. A buyer paid and stayed locked.

Now: fuel-mix and energy prices open at Developer, pipeline stays at its
identified gate, and a valid key below the gate with pack credits gets the
answer for one credit, burned only on a delivered 200. Every refusal answers
with a wall that offers only the pack and Developer, both of which open it.
site-score resolves dch_live_ keys. No key and a free key are refused as
before: the depth gates are not loosened for them.

The routes run for real: fuel-mix and energy prices registered by their own
modules, pipeline through the deals blueprint behind main.py's require_plan
stub, site-score as main.py's own handler, pulled out with ast and executed
(as tests/test_rate_limit_partner_egress.py does for the limiter).
"""
import ast
import base64
import builtins
import copy
import functools
import json
import logging
import os
import pathlib
import sys

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "keyed-walls-test-internal-key"
FREE_KEY = "dch_live_" + "f" * 32
PACK_KEY = "dch_live_" + "p" * 32          # free tier, holding $10-pack credits
DEV_KEY = "dch_live_" + "d" * 32
PRO_KEY = "dch_live_" + "k" * 32           # a k- Developer is stored 'paid' -> pro
STARTER_KEY = "dch_live_" + "s" * 32
TRIAL_KEY = "dch_trial_" + "t" * 32        # auto-trial keys resolve to identified
DCHUB_DEV_KEY = "dchub_" + "a" * 30
UNKNOWN_KEY = "dch_live_" + "u" * 32
PLANS = {FREE_KEY: "free", PACK_KEY: "free", DEV_KEY: "developer", PRO_KEY: "pro",
         STARTER_KEY: "starter", TRIAL_KEY: "identified", DCHUB_DEV_KEY: "developer"}
PARTNER_IP = "104.248.242.235"
UNDECLARED_IP = "203.0.113.7"


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")


@pytest.fixture
def ledger(monkeypatch):
    """Key lookups, pack balances and credit burns, answered from a table."""
    import api_tier_gating
    import routes.mcp_conversion_plays as plays
    import routes.partner_attribution as pa
    import util.location_meter as lm
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k} if k in PLANS else None)
    monkeypatch.setattr(lm, "pack_active", lambda api_key=None, **kw: api_key == PACK_KEY)
    state = {"burns": [], "burn_ok": True}

    def consume(key, sid, n):
        state["burns"].append((key, n))
        return {"ok": state["burn_ok"], "remaining": 41}
    monkeypatch.setattr(plays, "consume_credits", consume)
    pa._drain()
    yield state
    pa._drain()


# ── fuel-mix and energy prices: their own modules' routes ───────────────────

@pytest.fixture
def client(ledger, monkeypatch):
    import enhancements.iso_integrations as iso
    import enhancements.site_scoring as ss
    monkeypatch.setattr(iso.ISOService, "get_fuel_mix",
                        lambda self, name: {"iso": name, "mix": {"gas": 0.41}})
    monkeypatch.setattr(ss.EnergyPricingService, "get_state_electricity_prices",
                        lambda self, st: {"state": st, "industrial_cents_kwh": 7.1})
    app = flask.Flask("keyed-walls")
    iso.register_iso_routes(app)
    ss.register_scoring_routes(app)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c


DEV_ROUTES = ["/api/grid/fuel-mix?iso=ERCOT", "/api/energy/prices/TX"]


def _get(client, path, key=None, ip=UNDECLARED_IP):
    h = {"User-Agent": "node", "CF-Connecting-IP": ip}
    if key:
        h["X-API-Key"] = key
    return client.get(path, headers=h)


def _assert_honest_wall(r, error, required_plan):
    assert r.status_code == 403, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error"] == error and body["required_plan"] == required_plan
    # what the wall OFFERS; current_plan is the caller's own plan, reported back
    text = json.dumps({k: v for k, v in body.items() if k != "current_plan"}).lower()
    for gone in ("starter", "$9", "keys/claim", "recommended_upgrade", "agent_claim"):
        assert gone not in text, f"the wall still offers {gone!r}: {text[:300]}"
    opts = body["upgrade_options"]
    assert [(o["plan"], o["opens"]) for o in opts] == [("pack", "rest"), ("developer", "rest")]
    assert _plan_of(body["upgrade_url"])[0] == "metered"          # the pack leads
    assert [_plan_of(o["url"])[0] for o in opts] == ["metered", "developer"]
    assert "private" in r.headers.get("Cache-Control", "")
    return body


@pytest.mark.parametrize("path", DEV_ROUTES)
@pytest.mark.parametrize("key,plan", [(DEV_KEY, "developer"), (PRO_KEY, "pro"),
                                      (DCHUB_DEV_KEY, "developer")])
def test_developer_and_above_open_it(client, ledger, path, key, plan):
    r = _get(client, path, key)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert ledger["burns"] == []                      # a plan that opens it pays nothing


@pytest.mark.parametrize("path", DEV_ROUTES)
def test_a_pack_key_opens_it_for_one_credit(client, ledger, path):
    r = _get(client, path, PACK_KEY)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]


@pytest.mark.parametrize("path", DEV_ROUTES)
def test_a_pack_that_cannot_burn_gets_the_wall_not_the_data(client, ledger, path):
    ledger["burn_ok"] = False
    _assert_honest_wall(_get(client, path, PACK_KEY), "plan_upgrade_required", "developer")


@pytest.mark.parametrize("path", DEV_ROUTES)
@pytest.mark.parametrize("key", [FREE_KEY, STARTER_KEY, TRIAL_KEY])
def test_a_key_below_developer_without_credits_gets_the_honest_wall(client, ledger, path, key):
    body = _assert_honest_wall(_get(client, path, key), "plan_upgrade_required", "developer")
    assert body["current_plan"] == PLANS[key]
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", DEV_ROUTES)
def test_an_answer_that_fails_burns_no_credit(client, ledger, path, monkeypatch):
    import enhancements.iso_integrations as iso
    import enhancements.site_scoring as ss

    def down(*a, **k):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(iso.ISOService, "get_fuel_mix", down)
    monkeypatch.setattr(ss.EnergyPricingService, "get_state_electricity_prices", down)
    assert _get(client, path, PACK_KEY).status_code >= 500
    assert ledger["burns"] == []


def test_a_signed_in_user_below_the_plan_gets_the_wall_not_an_error(client, monkeypatch):
    """A JWT caller has no API key; the refusal must still be the wall."""
    import api_tier_gating
    monkeypatch.setattr(api_tier_gating, "_get_decode_jwt",
                        lambda: (lambda tok: {"user_id": "u-1", "email": "u@example.com"}))
    monkeypatch.setattr(api_tier_gating, "get_user_plan", lambda **k: "free")
    r = client.get(DEV_ROUTES[0], headers={"Authorization": "Bearer header.payload.sig",
                                           "CF-Connecting-IP": UNDECLARED_IP})
    body = _assert_honest_wall(r, "plan_upgrade_required", "developer")
    assert body["current_plan"] == "free"


@pytest.mark.parametrize("path", DEV_ROUTES)
def test_no_key_is_still_refused(client, ledger, path):
    _assert_honest_wall(_get(client, path), "plan_required", "developer")


@pytest.mark.parametrize("path", DEV_ROUTES)
def test_an_unknown_key_is_still_a_401(client, path):
    assert _get(client, path, UNKNOWN_KEY).status_code == 401


def test_a_partner_wall_carries_a_ref_recorded_to_the_partner(client):
    import routes.partner_attribution as pa
    body = _get(client, DEV_ROUTES[0], ip=PARTNER_IP).get_json()
    refs = {_plan_of(u)[1] for u in [body["upgrade_url"]] + [o["url"] for o in body["upgrade_options"]]}
    assert len(refs) == 1
    ref = refs.pop()
    assert ref.startswith("a-") and len(ref) == 26
    with pa._BUF_LOCK:
        assert pa._BUF[ref][:3] == ["anythingmcp/", "go_c", "/api/grid/fuel-mix"]


def test_every_other_wall_is_caller_independent(client):
    import routes.partner_attribution as pa
    body = _get(client, DEV_ROUTES[1]).get_json()
    assert {_plan_of(u)[1] for u in [body["upgrade_url"]] +
            [o["url"] for o in body["upgrade_options"]]} == {""}
    assert pa.pending() == 0


# ── pipeline: the deals blueprint, behind main.py's own require_plan stub ────

@functools.lru_cache(maxsize=1)
def _main_tree():
    return ast.parse((ROOT / "main.py").read_text())


def _main_function(name):
    found = [n for n in _main_tree().body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, (name, len(found))
    node = copy.deepcopy(found[0])
    node.decorator_list = []
    return node


def _free_names(node):
    loads, bound = set(), set()
    for x in ast.walk(node):
        if isinstance(x, ast.Name):
            (loads if isinstance(x.ctx, ast.Load) else bound).add(x.id)
        elif isinstance(x, ast.FunctionDef):
            bound.add(x.name)
        elif isinstance(x, ast.arg):
            bound.add(x.arg)
        elif isinstance(x, ast.alias):
            bound.add((x.asname or x.name).split(".")[0])
        elif isinstance(x, ast.ExceptHandler) and x.name:
            bound.add(x.name)
    return loads - bound - set(dir(builtins))


def _exec_main(names, ns):
    nodes = [_main_function(n) for n in names]
    missing = set().union(*(_free_names(n) for n in nodes)) - set(ns) - set(names)
    assert not missing, f"main.py now reads {sorted(missing)}; supply it"
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    return ns


def _main_stub(real):
    from internal_auth import is_valid_internal_key
    return _exec_main(["require_plan"], {
        "request": flask.request, "jsonify": flask.jsonify, "os": os, "logging": logging,
        "_early_wraps": functools.wraps, "is_valid_internal_key": is_valid_internal_key,
        "get_ai_wars_key_info": lambda: None, "_real_require_plan": real,
    })["require_plan"]


def test_the_main_stub_hands_the_option_to_the_real_gate():
    seen = []

    def real(min_plan, **opts):
        seen.append((min_plan, opts))
        return lambda f: f
    stub = _main_stub(real)
    with flask.Flask(__name__).test_request_context("/api/v1/pipeline"):
        assert stub("identified", pack_opens=True)(lambda: "ran")() == "ran"
        assert stub("pro")(lambda: "ran")() == "ran"
    assert seen == [("identified", {"pack_opens": True}), ("pro", {})]


@pytest.fixture
def pipeline(ledger, monkeypatch):
    import api_tier_gating
    import routes.deals_routes as dr
    tiers = []

    def protect(f):
        @functools.wraps(f)
        def wrapper(*a, **k):
            tiers.append(getattr(flask.g, "user_tier", None))
            return f(*a, **k)
        return wrapper

    def no_db():
        raise RuntimeError("no database in this test")
    monkeypatch.setattr(dr, "_require_plan", _main_stub(api_tier_gating.require_plan))
    monkeypatch.setattr(dr, "_protect_data", protect)
    monkeypatch.setattr(dr, "_get_db", no_db)
    app = flask.Flask("keyed-walls-pipeline")
    app.register_blueprint(dr.deals_bp)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c, tiers


@pytest.mark.parametrize("key", [TRIAL_KEY, STARTER_KEY, DEV_KEY, PRO_KEY])
def test_pipeline_opens_at_its_gate_as_before(pipeline, ledger, key):
    c, _ = pipeline
    r = _get(c, "/api/v1/pipeline", key)
    assert r.status_code == 200 and r.get_json()["success"] is True
    assert ledger["burns"] == []


def test_pipeline_opens_for_a_pack_key_at_one_credit_under_pack_caps(pipeline, ledger):
    c, tiers = pipeline
    r = _get(c, "/api/v1/pipeline", PACK_KEY)
    assert r.status_code == 200 and r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]
    assert tiers == ["pack"]                        # protect_data ran as the pack


@pytest.mark.parametrize("key,error", [(FREE_KEY, "plan_upgrade_required"),
                                       (None, "plan_required")])
def test_pipeline_refuses_a_free_key_and_no_key_with_the_honest_wall(pipeline, ledger, key, error):
    c, _ = pipeline
    _assert_honest_wall(_get(c, "/api/v1/pipeline", key), error, "identified")
    assert ledger["burns"] == []


# ── site-score: main.py's own handler ────────────────────────────────────────

class _GatePassed(Exception):
    """Raised by the database the handler reaches only once its gate opened."""


def _site_score(ledger_ok=True):
    from internal_auth import is_valid_internal_key

    def reached_the_data():
        raise _GatePassed("gate-passed")
    ns = _exec_main(["_honest_rest_wall", "api_site_score"], {
        "request": flask.request, "jsonify": flask.jsonify,
        "is_valid_internal_key": is_valid_internal_key,
        "get_read_db": reached_the_data, "logger": logging.getLogger("t"),
    })
    return ns["api_site_score"]


def _score(key=None, ip=UNDECLARED_IP):
    h = {"CF-Connecting-IP": ip}
    if key:
        h["X-API-Key"] = key
    handler = _site_score()
    with flask.Flask(__name__).test_request_context(
            "/api/site-score?lat=32.78&lon=-96.8", headers=h):
        out = handler()
        resp, status = (out if isinstance(out, tuple) else (out, out.status_code))
        return status, resp.get_json()


@pytest.mark.parametrize("key", [DEV_KEY, PRO_KEY, DCHUB_DEV_KEY])
def test_site_score_opens_for_a_developer_or_pro_key_of_any_shape(ledger, key):
    status, body = _score(key)
    assert (status, body["error"]) == (500, "gate-passed"), body


@pytest.mark.parametrize("key", [None, FREE_KEY, PACK_KEY, STARTER_KEY, TRIAL_KEY, UNKNOWN_KEY])
def test_site_score_still_refuses_below_developer(ledger, key):
    status, body = _score(key)
    assert status == 403 and body["error"] == "plan_required"
    assert _plan_of(body["upgrade_url"])[0] == "developer"
    assert [(o["plan"], o["opens"]) for o in body["upgrade_options"]] == [("developer", "rest")]


def test_site_score_partner_wall_links_share_one_partner_ref(ledger):
    _, body = _score(ip=PARTNER_IP)
    refs = {_plan_of(u)[1] for u in [body["upgrade_url"]] +
            [o["url"] for o in body["upgrade_options"]]}
    assert len(refs) == 1 and refs.pop().startswith("a-")


# ── the 4xx hint middleware leaves these walls as they are ──────────────────

def test_the_hint_middleware_does_not_add_starter_back(client, monkeypatch):
    from routes import paywall_hint_middleware as phm
    events = []
    monkeypatch.setattr(phm, "_log_ab_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(phm, "_personal_hit_pitch", lambda *a, **k: "")
    phm.register_paywall_hint_middleware(client.application)
    for path in DEV_ROUTES:
        body = _assert_honest_wall(_get(client, path, FREE_KEY),
                                   "plan_upgrade_required", "developer")
        assert "_upgrade_hint" not in body
    assert events == []


def test_the_hint_middleware_still_enriches_other_403s(client, monkeypatch):
    from routes import paywall_hint_middleware as phm
    monkeypatch.setattr(phm, "_log_ab_event", lambda *a, **k: None)
    monkeypatch.setattr(phm, "_personal_hit_pitch", lambda *a, **k: "")
    app = client.application
    app.add_url_rule("/api/test-other-403", "other403",
                     lambda: (flask.jsonify({"error": "plan_required"}), 403))
    phm.register_paywall_hint_middleware(app)
    assert "_upgrade_hint" in _get(client, "/api/test-other-403").get_json()


# ── the spec says what opens each one ────────────────────────────────────────

KEYED = {"/api/v1/pipeline": ("a trial key or any paid plan", True),
         "/api/site-score": ("Developer plan or above", False),
         "/api/grid/fuel-mix": ("Developer plan or above", True),
         "/api/energy/prices/{state}": ("Developer plan or above", True)}


@pytest.mark.parametrize("path", sorted(KEYED))
def test_the_spec_names_what_opens_each_keyed_operation(path):
    from tests.test_curated_openapi_contract import _op, spec as _spec_fixture
    spec = _spec_fixture.__wrapped__()
    op = _op(spec, path)
    desc = op["description"]
    names, pack = KEYED[path]
    assert names in desc
    assert ("pack credits" in desc) is pack
    assert "free key from /api/v1/keys/claim does not open it" in desc
    assert "$" not in desc
    assert op["security"] == [{"apiKey": []}]
