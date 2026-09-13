"""resolve-on-absence may close a finding only on evidence that its producer looked.

Each fix here was right about one hole and missed the next:

  2026-07-17  scoped to radar rows. Foreign detectors write on slower cadences,
              and closing their rows 2 min after each write minted fake episodes.
  2026-09-05  the 2-minute arm scoped to detectors that COMPLETED the sweep.
              scan_all abandons 15-53 of ~140 detectors at its 25s budget, and a
              finding whose detector never reported was closed, then reopened as
              a new episode (gated_endpoint_missing_coaching 1099 -> 1100 in 4h).
  2026-09-13  "completed" still covered a detector that got no database, one whose
              query raised and was swallowed into [], and one that reported its
              own crash. The completed set came from _LAST_SWEEP, which a scan
              that never persists can replace first. And a 24h arm closed ANY
              open row not re-written for a day, from ANY detector, run or not.

THE INVARIANT (routes/brain_findings_resolve.py): every arm needs evidence that
the producer looked and did not see the row; anything unprovable stays open.

No database here: these pin which statements a sweep may run and with what, and
that the runner and the persist step feed them the right evidence.
tests/test_radar_resolve_arms_sql.py runs the statements on a real Postgres.
"""
import concurrent.futures as cf
import re
import sys
import time

import pytest


@pytest.fixture
def r(monkeypatch):
    from routes import brain_consistency_radar as mod
    monkeypatch.setattr(mod, "_LAST_SWEEP", {"completed_fns": [], "abandoned_fns": [],
                                             "registered_fns": [], "at": 0.0})
    monkeypatch.setattr(mod, "_SWEEP_OUTCOMES", {})
    return mod


# ── which statements a sweep may run ──────────────────────────────────────

SWEEP = {
    "sweep_id": "s_now", "at": 1.0,
    "completed_fns": ["check_clean", "check_no_db", "check_self_crash", "check_also_clean"],
    "degraded_fns": ["check_no_db"],
    "crashed_fns": ["check_raised"],
    "timeout_fns": ["check_slow_collect"],
    "abandoned_fns": ["check_abandoned"],
    "registered_fns": ["check_clean", "check_no_db", "check_self_crash", "check_also_clean",
                       "check_raised", "check_slow_collect", "check_abandoned"],
}
SELF_CRASH = {"issue": "consistency_radar_detector_crashed:check_self_crash",
              "url": "check_self_crash", "_detector_fn": "check_self_crash"}


def _arms(sweep, findings=(), *, fn_col_live=True, ledger_live=True):
    from routes import brain_findings_resolve as res
    return {arm: params for arm, _sql, params in
            res.plan(sweep, list(findings), fn_col_live=fn_col_live, ledger_live=ledger_live)}


def test_only_a_detector_that_ran_clean_can_close_a_row_on_absence():
    assert _arms(SWEEP, [SELF_CRASH])["clean_absence"] == {
        "clean": ["check_also_clean", "check_clean"], "sweep_id": "s_now"}, (
        "a degraded (no database, or a query raised), crashed, timed-out, abandoned "
        "or self-reported-crash detector must never count as having looked")


@pytest.mark.parametrize("sweep,ledger_live", [
    (None, True),
    ({**SWEEP, "sweep_id": ""}, True),
    (SWEEP, False),
], ids=["no-single-sweep", "sweep-without-identity", "no-ledger-table"])
def test_without_its_own_sweep_and_the_ledger_no_radar_row_closes(sweep, ledger_live):
    assert set(_arms(sweep, ledger_live=ledger_live)) == {"unattributed", "foreign"}


def test_the_radar_arms_need_the_provenance_column():
    assert set(_arms(SWEEP, fn_col_live=False)) == {"foreign"}


def test_the_orphan_arm_spares_every_detector_the_sweep_submitted():
    from routes import brain_findings_resolve as res
    assert _arms(SWEEP)["orphaned"] == {"registered": sorted(SWEEP["registered_fns"]),
                                        "days": res.ORPHAN_AFTER_DAYS}
    assert res.ORPHAN_AFTER_DAYS >= 7, "a detector missing for a day may just be a failed import"


