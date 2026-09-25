"""Resolve-on-absence arms against a real Postgres (2026-09-13).

routes/brain_findings_resolve.py decides which open brain_findings rows a radar
sweep may close. tests/test_radar_resolve_scoped_to_completed_detectors.py pins
which statements run, with which parameters, against a fake cursor. Only Postgres
shows what they CLOSE: NULLIF/COALESCE over NULL and '' detectors, the `?` test on
the ledger's JSONB keys, the sibling subquery over the table being updated, and
make_interval over an adapted int.

THE ROWS. Every open row that must stay open fails exactly one clause:

  row            producer                        last seen  expected
  a_gone         check_a, clean now              2h         RESOLVED clean_absence
  legacy         check_a, detector NULL          2h         RESOLVED clean_absence
  a_seen_prev    check_a                         2h         open  the earlier run REPORTED it
  a_recent       check_a                         10m        open  no recorded run since
  a_live_now     check_a                         now        open  written this transaction
  f_old          check_f                         2h         open  its only run is THIS sweep
  b_old          check_b, degraded now           2h         open  not clean now
  c_old          check_c, self-reported crash    2h         open  not clean now
  d_old          check_d, earlier run crashed    2h         open  no earlier CLEAN run
  e_old          check_e, earlier run truncated  2h         open  a capped key list proves nothing
  stale_attr     check_d                         3d         open  the old 24h arm closed this
  fq_fn          fast_qa, detector_fn check_a    2h         open  a foreign row never takes a radar arm
  esc            check_a, escalated              2h         escalated
  orphan         check_removed                   8d         RESOLVED orphaned
  orphan_recent  check_removed2                  2d         open  not missing long enough
  abandoned      check_g, submitted this sweep   30d        open  a live detector that never finishes
  flaky          check_h, in the ledger 1d ago   10d        open  missing from THIS sweep only
  scan_partial   radar, detector_fn NULL         25h        RESOLVED unattributed
  anon           detector '', detector_fn ''     25h        RESOLVED unattributed
  inspector      radar, detector_fn ''           1h         open
  fq_old         fast_qa, fq_live written 1h ago 25h        RESOLVED foreign
  pw_old         paywall_test, nothing since     25h        open  its detector has gone quiet
  dm_old         publisher_deadman, dm_done 2h   30h        RESOLVED foreign
  done / dm_done already resolved                           resolved_at untouched

It also runs the radar's query-tracking connection on the real driver, which a
fake cursor cannot stand in for: psycopg2's cursors are C types, and a cursor
factory is checked against them when the cursor is made.

Set RESOLVE_ARMS_SQL_DSN to run it. CI's db-parity job passes the service DSN and
then asserts this file did not skip. Works in its own schema, inside a
transaction it rolls back.
"""
import json
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("RESOLVE_ARMS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="RESOLVE_ARMS_SQL_DSN not set — no Postgres to run against")

SCHEMA = "resolve_arms_sql"

ROWS = [
    # issue, url, detector, detector_fn, status, last seen this long ago
    ("a_gone", "/a", "consistency_radar", "check_a", "open", "2 hours"),
    ("legacy", "/l", None, "check_a", "open", "2 hours"),
    ("a_seen_prev", "/a", "consistency_radar", "check_a", "open", "2 hours"),
    ("a_recent", "/a", "consistency_radar", "check_a", "open", "10 minutes"),
    ("a_live_now", "/a", "consistency_radar", "check_a", "open", "0 seconds"),
    ("f_old", "/f", "consistency_radar", "check_f", "open", "2 hours"),
    ("b_old", "/b", "consistency_radar", "check_b", "open", "2 hours"),
    ("c_old", "/c", "consistency_radar", "check_c", "open", "2 hours"),
    ("d_old", "/d", "consistency_radar", "check_d", "open", "2 hours"),
    ("e_old", "/e", "consistency_radar", "check_e", "open", "2 hours"),
    ("stale_attr", "/s", "consistency_radar", "check_d", "open", "3 days"),
    ("fq_fn", "/x", "fast_qa", "check_a", "open", "2 hours"),
    ("esc", "/a", "consistency_radar", "check_a", "escalated", "2 hours"),
    ("orphan", "/o", "consistency_radar", "check_removed", "open", "8 days"),
    ("orphan_recent", "/o", "consistency_radar", "check_removed2", "open", "2 days"),
    ("abandoned", "/g", "consistency_radar", "check_g", "open", "30 days"),
    ("flaky", "/h", "consistency_radar", "check_h", "open", "10 days"),
    ("scan_partial", "/api", "consistency_radar", None, "open", "25 hours"),
    ("anon", "/n", "", "", "open", "25 hours"),
    ("inspector", "/i", "consistency_radar", "", "open", "1 hour"),
    ("fq_old", "/q", "fast_qa", None, "open", "25 hours"),
    ("fq_live", "/q2", "fast_qa", None, "open", "1 hour"),
    ("pw_old", "/p", "paywall_test", None, "open", "25 hours"),
    ("dm_old", "/m", "publisher_deadman", None, "open", "30 hours"),
    ("dm_done", "/m2", "publisher_deadman", None, "resolved", "2 hours"),
    ("done", "/z", "consistency_radar", "check_a", "resolved", "2 hours"),
]

RUNS = [
    # sweep_id, recorded this long ago, detector_fn, outcome, reported keys, truncated
    ("s_prev", "40 minutes", "check_a", "completed", ["a_seen_prev|/a"], False),
    ("s_prev", "40 minutes", "check_b", "completed", [], False),
    ("s_prev", "40 minutes", "check_c", "completed", [], False),
    ("s_prev", "40 minutes", "check_d", "crashed", [], False),
    ("s_prev", "40 minutes", "check_e", "completed", ["x|y"], True),
    ("s_now", "1 minute", "check_f", "completed", [], False),
    ("s_prev", "40 minutes", "check_g", "abandoned", [], False),
    ("s_day", "1 day", "check_h", "abandoned", [], False),
]

SWEEP = {
    "sweep_id": "s_now", "at": 0.0,
    "completed_fns": ["check_a", "check_b", "check_c", "check_d", "check_e", "check_f"],
    "degraded_fns": ["check_b"],
    "crashed_fns": [], "timeout_fns": [],
    "abandoned_fns": ["check_g"],
    "registered_fns": ["check_a", "check_b", "check_c", "check_d", "check_e", "check_f",
                       "check_g"],
}
FINDINGS = [
    {"issue": "a_live_now", "url": "/a", "_detector_fn": "check_a"},
    {"issue": "consistency_radar_detector_crashed:check_c", "url": "check_c",
     "_detector_fn": "check_c"},
]

RESOLVED = {"a_gone", "legacy", "orphan", "scan_partial", "anon", "fq_old", "dm_old"}
BY_ARM = {"clean_absence": 2, "orphaned": 1, "unattributed": 2, "foreign": 2}


@pytest.fixture
def cur():
    from routes.brain_consistency_radar import _BRAIN_FINDINGS_DDL
    from routes.brain_detector_ledger import LEDGER_DDL, LEDGER_INDEXES
    conn = psycopg2.connect(DSN)
    c = conn.cursor()
    try:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute(_BRAIN_FINDINGS_DDL)
        c.execute("ALTER TABLE brain_findings ADD COLUMN IF NOT EXISTS detector_fn TEXT")
        c.execute(LEDGER_DDL)
        for ddl in LEDGER_INDEXES:
            c.execute(ddl)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS brain_detector_runs_sweep_fn "
                  "ON brain_detector_runs (sweep_id, detector_fn)")
        for issue, url, detector, fn, status, ago in ROWS:
            c.execute("""
                INSERT INTO brain_findings
                       (issue, url, detector, detector_fn, status, last_seen, resolved_at)
                VALUES (%s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING - %s::interval,
                        CASE WHEN %s = 'resolved' THEN NOW() - INTERVAL '1 day' END)
                ON CONFLICT (issue, url) DO NOTHING
            """, (issue, url, detector, fn, status, ago, status))
        for sweep_id, ago, fn, outcome, keys, truncated in RUNS:
            c.execute("""
                INSERT INTO brain_detector_runs
                       (sweep_id, swept_at, detector_fn, outcome, reported,
                        reported_count, reported_truncated)
                VALUES (%s, NOW() ON CONFLICT DO NOTHING - %s::interval, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (sweep_id, detector_fn) DO NOTHING
            """, (sweep_id, ago, fn, outcome, json.dumps(keys), len(keys), truncated))
        yield c
    finally:
        conn.rollback()
        conn.close()


