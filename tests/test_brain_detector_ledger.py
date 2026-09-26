#!/usr/bin/env python3
"""tests/test_brain_detector_ledger.py — the detector ledger and its verdicts.

NO NETWORK, NO DATABASE.

WHY THIS EXISTS (measured 2026-09-13): nothing durable recorded that a detector
ran. brain_findings bumps last_seen on every write and resolves any row quiet for
24h whether or not its detector ran, and one live radar sweep left 54 of 143
detectors unfinished while check_iso_metric_dropped crashed. "Stopped firing" is
only proven by a detector that COMPLETED, repeatedly, and did not report it.
"""
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
FN = "check_operator_profile_gap"
LEDGER = {"first_sweep": NOW - timedelta(days=20), "last_sweep": NOW - timedelta(hours=1),
          "sweeps": 900}


def _h(hours):
    return NOW - timedelta(hours=hours)


def _d(days):
    return NOW - timedelta(days=days)


@pytest.fixture
def m(monkeypatch):
    from routes import brain_detector_ledger as mod
    monkeypatch.setitem(mod.ABSENCE_PROVABLE, FN, "reviewed for this test")
    return mod


def _row(**kw):
    row = {"status": "resolved", "resolved_at": _d(8), "last_seen": _d(9),
           "detector": "consistency_radar", "detector_fn": FN}
    row.update(kw)
    return row


def _stats(**kw):
    s = {"last_reported": _d(9), "first_run": _d(20), "last_completed": _h(2),
         "completed_since": 40, "truncated_since": False}
    s.update(kw)
    return s


# ── the verdict ───────────────────────────────────────────────────────────

def test_a_reviewed_detector_that_kept_completing_without_it_proves_quiet(m):
    v = m.judge([_row()], _stats(), LEDGER, NOW)
    assert v["verdict"] == m.QUIET_PROVEN, v
    assert v["detector_fn"] == FN


@pytest.mark.parametrize("rows,stats,ledger,verdict,why", [
    ([], None, LEDGER, "unmeasured", "no brain_findings row"),
    ([_row(status="open", resolved_at=None, last_seen=_h(10))], None, LEDGER, "firing",
     "open row was written"),
    ([_row()], {"last_reported": _h(10)}, LEDGER, "firing", "reported it in a completed run"),
    ([_row(detector="site_sentinel")], None, LEDGER, "unmeasured", "ledger does not record"),
    ([_row(detector_fn="")], None, LEDGER, "unmeasured", "no detector_fn"),
    ([_row(), _row(detector_fn="check_other")], None, LEDGER, "unmeasured", "more than one"),
    ([_row()], None, {"first_sweep": None}, "unmeasured", "no sweeps yet"),
    ([_row()], {"first_run": None}, LEDGER, "unmeasured", "no runs in the detector ledger"),
    ([_row(status="wont_fix")], None, LEDGER, "unmeasured", "not evidence of a fix"),
    ([_row(status="open", resolved_at=None, last_seen=_h(60))], None, LEDGER, "quiet_unproven",
     "still open"),
    ([_row()], {"last_completed": _h(30)}, LEDGER, "quiet_unproven", "has not completed"),
    ([_row()], {"last_completed": None}, LEDGER, "quiet_unproven", "has not completed"),
    ([_row()], {"truncated_since": True}, LEDGER, "quiet_unproven", "reported-keys cap"),
    ([_row()], {"completed_since": 5}, LEDGER, "quiet_unproven", "proof needs 6"),
    ([_row()], {"last_reported": _d(6)}, LEDGER, "quiet_unproven", "proof needs 6"),
], ids=["no-row", "open-and-fresh", "reported-recently", "foreign-detector", "no-detector-fn",
        "two-detector-fns", "empty-ledger", "detector-never-ran", "wont-fix", "open-but-stale",
        "stopped-completing", "never-completed", "truncated", "too-few-runs", "too-few-days"])
def test_every_missing_proof_keeps_it_unproven(m, rows, stats, ledger, verdict, why):
    v = m.judge(rows, _stats(**(stats or {})), ledger, NOW)
    assert v["verdict"] == verdict and why in v["reason"], v


def test_quiet_is_measured_inside_the_ledger_not_before_it(m):
    """A finding last reported before the ledger existed has only been watched
    since the ledger's first sweep — three days is not seven."""
    young = {"first_sweep": _d(3), "last_sweep": _h(1), "sweeps": 100}
    v = m.judge([_row()], _stats(last_reported=None, first_run=_d(3)), young, NOW)
    assert v["verdict"] == m.QUIET_UNPROVEN and "3.0 day" in v["reason"], v


