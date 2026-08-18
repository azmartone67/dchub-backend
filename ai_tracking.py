"""
DC Hub - AI Platform Tracking (Neon PostgreSQL Native)
======================================================
Drop-in replacement for the SQLite-based ai_tracking.py.
Writes directly to Neon PostgreSQL as primary store.
Falls back to local SQLite buffer if Neon is unreachable,
with a background sync thread to flush buffered rows to Neon.

Works identically on Railway (primary) and Replit (failover).

Usage in main.py:
    from ai_tracking import init_ai_tracking, log_mcp_connection, extract_tool_name
    init_ai_tracking(app)

Requires: DATABASE_URL env var → Neon PostgreSQL connection string

Changelog (2026-04-18):
- `mcp_connections` schema expanded to capture tool_name, status_code,
  response_ms, params, client_name, client_version, protocol_version,
  success, error_message. These are the columns needed to measure
  verified tool-call volume per-tool (blocker for description audit).
- Idempotent ALTER TABLE migrations added so live tables pick up new
  columns without downtime.
- `log_mcp_connection()` signature extended (backward-compatible).
- New helper `extract_tool_name(body)` pulls tool name from JSON-RPC body.
- New query helpers: `get_mcp_method_breakdown`, `get_verified_call_totals`.
- SQLite fallback buffer fixed: correct `?` placeholders, correct
  `INTEGER PRIMARY KEY AUTOINCREMENT`, idempotent column migrations,
  proper row factory so `row["x"]` works.
"""

import os
import re
import json
import time
import sqlite3
import threading
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, make_response

logger = logging.getLogger("ai_tracking")

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get("NEON_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
SQLITE_BUFFER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ai_tracking_buffer.db"
)
SYNC_INTERVAL_SECONDS = 60  # flush SQLite buffer to Neon every 60s

# r-failover-guard (2026-07-14): on the Render failover STANDBY (pointed at a
# READ-ONLY Neon replica) these analytics INSERTs raise psycopg2 25006 ("cannot
# execute INSERT in a read-only transaction") and spam the log every 60s. Skip the
# writer on the standby. Env markers mirror main.py:82's IS_FAILOVER, computed
# locally to avoid a main<->module circular import. Primary (Railway) = unchanged.
_IS_FAILOVER = (
    os.environ.get("RENDER", "").lower() in ("true", "1", "yes")
    or bool(os.environ.get("RENDER_SERVICE_ID"))
    or os.environ.get("DCHUB_FAILOVER", "").lower() in ("true", "1", "yes")
)
GATEWAY_VERSION = "4.2.0"

# Declared shape of mcp_connections. Used by check_mcp_schema() at startup
# to detect drift between this file's assumptions and the live table.
# If any column here is missing on the live table, `log_mcp_connection`
# INSERTs will silently fail — which is the exact bug that masked zero
# verified tool calls for weeks before v4.1.
EXPECTED_MCP_CONNECTIONS_COLUMNS = {
    "id", "platform", "client_name", "client_version", "protocol_version",
    "method", "user_agent", "ip_address", "success", "error_message",
    "tool_name", "params", "status_code", "response_ms", "created_at",
}

# ═══════════════════════════════════════════════════════════════
#  PLATFORM DETECTION
# ═══════════════════════════════════════════════════════════════

AI_PLATFORMS = {
    "chatgpt":    {"name": "ChatGPT",    "color": "#10a37f", "company": "OpenAI",     "agents": ["ChatGPT-User", "GPTBot", "OAI-SearchBot"]},
    "claude":     {"name": "Claude",     "color": "#7c3aed", "company": "Anthropic",  "agents": ["Claude-Web", "Anthropic", "claude"]},
    "gemini":     {"name": "Gemini",     "color": "#4285f4", "company": "Google",     "agents": ["Googlebot", "Google-Extended", "GoogleOther", "Gemini"]},
    "perplexity": {"name": "Perplexity", "color": "#1fb8cd", "company": "Perplexity", "agents": ["PerplexityBot"]},
    "copilot":    {"name": "Copilot",    "color": "#0078d4", "company": "Microsoft",  "agents": ["Copilot", "BingBot", "bingbot"]},
    "grok":       {"name": "Grok",       "color": "#1d9bf0", "company": "xAI",        "agents": ["Grok", "xAI"]},
    "deepseek":   {"name": "DeepSeek",   "color": "#0066ff", "company": "DeepSeek",   "agents": ["DeepSeek"]},
    "cursor":     {"name": "Cursor",     "color": "#00e5a0", "company": "Cursor",     "agents": ["Cursor"]},
    "windsurf":   {"name": "Windsurf",   "color": "#00d4aa", "company": "Codeium",    "agents": ["Windsurf", "Codeium"]},
    "meta":       {"name": "Meta AI",    "color": "#0668E1", "company": "Meta",       "agents": ["Meta-ExternalAgent", "meta-externalagent", "FacebookBot"]},
    "cohere":     {"name": "Cohere",     "color": "#39594d", "company": "Cohere",     "agents": ["cohere-ai"]},
    "you":        {"name": "You.com",    "color": "#6c5ce7", "company": "You.com",    "agents": ["YouBot"]},
    "smithery":   {"name": "Smithery",   "color": "#f59e0b", "company": "Smithery",   "agents": ["Smithery", "smithery"]},
    # r61 (2026-05-31): close live-classifier gaps for partners the user
    # asked about. groq was already in the backfill _RULES + citations but
    # missing here (so live groq MCP traffic fell to mcp_generic). HF's
    # python client sets `hf_hub/<ver>` in its UA; Mistral's agents send
    # "mistral"/"mistralai".
    "groq":        {"name": "Groq",         "color": "#f55036", "company": "Groq",         "agents": ["Groq", "groq"]},
    "huggingface": {"name": "Hugging Face", "color": "#ff9d00", "company": "Hugging Face", "agents": ["huggingface", "hf_hub", "HuggingFace"]},
    "mistral":     {"name": "Mistral",      "color": "#ff7000", "company": "Mistral AI",   "agents": ["mistralai", "Mistral", "mistral"]},
    # webmcp-lane (2026-07-11): in-page agents executing our WebMCP page tools
    # (js/dchub-webmcp.js, Chrome origin trial). Their fetches carry the
    # BROWSER UA (Chrome), which detect_platform buckets as 'direct' — so
    # classification is request-marker-based (is_webmcp_request below: the
    # src=webmcp param / X-DC-Source: webmcp header the script stamps on every
    # bound call), never UA-based. The 'webmcp' agent marker here only matters
    # if a client self-identifies in its UA; it exists so the /ai dashboard
    # has canonical name/color/company metadata for the bucket.
    "webmcp":      {"name": "WebMCP",       "color": "#f4b400", "company": "In-page agents (Chrome origin trial)", "agents": ["webmcp"]},
    # 2026-07-18: the 2026 agent wave — none of these were classifiable, so
    # their traffic fell to mcp_generic/direct and the /ai roster never showed
    # them (reported: "I don't see new AI agents on the site, like Kimi").
    # Bare 'z.ai' is deliberately NOT a marker — every '*z.ai' domain in a UA
    # URL would substring-match it.
    "kimi":    {"name": "Kimi",    "color": "#1e6fff", "company": "Moonshot AI", "agents": ["Kimi", "kimi", "Moonshot", "moonshot"]},
    "qwen":    {"name": "Qwen",    "color": "#615ced", "company": "Alibaba",    "agents": ["Qwen", "qwen", "Tongyi", "tongyi"]},
    "zai":     {"name": "Z.ai",    "color": "#5f6dff", "company": "Zhipu AI",   "agents": ["Zhipu", "zhipu", "ChatGLM", "chatglm", "glm-", "z-ai"]},
    "minimax": {"name": "MiniMax", "color": "#e9455e", "company": "MiniMax",    "agents": ["MiniMax", "minimax"]},
}

# Endpoints that indicate AI platform activity
AI_ENDPOINT_PATTERNS = [
    r"^/mcp",
    r"^/api/v1/",
    r"^/api/facilities",
    r"^/api/deals",
    r"^/api/news",
    r"^/api/market",
    r"^/api/grid",
    r"^/api/site-score",
    r"^/\.well-known/",
    r"^/llms\.txt",
    r"^/ai-agents\.json",
    r"^/openapi\.json",
    r"^/AGENTS\.md",
]


# r62 (2026-06-01): self/internal traffic was inflating named-platform
# buckets — the brain self-probes ~1/sec and any UA containing a platform
# substring (e.g. a "claude-helper" health bot, or mcp-remote backfilled
# to claude) got counted as that platform. The 449k headline was ~57%
# inflation once these + dead Direct/Mcp buckets are removed. Classify
# these as 'internal' so the AI dashboards (which iterate AI_PLATFORMS)
# never surface them as real agent demand.
_INTERNAL_UA_MARKERS = (
    "dchub", "dc-hub", "dc hub", "x-dc-probe", "dc-probe",
    "dchub-brain", "brain-probe", "self-probe", "prewarm", "pre-warm",
    "healthcheck", "health-check", "uptime", "uptimerobot",
    "-scanner", "-health", "-probe", "loopback", "127.0.0.1", "localhost",
    "railway", "render-health", "cf-worker-internal",
    # r62-qa: broaden so probe/scanner/test/registry-crawler UAs are bucketed
    # to 'internal' at WRITE time (so they never mint a fresh "AI platform" row
    # — the read-time _is_junk_platform predicate de-inflates existing rows).
    "scanner", "prober", "-checker", "-validator", "-inspector", "-enumerator",
    "-corpus", "sandbox", "smoke", "noauth", "no-auth", "tester", "qa-",
    "registry-", "mcp-crawler", "mcp-survey", "mcp-introspect", "mcp-shield",
    "fabrique", "chiark", "yellowmcp", "agentpulse", "mcpscoring", "bluenexus",
    "brainvolt", "epic-ai", "umai-", "rnwy", "rootz", "proof-scan",
    "dataset-pipeline", "intent2mcp", "media_crawler",
)


# r-cumulative-referer (2026-07-31): generic HTTP-lib UAs are split OUT of
# the self/probe list. They stay 'internal' when they carry no other
# evidence — but a generic lib WITH a platform Referer is exactly how a
# HuggingFace Space or Cohere connector presents (python-requests/httpx +
# referer huggingface.co). The old single list classified those 'internal'
# BEFORE the platform loop could look, which is why the live feed (whose
# sibling classifier reads UA+Referer) showed HF/Cohere landing while their
# ai_cumulative cards sat frozen at 1 and 5 forever — two classifiers, two
# truths, on one page. httpx/aiohttp added: they previously fell to 'direct'.
_GENERIC_LIB_UA_MARKERS = (
    "python-requests", "python-httpx", "python-urllib", "aiohttp",
    "go-http-client", "curl/", "wget/", "okhttp", "node-fetch", "axios",
)


