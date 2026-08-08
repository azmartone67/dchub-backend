"""The flywheel served 502s through the edge for a cache-policy reason.

Measured 2026-08-08: edge HTTP 502 in 10.2s while the Railway origin returned
200 in 9.8s. Not an outage — a ~10s computation behind a 30s cache warmed by a
once-daily cron, so nearly every visitor paid the full tick and the Cloudflare
worker gave up first.
"""
import time

from routes import flywheel_master_shell as fw


def test_ttl_is_longer_than_the_tick_it_caches():
    # A cache shorter than the work it caches cannot ever serve a warm hit.
    # The tick measured ~10s live; 600s leaves real headroom.
    assert fw._TICK_TTL >= 300, "TTL must comfortably exceed the ~10s tick"


def test_fresh_cache_is_served_without_recomputing(monkeypatch):
    calls = []
    monkeypatch.setattr(fw, "_run_tick", lambda: calls.append(1) or {"v": "new"})
    with fw._cache_lock:
        fw._cache["payload"] = {"v": "cached"}
        fw._cache["ts"] = time.time()
    assert fw._tick_cached()["v"] == "cached"
    assert calls == []


def test_stale_cache_is_SERVED_not_recomputed_inline(monkeypatch):
    # ★ THE FIX. Old behaviour: block the caller for ~10s -> edge 502.
    started = []
    monkeypatch.setattr(fw, "_run_tick",
                        lambda: started.append(1) or {"v": "new"})
    monkeypatch.setattr(fw, "_refresh_async", lambda: started.append("bg"))
    with fw._cache_lock:
        fw._cache["payload"] = {"v": "cached"}
        fw._cache["ts"] = time.time() - (fw._TICK_TTL + 5)
    out = fw._tick_cached()
    assert out["v"] == "cached"          # served immediately
    assert out["served_stale"] is True   # and says so
    assert out["stale_age_seconds"] >= fw._TICK_TTL
    assert started == ["bg"]             # refresh happened BEHIND the response


def test_a_stale_serve_does_not_mutate_the_cached_payload(monkeypatch):
    # Kills: stamping served_stale onto the shared dict, so the NEXT reader
    # sees a stale flag on a fresh payload.
    monkeypatch.setattr(fw, "_refresh_async", lambda: None)
    cached = {"v": "cached"}
    with fw._cache_lock:
        fw._cache["payload"] = cached
        fw._cache["ts"] = time.time() - (fw._TICK_TTL + 5)
    fw._tick_cached()
    assert "served_stale" not in cached


def test_cold_cache_still_computes(monkeypatch):
    # Nothing to serve stale — blocking is correct here, and unavoidable once.
    monkeypatch.setattr(fw, "_run_tick", lambda: {"v": "computed"})
    with fw._cache_lock:
        fw._cache["payload"] = None
        fw._cache["ts"] = 0.0
    assert fw._tick_cached()["v"] == "computed"


def test_a_payload_past_STALE_OK_is_recomputed_not_served(monkeypatch):
    # Freshness has a floor: an hour-old board is not worth showing.
    monkeypatch.setattr(fw, "_run_tick", lambda: {"v": "computed"})
    with fw._cache_lock:
        fw._cache["payload"] = {"v": "ancient"}
        fw._cache["ts"] = time.time() - (fw._STALE_OK + 60)
    assert fw._tick_cached()["v"] == "computed"


def test_refresh_is_single_flight(monkeypatch):
    # A burst of visitors must not start N ten-second ticks on the same pool.
    import threading
    ticks = []
    gate = threading.Event()

    def _slow():
        ticks.append(1)
        gate.wait(2.0)          # hold the slot like the real ~10s tick does
        return {"v": "x"}

    monkeypatch.setattr(fw, "_run_tick", _slow)
    fw._refresh_async()
    fw._refresh_async()
    fw._refresh_async()
    time.sleep(0.15)
    assert len(ticks) == 1, "a burst must start exactly ONE tick"
    gate.set()
    for _ in range(60):
        if not fw._refreshing:
            break
        time.sleep(0.05)
