"""A dchub_ key gets the tier its api_keys row grants. Its prefix grants nothing.

Owner decision, 2026-09-22. A key string is fixed when it is minted; the row is
what changes afterwards. The reveal-nlr partner holds dchub_developer_ keys on
rows docs/NLR_UPGRADE_TO_ENTERPRISE.sql moved to enterprise, and
main.handle_payment_failed's dunning demote sets rate_limit_tier to 'free' and
leaves the key string and api_keys.plan alone.

mcp_gatekeeper._tier_of_row reads rate_limit_tier, then api_keys.plan, then
users.plan, through tier_registry.api_tier. Every expectation here is derived
from tier_registry.TIERS; no plan-to-tier table is typed in this file. The real
_resolve_from_db_hash runs, against a connection that hands back the row. That
connection ignores the SQL it is given, so tests/test_key_tier_follows_row_sql.py
runs the SQL against Postgres.
"""
import itertools
import secrets

import pytest

flask = pytest.importorskip("flask")
psycopg2 = pytest.importorskip("psycopg2")

import mcp_gatekeeper as mg  # noqa: E402
import tier_registry  # noqa: E402
from routes import tier_gate  # noqa: E402

NAMES = sorted(tier_registry.TIERS)
PAID = tier_registry.paid_plan_names()
EXTERNAL = {"REMOTE_ADDR": "203.0.113.9"}   # not loopback: no trust signal
# partner_key_issuer's dchub_<plan>_ for every plan name, generate_api_key's
# short forms, and a key with no plan in it.
PREFIXES = sorted({"dchub_%s_" % n for n in NAMES}
                  | {"dchub_dev_", "dchub_pro_", "dchub_ent_", "dchub_sta_", "dchub_"})


class _Db:
    """psycopg2.connect for _resolve_from_db_hash: the query's second parameter
    is the raw key, and the row held for that key is what fetchone returns."""

    def __init__(self):
        self.rows = {}

    def connect(self, *_a, **_k):
        rows = self.rows

        class Cur:
            row = None

            def __enter__(self):
                return self

            def __exit__(self, *_e):
                return False

            def execute(self, _sql, params):
                self.row = rows.get(params[1])

            def fetchone(self):
                return self.row

        class Conn:
            autocommit = False

            def cursor(self, **_k):
                return Cur()

            def close(self):
                pass

        return Conn()


@pytest.fixture
def db(monkeypatch):
    d = _Db()
    monkeypatch.setattr(psycopg2, "connect", d.connect)
    monkeypatch.setenv("NEON_DATABASE_URL", "postgres://row-stub/none")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(mg, "_key_store", {})
    return d


def _row(rate_limit_tier, plan, user_plan):
    return {"rate_limit_tier": rate_limit_tier, "plan": plan, "user_plan": user_plan}


def _key(prefix):
    return prefix + secrets.token_urlsafe(24)


def _fresh(key):
    """Resolve as a new process would: _key_store keeps a tier for the life of
    the process, so a row change is seen on the next start, not the next call."""
    mg._key_store.clear()
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers={"X-API-Key": key},
                                  environ_base=EXTERNAL):
        caller = tier_gate._resolve_caller_tier()[0]
    mg._key_store.clear()
    return mg.resolve_tier(key), caller


def test_every_plan_name_maps_through_tier_registry():
    got = {n: mg._tier_of_row(n, None, None) for n in NAMES}
    members = {m.lower() for m in mg.Tier.__members__}
    for n, t in got.items():
        api = tier_registry.api_tier(n)
        if api in members:
            assert t == mg.Tier[api.upper()], (n, api, t)
        assert (t >= mg.Tier.STARTER) == tier_registry.is_paid(n), (n, t)
    for a, b in itertools.permutations(NAMES, 2):
        if tier_registry.rank(a) == tier_registry.rank(b):
            assert got[a] == got[b], (a, b, got[a], got[b])
        elif tier_registry.rank(a) < tier_registry.rank(b):
            assert got[a] <= got[b], (a, b, got[a], got[b])
    # The aliases cover exactly the api tiers that are not Tier names, so a new
    # api tier in TIERS fails here instead of raising KeyError at runtime.
    apis = {tier_registry.api_tier(n) for n in NAMES}
    assert apis - members == set(mg._API_TIER_ALIASES)


