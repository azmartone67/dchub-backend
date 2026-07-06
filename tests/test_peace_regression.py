"""Regression guards for the 2026-07-05 "peace" work — pin the fixes so a later
autopilot edit can't silently revert them. Pure in-process asserts (no DB /
network). See memory: reference_dchub_selftraffic_peace + reference_dchub_coverage_master_shell.
"""
import importlib
import time


def test_radar_silences_transient_self_edge_errors():
    """brain-radar must NOT log transient self-edge timeouts (they're load, not
    drift) — the fix that stopped the log flood."""
    m = importlib.import_module("routes.brain_consistency_radar")
    assert m._radar_transient("TimeoutError: The read operation timed out")
    assert m._radar_transient("URLError: <urlopen error timed out>")
    assert m._radar_transient("Connection reset by peer")
    # a genuine drift/health error must STILL surface (not silenced)
    assert not m._radar_transient("HTTP 500 Internal Server Error")


def test_radar_tier_probe_is_cached():
    """The tier-drift probe must stay result-cached so it doesn't round-trip CF
    on every scan."""
    m = importlib.import_module("routes.brain_consistency_radar")
    assert hasattr(m, "_TIER_CACHE")
    assert isinstance(m._TIER_TTL_S, int) and m._TIER_TTL_S >= 300  # >= 5 min


def test_entsog_snapshot_is_stale_while_revalidate():
    """A cached EU-gas snapshot must return WITHOUT an inline rebuild — the fix
    that killed the 33s cache-miss."""
    m = importlib.import_module("routes.eu_gas_entsog")
    assert hasattr(m, "_rebuild_snapshot") and hasattr(m, "_bg_refresh")
    assert hasattr(m, "_SNAP_LOCK")
    m._SNAP_CACHE["data"] = {"countries": {}, "_sentinel": True}
    m._SNAP_CACHE["ts"] = time.time()
    called = {"n": 0}
    orig = m._rebuild_snapshot
    m._rebuild_snapshot = lambda: called.__setitem__("n", called["n"] + 1) or {}
    try:
        out = m._live_snapshot()
        assert out.get("_sentinel") is True
        assert called["n"] == 0, "a fresh cache must not trigger an inline rebuild"
    finally:
        m._rebuild_snapshot = orig
        m._SNAP_CACHE["data"] = None
        m._SNAP_CACHE["ts"] = 0.0


def test_coverage_feed_provenance_map():
    """Public layers stay labeled provenance='public' (unify, not discover);
    DC Hub's own layers stay 'curated'."""
    m = importlib.import_module("routes.infra_growth")
    assert m._PROVENANCE["substations"][0] == "public"
    assert m._PROVENANCE["fcc_fiber_hexes"] == ("public", "FCC")
    assert m._PROVENANCE["data_centers"][0] == "curated"


def test_coverage_messaging_banned_framing_scan():
    """The banned-framing scan must keep catching '21.9K verified' /
    'discovered middle-mile' and requiring the honesty tokens."""
    m = importlib.import_module("routes.coverage_master_shell")
    bad = {"x": "21,882 verified data centers; we discovered middle-mile coverage"}
    assert m._messaging_violations(bad), "banned framing must be caught"
    good = {"x": "4,856 verified data centers within a 21,882 tracked frontier, "
                 "open under CC-BY-4.0"}
    assert not m._messaging_violations(good), f"clean draft flagged: {m._messaging_violations(good)}"
