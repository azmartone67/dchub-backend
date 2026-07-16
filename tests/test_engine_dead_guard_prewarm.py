"""Optimization-engine hardening tests (2026-07-16): dead-compute 503 guard +
never-cold prewarm tick.

Pure-unit — no DB, no network, never imports main
(reference_dchub_green_main_0709). Each engine's _raw_get is monkeypatched;
routes.engine_trends.record is stubbed so the cache path never touches main/DB.

Root cause being guarded: after a deploy, a COLD engine tick (~13.7s) tripped
the CF worker's per-attempt timeout → failover to the stale Render backend,
whose loopback self-requests all fail server-side → it served a valid-looking
HTTP 200 with every dimension 0.0, which the worker cached (KV fresh 300s /
stale 86400s) — a self-refreshing poison loop.

Covers:
  * all sources dead → 503 {ok:false, error:'engine sources unavailable'},
    and the garbage frame is never cached into _RESP
  * legitimately-zero metrics from LIVE sources still serve 200 (never 503)
  * partial source outage serves 200 but does NOT cache (retry next request)
  * healthy tick serves 200, caches, repeat hit is served from cache
  * _RESP_TTL is 300s on both engines
  * stale-while-revalidate: expired-but-present cache answers instantly from
    the last GOOD copy + refreshes in a background single-flight thread; dead
    sources never overwrite the good copy; past _STALE_MAX it blocks (dead→503)
  * warm(): cold computes + caches; fresh is a TTL no-op; dead never caches
  * POST /api/v1/engines/prewarm warms BOTH engines in-process + kill switch
  * cron dispatch: engine_prewarm fires every invocation, POSTs the prewarm
    endpoint, and is NOT in _HEAVY_LABELS (must warm the WEB replica)
"""
from __future__ import annotations

import datetime
import time

import pytest

pytest.importorskip("flask")

import routes.mcp_leadership_engine as L
import routes.agent_utilization_engine as U


# ── healthy source fixtures ────────────────────────────────────────────────

_FUNNEL = {"paid_tool_demand_30d": [{"tool": "get_grid_data", "users": 25}],
           "conversions_30d": 4, "unique_ips_7d_real": 80,
           "tool_calls_7d_real": 900, "keys_by_tier": {"free": 10, "paid": 2}}
_REACH = {"distinct_agents_7d": 25, "distinct_platforms": 5}
_RETENTION = {"summary": {"pct_returned_next_week_mature": 5.0,
                          "returned_next_week_mature": 3,
                          "pct_reused_30d": 40.0, "latest_returning_ips": 2}}

LEAD_OK = {
    "/api/v1/brain/mcp-registries": {"present": ["a", "b"], "missing": ["c"], "total": 3},
    "/api/v1/mcp/standing": {"rank_highlights": ["#1 data-center intel"]},
    "/api/v1/ai/reach": _REACH,
    "/api/v1/mcp/retention": _RETENTION,
    "/api/v1/mcp/funnel": _FUNNEL,
    "/api/v1/citations/by-agent": {"by_agent": [{"agent": "claude"}, {"agent": "gpt"}]},
}
UTIL_OK = {
    "/api/v1/mcp/funnel": _FUNNEL,
    "/api/v1/ai/reach": _REACH,
    "/api/v1/mcp/retention": _RETENTION,
    "/api/v1/onboard": {"ok": True},
    "/api/v1/agent/cookbook": {"count": 6},
}


def _stub_sources(monkeypatch, mod, table):
    monkeypatch.setattr(mod, "_raw_get",
                        lambda path, timeout=18: dict(table.get(path) or {}))


