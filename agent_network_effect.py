"""
DC Hub — Agent Network Effect Module
=====================================
Adds /api/agents/registry and /api/agents/intelligence-index endpoints.
These power the AI Platform page's Network tab and Intelligence Index widget.

Usage in main.py:
    from agent_network_effect import register_agent_network
    register_agent_network(app)
"""

import os
from datetime import datetime, timezone
from flask import jsonify, request, make_response

# Buckets that are NOT external AI platforms — our own infra, probes,
# health-checkers, scrapers, and generic transport placeholders. Mirrors
# flask_mcp_endpoints._LIVE_PROOF_NONPLATFORM so the registry counts only
# real external agents (never inflated by our own traffic).
_NONPLATFORM = (
    "", "mcp", "mcp-worker", "mcp_generic", "mcp-generic", "unknown",
    "unknown-ua", "anonymous", "internal", "internal-dchub", "direct",
    "node-script", "python-script", "curl", "postman", "probe",
    "health", "scanner", "checker",
)

# Pretty display names for known external platforms. Anything not listed
# falls back to a title-cased version of the raw platform string — we never
# invent a vendor that isn't actually in the data.
_PLATFORM_DISPLAY = {
    "claude": "Claude", "chatgpt": "ChatGPT", "openai": "ChatGPT",
    "gemini": "Gemini", "copilot": "Copilot", "perplexity": "Perplexity",
    "grok": "Grok", "deepseek": "DeepSeek", "cursor": "Cursor",
    "cline": "Cline", "windsurf": "Windsurf", "meta": "Meta AI",
    "mistral": "Mistral", "cohere": "Cohere",
}

_UUID_RE_STR = (r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}$")

# DC Hub's OWN internal traffic must never count as an external "agent" — the
# registry was showing dchub-selfheal / pipeline_mcp as the top agents, i.e.
# self-traffic inflating adoption (the signal-inflation rule).
# Substring markers of DC Hub's own / test / probe traffic — matched ANYWHERE in
# the platform string. The inflators carried the marker mid-string ("Local-Agent-
# Mode-dchubVIAmcp-Remote0137", "R51-LOOP-Check", "Step2_TEST"), so prefix-only
# missed them. Real external AI platforms (claude/chatgpt/gemini/...) contain none.
_INTERNAL_MARKERS = (
    "dchub", "selfheal", "self-heal", "self_heal", "pipeline", "probe",
    "scanner", "checker", "monitor", "health", "heartbeat", "loop",
    "local-agent", "localhost", "127.0.0.1", "smoke", "warmup", "sentinel",
    "remote0", "remote1", "step2", "step3", "_test", "-test", "test-", "test_",
)
def _is_internal_caller(p):
    if not p:
        return True
    p = p.lower()
    if p in _NONPLATFORM or len(p) < 3:   # set members + single/double-char noise
        return True
    return any(m in p for m in _INTERNAL_MARKERS)


# Allowlist of recognized external AI platforms (substring match). The registry
# + agent count reflect ONLY these — a denylist can't keep up with the long tail
# of audit/test/probe one-offs (Scraper-Block-Verify, Leakaudit4, ...).
# Conservative by design: a brand-new platform not yet listed is undercounted,
# never noise-inflated. Extend as real platforms appear (cf. partner task).
_KNOWN_AI_TOKENS = (
    "claude", "anthropic", "chatgpt", "openai", "gpt", "gemini", "bard",
    "copilot", "perplexity", "grok", "deepseek", "cursor", "cline",
    "windsurf", "mistral", "cohere", "llama", "meta", "nvidia", "groq",
    "huggingface", "phind", "you.com", "poe", "replit",
)
def _is_recognized_platform(p):
    if not p:
        return False
    p = p.lower()
    return any(tok in p for tok in _KNOWN_AI_TOKENS)


