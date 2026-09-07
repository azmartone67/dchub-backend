"""brain_learning.sync_merge_outcomes — the mirror that carries settled
mechanical-fix verdicts into the column the L5 calibration reads.

WHY THIS EXISTS. `_calibration_stats` (brain_v2_layer5) tunes the auto-PR
threshold only once a loop_name has >= _CALIB_MIN_SAMPLES (3) rows with a
non-NULL merge_outcome. Measured live 2026-09-07: that count was 0 for EVERY
loop_name, so the threshold had never left _CALIB_BASE_THRESHOLD (0.85) since
the feature shipped, while the best pending proposal scored 0.83. The
verdicts existed the whole time — 47 settled code results in
brain_fix_outcomes — they were simply never carried across.

THE ONE THAT MATTERS is test_doc_only_rows_are_excluded_by_the_query. Of the
47 settled verdicts, 8 are doc-only rows (reconciler backfills carry
file_path='github:<branch>'), and 7 of those were graded still_broken=TRUE on
2026-07-10 — the day BEFORE the doc-only honesty rule shipped. Mirroring them
would record a fix verdict for a markdown note and teach the threshold to
trust a producer that ships nothing. `file_path NOT LIKE 'github:%'` is what
stops that, so it is asserted structurally and killed by mutation.

Stdlib + pytest only; no DB, no network. CI installs nothing else.
"""
import re

import pytest

from routes import brain_learning as bl


class _Cur:
    """Captures the statement and params; replays RETURNING rows."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = None
        self.params = None
        self.rowcount = len(self.rows)

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


@pytest.fixture()
def wired(monkeypatch):
    def _go(rows):
        cur = _Cur(rows)
        monkeypatch.setattr(bl, "_conn", lambda: _Conn(cur))
        monkeypatch.setattr(bl, "close_quietly", lambda c: None)
        return cur

    return _go


def test_doc_only_rows_are_excluded_by_the_query(wired):
    """The carve-out, asserted structurally: the doc-only predicate must sit
    in the row-selecting subquery, and appear exactly once so a second
    unguarded path cannot hide behind a substring match."""
    cur = wired([])
    bl.sync_merge_outcomes()
    sql = " ".join(cur.sql.split())
    assert sql.count("file_path NOT LIKE 'github:%%'") == 1
    # it must guard the SELECT that picks rows, not sit in a trailing clause
    sub = sql[sql.index("SELECT DISTINCT ON"):sql.index(") v")]
    assert "file_path NOT LIKE 'github:%%'" in sub
    assert "bo.proposal_kind = 'code'" in sub
    assert "bo.still_broken IS NOT NULL" in sub


def test_case_polarity_is_pinned_by_param_order(wired):
    """still_broken TRUE must map to ineffective, FALSE to healthy. The CASE
    reads its two arms from params, so their order IS the mapping — swapping
    the arms silently inverts every verdict, and inverted verdicts would
    teach the calibration exactly backwards."""
    cur = wired([])
    bl.sync_merge_outcomes()
    assert cur.params[0] == "merged_ineffective"   # WHEN still_broken THEN
    assert cur.params[1] == "merged_healthy"       # ELSE
    assert re.search(r"WHEN\s+v\.still_broken\s+THEN\s+%s\s+ELSE\s+%s",
                     " ".join(cur.sql.split()))


def test_newest_verdict_per_proposal_wins(wired):
    """A re-checked proposal has several brain_fix_outcomes rows. Without a
    deterministic pick the mirrored value depends on which duplicate the
    planner happens to return."""
    cur = wired([])
    bl.sync_merge_outcomes()
    sql = " ".join(cur.sql.split())
    assert "DISTINCT ON (bo.proposal_id)" in sql
    assert "ORDER BY bo.proposal_id, bo.checked_at DESC" in sql


def test_counts_report_what_was_written(wired):
    cur = wired([("merged_healthy",), ("merged_healthy",),
                 ("merged_ineffective",)])
    out = bl.sync_merge_outcomes()
    assert out["mirrored"] == 3
    assert out["healthy"] == 2
    assert out["ineffective"] == 1
    assert out["error"] is None


def test_only_fills_a_null_so_a_rerun_cannot_rewrite_history(wired):
    """The GG#4 push callback may already have written a row (one exists,
    from PR #2441 on 2026-08-08). A later mirror must not overwrite it."""
    cur = wired([])
    bl.sync_merge_outcomes()
    sql = " ".join(cur.sql.split())
    assert "q.merge_outcome IS NULL" in sql      # subquery: don't select it
    assert "AND p.merge_outcome IS NULL" in sql  # update: don't write it


def test_limit_is_clamped(wired):
    cur = wired([])
    bl.sync_merge_outcomes(limit=99999)
    assert cur.params[2] == 500
    bl.sync_merge_outcomes(limit=0)
    assert cur.params[2] == 1


def test_db_failure_is_reported_not_swallowed(monkeypatch):
    """A mirror that cannot run must say so — reporting 0 mirrored with no
    error is how the original callback failed as silence."""
    noted = {}

    class _Boom:
        def cursor(self):
            raise RuntimeError("connection reset")

        def close(self):
            pass

    monkeypatch.setattr(bl, "_conn", lambda: _Boom())
    monkeypatch.setattr(bl, "close_quietly", lambda c: None)
    monkeypatch.setattr(bl, "note_swallowed_write",
                        lambda *a, **k: noted.setdefault("hit", True))
    out = bl.sync_merge_outcomes()
    assert out["mirrored"] == 0
    assert "connection reset" in (out["error"] or "")
    assert noted.get("hit") is True
