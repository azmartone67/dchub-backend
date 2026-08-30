"""A deferred schema migration is not a failed job (2026-08-30, follow-up to #3366).

#3366 stopped the schema DDL from running per request, which is right and which
removed most of the exposure. It did not make the remaining exposure survivable,
and it named the wrong holder.

THE HOLDER
==========
Not overlapping sweeps. Sweeps 27s apart cannot block each other on a statement
that completes in milliseconds, and that theory does not explain
squasher_queue_drain failing in the same INSTANT on a DIFFERENT table
(squasher_work_queue vs welcome_email_log), nor ai_surface_audit_2h degrading
beside them.

It is the nightly pg_dump — .github/workflows/backup-neon-r2.yml, cron
"31 9 * * *" since 2026-08-30 ("0 8 * * *" on the day below) — which opens ONE
repeatable-read transaction, takes AccessShareLock
on EVERY table, and holds all of them until it commits:

    2026-08-30T08:00:28Z → 2026-08-30T08:16:12Z   success (15m44s)

All five self_reported_failure rows (08:04:51, 08:09:22, 08:14:29) sit inside
that window and nothing failed after it. One holder, many waiters — which is
also why three unrelated jobs degraded in one instant.

WHY ONCE-PER-PROCESS IS NOT ENOUGH
==================================
It makes the race rarer, not absent. A process whose FIRST call lands inside the
daily window still blocks, and a deploy, a worker recycle or a scale event puts a
fresh process there most days. When it blocks, the ALTER raises — and on a
non-autocommit connection that aborts the CALLER's transaction, so every
statement after it dies InFailedSqlTransaction. The cron lane returns
`{"ok": false}` and the outcomes table records a failure for what was a no-op
migration on a table that already had every column.

routes/hosting_capacity_ingest.py:2379 has done this correctly since it was
written: short lock_timeout, never let it fail the run, introspect the live
columns instead. This applies the same rule to the two lanes that actually went
red.

Tests never import main.
"""
from __future__ import annotations

import pytest

import founder_note as fn
from routes import squasher_queue as sq

# psycopg2 surfaces the lock timeout as SQLSTATE 55P03; the fake reproduces the
# message that actually reached the cron outcomes table.
LOCK_TIMEOUT = "canceling statement due to lock timeout\n"


class _Cur:
    """Records SQL. `autocommit` selects whether the savepoint path is taken,
    the way a real psycopg2 cursor's .connection does."""

    def __init__(self, autocommit=False, fail_on=None):
        self.sql = []
        self.fail_on = fail_on
        self.connection = type("_C", (), {"autocommit": autocommit})()

    def execute(self, q, *a):
        self.sql.append(" ".join(str(q).split()))
        if self.fail_on and self.fail_on in self.sql[-1]:
            raise RuntimeError(LOCK_TIMEOUT)

    def alters(self):
        return [q for q in self.sql if "ADD COLUMN IF NOT EXISTS" in q]


@pytest.fixture(autouse=True)
def _clean():
    fn._reset_for_tests()
    sq._reset_for_tests()
    yield
    fn._reset_for_tests()
    sq._reset_for_tests()


# ── 1 · a locked table does not fail the lane ─────────────────────────────

@pytest.mark.parametrize("boom", [
    "CREATE TABLE IF NOT EXISTS squasher_work_queue",
    "ADD COLUMN IF NOT EXISTS attempts",
    "ADD COLUMN IF NOT EXISTS action_class",
    "ADD COLUMN IF NOT EXISTS last_seen",
    "CREATE UNIQUE INDEX IF NOT EXISTS squasher_queue_open_uniq",
])
def test_a_locked_squasher_table_never_raises_at_the_caller(boom):
    """Whichever statement loses the race, drain() must not see an exception —
    it returns {"ok": false} on any, and that is the red row."""
    sq._ensure_table(_Cur(fail_on=boom))


