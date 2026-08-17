"""global-infra cache hardening (2026-08-17): Redis L2 + serve-stale + boot warm.

Pure-unit — no DB, no network, never imports main. Every builder is a stub and
the Redis client is a fake, so nothing here reaches gdacs.org / WRI / GEM /
PeeringDB.

Root cause being guarded: `_cached` was a per-process dict, so a COLD process
had no stale copy to fall back on and paid the full upstream build (7-9s) on a
user's request. `/api/v1/infrastructure/` is absent from the CF worker's
SLOW_PATH_PREFIXES, so a GET's first attempt is capped at 5s — every cold build
exceeded it and the caller got the worker's 503 envelope. Cold was routine, not
rare: 2 replicas each with their own dict, main deploying several times an hour,
and gunicorn recycling workers every ~1000 requests.

Covers:
  * fresh L1 serves without calling the builder
  * a cold process hydrates from Redis and NEVER builds (the post-deploy case)
  * X-Cache distinguishes hit / shared / stale / miss
  * expired-but-present serves instantly + refreshes in the background
  * a failed background refresh leaves the last GOOD copy intact
  * past _STALE_MAX it blocks and rebuilds; a build failure then still prefers
    the ancient copy over a 502
  * nothing cached anywhere + builder fails → 502 (unchanged contract)
  * single-flight: concurrent cold callers fire the builder ONCE
  * Redis round-trips through the zlib blob; a corrupt blob degrades, not raises
  * Redis being down degrades to L1 rather than failing the request
  * warm() builds when cold, no-ops when fresh (including fresh-in-Redis)
  * the warm thread is Railway-gated, kill-switched, and start-once
  * every _WARMABLE key is a real route builder
"""
from __future__ import annotations

import json
import threading
import time
import zlib

import pytest

pytest.importorskip("flask")

import routes.global_infra as G