def _is_internal_ua(ua_lower: str) -> bool:
    """True for our own self-traffic / infra probes — OUR identity markers.
    These win over everything, including a platform Referer: a probe of ours
    fetching with any referer is still ours (the r62 inflation class).
    Generic HTTP libs are judged separately in detect_platform, where a
    platform Referer may rescue them."""
    return any(m in ua_lower for m in _INTERNAL_UA_MARKERS)


def _is_generic_lib_ua(ua_lower: str) -> bool:
    return any(m in ua_lower for m in _GENERIC_LIB_UA_MARKERS)


def detect_platform(user_agent: str, referer: str = "") -> str:
    """Identify AI platform from User-Agent (+ optional Referer) evidence.

    Phase ZZZZZ-round6-attribution (2026-05-23): the old logic returned
    literal 'mcp' whenever the UA contained 'mcp' — even if a more
    specific platform marker was present. The funnel-by-client
    dashboard showed 2,903 paywall signals / 7d attributed to that
    generic bucket vs 1 each to claude-desktop and verify. With ~99.9%
    of traffic unattributed, A/B testing CTAs by client was blind.

    New behavior:
      1. Check AI_PLATFORMS specific markers first (Claude, ChatGPT,
         Cursor, etc.) — unchanged.
      2. If 'mcp' is in UA, inspect the SDK / transport substring to
         derive a more specific identity (typescript-sdk → 'mcp_sdk_ts',
         python-sdk → 'mcp_sdk_py', inspector → 'mcp_inspector', …).
      3. Only fall back to 'mcp_generic' (not 'mcp') if no specific MCP
         transport is identifiable — preserves the legacy bucket but
         names it honestly so it stops shadowing real platforms.
      4. Bots / crawlers / direct unchanged.
    """
    if not user_agent:
        return "direct"
    ua_lower = user_agent.lower()
    # r62: self-traffic / infra probes first — these were silently
    # inflating the named-platform buckets (brain self-probe ~1/sec).
    # SELF markers beat everything, including a platform Referer: our own
    # probe stays ours no matter what page it claims to come from.
    if _is_internal_ua(ua_lower):
        return "internal"
    # r-cumulative-referer (2026-07-31): the Referer joins the evidence for
    # the NAMED-platform loop only — matching the live feed's sibling
    # classifier so the two stop disagreeing on the same request. Referers
    # from our own pages are DISCARDED first: /integrations/grok as a
    # referer contains 'grok', and counting a browser on our own Grok page
    # as Grok traffic would be self-inflation with a platform's name on it.
    ref_lower = (referer or "").lower()
    if "dchub" in ref_lower or "localhost" in ref_lower or "127.0.0.1" in ref_lower:
        ref_lower = ""
    # Primary: specific AI-platform markers. Agent signatures match the UA
    # only (the original contract — they are free substrings and would roam a
    # referer hostname: 'mistralwinds.example.com' must not become Mistral).
    # The Referer contributes ONLY via strict domain-boundary key matching
    # ("//cohere." / ".cohere."), so dashboard.cohere.com and
    # huggingface.co/spaces attribute while a host merely containing the
    # word never does.
    for platform_key, info in AI_PLATFORMS.items():
        for agent_sig in info["agents"]:
            if agent_sig.lower() in ua_lower:
                return platform_key
        if ref_lower and (f"//{platform_key}." in ref_lower
                          or f".{platform_key}." in ref_lower):
            return platform_key
    # Generic HTTP libs with no platform evidence stay internal — a bare
    # python-requests/httpx/curl is a script, not a frontier model. (With a
    # platform Referer they were already claimed by the loop above.)
    if _is_generic_lib_ua(ua_lower):
        return "internal"
    # MCP SDK / transport-level identification (more specific than 'mcp')
    if ("mcp" in ua_lower
            or "model-context-protocol" in ua_lower
            or "modelcontextprotocol" in ua_lower):
        if "@modelcontextprotocol/sdk" in ua_lower or "typescript-sdk" in ua_lower:
            return "mcp_sdk_ts"
        if "python-sdk" in ua_lower or "py-mcp" in ua_lower or "mcp-python" in ua_lower:
            return "mcp_sdk_py"
        if "rust-sdk" in ua_lower or "rust-mcp" in ua_lower:
            return "mcp_sdk_rust"
        if "go-mcp" in ua_lower or "mcp-go" in ua_lower:
            return "mcp_sdk_go"
        if "inspector" in ua_lower:
            return "mcp_inspector"
        if "n8n" in ua_lower:
            return "n8n"
        if "continue" in ua_lower:
            return "continue_dev"
        if "codeium" in ua_lower:
            return "codeium"
        # Last resort — better than 'mcp' alone because the bucket
        # name signals "we tried to identify and could not" rather
        # than implying every caller is a generic mcp client.
        return "mcp_generic"
    # Check for generic bots
    if any(b in ua_lower for b in ["bot", "crawler", "spider", "scraper"]):
        return "seo_bot"
    return "direct"


def is_ai_endpoint(path: str) -> bool:
    """Check if path matches AI-relevant endpoint patterns."""
    return any(re.match(p, path) for p in AI_ENDPOINT_PATTERNS)


# ── WebMCP attribution (webmcp-lane, 2026-07-11) ────────────────────────
# js/dchub-webmcp.js (frontend) stamps every bound page-tool API call with
# BOTH markers: `src=webmcp` query param and `X-DC-Source: webmcp` header.
# These fetches run in the user's browser, so the UA is plain Chrome and
# detect_platform() returns 'direct' — which the tracking hook SKIPS. This
# marker check runs first so WebMCP traffic is attributed as platform
# 'webmcp'. Strictly ADDITIVE: nothing else sends these markers, so no
# existing traffic is ever reclassified; a request without the markers takes
# exactly the pre-existing path.
WEBMCP_PLATFORM = "webmcp"


def is_webmcp_request(req) -> bool:
    """True when a request carries the WebMCP attribution markers
    (src=webmcp query param OR X-DC-Source: webmcp header). Pure over the
    request object; never raises."""
    try:
        if (req.args.get("src") or "").strip().lower() == "webmcp":
            return True
        if (req.headers.get("X-DC-Source") or "").strip().lower() == "webmcp":
            return True
    except Exception:
        pass
    return False


def path_marks_webmcp(path: str) -> bool:
    """True when a forwarded path string (CF-worker fire-and-forget tracking
    sends path only, no headers) carries the src=webmcp marker. Pure."""
    return "src=webmcp" in (path or "").lower()


def extract_tool_name(body):
    """
    Pull the tool name out of an MCP JSON-RPC request body.
    Returns None for anything other than a `tools/call` request.

    Use this in main.py's /mcp proxy before calling log_mcp_connection:

        from ai_tracking import extract_tool_name, log_mcp_connection
        tool_name = extract_tool_name(request_json)
        log_mcp_connection(..., method=request_json.get("method"),
                            tool_name=tool_name, ...)
    """
    if not isinstance(body, dict):
        return None
    if body.get("method") != "tools/call":
        return None
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return str(name) if name else None


# ═══════════════════════════════════════════════════════════════
#  NEON POSTGRESQL CONNECTION
# ═══════════════════════════════════════════════════════════════

_pg_local = threading.local()
_pg_lock = threading.Lock()


def _get_pg():
    """Get a psycopg2 connection to Neon. Thread-local with auto-reconnect."""
    import psycopg2
    import psycopg2.extras

    conn = getattr(_pg_local, "conn", None)
    if conn is not None:
        try:
            conn.cursor().execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _pg_local.conn = None

    # New connection
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
    conn.autocommit = False
    try:
        from db_utils import cap_lock_wait
        cap_lock_wait(conn)   # boot-DDL fail-fast on lock contention
    except Exception:
        pass
    _pg_local.conn = conn
    return conn


def _execute(sql, params=None, fetch=False, fetchall=False):
    """Execute SQL against Neon with auto-reconnect and dict cursors."""
    import psycopg2.extras

    conn = _get_pg()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(sql, params)
        if fetchall:
            rows = cur.fetchall()
            conn.commit()
            return [dict(r) for r in rows]
        elif fetch:
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
        else:
            conn.commit()
            return None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # Force reconnect on next call
        try:
            conn.close()
        except Exception:
            pass
        _pg_local.conn = None
        raise e


# ═══════════════════════════════════════════════════════════════
#  SQLITE FALLBACK BUFFER
# ═══════════════════════════════════════════════════════════════

