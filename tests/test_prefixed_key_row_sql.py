"""Plan-named keys against a REAL Postgres: only a stored, active key resolves.

Skips without TIER_GATES_SQL_DSN. The db-parity job in pre-merge.yml sets it
and then FAILS if this file skipped.

tests/test_prefixed_key_needs_a_row.py pins that the resolvers consult the row
lookup. Whether a key HAS a row is decided in SQL, so this runs it:

  X-API-Key  mcp_gatekeeper.resolve_tier -> _resolve_from_db_hash
  Bearer     routes.tier_gate._resolve_caller_tier's api_keys fallback

Every row is written by production code — partner keys by
partner_key_issuer._issue_internal (raw key in key_hash), the Stripe-path key by
api_tier_gating.generate_api_key (sha256 in key_hash), revocation by the real
revoke route — into tables built from db_persistence.CRITICAL_TABLES, in a
private schema dropped afterwards.
"""
import ast
import os
import pathlib
import secrets
from urllib.parse import quote

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

import api_tier_gating  # noqa: E402
import mcp_gatekeeper as mg  # noqa: E402
from routes import partner_key_issuer as issuer  # noqa: E402
from routes import tier_gate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("TIER_GATES_SQL_DSN")
SCHEMA = "prefixed_key_row_t"
ADMIN = "admin-" + secrets.token_hex(8)
EXTERNAL = {"REMOTE_ADDR": "203.0.113.9"}
PARTNER_PLANS = ("developer", "starter", "pro", "enterprise")

_REAL_CONNECT = psycopg2.connect   # captured before any test patches it


def _scoped_dsn():
    return DSN + ("&" if "?" in DSN else "?") + "options=" + quote("-c search_path=" + SCHEMA)


def _ddl(table):
    for n in ast.parse((ROOT / "db_persistence.py").read_text()).body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CRITICAL_TABLES" for t in n.targets):
            cols = ast.literal_eval(n.value)[table]["columns"]
            return "CREATE TABLE %s (%s)" % (table, ", ".join("%s %s" % c for c in cols))
    raise AssertionError("db_persistence.CRITICAL_TABLES moved")


def _connect(dsn=None, **kw):
    # tier_gate asks for sslmode=require; the CI Postgres has no TLS. The DSN's
    # own sslmode decides instead. Nothing else about the connection changes.
    kw.pop("sslmode", None)
    return _REAL_CONNECT(dsn, **kw)


@pytest.fixture(scope="module")
def world():
    if not DSN:
        pytest.skip("TIER_GATES_SQL_DSN not set")
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        cur.execute("CREATE SCHEMA %s" % SCHEMA)
        cur.execute("SET search_path TO %s" % SCHEMA)
        cur.execute(_ddl("users"))
        cur.execute(_ddl("api_keys"))
    mp = pytest.MonkeyPatch()
    for var in ("DATABASE_URL", "NEON_DATABASE_URL"):
        mp.setenv(var, _scoped_dsn())
    mp.setenv("DCHUB_ADMIN_KEY", ADMIN)
    mp.setattr(psycopg2, "connect", _connect)
    try:
        keys = {}
        for plan in PARTNER_PLANS:
            out = issuer._issue_internal(partner_slug="t-" + plan,
                                         target_email=plan + "@example.test",
                                         plan=plan)
            assert out.get("ok"), out
            keys[plan] = out
        with admin.cursor() as cur:
            cur.execute("INSERT INTO users (id, email, password_hash, plan) VALUES "
                        "('u-stripe', 'stripe@example.test', 'x', 'pro')")
        mp.setattr(api_tier_gating, "get_db", lambda: _REAL_CONNECT(_scoped_dsn()))
        keys["stripe"] = api_tier_gating.generate_api_key("u-stripe", plan="pro")
        yield keys
    finally:
        mp.undo()
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        admin.close()


@pytest.fixture
def db(world, monkeypatch):
    for var in ("DATABASE_URL", "NEON_DATABASE_URL"):
        monkeypatch.setenv(var, _scoped_dsn())
    monkeypatch.setattr(psycopg2, "connect", _connect)
    monkeypatch.setattr(mg, "_key_store", {})
    monkeypatch.setattr(mg, "_row_tiers", {})
    return world


def _tier(headers):
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers=headers, environ_base=EXTERNAL):
        return tier_gate._resolve_caller_tier()[0]


def _revoke(key_prefix):
    app = flask.Flask(__name__)
    app.register_blueprint(issuer.partner_key_issuer_bp)
    r = app.test_client().post("/api/v1/admin/partner-key/revoke/" + key_prefix,
                               headers={"X-Admin-Key": ADMIN})
    assert r.status_code == 200 and r.get_json().get("ok") is not False, r.get_json()


@pytest.mark.parametrize("plan", PARTNER_PLANS)
def test_an_issued_partner_key_keeps_its_plan(db, plan):
    key = db[plan]["key"]
    assert key.startswith("dchub_%s_" % plan)
    assert _tier({"X-API-Key": key}) == plan.upper()
    assert _tier({"Authorization": "Bearer " + key}) == plan.upper()


@pytest.mark.parametrize("plan", PARTNER_PLANS)
def test_the_same_prefix_with_no_row_is_free(db, plan):
    forged = "dchub_%s_%s" % (plan, secrets.token_urlsafe(24))
    assert mg.resolve_tier(forged) == mg.Tier.FREE
    assert _tier({"X-API-Key": forged}) == "FREE"
    assert _tier({"Authorization": "Bearer " + forged}) == "FREE"


def test_a_sha256_stored_key_resolves_on_both_headers(db):
    key = db["stripe"]
    assert key.startswith("dchub_pro_")
    assert _tier({"X-API-Key": key}) == "PRO"
    assert _tier({"Authorization": "Bearer " + key}) == "PRO"


def test_a_bearer_token_sharing_only_the_stored_prefix_is_free(db):
    key = db["stripe"]
    other = key[:16] + secrets.token_urlsafe(24)
    assert other != key
    assert _tier({"Authorization": "Bearer " + other}) == "FREE"


def test_a_revoked_partner_key_is_free(db):
    issued = issuer._issue_internal(partner_slug="t-revoke",
                                    target_email="revoke@example.test",
                                    plan="enterprise")
    assert _tier({"X-API-Key": issued["key"]}) == "ENTERPRISE"   # live before
    _revoke(issued["key_prefix"])
    mg._key_store.clear()
    mg._row_tiers.clear()     # a new process: no row tier kept from before
    assert _tier({"X-API-Key": issued["key"]}) == "FREE"
    assert _tier({"Authorization": "Bearer " + issued["key"]}) == "FREE"
