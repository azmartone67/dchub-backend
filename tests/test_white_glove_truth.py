"""Guard: the customer board must not report a customer as handled when nobody
handled them.

Two measured falsehoods this pins (both 2026-08-19, rob@hedmarkholdings.com):

1. `welcomed` was a bare EXISTS over welcome_email_log with no status filter.
   That table logs ATTEMPTS — his 4 rows included two `skipped_duplicate`. A
   customer whose every attempt was skipped therefore read `welcomed: true`,
   i.e. "we told them" when nothing left the building.

2. Nothing anywhere named a customer no HUMAN had contacted. Onboarding Master
   Shell #43 lane 5 reports the condition in prose ("4 payer(s) in 30d received
   no human contact") and never says who.

_classify is pure — no DB, no Flask — so these run in the pure-function harness.
"""
import datetime

import pytest

from routes.customer_white_glove import GRACE_HOURS, _classify

NOW = datetime.datetime(2026, 8, 19, 19, 0, tzinfo=datetime.timezone.utc)


def _payer(**over):
    """A healthy, in-grace payer. Tests override one field at a time so a
    failure names the field that caused it."""
    base = {
        "subscription_status": "active",
        "demoted_at": None,
        "payment_failed_count": 0,
        "created_at": (NOW - datetime.timedelta(hours=2)).isoformat(),
        "total_calls": 0,
        "last_used_at": None,
        "nudged": False,
        "nudged_at": None,
        "welcomed": True,
        "welcome_attempted": True,
        "human_contacted_at": None,
    }
    base.update(over)
    return base


def test_delivered_welcome_in_grace_is_calm():
    stage, action, prio = _classify(_payer(), NOW)
    assert stage == "new"
    assert prio == 0


def test_attempted_but_undelivered_welcome_escalates():
    """★ THE REGRESSION. Every welcome attempt skipped/failed = they paid and
    heard nothing. This must NOT read as a quiet grace period."""
    stage, action, prio = _classify(
        _payer(welcomed=False, welcome_attempted=True), NOW)
    assert stage == "stranded", (
        f"a payer whose welcome never sent classified as {stage!r} — before this "
        "fix that was 'new' with a 'welcome sent' action"
    )
    assert prio == 3
    assert action.startswith("ESCALATE")
    assert "never delivered" in action


def test_undelivered_outranks_the_grace_window():
    """Ordering matters: the grace-period branch used to swallow this because
    it came first and only looked at age."""
    fresh = _payer(welcomed=False, welcome_attempted=True,
                   created_at=(NOW - datetime.timedelta(minutes=5)).isoformat())
    stage, _, prio = _classify(fresh, NOW)
    assert (stage, prio) == ("stranded", 3)


def test_no_welcome_attempted_at_all_is_not_an_undelivered_escalation():
    """A customer with NO log rows is a different (and older) diagnosis than
    one whose sends were all skipped — don't collapse them."""
    stage, action, _ = _classify(
        _payer(welcomed=False, welcome_attempted=False), NOW)
    assert "never delivered" not in action
    assert stage == "new"


def test_failed_payment_still_outranks_undelivered_welcome():
    """Money failing beats comms failing — guard the branch order both ways so
    a later edit can't silently reshuffle it."""
    stage, _, prio = _classify(
        _payer(welcomed=False, welcome_attempted=True, payment_failed_count=2), NOW)
    assert stage == "at_risk"
    assert prio == 3


def _roster_for(rows, monkeypatch):
    """Drive the REAL _roster over synthetic _measure output. Asserting on an
    expression re-written in the test would pass no matter what the module
    does — this exercises the shipped code path."""
    import routes.customer_white_glove as cwg
    monkeypatch.setattr(cwg, "_measure", lambda: rows)
    return {r["email"]: r for r in cwg._roster(now=NOW)}


@pytest.mark.parametrize("hours,expected", [(1, False), (GRACE_HOURS + 1, True)])
def test_needs_human_turns_on_after_grace(hours, expected, monkeypatch):
    row = _payer(email="a@b.com", name="", plan="developer",
                 created_at=(NOW - datetime.timedelta(hours=hours)).isoformat(),
                 web_calls=0, mcp_calls=0, total_calls=0)
    out = _roster_for([row], monkeypatch)["a@b.com"]
    assert out["needs_human"] is expected


def test_contacted_customer_never_needs_human(monkeypatch):
    row = _payer(email="a@b.com", name="", plan="developer",
                 created_at=(NOW - datetime.timedelta(days=9)).isoformat(),
                 web_calls=0, mcp_calls=0, total_calls=0,
                 human_contacted_at=NOW - datetime.timedelta(days=1))
    out = _roster_for([row], monkeypatch)["a@b.com"]
    assert out["needs_human"] is False
    assert out["human_contacted_at"] is not None


def test_undelivered_welcome_is_visible_on_the_roster_row(monkeypatch):
    """The board must carry the raw fact, not just the derived stage — an
    operator reading one row needs to see WHY it escalated."""
    row = _payer(email="a@b.com", name="", plan="developer",
                 web_calls=0, mcp_calls=0, total_calls=0,
                 welcomed=False, welcome_attempted=True)
    out = _roster_for([row], monkeypatch)["a@b.com"]
    assert out["welcomed"] is False
    assert out["welcome_attempted"] is True
    assert out["escalate"] is True


def test_self_health_counts_the_gap(monkeypatch):
    """needs_human / welcome_undelivered are the two numbers the operator had
    no way to get before. Zero must be reachable too, or the flag is noise."""
    import routes.customer_white_glove as cwg
    old = _payer(email="old@b.com", name="", plan="developer",
                 created_at=(NOW - datetime.timedelta(days=9)).isoformat(),
                 web_calls=0, mcp_calls=0, total_calls=0,
                 welcomed=False, welcome_attempted=True)
    handled = _payer(email="ok@b.com", name="", plan="developer",
                     created_at=(NOW - datetime.timedelta(days=9)).isoformat(),
                     web_calls=5, mcp_calls=5, total_calls=10,
                     last_used_at=NOW - datetime.timedelta(days=1),
                     human_contacted_at=NOW - datetime.timedelta(days=2))
    monkeypatch.setattr(cwg, "_measure", lambda: [old, handled])
    roster = cwg._roster(now=NOW)
    health = cwg._self_health(roster)
    assert health["needs_human"] == 1
    assert health["welcome_undelivered"] == 1
    assert health["human_touch_gap"] is True

    monkeypatch.setattr(cwg, "_measure", lambda: [handled])
    health2 = cwg._self_health(cwg._roster(now=NOW))
    assert health2["needs_human"] == 0
    assert health2["welcome_undelivered"] == 0
    assert health2["human_touch_gap"] is False
