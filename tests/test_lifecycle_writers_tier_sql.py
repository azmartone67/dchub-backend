"""The billing lifecycle writers must write the tier columns the gates read.

The gates read a key's tier two ways:
  * mcp_gatekeeper._tier_of_row / flask_mcp_endpoints._api_key_row_node_tier:
    the FIRST non-empty of api_keys.rate_limit_tier, api_keys.plan, users.plan.
  * flask_mcp_endpoints.resolve_effective_node_tier (validate_key's tier
    resolution without the HTTP hop): the HIGHEST of mcp_dev_keys.tier, the
    users row and api_keys.rate_limit_tier.

So each writer below has to reach those columns:
  main.handle_payment_failed   the dunning demote lowers mcp_dev_keys.tier and
                               records the tier each key held
  main.handle_invoice_paid     puts back exactly that record
  api_tier_gating (v2 hook)    a cancel writes rate_limit_tier and
                               mcp_dev_keys.tier for every user on the
                               customer; active/trialing raises
                               rate_limit_tier for a recognised price only
  main.handle_subscription_*   .deleted and .updated with status canceled
                               both lower mcp_dev_keys.tier
  main.handle_checkout_completed  a k-<sha256(api_key)> checkout records the
                               paying customer on the key it raises, and every
                               demote above matches that record as well as the
                               customer's addresses

Real handlers against a real Postgres. main.py cannot be imported in a unit
test, so its two handlers are compiled out of its AST with _pg_execute bound to
this database; every other global they read is stubbed and checked for below,
so a new dependency fails here instead of vanishing into the handler's own
except. The v2 handlers run from the imported module with get_db() returning
db_utils.PGConnectionWrapper, so production's SQL translation runs too.

Skips without TIER_GATES_SQL_DSN. The db-parity job in pre-merge.yml sets it
and fails if this file skipped.
"""
import ast
import builtins
import copy
import hashlib
import logging
import os
import pathlib
import re
import sys
from contextlib import contextmanager
from urllib.parse import quote

import pytest

