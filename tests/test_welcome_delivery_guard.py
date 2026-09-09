"""The welcome email must ALARM on its outcome, not only on an exception.

r-welcome-429 (2026-09-09). lbthrall@gmail.com paid $99 and received nothing:
Resend answered 429, `_resend_email` swallowed the error and returned False, and
the 🚨 admin alert lived ONLY inside `except Exception`. A swallowed error never
reaches an except branch, so three failed sends alarmed nobody and a paying
customer sat locked out with zero API calls.

These guards pin the three properties that would each have prevented it. They
bind to the AST of the real functions — not to substrings in the file blob,
which stay green when the code they claim to describe moves or dies.

House rule: never import main (tests/test_activation_emails.py:2).
"""
import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")


def _tree():
    with open(MAIN, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in main.py — the guard is aimed at a dead target")


def _calls_named(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def _nodes_under_except(func):
    """Every node that lives inside an `except` handler of this function."""
    inside = set()
    for n in ast.walk(func):
        if isinstance(n, ast.ExceptHandler):
            for child in ast.walk(n):
                inside.add(id(child))
    return inside


# ── 1. the alarm is not trapped in `except` ──────────────────────────────
def test_welcome_failure_alerts_outside_the_except_branch():
    """THE regression. An alert reachable only from `except` cannot fire on a
    swallowed 429 — which is exactly how a paying customer went un-noticed."""
    fn = _func(_tree(), "send_welcome_email_sendgrid")
    calls = _calls_named(fn, "_alert_welcome_failure")
    assert calls, "_alert_welcome_failure() is never called — nothing pages the operator"

    in_except = _nodes_under_except(fn)
    outside = [c for c in calls if id(c) not in in_except]
    assert outside, (
        "every _alert_welcome_failure() call sits inside an `except` handler. "
        "_resend_email returns False instead of raising, so a rate-limit failure "
        "would once again be logged and never alerted."
    )


# ── 2. a 429 is retried, not treated as terminal ─────────────────────────
def test_resend_email_retries_rate_limits():
    fn = _func(_tree(), "_resend_email")

    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
    assert loops, "_resend_email has no retry loop — one 429 is still fatal"

    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_resend_email catches nothing — it cannot distinguish 429 from a bad address"

    codes = {n.value for n in ast.walk(fn)
             if isinstance(n, ast.Constant) and isinstance(n.value, int)}
    assert 429 in codes, (
        "_resend_email never mentions 429. Retrying every error is as wrong as "
        "retrying none — a bad recipient must fail fast so the operator is told."
    )


# ── 3. the alert stub cannot silently swallow the alarm ──────────────────
def test_admin_alert_stub_is_not_a_silent_noop():
    """`def send_admin_alert_email(*a, **k): pass` made the fallback path a
    no-op: callers' try/except saw success while the alarm went nowhere."""
    for fn in [n for n in ast.walk(_tree())
               if isinstance(n, ast.FunctionDef) and n.name == "send_admin_alert_email"]:
        body = [s for s in fn.body if not isinstance(s, ast.Expr)
                or not isinstance(s.value, ast.Constant)]     # drop docstrings
        assert not (len(body) == 1 and isinstance(body[0], ast.Pass)), (
            "send_admin_alert_email is a bare `pass`. An alert that silently "
            "succeeds is worse than no alert at all."
        )


# ── 4. zero findings must never read as health ───────────────────────────
@pytest.mark.parametrize("considered,stranded,healthy", [
    (0, 0, False),    # ★ nothing considered = broken query, NOT a clean bill
    (5, 0, True),
    (5, 1, False),
    (1, 1, False),
])
def test_reconcile_verdict_zero_is_not_health(considered, stranded, healthy):
    from routes.welcome_delivery_reconciler import reconcile_verdict
    verdict, ok = reconcile_verdict(considered, stranded, 168)
    assert ok is healthy, f"considered={considered} stranded={stranded} -> {verdict}"
    if considered == 0:
        assert "NO_DATA" in verdict


# ── 5. one pacer, one rate ───────────────────────────────────────────────
def test_resend_pacing_uses_the_shared_lock():
    """main and email_fallback both POST api.resend.com. Two independent
    pacers would each look correct and together permit twice Resend's limit —
    the same shape of error as a fallback that reuses its primary's provider."""
    fn = _func(_tree(), "_resend_pace")
    names = {n.module for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(fn) if isinstance(n, ast.Import) for a in n.names}
    assert "email_fallback" in names, (
        "_resend_pace does not delegate to email_fallback's shared pacer — "
        "two locks means two independent rates against one API limit."
    )