@pytest.mark.parametrize("name", NAMES)
def test_rate_limit_tier_then_plan_then_users_plan(name):
    want = mg._tier_of_row(name, None, None)
    other = "free" if want != mg.Tier.FREE else "enterprise"
    assert mg._tier_of_row(other, None, None) != want
    assert mg._tier_of_row(name.upper(), other, other) == want
    for absent in (None, "", "  "):
        assert mg._tier_of_row(name, other, other) == want
        assert mg._tier_of_row(absent, name, other) == want
        assert mg._tier_of_row(absent, absent, name) == want
        assert mg._tier_of_row(absent, absent, absent) == mg.Tier.FREE


def test_no_prefix_changes_the_tier_its_row_grants(db):
    wrong = []
    for prefix, name in itertools.product(PREFIXES, NAMES):
        key = _key(prefix)
        db.rows[key] = _row(name, name, name)
        want = mg._tier_of_row(name, None, None)
        got = mg.resolve_tier(key)
        if got != want:
            wrong.append((prefix, name, got.name, want.name))
    assert not wrong, wrong[:10]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_key_with_no_active_row_is_free(db, prefix):
    assert _fresh(_key(prefix)) == (mg.Tier.FREE, "FREE")


@pytest.mark.parametrize("plan", PAID)
def test_the_billing_lifecycle_reaches_a_key_named_for_its_plan(db, plan):
    key = _key("dchub_%s_" % plan)
    paid = mg._tier_of_row(plan, None, None)
    assert paid >= mg.Tier.STARTER
    states = (
        # generate_api_key / partner_key_issuer: rate_limit_tier = api_tier(plan)
        ("minted", _row(tier_registry.api_tier(plan), plan, plan), paid),
        # handle_payment_failed: rate_limit_tier = 'free', plans untouched
        ("dunning-demoted", _row("free", plan, plan), mg.Tier.FREE),
        # handle_invoice_paid: rate_limit_tier = plan
        ("restored", _row(plan, plan, plan), paid),
        # handle_subscription_deleted: rate_limit_tier and users.plan = 'free'
        ("canceled", _row("free", plan, "free"), mg.Tier.FREE),
    )
    for state, row, want in states:
        db.rows[key] = row
        assert _fresh(key) == (want, mg.TIER_NAME[want].upper()), state


@pytest.mark.parametrize("plan", PAID)
def test_a_dunning_demoted_key_fails_the_gate_its_plan_passed(db, plan, monkeypatch):
    monkeypatch.setattr(tier_gate, "_gate_response",
                        lambda *a, **k: (flask.jsonify(gated=True), 402))
    need = mg.TIER_NAME[mg._tier_of_row(plan, None, None)].upper()
    app = flask.Flask(__name__)

    @app.route("/key-tier-row/paid")
    @tier_gate.require_tier(need)
    def paid():
        return "full"

    key = _key("dchub_%s_" % plan)
    for row, ok in ((_row(plan, plan, plan), True), (_row("free", plan, plan), False)):
        db.rows[key] = row
        mg._key_store.clear()
        got = app.test_client().get("/key-tier-row/paid", headers={"X-API-Key": key},
                                    environ_base=EXTERNAL).status_code
        assert got == (200 if ok else 402), (row, got)
        mg._key_store.clear()
        with app.test_request_context("/", headers={"X-API-Key": key},
                                      environ_base=EXTERNAL):
            assert tier_gate.caller_is_privileged(need) is ok, row


def test_developer_keys_on_enterprise_rows_are_enterprise(db, monkeypatch):
    """The reveal-nlr shape: dchub_developer_ keys, every column enterprise."""
    monkeypatch.setattr(tier_gate, "_gate_response",
                        lambda *a, **k: (flask.jsonify(gated=True), 402))
    key = _key("dchub_developer_")
    db.rows[key] = _row("enterprise", "enterprise", "enterprise")
    assert _fresh(key) == (mg.Tier.ENTERPRISE, "ENTERPRISE")
    app = flask.Flask(__name__)

    @app.route("/key-tier-row/ent")
    @tier_gate.require_tier("ENTERPRISE")
    def ent():
        return "full"

    mg._key_store.clear()
    assert app.test_client().get("/key-tier-row/ent", headers={"X-API-Key": key},
                                 environ_base=EXTERNAL).status_code == 200
    mg._key_store.clear()
    with app.test_request_context("/", headers={"X-API-Key": key},
                                  environ_base=EXTERNAL):
        assert tier_gate.caller_is_privileged("ENTERPRISE") is True
