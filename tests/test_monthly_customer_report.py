"""Guards for routes/monthly_customer_report.py — the send-side ones.

This module emails real paying customers, so the tests that matter are the ones
that prove a WRONG send cannot happen: a suppressed address, a second send in
the same month, an un-armed run, a report for a month the customer predates, or
recap copy for somebody with no calls.

Every test here is a real behaviour test — none assert on SQL substrings, which
pass whether or not the clause they quote does anything.
"""
import datetime

import pytest

mcr = pytest.importorskip("routes.monthly_customer_report")


# ── month arithmetic ─────────────────────────────────────────────────────

def test_default_month_is_the_last_COMPLETE_month():
    """Run on the 1st, a current-month report is one day of data and reads to
    the customer as a usage collapse."""
    y, m = mcr.parse_month(None)
    today = datetime.date.today()
    assert (y, m) != (today.year, today.month)
    expected = (today.replace(day=1) - datetime.timedelta(days=1))
    assert (y, m) == (expected.year, expected.month)


def test_month_bounds_are_half_open_and_cover_february():
    start, end = mcr.month_bounds(2026, 2)
    assert start == datetime.date(2026, 2, 1)
    assert end == datetime.date(2026, 3, 1)
    start, end = mcr.month_bounds(2026, 12)
    assert end == datetime.date(2027, 1, 1), "December must roll the year"


def test_email_key_carries_the_month_so_idempotency_is_per_month():
    assert mcr.email_key_for(2026, 8) != mcr.email_key_for(2026, 9)
    assert mcr.email_key_for(2026, 8) == "monthly_report_2026_08"


# ── the signup guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("created,expected", [
    # The two formats users.created_at actually holds in production.
    ("2026-09-08T23:52:10.000000", True),    # lbthrall — signed up after Aug
    ("2026-08-08T21:07:43.420312", False),   # tj — signed up during Aug
    ("2026-07-26 07:42:40.586695+00", False),
    # Fail OPEN: unknown/garbage must never silently drop a real customer.
    (None, False), ("", False), ("not-a-date", False), ("0", False),
])
def test_signed_up_after_only_drops_a_PROVEN_post_month_signup(created, expected):
    assert mcr._signed_up_after(created, datetime.date(2026, 9, 1)) is expected


# ── copy correctness ─────────────────────────────────────────────────────

def _flat(html: str) -> str:
    """Copy assertions must survive the template's line wrapping — the source
    f-strings break sentences mid-phrase, so a raw substring test is testing
    the indentation, not the words."""
    return " ".join(html.split())


def _row(**kw):
    base = dict(email="a@b.com", name="Ada Lovelace", plan="pro", invoices=2,
                engagement_stage="stranded", last_login=None,
                subscription_status="active", created_at=None,
                ok_calls=0, all_calls=0, active_days=0, distinct_tools=0,
                first_call=None, last_call=None, calls_ever=0, last_ever=None,
                n_keys=1, top_tools=[], blocked_tools=[], suggested_tools=[])
    base.update(kw)
    base["lane"] = "recap" if base["ok_calls"] > 0 else "activation"
    return base


def test_zero_call_customer_never_receives_recap_copy():
    r = _row(ok_calls=0)
    assert r["lane"] == "activation"
    html = mcr.render_email(r, 2026, 8)
    flat = _flat(html)
    assert "no record of any API calls" in flat
    # The recap's headline claim must not appear for someone with no calls.
    assert "queries across" not in flat


def test_active_customer_gets_their_real_numbers_not_a_template():
    r = _row(ok_calls=1652, active_days=30, distinct_tools=48,
             top_tools=[{"tool": "get_global_power", "calls": 358}])
    html = mcr.render_email(r, 2026, 8)
    flat = _flat(html)
    assert "1,652 queries" in flat and "48" in flat
    assert "get global power" in flat and "358" in flat
    assert "no record of any API calls" not in flat


def test_activation_copy_makes_no_billing_claim_it_cannot_prove():
    """'renewed this month' was the original wording. The report is sent AFTER
    the month it covers and we hold no renewal DATE (stripe_webhook_events has
    no customer column), so the copy must lean on invoices_paid_count."""
    flat = _flat(mcr.render_email(_row(invoices=2), 2026, 8))
    assert "renewed this month" not in flat
    assert "2 invoices" in flat


def test_singular_billing_line_for_a_first_invoice():
    flat = _flat(mcr.render_email(_row(invoices=1), 2026, 8))
    assert "1 invoices" not in flat and "invoices now" not in flat


def test_subject_matches_the_lane():
    assert "1,652" in mcr.subject_for(_row(ok_calls=1652), 2026, 8)
    assert mcr.subject_for(_row(ok_calls=0), 2026, 8).startswith("A question")


def test_first_name_prefers_the_registered_name_then_the_local_part():
    assert mcr._first_name("Theodore Karklins", "tj@karklins.com") == "Theodore"
    assert mcr._first_name("", "seydi.sokhona@gmail.com") == "Seydi"
    assert mcr._first_name("", "") == "there"


