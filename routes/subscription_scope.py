"""Which Stripe subscription an account's access rests on (backend#5555, P1).

users has ONE stripe_customer_id, but Payment Links create a NEW Stripe
customer on every checkout, so one person can hold live subscriptions under
several customers. Two writers assumed one:

  * handle_checkout_completed overwrote stripe_customer_id (and role) on every
    checkout, re-pointing an account at the newest customer;
  * the cancel handlers demote every users row WHERE stripe_customer_id = the
    canceled customer.

Together: a second checkout, then cancelling it, demoted the account off its
FIRST, still-paid subscription. Measured 2026-09-25 04:18:59Z: cancelling a
test trial demoted admin001 and 21 of its MCP keys.

These helpers answer the one question both writers need, from Stripe:
  * other_live_subscription(emails, ended_sub) — another live subscription
    (active / trialing / past_due) held under any of the account's addresses;
  * customer_has_live_subscription(customer) — does this customer still pay.

They RAISE on a Stripe failure; each caller decides its own fail-safe.
"""
from __future__ import annotations

import os

LIVE = ("active", "trialing", "past_due")


def _stripe(stripe_mod=None):
    if stripe_mod is not None:
        return stripe_mod
    import stripe  # noqa: PLC0415
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    return stripe


def _get(obj, key, default=None):
    try:
        return obj.get(key, default) if hasattr(obj, "get") else getattr(obj, key, default)
    except Exception:  # noqa: BLE001
        return default


def _live_subs(stripe, customer_id):
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
    return [s for s in (_get(subs, "data") or []) if _get(s, "status") in LIVE]


def other_live_subscription(emails, ended_sub_id, *, stripe_mod=None):
    """(customer_id, status) of the newest live subscription, other than
    ended_sub_id, under any customer whose email is one of `emails`; None when
    there is none."""
    stripe = _stripe(stripe_mod)
    best = None
    seen = set()
    for email in emails or ():
        e = str(email or "").strip()
        if not e or e.lower() in seen:
            continue
        seen.add(e.lower())
        custs = stripe.Customer.list(email=e, limit=100)
        for c in (_get(custs, "data") or []):
            cid = _get(c, "id")
            for s in _live_subs(stripe, cid):
                if _get(s, "id") == ended_sub_id:
                    continue
                created = _get(s, "created") or 0
                if best is None or created > best[0]:
                    best = (created, cid, _get(s, "status"))
    return (best[1], best[2]) if best else None


def customer_has_live_subscription(customer_id, *, stripe_mod=None) -> bool:
    if not customer_id:
        return False
    return bool(_live_subs(_stripe(stripe_mod), customer_id))
