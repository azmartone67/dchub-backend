"""routes/brain_evolution_scorecard.py — the verdict is pure; the SQL runs
against a disposable Postgres when DCHUB_PG_TEST_DSN is set."""
import os

import pytest

from routes import brain_evolution_scorecard as sc

DSN = os.environ.get("DCHUB_PG_TEST_DSN", "")


def _snap(week, fix=None, recur=None, rej="dead", spec=0):
    return {"week_start": week, "metrics": {
        "fix_success": {"pct_30d": fix},
        "recurring_share": {"pct_7d": recur},
        "spec_to_code": {"spec_prs_opened_30d": spec},
        "negative_signal": {"state": rej}}}


# ── rate floors ──────────────────────────────────────────────────────
def test_rate_is_none_under_the_floor_or_unreadable():
    """MUTATION: drop the floor → 1 of 2 reads as a 50% fix rate."""
    assert sc.rate_pct(1, 2, 20) is None
    assert sc.rate_pct(None, 50, 20) is None and sc.rate_pct(5, None, 20) is None
    assert sc.rate_pct(65, 199, 20) == 32.7          # the 2026-09-24 baseline


# ── verdict ──────────────────────────────────────────────────────────
def test_one_snapshot_claims_no_direction():
    v = sc.evolution_verdict([_snap("2026-09-21", 32.7)])
    assert v["status"] is None and "two at least 7 days apart" in v["reason"]


def test_improving_fixes_without_more_recurrence_is_evolving():
    v = sc.evolution_verdict([_snap("2026-09-21", 32.7, 40.0),
                              _snap("2026-10-12", 45.0, 39.0)])
    assert v["status"] == "evolving"
    assert v["compared"] == {"from": "2026-09-21", "to": "2026-10-12"}
    assert v["signals"]["fix_success_delta_pp"] == 12.3


def test_recurrence_vetoes_evolving():
    """MUTATION: ignore recurring_direction → fixes that 'hold' while the same
    problems come back more often would read as learning."""
    v = sc.evolution_verdict([_snap("2026-09-21", 32.7, 40.0),
                              _snap("2026-09-28", 45.0, 50.0)])
    assert v["status"] == "not_yet" and "recurring" in v["reason"]


def test_declining_fixes_is_regressing_and_flat_is_not_yet():
    assert sc.evolution_verdict([_snap("2026-09-21", 40.0),
                                 _snap("2026-09-28", 30.0)])["status"] == "regressing"
    v = sc.evolution_verdict([_snap("2026-09-21", 40.0),
                              _snap("2026-09-28", 42.0)])
    assert v["status"] == "not_yet" and v["reason"] == "fix_success flat"


def test_window_uses_the_oldest_snapshot_inside_four_weeks():
    """A snapshot 5 weeks back is outside the month window; one 6 days back
    is too close. MUTATION: compare against snaps[0] → uses the 5-week one."""
    v = sc.evolution_verdict([_snap("2026-08-17", 10.0),
                              _snap("2026-09-07", 30.0),
                              _snap("2026-09-28", 31.0),
                              _snap("2026-10-05", 32.0)])
    assert v["compared"]["from"] == "2026-09-07"
    assert v["status"] == "not_yet"


def test_unmeasured_end_is_not_a_verdict():
    v = sc.evolution_verdict([_snap("2026-09-21", None), _snap("2026-09-28", 50.0)])
    assert v["status"] is None and "unmeasured" in v["reason"]


def test_week_start_is_monday():
    from datetime import date
    assert sc.week_start(date(2026, 9, 24)) == date(2026, 9, 21)
    assert sc.week_start(date(2026, 9, 21)) == date(2026, 9, 21)


# ── real Postgres ────────────────────────────────────────────────────
_TABLES = ("brain_evolution_scorecard", "brain_fix_outcomes",
           "brain_issue_persistence", "loop_closure_spec_attempts",
           "squasher_work_queue", "brain_review_decisions", "brain_lessons")


@pytest.fixture
def pg(monkeypatch):
    if not DSN:
        pytest.skip("set DCHUB_PG_TEST_DSN to a disposable Postgres")
    if any(h in DSN for h in ("neon.tech", "railway", "rlwy", "amazonaws")):
        pytest.fail("DCHUB_PG_TEST_DSN looks managed — point it at a throwaway")
    import psycopg2
    # Production's raw connector is AUTOCOMMIT (routes/ai_reach.py); the
    # module's own _conn must undo that or every SAVEPOINT fails.
    from routes import ai_reach

    def _autocommit():
        c = psycopg2.connect(DSN)
        c.autocommit = True
        return c
    monkeypatch.setattr(ai_reach, "_conn", _autocommit)
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        for t in _TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    yield c
    with c.cursor() as cur:
        for t in _TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    c.close()


def test_missing_sources_are_unmeasured_not_zero_on_postgres(pg):
    """MUTATION: default an unreadable source to 0 → a brand-new database
    reports 'no rejections, no recurrence' as measured facts."""
    out = sc.snapshot()
    assert out["ok"], out
    m = out["metrics"]
    assert m["fix_success"]["graded"] is None and m["fix_success"]["pct_30d"] is None
    assert m["recurring_share"]["active_7d"] is None
    assert m["spec_to_code"]["spec_prs_opened_30d"] is None
    assert m["negative_signal"]["state"] is None


def test_snapshot_measures_and_upserts_one_row_per_week_on_postgres(pg):
    with pg.cursor() as cur:
        cur.execute("CREATE TABLE brain_fix_outcomes (id BIGSERIAL PRIMARY KEY,"
                    " checked_at TIMESTAMPTZ, still_broken BOOLEAN)")
        cur.execute("INSERT INTO brain_fix_outcomes (checked_at, still_broken)"
                    " SELECT NOW(), g > 13 FROM generate_series(1, 40) g")
        cur.execute("INSERT INTO brain_fix_outcomes (checked_at, still_broken)"
                    " VALUES (NOW(), NULL), (NOW() - INTERVAL '40 days', FALSE)")
        cur.execute("CREATE TABLE brain_issue_persistence (id BIGSERIAL PRIMARY"
                    " KEY, first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ)")
        cur.execute("INSERT INTO brain_issue_persistence (first_seen_at,"
                    " last_seen_at) SELECT CASE WHEN g <= 15 THEN NOW() -"
                    " INTERVAL '60 days' ELSE NOW() END, NOW()"
                    " FROM generate_series(1, 25) g")
        cur.execute("INSERT INTO brain_issue_persistence (first_seen_at,"
                    " last_seen_at) VALUES (NOW() ON CONFLICT DO NOTHING - INTERVAL '90 days',"
                    " NOW() - INTERVAL '20 days')")   # not active this week
        cur.execute("CREATE TABLE brain_review_decisions (id BIGSERIAL PRIMARY"
                    " KEY, decision TEXT, decided_at TIMESTAMPTZ DEFAULT NOW())")
        cur.execute("INSERT INTO brain_review_decisions (decision) VALUES"
                    " ('approve'), ('reject')")
    out = sc.snapshot()
    m = out["metrics"]
    assert (m["fix_success"]["held"], m["fix_success"]["graded"]) == (13, 40)
    assert m["fix_success"]["pct_30d"] == 32.5
    assert (m["recurring_share"]["chronic"], m["recurring_share"]["active_7d"]) == (15, 25)
    assert m["recurring_share"]["pct_7d"] == 60.0
    assert m["negative_signal"]["state"] == "live"
    sc.snapshot()                                     # same week → upsert
    r = sc.read_scorecard()
    assert r["ok"] and len(r["history"]) == 1
    assert r["verdict"]["status"] is None
