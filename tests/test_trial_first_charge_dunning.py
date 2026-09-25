"""A trialist whose first real charge fails is demoted at once (r-trial-dunning).

Owner decision 2026-09-24 ("demote now"). Before: subscription.updated ->
past_due only records the status; the never-paid dunning guard waited for 2
failures; and "paid" meant status='paid', so the PAID $0 invoice Stripe issues
at trial start made a trialist a "prior payer" (the 4-failure / ~21-day path).
A trialist with a failing card kept Pro for days to weeks.

Now:
  * a payer is someone with an invoice that took money (amount_paid > 0), in
    handle_invoice_paid's count, its no-Stripe fallback, and
    handle_payment_failed's Stripe cross-check;
  * the FIRST failed post-trial charge of a subscription that had a trial
    demotes a never-paid customer through the existing reversible
    'first_charge_never_succeeded' path (invoice.paid restores it);
  * customers who really paid, and never-paid customers without a trial, keep
    today's thresholds.

Driven through the real handlers' SQL on sqlite (the dunning harness), with a
fake Stripe.
"""
import copy

import pytest

from tests.test_dunning_stamp_overwrites_a_stale_reason import (
    CUSTOMER, _Harness, _load)


class _Obj(dict):
    pass


class _FakeStripe:
    def __init__(self, amounts=(), trial_end=1790000000, list_raises=False, retrieve_raises=False):
        outer = self

        class Invoice:
            @staticmethod
            def list(customer=None, status=None, limit=None):
                if list_raises:
                    raise RuntimeError("stripe down")
                return type("L", (), {"data": [_Obj(amount_paid=a) for a in amounts]})()

        class Subscription:
            @staticmethod
            def retrieve(sub_id):
                outer.retrieved.append(sub_id)
                if retrieve_raises:
                    raise RuntimeError("stripe down")
                return _Obj(id=sub_id, trial_end=trial_end)

        self.Invoice, self.Subscription, self.retrieved = Invoice, Subscription, []


def _ns(h, stripe_obj, available=True):
    import sqlite3
    mirror = sqlite3.connect(":memory:")
    mirror.executescript(
        "CREATE TABLE users (stripe_customer_id TEXT, subscription_status TEXT,"
        " payment_failed_count INTEGER, invoices_paid_count INTEGER);")
    return {
        "STRIPE_AVAILABLE": available, "stripe": stripe_obj,
        "_pg_execute": h.pg_execute, "get_db": lambda: mirror,
        "_sync_tables_bg": lambda *a, **k: None,
        "note_swallowed_write": lambda *a, **k: None,
        "utc_iso_z": lambda: "2026-09-24T00:00:00Z",
        "_send_dunning_demote_notice": lambda *a, **k: h.notices.append(a),
    }


def _harness(stripe_obj, available=True):
    h = _Harness()
    ns = _ns(h, stripe_obj, available)
    h.failed = _load("handle_payment_failed", ns)
    h.paid = _load("handle_invoice_paid", ns)
    h.count = _load("_real_paid_invoice_count", ns)
    h.post_trial = _load("_is_post_trial_first_charge", ns)
    return h


def _failed_invoice(reason="subscription_cycle", sub="sub_trial"):
    return {"id": "in_1", "customer": CUSTOMER, "attempt_count": 1,
            "billing_reason": reason, "subscription": sub}


def _seed(h, paid=0, failed=0):
    h.add_user(1, reason=None, demoted_at=None, paid=paid, failed=failed)


# ── counting: money taken, not status='paid' ──────────────────────────────

@pytest.mark.parametrize("amounts,n", [((0,), 0), ((0, 9900), 1), ((9900, 9900), 2), ((), 0)])
def test_only_invoices_that_took_money_count(amounts, n):
    assert _harness(_FakeStripe(amounts)).count(CUSTOMER) == n


def test_no_stripe_or_an_error_is_unknown_not_zero():
    assert _harness(_FakeStripe(), available=False).count(CUSTOMER) is None
    assert _harness(_FakeStripe(list_raises=True)).count(CUSTOMER) is None