def test_an_unreviewed_detector_is_never_proven_quiet(m, monkeypatch):
    monkeypatch.delitem(m.ABSENCE_PROVABLE, FN)
    v = m.judge([_row()], _stats(), LEDGER, NOW)
    assert v["verdict"] == m.QUIET_UNPROVEN and "not reviewed" in v["reason"], v


# ── the rows a sweep writes ───────────────────────────────────────────────

def test_only_a_completed_detector_is_credited_with_what_it_reported(m):
    sweep = {"completed_fns": ["a"], "crashed_fns": ["b"], "abandoned_fns": ["c"],
             "timeout_fns": ["d"]}
    findings = [{"issue": "x", "url": "u", "_detector_fn": "a"},
                {"issue": "consistency_radar_detector_crashed:b", "url": "b"}]
    rows = {r["detector_fn"]: r for r in m.sweep_rows(sweep, findings)}
    assert {k: r["outcome"] for k, r in rows.items()} == {
        "a": "completed", "b": "crashed", "c": "abandoned", "d": "timeout"}
    assert rows["a"]["reported"] == ["x|u"]
    assert all(rows[k]["reported"] == [] for k in "bcd")


def test_a_detector_with_two_outcomes_takes_the_worse(m):
    rows = m.sweep_rows({"completed_fns": ["a"], "abandoned_fns": ["a"]},
                        [{"issue": "x", "url": "u", "_detector_fn": "a"}])
    assert [(r["outcome"], r["reported"]) for r in rows] == [("abandoned", [])]


def test_a_detector_that_reports_its_own_crash_did_not_complete(m):
    """check_cron_freshness and five others catch their own failure, return it
    as a finding and return normally — the runner counts that as completed."""
    fn = "check_cron_freshness"
    rows = m.sweep_rows({"completed_fns": [fn]},
                        [{"issue": f"consistency_radar_detector_crashed:{fn}", "url": fn,
                          "_detector_fn": fn}])
    assert [(r["outcome"], r["reported"]) for r in rows] == [("crashed", [])]


def test_a_detector_that_had_no_database_is_degraded_not_completed(m):
    rows = m.sweep_rows({"completed_fns": ["a"], "degraded_fns": ["a"]},
                        [{"issue": "x", "url": "u", "_detector_fn": "a"}])
    assert [(r["outcome"], r["reported"]) for r in rows] == [("degraded", [])]


def test_reported_keys_are_capped_and_the_cap_is_flagged(m, monkeypatch):
    monkeypatch.setattr(m, "MAX_REPORTED_KEYS", 2)
    rows = m.sweep_rows({"completed_fns": ["a"]},
                        [{"issue": f"x{i}", "url": "u", "_detector_fn": "a"} for i in range(3)])
    assert rows[0]["reported_count"] == 3 and len(rows[0]["reported"]) == 2
    assert rows[0]["reported_truncated"] is True


class FakeCur:
    def __init__(self, fetches=()):
        self.sql, self._fetches = [], list(fetches)

    def execute(self, q, params=None):
        self.sql.append((" ".join(str(q).split()), params))

    def fetchone(self):
        return self._fetches.pop(0) if self._fetches else None

    def inserts(self):
        return [(q, p) for q, p in self.sql if q.startswith("INSERT INTO brain_detector_runs")]


SWEEP = {"sweep_id": "s1", "at": 1_790_000_000.0, "completed_fns": ["a", "b"],
         "abandoned_fns": ["c"]}


def test_a_sweep_is_recorded_with_its_outcomes_and_old_rows_pruned(m):
    import json
    cur = FakeCur(fetches=[(True,), (False,)])
    res = m.record_sweep(cur, SWEEP, [{"issue": "x", "url": "u", "_detector_fn": "a"}])
    assert res == {"recorded": 3, "skipped": None}
    [(q, params)] = cur.inserts()
    assert q.endswith("ON CONFLICT (sweep_id, detector_fn) DO NOTHING"), q
    rows = json.loads(params[0])
    assert [(r["detector_fn"], r["outcome"], r["reported"]) for r in rows] == [
        ("a", "completed", ["x|u"]), ("b", "completed", []), ("c", "abandoned", [])]
    assert {r["sweep_id"] for r in rows} == {"s1"}
    assert {r["swept_at"] for r in rows} == {
        datetime.fromtimestamp(SWEEP["at"], tz=timezone.utc).isoformat()}
    assert any("CREATE UNIQUE INDEX IF NOT EXISTS brain_detector_runs_sweep_fn" in q
               for q, _ in cur.sql), "ON CONFLICT needs its unique index"
    assert any(q.startswith("DELETE FROM brain_detector_runs") for q, _ in cur.sql)


