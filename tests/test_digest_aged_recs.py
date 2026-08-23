"""The weekly digest's AGE-based second selection — shell #65 lane 2.

WHY THIS FILE EXISTS. On 2026-08-23 the shell measured 298 strategic
recommendations at status='new' for more than 14 days, oldest 77d, and named
the mechanism exactly: render_weekly_digest read ONE ISO week via
_read_recs_for(week_of), so a row that aged out of its own week could never
appear in the only artifact that mails recommendations to a human — however
green the digest workflow ran. A green digest and 298 unreachable rows were
the same fact.

WHAT THESE PIN, each a real failure rather than a restatement:

  · The aged rows are NAMED in a populated digest (html AND plaintext).
  · A week with NO new synthesis still mails the aged rows. This was the
    second half of the trap: `if not recs` returned is_empty=True, and
    send_weekly_digest turns is_empty into "no_recommendations_yet" and mails
    NOTHING — so the backlog was invisible twice over.
  · Truly empty (no week recs AND no aged rows) is still is_empty.
  · An UNREADABLE backlog is not a zero. _read_stale_recs fail-closes to None
    like _read_recs_for, and the section renders its unreadable state instead
    of a reassuring "nothing has aged out".
  · A truncated list always prints the TRUE total beside what it listed, so a
    silent cap cannot read as full coverage.

CI-SAFETY: no DB and no network — the reader is stubbed at the module boundary.
"""
from __future__ import annotations

import datetime as _dt
import importlib

import pytest

bwd = importlib.import_module("routes.brain_weekly_digest")


WEEK = _dt.date(2026, 8, 17)


def _aged(n, start_age=77.0):
    return [{"id": 1000 + i, "title": "Aged rec %d" % i, "kind": "strategic_gap_4w",
             "week_of": _dt.date(2026, 6, 8), "created_at": None, "pr_url": None,
             "age_days": start_age - i} for i in range(n)]


def _week_recs():
    return [{"kind": "synthesis_meta", "title": "meta", "spec_md": "",
             "strategy_payload": {"summary": "This week ran."}},
            {"kind": "strategic_gap_4w", "title": "Fresh gap this week",
             "spec_md": "do the thing", "pr_url": None}]


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Never touch a database in CI."""
    monkeypatch.setattr(bwd, "_get_db", lambda: None, raising=True)


def _patch(monkeypatch, week_recs, stale, total, err=None):
    import routes.brain_strategic_planner as planner
    monkeypatch.setattr(planner, "_read_recs_for", lambda w: week_recs,
                        raising=True)
    monkeypatch.setattr(planner, "_week_of_iso", lambda: WEEK, raising=True)
    monkeypatch.setattr(bwd, "_read_stale_recs",
                        lambda *a, **k: (stale, total, err), raising=True)


def test_aged_rows_are_named_in_a_populated_digest(monkeypatch):
    """The whole point: a rec outside this ISO week reaches the email."""
    _patch(monkeypatch, _week_recs(), _aged(3), 298)
    out = bwd.render_weekly_digest(WEEK)

    assert out["is_empty"] is False
    assert out["stale_count"] == 298
    assert "Aged rec 0" in out["html"], "aged row missing from the HTML body"
    assert "Aged rec 0" in out["text"], "aged row missing from the plaintext"
    assert "still open after" in out["html"]
    # the fresh week's content is NOT displaced by the aged section
    assert "Fresh gap this week" in out["html"]


def test_no_synthesis_this_week_still_mails_the_aged_rows(monkeypatch):
    """★ The second half of the trap. `not recs` used to mean is_empty=True,
    which send_weekly_digest turns into "no_recommendations_yet" — no email at
    all, so the backlog was invisible twice over."""
    _patch(monkeypatch, [], _aged(3), 298)
    out = bwd.render_weekly_digest(WEEK)

    assert out["is_empty"] is False, (
        "a week with no synthesis but 298 aged rows must still send")
    assert out["stale_count"] == 298
    assert "Aged rec 0" in out["html"]
    assert "298" in out["subject"], "the subject must carry the backlog size"


def test_truly_empty_is_still_empty(monkeypatch):
    """The control: no week recs AND no aged rows really is nothing to say."""
    _patch(monkeypatch, [], [], 0)
    out = bwd.render_weekly_digest(WEEK)
    assert out["is_empty"] is True
    assert out["stale_count"] == 0


def test_unreadable_backlog_is_not_a_zero(monkeypatch):
    """FAIL-CLOSED. A DB blip must not render "nothing has aged out"."""
    _patch(monkeypatch, _week_recs(), None, None, "OperationalError: boom")
    out = bwd.render_weekly_digest(WEEK)

    assert out["is_empty"] is False
    assert "unreadable" in out["html"].lower()
    assert "NOT a claim" in out["html"] or "not a claim" in out["html"].lower()


def test_unreadable_backlog_with_no_week_recs_does_not_claim_empty(monkeypatch):
    """`stale is None` is unknown, not empty — it must not take the is_empty
    path, which would mail nothing and record "no recommendations yet"."""
    _patch(monkeypatch, [], None, None, "OperationalError: boom")
    out = bwd.render_weekly_digest(WEEK)
    assert out["is_empty"] is False, (
        "an unreadable backlog must never be reported as an empty one")


def test_truncation_names_the_true_total(monkeypatch):
    """No silent caps: listing 25 of 298 must print 298."""
    _patch(monkeypatch, _week_recs(), _aged(25), 298)
    out = bwd.render_weekly_digest(WEEK)
    assert "Showing 25 of 298" in out["html"]
    assert "273 more" in out["html"]


def test_reader_is_week_independent():
    """AST-level: the aged query must not filter on week_of. A reader that
    reintroduced a week bound would restore the exact defect."""
    import ast
    import inspect
    src = inspect.getsource(bwd._read_stale_recs)
    tree = ast.parse(src.lstrip())
    sql = " ".join(n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert "status = 'new'" in sql
    assert "make_interval" in sql, "the age bound must be a bound parameter"
    assert "week_of =" not in sql, (
        "the aged pass must not filter by ISO week — that is the defect")
