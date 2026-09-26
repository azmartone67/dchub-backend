#!/usr/bin/env python3
"""An MCP key's 'paid' tier resolves, on REST, to the plan the key bought.

Owner decision 2026-09-22 (frontend#1534). mcp_dev_keys.tier only holds free,
paid or enterprise, so the webhook writes 'paid' for Developer and Pro alike,
and every REST resolver read it as Pro: a $49 Developer key opened every
Pro-only REST route. protect_data read the same value as Developer, so a $99
Pro key got Developer's record caps. routes/tier_gate did not know MCP keys at
all, so a Pro key got the free teaser (deal $ and MW masked) on every route
gated through caller_is_privileged. dch_oauth_ keys, which live in the same
table, got a 401 from validate_api_key.

These tests drive the four real resolvers against a real Postgres:
api_tier_gating.validate_api_key (every @require_plan), routes/tier_gate
(caller_is_privileged), util/tier_gate.resolve_tier and protect_data's
_resolve_key_tier. The MCP server's own calls (a valid X-Internal-Key) must
resolve exactly as they did before, so MCP behaviour does not change.

The database tests skip without PACK_EXPIRY_SQL_DSN. The db-parity job in
pre-merge.yml sets it and then FAILS if this file skipped. Everything lives in
its own schema, which every resolver's connection reaches through the DSN's
search_path, and which is dropped afterwards: the public users and
mcp_dev_keys that later steps of that job create or reuse are never touched.
mcp_conversions is created with the columns the resolver reads (the module DDL
carries a foreign key to a table this file does not own); mcp_checkout_payments
comes from its own module's DDL.
"""
import hashlib
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("PACK_EXPIRY_SQL_DSN")
INTERNAL = "test-internal-key-mcp-key-rest-plan"
SCHEMA = "mcp_key_rest_plan"


def _in_schema(dsn):
    """The DSN with this file's schema as the only search_path."""
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-csearch_path%3D{SCHEMA}"

# name: (email, mcp_dev_keys.tier, status)
KEYS = {
    "dch_live_dev_by_users":      ("dev@users.test", "paid", "active"),
    "dch_live_pro_by_users":      ("pro@users.test", "paid", "active"),
    "dch_live_dev_by_conversion": ("dev@conv.test", "paid", "active"),
    "dch_live_dev_canceled_user": ("canceled@users.test", "paid", "active"),
    "dch_live_dev_keybound":      (None, "paid", "active"),
    "dch_live_pro_keybound":      (None, "paid", "active"),
    "dch_live_paid_no_record":    (None, "paid", "active"),
    "dch_live_team_by_users":     ("team@users.test", "paid", "active"),
    "dch_live_enterprise":        ("ent@users.test", "enterprise", "active"),
    "dch_live_free":              (None, "free", "active"),
    "dch_live_revoked":           ("dev@users.test", "paid", "revoked"),
    "dch_oauth_free":             (None, "free", "active"),
    "dch_oauth_dev":              ("oauth-dev@users.test", "paid", "active"),
}

# email: (plan, subscription_status)
USERS = {
    "dev@users.test": ("developer", "active"),
    "pro@users.test": ("pro", "active"),
    "canceled@users.test": ("pro", "canceled"),
    "team@users.test": ("team", "active"),
    "ent@users.test": ("enterprise", "active"),
    "oauth-dev@users.test": ("developer", "active"),
}

# email: plan_to
CONVERSIONS = {
    "dev@conv.test": "developer",
    "canceled@users.test": "developer",
}

# key: monthly amount in cents of its key-bound ('k-' + sha256) subscription
KEYBOUND = {
    "dch_live_dev_keybound": 4900,
    "dch_live_pro_keybound": 9900,
}

# What a direct REST caller must get from validate_api_key
REST_PLAN = {
    "dch_live_dev_by_users": "developer",
    "dch_live_pro_by_users": "pro",
    "dch_live_dev_by_conversion": "developer",
    "dch_live_dev_canceled_user": "developer",   # users says free, so the conversion decides
    "dch_live_dev_keybound": "developer",
    "dch_live_pro_keybound": "pro",
    "dch_live_paid_no_record": "pro",            # nothing on record: the old mapping
    "dch_live_team_by_users": "team",
    "dch_live_enterprise": "enterprise",
    "dch_live_free": "free",
    "dch_oauth_free": "free",
    "dch_oauth_dev": "developer",
}


