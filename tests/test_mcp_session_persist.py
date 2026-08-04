"""The 61% attribution bug: session persisted from the wrong header (2026-08-03).

House rule: tests NEVER import main. These read main.py as text, which is the
established pattern for asserting on shipped code that opens DB pools and
registers ~200 blueprints at import.

Phase NN (2026-05-14) built session -> platform recovery: the MCP `initialize`
handshake is the only place clientInfo.name arrives, so it is cached and
persisted to mcp_sessions, and /api/v1/mcp/track later recovers attribution by
joining on session_id.

It persisted only `if session_id:`, where session_id came from the
Mcp-Session-Id REQUEST header. Per the MCP Streamable HTTP spec the SERVER
assigns that id in the initialize RESPONSE — a client cannot send it on
initialize by definition. The guard was therefore false at exactly the moment
the client's name had just been resolved.

Consequence, measured 2026-08-03: mcp_sessions did not exist at all, and 9,220
calls (61% of 30d traffic, 207 agents) carried the generic `mcp` tag.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _main() -> str:
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        return fh.read()


def test_session_is_persisted_from_the_response_header():
    """★THE FIX. The id exists only on the upstream RESPONSE at initialize
    time; both proxy branches already forward it to the client, so it is
    available to persist."""
    src = _main()
    i = src.index("THE 61% BUG")
    window = src[i:i + 2200]
    assert "resp.headers.get('Mcp-Session-Id', '')" in window
    assert "_persist_mcp_session(_sid, platform, client_name)" in window


def test_it_only_fires_on_initialize():
    """Persisting on any other method would key the map on whatever platform
    the fallback happened to sniff, poisoning the very table it repairs."""
    src = _main()
    i = src.index("THE 61% BUG")
    window = src[i:i + 2200]
    assert "if rpc_method == 'initialize':" in window


def test_the_in_memory_cache_is_seeded_too():
    """It sat behind the SAME broken guard, so non-initialize calls looked up
    an always-empty dict and fell through to User-Agent sniffing."""
    src = _main()
    i = src.index("THE 61% BUG")
    window = src[i:i + 2200]
    assert "_mcp_session_platforms[_sid]" in window


def test_the_request_header_remains_a_fallback():
    """A client that does send an id on initialize must still be honoured —
    the fix adds a source, it does not replace one."""
    src = _main()
    i = src.index("THE 61% BUG")
    window = src[i:i + 2200]
    assert "or session_id" in window


def test_the_old_request_only_guard_is_still_present_for_compatibility():
    """The original initialize-block persist stays: it is harmless (its guard
    is simply false for spec-compliant clients) and removing it would change
    behaviour for any client that DOES send the header, which is not what this
    fix is about."""
    src = _main()
    assert src.count("_persist_mcp_session(") >= 3, (
        "expected the def, the original call site, and the response-header "
        "call site")


def test_the_comment_records_why_it_looked_half_working():
    """★The trap that hid this for three months: the OTHER half of Phase NN
    (UUID rejection + UA mapping) DID land and took attribution 98.8% -> 61%,
    so the aggregate number looked like a partially-working join rather than a
    working mapper next to a join that had never run."""
    src = _main()
    i = src.index("THE 61% BUG")
    window = src[i:i + 2200]
    assert "98.8%" in window and "never run at all" in window
