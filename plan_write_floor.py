"""The no-downgrade floor for CHECKOUT-path plan writes, shared by both writers.

WHY THIS MODULE EXISTS — measured, not imagined. #4724 added a floor to
main.py's `handle_checkout_completed` after a real $10.88 credit-pack purchase
(cs_live_a1H3pe…QbC1MuHlXex) moved admin001 from plan='pro' to plan='starter'
at 2026-09-17 18:35:34 and took 24 api_keys rows down with it.

That floor guarded ONE writer. `users.plan` is written on a checkout by TWO
live routes:

  * POST /api/v1/stripe/webhook + /api/stripe/webhook  -> main.py
    handle_checkout_completed                           (floored by #4724)
  * POST /api/v2/stripe/webhook                        -> api_tier_gating.py
    _handle_checkout_v2                                 (UNFLOORED until now)

Both are registered in the running app — `init_tier_gating` (main.py:37448)
calls `register_stripe_v2_routes` (api_tier_gating.py:1655), and both paths
answer 405 to GET in production, i.e. they are routed, not dead code.

The v2 writer's own downgrade vector is wider than the one that actually fired:
`_map_stripe_plan_to_tier` returns 'pro' for ANY unrecognised plan key, so a
checkout whose metadata.plan is missing or renamed demotes an `enterprise`
(rank 5) account to `pro` (rank 4) with no bad amount band required.

CHECKOUT-ONLY, deliberately. A genuine paid downgrade arrives as
customer.subscription.updated / .deleted; those handlers are NOT floored and
must stay that way, or a churned account is stranded on a paid tier.
"""
from __future__ import annotations


def plan_rank(plan) -> int:
    """Rank of a plan name, or -1 when unknown/unreadable.

    Unknown ranks -1 so an unrecognised INCOMING plan can never out-rank a
    real held plan, and an unrecognised HELD plan never blocks a write.
    """
    try:
        from tier_registry import TIERS
        return (TIERS.get((plan or '').strip().lower()) or {}).get('rank', -1)
    except Exception:  # noqa: BLE001 - fail-soft: provisioning must not break
        return -1


def keep_higher_plan(current_plan, incoming_plan):
    """Return the plan a checkout should actually write.

    The incoming plan wins, EXCEPT when the account already holds a strictly
    higher-ranked plan — then the held plan is kept. Equal ranks write the
    incoming plan, so a same-rank swap (pro -> founding, both rank 4) is still
    a real plan change and still lands.

    Fail-soft: with no current plan to compare against, the incoming plan wins.
    """
    cur = (current_plan or '').strip().lower()
    if not cur:
        return incoming_plan
    if plan_rank(cur) > plan_rank(incoming_plan):
        return cur
    return incoming_plan


def would_downgrade(current_plan, incoming_plan) -> bool:
    """True when writing `incoming_plan` would lower `current_plan`."""
    return keep_higher_plan(current_plan, incoming_plan) != incoming_plan