# ── fakes ─────────────────────────────────────────────────────────────
class FakeRedis:
    """Binary KV with the two methods _rds() consumers use."""

    def __init__(self, fail=False):
        self.store = {}
        self.fail = fail
        self.sets = 0

    def get(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(k)

    def setex(self, k, ttl, v):
        if self.fail:
            raise RuntimeError("redis down")
        self.sets += 1
        self.store[k] = v


def _fc(n=1):
    return json.dumps({"type": "FeatureCollection",
                       "features": [{"id": i} for i in range(n)], "count": n})


class Builder:
    """Counts calls so a test can prove the builder did NOT run."""

    def __init__(self, body=None, exc=None, delay=0.0):
        self.body = body if body is not None else _fc()
        self.exc = exc
        self.delay = delay
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.body


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Isolate module cache state between tests; default to no Redis."""
    G._cache.clear()
    G._REFRESH.clear()
    G._BUILD.clear()
    G._SELFWARM["started"] = False
    monkeypatch.setattr(G, "_redis", {"client": None, "tried": True})
    yield
    G._cache.clear()
    G._REFRESH.clear()
    G._BUILD.clear()
    G._SELFWARM["started"] = False


def _use_redis(monkeypatch, fake):
    monkeypatch.setattr(G, "_redis", {"client": fake, "tried": True})
    return fake


def _call(key, builder):
    """Run _cached inside an app context and return (status, body, x_cache)."""
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/"):
        r = G._cached(key, builder)
        if isinstance(r, tuple):  # the 502 error tuple
            resp, status = r
            return status, resp.get_data(as_text=True), None
        return r.status_code, r.get_data(as_text=True), r.headers.get("X-Cache")


def _join_refresh(key, timeout=5.0):
    """Wait for the background refresh thread for `key` to finish."""
    for t in threading.enumerate():
        if t.name == f"ginfra-refresh-{key}":
            t.join(timeout)
    lk = G._REFRESH.get(key)
    if lk:  # the thread releases it in a finally
        assert lk.acquire(timeout=timeout), "refresh thread never released its lock"
        lk.release()


# ── the hot path ──────────────────────────────────────────────────────
def test_fresh_l1_serves_without_building():
    G._cache["k"] = {"data": _fc(3), "ts": time.time()}
    b = Builder()
    status, body, xc = _call("k", b)
    assert status == 200 and xc == "hit"
    assert b.calls == 0
    assert json.loads(body)["count"] == 3


def test_cold_process_hydrates_from_redis_and_never_builds(monkeypatch):
    """THE POST-DEPLOY CASE. A brand-new process (empty L1) with a warm shared
    copy must answer from Redis — this is the request that used to 503."""
    fake = _use_redis(monkeypatch, FakeRedis())
    fake.store[G._RKEY + "gdacs"] = G._blob_dump(_fc(303), time.time())
    b = Builder()
    status, body, xc = _call("gdacs", b)
    assert status == 200
    assert xc == "shared", "a Redis hydrate must be distinguishable from an L1 hit"
    assert b.calls == 0, "a cold process must not fetch upstream when Redis has the payload"
    assert json.loads(body)["count"] == 303
    assert G._cache["gdacs"]["data"], "the Redis read should populate L1 for next time"


def test_fresh_l1_short_circuits_before_redis(monkeypatch):
    fake = _use_redis(monkeypatch, FakeRedis(fail=True))  # any read raises
    G._cache["k"] = {"data": _fc(), "ts": time.time()}
    status, _, xc = _call("k", Builder())
    assert (status, xc) == (200, "hit")


def test_newer_redis_copy_wins_over_expired_l1(monkeypatch):
    """The other replica refreshed while this one's L1 aged out — take theirs
    rather than rebuild a dataset that is already fresh in the shared lane."""
    fake = _use_redis(monkeypatch, FakeRedis())
    G._cache["k"] = {"data": _fc(1), "ts": time.time() - G._TTL - 10}
    fake.store[G._RKEY + "k"] = G._blob_dump(_fc(99), time.time())
    b = Builder()
    status, body, xc = _call("k", b)
    assert (status, xc) == (200, "shared")
    assert b.calls == 0
    assert json.loads(body)["count"] == 99


# ── serve-stale-while-revalidate ──────────────────────────────────────
def test_expired_serves_stale_instantly_then_refreshes():
    G._cache["k"] = {"data": _fc(1), "ts": time.time() - G._TTL - 5}
    b = Builder(body=_fc(42))
    status, body, xc = _call("k", b)
    assert (status, xc) == (200, "stale")
    assert json.loads(body)["count"] == 1, "the user gets the OLD copy immediately"
    _join_refresh("k")
    assert b.calls == 1
    assert json.loads(G._cache["k"]["data"])["count"] == 42, "refresh must land"


def test_failed_background_refresh_keeps_the_good_copy():
    good = _fc(7)
    G._cache["k"] = {"data": good, "ts": time.time() - G._TTL - 5}
    b = Builder(exc=ValueError("gdacs: upstream is not a FeatureCollection"))
    status, body, xc = _call("k", b)
    assert (status, xc) == (200, "stale") and body == good
    _join_refresh("k")
    assert b.calls == 1
    assert G._cache["k"]["data"] == good, "a bad upstream must never overwrite good data"


def test_refresh_is_single_flight():
    G._cache["k"] = {"data": _fc(), "ts": time.time() - G._TTL - 5}
    b = Builder(delay=0.3)
    for _ in range(5):
        _call("k", b)
    _join_refresh("k")
    assert b.calls == 1, f"5 stale reads fired {b.calls} upstream refreshes"


def test_past_stale_max_blocks_and_rebuilds():
    G._cache["k"] = {"data": _fc(1), "ts": time.time() - G._STALE_MAX - 5}
    b = Builder(body=_fc(2))
    status, body, xc = _call("k", b)
    assert (status, xc) == (200, "miss")
    assert b.calls == 1
    assert json.loads(body)["count"] == 2, "week-old data must not be served forever"


def test_ancient_copy_still_beats_a_502():
    ancient = _fc(1)
    G._cache["k"] = {"data": ancient, "ts": time.time() - G._STALE_MAX - 5}
    status, body, xc = _call("k", Builder(exc=RuntimeError("HTTP Error 429")))
    assert (status, xc) == (200, "stale") and body == ancient


# ── the cold, empty, failing path (unchanged contract) ────────────────
def test_nothing_cached_and_builder_fails_is_502():
    status, body, _ = _call("k", Builder(exc=RuntimeError("HTTP Error 429: Too Many Requests")))
    assert status == 502
    doc = json.loads(body)
    assert doc["ok"] is False and "429" in doc["error"]


def test_cold_build_is_single_flight():
    """2 replicas × 8 gunicorn threads: a burst of map loads on a cold process
    must fire ONE upstream fetch, not one per request thread."""
    b = Builder(body=_fc(5), delay=0.4)
    results = []

    def go():
        results.append(_call("k", b))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 6 and all(r[0] == 200 for r in results)
    assert b.calls == 1, f"6 concurrent cold callers fired {b.calls} upstream builds"


# ── the Redis layer itself ────────────────────────────────────────────
def test_blob_roundtrips_and_compresses():
    body = _fc(2000)
    ts = time.time()
    raw = G._blob_dump(body, ts)
    assert len(raw) < len(body), "the blob must actually compress"
    back, back_ts = G._blob_load(raw)
    assert back == body and int(back_ts) == int(ts)


def test_store_writes_through_to_redis(monkeypatch):
    fake = _use_redis(monkeypatch, FakeRedis())
    G._store("k", _fc(4))
    assert fake.sets == 1
    raw = fake.store[G._RKEY + "k"]
    assert json.loads(G._blob_load(raw)[0])["count"] == 4


def test_corrupt_redis_blob_degrades_to_a_build(monkeypatch):
    fake = _use_redis(monkeypatch, FakeRedis())
    fake.store[G._RKEY + "k"] = b"not-a-blob-at-all"
    b = Builder(body=_fc(9))
    status, body, xc = _call("k", b)
    assert (status, xc) == (200, "miss")
    assert json.loads(body)["count"] == 9


def test_redis_down_degrades_to_l1(monkeypatch):
    _use_redis(monkeypatch, FakeRedis(fail=True))
    b = Builder(body=_fc(6))
    assert _call("k", b)[0] == 200          # the build still succeeds
    assert G._cache["k"]["data"], "L1 must still be populated when Redis is down"
    assert _call("k", Builder())[2] == "hit"  # and still served, without rebuilding


def test_rds_returns_none_without_redis_url(monkeypatch):
    monkeypatch.setattr(G, "_redis", {"client": None, "tried": False})
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert G._rds() is None


# ── warm() ────────────────────────────────────────────────────────────
def test_warm_builds_when_cold(monkeypatch):
    b = Builder(body=_fc(11))
    monkeypatch.setattr(G, "_WARMABLE", {"k": b})
    out = G.warm()
    assert out["k"]["warmed"] is True and b.calls == 1
    assert json.loads(G._cache["k"]["data"])["count"] == 11


def test_warm_is_a_noop_when_fresh(monkeypatch):
    b = Builder()
    monkeypatch.setattr(G, "_WARMABLE", {"k": b})
    G._cache["k"] = {"data": _fc(), "ts": time.time()}
    out = G.warm()
    assert out["k"]["warmed"] is False and out["k"]["reason"] == "fresh"
    assert b.calls == 0


def test_warm_is_a_noop_when_another_replica_already_filled_redis(monkeypatch):
    """The second replica's boot warm must be cheap, not a duplicate fetch."""
    fake = _use_redis(monkeypatch, FakeRedis())
    fake.store[G._RKEY + "k"] = G._blob_dump(_fc(50), time.time())
    b = Builder()
    monkeypatch.setattr(G, "_WARMABLE", {"k": b})
    out = G.warm()
    assert out["k"]["warmed"] is False and out["k"]["source"] == "redis"
    assert b.calls == 0


def test_warm_force_rebuilds_regardless(monkeypatch):
    b = Builder()
    monkeypatch.setattr(G, "_WARMABLE", {"k": b})
    G._cache["k"] = {"data": _fc(), "ts": time.time()}
    G.warm(force=True)
    assert b.calls == 1


def test_warm_survives_one_dead_source(monkeypatch):
    ok, dead = Builder(body=_fc(1)), Builder(exc=RuntimeError("HTTP Error 429"))
    monkeypatch.setattr(G, "_WARMABLE", {"dead": dead, "ok": ok})
    out = G.warm()
    assert out["dead"]["warmed"] is False and "429" in out["dead"]["error"]
    assert out["ok"]["warmed"] is True, "one dead upstream must not abort the rest"


# ── the warm thread's gates ───────────────────────────────────────────
def test_selfwarm_does_not_start_outside_railway(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    assert G._start_selfwarm_thread() is False
    assert G._SELFWARM["started"] is False, "a test run must never grow this thread"


def test_selfwarm_kill_switch(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("GLOBAL_INFRA_PREWARM_DISABLE", "1")
    assert G._start_selfwarm_thread() is False
    assert G._SELFWARM["started"] is False


def test_selfwarm_starts_once(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.delenv("GLOBAL_INFRA_PREWARM_DISABLE", raising=False)
    started = []
    monkeypatch.setattr(G.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: started.append(1)})())
    assert G._start_selfwarm_thread() is True
    assert G._start_selfwarm_thread() is False, "a second call must not spawn a second loop"
    assert len(started) == 1


def test_warm_interval_stays_inside_the_ttl():
    assert G._SELFWARM_INTERVAL < G._TTL
    assert G._TTL < G._STALE_MAX


# ── the warm set matches what the routes actually serve ───────────────
def test_every_warmable_key_is_a_served_route_builder():
    """A warm that populated a key no route reads would look green and do
    nothing — the same shape as the wrong-table class."""
    import inspect
    src = inspect.getsource(G)
    assert set(G._WARMABLE) == {"gdacs", "gem", "wri", "ixps"}
    for key in G._WARMABLE:
        assert f'_cached("{key}"' in src, f"_WARMABLE key {key!r} is not served by any route"
