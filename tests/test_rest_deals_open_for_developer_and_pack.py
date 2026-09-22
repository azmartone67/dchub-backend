"""Deal $ values and MW open over REST for Developer and for pack credits
(frontend#1534, "REST honours /pricing", step 2, 2026-09-22).

/pricing sells M&A as "Full + CSV export" at Developer, and the $10 pack as
full-depth credits. Before this change the REST deal routes did not deliver
either:
  /api/deals, /api/v1/deals   $ and MW unmasked only for caller_is_privileged('PRO')
  /api/v1/transactions        require_plan('pro'), then the same Pro mask inside
and the pack opened none of them. Now Developer and above get the full answer,
a valid key below Developer holding pack credits gets it for one credit (burned
only on a delivered 200), and every other caller gets exactly what it got
before. The MCP server's own calls (a valid X-Internal-Key) stay full.

The routes run for real: the deals blueprint behind main.py's own require_plan
stub, pulled out of main.py with ast (the tests/test_keyed_rest_walls_open.py
harness). With no database the routes serve their labelled seed, whose 81 rows
all carry a value, so a masked value is a real mask and not a missing number.
"""
import ast
import base64
import builtins
import copy
import functools
import logging
import os
import pathlib
import sys

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "rest-deals-test-internal-key"
FREE_KEY = "dch_live_" + "f" * 32
PACK_KEY = "dch_live_" + "p" * 32          # free tier, holding pack credits
DEV_KEY = "dch_live_" + "d" * 32
PRO_KEY = "dch_live_" + "r" * 32
DCHUB_DEV_KEY = "dchub_" + "a" * 30
PLANS = {FREE_KEY: "free", PACK_KEY: "free", DEV_KEY: "developer", PRO_KEY: "pro",
         DCHUB_DEV_KEY: "developer"}
PUBLIC_IP = "203.0.113.7"


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")[0]


# ── main.py's require_plan stub, executed from its AST ───────────────────────

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


def _main_stub(real):
    from internal_auth import is_valid_internal_key
    node = _main_function("require_plan")
    ns = {"request": flask.request, "jsonify": flask.jsonify, "os": os, "logging": logging,
          "_early_wraps": functools.wraps, "is_valid_internal_key": is_valid_internal_key,
          "get_ai_wars_key_info": lambda: None, "_real_require_plan": real}
    missing = _free_names(node) - set(ns) - {"require_plan"}
    assert not missing, f"main.py's require_plan now reads {sorted(missing)}; supply it"
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    return ns["require_plan"]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    """Key lookups, pack balances and credit burns, answered from a table."""
    import api_tier_gating
    import routes.mcp_conversion_plays as plays
    import util.location_meter as lm
    import util.mcp_key_plan as mkp
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k} if k in PLANS else None)
    monkeypatch.setattr(lm, "pack_active", lambda api_key=None, **kw: api_key == PACK_KEY)
    import mcp_gatekeeper
    # A dchub_ key resolves from its api_keys row in routes/tier_gate; answer it here.
    monkeypatch.setattr(mcp_gatekeeper, "_resolve_from_db_hash",
                        lambda k: mcp_gatekeeper.Tier.DEVELOPER if k == DCHUB_DEV_KEY else None)
    mcp_gatekeeper._key_store.pop(DCHUB_DEV_KEY, None)
    mkp._rest_plan_cache.clear()
    state = {"burns": [], "burn_ok": True}

    def consume(key, sid, n):
        state["burns"].append((key, n))
        return {"ok": state["burn_ok"], "remaining": 41}
    monkeypatch.setattr(plays, "consume_credits", consume)
    yield state
    mkp._rest_plan_cache.clear()
    mcp_gatekeeper._key_store.pop(DCHUB_DEV_KEY, None)


@pytest.fixture
def deals(ledger, monkeypatch):
    import api_tier_gating
    import routes.deals_routes as dr
    tiers = []

    def protect(f):
        @functools.wraps(f)
        def wrapper(*a, **k):
            tiers.append(getattr(flask.g, "user_tier", None))
            return f(*a, **k)
        return wrapper
    monkeypatch.setattr(dr, "_require_plan", _main_stub(api_tier_gating.require_plan))
    monkeypatch.setattr(dr, "_protect_data", protect)
    monkeypatch.setattr(dr, "_get_ai_wars_key_info", lambda: None)
    app = flask.Flask("rest-deals")
    app.register_blueprint(dr.deals_bp)
    c = app.test_client()
    # Not loopback: caller_is_privileged trusts 127.0.0.1 outright.
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c, tiers