def test_a_sweep_record_without_its_submitted_set_calls_nothing_orphaned():
    assert "orphaned" not in _arms({k: v for k, v in SWEEP.items() if k != "registered_fns"})


def test_a_sweep_with_no_clean_detector_runs_no_clean_absence_statement():
    sweep = {**SWEEP, "completed_fns": ["check_no_db"], "degraded_fns": ["check_no_db"]}
    assert "clean_absence" not in _arms(sweep)


# ── running them ──────────────────────────────────────────────────────────

class RecordingCur:
    """Records statements, answers the ledger probe, and can fail one arm."""

    def __init__(self, *, ledger_live=True, fail_update_containing=None):
        self.sql, self.ledger_live, self.fail, self._last = [], ledger_live, fail_update_containing, ""

    def execute(self, q, params=None):
        self._last = " ".join(str(q).split())
        self.sql.append(self._last)
        if self.fail and self._last.startswith("UPDATE") and self.fail in self._last:
            raise RuntimeError("canceling statement due to statement timeout")

    def fetchone(self):
        return (self.ledger_live,)

    @property
    def rowcount(self):
        return 2 if self._last.startswith("UPDATE") else -1


def test_an_arm_that_fails_is_rolled_back_alone():
    from routes import brain_findings_resolve as res
    cur = RecordingCur(fail_update_containing="r.outcome = 'completed'")
    assert res.resolve_absent(cur, dict(SWEEP), [SELF_CRASH], fn_col_live=True) == {
        "orphaned": 2, "unattributed": 2, "foreign": 2}
    assert "ROLLBACK TO SAVEPOINT bf_resolve_clean_absence" in cur.sql
    assert "RELEASE SAVEPOINT bf_resolve_foreign" in cur.sql


def test_without_the_ledger_table_the_radar_arms_never_run():
    from routes import brain_findings_resolve as res
    cur = RecordingCur(ledger_live=False)
    assert set(res.resolve_absent(cur, dict(SWEEP), [], fn_col_live=True)) == {
        "unattributed", "foreign"}


def test_nothing_is_resolved_when_the_plan_cannot_be_built(monkeypatch):
    from routes import brain_findings_resolve as res
    monkeypatch.setitem(sys.modules, "routes.brain_detector_ledger", None)
    cur = RecordingCur()
    assert res.resolve_absent(cur, dict(SWEEP), [], fn_col_live=True) == {}
    assert not [q for q in cur.sql if q.startswith("UPDATE")]


# ── the runner's evidence ─────────────────────────────────────────────────

def check_alpha():
    return [{"issue": "i_alpha", "url": "/a", "count": 1}]


def check_beta():
    return [{"issue": "i_beta", "url": "/b", "count": 1}]


def check_boom():
    raise ValueError("statement timeout")


def test_the_sweep_stamps_each_finding_and_publishes_who_reported(r):
    """Behavioural, not a source grep: when the sweep moved from scan_all into
    _run_detectors (2026-09-05) three substring assertions left with it, and a
    substring test cannot tell "the code moved" from "the stamping was deleted"."""
    out = r._run_detectors([check_alpha, check_beta], budget_s=10.0)
    assert {f["issue"]: f.get("_detector_fn") for f in out} == {
        "i_alpha": "check_alpha", "i_beta": "check_beta"}
    sweep = r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]
    assert sorted(sweep["completed_fns"]) == ["check_alpha", "check_beta"]
    assert sweep["registered_fns"] == ["check_alpha", "check_beta"]


def test_a_runner_finding_names_the_detector_it_is_about_not_as_its_producer(r):
    out = r._run_detectors([check_boom], budget_s=10.0)
    [crash] = [f for f in out if f["issue"] == "consistency_radar_detector_crashed:check_boom"]
    assert crash["_about_fn"] == "check_boom"
    assert "_detector_fn" not in crash, (
        "the ledger attributes reported keys by _detector_fn; the runner's own "
        "finding must never read as check_boom having reported it")