def test_a_locked_welcome_email_log_never_raises_at_the_sweep():
    fn._ensure_log_schema(_Cur(autocommit=True, fail_on="ALTER TABLE"))


# ── 2 · and the failure does not poison the caller's transaction ──────────

def test_squasher_ddl_is_savepoint_isolated_on_a_transactional_connection():
    """Unguarded, a raising ALTER aborts the CALLER's transaction and every
    statement after it dies InFailedSqlTransaction — drain() reads the queue
    immediately after this call."""
    cur = _Cur(fail_on="ADD COLUMN IF NOT EXISTS attempts")
    sq._ensure_table(cur)
    failed = next(i for i, s in enumerate(cur.sql)
                  if "ADD COLUMN IF NOT EXISTS attempts" in s)
    opened = cur.sql.index("SAVEPOINT sq_schema")
    rolled = cur.sql.index("ROLLBACK TO SAVEPOINT sq_schema")
    assert opened < failed < rolled, (
        "the DDL was not enclosed in a savepoint that rolled back after it "
        "failed; the caller's transaction is aborted")
    assert "RELEASE SAVEPOINT sq_schema" not in cur.sql, (
        "a failed block must not release its savepoint")


def test_a_clean_squasher_pass_releases_its_savepoint():
    cur = _Cur()
    sq._ensure_table(cur)
    assert "SAVEPOINT sq_schema" in cur.sql
    assert "RELEASE SAVEPOINT sq_schema" in cur.sql
    assert "ROLLBACK TO SAVEPOINT sq_schema" not in cur.sql


def test_no_savepoint_is_issued_on_an_autocommit_connection():
    """SAVEPOINT outside a transaction block raises outright — the psycopg2
    savepoint/autocommit trap. There, statement isolation is already the
    default."""
    cur = _Cur(autocommit=True)
    sq._ensure_table(cur)
    assert not [s for s in cur.sql if "SAVEPOINT" in s]


def test_founder_note_never_issues_a_savepoint():
    cur = _Cur(autocommit=True)
    fn._ensure_log_schema(cur)
    assert cur.alters(), "guard is vacuous — no DDL ran at all"
    assert not [s for s in cur.sql if "SAVEPOINT" in s]


# ── 3 · a deferred pass is not a completed pass ───────────────────────────

@pytest.mark.parametrize("boom", ["ADD COLUMN IF NOT EXISTS attempts",
                                  "CREATE TABLE IF NOT EXISTS squasher_work_queue"])
def test_a_swallowed_squasher_failure_still_retries_next_call(boom):
    """Swallowing must not become skipping: the columns never landed."""
    sq._ensure_table(_Cur(fail_on=boom))
    retry = _Cur()
    sq._ensure_table(retry)
    assert retry.alters(), "a deferred migration was remembered as applied"


def test_a_swallowed_founder_note_failure_still_retries_next_sweep():
    fn._ensure_log_schema(_Cur(autocommit=True, fail_on="ALTER TABLE"))
    retry = _Cur(autocommit=True)
    fn._ensure_log_schema(retry)
    assert retry.alters(), "a deferred migration was remembered as applied"


# ── 4 · the v2 open-row index keeps its retry ─────────────────────────────

def test_a_failed_open_index_does_not_mark_the_schema_done():
    """_ensure_open_index cannot land until collapse_duplicate_open_rows() has
    removed the duplicate open rows it forbids, and the NEXT drain is what does
    that — its docstring says so. #3366 discarded its verdict and set the flag
    anyway, which retires that retry after one call and leaves the v2 guard
    permanently absent for the life of the process."""
    blocked = _Cur(fail_on=sq._OPEN_INDEX)
    sq._ensure_table(blocked)
    assert sq._OPEN_INDEX in " ".join(blocked.sql), "guard is vacuous"

    retry = _Cur()
    sq._ensure_table(retry)
    assert sq._OPEN_INDEX in " ".join(retry.sql), (
        "the v2 open-row guard was never retried — one row per finding_key "
        "stops being enforced for the life of the process")
