"""Zero delivery confirmations must read as an alarm, not as silence.

welcome_email_log stamps a resend_message_id on every send so
/api/v1/webhooks/resend can close the loop. As of 2026-08-28 email_events held
ONE row all time — a synthetic deploy-verify@example.com from 2026-07-17 — while
14 real welcome emails went out in the preceding 30 days. Every "we welcomed
them" claim in this repo therefore means *sent*, not *delivered*, and nothing
anywhere said so.

The failure mode is silence, so the thing worth guarding is the READING of zero.
delivery_verdict is pure, so that rule is tested for real rather than asserted
against a string in the source.
"""

import ast
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from routes.onboarding_recover import delivery_verdict  # noqa: E402

SRC = open(os.path.join(ROOT, "routes", "onboarding_recover.py"),
           encoding="utf-8").read()


def test_sends_with_no_delivery_events_is_not_healthy():
    """THE RULE. 14 sends, 0 events — the live state when this was written."""
    verdict, healthy = delivery_verdict(matchable=14, events=0, confirmed=0, days=30)
    assert healthy is False, (
        "14 welcome emails and zero delivery events reported HEALTHY — that is "
        "exactly how a blind spot stays invisible")
    assert "BLIND" in verdict


def test_the_blind_verdict_names_the_owner_action():
    """A verdict nobody can act on is another kind of silence."""
    verdict, _ = delivery_verdict(14, 0, 0, 30)
    assert "webhooks/resend" in verdict and "RESEND_WEBHOOK_SECRET" in verdict, (
        "the BLIND verdict must name both owner-side steps; code cannot mint "
        "the data, so the endpoint's only job is to hand over the fix")


def test_no_sends_is_healthy_and_is_not_confused_with_blind():
    verdict, healthy = delivery_verdict(0, 0, 0, 30)
    assert healthy is True and "NO_SENDS" in verdict, (
        "an empty window must not be reported as a delivery failure")


def test_partial_confirmation_is_not_healthy_and_does_not_claim_failure():
    verdict, healthy = delivery_verdict(10, 6, 6, 30)
    assert healthy is False
    assert "unproven" in verdict and "known-failed" in verdict, (
        "unconfirmed is not the same as failed; the wording must not "
        "overclaim in either direction")


def test_full_confirmation_is_healthy():
    verdict, healthy = delivery_verdict(10, 10, 10, 30)
    assert healthy is True and "CONFIRMED" in verdict


def test_unmatchable_sends_are_counted_separately():
    """A row with no resend_message_id cannot be matched by construction.
    Holding it against delivery would manufacture a permanent PARTIAL."""
    assert "sends_without_a_message_id" in SRC, (
        "sends with no message id must be reported separately, not silently "
        "counted as undelivered")


def test_the_status_filter_matches_only_sent_rows_and_doubles_its_percent():
    """welcome_email_log records ATTEMPTS — skipped_duplicate is a row. And a
    bare % in a parameterised query raises 'tuple index out of range'."""
    qs = [n.value for n in ast.walk(ast.parse(SRC))
          if isinstance(n, ast.Constant) and isinstance(n.value, str)
          and "welcome_email_log" in n.value]
    assert qs, "no welcome_email_log query found"
    joined = " ".join(qs)
    assert "LIKE 'sent%%'" in joined, (
        "the send count must filter on a sent-prefix status with the percent "
        "DOUBLED — psycopg2 scans the whole query for format specs")
    for q in qs:
        assert not re.search(r"(?<!%)%(?![%s(])", q), f"bare percent in: {q[:60]}"


def test_the_endpoint_is_admin_gated():
    """Scope by AST, not a character window — the docstring alone is longer
    than any slice I would have guessed, and a slice that misses the call
    fails for the wrong reason."""
    fns = [n for n in ast.walk(ast.parse(SRC))
           if isinstance(n, ast.FunctionDef) and n.name == "delivery_truth"]
    assert fns, "delivery_truth not found"
    called = {n.func.id for n in ast.walk(fns[0])
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_admin_ok" in called, "delivery-truth endpoint is not admin-gated"
