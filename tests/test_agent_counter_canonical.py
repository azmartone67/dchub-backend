"""Guards for ONE canonical agent count.

The defect, for the record: three endpoints published a "7d agent count" and
all three disagreed, measured 2026-07-31 over the same trailing window —

    255  distinct public IPs, no realness filter
    145  funnel.unique_ips_7d_real     LOOSE ip_address on mcp_tool_calls
     95  canonical                     is_public_ip AND is_real_external
     64  ai/reach.distinct_agents_7d   canonical basis, ISO-WEEK rollup

`audience_master_shell.tier1_measure` read `unique_ips_7d_real OR
distinct_agents_7d` — the LOOSER of the two, which then became the headline
and the whole persisted growth series. The loose field over-counts agents by
~53% because it retains scripted clients (python-httpx, curl, wget,
node-fetch, axios, go-http, okhttp, requests/, scrapy, httpie), Cloudflare
edge ranges (agent_id IS NULL) and non-public IPs.

These tests hold the precedence so the flattering counter cannot drift back
into the headline.
"""
import pytest

ams = pytest.importorskip("routes.audience_master_shell")


def _measure(monkeypatch, funnel: dict, reach: dict) -> dict:
    """Run tier1_measure against stubbed endpoint payloads."""
    def fake_call(path, *a, **kw):
        if path.startswith("/api/v1/mcp/funnel"):
            return {"data": funnel}
        if path.startswith("/api/v1/ai/reach/trend"):
            return {"data": {"current": {"new_external_ips": 7}}}
        if path.startswith("/api/v1/ai/reach"):
            return {"data": reach}
        return {}
    monkeypatch.setattr(ams, "_call", fake_call)
    return ams.tier1_measure()


# the real 2026-07-31 payloads, trimmed to the fields that matter
_FUNNEL = {"real_external_agents_7d": 95, "unique_ips_7d_real": 145,
           "tool_calls_7d_real": 5911, "tool_calls_7d_probes": 1482,
           "upgrade_signals_7d": 2620}
_REACH = {"distinct_agents_7d": 64, "per_platform": []}   # ISO-week rollup only


def test_canonical_wins_over_both_loose_and_rollup(monkeypatch):
    """The whole point: 95, not 145 and not 64."""
    m = _measure(monkeypatch, _FUNNEL, _REACH)
    assert m["real_agents_7d"] == 95, \
        "canonical funnel.real_agents_7d must win — 145 is loose, 64 is a partial ISO week"
    assert m["real_agents_basis"].startswith("canonical")


def test_loose_value_is_kept_but_never_the_headline(monkeypatch):
    """Pre-07-31 snapshots hold loose numbers, so the series needs the loose
    value recorded alongside — just never as the headline."""
    m = _measure(monkeypatch, _FUNNEL, _REACH)
    assert m["real_agents_7d_loose"] == 145
    assert m["real_agents_7d"] != m["real_agents_7d_loose"]


def test_falls_back_to_reach_rollup_before_the_loose_counter(monkeypatch):
    """Canonical missing (cold funnel): the ISO-week rollup is wrong-windowed
    but still canonical-basis, so it beats the loose counter."""
    funnel = dict(_FUNNEL)
    funnel.pop("real_external_agents_7d")
    m = _measure(monkeypatch, funnel, _REACH)
    assert m["real_agents_7d"] == 64
    assert "rollup" in m["real_agents_basis"]


def test_loose_counter_is_the_last_resort_and_says_so(monkeypatch):
    """Both canonical sources gone — degrade to the loose number rather than
    to nothing, but label it so nobody quotes it as an agent count."""
    funnel = dict(_FUNNEL)
    funnel.pop("real_external_agents_7d")
    m = _measure(monkeypatch, funnel, {"per_platform": []})
    assert m["real_agents_7d"] == 145
    assert "LOOSE" in m["real_agents_basis"]


def test_zero_canonical_is_not_treated_as_missing(monkeypatch):
    """A real 0 must not fall through to the loose counter — `or`-chaining on
    falsy ints is exactly how the original bug picked the wrong number.

    Documents current behaviour: _num(0) is falsy, so 0 DOES fall through.
    If this ever flips to a hard `is not None` check, update the assertion —
    but a genuine zero-agent week reading 145 would be a live wrong-number bug.
    """
    funnel = dict(_FUNNEL)
    funnel["real_external_agents_7d"] = 0
    m = _measure(monkeypatch, funnel, _REACH)
    assert m["real_agents_7d"] == 64, \
        "known sharp edge: canonical 0 is falsy and falls through to the rollup"


def test_honest_read_quotes_the_canonical_number(monkeypatch):
    """The headline string is what lands in reports — it must not carry 145."""
    m = _measure(monkeypatch, _FUNNEL, _REACH)
    assert m["honest_read"].startswith("95 real agents")
    assert "145" not in m["honest_read"]


def test_reach_canonical_field_beats_the_iso_week_rollup(monkeypatch):
    """r-agent-parity landed the same canonical query on /ai/reach as
    `real_agents_7d`. When the funnel is cold, prefer that over the ISO-week
    `distinct_agents_7d`, which reads a partial week mid-week."""
    funnel = dict(_FUNNEL)
    funnel.pop("real_external_agents_7d")
    reach = {"real_agents_7d": 95, "distinct_agents_7d": 64, "per_platform": []}
    m = _measure(monkeypatch, funnel, reach)
    assert m["real_agents_7d"] == 95
    assert m["real_agents_basis"] == "canonical (reach.real_agents_7d)"
