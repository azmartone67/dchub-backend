"""check_facility_duplicate_clusters is absence-provable only from the day its
silent paths were fixed.

NO NETWORK, NO DATABASE.

2026-09-26 review: facility_dedup._plan() reads through its own connection,
which the radar's query-tracking cursor never sees, and returns None when it
cannot connect. The detector skipped that market and the run read "completed"
— an unreadable market looked like a clean one. Runs recorded before the fix
cannot be told apart, so REVIEWED_SINCE floors the evidence.
"""
from datetime import datetime, timedelta, timezone
import sys
import types

import pytest

FN = "check_facility_duplicate_clusters"


@pytest.fixture
def radar(monkeypatch):
    from routes import brain_consistency_radar as r

    class Conn:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, q, p=None):
            pass
        def fetchone(self):
            return (0,)
        def close(self):
            pass

    monkeypatch.setattr(r, "_db", lambda: Conn())
    return r


def _fd(monkeypatch, plan):
    fd = types.ModuleType("routes.facility_dedup")
    fd._plan = plan
    monkeypatch.setitem(sys.modules, "routes.facility_dedup", fd)
    import routes
    monkeypatch.setattr(routes, "facility_dedup", fd, raising=False)


def test_an_unreadable_market_marks_the_run_degraded(radar, monkeypatch):
    _fd(monkeypatch, lambda code: None)
    radar._DB_UNAVAILABLE.hit = False
    assert radar.check_facility_duplicate_clusters() == []
    assert radar._DB_UNAVAILABLE.hit is True


def test_CONTROL_a_readable_clean_market_is_not_degraded(radar, monkeypatch):
    _fd(monkeypatch, lambda code: {"plan": []})
    radar._DB_UNAVAILABLE.hit = False
    assert radar.check_facility_duplicate_clusters() == []
    assert radar._DB_UNAVAILABLE.hit is False


def test_a_failed_import_marks_the_run_degraded(radar, monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.facility_dedup", None)
    monkeypatch.setitem(sys.modules, "facility_dedup", None)
    import routes
    monkeypatch.delattr(routes, "facility_dedup", raising=False)
    radar._DB_UNAVAILABLE.hit = False
    assert radar.check_facility_duplicate_clusters() == []
    assert radar._DB_UNAVAILABLE.hit is True


# ── the ledger side ────────────────────────────────────────────────────────

@pytest.fixture
def m():
    from routes import brain_detector_ledger as mod
    return mod


def _row(now):
    return {"status": "resolved", "resolved_at": now - timedelta(days=20),
            "last_seen": now - timedelta(days=21), "detector": "consistency_radar",
            "detector_fn": FN}


def _stats(now, completed):
    return {"last_reported": now - timedelta(days=21), "first_run": now - timedelta(days=13),
            "last_completed": now - timedelta(hours=1), "completed_since": completed,
            "truncated_since": False}


LEDGER_OLD = {"first_sweep": datetime(2026, 9, 13, tzinfo=timezone.utc)}


def test_the_detector_is_reviewed_with_a_floor(m):
    assert FN in m.ABSENCE_PROVABLE and FN in m.REVIEWED_SINCE


def test_quiet_before_the_floor_is_not_proof(m):
    since = datetime.fromisoformat(m.REVIEWED_SINCE[FN])
    now = since + timedelta(days=3)
    v = m.judge([_row(now)], _stats(now, 150), LEDGER_OLD, now)
    assert v["verdict"] == m.QUIET_UNPROVEN and "over 3.0 day(s)" in v["reason"], v


def test_quiet_a_week_after_the_floor_is_proof(m):
    since = datetime.fromisoformat(m.REVIEWED_SINCE[FN])
    now = since + timedelta(days=7, hours=1)
    v = m.judge([_row(now)], _stats(now, 300), LEDGER_OLD, now)
    assert v["verdict"] == m.QUIET_PROVEN, v


def test_the_stats_read_carries_the_floor(m):
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)

    class Cur:
        def __init__(self):
            self.sql = []
        def execute(self, q, p=None):
            self.last = " ".join(str(q).split())
            self.sql.append((self.last, p))
        def fetchone(self):
            if "to_regclass" in self.last:
                return (True,)
            if "COUNT(DISTINCT sweep_id)" in self.last:
                return (LEDGER_OLD["first_sweep"], now, 900)
            if self.last.startswith("WITH d AS"):
                return (now - timedelta(days=21), LEDGER_OLD["first_sweep"],
                        now - timedelta(hours=1), 300, False)
        def fetchall(self):
            if "information_schema" in self.last:
                return [("detector_fn",)]
            if "FROM brain_findings" in self.last:
                return [("resolved", now - timedelta(days=20), now - timedelta(days=21),
                         "consistency_radar", FN, "/api/v1/admin/facility-dedup/analyze?country=CA")]
            return []

    cur = Cur()
    m.evidence_for(cur, [{"issue": "facility_duplicates_unmarked",
                          "url": "/api/v1/admin/facility-dedup/analyze?country=CA"}], now=now)
    [(sql, p)] = [(q, p) for q, p in cur.sql if q.startswith("WITH d AS")]
    assert p["since"] == m.REVIEWED_SINCE[FN]
    assert sql.count("%(since)s") == 2, "both completed_since and truncated_since honour the floor"
