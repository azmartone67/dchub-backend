"""Schema DDL runs ONCE PER PROCESS, not once per request (2026-08-30).

`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` acquires ACCESS EXCLUSIVE **before**
it evaluates the condition. A no-op ALTER on a column that already exists
therefore still takes the strongest lock Postgres has, and conflicts with
everything on that table — plain SELECTs included.

Reproduced on PostgreSQL 18.4, with one ordinary reader held open:

    ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT  (c EXISTS) -> canceling
                                            statement due to lock timeout
    CREATE TABLE IF NOT EXISTS t              (t EXISTS) -> OK, 0.00s

That is the production error string verbatim. The CREATE is innocent; the
ALTER is not.

Both callers below re-ran their whole DDL block on EVERY request —
founder_note from find_candidates() (whose cron predicate is `lambda now:
True`, so it fired on every heartbeat across three schedulers) and
squasher_queue from nine request paths including drain(). They blocked
themselves and each other: five lock-timeout failures between 08:04:51 and
08:14:29 on 2026-08-30, recorded as self_reported_failure in the cron
outcomes table.

No network, no DB — a recording cursor is enough to prove the call count.
"""
import pytest

import founder_note as fn
from routes import squasher_queue as sq


class _Cur:
    """Records SQL. `.connection.autocommit` keeps _ensure_open_index on its
    unguarded path so this fixture does not have to model savepoints."""
    def __init__(self, fail_on=None):
        self.sql = []
        self.fail_on = fail_on
        self.connection = type("_C", (), {"autocommit": True})()

    def execute(self, q, *a):
        self.sql.append(" ".join(str(q).split()))
        if self.fail_on and self.fail_on in self.sql[-1]:
            raise RuntimeError("canceling statement due to lock timeout")

    def alters(self):
        return [q for q in self.sql if "ADD COLUMN IF NOT EXISTS" in q]


@pytest.fixture(autouse=True)
def _clean():
    fn._reset_for_tests()
    sq._reset_for_tests()
    yield
    fn._reset_for_tests()
    sq._reset_for_tests()


# ── the fix: once per process ─────────────────────────────────────────
@pytest.mark.parametrize("mod,call", [
    ("founder_note", lambda c: fn._ensure_log_schema(c)),
    ("squasher_queue", lambda c: sq._ensure_table(c)),
])
def test_ddl_runs_on_the_first_call_and_never_again(mod, call):
    first = _Cur()
    call(first)
    # ★ Anti-vacuous carrier. If the DDL stopped running AT ALL this test
    # would pass on an empty list forever while advertising coverage of a
    # schema step that no longer happens.
    assert first.alters(), f"{mod}: no ALTER ran on the FIRST call — guard is vacuous"

    for _ in range(5):
        again = _Cur()
        call(again)
        assert again.sql == [], (
            f"{mod}: re-ran DDL on a later call ({len(again.alters())} no-op "
            f"ALTERs, each ACCESS EXCLUSIVE). This is the defect: the lock is "
            f"taken before IF NOT EXISTS is evaluated.")


# ── it must not over-correct into skipping the schema forever ─────────
@pytest.mark.parametrize("mod,call,boom", [
    ("founder_note", lambda c: fn._ensure_log_schema(c), "ALTER TABLE welcome_email_log"),
    ("squasher_queue", lambda c: sq._ensure_table(c), "ADD COLUMN IF NOT EXISTS attempts"),
])
def test_a_failed_ensure_retries_rather_than_sticking(mod, call, boom):
    """★ The other direction. The flag is set only AFTER the DDL succeeds, so
    a call that loses the lock race must try again — not mark the schema done
    and leave the table unmigrated for the life of the process."""
    with pytest.raises(Exception):
        call(_Cur(fail_on=boom))

    retry = _Cur()
    call(retry)
    assert retry.alters(), (
        f"{mod}: a FAILED ensure marked the schema done — the next call "
        f"skipped DDL that never actually ran")


@pytest.mark.parametrize("mod,call", [
    ("founder_note", lambda c: fn._ensure_log_schema(c)),
    ("squasher_queue", lambda c: sq._ensure_table(c)),
])
def test_reset_hook_clears_the_process_flag(mod, call):
    call(_Cur())
    mid = _Cur()
    call(mid)
    assert mid.sql == [], "precondition: the flag must be set after one call"
    (fn if mod == "founder_note" else sq)._reset_for_tests()
    after = _Cur()
    call(after)
    assert after.alters(), f"{mod}: _reset_for_tests did not clear the flag"