psycopg2 = pytest.importorskip("psycopg2")
Json = pytest.importorskip("psycopg2.extras").Json

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# flask_mcp_endpoints refuses to import without a DB URL; nothing connects
# until a request runs. Confined to the import and put back.
_injected = not (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL"))
if _injected:
    os.environ["NEON_DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/db"
try:
    import flask_mcp_endpoints as fme  # noqa: E402
finally:
    if _injected:
        os.environ.pop("NEON_DATABASE_URL", None)
import api_tier_gating  # noqa: E402
import db_utils  # noqa: E402
import mcp_gatekeeper as mg  # noqa: E402

DSN = os.environ.get("TIER_GATES_SQL_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TIER_GATES_SQL_DSN not set")

SCHEMA = "lifecycle_writers_t"
MAIN = ast.parse((ROOT / "main.py").read_text())
CUS = "cus_lifecycle_t"
EMAIL = "payer@example.com"
DCH = "dch_live_" + "a" * 32        # an MCP key: an mcp_dev_keys row, no api_keys row
OTHER = "dch_live_" + "b" * 32
HASH = "h" * 64                     # an api_keys row's key_hash
PRICES = {"pro_monthly": "price_pro_t", "enterprise_monthly": "price_ent_t",
          "developer_monthly": "price_dev_t"}
_REAL_CONNECT = psycopg2.connect


def _scoped_dsn():
    return DSN + ("&" if "?" in DSN else "?") + "options=" + quote("-c search_path=" + SCHEMA)


def _connect():
    return _REAL_CONNECT(_scoped_dsn())


def _critical_ddl(table):
    for n in ast.parse((ROOT / "db_persistence.py").read_text()).body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CRITICAL_TABLES" for t in n.targets):
            cols = ast.literal_eval(n.value)[table]["columns"]
            return "CREATE TABLE %s (%s)" % (table, ", ".join("%s %s" % c for c in cols))
    raise AssertionError("db_persistence.CRITICAL_TABLES moved")


def _users_migrations():
    """The ALTERs main.py runs for the dunning columns, read from main.py."""
    want = ("invoices_paid_count", "payment_failed_count", "demoted_at", "demoted_reason")
    got = {}
    for c in ast.walk(MAIN):
        if isinstance(c, ast.Constant) and isinstance(c.value, str):
            m = re.match(r"ALTER TABLE users ADD COLUMN IF NOT EXISTS (\w+) ", c.value)
            if m and m.group(1) in want:
                got.setdefault(m.group(1), c.value)
    assert sorted(got) == sorted(want), got
    return list(got.values())


def _onetime_migrations():
    """The one-time-purchase columns, read from routes/schema_repair.py.

    handle_checkout_completed writes users.tier_expires_at and users.source_plan
    — the one-time branch stamps them, and since 2026-09-22 a SUBSCRIPTION
    checkout NULLs them (r-onetime-carryover), which is the path this file's
    harness drives. main.py declares neither: their ALTERs live in
    routes/schema_repair.py, so read them from there rather than restating the
    types here, where a change to either would not reach this fixture.
    """
    want = ("tier_expires_at", "source_plan")
    src = (ROOT / "routes" / "schema_repair.py").read_text(encoding="utf-8")
    got = {}
    for m in re.finditer(
            r"ALTER TABLE users ADD COLUMN IF NOT EXISTS (\w+) [^\"']+", src):
        if m.group(1) in want:
            got.setdefault(m.group(1), m.group(0))
    assert sorted(got) == sorted(want), (
        "routes/schema_repair.py no longer declares %s — the checkout writes "
        "them, so this harness cannot build a users table it can run against"
        % (set(want) - set(got)))
    return list(got.values())


def _mcp_dev_keys_ddl():
    sql = (ROOT / "dchub-mcp-v2.1" / "migration_001_api_keys.sql").read_text()
    sql = re.sub(r"--[^\n]*", "", sql)
    m = re.search(r"CREATE TABLE IF NOT EXISTS mcp_dev_keys \(.*?\n\);", sql, re.S)
    assert m, "mcp_dev_keys DDL moved"
    return m.group(0)


def _free_names(fn):
    bound = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
    loads = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return loads - bound - set(dir(builtins))


def _compile(name, ns):
    fn = copy.deepcopy(next(n for n in MAIN.body
                            if isinstance(n, ast.FunctionDef) and n.name == name))
    fn.decorator_list = []
    missing = _free_names(fn) - set(ns)
    assert not missing, f"main.{name} now reads {sorted(missing)}: stub them"
    g = dict(ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), g)
    return g[name]


class _Pool:
    """flask_mcp_endpoints._pool, on the test database."""
    @contextmanager
    def connection(self):
        conn = _connect()
        try:
            yield conn
        finally:
            conn.close()


class _Harness:
    def __init__(self, monkeypatch):
        self.sql_errors = []
        self.notices = []

        def _pg_execute(query, params=(), fetch=False):
            # main._pg_execute's contract: (rowcount, rows), or (0, []) on any
            # error. Errors are also recorded, so a broken statement fails the
            # test instead of reading as "matched nothing".
            conn = _connect()
            try:
                cur = conn.cursor()
                cur.execute(query, params)
                rows = cur.fetchall() if fetch else []
                rc = cur.rowcount
                conn.commit()
                return (rc, rows)
            except Exception as e:  # noqa: BLE001 - mirrors _pg_execute
                self.sql_errors.append(f"{e}".strip())
                return (0, [])
            finally:
                conn.close()

        def _no_sqlite_mirror(*a, **k):
            raise RuntimeError("no SQLite mirror in this test")

        ns = {
            "_pg_execute": _pg_execute,
            "get_db": _no_sqlite_mirror,
            "_sync_tables_bg": lambda *a, **k: None,
            "note_swallowed_write": lambda *a, **k: None,
            "utc_iso_z": lambda: "2026-09-22T00:00:00Z",
            "STRIPE_AVAILABLE": False,
            "stripe": None,
            "_send_dunning_demote_notice": lambda *a, **k: self.notices.append(a),
        }
        # r-trial-dunning: the dunning handlers call these two helpers; compile
        # the real ones against the same stubs (STRIPE_AVAILABLE=False -> the
        # count is None and the post-trial check is False: today's behaviour).
        ns["_real_paid_invoice_count"] = _compile("_real_paid_invoice_count", ns)
        ns["_is_post_trial_first_charge"] = _compile("_is_post_trial_first_charge", ns)
        self.payment_failed_fn = _compile("handle_payment_failed", ns)
        self.invoice_paid_fn = _compile("handle_invoice_paid", ns)

        sub_ns = dict(ns, _handle_subscription_plan_change=lambda *a, **k: None)
        sub_ns["_demote_customer_mcp_keys"] = _compile("_demote_customer_mcp_keys", sub_ns)
        self.sub_updated_fn = _compile("handle_subscription_updated", sub_ns)
        self.sub_deleted_fn = _compile("handle_subscription_deleted", sub_ns)

        def _pg_execute_many(stmts):
            for q, p in stmts:
                _pg_execute(q, p)

        co_ns = dict(ns, _pg_execute_many=_pg_execute_many, hashlib=hashlib,
                     logger=logging.getLogger("lifecycle_writers_t"),
                     hash_password=lambda pw: "x",
                     _apply_plan_guard=lambda s, p, t, u, e: (p, t, None),
                     _plan_write_floor=lambda e, u, p, t: (p, t),
                     _checkout_trial_end=lambda s: None,
                     send_admin_alert_email=lambda *a, **k: None,
                     send_welcome_email_sendgrid=lambda *a, **k: None)
        # The REAL offer→plan helper and its map, not a stub: a trial
        # checkout's plan comes from it (r-pro-trial7).
        offer_map = next(n.value for n in MAIN.body if isinstance(n, ast.Assign)
                         and any(getattr(t, "id", "") == "_CHECKOUT_OFFER_PLAN"
                                 for t in n.targets))
        co_ns["_plan_from_checkout_offer"] = _compile(
            "_plan_from_checkout_offer",
            dict(co_ns, _CHECKOUT_OFFER_PLAN=ast.literal_eval(offer_map)))
        self.checkout_fn = _compile("handle_checkout_completed", co_ns)

        monkeypatch.setattr(api_tier_gating, "get_db",
                            lambda *a, **k: db_utils.PGConnectionWrapper(
                                _connect(), return_func=lambda c: c.close()))
        for k, v in PRICES.items():
            monkeypatch.setitem(api_tier_gating.STRIPE_PRICES_V2, k, v)
        monkeypatch.setattr(fme, "_pool", _Pool())
        monkeypatch.delenv("MONETIZE_METERED_ENFORCE", raising=False)

    # ── the database ──────────────────────────────────────────────────────
    def sql(self, query, params=None, fetch=False):
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def user(self, uid="u1", email=EMAIL, plan="founding", cus=CUS, status="active",
             paid=1, failed=0):
        self.sql("INSERT INTO users (id, email, password_hash, role, plan, stripe_customer_id, "
                 "subscription_status, invoices_paid_count, payment_failed_count) "
                 "VALUES (%s,%s,'x','pro',%s,%s,%s,%s,%s)",
                 (uid, email, plan, cus, status, paid, failed))

    def api_key(self, uid="u1", key_hash=HASH, rate_limit_tier="pro", plan="pro", is_active=1):
        self.sql("INSERT INTO api_keys (user_id, key_hash, key_prefix, rate_limit_tier, "
                 "is_active, plan) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (uid, key_hash, key_hash[:8], rate_limit_tier, is_active, plan))

    def mcp_key(self, api_key=DCH, email=EMAIL, tier="paid", metadata=None):
        self.sql("INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, metadata) "
                 "VALUES (%s, %s, %s, %s, %s::jsonb)",
                 (api_key, "dev_" + api_key[-4:], email, tier,
                  None if metadata is None else Json(metadata)))

    def mcp_row(self, api_key=DCH):
        return tuple(self.sql("SELECT tier, metadata FROM mcp_dev_keys WHERE api_key = %s",
                              (api_key,), fetch=True)[0])

    def api_row(self, key_hash=HASH):
        """(rate_limit_tier, api_keys.plan, users.plan): _tier_of_row's inputs."""
        return tuple(self.sql("SELECT k.rate_limit_tier, k.plan, u.plan FROM api_keys k "
                              "JOIN users u ON u.id = k.user_id WHERE k.key_hash = %s",
                              (key_hash,), fetch=True)[0])

    # ── the handlers ──────────────────────────────────────────────────────
    def payment_failed(self, times=1):
        for _ in range(times):
            self.payment_failed_fn({"customer": CUS, "attempt_count": 1})

    def invoice_paid(self, customer=CUS):
        self.invoice_paid_fn({"customer": customer})

    def k_checkout(self, api_key=DCH, customer=CUS, email=EMAIL):
        """A Pro subscription checkout opened by whoever holds api_key
        (client_reference_id k-<sha256(api_key)>) and paid from `email`."""
        self.checkout_fn({
            "id": "cs_lifecycle_t", "mode": "subscription", "customer": customer,
            "customer_details": {"email": email}, "amount_total": 9900,
            "metadata": {"plan": "pro_monthly"}, "subscription": "sub_lifecycle_t",
            "client_reference_id": "k-" + hashlib.sha256(api_key.encode()).hexdigest()})

    # The subscription handlers open their mirror block with get_db() outside
    # a try, after every Postgres write. The mirror is not under test, so the
    # stub raises there; reaching it means the Postgres writes all ran.
    def sub_updated(self, status, customer=CUS):
        with pytest.raises(RuntimeError, match="no SQLite mirror"):
            self.sub_updated_fn({"customer": customer, "status": status})

    def sub_deleted(self, customer=CUS):
        with pytest.raises(RuntimeError, match="no SQLite mirror"):
            self.sub_deleted_fn({"customer": customer})

    def v2_deleted(self):
        api_tier_gating._handle_sub_deleted_v2({"customer": CUS})

    def v2_updated(self, status, price=None):
        items = [{"price": {"id": price}}] if price is not None else []
        api_tier_gating._handle_sub_updated_v2(
            {"customer": CUS, "status": status, "items": {"data": items}})

    # ── the readers ───────────────────────────────────────────────────────
    @staticmethod
    def node_tier(api_key=DCH):
        return fme.resolve_effective_node_tier(api_key)


@pytest.fixture
def h(monkeypatch):
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    cur.execute(f"SET search_path TO {SCHEMA}")
    # handle_checkout_completed writes users.plan_updated_at (and
    # routes/auth_routes.py reads it); no DDL in the tree declares it.
    for stmt in [_critical_ddl("users"), *_users_migrations(),
                 *_onetime_migrations(),
                 "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_updated_at TIMESTAMPTZ",
                 _critical_ddl("api_keys"), _mcp_dev_keys_ddl()]:
        cur.execute(stmt)
    harness = _Harness(monkeypatch)
    try:
        yield harness
        assert harness.sql_errors == [], harness.sql_errors
    finally:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.close()


# ═══ main.handle_payment_failed: the dunning demote reaches the MCP key ═══════

def test_the_dunning_demote_reaches_a_checkout_promoted_mcp_key(h):
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid")
    assert h.node_tier() == "paid"
    h.payment_failed()                                   # failure #4
    assert h.mcp_row() == ("free", {"dunning_demote": {
        "from": "paid", "customer": CUS, "reason": "dunning_prior_payer"}})
    assert h.node_tier() == "free"
    assert len(h.notices) == 1                           # the demote really fired


def test_the_grace_window_leaves_the_mcp_key_alone(h):
    h.user(paid=1, failed=0)
    h.mcp_key(tier="paid")
    h.payment_failed(times=3)
    assert h.mcp_row() == ("paid", None)
    assert h.node_tier() == "paid"


def test_a_first_charge_demote_reaches_the_mcp_key_too(h):
    h.user(paid=0, failed=1, plan="developer")
    h.mcp_key(tier="enterprise")
    h.payment_failed()                                   # failure #2, never paid
    tier, meta = h.mcp_row()
    assert tier == "free"
    assert meta["dunning_demote"] == {"from": "enterprise", "customer": CUS,
                                      "reason": "first_charge_never_succeeded"}


def test_only_keys_bound_to_the_demoted_address_move(h):
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid", email=EMAIL.upper())          # matched case-blind
    h.mcp_key(api_key=OTHER, email="someone@else.example", tier="paid")
    h.payment_failed()
    assert h.mcp_row()[0] == "free"
    assert h.mcp_row(OTHER) == ("paid", None)


def test_a_key_that_was_already_free_is_not_recorded(h):
    """No record, so no later payment can raise a key the demote never lowered."""
    h.user(paid=1, failed=3)
    h.mcp_key(tier="free", metadata={"email_verified_for": EMAIL})
    h.payment_failed()
    assert h.mcp_row() == ("free", {"email_verified_for": EMAIL})


# ═══ main.handle_invoice_paid: exactly what the demote took comes back ════════

@pytest.mark.parametrize("held", ["paid", "enterprise"])
@pytest.mark.parametrize("paid,failed", [(1, 3), (0, 1)],
                         ids=["prior_payer", "first_charge"])
def test_a_payment_puts_back_exactly_the_tier_the_demote_took(h, held, paid, failed):
    h.user(paid=paid, failed=failed)
    h.mcp_key(tier=held)
    h.payment_failed()
    assert h.mcp_row()[0] == "free"
    h.invoice_paid()
    assert h.mcp_row() == (held, {})
    assert h.node_tier() == held
    h.invoice_paid()                                     # idempotent
    assert h.mcp_row() == (held, {})


def test_other_metadata_survives_the_demote_and_the_restore(h):
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid", metadata={"email_verified_for": EMAIL})
    h.payment_failed()
    h.invoice_paid()
    assert h.mcp_row() == ("paid", {"email_verified_for": EMAIL})


def test_a_payment_never_raises_a_key_the_demote_did_not_lower(h):
    h.user(paid=1, failed=3)
    h.mcp_key(tier="free")
    h.payment_failed()
    h.invoice_paid()
    assert h.mcp_row() == ("free", None)


def test_a_key_raised_since_the_demote_keeps_the_higher_tier(h):
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid")
    h.payment_failed()
    h.sql("UPDATE mcp_dev_keys SET tier = 'enterprise' WHERE api_key = %s", (DCH,))
    h.invoice_paid()
    assert h.mcp_row() == ("enterprise", {})


def test_a_malformed_record_does_not_block_the_other_keys(h):
    """The restore skips a record whose tier is not one it could have taken,
    rather than failing the whole statement on the tier CHECK constraint."""
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid")
    h.mcp_key(api_key=OTHER, tier="paid")                # same address
    h.payment_failed()
    h.sql("""UPDATE mcp_dev_keys
                SET metadata = jsonb_set(metadata, '{dunning_demote,from}', '"bogus"')
              WHERE api_key = %s""", (OTHER,))
    h.invoice_paid()
    assert h.mcp_row() == ("paid", {})
    assert h.mcp_row(OTHER)[0] == "free"


def test_a_record_on_a_key_not_bound_to_the_payer_raises_nothing(h):
    """The restore needs the record AND the key still bound to the paying
    customer's address, the way the demote chose it: a record alone is not
    enough to raise a key."""
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid")
    h.payment_failed()
    h.sql("UPDATE mcp_dev_keys SET email = 'someone@else.example' WHERE api_key = %s", (DCH,))
    h.mcp_key(api_key=OTHER, email="another@else.example", tier="free",
              metadata={"dunning_demote": {"from": "enterprise", "customer": CUS,
                                           "reason": "dunning_prior_payer"}})
    h.invoice_paid()
    assert h.mcp_row()[0] == "free"
    assert h.mcp_row(OTHER)[0] == "free"


def test_a_record_from_another_customers_demote_is_not_restored(h):
    """The record names the customer whose demote wrote it. A key demoted
    under one customer and later bound to another's address is not raised
    by the second customer's payment."""
    h.user(uid="u1", paid=1, failed=0)                   # the payer, CUS
    h.user(uid="u2", email="b@example.com", cus="cus_b", paid=1, failed=3)
    h.mcp_key(tier="paid", email="b@example.com")
    h.payment_failed_fn({"customer": "cus_b", "attempt_count": 1})
    assert h.mcp_row()[1]["dunning_demote"]["customer"] == "cus_b"
    h.sql("UPDATE mcp_dev_keys SET email = %s WHERE api_key = %s", (EMAIL, DCH))
    h.invoice_paid()
    assert h.mcp_row()[0] == "free"


def test_another_customers_payment_restores_nothing(h):
    h.user(paid=1, failed=3)
    h.mcp_key(tier="paid")
    h.payment_failed()
    h.invoice_paid(customer="cus_someone_else")
    assert h.mcp_row()[0] == "free"


# ═══ v2 cancel: every column the gates read, every user on the customer ══════

@pytest.mark.parametrize("event", ["deleted", "updated_canceled"])
def test_a_v2_cancel_reaches_every_column_the_gates_read(h, event):
    h.user(plan="pro")
    h.api_key(rate_limit_tier="pro", plan="pro")
    h.mcp_key(tier="paid")
    if event == "deleted":
        h.v2_deleted()
    else:
        h.v2_updated("canceled")
    rlt, plan, user_plan = h.api_row()
    assert (rlt, plan, user_plan) == ("free", "free", "free")
    assert mg._tier_of_row(rlt, plan, user_plan) == mg.Tier.FREE
    assert fme._api_key_row_node_tier(rlt, plan, user_plan) == "free"
    assert h.mcp_row()[0] == "free"
    assert h.node_tier() == "free"


def test_a_v2_cancel_covers_every_user_on_the_customer_and_only_them(h):
    h.user(uid="u1")
    h.user(uid="u2", email="second@example.com")
    h.user(uid="u3", email="bystander@example.com", cus="cus_other")
    h.api_key(uid="u1", key_hash="k1", rate_limit_tier="pro")
    h.api_key(uid="u2", key_hash="k2", rate_limit_tier="enterprise", is_active=None)
    h.api_key(uid="u3", key_hash="k3", rate_limit_tier="pro")
    h.mcp_key(api_key=OTHER, email="second@example.com", tier="enterprise")
    h.mcp_key(api_key="dch_live_" + "c" * 32, email="bystander@example.com", tier="paid")
    h.v2_deleted()
    assert h.api_row("k1")[0] == "free"
    assert h.api_row("k2")[0] == "free"      # the gates read a NULL is_active as active
    assert h.mcp_row(OTHER)[0] == "free"
    assert h.api_row("k3")[0] == "pro"
    assert h.mcp_row("dch_live_" + "c" * 32)[0] == "paid"


# ═══ v2 active/trialing: an upgrade lands where the gates read it ═════════════

@pytest.mark.parametrize("status", ["active", "trialing"])
def test_a_v2_upgrade_lands_in_rate_limit_tier(h, status):
    h.user(plan="pro")
    h.api_key(rate_limit_tier="pro", plan="pro")
    h.v2_updated(status, price="price_ent_t")
    rlt, plan, user_plan = h.api_row()
    assert (rlt, plan) == ("enterprise", "enterprise")
    assert mg._tier_of_row(rlt, plan, user_plan) == mg.Tier.ENTERPRISE


def test_a_v2_reactivation_lifts_a_free_rate_limit_tier(h):
    h.user(plan="founding")
    h.api_key(rate_limit_tier="free", plan="founding")
    h.v2_updated("active", price="price_pro_t")
    assert h.api_row()[0] == "pro"


def test_a_v2_update_never_lowers_rate_limit_tier(h):
    h.user(plan="enterprise")
    h.api_key(rate_limit_tier="enterprise", plan="enterprise")
    h.v2_updated("active", price="price_dev_t")
    assert h.api_row()[0] == "enterprise"


@pytest.mark.parametrize("price", ["price_not_configured", None])
def test_an_unrecognised_price_never_reaches_rate_limit_tier(h, price):
    """The handler falls back to plan='pro' when no price matches. That guess
    must not become the tier the gates read."""
    h.user(plan="starter")
    h.api_key(rate_limit_tier="starter", plan="starter")
    h.v2_updated("active", price=price)
    assert h.api_row()[0] == "starter"


def test_an_empty_configured_price_does_not_match_an_item_without_one(h, monkeypatch):
    monkeypatch.setitem(api_tier_gating.STRIPE_PRICES_V2, "enterprise_monthly", "")
    h.user(plan="starter")
    h.api_key(rate_limit_tier="starter", plan="starter")
    h.v2_updated("active", price="")
    assert h.api_row()[0] == "starter"


# ═══ main.handle_subscription_updated: a cancel reaches the MCP key ═══════════

def test_an_update_to_canceled_lowers_the_mcp_key_as_deleted_does(h):
    h.user(plan="pro")
    h.api_key(rate_limit_tier="pro", plan="pro")
    h.mcp_key(tier="paid", email=EMAIL.upper())          # matched case-blind
    h.sub_updated("canceled")
    assert h.api_row()[0] == "free"
    assert h.mcp_row()[0] == "free"
    assert h.node_tier() == "free"


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "unpaid"])
def test_an_update_that_is_not_a_cancel_leaves_the_mcp_keys(h, status):
    h.user(plan="pro")
    h.mcp_key(tier="paid")
    h.mcp_key(api_key=OTHER, email=None, tier="paid", metadata={"stripe_customer_id": CUS})
    h.sub_updated(status)
    assert h.mcp_row()[0] == "paid"
    assert h.mcp_row(OTHER)[0] == "paid"