def _get(client, path, key=None, internal=False):
    h = {"User-Agent": "node", "CF-Connecting-IP": PUBLIC_IP}
    if key:
        h["X-API-Key"] = key
    if internal:
        h["X-Internal-Key"] = SECRET
    return client.get(path, headers=h)


def _full(body):
    rows = body["transactions"]
    return bool(rows) and all(r.get("value") is not None for r in rows) and body["tier"] == "paid"


def _masked(body):
    rows = body["transactions"]
    return (bool(rows) and len(rows) <= 3 and body["tier"] == "free"
            and all(r.get("value") is None and r.get("mw") is None for r in rows))


ALIASES = ["/api/deals", "/api/v1/deals"]


# ── /api/deals and /api/v1/deals ─────────────────────────────────────────────

@pytest.mark.parametrize("path", ALIASES)
@pytest.mark.parametrize("key", [DEV_KEY, PRO_KEY, DCHUB_DEV_KEY])
def test_developer_and_above_get_deal_values(deals, ledger, path, key):
    c, _ = deals
    r = _get(c, path, key)
    assert r.status_code == 200 and _full(r.get_json()), key
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", ALIASES)
def test_a_pack_key_gets_deal_values_for_one_credit(deals, ledger, path):
    c, tiers = deals
    r = _get(c, path, PACK_KEY)
    assert r.status_code == 200 and _full(r.get_json())
    assert r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]
    assert tiers == ["pack"]                       # protect_data ran as the pack


@pytest.mark.parametrize("path", ALIASES)
def test_a_pack_that_cannot_burn_gets_the_masked_answer(deals, ledger, path):
    """serve_below_plan falls back to the preview with g.user_tier still
    'pack'; the preview must be masked anyway."""
    c, _ = deals
    ledger["burn_ok"] = False
    r = _get(c, path, PACK_KEY)
    assert r.status_code == 200 and _masked(r.get_json())
    assert "X-DCHub-Access" not in r.headers


@pytest.mark.parametrize("path", ALIASES)
@pytest.mark.parametrize("key", [None, FREE_KEY, "dch_live_" + "u" * 32])
def test_everyone_else_gets_what_they_got_before(deals, ledger, path, key):
    c, _ = deals
    r = _get(c, path, key)
    body = r.get_json()
    assert r.status_code == 200 and _masked(body)
    assert ledger["burns"] == []
    assert _plan_of(body["upgrade_url"]) == "metered"      # the pack leads the wall
    opts = {o["plan"]: o["opens"] for o in body["upgrade_options"]}
    assert opts.get("developer") == "rest"


@pytest.mark.parametrize("path", ALIASES)
def test_the_mcp_servers_call_stays_full(deals, ledger, path):
    c, _ = deals
    r = _get(c, path, FREE_KEY, internal=True)
    assert r.status_code == 200 and _full(r.get_json())
    assert ledger["burns"] == []


# ── /api/v1/transactions ─────────────────────────────────────────────────────

TX = "/api/v1/transactions"


@pytest.mark.parametrize("key", [DEV_KEY, PRO_KEY, DCHUB_DEV_KEY])
def test_transactions_open_at_developer(deals, ledger, key):
    c, _ = deals
    r = _get(c, TX, key)
    assert r.status_code == 200 and _full(r.get_json()), key
    assert ledger["burns"] == []


def test_transactions_open_for_a_pack_key_at_one_credit(deals, ledger):
    c, tiers = deals
    r = _get(c, TX, PACK_KEY)
    assert r.status_code == 200 and _full(r.get_json())
    assert r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]
    assert tiers == ["pack"]                       # protect_data ran once, as the pack


def _teaser(body):
    rows = body["transactions"]
    return (body["tier"] == "free" and len(rows) <= 3
            and all("value" not in row and "mw" not in row for row in rows))


@pytest.mark.parametrize("key", [None, FREE_KEY])
def test_transactions_below_developer_get_the_teaser_as_before(deals, ledger, key):
    c, _ = deals
    r = _get(c, TX, key)
    body = r.get_json()
    assert r.status_code == 200 and _teaser(body)
    assert ledger["burns"] == []
    assert _plan_of(body["upgrade_url"]) == "metered"


def test_transactions_pack_that_cannot_burn_gets_the_teaser(deals, ledger):
    c, _ = deals
    ledger["burn_ok"] = False
    r = _get(c, TX, PACK_KEY)
    assert r.status_code == 200 and _teaser(r.get_json())
