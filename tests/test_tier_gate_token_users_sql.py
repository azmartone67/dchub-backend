"""tier_gate's raw-token DB fallback against a REAL Postgres: only a stored
api key resolves there, never a users row.

Skips without TIER_GATES_SQL_DSN. The db-parity job in pre-merge.yml sets it
and then FAILS if this file skipped.

routes.tier_gate._resolve_caller_tier reads the dchub_token cookie or an
Authorization: Bearer token. The login credential on that channel is the
signed JWT routes.auth_routes.generate_jwt mints. While nothing PRO+ has
resolved, the resolver falls back to the database, where a stored api key
resolves its tier. The token is not looked up in users: nothing writes a
users.session_token, and a users.id is an identifier, not a credential.

Tables come from db_persistence.CRITICAL_TABLES, in a private schema dropped
afterwards. CRITICAL_TABLES has no users.session_token, and a statement that
names a missing column raises, which the resolver swallows. On that schema
alone a users lookup naming the column could never be seen to match, so every
test also runs with the column added.
"""
import ast
import os
import pathlib
import secrets
from urllib.parse import quote

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")
pytest.importorskip("jwt")

from routes import auth_routes  # noqa: E402
from routes import tier_gate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("TIER_GATES_SQL_DSN")
SCHEMA = "tier_token_users_t"
JWT_SECRET = "jwt-" + secrets.token_hex(16)
CHANNELS = ("bearer", "cookie")

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


@pytest.fixture(scope="module", params=["critical_tables", "with_session_token"])
def world(request):
    if not DSN:
        pytest.skip("TIER_GATES_SQL_DSN not set")
    row = {"user_id": secrets.token_hex(8),   # the id shape auth_routes mints
           "api_key": "dch_live_" + secrets.token_hex(16),
           "session_token": None}
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    mp = pytest.MonkeyPatch()
    try:
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
            cur.execute("CREATE SCHEMA %s" % SCHEMA)
            cur.execute("SET search_path TO %s" % SCHEMA)
            cur.execute(_ddl("users"))
            cur.execute(_ddl("api_keys"))
            cur.execute("INSERT INTO users (id, email, password_hash, plan) "
                        "VALUES (%s, 'payer@example.test', 'x', 'pro')",
                        (row["user_id"],))
            if request.param == "with_session_token":
                row["session_token"] = secrets.token_urlsafe(32)
                cur.execute("ALTER TABLE users ADD COLUMN session_token TEXT")
                cur.execute("UPDATE users SET session_token = %s WHERE id = %s",
                            (row["session_token"], row["user_id"]))
            # An active PRO key stored whole, as partner keys are.
            cur.execute("INSERT INTO api_keys (user_id, key_hash, key_prefix, "
                        "rate_limit_tier, is_active) VALUES ('u-key', %s, %s, 'pro', 1) ON CONFLICT DO NOTHING",
                        (row["api_key"], row["api_key"][:16]))
        mp.setenv("DATABASE_URL", _scoped_dsn())
        mp.setenv("JWT_SECRET", JWT_SECRET)
        mp.setattr(auth_routes, "_JWT_SECRET", JWT_SECRET)
        mp.setattr(psycopg2, "connect", _connect)
        yield row, admin
    finally:
        mp.undo()
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        admin.close()


def resolve(token, channel):
    headers = ({"Authorization": "Bearer " + token} if channel == "bearer"
               else {"Cookie": "dchub_token=" + token})
    with flask.Flask(__name__).test_request_context("/", headers=headers):
        return tier_gate._resolve_caller_tier()


def plan_where(admin, column, value):
    with admin.cursor() as cur:
        cur.execute("SELECT plan FROM " + SCHEMA + ".users WHERE " + column + " = %s",
                    (value,))
        return cur.fetchone()


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_stored_api_key_resolves_through_the_fallback(world, channel):
    # The control that gives every FREE below its meaning: the fallback
    # reaches this Postgres and reads it.
    row, _ = world
    tier, info = resolve(row["api_key"], channel)
    assert (tier, info["source"]) == ("PRO", "cookie:api_keys"), info


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_token_equal_to_a_users_id_resolves_free(world, channel):
    row, admin = world
    assert plan_where(admin, "id", row["user_id"]) == ("pro",)
    tier, info = resolve(row["user_id"], channel)
    assert (tier, info["source"]) == ("FREE", "anonymous"), info
    assert "cookie_resolve_err" not in info, info


@pytest.mark.parametrize("world", ["with_session_token"], indirect=True)
@pytest.mark.parametrize("channel", CHANNELS)
def test_a_token_equal_to_a_stored_session_token_resolves_free(world, channel):
    row, admin = world
    assert plan_where(admin, "session_token", row["session_token"]) == ("pro",)
    tier, info = resolve(row["session_token"], channel)
    assert (tier, info["source"]) == ("FREE", "anonymous"), info


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_signed_jwt_still_resolves_its_plan(world, channel):
    row, _ = world
    token = auth_routes.generate_jwt(row["user_id"], "payer@example.test", "user", "pro")
    tier, info = resolve(token, channel)
    assert (tier, info["source"]) == ("PRO", "cookie:jwt"), info