def test_the_trial_start_invoice_does_not_make_a_payer():
    """invoice.paid for the $0 trial-start invoice: the count stays 0."""
    h = _harness(_FakeStripe(amounts=(0,)))
    _seed(h)
    h.paid({"customer": CUSTOMER, "amount_paid": 0})
    assert h.db.execute("SELECT invoices_paid_count FROM users").fetchone()[0] == 0


@pytest.mark.parametrize("amount,delta", [(0, 0), (9900, 1)])
def test_the_no_stripe_fallback_counts_only_money(amount, delta):
    h = _harness(_FakeStripe(), available=False)
    _seed(h)
    h.paid({"customer": CUSTOMER, "amount_paid": amount})
    assert h.db.execute("SELECT invoices_paid_count FROM users").fetchone()[0] == delta


# ── which failure is a post-trial first charge ────────────────────────────

@pytest.mark.parametrize("inv,stripe_kw,want", [
    (_failed_invoice("subscription_cycle"), {}, True),                  # trial ended naturally
    (_failed_invoice("subscription_update"), {}, True),                 # trial ended now (repeat guard)
    (_failed_invoice("subscription_cycle"), {"trial_end": None}, False),  # never had a trial
    (_failed_invoice("manual"), {}, False),
    (_failed_invoice("subscription_cycle", sub=""), {}, False),
    (_failed_invoice("subscription_cycle"), {"retrieve_raises": True}, False),  # fail-safe
], ids=["cycle", "update", "no_trial", "manual", "no_sub", "stripe_error"])
def test_post_trial_first_charge_detection(inv, stripe_kw, want):
    assert _harness(_FakeStripe(**stripe_kw)).post_trial(inv) is want


# ── the demote ────────────────────────────────────────────────────────────

def test_a_trialist_whose_first_charge_fails_is_demoted_at_once():
    h = _harness(_FakeStripe(amounts=(0,)))           # only the $0 trial invoice
    _seed(h)
    h.failed(_failed_invoice())
    u = h.user(1)
    assert u["demoted_reason"] == "first_charge_never_succeeded", u
    assert h.key(1) == {"rate_limit_tier": "free", "plan": "pro"}, "plan kept for restore"


def test_the_first_real_payment_restores_it():
    h = _harness(_FakeStripe(amounts=(0,)))
    _seed(h)
    h.failed(_failed_invoice())
    assert h.key(1)["rate_limit_tier"] == "free"
    h2 = _harness(_FakeStripe(amounts=(0, 9900)))    # Stripe's retry succeeded
    h2.db = h.db
    h2.paid({"customer": CUSTOMER, "amount_paid": 9900})
    assert h2.key(1)["rate_limit_tier"] == "pro"
    assert h2.user(1)["demoted_reason"] is None


def test_a_never_paid_customer_without_a_trial_keeps_the_two_failure_rule():
    h = _harness(_FakeStripe(amounts=(), trial_end=None))
    _seed(h)
    h.failed(_failed_invoice())
    assert h.user(1)["demoted_reason"] is None
    h.failed(_failed_invoice())
    assert h.user(1)["demoted_reason"] == "first_charge_never_succeeded"


def test_a_customer_who_really_paid_is_not_demoted_on_one_failure():
    """A real $99 payment makes them a prior payer: today's 4-failure path."""
    h = _harness(_FakeStripe(amounts=(0, 9900)))
    _seed(h, paid=1)
    h.failed(_failed_invoice())
    assert h.user(1)["demoted_reason"] is None
    assert h.key(1)["rate_limit_tier"] == "pro"


def test_a_stripe_lookup_failure_keeps_the_existing_rule():
    h = _harness(_FakeStripe(amounts=(0,), retrieve_raises=True))
    _seed(h)
    h.failed(_failed_invoice())
    assert h.user(1)["demoted_reason"] is None
