#!/usr/bin/env python3
"""tests/test_eia_reseed_zero_write.py — a reseed that wrote NOTHING must not
report a completed refresh.

NO NETWORK, NO DB. upsert_plants() is exercised against a fake connection.

Observed in prod 2026-08-21 (Railway deploy logs):

    Truncated eia_generators (old data had no coordinates)
    Batch insert error at chunk 0: column "name" of relation
      "eia_generators" does not exist
      ✅ Inserted 0 plants into eia_generators
    ✅ DONE — <n> plants with <n> coordinates in eia_generators

Every chunk failed on a live-schema mismatch, and the run still printed two
check-marks. main() takes its "DONE" line from verify()'s row count, which
reads the OLD rows this reseed exists to replace — so a total write failure is
indistinguishable from a completed refresh in the logs.

★ The data survived only because the except-handler calls conn.rollback() and
TRUNCATE is transactional in PostgreSQL, so the truncate was undone with the
first failing chunk. That is luck, not design: had chunk 0 succeeded and a
later chunk failed, the TRUNCATE would already be committed. The
partial-write case is fenced here too, for that reason.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _Cur:
    """Cursor whose executes succeed and whose batch inserts fail on demand."""

    def __init__(self, fail_from=0):
        self.fail_from = fail_from
        self.calls = []

    def execute(self, sql, *a):
        self.calls.append(sql)

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def _plants(n):
    keys = ("plant_id", "name", "lat", "lng", "state", "county", "operator",
            "balancing_authority", "fuel_type", "capacity_mw",
            "generator_count", "status")
    return [{k: (i if k == "plant_id" else f"v{i}") for k in keys} for i in range(n)]


_last_conn = None
_last_cur = None


def _run(n_plants, fail_from):
    """Call the REAL upsert_plants with execute_values patched to fail.

    fail_from=0 -> every chunk fails (the prod case). fail_from=1 -> chunk 0
    lands and the rest fail (the partial-write case)."""
    global _last_conn, _last_cur
    import eia_generator_reseed as mod

    seen = {"chunks": 0}

    def fake_execute_values(cur, sql, chunk, template=None):
        idx = seen["chunks"]
        seen["chunks"] += 1
        if idx >= fail_from:
            raise Exception('column "name" of relation "eia_generators" does not exist')

    real = mod.execute_values
    mod.execute_values = fake_execute_values
    cur = _Cur()
    conn = _Conn(cur)
    _last_conn, _last_cur = conn, cur
    try:
        return mod.upsert_plants(conn, _plants(n_plants)), conn, cur
    finally:
        mod.execute_values = real


def test_a_reseed_that_wrote_nothing_raises():
    """0 of N inserted is a failure, not a quiet return of 0."""
    try:
        _run(1200, fail_from=0)
    except RuntimeError as e:
        msg = str(e)
        assert "wrote NOTHING" in msg, msg
        assert "information_schema.columns" in msg, (
            "the error must name the actual diagnostic — the live schema — not "
            "just say it failed"
        )
        return
    raise AssertionError(
        "upsert_plants returned normally after every chunk failed. That is the "
        "prod behaviour: '✅ Inserted 0 plants' followed by '✅ DONE'."
    )


def test_the_truncate_is_rolled_back_on_failure():
    """The property the surviving data depends on.

    TRUNCATE and the failing INSERT share a transaction, so the except-handler's
    rollback undoes the truncate. If someone removes that rollback, an empty
    table gets committed."""
    try:
        _run(600, fail_from=0)
    except RuntimeError:
        pass  # the guard fires before returning; the recorded state is what matters
    assert any("TRUNCATE" in c for c in _last_cur.calls), "no TRUNCATE issued"
    assert _last_conn.rollbacks >= 1, (
        "the failing chunk must roll back, or the TRUNCATE commits and the "
        "table is left empty"
    )


def test_a_partial_write_is_not_silent():
    """chunk 0 succeeds, the rest fail — the table is incomplete, not stale."""
    inserted, conn, cur = _run(1200, fail_from=1)
    assert 0 < inserted < 1200, f"expected a partial write, got {inserted}"
    assert conn.rollbacks >= 1


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
