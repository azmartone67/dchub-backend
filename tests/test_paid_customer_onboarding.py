#!/usr/bin/env python3
"""tests/test_paid_customer_onboarding.py — r-coldbuy onboarding guards.

NO NETWORK, NO DB. These are source-level contracts on the four places a
paying customer's onboarding went wrong on 2026-08-08 (founding customer #15,
$49 Developer, bought via a Stripe payment link before ever claiming a key):

  1. claim_key ran NO paid-tier inheritance, so `claim_free_key({email})` —
     the first thing an agent calls — handed a paying customer a free key.
     identify_key had carried the rule since r77; claim_key did not.
  2. A password RESET sent a "your FREE account is active" welcome.
  3. Google sign-in re-sent the free welcome to EXISTING users, paying included.
  4. The Pro-welcome fired for a non-Pro plan and duplicated the real welcome.

Each assertion below fails if its fix is reverted — that is the point. Verified
by mutation: reverting any one change turns its test red (see PR description).

Run standalone:   python3 tests/test_paid_customer_onboarding.py
Run under pytest: pytest tests/test_paid_customer_onboarding.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _func_body(src, header, stop_prefix="\n@"):
    """Slice a function body out of a source file by its `def` header."""
    i = src.index(header)
    j = src.find(stop_prefix, i + len(header))
    return src[i: j if j != -1 else len(src)]


# ── 1. the core gap: claim_key must inherit an already-purchased tier ─────

def test_claim_key_inherits_paid_tier():
    src = _read("flask_mcp_endpoints.py")
    body = _func_body(src, "def claim_key():")
    assert "_inherit_paid_tier(" in body, (
        "claim_key must apply the caller's already-purchased tier — without it "
        "a pay-first customer's first claim_free_key returns a free key."
    )


def test_identify_and_claim_share_one_inheritance_rule():
    """Both entry points must call the SAME helper, not two copies of the SQL."""
    src = _read("flask_mcp_endpoints.py")
    assert src.count("def _inherit_paid_tier(") == 1, "helper must be defined once"
    for fn in ("def claim_key():", "def identify_key():"):
        assert "_inherit_paid_tier(" in _func_body(src, fn), f"{fn} must call the helper"


def test_inheritance_is_upgrade_only():
    """The rule must never demote a key that is already paid/enterprise."""
    src = _read("flask_mcp_endpoints.py")
    body = _func_body(src, "def _inherit_paid_tier(", stop_prefix="\n\n@")
    assert "NOT IN ('paid','enterprise')" in body, (
        "inheritance must be upgrade-only — a paid key must never be rewritten"
    )
    assert "subscription_status" in body, (
        "inheritance must require an ACTIVE subscription"
    )


def test_paid_claim_does_not_advertise_the_free_tier():
    """A key born paid must not be described back to the agent as free."""
    src = _read("flask_mcp_endpoints.py")
    body = _func_body(src, "def claim_key():")
    assert "None if _claimed_paid else" in body, (
        "free_tier_summary / upgrade_url must be suppressed for a paid claim"
    )


# ── 2. a password reset is not a signup ──────────────────────────────────

def test_password_reset_sends_no_welcome_email():
    src = _read("routes/auth_routes.py")
    reset = _func_body(src, "def reset_password(")
    # Strip comments first — the block carries a comment NAMING the removed
    # call, and a substring match on the name alone would read that as a send.
    reset = "\n".join(l for l in reset.splitlines()
                      if not l.lstrip().startswith("#"))
    assert "send_free_welcome_email_sendgrid(" not in reset, (
        "a password reset must not send a welcome email — it told a paying "
        "founding customer their FREE account was active"
    )


# ── 3. Google sign-in must welcome only genuinely new accounts ───────────

def test_google_signin_welcomes_only_new_accounts():
    src = _read("routes/auth_routes.py")
    # Every free-welcome send in the Google OAuth handlers must sit under a
    # new-account guard. Check each call site's preceding 6 lines.
    for m in re.finditer(r"send_free_welcome_email_sendgrid\(email, name\)", src):
        preceding = src[:m.start()].rsplit("pg_conn.commit()", 1)[-1]
        if "google_id" not in src[max(0, m.start() - 2000): m.start()]:
            continue  # the plain /register site — welcoming there is correct
        assert "if not existing:" in preceding, (
            "Google sign-in must not re-welcome an existing user on every login"
        )


# ── 4. free-tier messaging must never reach a paid account ───────────────

def test_free_welcome_is_suppressed_for_paid_accounts():
    src = _read("main.py")
    body = _func_body(src, "def send_free_welcome_email_sendgrid(",
                      stop_prefix="\ndef ")
    assert "_has_active_paid_plan(to_email)" in body, (
        "the free welcome must be suppressed for accounts on a paid plan"
    )


def test_paid_plan_check_fails_open():
    """A DB blip must not suppress a legitimate free welcome."""
    src = _read("main.py")
    body = _func_body(src, "def _has_active_paid_plan(", stop_prefix="\ndef ")
    assert "return False" in body.split("except")[-1], (
        "_has_active_paid_plan must return False on error so a real free "
        "welcome is never silently swallowed"
    )


def test_pro_welcome_requires_a_pro_family_plan():
    src = _read("main.py")
    assert "_plan_now in ('pro', 'founding', 'enterprise')" in src, (
        "the Pro welcome must not fire for a $49 Developer buyer"
    )
    assert "and not _welcome_recently_sent(customer_email)" in src, (
        "the Pro welcome must respect the same 24h dedupe as the main welcome"
    )


# ── 5. the repair path for customers already stranded ────────────────────

def test_reconcile_endpoint_exists_and_is_admin_gated():
    src = _read("main.py")
    body = _func_body(src, "def reconcile_mcp_tiers():", stop_prefix="\n@app.route")
    assert "X-Admin-Key" in body, "reconcile endpoint must be admin-gated"
    assert "dry_run" in body, "reconcile must support a dry run before writing"
    assert "NOT IN ('paid','enterprise')" in body, "reconcile must be upgrade-only"


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