def test_recipient_name_is_escaped_into_the_html():
    html = mcr.render_email(_row(name="<script>x</script> Bad"), 2026, 8)
    assert "<script>" not in html and "&lt;script&gt;" in html


# ── the send-side guards ─────────────────────────────────────────────────

class _FakeConn:
    def close(self):
        pass


def _install(monkeypatch, roster, suppressed=frozenset(), already=frozenset(),
             send_ok=True):
    sent = []
    monkeypatch.setattr(mcr, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(mcr, "collect_month", lambda y, m, conn=None: list(roster))
    monkeypatch.setattr(mcr, "_suppressed", lambda c: set(suppressed))
    monkeypatch.setattr(mcr, "_already_sent", lambda c, k: set(already))
    monkeypatch.setattr(mcr, "_log_sent", lambda c, e, k, status="sent": None)

    import email_fallback
    def _fake_send(to_email, subject, html_content=None, text_content=None,
                   from_email=None, from_name=None):
        sent.append(to_email)
        return send_ok
    monkeypatch.setattr(email_fallback, "send_email_resilient", _fake_send)
    return sent


def test_unarmed_run_sends_nothing_even_with_confirm(monkeypatch):
    monkeypatch.delenv("MONTHLY_CUSTOMER_REPORT_ARM", raising=False)
    sent = _install(monkeypatch, [_row(email="real@customer.com")])
    res = mcr.run_monthly_report(2026, 8, armed=True)
    assert sent == [] and res["sent"] == 0 and res["armed"] is False
    assert res["eligible"] == 1, "dry-run must still REPORT who would be mailed"


def test_arm_flag_without_confirm_sends_nothing(monkeypatch):
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch, [_row(email="real@customer.com")])
    res = mcr.run_monthly_report(2026, 8, armed=False)
    assert sent == [] and res["armed"] is False


def test_both_confirm_and_arm_flag_actually_send(monkeypatch):
    """The counterpart the other two need: if this fails, the tests above pass
    vacuously because nothing can ever send."""
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch, [_row(email="real@customer.com")])
    res = mcr.run_monthly_report(2026, 8, armed=True)
    assert sent == ["real@customer.com"] and res["sent"] == 1


def test_suppressed_address_is_never_mailed(monkeypatch):
    """kevin.d.serfass@gmail.com unsubscribed 2026-06-22 and is on
    email_suppression. An unsubscribe must outrank a monthly report."""
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch,
                    [_row(email="kevin.d.serfass@gmail.com"), _row(email="ok@x.com")],
                    suppressed={"kevin.d.serfass@gmail.com"})
    res = mcr.run_monthly_report(2026, 8, armed=True)
    assert sent == ["ok@x.com"]
    assert {s["reason"] for s in res["skipped"]} == {"suppressed"}


def test_second_send_in_the_same_month_is_skipped(monkeypatch):
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch, [_row(email="already@x.com")],
                    already={"already@x.com"})
    res = mcr.run_monthly_report(2026, 8, armed=True)
    assert sent == []
    assert res["skipped"][0]["reason"] == "already_sent_this_month"


def test_internal_addresses_are_skipped(monkeypatch):
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch,
                    [_row(email="qa-canary-dev@dchub.cloud"), _row(email="ok@x.com")])
    mcr.run_monthly_report(2026, 8, armed=True)
    assert sent == ["ok@x.com"]


def test_a_failed_send_is_counted_as_an_error_not_a_success(monkeypatch):
    """send_email_resilient swallows its own exceptions and returns False, so
    the failure path is a return value, not a raise."""
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    _install(monkeypatch, [_row(email="ok@x.com")], send_ok=False)
    res = mcr.run_monthly_report(2026, 8, armed=True)
    assert res["sent"] == 0 and res["errors"] == 1


def test_unreadable_suppression_list_refuses_to_send(monkeypatch):
    """An empty suppression set and an unreadable one look identical to the
    filter. Fail closed, or one DB blip mails every unsubscriber."""
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch, [_row(email="ok@x.com")])
    def _boom(c):
        raise RuntimeError("email_suppression unreadable, refusing to send")
    monkeypatch.setattr(mcr, "_suppressed", _boom)
    with pytest.raises(RuntimeError):
        mcr.run_monthly_report(2026, 8, armed=True)
    assert sent == []


def test_limit_caps_the_batch(monkeypatch):
    monkeypatch.setenv("MONTHLY_CUSTOMER_REPORT_ARM", "1")
    sent = _install(monkeypatch, [_row(email=f"c{i}@x.com") for i in range(10)])
    mcr.run_monthly_report(2026, 8, armed=True, limit=3)
    assert len(sent) == 3


def test_lanes_are_reported_separately(monkeypatch):
    monkeypatch.delenv("MONTHLY_CUSTOMER_REPORT_ARM", raising=False)
    _install(monkeypatch, [_row(email="a@x.com", ok_calls=5),
                           _row(email="b@x.com", ok_calls=0),
                           _row(email="c@x.com", ok_calls=0)])
    res = mcr.run_monthly_report(2026, 8)
    assert res["lanes"] == {"recap": 1, "activation": 2}
