"""The covering-index build must prove it landed, not that an object appeared.

CREATE INDEX CONCURRENTLY can fail and leave the index present but INVALID: it
has a name, `pg_indexes` lists it, and the planner never uses it. Two scripts
already in this repo — `add_performance_indexes.py` and
`tools/add_brain_rag_hnsw_index.py` — verify through `pg_indexes`, whose view
definition carries no `indisvalid`, so a failed build reads to them exactly like
a successful one. Both also use CREATE INDEX CONCURRENTLY **IF NOT EXISTS**,
which SKIPS a leftover invalid index, so every later re-run reports success too
and the index stays dead permanently.

These tests drive the real script against a fake driver, so they fail when the
verification is weakened rather than merely when the wording changes.

House rule: no test here imports main.
"""
from __future__ import annotations

import importlib.util
import os
import re

import psycopg2  # the real driver: patched per-test, never stubbed into
                 # sys.modules, so a missing psycopg2 fails instead of passing
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "add_surface_telemetry_covering_index.py")


def _mod():
    spec = importlib.util.spec_from_file_location("_sti_build", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeCursor:
    """Answers the script's queries; records every statement it is given."""

    def __init__(self, index_exists, index_valid, plan_after_build):
        self.executed = []
        self._index_exists = index_exists
        self._index_valid = index_valid
        self._plan_after = plan_after_build
        self._built = False
        self._one = None
        self._all = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))
        s = " ".join(sql.split()).upper()
        if s.startswith("CREATE INDEX"):
            self._built = True
        elif s.startswith("DROP INDEX"):
            self._index_exists = False
        elif "TO_REGCLASS" in s:
            self._one = ("public.surface_telemetry",)
        elif s.startswith("SELECT COUNT(*) FROM SURFACE_TELEMETRY"):
            self._one = (15_784_504,)
        elif "PG_TOTAL_RELATION_SIZE" in s:
            self._one = ("4682 MB",)
        elif "PG_RELATION_SIZE" in s:
            self._one = ("1300 MB",)
        elif "FROM PG_INDEX" in s:
            valid = self._index_valid or self._built
            self._one = (valid, valid) if (self._index_exists or self._built) else None
        elif s.startswith("SELECT SURFACE_ID FROM"):
            self._one = ("auto_homepage",)
        elif s.startswith("EXPLAIN"):
            plan = self._plan_after if self._built else "Aggregate\n  -> Parallel Seq Scan on surface_telemetry"
            self._all = [(line,) for line in plan.splitlines()]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.autocommit = False

    def cursor(self):
        return self._cur


@pytest.fixture
def run(monkeypatch):
    """Run the script with a fake driver. Returns (exit_code, cursor)."""
    def _run(argv, index_exists=False, index_valid=False,
             plan_after_build="Aggregate\n  -> Parallel Index Only Scan using ix_surface_telemetry_surface_ts_incl_anon"):
        cur = FakeCursor(index_exists, index_valid, plan_after_build)
        monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
        monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: FakeConn(cur))
        return _mod().main(argv), cur
    return _run


def _ddl(cur):
    return [s for s in cur.executed if s.upper().startswith("CREATE INDEX")]


# ── the dry run must be inert ──────────────────────────────────────────────

def test_dry_run_changes_nothing(run):
    code, cur = run(["prog"])
    assert code == 0
    assert not _ddl(cur), f"a dry run issued DDL: {_ddl(cur)}"
    assert not [s for s in cur.executed if s.upper().startswith("DROP INDEX")]


def test_apply_actually_builds(run):
    code, cur = run(["prog", "--apply"])
    assert code == 0, "a clean build did not report success"
    assert _ddl(cur), "--apply issued no CREATE INDEX"


# ── the build itself ───────────────────────────────────────────────────────

def test_the_build_is_concurrent(run):
    _, cur = run(["prog", "--apply"])
    assert "CONCURRENTLY" in _ddl(cur)[0].upper(), (
        "a non-concurrent CREATE INDEX takes an exclusive lock on a 4.7 GB "
        "table that is written ~258,000 times a day")


def test_the_build_does_not_use_if_not_exists(run):
    """IF NOT EXISTS silently skips a leftover INVALID index, which turns one
    failed build into permanent, self-reporting success."""
    _, cur = run(["prog", "--apply"])
    assert "IF NOT EXISTS" not in _ddl(cur)[0].upper(), (
        "IF NOT EXISTS would skip an invalid leftover instead of rebuilding it")