def _get_buffer_db():
    """Get SQLite connection for the local fallback buffer."""
    conn = sqlite3.connect(SQLITE_BUFFER_PATH, timeout=5)
    conn.row_factory = sqlite3.Row  # row["x"] access
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS buffered_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            endpoint TEXT,
            user_agent TEXT,
            ip_address TEXT,
            status_code INTEGER DEFAULT 200,
            response_ms INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            synced INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS buffered_mcp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            method TEXT,
            user_agent TEXT,
            ip_address TEXT,
            tool_name TEXT,
            params TEXT,
            status_code INTEGER,
            response_ms INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            synced INTEGER DEFAULT 0
        )
    """)
    # Idempotent column additions for existing buffer DBs.
    for col_ddl in [
        "client_name TEXT",
        "client_version TEXT",
        "protocol_version TEXT",
        "success INTEGER",
        "error_message TEXT",
    ]:
        col_name = col_ddl.split()[0]
        try:
            c.execute(f"ALTER TABLE buffered_mcp ADD COLUMN {col_ddl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════
#  DATABASE INITIALIZATION
# ═══════════════════════════════════════════════════════════════

def init_db():
    """Create tracking tables in Neon if they don't exist, and migrate
    mcp_connections to the full schema if the live table is older."""
    if not DATABASE_URL:
        logger.warning("AI Tracking: No DATABASE_URL set — tracking disabled")
        return False
    try:
        _execute("""
            CREATE TABLE IF NOT EXISTS ai_requests (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50) NOT NULL,
                endpoint VARCHAR(500),
                user_agent VARCHAR(500),
                ip_address VARCHAR(50),
                status_code INTEGER DEFAULT 200,
                response_ms INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _execute("""
            CREATE TABLE IF NOT EXISTS ai_daily_stats (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                platform VARCHAR(50) NOT NULL,
                request_count INTEGER DEFAULT 0,
                UNIQUE(date, platform)
            )
        """)
        _execute("""
            CREATE TABLE IF NOT EXISTS ai_cumulative (
                platform VARCHAR(50) PRIMARY KEY,
                total_requests INTEGER DEFAULT 0,
                first_seen TIMESTAMPTZ,
                last_seen TIMESTAMPTZ,
                requests_7d INTEGER DEFAULT 0,
                name VARCHAR(100),
                color VARCHAR(20),
                company VARCHAR(100)
            )
        """)
        # Full mcp_connections shape for fresh installs.
        _execute("""
            CREATE TABLE IF NOT EXISTS mcp_connections (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50),
                client_name TEXT,
                client_version TEXT,
                protocol_version TEXT,
                method VARCHAR(100),
                user_agent VARCHAR(500),
                ip_address VARCHAR(50),
                success BOOLEAN,
                error_message TEXT,
                tool_name TEXT,
                params TEXT,
                status_code INTEGER,
                response_ms INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # ── Idempotent migrations for existing deployments ───────
        # These pick up the columns the current live schema is missing,
        # so INSERTs from log_mcp_connection() stop silently failing.
        for migration_sql in [
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS tool_name TEXT",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS params TEXT",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS status_code INTEGER",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS response_ms INTEGER",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS client_name TEXT",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS client_version TEXT",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS protocol_version TEXT",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS success BOOLEAN",
            "ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS error_message TEXT",
            # r-mcpconn-uniqdrop (2026-07-02): a rogue UNIQUE(platform)
            # constraint appeared on live Neon (no repo code creates it —
            # only commit d41df552 ever referenced the name). It capped this
            # append-only event log at ONE row per platform: every direct
            # INSERT for an already-seen platform raised 23505 into the
            # SQLite buffer, and the poison rows head-of-line-blocked the
            # 60s sync loop forever ("Key (platform)=(canon)" log spam).
            # Dropped live 2026-07-02; kept here so any restore/replica or
            # re-created constraint is removed again at boot.
            "ALTER TABLE mcp_connections DROP CONSTRAINT IF EXISTS mcp_connections_platform_unique",
            "DROP INDEX IF EXISTS mcp_connections_platform_unique",
        ]:
            try:
                _execute(migration_sql)
            except Exception as e:
                logger.warning(f"mcp_connections migration skipped: {e}")

        # Indexes
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_created ON ai_requests(created_at DESC)")
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_platform ON ai_requests(platform)")
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_daily_date ON ai_daily_stats(date)")
        _execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_created ON mcp_connections(created_at DESC)")
        _execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_method ON mcp_connections(method)")
        _execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_platform ON mcp_connections(platform)")
        _execute("""CREATE INDEX IF NOT EXISTS idx_mcp_connections_tool_name
                    ON mcp_connections(tool_name) WHERE tool_name IS NOT NULL""")

        # Seed cumulative rows for known platforms
        for key, info in AI_PLATFORMS.items():
            _execute("""
                INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen, name, color, company)
                VALUES (%s, 0, NOW(), NOW(), %s, %s, %s)
                ON CONFLICT (platform) DO UPDATE SET
                    name = EXCLUDED.name,
                    color = EXCLUDED.color,
                    company = EXCLUDED.company
            """, (key, info["name"], info["color"], info["company"]))

        logger.info("AI Tracking DB initialized (Neon PostgreSQL) — v%s", GATEWAY_VERSION)
        return True
    except Exception as e:
        logger.error(f"AI Tracking DB init failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  LOGGING — PRIMARY: NEON, FALLBACK: SQLITE BUFFER
# ═══════════════════════════════════════════════════════════════

def log_ai_request(platform, endpoint, user_agent="", ip_address="",
                   status_code=200, response_ms=0):
    """Record a single AI request. Writes to Neon, falls back to SQLite buffer."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    if not DATABASE_URL:
        return

    try:
        # 1. Insert into ai_requests
        _execute("""
            INSERT INTO ai_requests (platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (platform, endpoint[:500] if endpoint else "", user_agent[:500] if user_agent else "",
              ip_address, status_code, response_ms, now))

        # 2. Upsert ai_daily_stats
        _execute("""
            INSERT INTO ai_daily_stats (date, platform, request_count)
            VALUES (%s, %s, 1)
            ON CONFLICT (date, platform) DO UPDATE SET
                request_count = ai_daily_stats.request_count + 1
        """, (today, platform))

        # 3. Upsert ai_cumulative
        info = AI_PLATFORMS.get(platform, {"name": platform.title(), "color": "#64748b", "company": "Unknown"})
        _execute("""
            INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen, name, color, company)
            VALUES (%s, 1, %s, %s, %s, %s, %s)
            ON CONFLICT (platform) DO UPDATE SET
                total_requests = ai_cumulative.total_requests + 1,
                last_seen = EXCLUDED.last_seen,
                name = EXCLUDED.name,
                color = EXCLUDED.color,
                company = EXCLUDED.company
        """, (platform, now, now, info["name"], info["color"], info["company"]))

    except Exception as e:
        # 25006 = read_only_sql_transaction. A read-only instance (the Render
        # GET-failover) can NEVER write to Neon, so the SQLite buffer can never
        # drain from there — buffering just fills ephemeral disk and the sync
        # thread's replay fails too. Drop it (telemetry only) instead of
        # accumulating an un-drainable buffer. Mirrors the 23505 drop below.
        if getattr(e, "pgcode", None) == "25006":
            return
        logger.warning(f"Neon write failed, buffering locally: {e}")
        try:
            buf = _get_buffer_db()
            buf.execute("""
                INSERT INTO buffered_requests (platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (platform, endpoint[:500] if endpoint else "", user_agent[:500] if user_agent else "",
                  ip_address, status_code, response_ms, now.isoformat()))
            buf.commit()
            buf.close()
        except Exception as buf_err:
            logger.error(f"SQLite buffer write also failed: {buf_err}")


def log_mcp_connection(
    platform,
    method,
    user_agent="",
    ip_address="",
    tool_name=None,
    params=None,
    status_code=None,
    response_ms=0,
    client_name=None,
    client_version=None,
    protocol_version=None,
    success=None,
    error_message=None,
):
    """
    Record an MCP connection / tool call. Writes to Neon, falls back to SQLite buffer.

    For verified-tool-call accounting, you MUST pass:
      - method="tools/call" when it is a tool call (other values: "tools/list",
        "initialize", "ping", etc.)
      - tool_name=<name> when method=="tools/call"   (use extract_tool_name())
      - success=True/False + status_code=<int>

    All new parameters default to None/0 for backward compatibility with older
    callers that only passed the first four args.
    """
    if not DATABASE_URL:
        return

    now = datetime.now(timezone.utc)
    platform = platform or "unknown"
    method = method or ""
    ua = (user_agent or "")[:500]
    ip = ip_address or ""
    params_str = None
    if params is not None:
        if isinstance(params, (dict, list)):
            try:
                params_str = json.dumps(params)[:1000]
            except Exception:
                params_str = str(params)[:1000]
        else:
            params_str = str(params)[:1000]

    try:
        _execute("""
            INSERT INTO mcp_connections (
                platform, method, user_agent, ip_address,
                tool_name, params, status_code, response_ms,
                client_name, client_version, protocol_version,
                success, error_message, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            # r-mcpconn-append (2026-06-23): mcp_connections is an APPEND-ONLY event
            # log (one row per RPC), NOT an upsert-by-platform table. The old
            # `ON CONFLICT (platform) DO UPDATE` had NO matching unique constraint on
            # platform, so EVERY direct insert raised and fell into the SQLite buffer
            # — the table held ~3 rows/7d and the per-platform funnel read 0 for all.
            # Removed the clause so per-platform/per-tool rows actually accumulate.
            platform, method, ua, ip,
            tool_name, params_str, status_code, response_ms or 0,
            client_name, client_version, protocol_version,
            success, error_message, now,
        ))
    except Exception as e:
        if getattr(e, "pgcode", None) == "23505":
            # Unique violation is PERMANENT — the same row can never insert,
            # so buffering it would replay the failure every sync cycle and
            # head-of-line-block all telemetry behind it. Drop it loudly.
            logger.error(f"MCP connection log dropped (unique violation, not buffering): {e}")
            return
        if getattr(e, "pgcode", None) == "25006":
            # read-only instance (Render failover) — can never drain the buffer.
            return
        logger.warning(f"MCP connection log failed, buffering locally: {e}")
        try:
            buf = _get_buffer_db()
            buf.execute("""
                INSERT INTO buffered_mcp (
                    platform, method, user_agent, ip_address,
                    tool_name, params, status_code, response_ms,
                    client_name, client_version, protocol_version,
                    success, error_message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                platform, method, ua, ip,
                tool_name, params_str, status_code, response_ms or 0,
                client_name, client_version, protocol_version,
                (1 if success is True else (0 if success is False else None)),
                error_message, now.isoformat(),
            ))
            buf.commit()
            buf.close()
        except Exception as buf_err:
            logger.error(f"MCP buffer write also failed: {buf_err}")


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND SYNC — FLUSH SQLITE BUFFER TO NEON
# ═══════════════════════════════════════════════════════════════

_sync_thread = None
_sync_stop = threading.Event()