@pytest.mark.parametrize("fetches,sweep,env,skipped", [
    ([(False,)], SWEEP, None, "another writer"),
    ([(True,), (True,)], SWEEP, None, "throttled"),
    ([], {**SWEEP, "sweep_id": ""}, None, "no sweep identity"),
    ([], SWEEP, "1", "disabled"),
], ids=["lock-held", "recent-sweep", "no-identity", "kill-switch"])
def test_nothing_is_written_when_a_sweep_should_not_be_recorded(m, monkeypatch, fetches, sweep,
                                                               env, skipped):
    if env:
        monkeypatch.setenv("BRAIN_DETECTOR_LEDGER_DISABLE", env)
    cur = FakeCur(fetches=fetches)
    res = m.record_sweep(cur, sweep, [])
    assert res["recorded"] == 0 and skipped in res["skipped"], res
    assert not cur.inserts()


# ── the evidence read ─────────────────────────────────────────────────────

class ScriptCur:
    """Answers the evidence queries from fixed data, keyed on the SQL."""
    def __init__(self, *, ledger_exists=True, bounds=None, cols=("detector_fn",),
                 exact=(), prefix=(), stats=None):
        self.ledger_exists, self.bounds, self.cols = ledger_exists, bounds, cols
        self.exact, self.prefix, self.stats = list(exact), list(prefix), stats
        self.sql = []

    def execute(self, q, params=None):
        self.last = " ".join(str(q).split())
        self.sql.append((self.last, params))

    def fetchone(self):
        if "to_regclass" in self.last:
            return (self.ledger_exists,)
        if "COUNT(DISTINCT sweep_id)" in self.last:
            return self.bounds or (LEDGER["first_sweep"], LEDGER["last_sweep"], 900)
        if self.last.startswith("WITH d AS"):
            return self.stats
        return None

    def fetchall(self):
        if "information_schema.columns" in self.last:
            return [(c,) for c in self.cols]
        if "LIKE" in self.last:
            return list(self.prefix)
        if "FROM brain_findings" in self.last:
            return list(self.exact)
        return []


STATS_ROW = (_d(9), _d(20), _h(2), 40, False)


def test_an_exact_match_is_judged_on_its_own_detector_and_key(m):
    cur = ScriptCur(exact=[("resolved", _d(8), _d(9), "consistency_radar", FN, "/operators/x")],
                    stats=STATS_ROW)
    out = m.evidence_for(cur, [{"issue": "operator_profile_gap:X", "url": "/operators/x"}], now=NOW)
    [f] = out["findings"]
    assert out["state"] == "MEASURED" and f["match"] == "exact" and f["verdict"] == m.QUIET_PROVEN
    [(_, params)] = [(q, p) for q, p in cur.sql if q.startswith("WITH d AS")]
    assert params == {"fn": FN, "keys": ["operator_profile_gap:X|/operators/x"]}
    assert f["evidence"]["last_completed"] == _h(2).isoformat()


def test_a_cut_url_matches_only_a_unique_prefix(m):
    one = ScriptCur(prefix=[("resolved", _d(8), _d(9), "consistency_radar", FN, "/operators/equinix-inc")],
                    stats=STATS_ROW)
    [f] = m.evidence_for(one, [{"issue": "i", "url": "/operators/equin", "url_prefix": True}],
                         now=NOW)["findings"]
    assert f["match"] == "prefix"
    assert [p for q, p in one.sql if q.startswith("WITH d AS")][0]["keys"] == ["i|/operators/equinix-inc"]
    two = ScriptCur(prefix=[("resolved", _d(8), _d(9), "consistency_radar", FN, "/operators/equinix-inc"),
                            ("resolved", _d(8), _d(9), "consistency_radar", FN, "/operators/equinix")])
    [f] = m.evidence_for(two, [{"issue": "i", "url": "/operators/equin", "url_prefix": True}],
                         now=NOW)["findings"]
    assert f["match"] == "ambiguous" and f["verdict"] == m.UNMEASURED


def test_without_the_ledger_table_nothing_is_proven(m):
    cur = ScriptCur(ledger_exists=False,
                    exact=[("resolved", _d(8), _d(9), "consistency_radar", FN, "u")])
    [f] = m.evidence_for(cur, [{"issue": "i", "url": "u"}], now=NOW)["findings"]
    assert f["verdict"] == m.UNMEASURED and "no sweeps yet" in f["reason"]
    assert not [q for q, _ in cur.sql if q.startswith("WITH d AS")]


def test_without_the_detector_fn_column_nothing_is_proven(m):
    cur = ScriptCur(cols=("issue", "url"), exact=[("resolved", _d(8), _d(9), "consistency_radar", None, "u")],
                    stats=STATS_ROW)
    [f] = m.evidence_for(cur, [{"issue": "i", "url": "u"}], now=NOW)["findings"]
    assert f["verdict"] == m.UNMEASURED and "no detector_fn" in f["reason"]
    assert any("NULL" in q for q, _ in cur.sql if "FROM brain_findings" in q)