def _closed_now(cur):
    cur.execute("SELECT issue, status, resolved_at = NOW() FROM brain_findings")
    return {issue: (status, bool(just_now)) for issue, status, just_now in cur.fetchall()}


def test_each_arm_closes_exactly_what_its_evidence_covers(cur):
    from routes.brain_findings_resolve import resolve_absent
    assert resolve_absent(cur, dict(SWEEP), FINDINGS, fn_col_live=True) == BY_ARM
    rows = _closed_now(cur)
    assert {i for i, (_s, now) in rows.items() if now} == RESOLVED
    assert {rows[i][0] for i in RESOLVED} == {"resolved"}
    untouched = {i: s for i, (s, now) in rows.items() if not now}
    assert untouched.pop("esc") == "escalated"
    assert untouched.pop("done") == untouched.pop("dm_done") == "resolved"
    assert set(untouched.values()) == {"open"}, untouched


def test_without_the_ledger_table_no_radar_row_closes_on_absence(cur):
    from routes.brain_findings_resolve import resolve_absent
    cur.execute("DROP TABLE brain_detector_runs")
    assert resolve_absent(cur, dict(SWEEP), FINDINGS, fn_col_live=True) == {
        "unattributed": 2, "foreign": 2}
    rows = _closed_now(cur)
    assert not rows["a_gone"][1] and not rows["orphan"][1]


