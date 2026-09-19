"""_checkout_plan_guard.py — two rules a checkout must obey before it writes a plan.

WHY THIS EXISTS (2026-09-18, owner-reported)
============================================
azmartone@gmail.com held `pro`, bought the $10 one-time credit pack through an
agent unlock link, and came out of `handle_checkout_completed` as `starter`.
Two independent defects composed:

  1. THE PACK IS NOT A PLAN. The $10/1,000-call pack is a CREDIT grant
     (routes/mcp_conversion_plays.grant_credit_pack) — it buys calls, not a
     tier. But `handle_checkout_completed` resolves a plan from the AMOUNT
     for any checkout whose payment_link is unmapped, and the $9-Starter band
     added on 2026-06-22 is `8 <= dollars <= 11`. r-pack10 repriced the pack
     from $5 to $10 three days later, on 2026-06-25, and walked straight into
     that band. $5 had fallen through to the safe 'free' default; $10 reads as
     Starter. Nothing in either change could see the other.

  2. THE PLAN WRITE WAS UNCONDITIONAL. `UPDATE users SET plan = %s ... WHERE
     email = %s` with no comparison against what the account already had, so
     ANY cheaper checkout — a mis-banded pack, a genuine $9 Starter bought by
     a Pro account for a colleague — silently demotes a paying customer.

Rule 1 alone leaves a NEW buyer of a $10 pack provisioned as Starter (500 REST
calls/day, 200 MCP/day, forever, for $10 once) — the mirror of the $9→Pro leak
r-starter9 closed. Rule 2 alone leaves the mis-banding in place and only
protects accounts that already outrank it. Both, or the hole stays open on one
side.

WHAT THIS IS NOT. Neither rule touches `customer.subscription.updated` /
`.deleted` — those are where a REAL downgrade or cancellation arrives, they
have their own handlers, and they must keep working. This module is only about
`checkout.session.completed`, where the event means "someone just paid us".
"""
from __future__ import annotations

# ONE price canon. The grant path (main.py's pack branch) imports these same
# two names from the same module; a detector with its own copy of the numbers
# is how the grant and the plan write come to disagree about what a pack is.
from routes.mcp_conversion_plays import PACK5_PRICE_CENTS, PACK10_PRICE_CENTS
from tier_registry import TIERS, rank as tier_rank


def pack_price_cents() -> frozenset:
    """Every one-time credit-pack price, read live (both are env-overridable)."""
    return frozenset({int(PACK5_PRICE_CENTS), int(PACK10_PRICE_CENTS)})


def is_credit_pack_checkout(session) -> bool:
    """True iff this completed checkout bought CREDITS, not a subscription tier.

    Mirrors the grant branch's own predicate: mode='payment' and a pack price
    on the PRE-TAX subtotal or the total. The subtotal is the load-bearing half
    — Stripe Tax pushes amount_total above the price (the r-pack5-tax-fix
    lesson: $5 + 8.8% AZ tax = 544), and a taxed $10 pack lands at $10.88,
    which is still inside the 8..11 Starter band. Matching only amount_total
    would leave every taxed pack mis-tiered.

    Deliberately NOT gated on client_reference_id. The grant is split across
    two branches by ref shape (pk- keybound vs session-bound), and both of them
    bought credits; a plan guard that reproduced that split would protect one
    kind of pack buyer and not the other.
    """
    try:
        if (session.get('mode') or '').lower() != 'payment':
            return False
        prices = pack_price_cents()
        sub = int(session.get('amount_subtotal') or 0)
        amt = int(session.get('amount_total') or 0)
        return bool(prices & {sub, amt})
    except Exception:
        return False


def _api_tier_for(plan: str) -> str:
    entry = TIERS.get(str(plan or '').strip().lower())
    return entry['api_tier'] if entry else 'free'


def resolve_plan_write(session, plan_name, api_tier, current_plan):
    """→ (plan_name, api_tier, note) — what this checkout may write.

    `note` is None when the resolved plan is written as-is, and otherwise a
    short human string naming which rule fired, for the log line and the admin
    alert. Returning the reason rather than logging it here keeps this pure and
    testable with no database and no mailer.

    `current_plan` is whatever the account holds today ('' / None for a brand
    new account). An unknown tier name ranks as free, so a garbage value in the
    column can never pin an account above what it paid for.
    """
    current = str(current_plan or '').strip().lower()
    proposed = str(plan_name or '').strip().lower()

    if is_credit_pack_checkout(session):
        # A pack changes the credit balance and nothing else. Hold the account
        # exactly where it is; a brand-new buyer lands on free and gets what
        # they paid for (1,000 calls) through the credit rail.
        held = current if current in TIERS else 'free'
        if held != proposed:
            return (held, _api_tier_for(held),
                    'credit pack ($%.2f one-time) — plan held at %r, not %r'
                    % ((int(session.get('amount_subtotal') or session.get('amount_total') or 0)) / 100.0,
                       held, proposed))
        return (plan_name, api_tier, None)

    if current and tier_rank(current) > tier_rank(proposed):
        return (current, _api_tier_for(current),
                'checkout resolved to %r but account already holds %r — '
                'refusing to downgrade on a payment event' % (proposed, current))

    return (plan_name, api_tier, None)
