"""Customer-facing wording for the Pro 7-day trial (r-trial-copy, 2026-09-24).

The live trial gate (2026-09-24) found every email a trialist gets written for a
PAYMENT: the receipt said "Thanks, payment received, 0.00 USD", the key welcome
said "Welcome to DC Hub Paid:Mint!" (an internal provenance tag) with
"10,000 API calls/day" (Pro is 2,000), and the upgrade welcome said "Your
Upgrade is Active". None said a trial had started, when the first $99 lands,
or how to cancel.

This module is the one place that wording lives:
  * trial_end_for_checkout(session) — the trial's end (unix seconds) for the
    trial checkout, else None. One Stripe read, cached per session.
  * trial_end_for_subscription(sub) — the same from a subscription object.
  * trial_block_html(trial_end) — the paragraph every trial email carries.
  * plan_display(plan_name, trial_end) / daily_calls(plan_name, trial_end) —
    the plan label and call limit for the welcome, from tier_registry canon.

Never raises: a failed lookup returns None and the email keeps its paid wording.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

log = logging.getLogger("trial_copy")

TRIAL_PLAN = "pro"
TRIAL_DAYS = 7
CANCEL_URL = "https://dchub.cloud/dashboard"

_CACHE: dict = {}


def _price() -> str:
    try:
        from tier_registry import price_display
        return price_display(TRIAL_PLAN)
    except Exception:  # noqa: BLE001
        return "the Pro price"


def trial_end_for_subscription(sub) -> int | None:
    try:
        if not sub or (sub.get("status") or "") != "trialing":
            return None
        te = sub.get("trial_end")
        return int(te) if te else None
    except Exception:  # noqa: BLE001
        return None


def trial_end_for_checkout(session) -> int | None:
    """trial_end of the trial checkout's subscription, if it is trialing."""
    try:
        from routes.pro_trial_guard import is_trial_offer
        if not isinstance(session, dict) or not is_trial_offer(session):
            return None
        sid = session.get("id") or ""
        if sid and sid in _CACHE:
            return _CACHE[sid]
        sub = session.get("subscription")
        if not isinstance(sub, dict):
            import stripe
            key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
            if not key:
                return None
            sub = stripe.Subscription.retrieve(str(sub), api_key=key)
        te = trial_end_for_subscription(sub)
        if sid:
            if len(_CACHE) > 256:
                _CACHE.clear()
            _CACHE[sid] = te
        return te
    except Exception as e:  # noqa: BLE001
        log.warning("trial_copy: trial lookup failed: %s", e)
        return None


def trial_end_date(trial_end) -> str:
    """'October 1, 2026' (UTC), or '' when unknown."""
    try:
        d = _dt.datetime.fromtimestamp(int(trial_end), _dt.timezone.utc)
        return d.strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:  # noqa: BLE001
        return ""


def trial_block_html(trial_end) -> str:
    when = trial_end_date(trial_end)
    first = ("Pro is %s from <strong>%s</strong> unless you cancel before then."
             % (_price(), when) if when else
             "Pro is %s when the trial ends unless you cancel before then." % _price())
    return ("<div style='border:1px solid #d6e4ff;background:#f3f7ff;border-radius:10px;"
            "padding:14px 18px;margin:16px 0;'>"
            "<p style='margin:0 0 6px;font-weight:600;'>Your %d-day Pro trial has started.</p>"
            "<p style='margin:0 0 6px;'>Nothing was charged today. %s</p>"
            "<p style='margin:0;'>Cancel anytime: <a href='%s'>dchub.cloud/dashboard</a> "
            "&rarr; Manage billing, or reply to this email.</p></div>"
            % (TRIAL_DAYS, first, CANCEL_URL))


def _base_plan(plan_name) -> str:
    # plan_name can carry provenance ("paid:mint", "paid:upgrade") for the send
    # log; the customer sees only the plan.
    return str(plan_name or "").split(":", 1)[0].strip().lower().replace("_", " ")


def plan_display(plan_name, trial_end=None) -> str:
    if trial_end:
        return "Pro (%d-day free trial)" % TRIAL_DAYS
    base = _base_plan(plan_name)
    if base == "paid":
        # mcp_dev_keys.tier 'paid' cannot say Developer vs Pro vs founding.
        return "Paid"
    try:
        from tier_registry import TIERS, label
        if base.replace(" ", "_") in TIERS:
            return label(base.replace(" ", "_"))
    except Exception:  # noqa: BLE001
        pass
    return base.title() if base else "DC Hub"


def daily_calls(plan_name, trial_end=None) -> int | None:
    """Calls/day from tier_registry for a plan it knows, else None (omit)."""
    base = TRIAL_PLAN if trial_end else _base_plan(plan_name).replace(" ", "_")
    try:
        from tier_registry import TIERS, calls_per_day
        if base in TIERS:
            n = calls_per_day(base)
            return int(n) if n else None
    except Exception:  # noqa: BLE001
        pass
    return None


# r-repeat-receipt (2026-09-24, live gate run 3). A repeat trial is ended now
# by routes/pro_trial_guard; Stripe then charges the first month on the
# subscription's latest invoice. The receipt must state THAT charge.
def repeat_charge_for(sub_id) -> dict:
    """{'amount_cents', 'currency'} of the ended trial's first real charge.

    Reads the subscription's latest invoice (amount_paid, else amount_due). On
    any failure returns {} and the receipt says the subscription started at the
    Pro list price without claiming an exact amount. Never raises."""
    if not sub_id:
        return {}
    try:
        import stripe
        key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
        if not key:
            return {}
        sub = stripe.Subscription.retrieve(str(sub_id), api_key=key,
                                           expand=["latest_invoice"])
        inv = sub.get("latest_invoice") if hasattr(sub, "get") else None
        if not hasattr(inv, "get"):
            return {}
        cents = inv.get("amount_paid") or inv.get("amount_due")
        if not isinstance(cents, int) or cents <= 0:
            return {}
        return {"amount_cents": cents, "currency": inv.get("currency") or "usd"}
    except Exception as e:  # noqa: BLE001
        log.warning("trial_copy: repeat charge lookup failed: %s", e)
        return {}


def repeat_block_html(charge) -> str:
    charge = charge or {}
    cents = charge.get("amount_cents")
    if isinstance(cents, int) and cents > 0:
        amount = "$%s" % f"{cents / 100:,.2f}"
        what = "%s charged today, renews monthly" % amount
    else:
        what = "billed at %s, renews monthly" % _price()
    return ("<div style='border:1px solid #d6e4ff;background:#f3f7ff;border-radius:10px;"
            "padding:14px 18px;margin:16px 0;'>"
            "<p style='margin:0 0 6px;font-weight:600;'>You've already used your free "
            "trial, so your Pro subscription started today.</p>"
            "<p style='margin:0 0 6px;'>%s.</p>"
            "<p style='margin:0;'>Cancel anytime: <a href='%s'>dchub.cloud/dashboard</a> "
            "&rarr; Manage billing, or reply to this email.</p></div>"
            % (what[0].upper() + what[1:], CANCEL_URL))