def test_a_result_nobody_read_is_abandoned_and_still_counts_as_submitted(r, monkeypatch):
    """A detector that finished between the last read and the tally used to land in
    no outcome list at all: invisible to the ledger, and missing from the set the
    orphan arm uses to tell a live detector from a deleted one."""
    real = cf.as_completed

    def read_one_then_give_up(fs, timeout=None):
        it = real(fs, timeout=timeout)
        yield next(it)
        time.sleep(0.3)  # the other detector finishes now, and is never read
        raise cf.TimeoutError()

    monkeypatch.setattr(cf, "as_completed", read_one_then_give_up)
    r._run_detectors([check_alpha, check_beta], budget_s=10.0)
    sweep = r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]
    [read] = sweep["completed_fns"]
    assert sweep["abandoned_fns"] == sorted({"check_alpha", "check_beta"} - {read})
    assert sweep["registered_fns"] == ["check_alpha", "check_beta"]


def check_slow():
    time.sleep(3)
    return []


def test_every_submitted_detector_lands_in_exactly_one_outcome(r):
    r._run_detectors([check_alpha, check_boom, check_slow], budget_s=1.0)
    sweep = r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]
    placed = sorted(fn for key in ("completed_fns", "crashed_fns", "timeout_fns", "abandoned_fns")
                    for fn in sweep[key])
    assert placed == sorted(sweep["registered_fns"]) == ["check_alpha", "check_boom",
                                                         "check_slow"], sweep


class FakeBaseCursor:
    """Stands in for a psycopg2 cursor class. tests/test_radar_resolve_arms_sql.py
    subclasses the real driver's cursor types the same way."""

    def execute(self, query, vars=None):
        if "missing_table" in query:
            raise RuntimeError('relation "missing_table" does not exist')

    def executemany(self, query, vars_list):
        raise RuntimeError("canceling statement due to statement timeout")


def test_a_tracking_cursor_marks_only_a_query_that_raised(r):
    tracked = r._query_tracking_cursor(FakeBaseCursor)
    assert r._query_tracking_cursor(FakeBaseCursor) is tracked
    r._DB_UNAVAILABLE.hit = False
    tracked().execute("SELECT 1")
    assert r._DB_UNAVAILABLE.hit is False
    with pytest.raises(RuntimeError):
        tracked().execute("SELECT * FROM missing_table")
    assert r._DB_UNAVAILABLE.hit is True, "the error must reach the detector AND be seen"
    r._DB_UNAVAILABLE.hit = False
    with pytest.raises(RuntimeError):
        tracked().executemany("UPDATE t SET x = %s", [(1,)])
    assert r._DB_UNAVAILABLE.hit is True


def test_a_detector_that_swallows_a_raised_query_is_recorded_degraded(r):
    tracked = r._query_tracking_cursor(FakeBaseCursor)

    def check_swallows_a_query_error():
        try:
            tracked().execute("SELECT * FROM missing_table")
        except Exception:
            pass
        return []

    def check_queries_cleanly():
        tracked().execute("SELECT 1")
        return []

    r._run_detectors([check_swallows_a_query_error, check_queries_cleanly], budget_s=10.0)
    sweep = r._SWEEP_OUTCOMES[r._LAST_SWEEP["sweep_id"]]
    assert sweep["degraded_fns"] == ["check_swallows_a_query_error"]
    assert sorted(sweep["completed_fns"]) == ["check_queries_cleanly",
                                              "check_swallows_a_query_error"]


def test_db_and_ro_conn_open_query_tracking_connections(r, monkeypatch):
    import psycopg2
    import psycopg2.extensions

    class Conn:
        autocommit = None

    kwargs_seen = []
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: kwargs_seen.append(k) or Conn())
    for name in ("NEON_REPLICA_URL", "READ_REPLICA_URL", "DATABASE_REPLICA_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    assert r._db() is not None and r._ro_conn() is not None
    factory = r._query_tracking_connection()
    assert issubclass(factory, psycopg2.extensions.connection)
    assert [k.get("connection_factory") for k in kwargs_seen] == [factory, factory]


def test_ro_conn_that_reaches_no_database_marks_the_run(r, monkeypatch):
    import psycopg2

    def refuse(*a, **k):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    r._DB_UNAVAILABLE.hit = False
    assert r._ro_conn() is None
    assert r._DB_UNAVAILABLE.hit is True


# ── the persist step hands the arms this sweep's evidence ─────────────────

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
            "crashed_fns": [], "timeout_fns": [], "abandoned_fns": [],
            "registered_fns": ["check_ok"]}


