"""
DC Hub - Persistent AI Platform Tracking
==========================================
Drop-in module for cumulative AI agent request tracking.
Stores all requests in SQLite so counts survive Replit restarts.

Usage in main.py:
    from ai_tracking import init_ai_tracking
    init_ai_tracking(app)

That's it. All existing /api/v1/ai-tracking/* routes are replaced
with persistent versions, and a middleware auto-logs every request
that looks like it came from an AI platform.
"""

import os
import re
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g
from db_utils import get_db

# ─── Database path ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_tracking.db")

# ─── Known AI platforms & their User-Agent signatures ──────────
AI_PLATFORMS = {
    "claude":      {"name": "Claude",      "company": "Anthropic",  "color": "#d97706", "ua_patterns": ["claude", "anthropic"]},
    "chatgpt":     {"name": "ChatGPT",     "company": "OpenAI",     "color": "#10b981", "ua_patterns": ["chatgpt", "openai", "gptbot"]},
    "gemini":      {"name": "Gemini",       "company": "Google",     "color": "#4285f4", "ua_patterns": ["gemini", "google-extended", "googleother"]},
    "perplexity":  {"name": "Perplexity",   "company": "Perplexity", "color": "#06b6d4", "ua_patterns": ["perplexity", "perplexitybot"]},
    "grok":        {"name": "Grok",         "company": "xAI",        "color": "#ef4444", "ua_patterns": ["grok", "xai"]},
    "copilot":     {"name": "Copilot",      "company": "Microsoft",  "color": "#8b5cf6", "ua_patterns": ["copilot", "bingbot", "microsoft"]},
    "groq":        {"name": "Groq",         "company": "Groq",       "color": "#f97316", "ua_patterns": ["groq"]},
    "youcom":      {"name": "You.com",      "company": "You.com",    "color": "#eab308", "ua_patterns": ["you.com", "youchat", "youbot"]},
    "poe":         {"name": "Poe",          "company": "Quora",      "color": "#a855f7", "ua_patterns": ["poe", "quora"]},
    "meta":        {"name": "Meta AI",      "company": "Meta",       "color": "#3b82f6", "ua_patterns": ["meta-externalagent", "facebookexternalhit", "meta.ai"]},
    "cohere":      {"name": "Cohere",       "company": "Cohere",     "color": "#14b8a6", "ua_patterns": ["cohere"]},
    "huggingface": {"name": "HuggingFace",  "company": "Hugging Face","color": "#fbbf24", "ua_patterns": ["huggingface", "hugging"]},
    "deepseek":    {"name": "DeepSeek",     "company": "DeepSeek",   "color": "#6366f1", "ua_patterns": ["deepseek"]},
    "mistral":     {"name": "Mistral",      "company": "Mistral AI", "color": "#f43f5e", "ua_patterns": ["mistral"]},
}

# Endpoints that count as AI-relevant (used for "direct" traffic attribution)
AI_ENDPOINT_PATTERNS = [
    r"^/api/ai/",
    r"^/api/v1/ai",
    r"^/api/v1/discovery",
    r"^/api/market-report",
    r"^/api/news",
    r"^/api/v1/deals",
    r"^/api/v1/pipeline",
    r"^/api/v2/infrastructure",
    r"^/api/v1/energy",
    r"^/a2a/",
    r"^/ai/",
    r"^/llms\.txt",
    r"^/llms-full\.txt",
    r"^/AGENTS\.md",
    r"^/\.well-known/",
    r"^/api/facilities",
    r"^/api/intelligence",
    r"^/robots\.txt",
    r"^/sitemap",
    r"^/api/v1/stats",
    r"^/api/ecosystem",
    r"^/api/health",
    r"^/health",
    r"^/api/v1/facilities",
    r"^/api/grid/",
    r"^/api/site-score",
    r"^/$",
    r"^/for-ai",
]


# ═══════════════════════════════════════════════════════════════
#  DATABASE SETUP
# ═══════════════════════════════════════════════════════════════

