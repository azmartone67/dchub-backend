"""The RAG shell's zero-reach signal had no reader (2026-08-26).

`_reach_flag()` fires whenever rag_agents_7d < _REACH_TARGET and says, correctly,
that adoption is a GEO/distribution problem and "never a shell action". It was
returned ONLY in the master-tick response body. The tick's sole caller is cron
`rag_master_tick_daily`, whose _hit() does `body = resp.read(512)` and returns
{"status", "bytes"} — the body is discarded. It was never persisted, so
/api/v1/admin/rag/master-state never carried it (verified live: 0 occurrences of
"reach_flag" in the 67KB response while rag_agents_7d was 0).

So: whatever _persist writes into `detail` is the ONLY durable channel.
"""
import json
import pytest
from routes import rag_master_shell as rms


class _Cur:
    def __init__(self, sink): self.sink = sink
    def execute(self, sql, params=None): self.sink.append((sql, params))
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, sink): self.sink = sink
    def cursor(self, *a, **k): return _Cur(self.sink)
    def close(self): pass


def _persist_detail(monkeypatch, measure):
    """Run _persist and return the `detail` JSONB it would write."""
    sink = []
    monkeypatch.setattr(rms, "_ensure_tables", lambda: True)
    monkeypatch.setattr(rms, "_conn", lambda: _Conn(sink))
    levers = {"scores": {"reach": 0.0}, "weakest": "retrieval"}
    assert rms._persist(measure, levers, 51.57, {"action": "none"}) is True
    assert sink, "no INSERT was issued"
    return json.loads(sink[-1][1][-1])


def test_zero_reach_flag_reaches_the_snapshot(monkeypatch):
    """THE REGRESSION: 0 agents must leave a durable trace in `detail`."""
    d = _persist_detail(monkeypatch, {"rag_agents_7d": 0,
                                      "rag_context_packs_7d": 0})
    assert d.get("reach_flag"), (
        "reach_flag absent from the persisted snapshot — /master-state will "
        "show a 0-agent module with no signal saying so")
    assert "0 external agents" in d["reach_flag"]


def test_north_star_carries_the_target_not_just_the_value(monkeypatch):
    """A bare 0 is unreadable without the target it is 0 against."""
    d = _persist_detail(monkeypatch, {"rag_agents_7d": 0,
                                      "rag_context_packs_7d": 0})
    assert d["north_star"]["target_agents_wk"] == rms._REACH_TARGET
    assert d["north_star"]["rag_agents_7d"] == 0


def test_flag_is_absent_once_reach_clears_the_target(monkeypatch):
    """Not a constant: at/above target there is nothing to flag. This is the
    CHEAT guard — a hardcoded string would pass the tests above and fail here."""
    d = _persist_detail(monkeypatch, {"rag_agents_7d": rms._REACH_TARGET,
                                      "rag_context_packs_7d": 5})
    assert d.get("reach_flag") is None


def test_measure_levers_action_still_persisted(monkeypatch):
    """Do not trade the existing payload for the new keys."""
    d = _persist_detail(monkeypatch, {"rag_agents_7d": 0,
                                      "rag_context_packs_7d": 0})
    assert set(("measure", "levers", "action")) <= set(d)
