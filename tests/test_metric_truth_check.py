"""routes/metric_truth_check.py test suite (2026-07-11).

All mocked (no DB, no network, never imports main). Contract under test:
  1. divergence math — the 30% band + the |shell−truth| >= 3 absolute floor
  2. a wildly-wrong shell value (the 5-vs-4,903 verified-facilities class)
     files EXACTLY one finding through the canonical writer
  3. kill switch METRIC_TRUTH_CHECK_DISABLE ⇒ zero DB touches
  4. idempotency — a second run in the same ISO week is a no-op
  5. a stale shell snapshot (>8d) is itself a finding (the 7d-stuck class)
  6. endpoints are admin-gated; cron_heartbeat wires blueprint + Sunday job
"""
import datetime as _dt

import flask
import pytest

import routes.metric_truth_check as mtc


NOW = _dt.datetime(2026, 7, 12, 15, 30, tzinfo=_dt.timezone.utc)  # a Sunday
FRESH = NOW - _dt.timedelta(hours=6)


# ── fakes: SQL-substring-routed cursor ────────────────────────────────
class _Cur:
    def __init__(self, responses, log):
        self.responses = responses      # list of (sql-fragment, row)
        self.log = log
        self._next = None
        self.connection = self          # for cur.connection.rollback()

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self._next = None
        for frag, row in self.responses:
            if frag in sql:
                self._next = row
                return

    def fetchone(self):
        return self._next

    def rollback(self):
        pass


