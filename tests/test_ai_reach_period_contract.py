"""/api/v1/ai/reach — the ?period= contract.

★ WHY THIS EXISTS (2026-08-09). The endpoint took no query parameters and read
none, so `?period=7d`, `?period=30d`, `?period=all` and no param at all returned
BYTE-IDENTICAL JSON — measured live, md5 identical across all three named
periods. Every field was hardcoded 7-day (distinct_agents_7d, real_agents_7d,
real_calls_7d, per_platform), so a caller asking for 30 days got 7 days of
numbers, under keys that say 7d, with a 200 and no warning.

These tests pin the three properties that make that impossible to recreate:

  1. A 30d/all response is COMPUTED OVER THAT WINDOW — the SQL carries the
     requested interval, not a 7-day one.
  2. A non-7d response carries NO `*_7d` keys. This is the anti-mislabel rule:
     emitting real_agents_7d inside a 30d payload would rebuild the original
     lie one level deeper, and it would look like a fix.
  3. An unrecognised period is a 400, not a 7-day payload wearing the caller's
     label.

The DB branch is exercised through a stub cursor, NOT skipped: the degraded
no-DATABASE_URL path returns before the window fields are ever built, so a test
that only saw the degraded response would pass against code that emits
real_agents_7d in the real branch — vacuous exactly where it matters.
"""
import datetime as _dt
import pytest

flask = pytest.importorskip("flask")


class _StubCursor:
    """Records every SQL string and answers the two SELECTs in order."""

    def __init__(self, log):
        self.log = log
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # The real code MUST NOT pass bound params: PLATFORM_CASE carries
        # literal % in its ILIKE patterns and psycopg2 would try to interpolate
        # them (a documented 500-class trap). Assert that here.
        assert params is None, f"bound params passed with literal-% SQL: {params!r}"
        self.log.append(sql)
        self._last = sql

    def fetchone(self):
        return {
            "agents": 123,
            "calls": 4567,
            "first_seen": _dt.datetime(2026, 5, 25, 1, 2, 3),
            "last_seen": _dt.datetime(2026, 8, 9, 4, 5, 6),
        }

    def fetchall(self):
        return [
            {"platform_id": "claude", "agents": 60, "requests": 3000},
            {"platform_id": "cursor", "agents": 5, "requests": 100},
        ]


class _StubConn:
    def __init__(self, log):
        self.log = log

    def cursor(self, **kw):
        return _StubCursor(self.log)

    def close(self):
        pass

    def rollback(self):
        pass


def _client(monkeypatch, log):
    from routes import ai_reach as mod
    monkeypatch.setattr(mod, "_conn", lambda: _StubConn(log))
    monkeypatch.setattr(mod, "_wcache", {})   # never serve another test's window
    app = flask.Flask(__name__)
    app.register_blueprint(mod.ai_reach_bp)
    return app.test_client()


def _get(monkeypatch, qs):
    log = []
    r = _client(monkeypatch, log).get("/api/v1/ai/reach" + qs)
    return r, r.get_json(), log


@pytest.mark.parametrize("period,needle", [("30d", "30 days"), ("all", None)])
def test_window_is_actually_applied_to_the_sql(monkeypatch, period, needle):
    """The requested window reaches the query — not a hardcoded 7 days."""
    r, body, log = _get(monkeypatch, f"?period={period}")
    assert r.status_code == 200, r.status_code
    sql = " ".join(log)
    assert "mcp_calls_identity" in sql
    assert "is_public_ip AND is_real_external" in sql
    # A 7-day interval must never appear in a 30d/all response's SQL.
    assert "interval '7 days'" not in sql, "30d/all still querying a 7-day window"
    if needle:
        assert needle in sql, f"requested window {period} not present in SQL"
    else:
        # all-time = no lower time bound at all
        assert "created_at >=" not in sql, "'all' must carry no lower time bound"
    assert body["period"] == period


@pytest.mark.parametrize("period", ["30d", "all"])
def test_non_7d_response_carries_no_7d_keys(monkeypatch, period):
    """THE anti-mislabel rule — a longer window must not ship 7d-named keys."""
    _r, body, _log = _get(monkeypatch, f"?period={period}")
    offenders = [k for k in body if k.endswith("_7d")]
    assert not offenders, f"{period} payload leaked 7d-named keys: {offenders}"
    # and it must publish window-neutral values instead
    assert body["real_agents"] == 123
    assert body["real_calls"] == 4567
    assert body["window_start"] and body["window_end"]


def test_window_publishes_its_own_basis(monkeypatch):
    """Each window names what it counted and over what span."""
    _r, body, _log = _get(monkeypatch, "?period=30d")
    assert body["basis"], "no basis published"
    assert "WINDOW FOR THIS RESPONSE" in body["basis"]
    assert "30" in body["basis"]
    assert body["window_days"] == 30


def test_all_time_says_it_means_since_retention_not_since_launch(monkeypatch):
    _r, body, _log = _get(monkeypatch, "?period=all")
    assert body["window_days"] is None
    b = body["basis"]
    assert "NOT" in b and "since DC Hub launched" in b, (
        "'all' must state it is bounded by retention, not by launch")
    assert body["window_start"].startswith("2026-05-25")


def test_unknown_period_is_rejected_not_silently_served_as_7d(monkeypatch):
    r, body, _log = _get(monkeypatch, "?period=90d")
    assert r.status_code == 400, (
        "unknown period returned %s — the old behaviour served 7d under the "
        "caller's label" % r.status_code)
    assert body["requested"] == "90d"
    assert "7d" in body["supported_periods"]
    # it must NOT contain reach numbers
    assert "real_agents" not in body and "per_platform" not in body


def test_default_and_7d_advertise_the_supported_periods(monkeypatch):
    """Discoverability: a caller can learn the windows exist."""
    from routes import ai_reach as mod
    monkeypatch.setattr(mod, "_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(mod, "_conn", lambda: None)   # fail-soft skeleton
    app = flask.Flask(__name__)
    app.register_blueprint(mod.ai_reach_bp)
    body = app.test_client().get("/api/v1/ai/reach").get_json()
    assert body["period"] == "7d"
    assert set(body["supported_periods"]) == {"7d", "30d", "all"}
    # the 7d payload KEEPS its 7d-named keys — that is correct there
    assert "distinct_agents_7d" in body