def get_tracking_db():
    """Get a thread-local database connection."""
    if not hasattr(g, '_ai_tracking_db'):
        g._ai_tracking_db = get_db()
    return g._ai_tracking_db


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            user_agent  TEXT,
            ip_address  TEXT,
            status_code INTEGER,
            response_ms INTEGER,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ai_requests_platform
            ON ai_requests(platform);

        CREATE INDEX IF NOT EXISTS idx_ai_requests_created
            ON ai_requests(created_at);

        CREATE INDEX IF NOT EXISTS idx_ai_requests_platform_created
            ON ai_requests(platform, created_at);

        -- Daily rollup table for fast chart queries
        CREATE TABLE IF NOT EXISTS ai_daily_stats (
            date        TEXT NOT NULL,
            platform    TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, platform)
        );

        -- All-time cumulative totals (single source of truth)
        CREATE TABLE IF NOT EXISTS ai_cumulative (
            platform    TEXT PRIMARY KEY,
            total_requests INTEGER NOT NULL DEFAULT 0,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    print("   📊 AI Tracking DB initialized at", DB_PATH)


# ═══════════════════════════════════════════════════════════════
#  PLATFORM DETECTION
# ═══════════════════════════════════════════════════════════════

def detect_platform(user_agent: str, query_params: dict = None) -> str:
    """
    Identify which AI platform made the request.
    Checks User-Agent first, then falls back to query params like ?platform=claude.
    """
    ua_lower = (user_agent or "").lower()

    # Check User-Agent patterns
    for key, info in AI_PLATFORMS.items():
        for pattern in info["ua_patterns"]:
            if pattern in ua_lower:
                return key

    # Check explicit ?platform= or ?source= param
    if query_params:
        explicit = (
            query_params.get("platform", "") or
            query_params.get("source", "") or
            query_params.get("agent", "")
        ).lower().strip()
        if explicit:
            for key, info in AI_PLATFORMS.items():
                if explicit == key or explicit == info["name"].lower():
                    return key

    # Check for generic bot patterns that might be AI crawlers
    bot_patterns = ["bot", "crawler", "spider", "scraper", "fetch", "ai", "llm"]
    if any(p in ua_lower for p in bot_patterns):
        return "unknown_ai"

    return "unknown"


def is_ai_endpoint(path: str) -> bool:
    """Check if the request path is an AI-relevant endpoint."""
    for pattern in AI_ENDPOINT_PATTERNS:
        if re.match(pattern, path):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

def log_ai_request(platform: str, endpoint: str, user_agent: str = "",
                   ip_address: str = "", status_code: int = 200,
                   response_ms: int = 0):
    """
    Record a single AI request and update all cumulative counters.
    Thread-safe via SQLite WAL mode.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    try:
        conn = get_db()
        # 1. Insert raw request log
        conn.execute("""
            INSERT INTO ai_requests (platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (platform, endpoint, user_agent, ip_address, status_code, response_ms, now.isoformat()))

        # 2. Upsert daily rollup
        conn.execute("""
            INSERT INTO ai_daily_stats (date, platform, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(date, platform) DO UPDATE SET request_count = request_count + 1
        """, (today, platform))

        # 3. Upsert cumulative totals
        conn.execute("""
            INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                total_requests = total_requests + 1,
                last_seen = ?
        """, (platform, now.isoformat(), now.isoformat(), now.isoformat()))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AI Tracking] Error logging request: {e}")


# ═══════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════

def get_cumulative_totals():
    """Get all-time totals per platform."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT platform, total_requests, first_seen, last_seen
        FROM ai_cumulative
        ORDER BY total_requests DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_stats(days: int = 7):
    """Get daily breakdown for the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT date, platform, request_count
        FROM ai_daily_stats
        WHERE date >= ?
        ORDER BY date DESC, request_count DESC
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_requests_today():
    """Get total request count for today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_db()
    result = conn.execute("""
        SELECT COALESCE(SUM(request_count), 0) as total
        FROM ai_daily_stats
        WHERE date = ?
    """, (today,)).fetchone()
    conn.close()
    return result[0] if result else 0


def get_all_time_total():
    """Get grand total of all AI requests ever."""
    conn = get_db()
    result = conn.execute("""
        SELECT COALESCE(SUM(total_requests), 0) as total
        FROM ai_cumulative
    """).fetchone()
    conn.close()
    return result[0] if result else 0


def get_recent_activity(limit: int = 20):
    """Get the most recent AI requests."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT platform, endpoint, user_agent, created_at
        FROM ai_requests
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_platform_chart_data(days: int = 7):
    """
    Build chart-ready data: per-platform totals for the last N days.
    Returns cumulative totals (what you want for the bar chart).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_db()
    conn.row_factory = sqlite3.Row

    # Sum each platform's requests over the period
    rows = conn.execute("""
        SELECT platform, SUM(request_count) as total
        FROM ai_daily_stats
        WHERE date >= ?
        GROUP BY platform
        ORDER BY total DESC
    """, (cutoff,)).fetchall()
    conn.close()

    result = {}
    for r in rows:
        p = r["platform"]
        if p in AI_PLATFORMS:
            result[p] = {
                "name": AI_PLATFORMS[p]["name"],
                "color": AI_PLATFORMS[p]["color"],
                "requests_7d": r["total"],
            }

    # Include platforms with zero recent requests
    for key, info in AI_PLATFORMS.items():
        if key not in result:
            result[key] = {
                "name": info["name"],
                "color": info["color"],
                "requests_7d": 0,
            }

    return result


def get_hourly_breakdown(hours: int = 24):
    """Get request counts per hour for the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT strftime('%Y-%m-%d %H:00', created_at) as hour, 
               platform, 
               COUNT(*) as count
        FROM ai_requests
        WHERE created_at >= ?
        GROUP BY hour, platform
        ORDER BY hour DESC
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
#  FLASK INTEGRATION
# ═══════════════════════════════════════════════════════════════

