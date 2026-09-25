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

Follow-ups (owner, 2026-09-25, after #5565):
  * a Stripe failure during a cancel's check must NOT demote: the caller raises
    CheckUnavailable and the webhook answers 5xx so Stripe redelivers and the
    check re-runs. Only a clean "no other live subscription" demotes.
  * a re-point sets the plan from the KEPT subscription's price (Dev $49 plus a
    canceled Pro trial lands on Developer, not Pro): plan_from_subscription and
    repoint_statements.
"""
from __future__ import annotations

import os

LIVE = ("active", "trialing", "past_due")

# Pro's own $99/mo price (routes/_stripe_links.py, r-price-collapse 2026-09-05).
# $99 is also the founding amount, so the amount bands alone read Pro as
# founding; the id says which it is.
PRO_PRICE_ID = "price_1UCTZDJ9ey2ATcQlrpMPBVWf"
FOUNDING_PRICE_ID = "price_1Tml5XJ9ey2ATcQl0pbU4htM"


class CheckUnavailable(RuntimeError):
    """Stripe could not answer whether another live subscription exists."""


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
    """(customer_id, status, subscription) of the newest live subscription,
    other than ended_sub_id, under any customer whose email is one of
    `emails`; None when there is none."""
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
                    best = (created, cid, _get(s, "status"), s)
    return (best[1], best[2], best[3]) if best else None


def customer_has_live_subscription(customer_id, *, stripe_mod=None) -> bool:
    if not customer_id:
        return False
    return bool(_live_subs(_stripe(stripe_mod), customer_id))


def plan_from_subscription(subscription):
    """(plan_name, api_tier, amount_dollars) from the subscription's first
    price: Pro's and founding's own price ids first, then the amount bands
    handle_checkout_completed uses. (None, None, amt) when unrecognised, so an
    odd amount never mints or moves a tier. (Moved from main.py
    _resolve_plan_from_subscription, which now delegates here.)"""
    try:
        _item = ((_get(subscription, "items") or {}).get("data") or [{}])[0] or {}
        _price = _get(_item, "price") or _get(_item, "plan") or {}
        _price_id = _get(_price, "id") or ""
        _unit = _get(_price, "unit_amount")
        if _unit is None:
            _unit = _get(_price, "amount")  # legacy 'plan' object shape
        amt = (_unit or 0) / 100.0
    except Exception:  # noqa: BLE001
        return None, None, 0
    founding = os.environ.get("STRIPE_PRICE_FOUNDING", FOUNDING_PRICE_ID)
    if _price_id and _price_id in (founding, FOUNDING_PRICE_ID):
        return "founding", "pro", amt
    if _price_id == PRO_PRICE_ID:
        return "pro", "pro", amt
    if 8 <= amt <= 11:
        return "starter", "starter", amt
    if 45 <= amt <= 55:
        return "developer", "developer", amt
    if 95 <= amt <= 105:
        return "founding", "pro", amt
    if (195 <= amt <= 205) or (295 <= amt <= 305):
        return "pro", "pro", amt
    if 695 <= amt <= 705:
        return "enterprise", "enterprise", amt
    return None, None, amt


def repoint_statements(ended_customer, keep):
    """[(sql, params)] that move the accounts on `ended_customer` onto the kept
    subscription `keep` = (customer_id, status, subscription), in order.

    Keys first (they find the account through the ended customer), the users
    row last. When the kept price is recognised, the plan, a non-admin role
    and the active api_keys tiers follow it; when it is not, only the customer
    and status move (a price nobody recognises must not set a plan).

    MCP keys only ever go DOWN here (enterprise -> paid when the kept plan is
    not Enterprise). Their rows are chosen by the account's address, and an
    address-chosen GRANT needs the proof clause
    (tests/test_tier_grants_are_classified.py). A kept Enterprise plan still
    serves Enterprise: validate_key reads the highest of the key, the users row
    and api_keys, and the users row is set here."""
    live_customer, live_status, live_sub = keep
    plan, api_tier, _amt = plan_from_subscription(live_sub)
    on_ended = "SELECT id FROM users WHERE stripe_customer_id = %s"
    stmts = []
    if plan:
        stmts.append((
            "UPDATE api_keys SET plan = %s, rate_limit_tier = %s "
            "WHERE is_active = 1 AND user_id IN (" + on_ended + ")",
            (plan, api_tier, ended_customer)))
        if api_tier != "enterprise":
            stmts.append((
                "UPDATE mcp_dev_keys SET tier = 'paid' WHERE tier = 'enterprise' "
                "AND (LOWER(email) IN (SELECT LOWER(email) FROM users WHERE stripe_customer_id = %s) "
                "OR metadata->>'stripe_customer_id' = %s)",
                (ended_customer, ended_customer)))
        stmts.append((
            "UPDATE users SET stripe_customer_id = %s, subscription_status = %s, plan = %s, "
            "role = CASE WHEN role = 'admin' THEN role ELSE %s END "
            "WHERE stripe_customer_id = %s",
            (live_customer, live_status, plan, api_tier, ended_customer)))
    else:
        stmts.append((
            "UPDATE users SET stripe_customer_id = %s, subscription_status = %s "
            "WHERE stripe_customer_id = %s",
            (live_customer, live_status, ended_customer)))
    return stmts, plan
