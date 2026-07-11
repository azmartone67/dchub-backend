"""
tests/test_brain_reasoning_lane_freshness.py — the 07-11 lane-stall fix.

NO DB, NO network, NO model: every DB/LLM touch is a fake or a monkeypatch.

THE BUG (observed 07-06 → 07-11): _write_candidate UPSERTs on
(finding_issue, finding_url) and only bumps updated_at, but _already_reasoned
judged freshness on created_at. Once a chronic top-ranked finding's candidate
aged past 7 days it was permanently "not already reasoned": every master tick
re-reasoned the SAME finding (1 LLM call each, ~12/day), the UPSERT refreshed
only updated_at, and REASON_PER_TICK=1 meant the budget never reached a new
finding — brain_reasoning_candidates gained zero new rows after 07-06 while
the lane kept burning budget.

Covers:
  · _already_reasoned's freshness predicate uses updated_at (the regression
    fence — created_at-only is the bug);
  · a fresh row ⇒ True, no row ⇒ False, DB error ⇒ fail-open False;
  · drain_reasoning_lane skips an already-reasoned top finding WITHOUT
    consuming the per-tick budget, and spends the budget on the NEXT
    unreasoned finding (the exact behavior the bug broke).
"""
import pytest

lane = pytest.importorskip("routes.brain_reasoning_lane")


# ── fakes ─────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, row=None, raise_on_execute=False):
        self.row = row
        self.raise_on_execute = raise_on_execute
        self.executed = []

    def execute(self, sql, params=None):
        if self.raise_on_execute:
            raise RuntimeError("boom")
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


# ── _already_reasoned: the freshness predicate ───────────────────────
def test_freshness_predicate_uses_updated_at():
    """The regression fence: the SQL must judge freshness on updated_at (the
    column the UPSERT refreshes), not created_at alone."""
    cur = _FakeCursor(row=(1,))
    lane._already_reasoned(_FakeConn(cur), "issue_x", "url_x")
    assert len(cur.executed) == 1
    sql = cur.executed[0][0]
    assert "updated_at" in sql, (
        "freshness must consider updated_at — created_at-only re-reasons the "
        "same aged finding every tick (the 07-06 lane stall)")
    assert "7 days" in sql


def test_already_reasoned_true_when_fresh_row():
    assert lane._already_reasoned(_FakeConn(_FakeCursor(row=(1,))), "i", "u") is True


def test_already_reasoned_false_when_no_row():
    assert lane._already_reasoned(_FakeConn(_FakeCursor(row=None)), "i", "u") is False


def test_already_reasoned_fails_open_on_db_error():
    conn = _FakeConn(_FakeCursor(raise_on_execute=True))
    assert lane._already_reasoned(conn, "i", "u") is False
    assert conn.rolled_back is True


# ── drain: skip fresh candidates WITHOUT consuming the budget ────────
def test_drain_skips_reasoned_and_advances_budget(monkeypatch):
    """Top-ranked finding already reasoned ⇒ skipped_existing (no LLM call, no
    budget), and the per-tick budget of 1 is spent on the NEXT finding. Under
    the bug, the top finding was never 'already reasoned' once its candidate
    aged 7d, so the budget re-burned on it forever and 'b' was never reached."""
    monkeypatch.setattr(lane, "_enabled", lambda: True)
    monkeypatch.setattr(lane, "_conn", lambda: _FakeConn(_FakeCursor()))
    monkeypatch.setattr(lane, "init_reasoning_lane_schema", lambda: None)
    monkeypatch.setattr(
        lane, "_rank_open_findings",
        lambda conn, k: [{"issue": "chronic_top", "url": "u1"},
                         {"issue": "fresh_next", "url": "u2"}])
    monkeypatch.setattr(
        lane, "_already_reasoned",
        lambda conn, issue, url: issue == "chronic_top")
    reasoned_issues = []

    def fake_structured_action(finding):
        reasoned_issues.append(finding["issue"])
        return {"type": "code", "action": "do x", "realizes": "y",
                "confidence": 0.9, "rationale": "r", "model": "m", "error": None}

    monkeypatch.setattr(lane, "_structured_action", fake_structured_action)
    monkeypatch.setattr(
        lane, "_write_candidate", lambda conn, f, sa, t, r: True)

    summary = lane.drain_reasoning_lane(topk=5, per_tick=1)

    assert summary["ok"] is True
    assert summary["skipped_existing"] == 1
    assert summary["reasoned"] == 1
    assert summary["written"] == 1
    # The budget went to the UNREASONED finding, not the chronic one.
    assert reasoned_issues == ["fresh_next"]


def test_drain_dark_without_flag(monkeypatch):
    monkeypatch.setattr(lane, "_enabled", lambda: False)
    summary = lane.drain_reasoning_lane()
    assert summary["ok"] is True
    assert summary["reasoned"] == 0
    assert "dark" in (summary.get("note") or "")