def _live_agent_rows():
    """Real per-platform tool-call counts from mcp_tool_calls (last 30d).

    Returns (rows, available). `rows` is a list of dicts derived ENTIRELY
    from a live DB read; `available` is False when the DB/table is
    unavailable or there is no external-platform data — in which case the
    caller must NOT fabricate counts.

    LIVE-SCHEMA TRUTH (verified via information_schema 2026-06-02):
        mcp_tool_calls(platform TEXT, created_at TIMESTAMP, ...)
    """
    import re as _re
    _uuid = _re.compile(_UUID_RE_STR)
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return [], False
    try:
        try:
            import psycopg as _pg
            conn = _pg.connect(url, autocommit=True)
        except ImportError:
            import psycopg2 as _pg  # type: ignore
            conn = _pg.connect(url)
            conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT LOWER(COALESCE(platform,'')) AS p, "
                "       COUNT(*) AS n, "
                "       MAX(created_at) AS last_seen "
                "FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days' "
                "GROUP BY p ORDER BY n DESC"
            )
            raw = cur.fetchall()
            cur.close()
        finally:
            conn.close()
    except Exception:
        return [], False

    rows = []
    for p, n, last_seen in (raw or []):
        # Allowlist: recognized AI platform AND not our own internal traffic.
        if not _is_recognized_platform(p) or _is_internal_caller(p):
            continue
        _n = int(n or 0)
        rows.append({
            "agent": _PLATFORM_DISPLAY.get(p, p.title() if p else "Unknown"),
            "platform_key": p,
            "integration": "MCP (Streamable HTTP)",
            "status": "active",
            "tool_calls_30d": _n,
            # Backward-compat alias for existing consumers (ai.html renders
            # `total_queries`). This is the SAME real number — not a separate
            # invented metric — so older UIs show live counts, not 0.
            "total_queries": _n,
            "last_active": (last_seen.isoformat()
                            if hasattr(last_seen, "isoformat") else None),
        })
    return rows, True


def _cors_json(data, status=200):
    """Return JSON with CORS headers."""
    resp = make_response(jsonify(data), status)
    origin = request.headers.get('Origin', '')
    allowed = ['https://dchub.cloud', 'https://www.dchub.cloud', 'http://localhost:3000']
    if origin in allowed:
        resp.headers['Access-Control-Allow-Origin'] = origin
    else:
        resp.headers['Access-Control-Allow-Origin'] = 'https://dchub.cloud'
    resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key, Accept'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


def register_agent_network(app):
    """Register agent network routes. Call once from main.py."""

    @app.route('/api/agents/registry', methods=['GET', 'OPTIONS'])
    def agent_registry():
        # Master-shell 2 (2026-06-02): DE-HARDCODED. This used to return
        # invented per-platform query counts (Claude=28500, ChatGPT=24200,
        # ...). Those were never real. It now reads live per-platform
        # tool-call counts from mcp_tool_calls (same honest source as
        # /api/v1/stats/live-proof). If live data is unavailable it returns
        # an EMPTY registry with data_available=false — it never falls back
        # to the old fake numbers.
        if request.method == 'OPTIONS':
            return _cors_json({})
        now = datetime.now(timezone.utc)
        rows, available = _live_agent_rows()
        # Honest sort: most-active first.
        rows.sort(key=lambda r: r.get("tool_calls_30d", 0), reverse=True)
        return _cors_json({
            "dc_hub_agent_registry": {
                "registry_version": "3.0-live",
                "updated_at": now.isoformat(),
                "data_available": available,
                "window": "30d",
                # Real distinct external-platform count (0 if no data).
                "total_connected_agents": len(rows),
                "agents": rows,
                "source": ("Live: COUNT(*) + MAX(created_at) per platform from "
                           "mcp_tool_calls (30d), external buckets only."),
                "note": (None if available else
                         "Live usage data unavailable right now; counts are "
                         "withheld rather than estimated."),
            }
        })

    # Phase RRR-shadow-cleanup (2026-05-18): removed agent_intelligence_index
    # mock that returned random.uniform() / random.randint() for ALL metrics.
    # main.py:18613 api_agents_intelligence_index queries the real DB
    # (facilities, pipeline, gdci_scores). Brain's check_shadowed_routes
    # flagged the dup. Removing the mock lets the real handler serve —
    # users now get actual numbers instead of randomized fake data.

    print("   🤖 Agent Network Effect: ✅ Registry registered (intelligence-index now served by main.py with real data)")