def _persist(r, monkeypatch, findings, *, full_sweep=True):
    import psycopg2
    from routes import brain_findings_resolve as res
    from routes import brain_findings_writer as writer
    conn, calls, writes = FakeConn(), [], []
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(writer, "upsert_brain_finding",
                        lambda cur, **kw: writes.append(kw) or "updated")
    monkeypatch.setattr(writer, "_ensure_schema", lambda cur, force=False: None)
    monkeypatch.setattr(writer, "_schema", {"cols": {"detector_fn"}})

    def fake_resolve(cur, sweep, fs, *, fn_col_live):
        calls.append((sweep, list(fs), fn_col_live))
        return {"foreign": 1}

    monkeypatch.setattr(res, "resolve_absent", fake_resolve)
    r._SWEEP_OUTCOMES["s1"] = dict(OUTCOMES)
    # Another scan_all() replaced _LAST_SWEEP between this sweep and its persist.
    r._LAST_SWEEP.update({"sweep_id": "heal-refresh", "completed_fns": ["check_everything"],
                          "registered_fns": ["check_everything"], "at": 2.0})
    r._persist_findings_to_db(findings, full_sweep=full_sweep)
    return calls, writes, conn


def _finding(sweep_id="s1"):
    f = {"issue": "operator_profile_gap:Equinix", "url": "/operators/equinix",
         "_detector_fn": "check_ok"}
    if sweep_id is not None:
        f["_sweep_id"] = sweep_id
    return f


def test_a_full_sweep_resolves_with_its_own_outcomes_not_last_sweep(r, monkeypatch):
    calls, _, conn = _persist(r, monkeypatch, [_finding()])
    assert conn.committed
    [(sweep, findings, fn_col_live)] = calls
    assert sweep == OUTCOMES, f"resolved against a sweep that is not this one: {sweep}"
    assert findings == [_finding()] and fn_col_live is True


@pytest.mark.parametrize("findings", [
    [_finding("s0")],
    [_finding(None)],
    [_finding(), _finding("s0")],
], ids=["unknown-sweep", "no-sweep-id", "mixed-sweeps"])
def test_findings_without_one_known_sweep_resolve_with_no_sweep(r, monkeypatch, findings):
    calls, _, _ = _persist(r, monkeypatch, findings)
    [(sweep, _fs, _col)] = calls
    assert sweep is None


def test_a_partial_persist_resolves_nothing(r, monkeypatch):
    calls, _, conn = _persist(r, monkeypatch, [_finding()], full_sweep=False)
    assert calls == [] and conn.committed


def test_a_runner_finding_is_stored_under_the_detector_it_is_about(r, monkeypatch):
    crash = {"issue": "consistency_radar_detector_crashed:check_boom", "url": "check_boom",
             "_about_fn": "check_boom", "_sweep_id": "s1"}
    _, writes, _ = _persist(r, monkeypatch, [_finding(), crash])
    assert [w["detector_fn"] for w in writes] == ["check_ok", "check_boom"]


def test_writer_only_writes_detector_fn_when_declared_and_present():
    import inspect
    from routes import brain_findings_writer as w
    src = inspect.getsource(w.upsert_brain_finding)
    assert "detector_fn" in inspect.signature(w.upsert_brain_finding).parameters
    m = re.search(r"write_fn\s*=\s*(.+)", src)
    assert m, "no write_fn guard"
    assert "detector_fn" in m.group(1) and "cols" in m.group(1), (
        f"write_fn must require both a declared value and the live column: {m.group(1)}")
