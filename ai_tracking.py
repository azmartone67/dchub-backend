"""
DC Hub - AI Platform Tracking (Neon PostgreSQL Native)
======================================================
Drop-in replacement for the SQLite-based ai_tracking.py.
Writes directly to Neon PostgreSQL as primary store.
Falls back to local SQLite buffer if Neon is unreachable,
with a background sync thread to flush buffered rows to Neon.

Works identically on Railway (primary) and Replit (failover).

Usage in main.py:
    from ai_tracking import init_ai_tracking
    init_ai_tracking(app)

Requires: DATABASE_URL env var → Neon PostgreSQL connection string
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
GATEWAY_VERSION = "4.0.0"

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


def detect_platform(user_agent: str) -> str:
    """Identify AI platform from User-Agent string."""
    if not user_agent:
        return "direct"
    ua_lower = user_agent.lower()
    for platform_key, info in AI_PLATFORMS.items():
        for agent_sig in info["agents"]:
            if agent_sig.lower() in ua_lower:
                return platform_key
    # Check for generic MCP clients
    if "mcp" in ua_lower or "model-context-protocol" in ua_lower:
        return "mcp"
    # Check for generic bots
    if any(b in ua_lower for b in ["bot", "crawler", "spider", "scraper"]):
        return "seo_bot"
    return "direct"


def is_ai_endpoint(path: str) -> bool:
    """Check if path matches AI-relevant endpoint patterns."""
    return any(re.match(p, path) for p in AI_ENDPOINT_PATTERNS)


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
    conn.row_factory = sqlite3.Row
    conn.execute("""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buffered_mcp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            method TEXT,
            user_agent TEXT,
            ip_address TEXT,
            tool_name TEXT,
            params TEXT,
            status_code INTEGER DEFAULT 200,
            response_ms INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            synced INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════
#  DATABASE INITIALIZATION
# ═══════════════════════════════════════════════════════════════