@pytest.fixture
def db(monkeypatch):
    if not DSN:
        pytest.skip("PACK_EXPIRY_SQL_DSN not set")
    import psycopg2
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    dsn = _in_schema(DSN)
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("NEON_DATABASE_URL", dsn)
    monkeypatch.delenv("DATABASE_READ_URL", raising=False)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT current_schemas(false)")
        assert cur.fetchone()[0] == [SCHEMA], "search_path did not isolate this file"
        cur.execute("""CREATE TABLE users (
                         id SERIAL PRIMARY KEY,
                         email TEXT,
                         plan TEXT,
                         subscription_status TEXT,
                         role TEXT,
                         demoted_at TIMESTAMPTZ)""")
        # Production carries this CHECK: 'paid' is all the column can say.
        cur.execute("""CREATE TABLE mcp_dev_keys (
                         api_key TEXT PRIMARY KEY,
                         developer_id TEXT,
                         email TEXT,
                         tier TEXT CHECK (tier IN ('free', 'paid', 'enterprise')),
                         status TEXT DEFAULT 'active',
                         metadata JSONB DEFAULT '{}'::jsonb,
                         created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE mcp_conversions (
                         id SERIAL PRIMARY KEY,
                         user_email TEXT NOT NULL,
                         plan_to TEXT NOT NULL,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        for email, (plan, status) in USERS.items():
            cur.execute("INSERT INTO users (email, plan, subscription_status) "
                        "VALUES (%s, %s, %s)", (email, plan, status))
        for key, (email, tier, status) in KEYS.items():
            cur.execute("INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status) "
                        "VALUES (%s, %s, %s, %s, %s)", (key, "dev_" + key, email, tier, status))
        for email, plan_to in CONVERSIONS.items():
            cur.execute("INSERT INTO mcp_conversions (user_email, plan_to) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (email, plan_to))
    import routes.checkout_payment_refs as cpr
    # ensure_schema remembers success per process; the table was just dropped.
    monkeypatch.setattr(cpr, "_SCHEMA_READY", [False])
    assert cpr.ensure_schema() is True, "mcp_checkout_payments DDL did not apply"
    with conn.cursor() as cur:
        for key, cents in KEYBOUND.items():
            ref = "k-" + hashlib.sha256(key.encode()).hexdigest()
            cur.execute("INSERT INTO mcp_checkout_payments (stripe_session_id, "
                        "client_reference_id, mode, amount_subtotal, amount_total) "
                        "VALUES (%s, %s, 'subscription', %s, %s)",
                        ("cs_" + key, ref, cents, cents))
    import util.mcp_key_plan as mkp
    import api_data_protection as adp
    mkp._rest_plan_cache.clear()
    adp._KEY_TIER_CACHE.clear()
    # protect_data connects with sslmode='require'; the parity Postgres has no TLS.
    real_connect = psycopg2.connect

    def _no_tls(*a, **kw):
        kw.pop("sslmode", None)
        return real_connect(*a, **kw)
    monkeypatch.setattr(psycopg2, "connect", _no_tls)
    try:
        yield conn
    finally:
        mkp._rest_plan_cache.clear()
        adp._KEY_TIER_CACHE.clear()
        conn.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.close()


@pytest.fixture
def app():
    from flask import Flask
    return Flask(__name__)


def _ctx(app, key, internal=False):
    """A request from a public address. Flask's test default is 127.0.0.1,
    which caller_is_privileged trusts outright and would make every
    assertion below pass."""
    headers = {"X-API-Key": key}
    if internal:
        headers["X-Internal-Key"] = INTERNAL
    return app.test_request_context("/api/v1/deals", headers=headers,
                                    environ_base={"REMOTE_ADDR": "203.0.113.9"})


# ── validate_api_key: every @require_plan ────────────────────────────────────

@pytest.mark.parametrize("key,plan", sorted(REST_PLAN.items()))
def test_validate_api_key_resolves_the_plan_the_key_bought(db, key, plan):
    from api_tier_gating import validate_api_key
    info = validate_api_key(key)
    assert info is not None, f"{key} resolved to no key at all"
    assert info["plan"] == plan


def test_an_oauth_key_is_a_key_on_rest(db):
    """It got None (a 401) before: validate_api_key only knew dch_live_."""
    from api_tier_gating import validate_api_key
    assert validate_api_key("dch_oauth_free") is not None


def test_a_revoked_key_is_still_no_key(db):
    from api_tier_gating import validate_api_key
    assert validate_api_key("dch_live_revoked") is None


def test_the_mcp_servers_call_keeps_the_old_mapping(db, app):
    """server.mjs forwards the user's key next to its X-Internal-Key; MCP
    behaviour must not change, so 'paid' stays Pro for that call."""
    from api_tier_gating import validate_api_key
    with _ctx(app, "dch_live_dev_by_users", internal=True):
        assert validate_api_key("dch_live_dev_by_users")["plan"] == "pro"
    with _ctx(app, "dch_live_dev_by_users"):
        assert validate_api_key("dch_live_dev_by_users")["plan"] == "developer"


def test_a_developer_key_no_longer_opens_a_pro_gate(db):
    from api_tier_gating import validate_api_key, user_has_access
    dev = validate_api_key("dch_live_dev_by_users")["plan"]
    pro = validate_api_key("dch_live_pro_by_users")["plan"]
    assert user_has_access(dev, "developer") and not user_has_access(dev, "pro")
    assert user_has_access(pro, "pro")


# ── routes/tier_gate: caller_is_privileged ──────────────────────────────────

def test_caller_is_privileged_sees_a_pro_mcp_key(db, app):
    """It resolved every MCP key FREE, so a Pro key got masked deal values."""
    from routes.tier_gate import caller_is_privileged, _resolve_caller_tier
    with _ctx(app, "dch_live_pro_by_users"):
        assert _resolve_caller_tier()[0] == "PRO"
        assert caller_is_privileged("PRO") is True
    with _ctx(app, "dch_live_dev_by_users"):
        assert _resolve_caller_tier()[0] == "DEVELOPER"
        assert caller_is_privileged("DEVELOPER") is True
        assert caller_is_privileged("PRO") is False
    with _ctx(app, "dch_oauth_dev"):
        assert _resolve_caller_tier()[0] == "DEVELOPER"
    with _ctx(app, "dch_live_free"):
        assert caller_is_privileged("IDENTIFIED") is False


def test_caller_tier_for_the_mcp_servers_call_resolves_the_key(db, app):
    """2026-09-25: the MCP server's call resolves the user's key with MCP's own
    mapping (paid -> Pro). It read FREE, so routes that compare the tier name
    (site_selection_canvas, deal_autopsy, grid_transition_radar, radar) locked
    a paying key. It stays privileged by its internal key; a free key stays
    FREE; and the REST answer for the same key is not taken from the cache."""
    from routes.tier_gate import caller_is_privileged, _resolve_caller_tier
    with _ctx(app, "dch_live_dev_by_users", internal=True):
        assert _resolve_caller_tier()[0] == "PRO"
        assert caller_is_privileged("PRO") is True
    with _ctx(app, "dch_live_dev_by_users"):
        assert _resolve_caller_tier()[0] == "DEVELOPER"
    with _ctx(app, "dch_live_free", internal=True):
        assert _resolve_caller_tier()[0] in ("FREE", "IDENTIFIED")


# ── util/tier_gate.resolve_tier ─────────────────────────────────────────────

def test_util_tier_gate_resolves_the_plan_the_key_bought(db, app):
    from util.tier_gate import resolve_tier, Tier
    cases = {"dch_live_dev_by_users": Tier.DEVELOPER,
             "dch_live_pro_by_users": Tier.PRO,
             "dch_live_dev_keybound": Tier.DEVELOPER,
             "dch_live_paid_no_record": Tier.PRO,
             "dch_oauth_dev": Tier.DEVELOPER}
    for key, want in cases.items():
        with _ctx(app, key):
            assert resolve_tier()[0] == want, key
    with _ctx(app, "dch_live_dev_by_users", internal=True):
        assert resolve_tier()[0] == Tier.PRO


# ── protect_data's key tier (routes with no @require_plan) ──────────────────

def test_protect_data_caps_follow_the_plan_the_key_bought(db, app):
    """'paid' read as Developer here, so a Pro key got Developer's caps."""
    import api_data_protection as adp
    with _ctx(app, "dch_live_pro_by_users"):
        assert adp._resolve_key_tier("dch_live_pro_by_users") == "pro"
    with _ctx(app, "dch_live_dev_by_users"):
        assert adp._resolve_key_tier("dch_live_dev_by_users") == "developer"
    with _ctx(app, "dch_live_team_by_users"):
        assert adp._resolve_key_tier("dch_live_team_by_users") == "pro"


def test_protect_data_keeps_the_mcp_servers_mapping_and_does_not_share_its_cache(db, app):
    """REST resolves first and caches; the MCP server's call right after must
    still get the old mapping, not the REST answer out of the cache."""
    import api_data_protection as adp
    with _ctx(app, "dch_live_pro_by_users"):
        assert adp._resolve_key_tier("dch_live_pro_by_users") == "pro"
    with _ctx(app, "dch_live_pro_by_users", internal=True):
        assert adp._resolve_key_tier("dch_live_pro_by_users") == "developer"


# ── the price table the key-bound source reads ──────────────────────────────

def test_plan_by_monthly_cents():
    from util.mcp_key_plan import plan_by_monthly_cents
    assert plan_by_monthly_cents(4900) == "developer"
    assert plan_by_monthly_cents(9900) == "pro"
    assert plan_by_monthly_cents(1234) is None
    assert plan_by_monthly_cents(None) is None