# ═══ a key a k- checkout raised: every demote reaches it by its customer ══════

HOLDERS = pytest.mark.parametrize("holder", [None, "holder@else.example"],
                                  ids=["no_address", "another_address"])


@HOLDERS
def test_a_k_checkout_records_the_paying_customer_on_the_key_it_raises(h, holder):
    h.mcp_key(email=holder, tier="free", metadata={"email_verified_for": holder})
    h.k_checkout()
    assert h.mcp_row() == ("paid", {"email_verified_for": holder, "stripe_customer_id": CUS})
    assert h.node_tier() == "paid"


@HOLDERS
@pytest.mark.parametrize("cancel", ["deleted", "updated_canceled", "v2_deleted",
                                    "v2_updated_canceled"])
def test_every_cancel_lowers_a_key_its_customers_k_checkout_raised(h, holder, cancel):
    h.mcp_key(email=holder, tier="free")
    h.k_checkout()
    assert h.mcp_row()[0] == "paid"
    {"deleted": h.sub_deleted,
     "updated_canceled": lambda: h.sub_updated("canceled"),
     "v2_deleted": h.v2_deleted,
     "v2_updated_canceled": lambda: h.v2_updated("canceled")}[cancel]()
    assert h.mcp_row()[0] == "free"
    assert h.node_tier() == "free"


