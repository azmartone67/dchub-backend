"""The welcome grace period runs from the PAYMENT, not the signup.

`users.created_at` is when the ACCOUNT was created. For a free user who later
converts — the healthiest conversion path there is — that is months before they
paid. Grace was measured off it, so such a customer was "past grace" the instant
they bought and fell straight to `stranded`, whose motion is the activation
nudge: "want a hand with your first query?"

Measured, not hypothesised: founding customer #18 signed up 2026-07-15, paid
2026-08-28 13:29Z, and the nudge fired 2026-08-28 18:24Z — five hours after
paying, while three other emails from the same purchase were still arriving.

_classify is pure, so this is behavioural with no DB.
"""

import datetime
import importlib.util
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "_cwg", os.path.join(ROOT, "routes", "customer_white_glove.py"))
cwg = importlib.util.module_from_spec(_spec)
sys.modules["_cwg"] = cwg
_spec.loader.exec_module(cwg)

NOW = datetime.datetime(2026, 8, 28, 19, 0, tzinfo=datetime.timezone.utc)


def _row(**kw):
    base = {
        "email": "x@y.com", "name": "", "plan": "founding",
        "subscription_status": "active", "payment_failed_count": 0,
        "demoted_at": None, "total_calls": 0, "web_calls": 0, "mcp_calls": 0,
        "last_used_at": None, "welcomed": True, "welcome_attempted": True,
        "nudged": False, "nudged_at": None,
        "created_at": "2026-07-15T20:58:19",     # signed up 44 days ago
        "paid_at": None,
    }
    base.update(kw)
    return base


def _iso(days_ago):
    return (NOW - datetime.timedelta(days=days_ago)).isoformat()


def test_a_customer_who_paid_hours_ago_is_in_grace_not_stranded():
    """THE BUG. Signed up 44 days ago, paid 5.5 hours ago, zero calls."""
    stage, action, _ = cwg._classify(_row(paid_at=_iso(0.23)), NOW)
    assert stage == "new", (
        f"a customer who paid 5.5 hours ago classified {stage!r} — the "
        f"stranded motion is an 'are you stuck?' nudge, and it fires the same "
        f"afternoon they bought. action={action!r}")


def test_a_genuinely_stranded_payer_is_still_stranded():
    """The fix must not buy silence. 100 days paid, still zero calls."""
    stage, _, _ = cwg._classify(_row(paid_at=_iso(100)), NOW)
    assert stage == "stranded", (
        "a payer 100 days in with zero calls must still be stranded — "
        "widening grace to hide the nudge would delete the signal instead")


def test_grace_boundary_is_the_payment_date():
    inside = cwg._classify(_row(paid_at=_iso(1.9)), NOW)[0]
    outside = cwg._classify(_row(paid_at=_iso(2.1)), NOW)[0]
    assert (inside, outside) == ("new", "stranded"), (
        f"GRACE_HOURS={cwg.GRACE_HOURS} should bracket at 2 days from payment; "
        f"got inside={inside} outside={outside}")


def test_no_payment_date_falls_back_to_signup_unchanged():
    """6 of 27 payers have no recorded payment date. They must behave exactly
    as before rather than silently entering permanent grace."""
    stage, _, _ = cwg._classify(_row(paid_at=None), NOW)
    assert stage == "stranded", (
        "with no payment date the old created_at basis must still apply — "
        "falling into grace on a missing value would mute the nudge forever")


def test_needs_human_cannot_contradict_the_stage():
    """The board's two grace reads must use the same basis. If `needs_human`
    still measured from signup, a row could read stage=new (in grace) AND
    needs_human=true (past grace) at the same time."""
    src = open(os.path.join(ROOT, "routes", "customer_white_glove.py"),
               encoding="utf-8").read()
    i = src.index('"needs_human": bool(')
    block = src[i:i + 420]
    assert "paid_at" in block, (
        "needs_human is still computed from created_at only — it will "
        "contradict the stage for every free user who converted")


def test_the_measure_query_actually_selects_a_payment_date():
    """_classify can only be right if the row carries paid_at."""
    src = open(os.path.join(ROOT, "routes", "customer_white_glove.py"),
               encoding="utf-8").read()
    q = src[src.index("SELECT u.email, u.name"):src.index('""", (PAID_PLANS,))')]
    assert "AS paid_at" in q, "the roster query no longer provides paid_at"
    # ★Anchor on tokens UNIQUE to the payment sources. Asserting
    # `"founding_customers" in q` was VACUOUS: the same query already joins
    # that table for human_contacted_at, so deleting the payment subquery
    # still passed (mutation M6). Verified by re-running M6 after this change.
    assert "mcp_conversions" in q, "lost the conversion payment source"
    assert "first_payment_at" in q, (
        "lost the founding payment source; mcp_conversions alone covers "
        "21 of 27 payers, founding_customers covers a different 15")
    # psycopg2 scans the whole string for format specs — a bare % raises
    # "tuple index out of range" and the board silently serves 0 payers.
    assert not re.search(r"(?<!%)%(?![%s])", q), (
        "bare percent sign in the parameterised roster query")
