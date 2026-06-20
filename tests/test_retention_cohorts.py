"""tests/test_retention_cohorts.py — RETENTION COHORT analyzer (2026-06-19).

NO real DB / NO network. We inject a FAKE psycopg2 connection whose cursor
pattern-matches each cohort query (by a substring of the SQL) and returns canned
rows. That lets us assert, with zero infra:

  · compute_retention_cohorts() returns the right STRUCTURED SHAPE
    (new_keys, return_rate, reuse_buckets{1,2-5,6+}, multiday_rate,
     first_tool_mix, retention_by_first_tool, median_time_to_2nd_call);
  · a retention rate is distinct-returners / distinct-first-callers
    (returned_keys/new_keys), NOT COUNT(*)/COUNT(*);
  · retention_by_first_tool rate = returned/first_callers per tool;
  · the DE-LOOP is applied — every emitted SQL excludes the canonical
    mcp_calls_deloop.PROBE_PLATFORMS, and COUNT(DISTINCT api_key) is used;
  · best-effort: {} when there is no DB and {} / new_keys=0 on empty data,
    never raising;
  · the admin gate on GET /api/v1/mcp/retention/cohorts (403 vs 200).
"""
import pytest

rc = pytest.importorskip("routes.retention_cohorts")


# ── A fake cursor that dispatches canned rows by SQL substring ──────────
class _FakeCursor:
    def __init__(self, store):
        self._store = store           # the recording list of executed SQL
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._store.append(sql)
        s = " ".join(sql.split())     # collapse whitespace for matching
        self._last = s

    def fetchall(self):
        # The analyzer reads every result via fetchall() (one helper, `run`),
        # so each canned result is a LIST of row tuples keyed on a SQL substring.
        s = self._last
        # (1)+(2)+(5) headline: new_keys / returned_keys / multiday_keys
        if "AS new_keys," in s and "AS returned_keys," in s and "AS multiday_keys" in s:
            return [(10, 4, 3)]       # 10 new, 4 returned, 3 multi-day
        # (3) reuse buckets: one_off / light / heavy
        if "AS one_off," in s and "AS light," in s and "AS heavy" in s:
            return [(6, 3, 1)]        # 6 one-off, 3 light, 1 heavy
        # (4) first-tool mix
        if "SELECT first_tool, COUNT(DISTINCT api_key) AS keys" in s:
            return [("get_grid_intelligence", 6), ("get_market_intel", 4)]
        # (5cont) retention by first tool
        if "f.first_tool," in s and "AS first_callers" in s:
            # grid: 6 first-callers, 3 returned (50%); market: 4 first, 1 (25%).
            return [
                ("get_grid_intelligence", 6, 3),
                ("get_market_intel", 4, 1),
            ]
        # (6) time-to-2nd-call: median_s / p95_s / n
        if "AS median_s," in s and "AS p95_s," in s:
            return [(3600.0, 86400.0, 4)]
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.executed = []            # every SQL string the analyzer issued
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.executed)

    def close(self):
        self.closed = True


# ── compute_retention_cohorts ──────────────────────────────────────────
def test_cohort_shape_and_return_rate_is_distinct_over_distinct():
    conn = _FakeConn()
    out = rc.compute_retention_cohorts(days=30, conn=conn)

    # window echoed + headcounts present.
    assert out["window_days"] == 30
    assert out["new_keys"] == 10
    assert out["returned_keys"] == 4

    # A retention rate is distinct-returners / distinct-first-callers → 4/10.
    assert out["return_rate"] == pytest.approx(40.0)
    # multi-day return rate = 3/10.
    assert out["multiday_returned_keys"] == 3
    assert out["multiday_rate"] == pytest.approx(30.0)

    # reuse buckets exact shape {1, 2-5, 6+}.
    assert out["reuse_buckets"] == {"1": 6, "2-5": 3, "6+": 1}

    # first-tool mix: list of {tool,count,pct}; pct over new_keys (10).
    mix = {m["tool"]: m for m in out["first_tool_mix"]}
    assert mix["get_grid_intelligence"]["count"] == 6
    assert mix["get_grid_intelligence"]["pct"] == pytest.approx(60.0)

    # retention_by_first_tool: per-tool rate = returned/first_callers.
    rbt = {r["tool"]: r for r in out["retention_by_first_tool"]}
    assert rbt["get_grid_intelligence"]["first_callers"] == 6
    assert rbt["get_grid_intelligence"]["returned"] == 3
    assert rbt["get_grid_intelligence"]["return_rate"] == pytest.approx(50.0)
    assert rbt["get_market_intel"]["return_rate"] == pytest.approx(25.0)
    # grid_intelligence retains better → it sorts first (desc by rate).
    assert out["retention_by_first_tool"][0]["tool"] == "get_grid_intelligence"

    # time-to-2nd-call median/p95 surfaced.
    assert out["median_time_to_2nd_call_seconds"] == pytest.approx(3600.0)
    assert out["p95_time_to_2nd_call_seconds"] == pytest.approx(86400.0)