def _sync_buffer_to_neon():
    """Background thread: periodically flush buffered SQLite rows to Neon."""
    while not _sync_stop.is_set():
        try:
            if not os.path.exists(SQLITE_BUFFER_PATH):
                _sync_stop.wait(SYNC_INTERVAL_SECONDS)
                continue

            buf = _get_buffer_db()
            cur = buf.cursor()

            # ── Sync buffered_requests ──────────────────────────
            cur.execute(
                "SELECT * FROM buffered_requests WHERE synced = 0 ORDER BY id LIMIT 100"
            )
            rows = cur.fetchall()

            synced_ids = []
            for row in rows:
                try:
                    _execute("""
                        INSERT INTO ai_requests (platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (row["platform"], row["endpoint"], row["user_agent"],
                          row["ip_address"], row["status_code"], row["response_ms"],
                          row["created_at"]))

                    # Also update daily stats and cumulative
                    date_str = row["created_at"][:10]
                    _execute("""
                        INSERT INTO ai_daily_stats (date, platform, request_count)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (date, platform) DO UPDATE SET
                            request_count = ai_daily_stats.request_count + 1
                    """, (date_str, row["platform"]))

                    info = AI_PLATFORMS.get(row["platform"], {"name": row["platform"].title(), "color": "#64748b", "company": "Unknown"})
                    _execute("""
                        INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen, name, color, company)
                        VALUES (%s, 1, %s, %s, %s, %s, %s)
                        ON CONFLICT (platform) DO UPDATE SET
                            total_requests = ai_cumulative.total_requests + 1,
                            last_seen = GREATEST(ai_cumulative.last_seen, EXCLUDED.last_seen)
                    """, (row["platform"], row["created_at"], row["created_at"],
                          info["name"], info["color"], info["company"]))

                    synced_ids.append(row["id"])
                except Exception as e:
                    if getattr(e, "pgcode", None) == "23505":
                        # Permanent duplicate — retrying can never succeed.
                        # Mark it synced (dead-letter) so it stops
                        # head-of-line-blocking every row behind it.
                        logger.warning(f"Buffer sync row {row['id']} dead-lettered (unique violation): {e}")
                        synced_ids.append(row["id"])
                        continue
                    logger.warning(f"Buffer sync row failed: {e}")
                    break  # Transient Neon failure — retry next cycle

            if synced_ids:
                placeholders = ",".join("?" * len(synced_ids))
                cur.execute(
                    f"UPDATE buffered_requests SET synced = 1 WHERE id IN ({placeholders})",
                    synced_ids,
                )
                buf.commit()
                logger.info(f"Synced {len(synced_ids)} buffered requests to Neon")

            # ── Sync buffered_mcp ───────────────────────────────
            cur.execute(
                "SELECT * FROM buffered_mcp WHERE synced = 0 ORDER BY id LIMIT 100"
            )
            mcp_rows = cur.fetchall()

            mcp_synced = []
            for row in mcp_rows:
                try:
                    success_val = None
                    if "success" in row.keys() and row["success"] is not None:
                        success_val = bool(row["success"])
                    _execute("""
                        INSERT INTO mcp_connections (
                            platform, method, user_agent, ip_address,
                            tool_name, params, status_code, response_ms,
                            client_name, client_version, protocol_version,
                            success, error_message, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        row["platform"], row["method"], row["user_agent"], row["ip_address"],
                        row["tool_name"], row["params"], row["status_code"], row["response_ms"],
                        row["client_name"] if "client_name" in row.keys() else None,
                        row["client_version"] if "client_version" in row.keys() else None,
                        row["protocol_version"] if "protocol_version" in row.keys() else None,
                        success_val,
                        row["error_message"] if "error_message" in row.keys() else None,
                        row["created_at"],
                    ))
                    mcp_synced.append(row["id"])
                except Exception as e:
                    if getattr(e, "pgcode", None) == "23505":
                        # Permanent duplicate (e.g. the rogue UNIQUE(platform)
                        # constraint of 2026-07-02) — dead-letter it so it
                        # stops replaying forever and blocking the queue.
                        logger.warning(f"MCP buffer sync row {row['id']} dead-lettered (unique violation): {e}")
                        mcp_synced.append(row["id"])
                        continue
                    logger.warning(f"MCP buffer sync row failed: {e}")
                    break

            if mcp_synced:
                placeholders = ",".join("?" * len(mcp_synced))
                cur.execute(
                    f"UPDATE buffered_mcp SET synced = 1 WHERE id IN ({placeholders})",
                    mcp_synced,
                )
                buf.commit()

            # Clean up old synced rows (keep last 24h for debugging)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cur.execute("DELETE FROM buffered_requests WHERE synced = 1 AND created_at < ?", (cutoff,))
            cur.execute("DELETE FROM buffered_mcp WHERE synced = 1 AND created_at < ?", (cutoff,))
            buf.commit()
            buf.close()

        except Exception as e:
            logger.error(f"Buffer sync cycle error: {e}")

        _sync_stop.wait(SYNC_INTERVAL_SECONDS)


def _start_sync_thread():
    """Start the background buffer sync thread."""
    if _IS_FAILOVER:
        logger.info("Buffer sync thread NOT started (IS_FAILOVER read-only replica)")
        return
    global _sync_thread
    if _sync_thread is not None and _sync_thread.is_alive():
        return
    _sync_thread = threading.Thread(target=_sync_buffer_to_neon, daemon=True, name="ai-tracking-sync")
    _sync_thread.start()
    logger.info("AI Tracking buffer sync thread started")


# ═══════════════════════════════════════════════════════════════
#  7-DAY ROLLING UPDATE (runs periodically)
# ═══════════════════════════════════════════════════════════════

