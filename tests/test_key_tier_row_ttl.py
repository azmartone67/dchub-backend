"""resolve_tier reads a dchub_ key's api_keys row again once _ROW_TIER_TTL_S has
passed, and keeps the tier it last read when that read fails.

Until 2026-09-22 the tier was cached for the life of the process, so billing
moving the row (main.handle_payment_failed's dunning demote,
handle_subscription_deleted, handle_invoice_paid's restore) reached a process
only when it restarted. The real _resolve_from_db_hash runs, against a
connection that hands back the row held for the key or raises as a database
that is down does. The clock is mcp_gatekeeper._monotonic, so nothing sleeps.
"""
import json
import secrets
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

import mcp_gatekeeper as mg  # noqa: E402
import tier_registry  # noqa: E402

TTL = 300.0
PAID = tier_registry.paid_plan_names()


class _Db:
    """psycopg2.connect for _resolve_from_db_hash: the query's second parameter
    is the raw key, and the row held for that key is what fetchone returns."""

    def __init__(self):
        self.rows = {}
        self.down = False
        self.reads = 0
        self.now = 1000.0

    def clock(self):
        return self.now

    def connect(self, *_a, **_k):
        self.reads += 1
        if self.down:
            raise psycopg2.OperationalError("could not connect to server")
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
    monkeypatch.setattr(mg, "_row_tiers", {})
    monkeypatch.setattr(mg, "_ROW_TIER_TTL_S", TTL)
    monkeypatch.setattr(mg, "_monotonic", d.clock)
    return d


def _row(rate_limit_tier, plan, user_plan):
    return {"rate_limit_tier": rate_limit_tier, "plan": plan, "user_plan": user_plan}


def _key(prefix):
    return prefix + secrets.token_urlsafe(24)


@pytest.mark.parametrize("plan", PAID)
def test_a_row_change_is_seen_once_the_ttl_passes_and_not_before(db, plan):
    key = _key("dchub_%s_" % plan)
    paid = mg._tier_of_row(plan, None, None)
    assert paid >= mg.Tier.STARTER
    db.rows[key] = _row(tier_registry.api_tier(plan), plan, plan)   # minted
    assert mg.resolve_tier(key) == paid
    read_at = db.now
    lifecycle = (
        # handle_payment_failed: rate_limit_tier = 'free', plans untouched
        ("dunning-demoted", _row("free", plan, plan), paid, mg.Tier.FREE),
        # handle_invoice_paid: rate_limit_tier = plan
        ("restored", _row(plan, plan, plan), mg.Tier.FREE, paid),
        # handle_subscription_deleted: rate_limit_tier and users.plan = 'free'
        ("canceled", _row("free", plan, "free"), paid, mg.Tier.FREE),
    )
    for state, row, before, after in lifecycle:
        db.rows[key] = row
        reads = db.reads
        db.now = read_at + TTL - 1
        assert mg.resolve_tier(key) == before, state
        assert db.reads == reads, state          # served without reading the row
        db.now = read_at + TTL
        assert mg.resolve_tier(key) == after, state
        assert db.reads == reads + 1, state
        read_at = db.now


def test_a_revoked_key_is_free_once_the_ttl_passes(db):
    key = _key("dchub_pro_")
    db.rows[key] = _row("pro", "pro", "pro")
    assert mg.resolve_tier(key) == mg.Tier.PRO
    del db.rows[key]                             # no active row
    db.now += TTL - 1
    assert mg.resolve_tier(key) == mg.Tier.PRO
    db.now += 1
    assert mg.resolve_tier(key) == mg.Tier.FREE
    # No row is not kept: a row that appears is read on the next call.
    db.rows[key] = _row("pro", "pro", "pro")
    assert mg.resolve_tier(key) == mg.Tier.PRO


@pytest.mark.parametrize("plan", PAID)
def test_a_failed_read_at_expiry_keeps_the_tier_it_last_read(db, plan):
    key = _key("dchub_%s_" % plan)
    paid = mg._tier_of_row(plan, None, None)
    db.rows[key] = _row(plan, plan, plan)
    assert mg.resolve_tier(key) == paid
    reads = db.reads
    db.rows[key] = _row("free", plan, plan)      # demoted while the DB is down
    db.down = True
    db.now += TTL
    assert mg.resolve_tier(key) == paid          # not FREE: the row is unknown
    assert db.reads == reads + 1                 # it did try the row
    # The next try is a TTL after the failed one, not on every call.
    db.now += TTL - 1
    assert mg.resolve_tier(key) == paid
    assert db.reads == reads + 1
    db.down = False
    db.now += 1
    assert mg.resolve_tier(key) == mg.Tier.FREE  # the first read that works decides
    assert db.reads == reads + 2


def test_a_failed_read_with_nothing_kept_is_free_and_is_not_kept(db):
    key = _key("dchub_pro_")
    db.rows[key] = _row("pro", "pro", "pro")
    db.down = True
    assert mg.resolve_tier(key) == mg.Tier.FREE
    db.down = False
    assert mg.resolve_tier(key) == mg.Tier.PRO   # read on the next call, no TTL wait
    assert db.reads == 2


def test_a_dchub_api_keys_key_never_expires_and_never_reads_a_row(db, monkeypatch):
    key = _key("dchub_pro_")
    monkeypatch.setenv("DCHUB_API_KEYS", key + ":pro")
    mg._load_keys_from_env()
    db.rows[key] = _row("free", "free", "free")  # a row that says otherwise
    for _ in range(3):
        assert mg.resolve_tier(key) == mg.Tier.PRO
        db.now += 10 * TTL
    db.down = True
    assert mg.resolve_tier(key) == mg.Tier.PRO
    assert db.reads == 0
    assert key not in mg._row_tiers


def test_dchub_key_tier_ttl_s_sets_the_ttl():
    code = textwrap.dedent("""
        import importlib, json, os
        out = {}
        for raw in (None, "45", "0", "-5", "soon"):
            os.environ.pop("DCHUB_KEY_TIER_TTL_S", None)
            if raw is not None:
                os.environ["DCHUB_KEY_TIER_TTL_S"] = raw
            import mcp_gatekeeper
            out[str(raw)] = importlib.reload(mcp_gatekeeper)._ROW_TIER_TTL_S
        print(json.dumps(out))
    """)
    r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"None": 300.0, "45": 45.0, "0": 0.0, "-5": 0.0, "soon": 300.0}