@HOLDERS
def test_the_dunning_demote_and_restore_reach_a_key_its_customers_k_checkout_raised(h, holder):
    h.mcp_key(email=holder, tier="free")
    h.k_checkout()
    h.sql("UPDATE users SET invoices_paid_count = 1, payment_failed_count = 3 "
          "WHERE stripe_customer_id = %s", (CUS,))
    h.payment_failed()                                   # failure #4
    tier, meta = h.mcp_row()
    assert tier == "free"
    assert meta["dunning_demote"] == {"from": "paid", "customer": CUS,
                                      "reason": "dunning_prior_payer"}
    h.invoice_paid()
    assert h.mcp_row() == ("paid", {"stripe_customer_id": CUS})


def test_another_customers_cancel_leaves_a_key_this_customer_raised(h):
    h.mcp_key(email=None, tier="free")
    h.k_checkout()
    h.sub_deleted(customer="cus_someone_else")
    h.sub_updated("canceled", customer="cus_someone_else")
    assert h.mcp_row() == ("paid", {"stripe_customer_id": CUS})


def test_a_k_checkout_leaves_an_enterprise_key_unrecorded_so_its_cancel_does_too(h):
    h.mcp_key(email=None, tier="enterprise")
    h.k_checkout()
    assert h.mcp_row() == ("enterprise", None)
    h.sub_deleted()
    assert h.mcp_row()[0] == "enterprise"