def init_db():
    """Create tracking tables in Neon if they don't exist."""
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
        _execute("""
            CREATE TABLE IF NOT EXISTS mcp_connections (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50),
                method VARCHAR(100),
                user_agent VARCHAR(500),
                ip_address VARCHAR(50),
                tool_name VARCHAR(200),
                params TEXT,
                status_code INTEGER DEFAULT 200,
                response_ms INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Indexes
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_created ON ai_requests(created_at DESC)")
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_platform ON ai_requests(platform)")
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_daily_date ON ai_daily_stats(date)")
        _execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_created ON mcp_connections(created_at DESC)")

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

        logger.info("AI Tracking DB initialized (Neon PostgreSQL)")
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


def log_mcp_connection(platform, method, user_agent="", ip_address="",
                       tool_name="", params="", status_code=200, response_ms=0):
    """Record an MCP connection/tool call. Writes to Neon, falls back to SQLite buffer."""
    if not DATABASE_URL:
        return
    try:
        _execute("""
            INSERT INTO mcp_connections (platform, method, user_agent, ip_address, tool_name, params, status_code, response_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (platform, method, user_agent[:500] if user_agent else "",
              ip_address, tool_name, params[:1000] if params else "",
              status_code, response_ms, datetime.now(timezone.utc)))
    except Exception as e:
        logger.warning(f"MCP connection log failed: {e}")
        try:
            buf = _get_buffer_db()
            buf.execute("""
                INSERT INTO buffered_mcp (platform, method, user_agent, ip_address, tool_name, params, status_code, response_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (platform, method, user_agent[:500] if user_agent else "",
                  ip_address, tool_name, params[:1000] if params else "",
                  status_code, response_ms, datetime.now(timezone.utc).isoformat()))
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

            # Sync buffered requests
            rows = buf.execute(
                "SELECT * FROM buffered_requests WHERE synced = 0 ORDER BY id LIMIT 100"
            ).fetchall()

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
                    logger.warning(f"Buffer sync row failed: {e}")
                    break  # Stop on first Neon failure, retry next cycle

            if synced_ids:
                placeholders = ",".join("?" * len(synced_ids))
                buf.execute(f"UPDATE buffered_requests SET synced = 1 WHERE id IN ({placeholders})", synced_ids)
                buf.commit()
                logger.info(f"Synced {len(synced_ids)} buffered requests to Neon")

            # Sync buffered MCP connections
            mcp_rows = buf.execute(
                "SELECT * FROM buffered_mcp WHERE synced = 0 ORDER BY id LIMIT 100"
            ).fetchall()

            mcp_synced = []
            for row in mcp_rows:
                try:
                    _execute("""
                        INSERT INTO mcp_connections (platform, method, user_agent, ip_address, tool_name, params, status_code, response_ms, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (row["platform"], row["method"], row["user_agent"],
                          row["ip_address"], row["tool_name"], row["params"],
                          row["status_code"], row["response_ms"], row["created_at"]))
                    mcp_synced.append(row["id"])
                except Exception:
                    break

            if mcp_synced:
                placeholders = ",".join("?" * len(mcp_synced))
                buf.execute(f"UPDATE buffered_mcp SET synced = 1 WHERE id IN ({placeholders})", mcp_synced)
                buf.commit()

            # Clean up old synced rows (keep last 24h for debugging)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            buf.execute("DELETE FROM buffered_requests WHERE synced = 1 AND created_at < ?", (cutoff,))
            buf.execute("DELETE FROM buffered_mcp WHERE synced = 1 AND created_at < ?", (cutoff,))
            buf.commit()
            buf.close()

        except Exception as e:
            logger.error(f"Buffer sync cycle error: {e}")

        _sync_stop.wait(SYNC_INTERVAL_SECONDS)


def _start_sync_thread():
    """Start the background buffer sync thread."""
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
        return _execute(
            "SELECT platform, total_requests, first_seen, last_seen, requests_7d, name, color, company FROM ai_cumulative ORDER BY total_requests DESC",
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


def get_recent_activity(limit=20):
    try:
        return _execute(
            "SELECT platform, endpoint, created_at FROM ai_requests ORDER BY created_at DESC LIMIT %s",
            (limit,), fetchall=True
        ) or []
    except Exception:
        return []


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


def get_mcp_tool_stats(days=7):
    """Get MCP tool usage stats from the last N days."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return _execute("""
            SELECT tool_name, COUNT(*) as call_count, AVG(response_ms) as avg_ms,
                   COUNT(CASE WHEN status_code = 200 THEN 1 END) as success_count
            FROM mcp_connections
            WHERE created_at >= %s AND tool_name IS NOT NULL AND tool_name != ''
            GROUP BY tool_name ORDER BY call_count DESC
        """, (cutoff,), fetchall=True) or []
    except Exception:
        return []


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

    # Init Neon tables
    db_ok = init_db()
    if not db_ok:
        logger.warning("AI Tracking running in degraded mode (no Neon)")

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
            if not is_ai_endpoint(path):
                return response

            ua = request.headers.get("User-Agent", "")
            platform = detect_platform(ua)

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

        # Build platform list (exclude direct/seo_bot)
        platforms = []
        for row in cumulative:
            p = row.get("platform", "")
            if p in ("direct", "seo_bot", "media_crawler", "unknown_ai"):
                continue
            platforms.append({
                "platform": p,
                "name": row.get("name") or AI_PLATFORMS.get(p, {}).get("name", p.title()),
                "color": row.get("color") or AI_PLATFORMS.get(p, {}).get("color", "#64748b"),
                "company": row.get("company") or AI_PLATFORMS.get(p, {}).get("company", ""),
                "total_requests": row.get("total_requests", 0),
                "requests_7d": row.get("requests_7d", 0),
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
            },
            "platforms": platforms,
            "chart_data": chart,
            "daily_stats": [
                {"date": str(r["date"]), "platform": r["platform"], "count": r["request_count"]}
                for r in daily
            ],
            "mcp_tools": mcp_tools,
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

            platform = detect_platform(user_agent)
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

            platform = plat if plat else detect_platform(user_agent)
            if platform in ("direct", "unknown", "seo_bot"):
                return cors_jsonify({"status": "skipped", "platform": platform})

            log_ai_request(platform=platform, endpoint=path, user_agent=user_agent,
                           ip_address=ip, status_code=status, response_ms=elapsed)

            return cors_jsonify({"status": "tracked", "platform": platform})
        except Exception as e:
            return cors_jsonify({"status": "error", "msg": str(e)[:100]})

    @app.route("/api/ai/track-mcp", methods=["POST", "OPTIONS"])
    def track_mcp_call():
        """Track MCP tool calls from the gateway or Worker."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            data = request.get_json(silent=True) or {}
            log_mcp_connection(
                platform=data.get("platform", "unknown"),
                method=data.get("method", ""),
                user_agent=data.get("user_agent", ""),
                ip_address=data.get("ip", ""),
                tool_name=data.get("tool_name", ""),
                params=json.dumps(data.get("params", {}))[:1000],
                status_code=data.get("status_code", 200),
                response_ms=data.get("response_ms", 0),
            )
            return cors_jsonify({"status": "tracked"})
        except Exception as e:
            return cors_jsonify({"status": "error", "msg": str(e)[:100]})

    @app.route("/api/ai/recent", methods=["GET", "OPTIONS"])
    def ai_recent():
        """Recent AI activity feed."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        limit = min(int(request.args.get("limit", 20)), 100)
        return cors_jsonify({
            "success": True,
            "activity": get_recent_activity(limit),
        })

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