def update_7d_rolling():
    """Update requests_7d column in ai_cumulative from ai_daily_stats."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        _execute("""
            UPDATE ai_cumulative c SET requests_7d = COALESCE(
                (SELECT SUM(request_count) FROM ai_daily_stats
                 WHERE platform = c.platform AND date >= %s), 0
            )
        """, (cutoff,))
    except Exception as e:
        logger.warning(f"7d rolling update failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════

def get_cumulative_totals():
    try:
        # r71 (2026-06-04): exclude internal buckets + never-used + long-dormant rows
        # so the PUBLIC platform list isn't padded with probes/scanners pinged once.
        # Keeps anything genuinely active (requests_7d>0) or seen in the last 30 days.
        # Honest social-proof for the page journalists land on.
        # 2026-07-18: ai_cumulative.last_seen is TEXT (schema drift), so the
        # bare `last_seen > now() - interval` comparison threw "operator does
        # not exist: text > timestamptz" on EVERY call — the except below
        # swallowed it and the public /ai platform roster rendered EMPTY
        # (total_platforms: 0) since r71 shipped. Guard-then-cast via CASE
        # (CASE guarantees the regex check runs before the cast can throw).
        return _execute(
            "SELECT platform, total_requests, first_seen, last_seen, requests_7d, name, color, company "
            "FROM ai_cumulative "
            "WHERE total_requests > 0 "
            "AND platform NOT IN ('internal','mcp','mcp_generic') "
            "AND (requests_7d > 0 OR CASE WHEN last_seen ~ '^\\d{4}-\\d{2}-\\d{2}' "
            "THEN last_seen::timestamptz > now() - interval '30 days' ELSE false END) "
            "ORDER BY total_requests DESC",
            fetchall=True
        ) or []
    except Exception:
        return []


def get_daily_stats(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        return _execute(
            "SELECT date, platform, request_count FROM ai_daily_stats WHERE date >= %s ORDER BY date DESC",
            (cutoff,), fetchall=True
        ) or []
    except Exception:
        return []


# The published explanation of what was subtracted and why. A filter nobody can
# audit is the same defect in a new coat, so this string travels WITH the
# numbers in every payload that carries them — it is not documentation.
_SELF_TRAFFIC_BASIS = (
    "mcp_calls_30d excludes MCP tool calls whose session_id is declared "
    "operator self-traffic in mcp_calls_deloop.self_traffic_session_prefixes. "
    "These are NOT inferred from the row: the operator's own agent client "
    "writes an mcp_client and user_agent byte-identical to a prospect's, so "
    "the exclusion is a named fact rather than a behavioural guess. "
    "mcp_calls_30d_including_self_traffic is the unfiltered figure, published "
    "so the subtraction can be audited or added back."
)


def get_mcp_calls_by_roster_platform_envelope(days=30):
    """MCP tool calls per /ai ROSTER platform key — the roster's missing half.

    ★ 2026-08-03. The roster's counters come from ai_cumulative, which is
    User-Agent attribution on direct HTTP hits — crawler and citation traffic.
    An MCP tool call never touches it: the agent talks to the gateway and the
    gateway talks to us, so a platform that integrates PROPERLY reads as
    near-zero while a platform that merely crawls reads as huge. Measured the
    same day: Smithery showed **6 lifetime** on its card (last seen 13 Apr)
    against **2,529 MCP calls in 30 days**. Two orders of magnitude, on the
    page whose job is honest AI-traffic numbers.

    The mapping reuses the two vocabularies that already exist — deliberately
    NOT a third, because a third is the regex-twin drift this repo keeps
    paying for:
      1. ai_platform_canon.canonical_platform() collapses a raw client name to
         its vendor: 'anthropic/api' -> 'claude', 'claude-code' -> 'claude',
         'gemini-cli' -> 'gemini'.
      2. anything it does not recognize is matched against the ROSTER's OWN
         AI_PLATFORMS[key]['agents'] markers — the same substring vocabulary
         detect_platform() runs over User-Agents. That is what turns
         'smithery connect' into 'smithery' without teaching anything new.

    Returns the PUBLISHABLE envelope, not a bare mapping — the filtered and
    unfiltered figures are computed in one pass so they can never disagree:

        {"calls":                        {roster_key: calls},  # self-traffic OUT
         "calls_including_self_traffic": {roster_key: calls},  # unfiltered
         "excluded": {"self_traffic_sessions": [...prefixes...],
                      "mcp_calls_removed": {roster_key: n},
                      "mcp_calls_removed_total": n,
                      "basis": "<why, in prose>"}}

    Unmappable buckets are DROPPED, never bucketed as "other":
    'mcp-generic-client' alone is ~11k calls from ~338 IPs, and attributing it
    to a named platform would publish a number we cannot stand behind. It
    stays visible in the funnel's calls_by_platform_30d, where it is labeled
    for what it is.
    """
    try:
        from ai_platform_canon import canonical_platform
    except Exception:
        canonical_platform = lambda p: None  # noqa: E731 — fall through to markers
    # The canon names OpenAI 'openai'; this roster's key is 'chatgpt'. That is
    # the ONLY key-space disagreement between the two — a bridge, not a second
    # mapping. A test pins that every emitted key is a real roster platform.
    _VENDOR_TO_ROSTER = {"openai": "chatgpt"}

    def _roster_key(bucket):
        if not bucket:
            return None
        v = canonical_platform(bucket)
        if v:
            v = _VENDOR_TO_ROSTER.get(v, v)
            return v if v in AI_PLATFORMS else None
        b = bucket.lower()
        for key, info in AI_PLATFORMS.items():
            for marker in info.get("agents", ()):
                if marker.lower() in b:
                    return key
        return None

    # ── r-selftraffic-roster (2026-08-18): the operator's own sessions ───────
    # This panel was publishing the operator's own Claude Code sessions as
    # external agent demand. Same defect the handoff funnel fixed one layer
    # down (human_acted DEFINITION v4, PR #2832) — that fix subtracts declared
    # operator sessions from the FUNNEL and does not reach this surface, which
    # is the one a reader actually looks at. "Claude" is the largest
    # MCP-calling platform on the page, so the headline was self-referential.
    #
    # ★ APPLIED IN THE QUERY, NOT BY RECREATING THE VIEW. mcp_calls_identity is
    # a VIEW and its definition EMBEDS the rendered predicate text at CREATE
    # time. Verified 2026-08-18: the live view still carries the OLD
    # `dchub-|dchub/|dchubhealer` UA pattern even though _SCRIPT_INTERNAL_UA
    # was broadened to bare `dchub` and deployed hours earlier — changing the
    # Python does NOT change the view. Recreating it would retroactively
    # rewrite is_real_external for ALL history and EVERY consumer of it; that
    # is a separate, announced change. A query-level exclusion has the blast
    # radius of this function.
    #
    # ★ SESSION-KEYED, NOT IP-KEYED — and that distinction is deliberate. The
    # view carries session_id, so this is the SAME identity basis as the funnel
    # fix. It is NOT agent_id (md5 of the first public X-Forwarded-For token):
    # an agent_id exclusion is an IP exclusion, and with shared egress and
    # rotating addresses that is a different and far more dangerous instrument.
    #
    # ★ DECLARED, NOT INFERRED. The operator's Claude Code writes
    # mcp_client 'claude' / user_agent 'node', byte-identical to a prospect's.
    # There is no behavioural signal, and inventing one ("bursty", "too many
    # tools in a day") would silently delete the best real leads — the exact
    # failure mode mcp_calls_deloop._AMBIGUOUS_NOT_EXCLUDED exists to prevent.
    # The vocabulary is reused, never re-declared: a second hand-maintained
    # list here is the regex-twin drift that module was written to stop.
    try:
        from mcp_calls_deloop import (external_session_predicate,
                                      self_traffic_session_prefixes)
        _not_self = external_session_predicate("session_id")
        _prefixes = list(self_traffic_session_prefixes())
    except Exception as _e:
        # Fail OPEN — but never silently. Nothing is removed and the published
        # envelope says so (empty prefix list, removed=0), so a reader can tell
        # "no exclusion applied" apart from "exclusion applied, found nothing".
        logger.warning("self-traffic session predicate unavailable: %s", _e)
        _not_self, _prefixes = "TRUE", []

    kept, gross = {}, {}
    try:
        rows = _execute(
            "SELECT platform, COUNT(*) AS n_all, "
            "       COUNT(*) FILTER (WHERE " + _not_self + ") AS n_ext "
            "FROM mcp_calls_identity "
            "WHERE is_public_ip AND is_real_external "
            f"  AND created_at >= now() - interval '{int(days)} days' "
            "GROUP BY platform",
            fetchall=True) or []
        for r in rows:
            if isinstance(r, dict):
                bucket, n_all, n_ext = r["platform"], r["n_all"], r["n_ext"]
            else:
                # Indexed, not unpacked-with-default: a row that lost the n_ext
                # column must RAISE into the fail-open below, not quietly fall
                # back to the unfiltered count. A silent fallback would restore
                # the exact number this function exists to remove.
                bucket, n_all, n_ext = r[0], r[1], r[2]
            key = _roster_key(bucket)
            if key:
                gross[key] = gross.get(key, 0) + int(n_all or 0)
                kept[key] = kept.get(key, 0) + int(n_ext or 0)
    except Exception as e:
        logger.warning("mcp calls by roster platform failed: %s", e)
        return {"calls": {}, "calls_including_self_traffic": {},
                "excluded": {"self_traffic_sessions": _prefixes,
                             "mcp_calls_removed": {},
                             "mcp_calls_removed_total": 0,
                             "basis": _SELF_TRAFFIC_BASIS + " (query failed; no "
                                      "figures published this cycle)"}}

    removed = {k: gross[k] - kept.get(k, 0)
               for k in gross if gross[k] - kept.get(k, 0)}
    return {
        "calls": kept,
        "calls_including_self_traffic": gross,
        "excluded": {
            "self_traffic_sessions": _prefixes,
            "mcp_calls_removed": removed,
            "mcp_calls_removed_total": sum(removed.values()),
            "basis": _SELF_TRAFFIC_BASIS,
        },
    }


def get_mcp_calls_by_roster_platform(days=30):
    """{roster_key: calls} with declared operator self-traffic EXCLUDED.

    Thin wrapper over get_mcp_calls_by_roster_platform_envelope() so the two
    figures can never be computed by two different queries. Callers that
    publish the number should use the envelope and publish the unfiltered
    figure and `excluded` block beside it — see /api/ai/tracking."""
    return get_mcp_calls_by_roster_platform_envelope(days).get("calls", {})


def get_platform_chart_data(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = _execute(
            "SELECT platform, SUM(request_count) as total FROM ai_daily_stats WHERE date >= %s GROUP BY platform ORDER BY total DESC",
            (cutoff,), fetchall=True
        ) or []
        chart = {}
        for r in rows:
            p = r["platform"]
            info = AI_PLATFORMS.get(p, {"name": p.title(), "color": "#64748b"})
            chart[p] = {"name": info["name"], "color": info["color"], "requests_7d": r["total"]}
        return chart
    except Exception:
        return {}


# Same exclusion the public roster already uses (get_cumulative_totals, r71):
# these are OUR OWN traffic and protocol buckets, not an AI platform anyone
# outside can see.
_NON_PLATFORM_BUCKETS = ("internal", "mcp", "mcp_generic")


# ── Self-refresh / telemetry endpoints (r-honest-feed-live, 2026-08-06) ──
#
# ★WHY THE PLATFORM FILTER ALONE IS NOT ENOUGH.
# Excluding platform='internal' removes the self-traffic that ADMITS it is
# ours. It does not remove ours that arrives wearing a platform's NAME.
#
# Measured on live data the day this was written: ONE residential IP polling
# /api/v1/mcp/handoff-funnel twice a minute — plain Chrome UA, attributed
# 'claude' by REFERER (a page hosted on claude.ai fetching us is
# indistinguishable from Claude fetching us, by UA alone) — accounted for
# 6,807 of 7,769 'claude' rows over 7 days. Newest-first with only the
# platform filter, that single poller held 14 of the 20 feed slots.
#
# Every one of those rows is REAL. That is precisely what makes it dangerous:
# the panel would have rendered a wall of "Claude · /api/v1/mcp/handoff-funnel
# · 30s ago" and read as heavy live Claude crawling. Real rows, false picture
# — the same end state as the client-side fabricator removed on 2026-08-04,
# reached by a different road. A feed that is merely SOURCED from real rows is
# not yet an honest feed.
#
# So the feed also drops requests to endpoints only our own dashboards poll.
# What survives is AI platforms fetching CONTENT: facility pages, market
# deep-dives, transactions, pockets.
#
# Mirrors the `boring` list in dchub-frontend/ai.html renderFeed(), extended
# with the pollers measured above. EXPORTED (not inlined) so the crawler-
# channel externality work reuses this list instead of growing a second copy
# that drifts — this repo has lost sessions to exactly that (manifest drift,
# the two-classifier feed/roster split).
FEED_SELF_REFRESH_ENDPOINTS = (
    # dashboard data sources (the /ai page and siblings polling themselves)
    "/api/v1/ai-tracking/", "/api/ai/tracking", "/api/ai/recent",
    "/api/news/live", "/ai/platforms", "/api/ai-ecosystem/status",
    "/api/v1/discovery", "/health",
    # MCP dashboards — the handoff-funnel poller above lives here
    "/api/v1/mcp/",
    # telemetry beacons our own pages fire
    "/api/v1/surface/track", "/api/v1/connect/click", "/api/v1/pricing/ab-",
    # dashboard chrome / metrics widgets
    "/api/v1/ai/reach", "/api/v1/me/tier", "/api/v1/tiers", "/api/v1/hero/",
    "/api/v1/stats/canonical", "/api/v1/platform-cards",
    "/api/v1/citations/", "/api/v1/brain/", "/api/v1/onboard",
    "/api/v1/agent/cookbook",
)

# Belt-and-braces UA guard. detect_platform() already buckets these to
# 'internal' at WRITE time, so this only catches rows written before a marker
# was added to _INTERNAL_UA_MARKERS — but the whole point of this panel is
# that stale self-traffic must not resurface wearing a platform's name.
FEED_SELF_UA_MARKERS = ("dchub", "dc-hub", "probe", "scanner", "uptime",
                        "healthcheck", "health-check", "railway", "localhost",
                        "smoke", "qa-", "tester")

# Our own traffic that is named as such in the PLATFORM column and slips both
# filters above. Measured: 'dchub-internal' (166 rows/30d) hits '/mcp
# (initialize)' — which the endpoint list does not match (it is not
# '/api/v1/mcp/') — under UAs like 'curl/8.7.1' and a plain Windows Chrome
# string, which the UA markers do not match either. It is only ever
# identifiable by the bucket name, so it is filtered by name.
FEED_SELF_PLATFORM_PATTERNS = ("dchub%",)

# ── Secrets-scanner probe paths (2026-08-15) ──────────────────────────────
# A scanner spoofing the ChatGPT UA probes /api/v1/env, /api/v1/config and
# /api/v1/settings hunting for leaked secrets — every hit 404s (verified
# 2026-08-14: no route exists under any of these prefixes), but each row is
# written with platform='chatgpt' (detect_platform buckets by UA) and the
# feed rendered it as live ChatGPT activity. The platform/UA/endpoint filters
# above all miss it: the platform is a real one's name, the UA is a real
# one's UA, and the path is not a dashboard self-refresh endpoint. The ONLY
# stable fingerprint is the path itself, so the feed drops rows whose path is
# a known probe target. Anchored-prefix patterns (trailing %, no leading %):
# a probe path is matched from the string start so `/api/v1/configuration`-
# style REAL endpoints could only collide if one existed — none does, and any
# future route under these prefixes must pick a different name or prune this
# list. Counts are untouched — this is a feed-display filter; the channel
# attribution work reads ai_requests directly.
FEED_SCANNER_PROBE_PATHS = ("/api/v1/env", "/api/v1/config", "/api/v1/settings")


def recent_activity_feed(limit=20, include_internal=False):
    """The live "Latest AI Requests" feed, plus the reason it is empty.

    ★THE BUG THIS FIXES (2026-08-06). The old query took the last N rows of
    ai_requests with NO platform filter, while the surface renders them under
    "real events only". Self-traffic is written to the SAME table with
    platform='internal' (detect_platform -> _is_internal_ua), and it dominates:
    3,321,235 of 3,899,804 all-time requests were internal when this was
    found — 85%. So the last 20 rows were, with near-certainty, 20 internal
    rows, and the page rendered "Quiet right now" while the very same page
    reported 40 distinct agents and 6,221 MCP tool calls over 7d.

    Nothing was broken about agent traffic. The feed could not see past our
    own. It worked when self-traffic was a smaller share and degraded silently
    as that share grew — no error, no alert, just a feed that got quieter as
    the site got busier.

    ★AND "EMPTY" MUST NOT MEAN THREE DIFFERENT THINGS. The old body swallowed
    every exception into `return []`, so a missing table, a broken query and a
    genuinely quiet feed were indistinguishable on screen. That is why this
    survived: "quiet" is the one reading nobody investigates. This returns the
    diagnosis with the rows — how many recent rows were scanned, how many were
    filtered out as ours, and the error string when there is one.
    """
    excl = "','".join(_NON_PLATFORM_BUCKETS)
    # Params, never interpolation — a literal % in the SQL string is a 500 in
    # psycopg2, and these patterns are all wildcards.
    ep_pat = ["%" + m + "%" for m in FEED_SELF_REFRESH_ENDPOINTS]
    # Scanner probe paths ride the same NOT LIKE ALL clause, anchored at the
    # start of the path (see FEED_SCANNER_PROBE_PATHS — a 404 probe wearing
    # ChatGPT's UA is not ChatGPT activity).
    ep_pat += [m + "%" for m in FEED_SCANNER_PROBE_PATHS]
    ua_pat = ["%" + m + "%" for m in FEED_SELF_UA_MARKERS]
    if include_internal:
        where, params = "", (limit,)
    else:
        where = (
            "WHERE platform NOT IN ('" + excl + "') "
            # ...and not ours self-identified in the platform column
            # (dchub-*), which the endpoint/UA filters cannot see.
            "AND COALESCE(platform,'') NOT LIKE ALL(%s) "
            # ...and not ours wearing a platform's name (see the comment on
            # FEED_SELF_REFRESH_ENDPOINTS — one poller held 14/20 slots).
            "AND COALESCE(endpoint,'') NOT LIKE ALL(%s) "
            "AND COALESCE(user_agent,'') NOT ILIKE ALL(%s) "
        )
        params = (list(FEED_SELF_PLATFORM_PATTERNS), ep_pat, ua_pat, limit)
    out = {"activity": [], "scanned": None, "excluded_internal": None,
           "basis": ("all rows (internal included)" if include_internal
                     else "external platforms only — excludes "
                          + ", ".join(_NON_PLATFORM_BUCKETS)
                          + "; and our own dashboard self-refresh endpoints"),
           "error": None}
    try:
        out["activity"] = _execute(
            "SELECT platform, endpoint, created_at FROM ai_requests "
            f"{where}ORDER BY created_at DESC LIMIT %s",
            params, fetchall=True) or []
    except Exception as e:  # noqa: BLE001
        # REPORTED, not swallowed. An empty feed with `error` set is a BROKEN
        # feed; an empty feed with error None and excluded_internal high is a
        # feed correctly filtering our own noise. Those need opposite responses.
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        logger.warning("recent_activity_feed query failed: %s", out["error"])
        return out
    # How much of the recent window was ours? This is the number that EXPLAINS
    # a quiet feed, which is why it earns one extra query.
    try:
        scanned = _execute(
            "SELECT COUNT(*) AS c, "
            f"COUNT(*) FILTER (WHERE platform IN ('{excl}')) AS internal_c "
            "FROM (SELECT platform FROM ai_requests "
            "      ORDER BY created_at DESC LIMIT %s) recent",
            (max(limit * 10, 200),), fetch=True)
        if scanned:
            out["scanned"] = scanned["c"]
            out["excluded_internal"] = scanned["internal_c"]
    except Exception as e:  # noqa: BLE001
        # Diagnostics are best-effort — they must never empty a working feed.
        logger.debug("recent_activity_feed diagnostics failed: %s", str(e)[:120])
    return out


def get_recent_activity(limit=20):
    """Back-compat: just the rows, real external platforms only."""
    return recent_activity_feed(limit).get("activity") or []


def feed_rows_for_surface(limit=20):
    """Rows shaped for the /ai "Latest AI Requests" panel.

    ★EVERY FIELD HERE COMES OFF A REAL ROW. `time_ago` in particular is
    derived from that row's own created_at — never a client-side "Just now",
    never a lifetime counter turned into an event. The panel's whole history
    is fabrication (a client-side generator stamped "Just now" on any platform
    with a nonzero ALL-TIME count, so HuggingFace — one request, ever —
    rendered as live traffic). Server-side relative time from the row's real
    timestamp is what makes the label checkable.

    An empty list is a valid, honest answer and renders the empty state.
    """
    rows = recent_activity_feed(limit).get("activity") or []
    shaped = []
    for r in rows:
        key = (r.get("platform") or "").strip().lower()
        info = AI_PLATFORMS.get(key, {})
        ts = r.get("created_at")
        shaped.append({
            "platform_key": key,
            "platform": info.get("name") or key,
            "color": info.get("color"),
            "endpoint": r.get("endpoint") or "",
            # ISO so the client can recompute if it wants to; time_ago so it
            # never has to invent one.
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts else None),
            "time_ago": _time_ago(ts) if ts else None,
        })
    return shaped


def get_requests_today():
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = _execute(
            "SELECT COALESCE(SUM(request_count), 0) as total FROM ai_daily_stats WHERE date = %s",
            (today,), fetch=True
        )
        return r["total"] if r else 0
    except Exception:
        return 0


def get_all_time_total():
    try:
        r = _execute(
            "SELECT COALESCE(SUM(total_requests), 0) as total FROM ai_cumulative",
            fetch=True
        )
        return r["total"] if r else 0
    except Exception:
        return 0


def _success_predicate():
    """Shared SQL fragment: row is considered a successful MCP call if
    success=TRUE, or (success IS NULL AND status_code is 2xx or NULL)."""
    return (
        "(success = TRUE OR "
        "(success IS NULL AND (status_code IS NULL OR status_code BETWEEN 200 AND 299)))"
    )


def get_mcp_tool_stats(days=7):
    """Per-tool verified call counts over the last N days.

    Filters to `method = 'tools/call'` so probes, handshakes, and catalog
    fetches don't pollute the tool-specific numbers.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return _execute(f"""
            SELECT
                tool_name,
                COUNT(*)                                           AS call_count,
                COUNT(*) FILTER (WHERE {_success_predicate()})     AS success_count,
                AVG(NULLIF(response_ms, 0))                        AS avg_ms
            FROM mcp_connections
            WHERE created_at >= %s
              AND method = 'tools/call'
              AND tool_name IS NOT NULL
              AND tool_name <> ''
            GROUP BY tool_name
            ORDER BY call_count DESC
        """, (cutoff,), fetchall=True) or []
    except Exception:
        return []


def get_mcp_method_breakdown(days=7):
    """Segment mcp_connections by JSON-RPC method — tools/call vs tools/list
    vs initialize vs ping etc. Tells you whether the traffic is real usage
    or just handshake/discovery noise."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return _execute(f"""
            SELECT
                method,
                COUNT(*)                                         AS n,
                COUNT(*) FILTER (WHERE {_success_predicate()})   AS ok,
                COUNT(DISTINCT platform)                         AS platforms,
                COUNT(DISTINCT ip_address)                       AS ips
            FROM mcp_connections
            WHERE created_at >= %s
            GROUP BY method
            ORDER BY n DESC
        """, (cutoff,), fetchall=True) or []
    except Exception:
        return []


def get_verified_call_totals(days=7):
    """One-row summary distinguishing verified tool calls from noise.
    This is the headline metric for MCP usage — the number you actually
    want to report, NOT `SELECT COUNT(*) FROM mcp_connections`."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return _execute(f"""
            SELECT
                COUNT(*)                                            AS total_rows,
                COUNT(*) FILTER (WHERE method = 'tools/call')       AS tool_call_attempts,
                COUNT(*) FILTER (
                    WHERE method = 'tools/call' AND {_success_predicate()}
                )                                                   AS verified_tool_calls,
                COUNT(*) FILTER (WHERE method = 'tools/list')       AS catalog_fetches,
                COUNT(*) FILTER (WHERE method = 'initialize')       AS handshakes,
                COUNT(DISTINCT platform)                            AS distinct_platforms,
                COUNT(DISTINCT ip_address)                          AS unique_ips
            FROM mcp_connections
            WHERE created_at >= %s
        """, (cutoff,), fetch=True) or {}
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════
#  v4.2 — SCHEMA DRIFT CHECK + PIPELINE CANARY
# ═══════════════════════════════════════════════════════════════
#  Two checks that catch the class of bug we hit in v4.0:
#    (1) check_mcp_schema()          — runs at startup. Compares live
#        mcp_connections columns against EXPECTED_MCP_CONNECTIONS_COLUMNS.
#        Logs LOUD warnings if drift is found. The ALTER TABLE IF NOT
#        EXISTS migrations in init_db() should keep this green, but if
#        a manual schema change happens upstream we want to know.
#    (2) canary_tool_name_coverage() — runs on demand. Counts how many
#        rows have method='tools/call' but tool_name IS NULL. That's
#        the tell that the logging pipeline is dropping tool names on
#        the floor even though calls are landing.
#
#  Both are exposed via /api/ai/mcp-health for dashboard/cron use.
# ═══════════════════════════════════════════════════════════════

def check_mcp_schema():
    """
    Inspect information_schema.columns for mcp_connections and compare
    against EXPECTED_MCP_CONNECTIONS_COLUMNS. Returns a dict:

        {
            "status": "ok" | "drift" | "error",
            "missing": [str],     # columns we expect but live table lacks
            "extra":   [str],     # columns live table has but we don't track
            "actual":  [str],     # the full live column list
            "message": str,       # human-readable summary
        }

    A "drift" status means log_mcp_connection will silently fail or
    write NULLs for whatever columns are missing. Treat it as a page.
    """
    if not DATABASE_URL:
        return {"status": "error", "message": "no DATABASE_URL", "missing": [], "extra": [], "actual": []}
    try:
        rows = _execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'mcp_connections'
        """, fetchall=True) or []
        actual = {r["column_name"] if isinstance(r, dict) else r[0] for r in rows}
        if not actual:
            return {
                "status": "error",
                "missing": sorted(EXPECTED_MCP_CONNECTIONS_COLUMNS),
                "extra": [],
                "actual": [],
                "message": "mcp_connections table not found — init_db probably failed",
            }
        missing = sorted(EXPECTED_MCP_CONNECTIONS_COLUMNS - actual)
        extra = sorted(actual - EXPECTED_MCP_CONNECTIONS_COLUMNS)
        if missing:
            return {
                "status": "drift",
                "missing": missing,
                "extra": extra,
                "actual": sorted(actual),
                "message": f"mcp_connections is missing columns: {missing}. log_mcp_connection will drop these fields.",
            }
        return {
            "status": "ok",
            "missing": [],
            "extra": extra,
            "actual": sorted(actual),
            "message": "mcp_connections schema matches expected shape",
        }
    except Exception as e:
        return {
            "status": "error",
            "missing": [],
            "extra": [],
            "actual": [],
            "message": f"schema check failed: {type(e).__name__}: {e}",
        }


