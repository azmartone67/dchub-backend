"""
test_web_path_honors_dunning_demote.py — the web/session path must not serve a
paid tier to an account the dunning guard has already demoted on the API path.
(2026-09-19)

MEASURED against production 2026-09-19. The entire demoted population is three
accounts, and one of them — demoted_reason='dunning_prior_payer', stamped
2026-08-23 — held plan='founding' with subscription_status='payment_failed'.
`founding` is rank 4, api_tier 'pro', Pro-equivalent limits (tier_registry:33).

main.py:handle_payment_failed demotes `api_keys.rate_limit_tier` and
deliberately PRESERVES `users.plan` ("so a successful retry will restore them").
So the two entitlement readers disagreed:

  flask_mcp_endpoints.py:1463 / ai_deals_api.py:54  read rate_limit_tier -> free
  api_tier_gating.get_user_plan()                   read users.plan      -> founding

One account, two answers, decided by which surface the caller hit.

★ THE FIX IS A CONJUNCTION, AND THIS FILE EXISTS MOSTLY TO PIN BOTH HALVES —
  each half alone is a DIFFERENT bug, and the obvious one-line version
  ("add 'payment_failed' to the force-free tuple") is the first of them:

  * status alone would cut a PAYING customer's web access on failure #1.
    handle_payment_failed sets subscription_status='payment_failed' on every
    failure, while the demote fires at DEMOTE_AFTER_N_FAILURES (>=4 for a prior
    payer), and its docstring grants real customers "~21 days of full paid
    access" through Stripe's retry cycle ON PURPOSE. test_grace_* pins that.

  * the stamp alone could lock someone out permanently. r46-restore clears
    demoted_at only for the two reasons handle_payment_failed writes, and only
    best-effort; a hand-set stamp has no clearer in this repo. Requiring
    the status means handle_invoice_paid (which sets subscription_status
    ='active') restores web access by the act of paying, whether or not anything
    ever clears the stamp. test_paying_restores_* pins that.

★ AND the rule is inert unless the query FETCHES demoted_at — a helper wired to
  nothing passes every unit test it has. test_query_fetches_demoted_at reads the
  shipped source of get_user_plan for that, so the wiring cannot rot silently.
"""
import ast
import inspect
import re

import api_tier_gating
from api_tier_gating import resolve_effective_plan


# ── the demoted window: both halves present ─────────────────────────────────
def test_demoted_and_unresolved_is_free():
    """The measured account: founding + payment_failed + a demote stamp."""
    assert resolve_effective_plan('founding', 'payment_failed', 'pro',
                                  '2026-08-23T00:00:00Z') == 'free'


def test_demoted_and_unresolved_is_free_for_every_paid_tier():
    for plan in ('starter', 'developer', 'pro', 'founding', 'team', 'enterprise'):
        assert resolve_effective_plan(plan, 'payment_failed', '', 'stamped') == 'free', plan


# ── half one: status without a stamp = the deliberate Stripe retry grace ────
def test_grace_payment_failed_without_a_stamp_keeps_the_plan():
    """Failure #1 of a real customer. Demoting here deletes the ~21-day grace
    handle_payment_failed documents and grants on purpose."""
    assert resolve_effective_plan('pro', 'payment_failed', '', None) == 'pro'


def test_grace_holds_for_every_paid_tier():
    for plan in ('starter', 'developer', 'pro', 'founding'):
        assert resolve_effective_plan(plan, 'payment_failed', '', None) == plan, plan


# ── half two: a stamp without the status = they paid; give access back ──────
def test_paying_restores_web_access_even_if_the_stamp_is_never_cleared():
    """handle_invoice_paid sets subscription_status='active'. r46-restore only
    clears demoted_at for 'dunning_prior_payer', so a first-charge stamp can
    outlive the debt — the status is what guarantees recovery."""
    assert resolve_effective_plan('pro', 'active', '', '2026-05-23T00:00:00Z') == 'pro'


# ── unchanged behaviour ─────────────────────────────────────────────────────
def test_canceled_and_unpaid_still_force_free():
    for status in ('canceled', 'unpaid'):
        assert resolve_effective_plan('pro', status, '', None) == 'free', status


def test_admin_role_outranks_every_other_branch():
    assert resolve_effective_plan('free', 'canceled', 'admin', 'stamped') == 'admin'


def test_healthy_paid_account_passes_through():
    assert resolve_effective_plan('pro', 'active', '', None) == 'pro'


def test_missing_plan_falls_back_to_free():
    assert resolve_effective_plan('', 'active', '', None) == 'free'
    assert resolve_effective_plan(None, 'active', '', None) == 'free'


# ── the wiring: a predicate nothing feeds is inert ──────────────────────────
def _get_user_plan_source():
    return inspect.getsource(api_tier_gating.get_user_plan)


def test_query_fetches_demoted_at():
    """EVERY users SELECT in get_user_plan must fetch demoted_at, or row[3] is
    always None and the whole rule above can never fire in production."""
    src = _get_user_plan_source()
    selects = re.findall(r'SELECT\s+(.*?)\s+FROM\s+users', src, re.I | re.S)
    assert selects, 'get_user_plan no longer queries users — re-point this test'
    for cols in selects:
        assert 'demoted_at' in cols, f'SELECT without demoted_at: {cols!r}'


def test_get_user_plan_delegates_to_the_predicate():
    """and it must actually CALL resolve_effective_plan — an inlined copy would
    drift from the one this file tests."""
    tree = ast.parse(inspect.getsource(api_tier_gating.get_user_plan).strip())
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert 'resolve_effective_plan' in called


def test_the_column_scan_is_not_vacuous():
    """control — the regex above must reject a SELECT that omits the column."""
    cols = re.findall(r'SELECT\s+(.*?)\s+FROM\s+users',
                      'c.execute("SELECT plan, role FROM users WHERE id = %s")', re.I | re.S)
    assert cols and 'demoted_at' not in cols[0]
