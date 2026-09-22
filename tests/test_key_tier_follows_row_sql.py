"""A key's tier follows its api_keys row through the real billing SQL, in Postgres.

Skips without TIER_GATES_SQL_DSN. The db-parity job in pre-merge.yml sets it and
then FAILS if this file skipped.

tests/test_key_tier_follows_row.py pins the row -> tier rule with the row handed
back by a stub. This runs the SQL: the lookup, and the statements that change a
row after it is minted, each read out of the file that runs it in production.

  minted     api_tier_gating.generate_api_key, for every paid plan in TIERS
  dunning    main.handle_payment_failed: stamp the user, rate_limit_tier='free'
  restored   main.handle_invoice_paid: rate_limit_tier = plan
  canceled   main.handle_subscription_deleted
  upgraded   docs/NLR_UPGRADE_TO_ENTERPRISE.sql, on partner_key_issuer keys

Each key is read both ways: X-API-Key (mcp_gatekeeper.resolve_tier) and Bearer
(routes.tier_gate's api_keys fallback). Everything happens in a private schema,
dropped afterwards.
"""
import ast
import datetime
import os
import pathlib
import re
import secrets
from urllib.parse import quote

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

import api_tier_gating  # noqa: E402
import mcp_gatekeeper as mg  # noqa: E402
import tier_registry  # noqa: E402
from routes import partner_key_issuer as issuer  # noqa: E402
from routes import tier_gate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("TIER_GATES_SQL_DSN")
SCHEMA = "key_tier_row_t"
EXTERNAL = {"REMOTE_ADDR": "203.0.113.9"}
PAID = tier_registry.paid_plan_names()
MAIN = ast.parse((ROOT / "main.py").read_text())
NLR_DOC = ROOT / "docs" / "NLR_UPGRADE_TO_ENTERPRISE.sql"

_REAL_CONNECT = psycopg2.connect   # captured before any test patches it


def _statements(handler, *needles):
    """main.py string constants holding every needle, in `handler` (None: the
    whole module), deduplicated on their whitespace-collapsed text."""
    scope = MAIN if handler is None else next(
        n for n in MAIN.body if isinstance(n, ast.FunctionDef) and n.name == handler)
    found = {}
    for c in ast.walk(scope):
        if isinstance(c, ast.Constant) and isinstance(c.value, str):
            flat = " ".join(c.value.split())
            if all(x in flat for x in needles):
                found.setdefault(flat, c.value)
    return list(found.values())


def _statement(handler, *needles):
    got = _statements(handler, *needles)
    assert len(got) == 1, (handler, needles, got)
    return got[0]


DEMOTE_USER = _statement("handle_payment_failed", "UPDATE users", "demoted_at = NOW()")
DEMOTE_KEY = _statement("handle_payment_failed", "UPDATE api_keys",
                        "rate_limit_tier = 'free'")
RESTORE_KEY = _statement("handle_invoice_paid", "UPDATE api_keys",
                         "rate_limit_tier = plan")
RESTORE_USER = _statement("handle_invoice_paid", "UPDATE users",
                          "demoted_at = NULL")
CANCEL_USER = _statement("handle_subscription_deleted", "UPDATE users",
                         "subscription_status = 'canceled'")
CANCEL_KEY = _statement("handle_subscription_deleted", "UPDATE api_keys",
                        "rate_limit_tier = 'free'", "WHERE user_id = %s")


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


def _run(sql, params=None):
    c = _REAL_CONNECT(_scoped_dsn())
    try:
        with c, c.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount
    finally:
        c.close()


