"""growth_master_shell published a loose IP count as "agents" (2026-08-26).

`real_agents = unique_ips_7d_real OR distinct_agents_7d` is the pre-fix
expression audience_master_shell.py:210 replaced on 2026-07-31. Measured live
2026-08-26T21:28Z the funnel served BOTH keys — `real_external_agents_7d: 16`
(canonical identity count, same value /api/v1/stats/live-proof publishes) and
`unique_ips_7d_real: 33` (raw DISTINCT ip_address, whose own basis string says
"never render it as 'agents'"). The shell took the 33, so growth_score and
weakest_lever were computed on a 2.1x-inflated number.

These fixtures use the real 16/33/17 values from that reading.
"""
import pytest
from routes import growth_master_shell as gs

_FUNNEL_BOTH = {"real_external_agents_7d": 16, "unique_ips_7d_real": 33,
                "tool_calls_7d_real": 1900, "tool_calls_7d_probes": 2380,
                "conversions_30d": 6}
_REACH_BOTH = {"real_agents_7d": 16, "distinct_agents_7d": 17}


def _patch(monkeypatch, funnel, reach):
    """Serve canned payloads for the 7 endpoints tier1_measure() reads."""
    def fake_req(path, method="GET", timeout=10):
        if path.startswith("/api/v1/mcp/funnel"):
            return {"ok": True, "data": funnel}
        if path.startswith("/api/v1/ai/reach/trend"):
            return {"ok": True, "data": {}}
        if path.startswith("/api/v1/ai/reach"):
            return {"ok": True, "data": reach}
        # publisher-status carries a post so the DB fallback branch stays out.
        if path.startswith("/api/v1/dchub-media/publisher-status"):
            return {"ok": True, "data": {"loops": {"li": {"successes_24h": 1,
                                                          "attempts_24h": 1}}}}
        return {"ok": True, "data": {}}
    monkeypatch.setattr(gs, "_req", fake_req)
    monkeypatch.setattr(gs, "_conn", lambda: None)
    return gs.tier1_measure()


def test_canonical_identity_count_wins_over_loose_ip_count(monkeypatch):
    """THE REGRESSION: both keys present -> must publish 16, never 33."""
    m = _patch(monkeypatch, _FUNNEL_BOTH, _REACH_BOTH)
    assert m["real_agents_7d"] == 16, (
        f"published {m['real_agents_7d']} — the loose IP count (33) or the ISO "
        "rollup (17) leaked into the agents headline")
    assert m["real_agents_basis"].startswith("canonical")


def test_loose_counter_is_still_carried_for_series_comparability(monkeypatch):
    """Pre-2026-08-26 rows hold the loose number; keep it readable."""
    m = _patch(monkeypatch, _FUNNEL_BOTH, _REACH_BOTH)
    assert m["real_agents_7d_loose"] == 33


def test_reach_canonical_beats_iso_week_rollup(monkeypatch):
    """Funnel canonical absent: prefer reach.real_agents_7d over the rollup,
    which runs ~33% low mid-week."""
    f = {k: v for k, v in _FUNNEL_BOTH.items() if k != "real_external_agents_7d"}
    m = _patch(monkeypatch, f, _REACH_BOTH)
    assert m["real_agents_7d"] == 16
    assert "reach.real_agents_7d" in m["real_agents_basis"]


def test_degrades_to_loose_but_SAYS_SO(monkeypatch):
    """A cold funnel must still yield a number — and label it as loose, so a
    reader never mistakes it for the canonical count."""
    m = _patch(monkeypatch, {"unique_ips_7d_real": 33}, {})
    assert m["real_agents_7d"] == 33
    assert "LOOSE" in m["real_agents_basis"]


def test_no_counters_at_all_is_None_not_zero(monkeypatch):
    """Absent != zero agents — a cold read must not publish a fake 0."""
    m = _patch(monkeypatch, {}, {})
    assert m["real_agents_7d"] is None