def test_the_index_actually_covers_anon_id(run):
    """Without INCLUDE (anon_id) this index is the one that already exists, and
    the query it is meant to fix still cannot do an index-only scan."""
    _, cur = run(["prog", "--apply"])
    d = _ddl(cur)[0].upper()
    assert "INCLUDE (ANON_ID)" in d, (
        "the new index does not carry anon_id — it is a duplicate of "
        "ix_surface_telemetry_surface_ts and fixes nothing")
    assert "SURFACE_ID" in d and "TS DESC" in d


# ── the verification ───────────────────────────────────────────────────────

def test_verification_reads_pg_index_not_pg_indexes(run):
    _, cur = run(["prog", "--apply"])
    # "pg_index" is a substring of "pg_indexes", so match the catalog table
    # precisely — otherwise a query against the VIEW satisfies the check for
    # the TABLE, and the assertion below passes on the thing it forbids.
    checks = [s for s in cur.executed
              if re.search(r"\bpg_index\b", s, re.I)]
    assert checks, "the script never asked the catalog whether the index is valid"
    assert any("indisvalid" in s.lower() for s in checks), (
        "no check of indisvalid — pg_indexes reports a failed CONCURRENTLY "
        "build exactly like a successful one")
    assert not [s for s in cur.executed if "pg_indexes" in s.lower()], (
        "verification went through pg_indexes, which has no validity column")


def test_an_invalid_result_is_a_failure_not_a_success(run):
    """The build 'succeeds' but the catalog says the index is unusable."""
    cur_holder = {}

    class NeverValid(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "FROM PG_INDEX" in " ".join(sql.split()).upper():
                self._one = (False, True)      # exists, NOT valid

    import psycopg2 as _pg
    mp = pytest.MonkeyPatch()
    try:
        c = NeverValid(False, False, "Index Only Scan")
        cur_holder["c"] = c
        mp.setenv("DATABASE_URL", "postgres://fake/db")
        mp.setattr(_pg, "connect", lambda *a, **k: FakeConn(c))
        code = _mod().main(["prog", "--apply"])
    finally:
        mp.undo()
    assert code == 1, (
        "an index left INVALID by the build was reported as success — that is "
        "the exact failure this script exists to make impossible")


def test_a_still_seq_scanning_plan_is_a_failure(run):
    """An index that exists, is valid, and that the planner ignores has fixed
    nothing. The outcome is the plan, not the catalog row."""
    code, cur = run(["prog", "--apply"],
                    plan_after_build="Aggregate\n  -> Parallel Seq Scan on surface_telemetry")
    assert code == 1, (
        "the planner still chose a sequential scan and the script reported "
        "success — it verified the artifact instead of the outcome")


def test_a_leftover_invalid_index_is_dropped_before_rebuilding(run):
    code, cur = run(["prog", "--apply"], index_exists=True, index_valid=False)
    drops = [s for s in cur.executed if s.upper().startswith("DROP INDEX")]
    assert drops, (
        "an existing INVALID index was not dropped; CREATE would then skip it "
        "and every future run would report success over a dead index")
    assert _ddl(cur), "dropped the invalid index but never rebuilt"
    assert cur.executed.index(drops[0]) < cur.executed.index(_ddl(cur)[0]), (
        "rebuilt before dropping")


def test_an_existing_valid_index_is_left_alone(run):
    code, cur = run(["prog", "--apply"], index_exists=True, index_valid=True)
    assert code == 0
    assert not _ddl(cur), "rebuilt an index that was already valid"


# ── blast radius ───────────────────────────────────────────────────────────

def test_the_superseded_index_is_never_dropped(run):
    """It has 6.4M recorded scans. Dropping it in the same operation that adds
    its replacement leaves no way to attribute a regression."""
    for argv in (["prog"], ["prog", "--apply"]):
        _, cur = run(argv)
        for s in cur.executed:
            assert "ix_surface_telemetry_surface_ts" not in s or "incl_anon" in s, (
                f"the live index was touched: {s}")


def test_it_never_borrows_a_pooled_connection():
    """db_utils' wrapper drops DDL outright, and CONCURRENTLY additionally
    cannot run inside a transaction."""
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()
    for pooled in ("get_db(", "get_bg_db(", "get_read_db(", "safe_write("):
        assert pooled not in src, f"{pooled} cannot execute this DDL"
    assert "autocommit = True" in src, (
        "CREATE INDEX CONCURRENTLY cannot run inside a transaction block")
