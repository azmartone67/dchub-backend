"""A checkout may raise an account's plan. It may never lower one.

WHY THIS EXISTS — measured, not imagined. On 2026-09-17 18:35:34 a REAL
$10.88 one-time credit-pack purchase (cs_live_a1H3pe…QbC1MuHlXex,
amount_total=1088, mode=payment, livemode=t) moved admin001 from
plan='pro' to plan='starter', and took 24 api_keys rows down with it.

Two independent defects had to line up, so this pins both:

  1. CLASSIFICATION. handle_checkout_completed's $9-Starter amount band was
     `8 <= amount_dollars <= 11`. The $10 pack the relay/upgrade funnel sells
     bills at $10.88 with tax — inside the band. Every pack buyer was
     provisioned as a Starter SUBSCRIBER.
  2. NO FLOOR. The users.plan write was unconditional, so that lower plan
     simply overwrote the higher one. source_plan='pro_onetime' and
     tier_expires_at=2027-06-24 were left behind, still recording the Pro
     entitlement that had just been overwritten — which is how the incident
     was reconstructed at all.

Defect 2 is the general one: the floor holds for ANY future band or metadata
slip, not just this price point. Defect 1 is still fixed here because a pack
must not read as a plan even on an account with nothing to lose.

The floor is CHECKOUT-ONLY by design. A genuine paid downgrade arrives as
customer.subscription.updated / .deleted, and those handlers are deliberately
NOT floored — the last test pins that, so this guard can never be widened into
one that strands a churned account on a paid tier.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
SRC = MAIN.read_text(encoding="utf-8")


def _extract(func_name: str):
    """Exec ONE function out of main.py without importing it.

    main.py is ~50k lines with import-time side effects; this keeps the test to
    the function under test. If the function grows a new free name, this raises
    NameError at call time rather than passing vacuously — supply it below.
    """
    tree = ast.parse(SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {}
            exec(compile(mod, str(MAIN), "exec"), ns)  # noqa: S102
            return ns[func_name], ns
    raise AssertionError(f"{func_name} not found in main.py — renamed?")


def _floor_with(current_plan):
    """_plan_write_floor bound to a fake DB holding `current_plan`."""
    fn, ns = _extract("_plan_write_floor")
    calls = []

    def _fake_pg_execute(sql, params=(), fetch=False):
        calls.append((sql, params))
        if current_plan is None:
            return 0, []
        return 0, [(current_plan,)]

    ns["_pg_execute"] = _fake_pg_execute
    ns["send_admin_alert_email"] = lambda *a, **k: None
    return fn, calls


# ── 1. The floor itself ──────────────────────────────────────────────────

def test_pack_resolving_to_starter_cannot_lower_pro():
    """THE incident, replayed: held pro, checkout says starter → stays pro."""
    fn, _ = _floor_with("pro")
    assert fn("azmartone@gmail.com", None, "starter", "starter") == ("pro", "pro")


def test_floor_returns_the_api_tier_of_the_plan_it_keeps():
    """Keeping the plan but writing the LOWER api_tier would gate a Pro
    account at Starter limits — a silent half-downgrade."""
    fn, _ = _floor_with("founding")           # rank 4, api_tier 'pro'
    assert fn("x@y.com", None, "starter", "starter") == ("founding", "pro")


@pytest.mark.parametrize("held,incoming", [
    ("starter", "pro"),          # genuine upgrade
    ("free", "starter"),         # genuine first purchase
    ("pro", "pro"),              # idempotent Stripe retry
    ("pro", "enterprise"),       # upgrade off an already-paid plan
])
def test_floor_never_blocks_an_upgrade_or_a_retry(held, incoming):
    fn, _ = _floor_with(held)
    assert fn("x@y.com", None, incoming, incoming)[0] == incoming


def test_unknown_account_is_passed_through_not_floored():
    """No row = a first-time buyer. Floor must not invent a plan for them."""
    fn, _ = _floor_with(None)
    assert fn("new@buyer.com", None, "starter", "starter") == ("starter", "starter")


def test_floor_is_fail_soft_and_never_blocks_provisioning():
    """A DB error must not cost the customer the tier they just paid for."""
    fn, ns = _extract("_plan_write_floor")

    def _boom(*a, **k):
        raise RuntimeError("neon unreachable")

    ns["_pg_execute"] = _boom
    ns["send_admin_alert_email"] = lambda *a, **k: None
    assert fn("x@y.com", None, "pro", "pro") == ("pro", "pro")


# ── 2. The classification that started it ────────────────────────────────

def _starter_band_line():
    for line in SRC.splitlines():
        s = line.strip()
        if s.startswith("if amount_dollars == 9 or (8 <= amount_dollars"):
            return s
    raise AssertionError("the $9 Starter amount band is gone — reclassified?")


def test_ten_dollar_pack_is_outside_the_starter_band():
    """$10.88 (the $10 pack + tax) must not read as a $9 Starter plan.

    Evaluated, not pattern-matched: the band's own expression is run against
    the amount Stripe actually charged.
    """
    band = _starter_band_line()
    expr = band[len("if "):].rstrip(":")
    assert eval(expr, {}, {"amount_dollars": 10.88}) is False, \
        f"$10.88 pack still classifies as Starter: {band}"
    assert eval(expr, {}, {"amount_dollars": 9}) is True, \
        f"the real $9 Starter stopped matching its own band: {band}"


# ── 3. The floor stays checkout-only ─────────────────────────────────────

def test_subscription_cancel_paths_are_not_floored():
    """Cancels MUST still demote. If someone ever 'consistently' applies the
    floor to these, a churned customer keeps paid access forever.
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name in ("handle_subscription_deleted",
                                  "handle_subscription_updated")):
            body = ast.get_source_segment(SRC, node) or ""
            assert "_plan_write_floor" not in body, (
                f"{node.name} calls the checkout floor — a cancellation must "
                "still be able to demote to free.")
            assert "'free'" in body, (
                f"{node.name} no longer demotes to free.")
