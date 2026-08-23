"""Withheld platform updates must carry an AGE and a DECISION URL — and must
not report a settled decision as an outstanding one. Shell #65 lane 2.

WHY THIS FILE EXISTS. Measured 2026-08-23: /api/v1/platform-updates published
its withheld list as {id, reason} and nothing else, so a human could see THAT
nine items were withheld but not how long they had waited or where to act. The
shell reported them as `pending=9`.

★ THE SECOND, WORSE HALF. All nine were `status: "archived"` — cards published
and then deliberately retired by PR #2804 on 2026-08-17 ("archive pre-August
wave"). `_card` returned the single reason "not approved" for EVERY
non-published entry, so the shell's `"not approved" in reason` filter counted
nine finished decisions as nine an owner still owed. A queue that counts settled
items as outstanding manufactures a backlog out of completed work.

CI-SAFETY: pure file/dict work. No DB, no network.
"""
from __future__ import annotations

import datetime as _dt
import importlib

pu = importlib.import_module("routes.platform_updates")


def _entry(**kw):
    e = {"id": "x", "title": "T", "body": "B", "announced": "2026-07-29"}
    e.update(kw)
    return e


def test_withheld_entries_carry_an_age_and_a_decision_url():
    block = pu.published_updates(force=True)
    assert block["withheld"], "expected withheld entries in the shipped store"
    for w in block["withheld"]:
        assert w.get("announced"), "%s carries no announced date" % w.get("id")
        assert w.get("age_days") is not None, "%s carries no age" % w.get("id")
        assert w.get("age_hours") is not None
        assert w.get("decision_url"), "%s carries no decision URL" % w.get("id")


def test_the_shells_own_predicate_is_satisfied():
    """Pin against shell #65's actual reader, not a restatement of it: the
    check greps a fixed set of age/url keys, so a rename that satisfied this
    file while missing the shell would be a green test over a red board."""
    shell = importlib.import_module("routes.agentic_loop_master_shell")
    block = pu.published_updates(force=True)
    for w in block["withheld"]:
        has_age, has_url = shell._withheld_carries(w)
        assert has_age and has_url, "shell reads %s as lacking" % w.get("id")


def test_archived_is_not_reported_as_awaiting_a_decision():
    """★ The core misreading. Archived = decided; it must not inflate a queue."""
    block = pu.published_updates(force=True)
    assert block["awaiting_decision_count"] == 0, (
        "every withheld entry in the shipped store is archived, so nothing is "
        "awaiting an owner's decision")
    assert block["retired_count"] == block["withheld_count"]
    for w in block["withheld"]:
        assert w["status"] == "archived"
        assert w["awaiting_decision"] is False
        assert "retired" in w["reason"]


def test_a_genuinely_unapproved_entry_is_still_pending():
    """The control: the archived carve-out must not swallow a real pending
    item. A draft, a typo'd status and a missing status all still await."""
    # NB "Published " / "PUBLISHED" DO publish — _is_published strips and
    # lowercases on purpose. That tolerance is pre-existing and deliberate.
    for st in ("draft", "readyy", None, "", "publish", "approved"):
        card, why = pu._card(_entry(status=st), pu.METRIC_TOKENS)
        assert card is None, "status %r must not publish" % st
        assert "not approved" in why, "status %r lost its pending reason" % st
        w = pu._withheld_entry(_entry(status=st), why)
        assert w["awaiting_decision"] is True, "status %r stopped awaiting" % st


def test_published_still_publishes():
    """The must-stay-green control for the approval gate itself."""
    card, why = pu._card(_entry(status="published"), pu.METRIC_TOKENS)
    assert card is not None and why is None


def test_a_missing_date_is_null_age_not_zero():
    """None is a real answer. Inventing 0 would turn a missing date into a
    brand-new item and hide exactly what the check looks for."""
    w = pu._withheld_entry(_entry(status="draft", announced=None), "not approved (x)")
    assert w["age_days"] is None and w["age_hours"] is None
    assert pu._age_days("") is None
    assert pu._age_days("not-a-date") is None
    assert pu._age_days(None) is None


def test_age_days_counts_real_days():
    d = (_dt.date.today() - _dt.timedelta(days=25)).isoformat()
    assert pu._age_days(d) == 25
    w = pu._withheld_entry(_entry(status="draft", announced=d), "not approved (x)")
    assert w["age_days"] == 25 and w["age_hours"] == 600


def test_the_feed_never_raises_on_a_malformed_entry():
    """This block is spliced into the public /whats-new feed."""
    w = pu._withheld_entry("not-a-dict", "unreadable")
    assert w["decision_url"] and w["awaiting_decision"] is True


# ── decide_today: an unreadable inbox is not an empty one ──────────────────

def test_an_unreadable_inbox_is_reported_not_silently_dropped():
    """★ decide_today is built AFTER all four lanes, on leftover budget, and
    _q() returns None (never []) when that budget is spent. `for r in rows or []`
    turned the refusal into no rows at all — so the one queue with a real
    one-click endpoint vanished and the board read "nothing to decide".

    Measured on prod 2026-08-23: tick_ms=9398 vs budget.seconds=11 — the read
    was refused, while lane 2's earlier b_collapse_ratio counted 11 open rows in
    the same two statuses at the same moment.
    """
    shell = importlib.import_module("routes.agentic_loop_master_shell")
    items = shell._decide_today({"conn": None}, limit=5)
    unread = [i for i in items if i.get("kind") == "unreadable"]
    assert unread, "an unreadable inbox produced no marker — absence is silent"
    assert "NOT a claim" in unread[0]["title"]
    assert unread[0]["id"] == "squasher_work_queue"


def test_a_readable_empty_inbox_emits_no_false_alarm():
    """The must-stay-green control: genuinely empty must NOT look unreadable."""
    shell = importlib.import_module("routes.agentic_loop_master_shell")
    import contextlib

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []

    class _Conn:
        autocommit = True
        def cursor(self): return _Cur()

    items = shell._decide_today({"conn": _Conn()}, limit=5)
    assert not [i for i in items if i.get("kind") == "unreadable"], (
        "a readable, empty inbox must not report itself unreadable")
