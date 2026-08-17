"""Welcome claim-then-send + payment receipt stay wired (onboarding lane 2).

The 4-welcomes-in-3-seconds flood (alexander@ryex.net, 2026-07-29) was not a
missing guard — it was check-then-act: every sender read welcome_email_log,
then sent, then wrote, and the write landed AFTER a ~1s network send, so
concurrent senders all passed the check before any of them logged. The fix
(2026-08-17) claims atomically BEFORE any I/O, the flask_mcp path stopped
writing a second audit row for the same send, and paying customers get a
receipt from the checkout webhook.

All static/AST — CI runs with no DATABASE_URL. Helpers assert they FOUND
their target first: an empty parse satisfies every "not in".
"""

import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _tree(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return ast.parse(f.read())


def _funcs(tree, name):
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == name]
    assert fns, f"{name} not found"
    return fns


def _calls_name(fn, callee):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == callee) or \
               (isinstance(f, ast.Attribute) and f.attr == callee):
                out.append(n)
    return out


def _consts(fn):
    return [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_sender_claims_before_sending():
    main = _tree("main.py")
    sender = _funcs(main, "send_welcome_email_sendgrid")[0]
    assert _calls_name(sender, "_claim_welcome_send"), (
        "send_welcome_email_sendgrid no longer claims atomically — the "
        "concurrent-sender race (4 welcomes in 3s) returns")


def test_claim_is_one_atomic_statement():
    main = _tree("main.py")
    claim = _funcs(main, "_claim_welcome_send")[0]
    atomic = [c for c in _consts(claim)
              if "INSERT INTO welcome_email_log" in c
              and "WHERE NOT EXISTS" in c]
    assert atomic, (
        "_claim_welcome_send must claim in ONE INSERT ... WHERE NOT EXISTS "
        "statement — splitting the check from the insert reopens the race")


def test_welcome_guard_ignores_receipts():
    main = _tree("main.py")
    guard = _funcs(main, "_welcome_recently_sent")[0]
    joined = " ".join(_consts(guard))
    assert "NOT LIKE 'receipt" in joined, (
        "_welcome_recently_sent must exclude receipt rows — a receipt logged "
        "first would suppress the customer's actual welcome email")


def test_flask_mcp_no_longer_double_logs_the_send():
    fmcp = _tree("flask_mcp_endpoints.py")
    spans = [(n, n.lineno, getattr(n, "end_lineno", n.lineno))
             for n in ast.walk(fmcp)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    callers = []
    for fn, _s, _e in spans:
        if _calls_name(fn, "send_welcome_email_sendgrid"):
            callers.append(fn)
    assert callers, "flask_mcp_endpoints no longer calls the welcome sender"
    for fn in callers:
        assert not _calls_name(fn, "_log_welcome_email"), (
            f"{fn.name} writes its own welcome_email_log row beside the "
            "sender's — the 'second log row' double-count returns")


def test_checkout_webhook_sends_a_receipt():
    main = _tree("main.py")
    receipt_fns = _funcs(main, "_send_payment_receipt")
    atomic = [c for c in _consts(receipt_fns[0])
              if "INSERT INTO welcome_email_log" in c
              and "WHERE NOT EXISTS" in c]
    assert atomic, (
        "_send_payment_receipt must claim per-session atomically or webhook "
        "re-delivery double-sends receipts")
    callers = [n for n in ast.walk(main) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)
               and n.func.id == "_send_payment_receipt"]
    assert callers, (
        "nothing calls _send_payment_receipt — paying customers get no "
        "receipt again (onboarding lane 2 com_receipt)")