def test_an_arm_postgres_rejects_rolls_back_alone(cur, monkeypatch):
    from routes import brain_findings_resolve as res
    monkeypatch.setattr(res, "FOREIGN_SQL", "UPDATE brain_findings SET no_such_column = 1")
    assert res.resolve_absent(cur, dict(SWEEP), FINDINGS, fn_col_live=True) == {
        "clean_absence": 2, "orphaned": 1, "unattributed": 2}
    assert {i for i, (_s, now) in _closed_now(cur).items() if now} == RESOLVED - {
        "fq_old", "dm_old"}, "the transaction must survive the arm Postgres rejected"


# ── the query-tracking connection, on the real driver ──────────────────────

def _tracking_conn(r):
    return psycopg2.connect(DSN, **r._query_tracking_kwargs())


def test_the_tracking_connection_marks_a_raised_query_and_nothing_else():
    from psycopg2.extras import RealDictCursor
    from routes import brain_consistency_radar as r
    conn = _tracking_conn(r)
    try:
        conn.autocommit = True
        r._DB_UNAVAILABLE.hit = False
        with conn.cursor() as c:
            c.execute("SELECT 1")
            assert c.fetchone() == (1,)
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT 1 AS one")
            assert c.fetchone() == {"one": 1}, "the caller's cursor_factory must still apply"
        assert r._DB_UNAVAILABLE.hit is False, "a query that succeeded marked the run"
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            with pytest.raises(psycopg2.errors.UndefinedTable):
                c.execute("SELECT * FROM resolve_arms_sql_no_such_table")
        assert r._DB_UNAVAILABLE.hit is True
        r._DB_UNAVAILABLE.hit = False
        with conn.cursor() as c:
            with pytest.raises(psycopg2.Error):
                c.executemany("UPDATE resolve_arms_sql_no_such_table SET x = %s", [(1,)])
        assert r._DB_UNAVAILABLE.hit is True
    finally:
        conn.close()


def test_a_named_cursor_on_the_tracking_connection_still_streams():
    from routes import brain_consistency_radar as r
    conn = _tracking_conn(r)
    try:
        with conn.cursor("resolve_arms_named") as c:
            c.execute("SELECT generate_series(1, 3)")
            assert [row[0] for row in c.fetchall()] == [1, 2, 3]
            assert type(c).__name__.startswith("QueryTracking")
    finally:
        conn.rollback()
        conn.close()


def test_a_detector_that_swallows_a_real_query_error_is_recorded_degraded(monkeypatch):
    from routes import brain_consistency_radar as r
    monkeypatch.setattr(r, "_LAST_SWEEP", {"completed_fns": [], "abandoned_fns": [],
                                           "registered_fns": [], "at": 0.0})
    monkeypatch.setattr(r, "_SWEEP_OUTCOMES", {})

    def probe(sql):
        conn = _tracking_conn(r)
        conn.autocommit = True
        try:
            with conn.cursor() as c:
                c.execute(sql)
        except Exception:
            pass  # the shape most radar detectors use: swallow it, report nothing
        finally:
            conn.close()
        return []

    def check_swallows_a_query_error():
        return probe("SELECT * FROM resolve_arms_sql_no_such_table")

    def check_queries_cleanly():
        return probe("SELECT 1")

    r._run_detectors([check_swallows_a_query_error, check_queries_cleanly], budget_s=20.0)
    sweep = r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]
    assert sweep["degraded_fns"] == ["check_swallows_a_query_error"]
    assert sorted(sweep["completed_fns"]) == ["check_queries_cleanly",
                                              "check_swallows_a_query_error"]