def test_deloop_and_distinct_applied_in_every_query():
    """HONEST-NUMBERS: every emitted SQL must (a) exclude the canonical
    PROBE_PLATFORMS and (b) count keys with COUNT(DISTINCT api_key)."""
    from mcp_calls_deloop import PROBE_PLATFORMS

    conn = _FakeConn()
    rc.compute_retention_cohorts(days=30, conn=conn)
    assert conn.executed, "analyzer issued no SQL"

    joined = " ".join(conn.executed)
    # The de-loop list is present (reused from the canonical module, NOT a
    # hand-rolled second list). Check a representative probe bucket + the folded
    # internal-traffic bucket — both must be excluded.
    assert "internal-dchub" in joined
    for probe in PROBE_PLATFORMS:
        assert f"'{probe}'" in joined, f"probe platform {probe!r} not excluded"
    # Every cohort query filters on the de-looped platform predicate.
    for sql in conn.executed:
        assert "NOT IN (" in sql, "a query skipped the de-loop exclusion"
    # COUNT(DISTINCT api_key) is the headcount discipline (never bare COUNT(*)
    # for a cohort population).
    assert "COUNT(DISTINCT api_key)" in joined or "COUNT(DISTINCT f.api_key)" in joined


def test_no_db_returns_empty_dict_not_raise():
    out = rc.compute_retention_cohorts(days=30, conn=None)
    assert out == {}


def test_empty_data_yields_zero_new_keys_no_raise(monkeypatch):
    """When there is no de-looped identified traffic, the headline query returns
    (0,0,0) → the analyzer reports new_keys=0 and never divides by zero."""
    class _EmptyCursor(_FakeCursor):
        def fetchone(self):
            s = self._last
            if "AS new_keys," in s:
                return (0, 0, 0)
            return None

        def fetchall(self):
            return []

    class _EmptyConn(_FakeConn):
        def cursor(self):
            return _EmptyCursor(self.executed)

    conn = _EmptyConn()
    out = rc.compute_retention_cohorts(days=30, conn=conn)
    assert out.get("new_keys") == 0
    assert "return_rate" not in out or out.get("return_rate") in (0.0, None)


def test_days_is_clamped():
    assert rc._clamp_days(0) == 1
    assert rc._clamp_days(9999) == 180
    assert rc._clamp_days("not a number") == 30
    assert rc._clamp_days(45) == 45


# ── evidence adapter (what the brain investigator reads) ───────────────
def test_evidence_items_have_claim_source_value(monkeypatch):
    canned = {
        "window_days": 30,
        "new_keys": 10,
        "returned_keys": 4,
        "return_rate": 40.0,
        "multiday_returned_keys": 3,
        "multiday_rate": 30.0,
        "reuse_buckets": {"1": 6, "2-5": 3, "6+": 1},
        "first_tool_mix": [{"tool": "get_grid_intelligence", "count": 6, "pct": 60.0}],
        "retention_by_first_tool": [
            {"tool": "get_grid_intelligence", "first_callers": 6, "returned": 3, "return_rate": 50.0},
        ],
        "median_time_to_2nd_call_seconds": 3600.0,
        "p95_time_to_2nd_call_seconds": 86400.0,
    }
    monkeypatch.setattr(rc, "compute_retention_cohorts", lambda **k: dict(canned))
    items = rc.retention_cohort_evidence(days=30)
    assert items, "expected evidence items"
    for it in items:
        assert set(it.keys()) >= {"claim", "source", "value"}
    joined = " ".join(f"{it['claim']} {it['value']}" for it in items).lower()
    # The return rate, the reuse split, and the retaining tool all surface.
    assert "return rate" in joined
    assert "40.0%" in joined
    assert "get_grid_intelligence" in joined


def test_evidence_empty_when_no_data(monkeypatch):
    monkeypatch.setattr(rc, "compute_retention_cohorts", lambda **k: {})
    assert rc.retention_cohort_evidence(days=30) == []


# ── endpoint admin gate ────────────────────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(rc.retention_cohorts_bp)
    return app.test_client()


def test_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setattr(rc, "_admin_ok", lambda: False)
    resp = client.get("/api/v1/mcp/retention/cohorts")
    assert resp.status_code == 403


def test_endpoint_returns_cohorts_when_admin(client, monkeypatch):
    monkeypatch.setattr(rc, "_admin_ok", lambda: True)
    monkeypatch.setattr(rc, "compute_retention_cohorts",
                        lambda **k: {"window_days": 30, "new_keys": 10,
                                     "return_rate": 40.0})
    monkeypatch.setattr(rc, "retention_cohort_evidence", lambda **k: [
        {"claim": "Return rate", "source": "mcp_call_log", "value": "40.0%"}])
    resp = client.get("/api/v1/mcp/retention/cohorts?days=30")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["cohorts"]["new_keys"] == 10
    assert data["cohorts"]["return_rate"] == 40.0


def test_endpoint_empty_window_is_ok(client, monkeypatch):
    monkeypatch.setattr(rc, "_admin_ok", lambda: True)
    monkeypatch.setattr(rc, "compute_retention_cohorts", lambda **k: {})
    resp = client.get("/api/v1/mcp/retention/cohorts")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["cohorts"] == {}
