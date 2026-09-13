#!/usr/bin/env python3
"""tests/test_radar_detector_ledger_hook.py — the radar records every detector's
outcome, and a full sweep reaches the detector ledger.

NO NETWORK, NO DATABASE. The real _run_detectors runs tiny fake detectors; the
real _persist_findings_to_db runs against a fake connection, with the findings
writer and the ledger writer stubbed.

WHY: the set of detectors that completed a sweep lived only in process memory
(_LAST_SWEEP), which any scan_all() call overwrites. Without a durable record,
"a detector ran and did not report X" is unknowable — and it is the only
evidence that X stopped firing.
"""
import time

import pytest


@pytest.fixture
def r(monkeypatch):
    from routes import brain_consistency_radar as mod
    monkeypatch.setattr(mod, "_LAST_SWEEP", {"completed_fns": [], "abandoned_fns": [], "at": 0.0})
    monkeypatch.setattr(mod, "_SWEEP_OUTCOMES", {})
    return mod


# ── _run_detectors ────────────────────────────────────────────────────────

def check_ok():
    return [{"issue": "operator_profile_gap:Equinix", "url": "/operators/equinix"}]


def check_boom():
    raise ValueError("statement timeout")


def check_slow():
    time.sleep(3)
    return [{"issue": "late", "url": "u"}]


def test_every_outcome_and_the_sweep_identity_are_recorded(r):
    out = r._run_detectors([check_ok, check_boom, check_slow], budget_s=1.0)
    sid = r._LAST_SWEEP["sweep_id"]
    outcomes = r._SWEEP_OUTCOMES[sid]
    assert outcomes["completed_fns"] == ["check_ok"]
    assert outcomes["crashed_fns"] == ["check_boom"]
    assert "check_slow" in outcomes["abandoned_fns"]
    assert sid and all(f.get("_sweep_id") == sid for f in out), out
    [reported] = [f for f in out if f["issue"].startswith("operator_profile_gap")]
    assert reported["_detector_fn"] == "check_ok"


def test_a_detector_that_got_no_database_is_recorded_as_degraded(r, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def check_needs_db():
        return [] if r._db() is None else [{"issue": "x", "url": "u"}]

    r._run_detectors([check_needs_db, check_ok], budget_s=5.0)
    outcomes = r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]
    assert outcomes["degraded_fns"] == ["check_needs_db"]
    # The resolve-on-absence arm still reads completed_fns exactly as before.
    assert sorted(outcomes["completed_fns"]) == ["check_needs_db", "check_ok"]


def test_the_no_database_marker_does_not_leak_into_the_next_detector(r, monkeypatch):
    """Detectors share worker threads, so a marker left set by one detector
    would brand the next one on that thread. One worker makes the reuse
    certain — with fresh threads per sweep a missing reset would never show."""
    import concurrent.futures as cf
    real = cf.ThreadPoolExecutor
    monkeypatch.setattr(cf, "ThreadPoolExecutor",
                        lambda *a, **k: real(max_workers=1, thread_name_prefix="one-worker"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def check_needs_db():
        return [] if r._db() is None else []

    r._run_detectors([check_needs_db, check_ok], budget_s=5.0)
    assert r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]["degraded_fns"] == ["check_needs_db"]


def test_each_sweep_gets_its_own_identity_and_the_history_is_bounded(r):
    ids = []
    for _ in range(r._SWEEP_OUTCOMES_KEEP + 5):
        r._run_detectors([check_ok], budget_s=5.0)
        ids.append(r._LAST_SWEEP["sweep_id"])
    assert len(set(ids)) == len(ids)
    assert len(r._SWEEP_OUTCOMES) == r._SWEEP_OUTCOMES_KEEP
    assert ids[-1] in r._SWEEP_OUTCOMES and ids[0] not in r._SWEEP_OUTCOMES


# ── _persist_findings_to_db → record_sweep ────────────────────────────────

class FakeCur:
    def __init__(self):
        self.sql = []

    def execute(self, q, params=None):
        self.sql.append(" ".join(str(q).split()))

    def fetchone(self):
        return (0, 0, 0)

    @property
    def rowcount(self):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self):
        self.cur, self.committed = FakeCur(), False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


OUTCOMES = {"sweep_id": "s1", "at": 1.0, "completed_fns": ["check_ok"], "degraded_fns": [],
            "crashed_fns": [], "timeout_fns": [], "abandoned_fns": []}


def _persist(r, monkeypatch, findings, *, full_sweep=True, ledger_raises=False, last_sweep_id="s1"):
    import psycopg2
    from routes import brain_detector_ledger as ledger
    from routes import brain_findings_writer as writer
    conn, calls = FakeConn(), []
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(writer, "upsert_brain_finding", lambda cur, **kw: "updated")
    monkeypatch.setattr(writer, "_ensure_schema", lambda cur, force=False: None)
    monkeypatch.setattr(writer, "_schema", {"cols": {"detector_fn"}})

    def fake_record(cur, sweep, fs):
        calls.append((dict(sweep), list(fs)))
        if ledger_raises:
            raise RuntimeError("permission denied for schema public")
        return {"recorded": len(fs), "skipped": None}

    monkeypatch.setattr(ledger, "record_sweep", fake_record)
    r._SWEEP_OUTCOMES["s1"] = dict(OUTCOMES)
    r._LAST_SWEEP.update({"sweep_id": last_sweep_id, "completed_fns": ["check_ok"], "at": 1.0})
    r._persist_findings_to_db(findings, full_sweep=full_sweep)
    return calls, conn


def _finding(sweep_id="s1"):
    f = {"issue": "operator_profile_gap:Equinix", "url": "/operators/equinix",
         "_detector_fn": "check_ok"}
    if sweep_id is not None:
        f["_sweep_id"] = sweep_id
    return f


def test_a_full_sweep_reaches_the_ledger_with_its_own_outcomes(r, monkeypatch):
    calls, conn = _persist(r, monkeypatch, [_finding()])
    assert len(calls) == 1 and conn.committed
    sweep, findings = calls[0]
    assert sweep == OUTCOMES and findings == [_finding()]


def test_a_sweep_is_still_recorded_after_another_scan_replaced_last_sweep(r, monkeypatch):
    """The heal-findings refresh thread calls scan_all() without the lock, so
    _LAST_SWEEP can describe a different sweep by the time this one persists."""
    calls, _ = _persist(r, monkeypatch, [_finding()], last_sweep_id="heal-refresh")
    assert len(calls) == 1 and calls[0][0]["sweep_id"] == "s1"


@pytest.mark.parametrize("findings,full_sweep", [
    ([_finding("s0")], True),
    ([_finding(None)], True),
    ([_finding(), _finding("s0")], True),
    ([_finding()], False),
], ids=["unknown-sweep", "no-sweep-id", "mixed-sweeps", "partial-persist"])
def test_findings_without_one_known_sweep_never_reach_the_ledger(r, monkeypatch, findings,
                                                                full_sweep):
    calls, _ = _persist(r, monkeypatch, findings, full_sweep=full_sweep)
    assert calls == []


def test_a_ledger_failure_rolls_back_only_its_savepoint(r, monkeypatch):
    calls, conn = _persist(r, monkeypatch, [_finding()], ledger_raises=True)
    assert len(calls) == 1
    assert "ROLLBACK TO SAVEPOINT bf_detector_ledger" in conn.cur.sql
    assert conn.committed, "a ledger failure must not discard the findings upsert"