def canary_tool_name_coverage(days=7):
    """
    Pipeline health canary — answers: "of the tools/call rows landing in
    mcp_connections, how many actually have a tool_name?" If this goes
    non-zero for NULLs, the extractor in main.py is broken even though
    inserts are succeeding.

    Returns:
        {
            "status": "ok" | "degraded" | "broken" | "no_data" | "error",
            "window_days": int,
            "tool_call_rows": int,           # rows with method='tools/call'
            "with_tool_name": int,           # rows with non-null tool_name
            "null_tool_name": int,           # rows with NULL tool_name (BAD)
            "coverage_pct": float,           # with_tool_name / tool_call_rows
            "alert": str,                    # human-readable alert
        }
    """
    if not DATABASE_URL:
        return {"status": "error", "alert": "no DATABASE_URL"}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = _execute("""
            SELECT
                COUNT(*) FILTER (WHERE method = 'tools/call')                              AS tool_call_rows,
                COUNT(*) FILTER (WHERE method = 'tools/call' AND tool_name IS NOT NULL)    AS with_tool_name,
                COUNT(*) FILTER (WHERE method = 'tools/call' AND tool_name IS NULL)        AS null_tool_name
            FROM mcp_connections
            WHERE created_at >= %s
        """, (cutoff,), fetch=True) or {}

        tool_call_rows = int(row.get("tool_call_rows", 0) or 0)
        with_name = int(row.get("with_tool_name", 0) or 0)
        null_name = int(row.get("null_tool_name", 0) or 0)

        if tool_call_rows == 0:
            return {
                "status": "no_data",
                "window_days": days,
                "tool_call_rows": 0,
                "with_tool_name": 0,
                "null_tool_name": 0,
                "coverage_pct": 0.0,
                "alert": f"No tools/call rows in last {days}d — the MCP is either unused or tier-gated to 0.",
            }

        coverage_pct = round(100.0 * with_name / tool_call_rows, 2)

        if null_name == 0:
            status, alert = "ok", f"{coverage_pct}% coverage — tool_name pipeline healthy"
        elif coverage_pct >= 95:
            status, alert = "ok", f"{coverage_pct}% coverage — {null_name} NULL rows, probably a race"
        elif coverage_pct >= 50:
            status, alert = "degraded", f"{coverage_pct}% coverage — {null_name} rows have NULL tool_name. Check main.py's extract_tool_name path."
        else:
            status, alert = "broken", f"Only {coverage_pct}% of tools/call rows have a tool_name — the logging pipeline is dropping names on the floor."

        return {
            "status": status,
            "window_days": days,
            "tool_call_rows": tool_call_rows,
            "with_tool_name": with_name,
            "null_tool_name": null_name,
            "coverage_pct": coverage_pct,
            "alert": alert,
        }
    except Exception as e:
        return {
            "status": "error",
            "window_days": days,
            "alert": f"canary query failed: {type(e).__name__}: {e}",
        }


