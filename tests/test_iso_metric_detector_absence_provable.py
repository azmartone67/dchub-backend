"""check_iso_metric_dropped is absence-provable, and its two issues are one finding.

NO NETWORK, NO DATABASE (the SQL itself is exercised against Postgres in the PR
evidence; these pin the decisions).

2026-09-26: 38 spec-debt findings from this detector sat quiet_unproven for
weeks only because the detector was not reviewed. The 09-13 review named two
objections; each is pinned here:
  · it reports iso_metric_count_zero_24h OR iso_metric_count_dropped for one
    url, so one key going quiet can mean the other took over;
  · a path returned [] without recording the run as degraded.
"""
from datetime import datetime, timedelta, timezone
import types

import pytest

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
FN = "check_iso_metric_dropped"
URL = "grid_data: iso=LGEE"
ZERO, DROP = "iso_metric_count_zero_24h", "iso_metric_count_dropped"


def _d(days):
    return NOW - timedelta(days=days)


class Cur:
    """Scripted cursor: brain_findings rows by issue, and the stats row."""

    def __init__(self, rows_by_issue, stats):
        self.rows_by_issue, self.stats, self.sql = rows_by_issue, stats, []

    def execute(self, q, params=None):
        self.last, self.params = " ".join(str(q).split()), params
        self.sql.append((self.last, params))

    def fetchone(self):
        if "to_regclass" in self.last:
            return (True,)
        if "COUNT(DISTINCT sweep_id)" in self.last:
            return (_d(13), NOW - timedelta(hours=1), 446)
        if self.last.startswith("WITH d AS"):
            return self.stats
        return None

    def fetchall(self):
        if "information_schema.columns" in self.last:
            return [("detector_fn",)]
        if "FROM brain_findings" in self.last:
            wanted = self.params[0]
            wanted = wanted if isinstance(wanted, list) else [wanted]
            return [r for i in wanted for r in self.rows_by_issue.get(i, [])]
        return []


def _resolved():
    return ("resolved", _d(9), _d(10), "consistency_radar", FN, URL)


QUIET_STATS = (_d(10), _d(13), NOW - timedelta(hours=2), 400, False)


@pytest.fixture
def m():
    from routes import brain_detector_ledger as mod
    return mod


def test_the_detector_is_reviewed_absence_provable(m):
    assert FN in m.ABSENCE_PROVABLE and "reviewed 2026-09-26" in m.ABSENCE_PROVABLE[FN]


def test_a_quiet_iso_finding_is_now_proven(m):
    cur = Cur({ZERO: [_resolved()]}, QUIET_STATS)
    [f] = m.evidence_for(cur, [{"issue": ZERO, "url": URL}], now=NOW)["findings"]
    assert f["verdict"] == m.QUIET_PROVEN, f


def test_the_stats_read_asks_for_BOTH_keys(m):
    cur = Cur({ZERO: [_resolved()]}, QUIET_STATS)
    m.evidence_for(cur, [{"issue": ZERO, "url": URL}], now=NOW)
    [params] = [p for q, p in cur.sql if q.startswith("WITH d AS")]
    assert params["keys"] == [f"{ZERO}|{URL}", f"{DROP}|{URL}"]
    [sql] = [q for q, _ in cur.sql if q.startswith("WITH d AS")]
    assert "?| %(keys)s" in sql, "last_reported must match ANY of the keys"


def test_an_open_sibling_row_blocks_the_proof(m):
    """zero_24h went quiet because the region now writes 1 metric: dropped fires."""
    open_drop = ("open", None, NOW - timedelta(hours=3), "consistency_radar", FN, URL)
    cur = Cur({ZERO: [_resolved()], DROP: [open_drop]}, QUIET_STATS)
    [f] = m.evidence_for(cur, [{"issue": ZERO, "url": URL}], now=NOW)["findings"]
    assert f["verdict"] == m.FIRING, f


def test_CONTROL_without_the_sibling_the_same_row_is_proven(m):
    """The sibling rule is what flips the verdict above, not something else."""
    cur = Cur({ZERO: [_resolved()], DROP: []}, QUIET_STATS)
    [f] = m.evidence_for(cur, [{"issue": ZERO, "url": URL}], now=NOW)["findings"]
    assert f["verdict"] == m.QUIET_PROVEN, f


def test_a_sibling_alone_is_not_evidence_for_a_finding_with_no_rows(m):
    cur = Cur({DROP: [_resolved()]}, QUIET_STATS)
    [f] = m.evidence_for(cur, [{"issue": ZERO, "url": URL}], now=NOW)["findings"]
    assert f["verdict"] == m.UNMEASURED and "no brain_findings row" in f["reason"]


def test_a_missing_grid_data_table_records_the_run_degraded(monkeypatch):
    from routes import brain_consistency_radar as r

    class C:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, q, p=None):
            pass
        def fetchone(self):
            return (None,)
        def close(self):
            pass

    monkeypatch.setattr(r, "_db", lambda: C())
    r._DB_UNAVAILABLE.hit = False
    assert r.check_iso_metric_dropped() == []
    assert r._DB_UNAVAILABLE.hit is True, "an empty result from no table must not read as healthy"
