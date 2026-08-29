"""tests/test_squasher_remit_and_lessons.py — the loop consults the lesson it
already wrote (2026-08-29).

Lane 7 of the wiring shell.

Every enqueue pre-registers a `fix` claim; at 7d the L16 tick judges it, and
REFUTED means the lane took the finding on and no fix landed. Those refutations
already flow into claim_lessons — a NEGATIVE_LESSON_CORPUS — and come back as
recall on a DECISION.

But this queue's own dedup never asked. It deduped on IDENTITY (one open row
per finding_key) and on BUDGET, and neither knows this exact finding has been
taken on and failed before. So it is re-enqueued next sweep, burns another ~80s
model call, and is refuted again. closed_with_pr=0 alongside a recurrence rate
of 0.687 is what that looks like from the inside: a loop that remembers in a
corpus it does not consult.

Ways this could go wrong, one test each:
  (1) ★ UNREAD HISTORY READ AS NO HISTORY — the ledger is unreachable and the
      lane concludes there were no prior failures. An unread history is not an
      empty one.
  (2) ★ LEDGER OUTAGE STOPS THE LANE — the opposite error: refusing all work
      because the history could not be read.
  (3) LESSON IGNORED — a finding refuted twice is taken on a third time.
  (4) FIRST ATTEMPT BLOCKED — the limit is off by one and nothing is ever
      attempted.
  (5) REMIT UNNAMED — wiring work and Python-logic work are indistinguishable,
      so the 5-of-5-wrong class cannot even be measured.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_squasher_remit_and_lessons.py -v
"""
from __future__ import annotations

import pytest


def _q():
    from routes import squasher_queue as q
    return q


# ── (1)(2) ★ known vs. unknown history ───────────────────────────────────

def test_an_unreadable_ledger_reports_unknown_not_zero(monkeypatch):
    """★REGRESSION (1). `refuted: 0` with `known: False` must never be read as
    'no prior failures'."""
    q = _q()
    import routes.claim_ledger as cl

    def boom(finding_key, cur=None):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(cl, "refuted_fix_attempts", boom)
    out = q._prior_refutations("/api/x")
    assert out["known"] is False
    assert out["refuted"] == 0


def test_a_readable_empty_history_is_known(monkeypatch):
    """THE PAIRED CONTROL: a real 'no prior failures' must be distinguishable
    from an outage."""
    q = _q()
    import routes.claim_ledger as cl
    monkeypatch.setattr(cl, "refuted_fix_attempts",
                        lambda finding_key, cur=None: {"known": True, "refuted": 0,
                                                       "last": None})
    out = q._prior_refutations("/api/x")
    assert out["known"] is True and out["refuted"] == 0


# ── (3)(4) the lesson changes behaviour, at the right threshold ──────────

def _fake_enqueue_env(monkeypatch, q, prior):
    """Drive enqueue() far enough to reach the refutation gate."""
    monkeypatch.setattr(q, "_disabled", lambda: False)
    monkeypatch.setattr(q, "_prior_refutations", lambda k: prior)

    class Cur:
        def __init__(self): self.n = 0

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def execute(self, sql, params=None):
            self.n += 1

        def fetchone(self):
            return None            # no open row; budget query never reached

    class Conn:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def cursor(self): return Cur()

        def commit(self): pass

    monkeypatch.setattr(q, "_conn", lambda: Conn())
    monkeypatch.setattr(q, "_ensure_table", lambda cur: None)


def test_a_finding_refuted_twice_is_refused(monkeypatch):
    """★REGRESSION (3). Re-taking it identically is what a recurrence rate
    measures."""
    q = _q()
    _fake_enqueue_env(monkeypatch, q,
                      {"known": True, "refuted": 2, "last": {"at": "2026-08-01"}})
    out = q.enqueue("/api/v1/x", title="t")
    assert out["ok"] is False
    assert out["error"] == "prior_fixes_refuted"
    assert out["refuted"] == 2
    assert "recurrence" in out["reason"]


def test_a_first_attempt_is_not_blocked(monkeypatch):
    """★REGRESSION (4). A limit that blocks the first try is not a lesson,
    it is an off switch."""
    q = _q()
    _fake_enqueue_env(monkeypatch, q, {"known": True, "refuted": 0, "last": None})
    out = q.enqueue("/api/v1/x", title="t")
    assert out.get("error") != "prior_fixes_refuted"


def test_one_refutation_still_gets_another_attempt(monkeypatch):
    """Two is the point where 'maybe it was transient' stops being the better
    explanation. One refutation is not yet a pattern."""
    q = _q()
    _fake_enqueue_env(monkeypatch, q, {"known": True, "refuted": 1, "last": None})
    out = q.enqueue("/api/v1/x", title="t")
    assert out.get("error") != "prior_fixes_refuted"


def test_an_unknown_history_does_not_refuse(monkeypatch):
    """★REGRESSION (2). Fail-OPEN on an unreadable ledger: refusing work
    because the history could not be read would let a ledger outage silently
    stop the lane."""
    q = _q()
    _fake_enqueue_env(monkeypatch, q, {"known": False, "refuted": 9, "last": None})
    out = q.enqueue("/api/v1/x", title="t")
    assert out.get("error") != "prior_fixes_refuted"


def test_the_limit_is_the_named_constant(monkeypatch):
    q = _q()
    assert q.REPEAT_REFUTATION_LIMIT == 2
    _fake_enqueue_env(monkeypatch, q,
                      {"known": True, "refuted": q.REPEAT_REFUTATION_LIMIT,
                       "last": None})
    assert q.enqueue("/api/v1/x")["error"] == "prior_fixes_refuted"


# ── (5) the remit is named on every row ──────────────────────────────────

def test_the_ledger_query_selects_only_refuted_fix_claims():
    """The lesson is specifically 'we tried and it did not hold'. Counting
    unobserved or confirmed claims would refuse work for the wrong reason."""
    import inspect
    from routes import claim_ledger
    src = inspect.getsource(claim_ledger.refuted_fix_attempts)
    assert '"fix"' in src and '"refuted"' in src
    assert "subject = %s" in src, "the query must key on THIS finding"


def test_refuted_fix_attempts_reports_unknown_when_the_cursor_raises():
    from routes.claim_ledger import refuted_fix_attempts

    class Cur:
        def execute(self, *a, **k):
            raise RuntimeError("relation does not exist")

        def fetchall(self):
            return []

    out = refuted_fix_attempts("/api/x", cur=Cur())
    assert out["known"] is False, "a failed read reported itself as a clean history"
    assert out["refuted"] == 0


def test_a_readable_cursor_counts_refutations():
    from routes.claim_ledger import refuted_fix_attempts

    class Cur:
        def execute(self, *a, **k): pass

        def fetchall(self):
            return [(None, "no fix landed", 168), (None, "still open", 168)]

    out = refuted_fix_attempts("/api/x", cur=Cur())
    assert out["known"] is True
    assert out["refuted"] == 2
    assert out["last"]["horizon_hours"] == 168