def get_mcp_health_summary(days=7):
    """
    Convenience bundle — one call returns schema + canary + headline totals.
    Wire this to /api/ai/mcp-health (see init_ai_tracking).
    """
    return {
        "version": GATEWAY_VERSION,
        "schema": check_mcp_schema(),
        "canary": canary_tool_name_coverage(days=days),
        "totals": get_verified_call_totals(days=days),
    }


# ═══════════════════════════════════════════════════════════════
#  CORS HELPER
# ═══════════════════════════════════════════════════════════════

def cors_jsonify(data, status=200):
    """Return JSON response with CORS headers."""
    resp = make_response(jsonify(data), status)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key, Accept, X-Requested-With"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


# ═══════════════════════════════════════════════════════════════
#  FLASK MIDDLEWARE & ROUTES
# ═══════════════════════════════════════════════════════════════

def _time_ago(ts):
    """Convert timestamp to human-readable relative time."""
    if ts is None:
        return "never"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return ts
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    diff = now - ts
    secs = int(diff.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    elif secs < 3600:
        return f"{secs // 60}m ago"
    elif secs < 86400:
        return f"{secs // 3600}h ago"
    else:
        return f"{secs // 86400}d ago"


def init_ai_tracking(app: Flask):
    """Initialize AI tracking — call once in main.py after Flask app creation."""

    # r-qa (2026-06-27): init_db() runs idempotent ALTER TABLE migrations on the
    # HOT, actively-written mcp_connections table. Each ALTER needs an ACCESS
    # EXCLUSIVE lock, and under live MCP write traffic during a cold boot the lock
    # wait can stall the gunicorn worker bind ~40s+ — long enough to trip Railway's
    # deploy healthcheck (a real FAILED backend deploy was traced to this). Run the
    # schema init/migration + drift check in a background daemon thread so the
    # worker binds and serves routes IMMEDIATELY. Safe to defer: the migrations are
    # idempotent (ADD COLUMN IF NOT EXISTS — already a no-op on live prod where the
    # columns exist), log_mcp_connection already tolerates a not-yet-migrated table,
    # and init_new_tables() (the bg deferred_db_init thread) also creates the table.
    def _deferred_schema_init():
        # Init Neon tables (runs idempotent migrations)
        try:
            db_ok = init_db()
            if not db_ok:
                logger.warning("AI Tracking running in degraded mode (no Neon)")
        except Exception as e:
            logger.error(f"AI Tracking: deferred init_db crashed: {e}")
            return

        # ── v4.2 startup schema check ──────────────────────────
        # This fires AFTER init_db() so it sees the post-migration shape.
        # If it still reports drift, init_db's ALTER TABLE migrations
        # failed and log_mcp_connection will silently drop fields.
        try:
            schema_report = check_mcp_schema()
            if schema_report["status"] == "ok":
                logger.info(
                    "AI Tracking v%s: mcp_connections schema OK (%d cols)",
                    GATEWAY_VERSION, len(schema_report["actual"]),
                )
            elif schema_report["status"] == "drift":
                logger.error(
                    "AI Tracking v%s: ⚠ SCHEMA DRIFT on mcp_connections — %s",
                    GATEWAY_VERSION, schema_report["message"],
                )
                logger.error("AI Tracking: missing columns = %s", schema_report["missing"])
            else:
                logger.error(
                    "AI Tracking v%s: schema check failed — %s",
                    GATEWAY_VERSION, schema_report["message"],
                )
        except Exception as e:
            logger.error(f"AI Tracking: check_mcp_schema crashed: {e}")

    threading.Thread(
        target=_deferred_schema_init, name="ai-tracking-schema-init", daemon=True
    ).start()

    # Start buffer sync thread
    _start_sync_thread()

    # ── Request middleware ──────────────────────────────────
    @app.before_request
    def _track_ai_request():
        """Track every request from identified AI platforms."""
        g.ai_track_start = time.time()

    @app.after_request
    def _log_ai_request(response):
        """After request: log if it's from an AI platform to an AI endpoint."""
        try:
            path = request.path
            # webmcp-lane (2026-07-11): WebMCP page tools stamp src=webmcp /
            # X-DC-Source: webmcp on every bound call. Classify those as
            # platform 'webmcp' BEFORE the UA detector (their UA is the plain
            # browser → 'direct' → previously skipped, i.e. invisible). The
            # marker also bypasses the endpoint-pattern gate so a bound path
            # outside AI_ENDPOINT_PATTERNS (e.g. /api/rankings/<cat>) still
            # lands. Additive: unmarked requests take the exact old path.
            webmcp = is_webmcp_request(request)
            if not webmcp and not is_ai_endpoint(path):
                return response

            ua = request.headers.get("User-Agent", "")
            platform = (WEBMCP_PLATFORM if webmcp else
                        detect_platform(ua, request.headers.get("Referer", "")))

            # Skip direct/browser traffic and SEO bots for cleaner analytics
            if platform in ("direct", "seo_bot"):
                return response

            elapsed = int((time.time() - getattr(g, "ai_track_start", time.time())) * 1000)
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
            if "," in ip:
                ip = ip.split(",")[0].strip()

            # Non-blocking: fire in a thread so we don't slow down the response
            threading.Thread(
                target=log_ai_request,
                args=(platform, path, ua, ip, response.status_code, elapsed),
                daemon=True
            ).start()
        except Exception:
            pass
        return response

    # ── API Routes ─────────────────────────────────────────

    @app.route("/api/ai/platforms", methods=["GET", "OPTIONS"])
    def ai_platforms():
        """Main analytics endpoint — consumed by the AI Analytics page."""
        if request.method == "OPTIONS":
            return cors_jsonify({})

        cumulative = get_cumulative_totals()
        daily = get_daily_stats(7)
        chart = get_platform_chart_data(7)
        today_count = get_requests_today()
        all_time = get_all_time_total()
        mcp_tools = get_mcp_tool_stats(7)
        mcp_verified = get_verified_call_totals(7)
        mcp_methods = get_mcp_method_breakdown(7)

        # Build platform list (exclude direct/seo_bot)
        # 2026-07-18: also drop probe/QA/internal rows ('v', 'zx', 'gate-test',
        # 'dchub-*') via the canonical junk predicate — with the last_seen
        # date filter working again, recently-active dev tags would otherwise
        # resurface on the public roster. Lazy import (main imports us first);
        # fail-open to the static excludes.
        try:
            from main import _is_junk_platform as _junk
        except Exception:
            _junk = lambda k: False
        # r-two-channels-roster (2026-08-03): the roster's second half. Every
        # figure above this line is CRAWLER traffic (UA-attributed HTTP hits);
        # tool calls arrive through the gateway and never touch ai_cumulative.
        # Publishing only the first half is what made Smithery read 6 against
        # 2,529 real calls. Fail-open to {} — a roster missing its MCP column
        # is worse than yesterday's, but a roster that 500s is worse than both.
        #
        # ★ r-selftraffic-roster (2026-08-18): mcp_calls_30d EXCLUDES sessions
        # declared as operator self-traffic. The unfiltered figure and the
        # `mcp_calls_excluded` block ship alongside — see the envelope's basis.
        try:
            _mcp_env = get_mcp_calls_by_roster_platform_envelope(30) or {}
            _mcp_calls = _mcp_env.get("calls") or {}
            _mcp_gross = _mcp_env.get("calls_including_self_traffic") or {}
            _mcp_excluded = _mcp_env.get("excluded") or {}
        except Exception as _e:
            logger.warning("roster mcp merge failed: %s", _e)
            _mcp_calls, _mcp_gross, _mcp_excluded = {}, {}, {}
        # A platform can be MCP-ONLY — real tool calls, zero crawler hits — in
        # which case it has no ai_cumulative row and vanished from the roster
        # entirely. Union the key sets so integrating is enough to appear.
        _cum_keys = {r.get("platform", "") for r in cumulative}
        _extra = [k for k in _mcp_calls if k and k not in _cum_keys]
        platforms = []
        for row in list(cumulative) + [{"platform": k} for k in _extra]:
            p = row.get("platform", "")
            if p in ("direct", "seo_bot", "media_crawler", "unknown_ai"):
                continue
            if _junk(p):
                continue
            platforms.append({
                "platform": p,
                "name": row.get("name") or AI_PLATFORMS.get(p, {}).get("name", p.title()),
                "color": row.get("color") or AI_PLATFORMS.get(p, {}).get("color", "#64748b"),
                "company": row.get("company") or AI_PLATFORMS.get(p, {}).get("company", ""),
                "total_requests": row.get("total_requests", 0),
                "requests_7d": row.get("requests_7d", 0),
                # The MCP channel, named so it can never be read as crawler
                # volume or silently summed into it — two channels, two
                # numbers, both labeled.
                "mcp_calls_30d": int(_mcp_calls.get(p, 0)),
                "mcp_calls_30d_including_self_traffic": int(
                    _mcp_gross.get(p, _mcp_calls.get(p, 0))),
                "first_seen": str(row.get("first_seen", "")),
                "last_seen": str(row.get("last_seen", "")),
                "last_seen_ago": _time_ago(row.get("last_seen")),
            })

        return cors_jsonify({
            "success": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tracking_version": GATEWAY_VERSION,
            "summary": {
                "total_platforms": len(platforms),
                "total_requests_all_time": all_time,
                "requests_today": today_count,
                # Honest MCP numbers over the last 7 days.
                "mcp_7d_tool_call_attempts": (mcp_verified or {}).get("tool_call_attempts", 0),
                "mcp_7d_verified_tool_calls": (mcp_verified or {}).get("verified_tool_calls", 0),
                "mcp_7d_handshakes": (mcp_verified or {}).get("handshakes", 0),
                "mcp_7d_catalog_fetches": (mcp_verified or {}).get("catalog_fetches", 0),
            },
            "platforms": platforms,
            # ★ PUBLISHED, NEVER SILENT — see /api/ai/tracking for the rationale.
            "mcp_calls_excluded": _mcp_excluded,
            "chart_data": chart,
            "daily_stats": [
                {"date": str(r["date"]), "platform": r["platform"], "count": r["request_count"]}
                for r in daily
            ],
            "mcp_tools": mcp_tools,
            "mcp_methods": mcp_methods,
            "mcp_verified_7d": mcp_verified,
        })

    @app.route("/api/ai/track-request", methods=["POST", "OPTIONS"])
    def track_worker_request():
        """Receive fire-and-forget tracking from Cloudflare Worker."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            data = request.get_json(silent=True) or {}
            path = data.get("path", "")
            user_agent = data.get("user_agent", "")
            ip = data.get("ip", "")
            status = data.get("status_code", 200)
            elapsed = data.get("response_ms", 0)

            if not path:
                return cors_jsonify({"status": "skipped", "reason": "no path"})

            # webmcp-lane (2026-07-11): the worker forwards path+UA only (no
            # request headers), so the src=webmcp query marker in the path is
            # the WebMCP signal here. Additive — see is_webmcp_request().
            platform = (WEBMCP_PLATFORM if path_marks_webmcp(path)
                        else detect_platform(user_agent,
                                             data.get("referer", "") or ""))
            if platform in ("direct", "seo_bot"):
                return cors_jsonify({"status": "skipped", "platform": platform})

            log_ai_request(platform=platform, endpoint=path, user_agent=user_agent,
                           ip_address=ip, status_code=status, response_ms=elapsed)

            return cors_jsonify({"status": "tracked", "platform": platform})
        except Exception as e:
            return cors_jsonify({"status": "error", "msg": str(e)[:100]})

    @app.route("/api/v1/ai-tracking/log", methods=["POST", "OPTIONS"])
    def ai_tracking_log():
        """Log AI platform interactions — used by Cloudflare Worker and external callers."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            data = request.get_json(silent=True) or {}
            path = data.get("path", "") or data.get("endpoint", "")
            user_agent = data.get("user_agent", "") or data.get("ua", "")
            ip = data.get("ip", "") or data.get("ip_address", "")
            status = data.get("status_code", 200)
            elapsed = data.get("response_ms", 0)
            plat = data.get("platform", "")

            if not path and not plat:
                return cors_jsonify({"status": "skipped", "reason": "no path or platform"})

            # webmcp-lane (2026-07-11): explicit platform wins; otherwise the
            # src=webmcp path marker beats the UA detector (browser UA →
            # 'direct' → skipped). Additive — see is_webmcp_request().
            if plat:
                platform = plat
            elif path_marks_webmcp(path):
                platform = WEBMCP_PLATFORM
            else:
                platform = detect_platform(user_agent,
                                           data.get("referer", "") or "")
            if platform in ("direct", "unknown", "seo_bot"):
                return cors_jsonify({"status": "skipped", "platform": platform})

            log_ai_request(platform=platform, endpoint=path, user_agent=user_agent,
                           ip_address=ip, status_code=status, response_ms=elapsed)

            return cors_jsonify({"status": "tracked", "platform": platform})
        except Exception as e:
            return cors_jsonify({"status": "error", "msg": str(e)[:100]})

    @app.route("/api/ai/track-mcp", methods=["POST", "OPTIONS"])
    def track_mcp_call():
        """Track MCP tool calls from the gateway or Worker.

        Body fields (all optional except method):
          platform, method (required), user_agent, ip, tool_name, params,
          status_code, response_ms, client_name, client_version,
          protocol_version, success, error_message, body (raw JSON-RPC body —
          used to auto-extract tool_name when it wasn't passed explicitly)
        """
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            data = request.get_json(silent=True) or {}

            # Auto-extract tool_name from raw JSON-RPC body if not provided.
            tool_name = data.get("tool_name")
            if not tool_name:
                body = data.get("body") or {
                    "method": data.get("method"),
                    "params": data.get("params"),
                }
                tool_name = extract_tool_name(body)

            params_field = data.get("params")
            params_json = None
            if params_field is not None:
                try:
                    params_json = json.dumps(params_field)[:1000]
                except Exception:
                    params_json = str(params_field)[:1000]

            success = data.get("success")
            # Accept strings like "true"/"false"/"1"/"0" too.
            if isinstance(success, str):
                s = success.strip().lower()
                success = True if s in ("true", "1", "yes") else (False if s in ("false", "0", "no") else None)

            log_mcp_connection(
                platform=data.get("platform", "unknown"),
                method=data.get("method", ""),
                user_agent=data.get("user_agent", ""),
                ip_address=data.get("ip", ""),
                tool_name=tool_name,
                params=params_json,
                status_code=data.get("status_code"),
                response_ms=data.get("response_ms", 0),
                client_name=data.get("client_name"),
                client_version=data.get("client_version"),
                protocol_version=data.get("protocol_version"),
                success=success,
                error_message=data.get("error_message"),
            )
            return cors_jsonify({"status": "tracked", "tool_name": tool_name})
        except Exception as e:
            return cors_jsonify({"status": "error", "msg": str(e)[:100]})

    @app.route("/api/ai/recent", methods=["GET", "OPTIONS"])
    def ai_recent():
        """Recent AI activity feed — EXTERNAL platforms only.

        `activity` keeps its shape, so the existing consumer is unaffected.
        The added fields exist so an empty feed can be READ rather than
        guessed at: `error` non-null means broken; `excluded_internal` high
        with error null means our own traffic crowded the window, which is
        exactly what made this render "Quiet right now" while 40 agents were
        calling tools. ?include_internal=1 shows the unfiltered stream for
        debugging.
        """
        if request.method == "OPTIONS":
            return cors_jsonify({})
        limit = min(int(request.args.get("limit", 20)), 100)
        inc = (request.args.get("include_internal") or "").strip() in ("1", "true")
        feed = recent_activity_feed(limit, include_internal=inc)
        return cors_jsonify({
            "success": feed.get("error") is None,
            "activity": feed.get("activity") or [],
            "basis": feed.get("basis"),
            "scanned_recent": feed.get("scanned"),
            "excluded_internal": feed.get("excluded_internal"),
            "error": feed.get("error"),
        })

    @app.route("/api/ai/mcp-stats", methods=["GET", "OPTIONS"])
    def ai_mcp_stats():
        """Honest MCP usage report — verified tool calls vs handshakes/probes.

        Query param: ?days=7 (default)
        """
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            days = int(request.args.get("days", 7))
            days = max(1, min(days, 90))
        except Exception:
            days = 7

        return cors_jsonify({
            "success": True,
            "window_days": days,
            "summary": get_verified_call_totals(days),
            "by_method": get_mcp_method_breakdown(days),
            "by_tool": get_mcp_tool_stats(days),
        })

    @app.route("/api/ai/mcp-health", methods=["GET", "OPTIONS"])
    def ai_mcp_health():
        """
        v4.2 — MCP pipeline health check. Exposes:
          - schema drift between ai_tracking's expected columns and the live table
          - canary coverage: are tools/call rows actually getting tool_name filled in?
          - the honest verified-call totals for the window

        Wire this to a cron/uptime check. Anything other than
        status == 'ok' on BOTH schema and canary is a real alert.
        """
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            days = int(request.args.get("days", 7))
            days = max(1, min(days, 90))
        except Exception:
            days = 7

        health = get_mcp_health_summary(days=days)
        http_status = 200
        if health["schema"]["status"] == "drift" or health["canary"]["status"] == "broken":
            http_status = 503  # pageable
        elif health["canary"]["status"] == "degraded":
            http_status = 207  # multi-status / warn
        return cors_jsonify(health, status=http_status)

    @app.route("/api/ai/health", methods=["GET"])
    def ai_tracking_health():
        """Quick health check for the tracking system."""
        try:
            total = get_all_time_total()
            today = get_requests_today()
            return cors_jsonify({
                "status": "ok",
                "neon_connected": True,
                "total_tracked": total,
                "today": today,
                "version": GATEWAY_VERSION,
            })
        except Exception as e:
            return cors_jsonify({
                "status": "degraded",
                "neon_connected": False,
                "error": str(e)[:200],
                "version": GATEWAY_VERSION,
            })

    # Schedule 7d rolling update every 5 minutes
    def _periodic_7d_update():
        while True:
            time.sleep(300)
            try:
                update_7d_rolling()
            except Exception:
                pass

    threading.Thread(target=_periodic_7d_update, daemon=True, name="ai-7d-rolling").start()

    logger.info(f"AI Tracking v{GATEWAY_VERSION} initialized — writing to Neon PostgreSQL")
