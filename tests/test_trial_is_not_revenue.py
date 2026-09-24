"""A trial is not revenue until it converts (r-trial-honest, 2026-09-24).

The Pro 7-day trial Payment Link (offer=pro_trial_7d, Pro $99/mo price) creates
a subscription with status 'trialing' and $0 charged. POST
/api/v1/stripe/webhook-mcp booked EVERY created/updated subscription into
mcp_conversions at the price's unit_amount whatever its status, so a trial
start read as a $99 paid conversion on day 0 and stayed booked if the trial was
cancelled or its day-7 charge failed. It also re-granted the paid MCP key on
ANY status, undoing main.py's demotion on past_due / canceled.

Guards: subscription_booking's decision for every Stripe status, with and
without a trial; and that the handler applies it (booking, signal flip,
provisioning), read from source because the handler needs Stripe + Postgres.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# flask_mcp_endpoints refuses to import without a DB URL; nothing connects
# here. Confined to the import and put back (same as
# test_node_validate_row_decides), because other suites skip on its absence.
_injected = not (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL"))
if _injected:
    os.environ["NEON_DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/db"
try:
    import flask_mcp_endpoints as fme
finally:
    if _injected:
        os.environ.pop("NEON_DATABASE_URL", None)
T = 1790000000  # any trial timestamp

STATUSES = ["active", "trialing", "past_due", "unpaid", "canceled",
            "incomplete", "incomplete_expired", "paused"]


@pytest.mark.parametrize("status", [x for x in STATUSES if x != "trialing"])
def test_no_trial_books_exactly_as_before(status):
    """A subscription that never had a trial keeps today's booking (always).
    ('trialing' always carries trial_end in Stripe, so it is not a no-trial case.)"""
    _, book, _ = fme.subscription_booking({"status": status})
    assert book is True


@pytest.mark.parametrize("status,book", [
    ("trialing", False),            # $0 so far — the day-0 $99 was the bug
    ("active", True),               # the first real charge converted it
    ("past_due", False),            # day-7 charge failed: never paid
    ("unpaid", False),
    ("canceled", False),            # cancelled during / at the end of the trial
    ("incomplete_expired", False),
])
def test_a_trial_books_only_once_active(status, book):
    _, got, _ = fme.subscription_booking({"status": status, "trial_start": T, "trial_end": T + 604800})
    assert got is book, status


@pytest.mark.parametrize("status,prov", [
    ("active", True), ("trialing", True),
    ("past_due", False), ("unpaid", False), ("canceled", False),
    ("incomplete", False), ("incomplete_expired", False), ("paused", False),
])
def test_the_paid_key_is_granted_only_while_active_or_trialing(status, prov):
    for sub in ({"status": status}, {"status": status, "trial_end": T}):
        assert fme.subscription_booking(sub)[2] is prov, sub


def test_status_is_normalised_and_missing_is_safe():
    assert fme.subscription_booking({"status": " Trialing "})[:2] == ("trialing", False)
    assert fme.subscription_booking({}) == ("", True, False)
    assert fme.subscription_booking(None) == ("", True, False)


def _handler_src():
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    i = src.index('@mcp_bp.post("/api/v1/stripe/webhook-mcp")')
    j = src.index("@mcp_bp.get(", i)
    return src[i:j]


def test_the_handler_applies_the_decision():
    h = _handler_src()
    assert "_sub_status, _book, _provision = subscription_booking(obj)" in h
    # booking: the INSERT is skipped when not booked
    # the SUBSCRIPTION insert is the last one; earlier ones are the one-time
    # (mode=payment) checkout branch, which is not this change
    ins = h.rindex("INSERT INTO mcp_conversions")
    assert re.search(r"if not _book:\s*\n\s*raise _NotBooked\(\)", h[h.rindex("try:", 0, ins):ins]), "INSERT not gated"
    # signal flip: skipped when not booked
    flip = h.rindex("mark_signals_converted(")          # the subscription path's call
    assert "if not _book:" in h[h.rindex("try:", 0, flip):flip], "signal flip not gated"
    # provisioning: the key grant's try bails first when not provisioning
    start = h.index("provisioned_key = None")
    m = re.search(r"if not _provision:\s*\n\s*raise _NotBooked\(\)", h[start:])
    assert m, "provisioning not gated"
    prov = start + m.start()
    assert prov < h.index("UPDATE mcp_dev_keys SET tier=%s") and prov < h.index("INSERT INTO mcp_dev_keys")
    assert "except _NotBooked:" in h[prov:h.index("r68-canonical", prov)]