def init_ai_tracking(app: Flask):
    """
    Initialize the AI tracking system. Call this once in main.py:
        from ai_tracking import init_ai_tracking
        init_ai_tracking(app)
    """
    init_db()

    # ── Close DB connections at end of request ──────────────
    @app.teardown_appcontext
    def close_ai_tracking_db(exception):
        db = getattr(g, '_ai_tracking_db', None)
        if db is not None:
            db.close()

    # ── Auto-log middleware ─────────────────────────────────
    @app.after_request
    def track_ai_request(response):
        """Automatically log requests from AI platforms.
        
        Logic:
        - Recognized AI platform (by User-Agent)? ALWAYS log, any endpoint.
        - Unknown UA but hitting an AI endpoint? Log as 'direct'.
        - Generic bot hitting AI endpoint? Log as 'unknown_ai'.
        - Everything else? Skip.
        """
        try:
            path = request.path
            ua = request.headers.get("User-Agent", "")
            platform = detect_platform(ua, dict(request.args))

            # ALWAYS log recognized AI platforms, regardless of endpoint
            if platform not in ("unknown", "unknown_ai"):
                ip = request.headers.get("X-Forwarded-For", request.remote_addr)
                if ip and "," in ip:
                    ip = ip.split(",")[0].strip()
                response_ms = 0
                if hasattr(g, '_request_start_time'):
                    response_ms = int((datetime.now(timezone.utc).timestamp() - g._request_start_time) * 1000)
                log_ai_request(
                    platform=platform,
                    endpoint=path,
                    user_agent=ua[:500],
                    ip_address=ip or "",
                    status_code=response.status_code,
                    response_ms=response_ms
                )
                return response

            # For unknown/unknown_ai, only log if hitting an AI-relevant endpoint
            if is_ai_endpoint(path):
                if platform == "unknown_ai":
                    label = "unknown_ai"
                else:
                    label = "direct"
                ip = request.headers.get("X-Forwarded-For", request.remote_addr)
                if ip and "," in ip:
                    ip = ip.split(",")[0].strip()
                response_ms = 0
                if hasattr(g, '_request_start_time'):
                    response_ms = int((datetime.now(timezone.utc).timestamp() - g._request_start_time) * 1000)
                log_ai_request(
                    platform=label,
                    endpoint=path,
                    user_agent=ua[:500],
                    ip_address=ip or "",
                    status_code=response.status_code,
                    response_ms=response_ms
                )

        except Exception as e:
            print(f"[AI Tracking] Middleware error: {e}")

        return response

    @app.before_request
    def start_timer():
        g._request_start_time = datetime.now(timezone.utc).timestamp()

    # ── API Routes ──────────────────────────────────────────

    @app.route("/api/v1/ai-tracking/stats")
    def ai_tracking_stats():
        """
        Main stats endpoint — returns everything the frontend needs.
        Cumulative totals, daily breakdown, and recent activity.
        """
        days = request.args.get("days", 7, type=int)
        cumulative = get_cumulative_totals()
        daily = get_daily_stats(days)
        chart = get_platform_chart_data(days)
        recent = get_recent_activity(20)
        today_count = get_requests_today()
        all_time = get_all_time_total()

        # Build platform summary with cumulative totals
        platforms = {}
        for entry in cumulative:
            p = entry["platform"]
            if p in AI_PLATFORMS:
                platforms[p] = {
                    "name": AI_PLATFORMS[p]["name"],
                    "company": AI_PLATFORMS[p]["company"],
                    "color": AI_PLATFORMS[p]["color"],
                    "total_requests": entry["total_requests"],
                    "first_seen": entry["first_seen"],
                    "last_seen": entry["last_seen"],
                    "requests_7d": chart.get(p, {}).get("requests_7d", 0),
                }

        # Include platforms with zero requests
        for key, info in AI_PLATFORMS.items():
            if key not in platforms:
                platforms[key] = {
                    "name": info["name"],
                    "company": info["company"],
                    "color": info["color"],
                    "total_requests": 0,
                    "first_seen": None,
                    "last_seen": None,
                    "requests_7d": 0,
                }

        # Format recent activity with time-ago
        recent_formatted = []
        for r in recent:
            p = r["platform"]
            info = AI_PLATFORMS.get(p, {"name": p.title(), "color": "#64748b"})
            recent_formatted.append({
                "platform": info["name"],
                "platform_key": p,
                "endpoint": r["endpoint"],
                "color": info.get("color", "#64748b"),
                "timestamp": r["created_at"],
                "time_ago": _time_ago(r["created_at"]),
            })

        return jsonify({
            "status": "live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_requests_today": today_count,
            "total_requests_all_time": all_time,
            "platforms_active": len([p for p in platforms.values() if p["total_requests"] > 0]),
            "platforms_tracked": len(AI_PLATFORMS),
            "platforms": platforms,
            "chart_data": chart,
            "daily_breakdown": daily,
            "recent_activity": recent_formatted,
            "tracking": "persistent",
            "note": "Cumulative totals stored in SQLite — survives restarts"
        })

    @app.route("/api/v1/ai-tracking/chart")
    def ai_tracking_chart():
        """Chart-specific endpoint for the 7-day bar chart."""
        days = request.args.get("days", 7, type=int)
        chart = get_platform_chart_data(days)

        # Sort by request count descending
        sorted_platforms = sorted(chart.items(), key=lambda x: x[1]["requests_7d"], reverse=True)

        return jsonify({
            "days": days,
            "platforms": [
                {
                    "key": key,
                    "name": data["name"],
                    "color": data["color"],
                    "requests": data["requests_7d"],
                }
                for key, data in sorted_platforms
            ],
            "total": sum(d["requests_7d"] for d in chart.values()),
        })

    @app.route("/api/v1/ai-tracking/cumulative")
    def ai_tracking_cumulative():
        """All-time cumulative totals per platform."""
        totals = get_cumulative_totals()
        return jsonify({
            "all_time_total": get_all_time_total(),
            "platforms": totals,
        })

    @app.route("/api/v1/ai-tracking/recent")
    def ai_tracking_recent():
        """Recent activity feed."""
        limit = request.args.get("limit", 20, type=int)
        recent = get_recent_activity(min(limit, 100))

        formatted = []
        for r in recent:
            p = r["platform"]
            info = AI_PLATFORMS.get(p, {"name": p.title(), "color": "#64748b"})
            formatted.append({
                "platform": info["name"],
                "platform_key": p,
                "endpoint": r["endpoint"],
                "color": info.get("color", "#64748b"),
                "timestamp": r["created_at"],
                "time_ago": _time_ago(r["created_at"]),
            })

        return jsonify({"recent": formatted})

    @app.route("/api/v1/ai-tracking/hourly")
    def ai_tracking_hourly():
        """Hourly breakdown for the last 24h."""
        return jsonify({"hourly": get_hourly_breakdown(24)})

    @app.route("/api/v1/ai-tracking/health")
    def ai_tracking_health():
        """Health check for the tracking system."""
        try:
            conn = get_db()
            total = conn.execute("SELECT COUNT(*) FROM ai_requests").fetchone()[0]
            platforms = conn.execute("SELECT COUNT(DISTINCT platform) FROM ai_cumulative WHERE total_requests > 0").fetchone()[0]
            conn.close()
            return jsonify({
                "status": "healthy",
                "db_path": DB_PATH,
                "total_logged": total,
                "platforms_seen": platforms,
                "tracking_mode": "persistent_sqlite",
            })
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500

    # Also register as /ai/tracking for backward compatibility
    @app.route("/ai/tracking")
    def ai_tracking_compat():
        """Legacy compat — redirects to the stats endpoint."""
        return ai_tracking_stats()

    @app.route("/api/ai/tracking")
    def ai_tracking_compat2():
        """Legacy compat v2."""
        return ai_tracking_stats()

    print("   📊 AI Tracking: Persistent SQLite tracking loaded")
    print(f"   📊 AI Tracking: DB at {DB_PATH}")
    print(f"   📊 AI Tracking: Monitoring {len(AI_PLATFORMS)} platforms")


# ═══════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════

def _time_ago(iso_str: str) -> str:
    """Convert ISO timestamp to human-readable 'X ago' string."""
    try:
        then = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = now - then

        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            mins = seconds // 60
            return f"{mins}m ago"
        elif seconds < 86400:
            hours = seconds // 3600
            return f"{hours}h ago"
        else:
            days = seconds // 86400
            return f"{days}d ago"
    except Exception:
        return "recently"