class _Conn:
    def __init__(self, responses, log):
        self.responses = responses
        self.log = log
        self.commits = 0

    def cursor(self):
        return _Cur(self.responses, self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _healthy_responses(week_claimed=True):
    """Canned rows for a run where only verified_facilities diverges."""
    return [
        ("information_schema.columns", (1,)),
        ("INSERT INTO metric_truth_runs", (("2026-W28",) if week_claimed else None)),
        ("FROM media_master_snapshots", (7, 4, FRESH)),
        ("FROM linkedin_posts", (5,)),
        ("FROM social_media_posts", (2,)),
        ("FROM ai_citations", (4,)),
        ("FROM facility_count_snapshots", (5, (NOW - _dt.timedelta(days=1)).date())),
        ("FROM discovered_facilities", (4903,)),
        ("FROM audience_snapshots", (21, FRESH)),
        ("FROM mcp_calls_identity", (20,)),
        ("FROM brain_automerge_log", (3,)),
        ("FROM brain_proposed_code_fixes", (4,)),
    ]


@pytest.fixture
def filed(monkeypatch):
    """Capture canonical-writer calls; never touch the real writer."""
    calls = []

    def _fake_upsert(cur, issue, url="", count=1, detail="", detector=None,
                     status="open"):
        calls.append({"issue": issue, "detail": detail, "detector": detector})
        return "inserted"

    monkeypatch.setattr(mtc, "upsert_brain_finding", _fake_upsert)
    return calls


# ── 1. divergence math ────────────────────────────────────────────────
def test_divergence_math():
    assert mtc.compute_divergence(None, 5) is None
    assert mtc.compute_divergence(5, None) is None
    assert mtc.compute_divergence(100, 100) == 0.0
    assert mtc.compute_divergence(35, 100) == pytest.approx(0.65)
    assert mtc.compute_divergence(0, 0) == 0.0

    assert mtc.is_diverged(35, 100, 0.30) is True            # the media bug
    assert mtc.is_diverged(5, 4903, 0.30) is True            # the fleet bug
    assert mtc.is_diverged(21, 20, 0.30) is False            # within band
    assert mtc.is_diverged(2, 0, 0.30) is False              # abs floor: diff 2 < 3
    assert mtc.is_diverged(4, 0, 0.30) is True               # 4-vs-0 trips
    assert mtc.is_diverged(None, 100, 0.30) is False


# ── 2. divergence files exactly one finding via the canonical writer ──
def test_run_check_files_finding_on_divergence(monkeypatch, filed):
    monkeypatch.delenv("METRIC_TRUTH_CHECK_DISABLE", raising=False)
    log = []
    conn = _Conn(_healthy_responses(), log)
    monkeypatch.setattr(mtc, "_conn", lambda: conn)

    report = mtc.run_check(now=NOW)
    assert report["ok"] is True
    verdicts = {r["metric"]: r["verdict"] for r in report["results"]}
    assert verdicts["verified_facilities"] == "diverged"
    assert verdicts["media_posts_24h"] == "ok"          # 7 vs 5+2
    assert verdicts["citation_velocity_7d"] == "ok"     # 4 vs 4
    assert verdicts["real_agents_7d"] == "ok"           # 21 vs 20, in band
    assert verdicts["automerge_merges_7d"] == "ok"      # |3-4| < abs floor

    assert report["findings_filed"] == 1
    assert len(filed) == 1
    assert "verified_facilities" in filed[0]["issue"]
    assert filed[0]["detector"] == "metric_truth_check"
    assert "4903" in filed[0]["detail"]
    # report carries the swallowed-write counters (same disease, one surface)
    assert "swallowed_write_counts" in report


# ── 3. kill switch ⇒ zero DB touches ─────────────────────────────────
def test_kill_switch_no_db(monkeypatch):
    monkeypatch.setenv("METRIC_TRUTH_CHECK_DISABLE", "1")

    def _boom():
        raise AssertionError("DB must not be touched when disabled")

    monkeypatch.setattr(mtc, "_conn", _boom)
    res = mtc.run_check(now=NOW)
    assert res["skipped"] == "METRIC_TRUTH_CHECK_DISABLE"


# ── 4. idempotent per ISO week ────────────────────────────────────────
def test_second_run_same_week_is_noop(monkeypatch, filed):
    monkeypatch.delenv("METRIC_TRUTH_CHECK_DISABLE", raising=False)
    log = []
    conn = _Conn(_healthy_responses(week_claimed=False), log)
    monkeypatch.setattr(mtc, "_conn", lambda: conn)

    res = mtc.run_check(now=NOW)
    assert res["skipped"] == "already_ran"
    assert res["iso_week"] == "2026-W28"
    assert filed == []
    # no metric SELECTs ran — the run stopped at the week claim
    assert not any("FROM media_master_snapshots" in s for (s, _p) in log)


# ── 5. stale shell snapshot is itself a finding ──────────────────────
def test_stale_snapshot_files_finding(monkeypatch, filed):
    monkeypatch.delenv("METRIC_TRUTH_CHECK_DISABLE", raising=False)
    stale_at = NOW - _dt.timedelta(days=20)
    responses = _healthy_responses()
    responses[2] = ("FROM media_master_snapshots", (7, 4, stale_at))
    conn = _Conn(responses, [])
    monkeypatch.setattr(mtc, "_conn", lambda: conn)

    report = mtc.run_check(now=NOW)
    verdicts = {r["metric"]: r["verdict"] for r in report["results"]}
    assert verdicts["media_posts_24h"] == "shell_stale_or_missing"
    assert verdicts["citation_velocity_7d"] == "shell_stale_or_missing"
    issues = [f["issue"] for f in filed]
    assert any("media_posts_24h" in i and "stale" in i for i in issues)


# ── 6. endpoints admin-gated; cron wiring ─────────────────────────────
@pytest.fixture
def client():
    app = flask.Flask(__name__)
    app.register_blueprint(mtc.metric_truth_bp)
    return app.test_client()


def test_check_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    assert client.post("/api/v1/admin/metric-truth/check").status_code == 403
    monkeypatch.setattr(mtc, "run_check", lambda force=False: {"ok": True})
    r = client.post("/api/v1/admin/metric-truth/check",
                    headers={"X-Admin-Key": "sekrit"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_check_endpoint_kill_switch(client, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    monkeypatch.setenv("METRIC_TRUTH_CHECK_DISABLE", "1")
    r = client.post("/api/v1/admin/metric-truth/check",
                    headers={"X-Admin-Key": "sekrit"})
    assert r.status_code == 200
    assert r.get_json()["skipped"] == "METRIC_TRUTH_CHECK_DISABLE"


def test_status_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    # no keys configured at all ⇒ still 403 (empty == empty must NOT pass)
    assert client.get("/api/v1/admin/metric-truth/status").status_code == 403


def test_cron_heartbeat_registers_blueprint_and_job(monkeypatch):
    from routes.cron_heartbeat import cron_heartbeat_bp, _DISPATCH, _HEAVY_LABELS
    app = flask.Flask(__name__)
    app.register_blueprint(cron_heartbeat_bp)
    assert "metric_truth" in app.blueprints
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/admin/metric-truth/check" in rules
    assert "/api/v1/admin/metric-truth/status" in rules

    jobs = {label: pred for (label, url, method, pred) in _DISPATCH}
    assert "metric_truth_check_weekly" in jobs
    assert "metric_truth_check_weekly" in _HEAVY_LABELS
    pred = jobs["metric_truth_check_weekly"]

    monkeypatch.delenv("METRIC_TRUTH_CHECK_DISABLE", raising=False)
    sun = _dt.datetime(2026, 7, 12, 15, 30)
    assert sun.weekday() == 6
    assert pred(sun) is True
    assert pred(_dt.datetime(2026, 7, 12, 16, 0)) is False   # wrong hour
    assert pred(_dt.datetime(2026, 7, 11, 15, 30)) is False  # Saturday
    # no-deploy kill switch works at the predicate level too
    monkeypatch.setenv("METRIC_TRUTH_CHECK_DISABLE", "1")
    assert pred(sun) is False
