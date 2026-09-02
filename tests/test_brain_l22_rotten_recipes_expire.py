"""L22 rotten recipes retire instead of replaying (2026-09-02 brain-agents
sweep finding 7). Measured: master-tick 2026-09-01T22:24Z tier2.l22_draft_prs
skipped 14/14 — "patched file fails ast.parse" ×10 and "search text not in
main.py (file changed since proposal)" ×4 — the same ids every tick.

draft_prs_run counts rotten skips on the proposal (draft_skips,
last_skip_reason); the master tick's tier2.draft_pr_expire_rotten marks a
proposal `stale` after ROTTEN_SKIP_THRESHOLD, and pending-pr (status =
'proposed') stops serving it.
"""
from __future__ import annotations

import inspect

import pytest

ba = pytest.importorskip("routes.brain_backlog_admin")


class _Cur:
    def __init__(self, answers=None, rowcount=1):
        self.answers = answers or {}
        self.calls = []
        self.rowcount = rowcount
        self._last = ""

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.calls.append((flat, params))
        self._last = flat

    def _rows(self):
        for key, rows in self.answers.items():
            if key in self._last:
                return rows
        return []

    def fetchone(self):
        r = self._rows()
        return r[0] if r else None

    def fetchall(self):
        return list(self._rows())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


_ALL = [("id",), ("status",), ("pr_url",), ("reviewer_note",),
        ("draft_skips",), ("last_skip_reason",), ("last_skip_at",)]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ba, "_SKIP_COLUMNS_ENSURED", False)


def _sqls(cur, needle):
    return [(s, p) for s, p in cur.calls if needle in s]


# ── what counts as rot ──────────────────────────────────────────────────────
@pytest.mark.parametrize("reason", [
    "patched file fails ast.parse: invalid syntax (<unknown>, line 3)",
    "search text not in main.py (file changed since proposal)",
    "file not in main: routes/gone.py",
])
def test_the_measured_reasons_are_rot(reason):
    assert ba.is_rotten_skip(reason) is True


@pytest.mark.parametrize("reason", [
    "duplicate_open_pr", "no changes in proposal", "no-op edit",
    "multi-file proposal; handled by GH Actions workflow only (this helper is single-file)",
    "search text appears 2× in main.py (ambiguous)", "", None,
])
def test_transient_or_structural_skips_are_not_rot(reason):
    assert ba.is_rotten_skip(reason) is False


def test_rotten_skip_ids_keeps_only_rot_with_an_id():
    skipped = [{"id": 140, "reason": "patched file fails ast.parse: x"},
               {"id": 133, "reason": "search text not in main.py (file changed since proposal)"},
               {"id": 7, "reason": "duplicate_open_pr"},
               {"reason": "patched file fails ast.parse"}, "junk", {"id": "x", "reason": "file not in main: a"}]
    assert ba.rotten_skip_ids(skipped) == [
        (140, "patched file fails ast.parse: x"),
        (133, "search text not in main.py (file changed since proposal)")]


# ── the counter ─────────────────────────────────────────────────────────────
def test_record_draft_skips_increments_each_rotten_proposal_once_guarded_on_no_pr(monkeypatch):
    cur = _Cur({"information_schema.columns": _ALL})
    conn = _Conn(cur)
    monkeypatch.setattr(ba, "_pg_conn", lambda: conn)
    out = ba.record_draft_skips([
        {"id": 140, "reason": "patched file fails ast.parse: x"},
        {"id": 133, "reason": "search text not in main.py (file changed since proposal)"},
        {"id": 7, "reason": "duplicate_open_pr"}])
    assert out == {"ok": True, "rotten": 2, "counted": 2}
    ups = _sqls(cur, "SET draft_skips = COALESCE(draft_skips, 0) + 1")
    assert [p[1] for _, p in ups] == [140, 133]
    for sql, _ in ups:
        assert "WHERE id = %s AND pr_url IS NULL" in sql
        assert "last_skip_reason = %s" in sql
    assert conn.commits == 1


def test_record_draft_skips_adds_only_the_missing_columns(monkeypatch):
    cur = _Cur({"information_schema.columns": [("id",), ("status",), ("draft_skips",)]})
    monkeypatch.setattr(ba, "_pg_conn", lambda: _Conn(cur))
    ba.record_draft_skips([{"id": 1, "reason": "file not in main: x"}])
    alters = [s for s, _ in cur.calls if s.startswith("ALTER TABLE brain_proposed_code_fixes")]
    assert len(alters) == 2 and not any("ADD COLUMN draft_skips" in a for a in alters)


def test_record_draft_skips_with_nothing_rotten_touches_no_db(monkeypatch):
    def _boom():
        raise AssertionError("must not connect")
    monkeypatch.setattr(ba, "_pg_conn", _boom)
    assert ba.record_draft_skips([{"id": 7, "reason": "duplicate_open_pr"}])["counted"] == 0


def test_record_draft_skips_is_fail_soft(monkeypatch):
    monkeypatch.setattr(ba, "_pg_conn", lambda: None)
    out = ba.record_draft_skips([{"id": 1, "reason": "file not in main: x"}])
    assert out["ok"] is False and out["counted"] == 0


def test_draft_prs_run_counts_its_skips():
    src = inspect.getsource(ba.draft_prs_run)
    assert "record_draft_skips(skipped)" in src
    assert "rotten_skips=rotten" in src


# ── the expiry ──────────────────────────────────────────────────────────────
def test_expire_marks_stale_at_the_threshold_and_reports_ids(monkeypatch):
    cur = _Cur({"information_schema.columns": _ALL, "RETURNING id": [(140,), (133,)]})
    conn = _Conn(cur)
    monkeypatch.setattr(ba, "_pg_conn", lambda: conn)
    out = ba.expire_rotten_proposals()
    assert out["ok"] is True and out["marked_stale"] == 2 and out["ids"] == [140, 133]
    assert out["threshold"] == ba.ROTTEN_SKIP_THRESHOLD == 3
    (sql, params), = _sqls(cur, "SET status = 'stale'")
    assert "COALESCE(status, 'proposed') = 'proposed'" in sql
    assert "pr_url IS NULL AND COALESCE(draft_skips, 0) >= %s" in sql
    assert params[-1] == 3
    assert "reviewer_note = COALESCE(reviewer_note, '') || %s" in sql
    assert conn.commits == 1


def test_expire_is_fail_soft(monkeypatch):
    monkeypatch.setattr(ba, "_pg_conn", lambda: None)
    out = ba.expire_rotten_proposals()
    assert out["ok"] is False and out["marked_stale"] == 0


def test_the_master_tick_runs_the_expiry_beside_draft_pr_expire():
    orch = pytest.importorskip("routes.brain_master_orchestrator")
    src = inspect.getsource(orch._run_master_tick)
    assert src.count('"tier2.draft_pr_expire_rotten"') == 2   # success + error path
    assert "expire_rotten_proposals" in src and "_rot()" in src
    assert src.index('"tier2.draft_pr_expire"') < src.index('"tier2.draft_pr_expire_rotten"')


def test_pending_pr_stops_serving_a_stale_row():
    """The exit: pending-pr serves status='proposed' only, so `stale` leaves
    the pool the moment it is marked."""
    l5 = pytest.importorskip("routes.brain_v2_layer5")
    src = inspect.getsource(l5.proposed_code_pending_pr)
    assert "COALESCE(status, 'proposed') = 'proposed'" in src