def _stub_dead(monkeypatch, mod):
    # a failed-over/stale backend: every loopback self-request fails → {}
    monkeypatch.setattr(mod, "_raw_get", lambda path, timeout=18: {})


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Reset both engines' module caches + stub the trend recorder (its real
    implementation lazy-imports main for a DB pool — forbidden in unit tests)."""
    import routes.engine_trends as T
    monkeypatch.setattr(T, "record", lambda *a, **k: None)
    for mod in (L, U):
        mod._RESP["at"] = 0.0
        mod._RESP["v"] = None
    yield
    for mod in (L, U):
        mod._RESP["at"] = 0.0
        mod._RESP["v"] = None


def _app(include_prewarm=False):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(L.mcp_leadership_bp)
    app.register_blueprint(U.agent_utilization_bp)
    if include_prewarm:
        from routes.engine_prewarm import engine_prewarm_bp
        app.register_blueprint(engine_prewarm_bp)
    return app


# ── FIX 1: dead-compute guard ──────────────────────────────────────────────

@pytest.mark.parametrize("mod,route", [(L, "/api/v1/mcp/leadership"),
                                       (U, "/api/v1/agents/utilization")])
def test_all_sources_dead_returns_503_and_never_caches(monkeypatch, mod, route):
    _stub_dead(monkeypatch, mod)
    client = _app().test_client()
    resp = client.get(route)
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "engine sources unavailable"
    # the garbage frame must never enter the response cache
    assert mod._RESP["v"] is None
    # and must never be edge/KV-cacheable
    assert "no-store" in (resp.headers.get("Cache-Control") or "")


def test_legit_zero_metrics_from_live_sources_still_200(monkeypatch):
    # every source RESPONDS but every metric is genuinely zero — that is a real
    # measurement (early-days truth), not a dead backend: must serve 200.
    zeros = {
        "/api/v1/brain/mcp-registries": {"present": [], "missing": [], "total": 0, "ok": True},
        "/api/v1/mcp/standing": {"rank_highlights": [], "ok": True},
        "/api/v1/ai/reach": {"distinct_agents_7d": 0, "distinct_platforms": 0, "ok": True},
        "/api/v1/mcp/retention": {"summary": {}, "ok": True},
        "/api/v1/mcp/funnel": {"paid_tool_demand_30d": [], "conversions_30d": 0, "ok": True},
        "/api/v1/citations/by-agent": {"by_agent": [], "ok": True},
    }
    _stub_sources(monkeypatch, L, zeros)
    resp = _app().test_client().get("/api/v1/mcp/leadership")
    assert resp.status_code == 200
    assert resp.get_json()["mcp_leadership_index"] == 0.0


def test_single_zero_dimension_never_trips_the_guard(monkeypatch):
    # one dimension legitimately zero (no citations yet) while the rest are
    # healthy → 200 with a nonzero index.
    table = dict(LEAD_OK)
    table["/api/v1/citations/by-agent"] = {"by_agent": [], "ok": True}
    _stub_sources(monkeypatch, L, table)
    resp = _app().test_client().get("/api/v1/mcp/leadership")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mcp_leadership_index"] > 0
    authority = next(d for d in body["dimensions"] if d["dimension"] == "authority")
    assert authority["score"] == 0.0


def test_partial_source_outage_serves_200_but_does_not_cache(monkeypatch):
    # one prefetch times out ({}) → degraded frame is SERVED (not a 503: other
    # sources are alive) but never frozen into the cache.
    table = dict(LEAD_OK)
    table["/api/v1/ai/reach"] = {}
    _stub_sources(monkeypatch, L, table)
    resp = _app().test_client().get("/api/v1/mcp/leadership")
    assert resp.status_code == 200
    assert L._RESP["v"] is None


# ── response cache behavior + TTL ──────────────────────────────────────────

def test_ttl_raised_to_300s_on_both_engines():
    assert L._RESP_TTL == 300.0
    assert U._RESP_TTL == 300.0


def test_healthy_tick_caches_and_repeat_hit_is_served_from_cache(monkeypatch):
    _stub_sources(monkeypatch, L, LEAD_OK)
    client = _app().test_client()
    first = client.get("/api/v1/mcp/leadership")
    assert first.status_code == 200
    assert L._RESP["v"] is not None

    # any further source read would blow up — the cache must answer instead
    def _boom(path, timeout=18):
        raise AssertionError("cache miss: _raw_get called on a fresh cache")
    monkeypatch.setattr(L, "_raw_get", _boom)
    second = client.get("/api/v1/mcp/leadership")
    assert second.status_code == 200
    assert second.get_json() == first.get_json()


def test_stale_past_ceiling_blocks_and_dead_sources_then_503(monkeypatch):
    _stub_sources(monkeypatch, L, LEAD_OK)
    client = _app().test_client()
    assert client.get("/api/v1/mcp/leadership").status_code == 200
    # cache older than the serve-stale ceiling + dead sources — the blocking
    # recompute must 503, not serve zeros (and not hour-old data forever)
    L._RESP["at"] = time.time() - (L._STALE_MAX + 5)
    _stub_dead(monkeypatch, L)
    resp = client.get("/api/v1/mcp/leadership")
    assert resp.status_code == 503


# ── stale-while-revalidate ─────────────────────────────────────────────────

def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_expired_cache_serves_stale_instantly_and_refreshes(monkeypatch):
    _stub_sources(monkeypatch, L, LEAD_OK)
    client = _app().test_client()
    first = client.get("/api/v1/mcp/leadership")
    assert first.status_code == 200
    stale_at = time.time() - (L._RESP_TTL + 5)
    L._RESP["at"] = stale_at

    # slow sources: the stale hit must NOT wait on the recompute
    def _slow(path, timeout=18):
        time.sleep(1.0)
        return dict(LEAD_OK.get(path) or {})
    monkeypatch.setattr(L, "_raw_get", _slow)

    t0 = time.time()
    resp = client.get("/api/v1/mcp/leadership")
    elapsed = time.time() - t0
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("served") == "stale-while-revalidate"
    assert body.get("cache_age_s", 0) > L._RESP_TTL
    assert body["mcp_leadership_index"] == first.get_json()["mcp_leadership_index"]
    assert elapsed < 0.5  # served from cache, not the 1s-per-source recompute
    # the background refresh lands and re-stamps the cache
    assert _wait_for(lambda: L._RESP["at"] > stale_at + 1)


@pytest.mark.parametrize("mod,route,table", [
    (L, "/api/v1/mcp/leadership", LEAD_OK),
    (U, "/api/v1/agents/utilization", UTIL_OK)])
def test_stale_serve_with_dead_sources_keeps_last_good_copy(monkeypatch, mod, route, table):
    _stub_sources(monkeypatch, mod, table)
    client = _app().test_client()
    assert client.get(route).status_code == 200
    good = mod._RESP["v"]
    mod._RESP["at"] = time.time() - (mod._RESP_TTL + 5)
    _stub_dead(monkeypatch, mod)
    resp = client.get(route)
    assert resp.status_code == 200  # stale GOOD copy beats a blocking 503 here
    assert resp.get_json().get("served") == "stale-while-revalidate"
    # the background refresh must NOT store the dead frame
    assert _wait_for(lambda: not mod._REFRESH_LOCK.locked())
    assert mod._RESP["v"] is good


# ── FIX 2: warm() + the prewarm endpoint ───────────────────────────────────

@pytest.mark.parametrize("mod,table", [(L, LEAD_OK), (U, UTIL_OK)])
def test_warm_cold_computes_then_fresh_noops(monkeypatch, mod, table):
    _stub_sources(monkeypatch, mod, table)
    cold = mod.warm()
    assert cold["warmed"] is True
    assert mod._RESP["v"] is not None
    fresh = mod.warm()
    assert fresh["warmed"] is False
    assert fresh["reason"] == "fresh"


@pytest.mark.parametrize("mod", [L, U])
def test_warm_never_caches_a_dead_frame(monkeypatch, mod):
    _stub_dead(monkeypatch, mod)
    out = mod.warm()
    assert out["warmed"] is False
    assert out["dead"] is True
    assert mod._RESP["v"] is None


def test_prewarm_endpoint_warms_both_engines_in_process(monkeypatch):
    monkeypatch.delenv("ENGINE_PREWARM_DISABLE", raising=False)
    _stub_sources(monkeypatch, L, LEAD_OK)
    _stub_sources(monkeypatch, U, UTIL_OK)
    resp = _app(include_prewarm=True).test_client().post("/api/v1/engines/prewarm")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["results"]["leadership"]["warmed"] is True
    assert body["results"]["utilization"]["warmed"] is True
    # the point of the tick: user requests now hit a warm cache
    assert L._RESP["v"] is not None
    assert U._RESP["v"] is not None


def test_prewarm_kill_switch(monkeypatch):
    monkeypatch.setenv("ENGINE_PREWARM_DISABLE", "1")
    resp = _app(include_prewarm=True).test_client().post("/api/v1/engines/prewarm")
    assert resp.status_code == 200
    assert resp.get_json()["disabled"] is True
    assert L._RESP["v"] is None  # nothing computed


# ── per-process self-warm loop ─────────────────────────────────────────────

def test_selfwarm_only_starts_on_railway_and_only_once(monkeypatch):
    import routes.engine_prewarm as P
    monkeypatch.setitem(P._SELFWARM, "started", False)
    started = []
    monkeypatch.setattr(P.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: started.append(kw)})())

    # local/dev/tests/Render: no Railway env → never starts
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.delenv("ENGINE_PREWARM_DISABLE", raising=False)
    assert P._start_selfwarm_thread() is False
    assert started == []

    # kill switch wins even on Railway
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj")
    monkeypatch.setenv("ENGINE_PREWARM_DISABLE", "1")
    assert P._start_selfwarm_thread() is False

    # Railway: starts exactly once, as a daemon thread
    monkeypatch.delenv("ENGINE_PREWARM_DISABLE")
    assert P._start_selfwarm_thread() is True
    assert len(started) == 1
    assert started[0]["daemon"] is True
    assert started[0]["target"] is P._selfwarm_loop
    assert P._start_selfwarm_thread() is False  # single-start
    assert len(started) == 1


def test_selfwarm_interval_stays_inside_response_ttl():
    import routes.engine_prewarm as P
    # the whole point: the per-process cache must never expire in steady state
    assert P._SELFWARM_INTERVAL < min(L._RESP_TTL, U._RESP_TTL)


# ── cron wiring ────────────────────────────────────────────────────────────

def test_cron_dispatch_fires_prewarm_every_invocation():
    import routes.cron_heartbeat as CH
    entries = [d for d in CH._DISPATCH if d[0] == "engine_prewarm"]
    assert len(entries) == 1
    label, url, method, pred = entries[0]
    assert url.endswith("/api/v1/engines/prewarm")
    assert method == "POST"
    # every invocation — the real heartbeat cadence is sporadic (~hourly at
    # random minutes), so any narrower window can miss for hours
    for dt in (datetime.datetime(2026, 7, 16, 4, 37),
               datetime.datetime(2026, 1, 1, 0, 0),
               datetime.datetime(2026, 12, 31, 23, 59)):
        assert pred(dt) is True
    # must stay a LIGHT job: heavy labels are proxied to the WORKER, which
    # would warm the wrong process's in-memory caches
    assert "engine_prewarm" not in CH._HEAVY_LABELS