def _nlr_updates():
    text = "\n".join(line.split("--", 1)[0] for line in NLR_DOC.read_text().splitlines())
    ups = [s.strip() for s in text.split(";") if s.strip().upper().startswith("UPDATE")]
    assert len(ups) == 3, ups   # users, api_keys, partner_keys_issued
    return ups


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
        # main.py adds the dunning stamp columns at boot; run its own DDL.
        alters = _statements(None, "ALTER TABLE users ADD COLUMN IF NOT EXISTS demoted_")
        assert len(alters) == 2, alters
        for sql in alters:
            cur.execute(sql)
    mp = pytest.MonkeyPatch()
    for var in ("DATABASE_URL", "NEON_DATABASE_URL"):
        mp.setenv(var, _scoped_dsn())
    mp.setattr(psycopg2, "connect", _connect)
    try:
        mp.setattr(api_tier_gating, "get_db", lambda: _REAL_CONNECT(_scoped_dsn()))
        keys = {}
        for plan in PAID:
            _run("INSERT INTO users (id, email, password_hash, plan, stripe_customer_id) "
                 "VALUES (%s, %s, 'x', %s, %s)",
                 ("u-" + plan, plan + "@example.test", plan, "cus_" + plan))
            keys[plan] = api_tier_gating.generate_api_key("u-" + plan, plan=plan)
        # The doc names the partner's contacts; issue each a developer key, as
        # partner_key_issuer did before the doc ran.
        emails = re.findall(r"'([^'\s]+@[^'\s]+)'", _nlr_updates()[0])
        assert emails
        nlr = []
        for email in emails:
            out = issuer._issue_internal(partner_slug="reveal-nlr",
                                         target_email=email, plan="developer")
            assert out.get("ok"), out
            nlr.append(out["key"])
        yield {"keys": keys, "nlr": nlr}
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


def _read(key):
    """(X-API-Key tier, Bearer tier), each read as a new process would: the
    gatekeeper keeps a row's tier for _ROW_TIER_TTL_S, so this empties it."""
    out = []
    for headers in ({"X-API-Key": key}, {"Authorization": "Bearer " + key}):
        mg._key_store.clear()
        mg._row_tiers.clear()
        app = flask.Flask(__name__)
        with app.test_request_context("/", headers=headers, environ_base=EXTERNAL):
            out.append(tier_gate._resolve_caller_tier()[0])
    return tuple(out)


def _is(key, want, state):
    x, bearer = _read(key)
    assert x == want, (state, "X-API-Key", x, want)
    # Bearer reports the row's own word (RESEARCH_SEED, TEAM); gates compare ranks.
    assert tier_gate._TIER_RANK.get(bearer) == tier_gate._TIER_RANK[want], (
        state, "Bearer", bearer, want)


@pytest.mark.parametrize("plan", PAID)
def test_a_paid_key_through_dunning_restore_and_cancel(db, plan):
    key, user, customer = db["keys"][plan], "u-" + plan, "cus_" + plan
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    paid = mg.TIER_NAME[mg._tier_of_row(plan, None, None)].upper()
    assert paid != "FREE"

    _is(key, paid, "minted")

    # A first charge that never succeeded, then a prior payer's failed renewal.
    # The payment that resolves each demote lifts it, through both halves of
    # the restore; the stamp must be cleared or the next demote cannot land.
    for reason in ("first_charge_never_succeeded", "dunning_prior_payer"):
        assert _run(DEMOTE_USER, (reason, user)) == 1
        assert _run(DEMOTE_KEY, (now, user)) == 1
        _is(key, "FREE", reason + "-demoted")

        assert _run(RESTORE_KEY, (customer,)) == 1
        assert _run(RESTORE_USER, (customer,)) == 1
        _is(key, paid, reason + "-restored")

    assert _run(CANCEL_USER, (customer,)) == 1
    assert _run(CANCEL_KEY, (now, user)) == 1
    _is(key, "FREE", "canceled")


def test_the_nlr_upgrade_makes_its_developer_keys_enterprise(db):
    assert db["nlr"] and all(k.startswith("dchub_developer_") for k in db["nlr"])
    for key in db["nlr"]:
        _is(key, "DEVELOPER", "issued")
    counts = [_run(sql) for sql in _nlr_updates()]
    assert all(n == len(db["nlr"]) for n in counts), counts
    for key in db["nlr"]:
        _is(key, "ENTERPRISE", "upgraded")
