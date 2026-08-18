"""The /ai platform roster must publish BOTH channels.

★ 2026-08-03. The roster read ai_cumulative only — UA-attributed HTTP hits,
i.e. crawler and citation traffic. MCP tool calls arrive through the gateway
and never touch that table, so a platform that integrates properly reads
near-zero. Smithery: 6 lifetime on the card against 2,529 MCP calls in 30 days.

Pure-function level with a monkeypatched query, because DB tests skip in CI —
a suite that only exercised the SQL would be green-by-absence on the branch
that matters.
"""
import ai_tracking
from ai_tracking import (AI_PLATFORMS, get_mcp_calls_by_roster_platform,
                         get_mcp_calls_by_roster_platform_envelope)


def _run(monkeypatch, rows):
    """rows are (platform, n) as the mapping tests write them; the real query
    returns (platform, n_all, n_ext) since r-selftraffic-roster. These tests
    are about MAPPING, so they declare no self-traffic: n_ext == n_all."""
    monkeypatch.setattr(ai_tracking, "_execute",
                        lambda *a, **k: [_row3(r) for r in rows])
    return get_mcp_calls_by_roster_platform(30)


def _row3(r):
    if isinstance(r, dict):
        return {"platform": r["platform"], "n_all": r["n"], "n_ext": r["n"]}
    return (r[0], r[1], r[1])


def test_smithery_connect_maps_to_the_roster_key(monkeypatch):
    """The case that proved the bug. 'smithery connect' is not a vendor the
    canon knows, so it must fall through to the roster's own agent markers."""
    assert _run(monkeypatch, [("smithery connect", 2529)]) == {"smithery": 2529}


def test_vendor_variants_collapse_onto_one_key(monkeypatch):
    out = _run(monkeypatch, [
        ("anthropic/api", 461), ("anthropic/claudeai", 71),
        ("claude", 54), ("claude-code", 50),
    ])
    assert out == {"claude": 636}


def test_openai_keyspace_bridge(monkeypatch):
    """The canon says 'openai'; this roster says 'chatgpt'. The one
    disagreement the bridge exists for — exercised with a bucket the canon
    actually recognizes as OpenAI."""
    assert _run(monkeypatch, [("openai/assistant", 36)]) == {"chatgpt": 36}
    assert _run(monkeypatch, [("chatgpt", 4)]) == {"chatgpt": 4}


def test_known_gap_codex_mcp_client_is_dropped_not_guessed(monkeypatch):
    """★ HONEST GAP, recorded rather than papered over.

    'codex-mcp-client' (36 calls / 1 IP live on 2026-08-03) is OpenAI's Codex
    CLI, so it BELONGS to chatgpt — but neither existing vocabulary says so:
    ai_platform_canon has no 'codex' token, and the roster's chatgpt markers
    are ChatGPT-User / GPTBot / OAI-SearchBot, none of which substring-match
    it. So it is DROPPED.

    Dropping is the correct behaviour for this module — inventing an
    attribution from a hunch is how a dashboard starts lying. The fix belongs
    in ai_platform_canon._VENDOR_ALIASES ('codex' -> 'openai'), which is the
    single source both this and count_platforms() read; it is deliberately NOT
    done here because that module's output feeds published platform COUNTS and
    deserves its own change. When it lands, this test flips to asserting
    {'chatgpt': 36} and that is the signal it worked."""
    assert _run(monkeypatch, [("codex-mcp-client", 36)]) == {}


def test_generic_buckets_are_dropped_never_invented(monkeypatch):
    """mcp-generic-client is ~11k calls from ~338 IPs. Attributing it to any
    named platform would publish a number we cannot stand behind."""
    out = _run(monkeypatch, [
        ("mcp-generic-client", 11391), ("unknown", 183),
        ("datacolo", 2560), ("internal-dchub", 999),
    ])
    for junk in ("mcp-generic-client", "unknown", "internal-dchub", "datacolo"):
        assert junk not in out
    assert all(k in AI_PLATFORMS for k in out)


def test_every_emitted_key_is_a_real_roster_platform(monkeypatch):
    """An unknown key renders a card with no name, colour or company — and
    silently invents a platform on a public page."""
    out = _run(monkeypatch, [
        ("smithery connect", 1), ("anthropic/api", 1), ("gemini-cli", 1),
        ("mistral", 1), ("cursor", 1), ("grok", 1), ("copilot", 1),
    ])
    assert out
    for k in out:
        assert k in AI_PLATFORMS, f"{k} is not a roster platform"


def test_counts_are_summed_not_overwritten(monkeypatch):
    out = _run(monkeypatch, [("claude", 10), ("claude-code", 5), ("anthropic/api", 1)])
    assert out["claude"] == 16


def test_query_failure_fails_open_to_empty(monkeypatch):
    """A roster missing its MCP column is worse than yesterday's; a roster
    that 500s is worse than both."""
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ai_tracking, "_execute", boom)
    assert get_mcp_calls_by_roster_platform(30) == {}


def test_dict_rows_are_supported(monkeypatch):
    """_execute yields dict rows on some cursors and tuples on others."""
    assert _run(monkeypatch, [{"platform": "smithery connect", "n": 7}]) == {"smithery": 7}


def test_no_third_vocabulary_was_introduced():
    """The mapping must reuse canonical_platform + the roster's own markers.
    A hand-written alias table here would drift from both."""
    import inspect
    src = inspect.getsource(get_mcp_calls_by_roster_platform_envelope)
    assert "canonical_platform" in src
    assert "AI_PLATFORMS.items()" in src
    # the ONLY hand-written mapping allowed is the documented key-space bridge
    assert src.count("_VENDOR_TO_ROSTER") >= 1
    assert '"openai": "chatgpt"' in src or "'openai': 'chatgpt'" in src


def test_sql_uses_the_canonical_identity_basis():
    import inspect
    src = inspect.getsource(get_mcp_calls_by_roster_platform_envelope)
    assert "mcp_calls_identity" in src
    assert "is_public_ip" in src and "is_real_external" in src
    # ★ r-selftraffic-roster (2026-08-18): this line was `"session_id" not in
    # src`. Its intent was "no ad-hoc identity basis invented here", not "never
    # key on a session" — so it is REPLACED, not deleted. The session exclusion
    # is permitted only through the shared vocabulary; a hand-rolled prefix
    # literal in this module would be the second maintained list that
    # mcp_calls_deloop exists to prevent.
    assert "external_session_predicate" in src
    assert "88e20dac" not in src, (
        "the operator session prefix must come from "
        "mcp_calls_deloop.self_traffic_session_prefixes, never be re-declared here")


def test_payload_attaches_the_channel_and_admits_mcp_only_platforms():
    """The builder must attach the field (or the page has nothing to render)
    and union the key sets (or an MCP-only platform stays invisible)."""
    src = open("ai_tracking.py").read()
    assert '"mcp_calls_30d": int(_mcp_calls.get(p, 0))' in src
    assert "_cum_keys" in src and "_extra" in src
