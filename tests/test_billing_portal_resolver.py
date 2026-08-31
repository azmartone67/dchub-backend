"""
r-portal-trap (2026-08-31) — the stranded-payer repair, pinned.

The bug being pinned: a customer who paid via a bare Stripe payment link has
users.stripe_customer_id = NULL, so the old single-read resolver 404'd and the
only self-serve cancel route was permanently closed while billing continued.

These tests use hand-rolled fakes (no DB, no network) so they run in CI
unconditionally. The fakes record the SQL they were handed, which is what lets
the backfill assertion be about behaviour rather than about a return value.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from routes.billing_portal import resolve_stripe_customer, _live_first


class FakeCursor:
    def __init__(self, outer): self.outer = outer
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.outer.executed.append((" ".join(sql.split()), params))
        self._sql = sql
    def fetchone(self):
        if "SELECT stripe_customer_id" in self._sql:
            return self.outer.user_row
        return None


class FakeConn:
    def __init__(self, user_row):
        self.user_row = user_row
        self.executed = []
        self.committed = 0
    def cursor(self): return FakeCursor(self)
    def commit(self): self.committed += 1
    def rollback(self): pass
    def close(self): pass


class FakeStripe:
    """Only the surface the resolver touches."""
    def __init__(self, customers): self._customers = customers; self.queried = []
    @property
    def Customer(self): return self
    def list(self, email=None, limit=None, expand=None):
        self.queried.append(email)
        return {"data": self._customers}


def _cust(cid, created, sub_status=None):
    subs = {"data": ([{"status": sub_status}] if sub_status else [])}
    return {"id": cid, "created": created, "subscriptions": subs}


# ---------------------------------------------------------------- happy path

def test_existing_column_is_used_and_stripe_is_never_queried():
    conn = FakeConn(("cus_LINKED", "a@example.com"))
    st = FakeStripe([])
    cid, reason, email = resolve_stripe_customer(st, conn, "u1")
    assert (cid, reason) == ("cus_LINKED", "column")
    assert st.queried == [], "must not hit Stripe when the column is already set"


# ------------------------------------------------------- THE REGRESSION PIN

def test_null_column_recovers_the_customer_by_email():
    """Alex's case: paying, but stripe_customer_id was never written."""
    conn = FakeConn((None, "Alex@Example.com "))
    st = FakeStripe([_cust("cus_PAYMENTLINK", 100, "active")])
    cid, reason, email = resolve_stripe_customer(st, conn, "u2")
    assert cid == "cus_PAYMENTLINK", "stranded payer must be recovered, not 404'd"
    assert reason == "email_backfill"
    assert st.queried == ["alex@example.com"], "email must be normalised before lookup"


def test_recovery_backfills_the_column_without_overwriting():
    conn = FakeConn((None, "a@example.com"))
    st = FakeStripe([_cust("cus_NEW", 100, "active")])
    resolve_stripe_customer(st, conn, "u3")
    updates = [sql for sql, _ in conn.executed if sql.startswith("UPDATE users")]
    assert len(updates) == 1, "the repair must persist so the next open is a column hit"
    assert "COALESCE(stripe_customer_id, '') = ''" in updates[0], \
        "backfill must be guarded so it can never clobber an existing link"


def test_live_subscription_wins_over_a_newer_dead_one():
    """Duplicate customers on one email: handing the portal a dead record shows
    an empty portal, which looks exactly like the bug being fixed."""
    ranked = _live_first([_cust("cus_DEAD", 999, None),
                          _cust("cus_LIVE", 1, "active")])
    assert ranked[0]["id"] == "cus_LIVE"


def test_newest_wins_when_none_are_live():
    ranked = _live_first([_cust("cus_OLD", 1, None), _cust("cus_NEW", 999, None)])
    assert ranked[0]["id"] == "cus_NEW"


# ------------------------------------------------------------ honest failure

def test_genuinely_unknown_customer_returns_no_id():
    conn = FakeConn((None, "nobody@example.com"))
    st = FakeStripe([])
    cid, reason, email = resolve_stripe_customer(st, conn, "u4")
    assert cid is None and reason is None
    assert email == "nobody@example.com", "email is returned for logging even on miss"


def test_stripe_outage_does_not_raise():
    class Boom(FakeStripe):
        def list(self, **kw): raise RuntimeError("stripe down")
    conn = FakeConn((None, "a@example.com"))
    cid, reason, _ = resolve_stripe_customer(Boom([]), conn, "u5")
    assert cid is None and reason is None
