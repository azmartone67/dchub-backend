"""
DC Hub - Persistent AI Platform Tracking (Railway/Neon Edition)
================================================================
Drop-in module for cumulative AI agent request tracking.
Writes directly to Neon PostgreSQL — no SQLite, no ephemeral storage issues.

Usage in main.py:
    from ai_tracking import init_ai_tracking
    init_ai_tracking(app)

Requires: DATABASE_URL env var pointing to your Neon PostgreSQL instance.
"""

import os
import re
import json
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, make_response

# ─── Neon PostgreSQL connection ──────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pg_pool = None
_pg_lock = threading.Lock()


def _get_pg():
    """Get a psycopg2 connection to Neon with auto-reconnect."""
    global _pg_pool
    import psycopg2
    try:
        if _pg_pool is None or _pg_pool.closed:
            with _pg_lock:
                if _pg_pool is None or _pg_pool.closed:
                    _pg_pool = psycopg2.connect(DATABASE_URL, connect_timeout=5)
                    _pg_pool.autocommit = False
        # Test connection is alive
        try:
            _pg_pool.cursor().execute("SELECT 1")
        except Exception:
            _pg_pool = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            _pg_pool.autocommit = False
        return _pg_pool
    except Exception as e:
        print(f"[AI Tracking] Neon connection failed: {e}")
        raise


def _execute(sql, params=None, fetch=False, fetchall=False):
    """Execute SQL against Neon with auto-reconnect."""
    conn = _get_pg()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if fetchall:
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            conn.commit()
            return [dict(zip(cols, r)) for r in rows]
        elif fetch:
            cols = [d[0] for d in cur.description] if cur.description else []
            row = cur.fetchone()
            conn.commit()
            return dict(zip(cols, row)) if row else None
        else:
            conn.commit()
            return None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise e


def cors_jsonify(data, status=200):
    """Return a JSON response with CORS headers for cross-origin access."""
    resp = make_response(jsonify(data), status)
    origin = request.headers.get('Origin', '')
    allowed = ['https://dchub.cloud', 'https://www.dchub.cloud', 'http://localhost:3000']
    if origin in allowed:
        resp.headers['Access-Control-Allow-Origin'] = origin
    else:
        resp.headers['Access-Control-Allow-Origin'] = 'https://dchub.cloud'
    resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With'
    return resp


# ─── Known AI platforms & their User-Agent signatures ──────────
AI_PLATFORMS = {
    "claude":      {"name": "Claude",      "company": "Anthropic",   "color": "#d97706", "ua_patterns": ["claude", "anthropic"]},
    "chatgpt":     {"name": "ChatGPT",     "company": "OpenAI",      "color": "#10b981", "ua_patterns": ["chatgpt", "openai", "gptbot"]},
    "gemini":      {"name": "Gemini",      "company": "Google",      "color": "#4285f4", "ua_patterns": ["gemini", "google-extended", "googleother"]},
    "perplexity":  {"name": "Perplexity",  "company": "Perplexity",  "color": "#06b6d4", "ua_patterns": ["perplexity", "perplexitybot"]},
    "grok":        {"name": "Grok",        "company": "xAI",         "color": "#ef4444", "ua_patterns": ["grok", "xai"]},
    "copilot":     {"name": "Copilot",     "company": "Microsoft",   "color": "#8b5cf6", "ua_patterns": ["copilot", "bingbot", "microsoft"]},
    "groq":        {"name": "Groq",        "company": "Groq",        "color": "#f97316", "ua_patterns": ["groq"]},
    "youcom":      {"name": "You.com",     "company": "You.com",     "color": "#eab308", "ua_patterns": ["you.com", "youchat", "youbot"]},
    "poe":         {"name": "Poe",         "company": "Quora",       "color": "#a855f7", "ua_patterns": ["poe", "quora"]},
    "meta":        {"name": "Meta AI",     "company": "Meta",        "color": "#3b82f6", "ua_patterns": ["meta-externalagent", "facebookexternalhit", "meta.ai"]},
    "cohere":      {"name": "Cohere",      "company": "Cohere",      "color": "#14b8a6", "ua_patterns": ["cohere"]},
    "huggingface": {"name": "HuggingFace", "company": "Hugging Face","color": "#fbbf24", "ua_patterns": ["huggingface", "hugging"]},
    "deepseek":    {"name": "DeepSeek",    "company": "DeepSeek",    "color": "#6366f1", "ua_patterns": ["deepseek"]},
    "mistral":     {"name": "Mistral",     "company": "Mistral AI",  "color": "#f43f5e", "ua_patterns": ["mistral", "le-chat", "lechat"]},
}

# ─── SEO bots / media crawlers (classified separately from AI) ─
SEO_BOTS = {
    "awariosmartbot": "seo_bot", "awariobot": "seo_bot", "googlebot": "seo_bot",
    "adsbot-google": "seo_bot", "bravebot": "seo_bot", "amazonbot": "seo_bot",
    "serankingbot": "seo_bot", "seranking": "seo_bot", "mj12bot": "seo_bot",
    "majestic": "seo_bot", "ahrefsbot": "seo_bot", "semrushbot": "seo_bot",
    "dotbot": "seo_bot", "yandexbot": "seo_bot", "baiduspider": "seo_bot",
    "sogou": "seo_bot", "ev-crawler": "media_crawler", "headline.com": "media_crawler",
    "newsbot": "media_crawler",
}

UNKNOWN_AI_INDICATORS = re.compile(
    r"(?:^|[\s/\-_.])"
    r"(?:ai|llm|agent|mcp|gpt|claude|copilot|assistant|chatbot|"
    r"model|inference|embedding|genai|langchain|autogpt)"
    r"(?:$|[\s/\-_.])", re.IGNORECASE
)

# Endpoints that count as AI-relevant (used for "direct" traffic attribution)
AI_ENDPOINT_PATTERNS = [
    r"^/api/ai/", r"^/api/v1/ai", r"^/api/v1/discovery", r"^/api/market-report",
    r"^/api/news", r"^/api/v1/deals", r"^/api/v1/pipeline", r"^/api/v2/infrastructure",
    r"^/api/v1/energy", r"^/a2a/", r"^/ai/", r"^/llms\.txt", r"^/llms-full\.txt",
    r"^/AGENTS\.md", r"^/\.well-known/", r"^/api/facilities", r"^/api/intelligence",
    r"^/robots\.txt", r"^/sitemap", r"^/api/v1/stats", r"^/api/ecosystem",
    r"^/api/health", r"^/health", r"^/api/v1/facilities", r"^/api/grid/",
    r"^/api/site-score", r"^/$", r"^/for-ai",
]


# ═══════════════════════════════════════════════════════════════
#  PLATFORM DETECTION
# ═══════════════════════════════════════════════════════════════

def detect_platform(user_agent: str, query_params: dict = None) -> str:
    """
    Identify which AI platform made the request.
    Priority: AI platforms > SEO bots > media crawlers > unknown_ai > unknown.
    """
    ua_lower = (user_agent or "").lower()

    # 1. Check known AI platforms first (highest priority)
    for key, info in AI_PLATFORMS.items():
        for pattern in info["ua_patterns"]:
            if pattern in ua_lower:
                return key

    # 2. Check explicit ?platform= or ?source= param
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

    # 3. Check SEO bots and media crawlers
    for bot_pattern, category in SEO_BOTS.items():
        if bot_pattern in ua_lower:
            return category

    # 4. Only flag as "unknown_ai" if UA contains actual AI-related terms
    if UNKNOWN_AI_INDICATORS.search(ua_lower):
        return "unknown_ai"

    # 5. Generic bots/crawlers without AI indicators
    if any(p in ua_lower for p in ["bot", "crawler", "spider", "scraper", "fetch"]):
        return "generic_bot"

    return "unknown"


def is_ai_endpoint(path: str) -> bool:
    """Check if the request path is an AI-relevant endpoint."""
    return any(re.match(p, path) for p in AI_ENDPOINT_PATTERNS)


# ═══════════════════════════════════════════════════════════════
#  DATABASE SETUP (Neon PostgreSQL)
# ═══════════════════════════════════════════════════════════════

def init_db():
    """Create tracking tables in Neon if they don't exist."""
    if not DATABASE_URL:
        print("   ⚠️  AI Tracking: No DATABASE_URL set — tracking disabled")
        return False

    try:
        _execute("""
            CREATE TABLE IF NOT EXISTS ai_requests (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50) NOT NULL,
                endpoint VARCHAR(500) NOT NULL,
                user_agent VARCHAR(500),
                ip_address VARCHAR(50),
                status_code INTEGER,
                response_ms INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        _execute("""
            CREATE TABLE IF NOT EXISTS ai_daily_stats (
                date DATE NOT NULL,
                platform VARCHAR(50) NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, platform)
            )
        """)
        _execute("""
            CREATE TABLE IF NOT EXISTS ai_cumulative (
                platform VARCHAR(50) PRIMARY KEY,
                total_requests INTEGER NOT NULL DEFAULT 0,
                first_seen TIMESTAMPTZ NOT NULL,
                last_seen TIMESTAMPTZ NOT NULL,
                requests_7d INTEGER DEFAULT 0,
                name VARCHAR(100),
                color VARCHAR(20),
                company VARCHAR(100)
            )
        """)
        _execute("""
            CREATE TABLE IF NOT EXISTS mcp_connections (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50) NOT NULL,
                method VARCHAR(100) NOT NULL,
                user_agent VARCHAR(500),
                ip_address VARCHAR(50),
                tool_name VARCHAR(200),
                params TEXT,
                status_code INTEGER,
                response_ms INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Indexes for performance
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_platform ON ai_requests(platform)")
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_created ON ai_requests(created_at)")
        _execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_platform_created ON ai_requests(platform, created_at)")
        _execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_created ON mcp_connections(created_at)")
        _execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_platform ON mcp_connections(platform)")

        print("   📊 AI Tracking DB initialized (Neon PostgreSQL)")
        return True
    except Exception as e:
        print(f"   ❌ AI Tracking DB init failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

def log_ai_request(platform, endpoint, user_agent="", ip_address="",
                   status_code=200, response_ms=0):
    """Record a single AI request directly in Neon."""
    if not DATABASE_URL:
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    try:
        _execute("""
            INSERT INTO ai_requests (platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (platform, endpoint, user_agent[:500], ip_address, status_code, response_ms, now))

        _execute("""
            INSERT INTO ai_daily_stats (date, platform, request_count)
            VALUES (%s, %s, 1)
            ON CONFLICT(date, platform) DO UPDATE SET request_count = ai_daily_stats.request_count + 1
        """, (today, platform))

        _execute("""
            INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen, requests_7d, name, color, company)
            VALUES (%s, 1, %s, %s, 1, %s, %s, %s)
            ON CONFLICT(platform) DO UPDATE SET
                total_requests = ai_cumulative.total_requests + 1,
                last_seen = EXCLUDED.last_seen,
                name = EXCLUDED.name,
                color = EXCLUDED.color,
                company = EXCLUDED.company
        """, (platform, now, now,
              AI_PLATFORMS.get(platform, {}).get("name", platform.title()),
              AI_PLATFORMS.get(platform, {}).get("color", "#64748b"),
              AI_PLATFORMS.get(platform, {}).get("company", "")))
    except Exception as e:
        print(f"[AI Tracking] Error logging request: {e}")


def log_mcp_connection(platform, method, user_agent="", ip_address="",
                       tool_name="", params="", status_code=200, response_ms=0):
    """Log an MCP JSON-RPC connection with method details."""
    if not DATABASE_URL:
        return
    try:
        _execute("""
            INSERT INTO mcp_connections (platform, method, user_agent, ip_address, tool_name, params, status_code, response_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (platform, method, user_agent[:500], ip_address, tool_name,
              params[:1000] if params else "", status_code, response_ms,
              datetime.now(timezone.utc)))
    except Exception as e:
        print(f"[AI Tracking] Error logging MCP connection: {e}")


# ═══════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════

def get_cumulative_totals():
    try:
        return _execute(
            "SELECT platform, total_requests, first_seen, last_seen FROM ai_cumulative ORDER BY total_requests DESC",
            fetchall=True) or []
    except Exception:
        return []


def get_daily_stats(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        return _execute(
            "SELECT date, platform, request_count FROM ai_daily_stats WHERE date >= %s ORDER BY date DESC, request_count DESC",
            (cutoff,), fetchall=True) or []
    except Exception:
        return []


def get_requests_today():
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = _execute("SELECT COALESCE(SUM(request_count), 0) as total FROM ai_daily_stats WHERE date = %s",
                     (today,), fetch=True)
        return r["total"] if r else 0
    except Exception:
        return 0


def get_all_time_total():
    try:
        r = _execute("SELECT COALESCE(SUM(total_requests), 0) as total FROM ai_cumulative", fetch=True)
        return r["total"] if r else 0
    except Exception:
        return 0


def get_recent_activity(limit=20):
    try:
        return _execute(
            "SELECT platform, endpoint, user_agent, created_at FROM ai_requests ORDER BY created_at DESC LIMIT %s",
            (limit,), fetchall=True) or []
    except Exception:
        return []


def get_platform_chart_data(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = _execute(
            "SELECT platform, SUM(request_count) as total FROM ai_daily_stats WHERE date >= %s GROUP BY platform ORDER BY total DESC",
            (cutoff,), fetchall=True) or []
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
                result[key] = {"name": info["name"], "color": info["color"], "requests_7d": 0}
        return result
    except Exception:
        return {}


def get_hourly_breakdown(hours=24):
    """Get request counts per hour for the last N hours."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return _execute("""
            SELECT to_char(created_at, 'YYYY-MM-DD HH24:00') as hour,
                   platform,
                   COUNT(*) as count
            FROM ai_requests
            WHERE created_at >= %s
            GROUP BY hour, platform
            ORDER BY hour DESC
        """, (cutoff,), fetchall=True) or []
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════

def _time_ago(ts_input) -> str:
    """Convert timestamp to human-readable 'X ago' string."""
    try:
        if isinstance(ts_input, str):
            then = datetime.fromisoformat(ts_input.replace("Z", "+00:00"))
        else:
            then = ts_input
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - then
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            return f"{seconds // 60}m ago"
        elif seconds < 86400:
            return f"{seconds // 3600}h ago"
        else:
            return f"{seconds // 86400}d ago"
    except Exception:
        return "recently"


# ═══════════════════════════════════════════════════════════════
#  FLASK INTEGRATION
# ═══════════════════════════════════════════════════════════════

def init_ai_tracking(app: Flask):
    """
    Initialize the AI tracking system. Call this once in main.py:
        from ai_tracking import init_ai_tracking
        init_ai_tracking(app)
    """
    db_ok = init_db()

    # ── Auto-log middleware ─────────────────────────────────
    @app.after_request
    def track_ai_request(response):
        """Automatically log requests from AI platforms.

        Logic:
        - Recognized AI platform (by User-Agent)? ALWAYS log, any endpoint.
        - SEO bots / media crawlers? Log with their category.
        - MCP POST requests? Log JSON-RPC method in mcp_connections table.
        - Unknown UA but hitting an AI endpoint? Log as 'direct'.
        - Genuine unknown_ai (UA has AI terms)? Log as 'unknown_ai'.
        - Generic bots without AI indicators? Skip (not interesting).
        - Everything else? Skip.
        """
        if not DATABASE_URL:
            return response
        try:
            path = request.path
            ua = request.headers.get("User-Agent", "")
            platform = detect_platform(ua, dict(request.args))

            ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if ip and "," in ip:
                ip = ip.split(",")[0].strip()
            response_ms = 0
            if hasattr(g, '_request_start_time'):
                response_ms = int((datetime.now(timezone.utc).timestamp() - g._request_start_time) * 1000)

            # Track MCP POST requests with JSON-RPC method details
            if path.startswith("/mcp") and request.method == "POST":
                mcp_method = ""
                tool_name = ""
                params_str = ""
                try:
                    body = request.get_json(silent=True) or {}
                    mcp_method = body.get("method", "unknown")
                    params = body.get("params", {})
                    if mcp_method == "tools/call":
                        tool_name = params.get("name", "")
                        params_str = json.dumps(params.get("arguments", {}))[:1000]
                    elif mcp_method == "tools/list":
                        pass
                    elif mcp_method == "initialize":
                        client_info = params.get("clientInfo", {})
                        params_str = json.dumps(client_info)[:1000]
                except Exception:
                    mcp_method = "parse_error"
                mcp_platform = platform if platform in AI_PLATFORMS else (platform if platform == "mcp" else "mcp")
                log_mcp_connection(
                    platform=mcp_platform, method=mcp_method, user_agent=ua[:500],
                    ip_address=ip or "", tool_name=tool_name, params=params_str,
                    status_code=response.status_code, response_ms=response_ms
                )
                log_ai_request(
                    platform=mcp_platform, endpoint=path, user_agent=ua[:500],
                    ip_address=ip or "", status_code=response.status_code, response_ms=response_ms
                )
                return response

            # ALWAYS log recognized AI platforms, regardless of endpoint
            if platform in AI_PLATFORMS:
                log_ai_request(
                    platform=platform, endpoint=path, user_agent=ua[:500],
                    ip_address=ip or "", status_code=response.status_code, response_ms=response_ms
                )
                return response

            # Log SEO bots and media crawlers with their category
            if platform in ("seo_bot", "media_crawler"):
                log_ai_request(
                    platform=platform, endpoint=path, user_agent=ua[:500],
                    ip_address=ip or "", status_code=response.status_code, response_ms=response_ms
                )
                return response

            # Log unknown_ai only if hitting AI-relevant endpoints
            if platform == "unknown_ai" and is_ai_endpoint(path):
                log_ai_request(
                    platform="unknown_ai", endpoint=path, user_agent=ua[:500],
                    ip_address=ip or "", status_code=response.status_code, response_ms=response_ms
                )
                return response

            # Skip generic_bot — not interesting for AI tracking

            # For unknown UAs, only log if hitting an AI-relevant endpoint
            if platform == "unknown" and is_ai_endpoint(path):
                log_ai_request(
                    platform="direct", endpoint=path, user_agent=ua[:500],
                    ip_address=ip or "", status_code=response.status_code, response_ms=response_ms
                )

        except Exception as e:
            print(f"[AI Tracking] Middleware error: {e}")

        return response

    @app.before_request
    def start_timer():
        g._request_start_time = datetime.now(timezone.utc).timestamp()

    # ── CORS: Handle preflight OPTIONS and add headers ────
    @app.after_request
    def add_cors_headers(response):
        """Add CORS headers to all ai-tracking responses."""
        path = request.path
        if '/ai-tracking' in path or '/ai/tracking' in path:
            origin = request.headers.get('Origin', '')
            allowed = ['https://dchub.cloud', 'https://www.dchub.cloud', 'http://localhost:3000']
            if origin in allowed:
                response.headers['Access-Control-Allow-Origin'] = origin
            else:
                response.headers['Access-Control-Allow-Origin'] = 'https://dchub.cloud'
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With'
        return response

    # ── API Routes ──────────────────────────────────────────

    @app.route("/api/v1/ai-tracking/stats", methods=["GET", "OPTIONS"])
    def ai_tracking_stats():
        """Main stats endpoint — returns everything the frontend needs."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        if not DATABASE_URL:
            return cors_jsonify({"error": "Tracking not configured", "tracking": "disabled"}, 503)

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
                    "first_seen": str(entry["first_seen"]) if entry["first_seen"] else None,
                    "last_seen": str(entry["last_seen"]) if entry["last_seen"] else None,
                    "requests_7d": chart.get(p, {}).get("requests_7d", 0),
                }

        # Include platforms with zero requests
        for key, info in AI_PLATFORMS.items():
            if key not in platforms:
                platforms[key] = {
                    "name": info["name"], "company": info["company"], "color": info["color"],
                    "total_requests": 0, "first_seen": None, "last_seen": None, "requests_7d": 0,
                }

        # Format recent activity
        recent_formatted = []
        for r in recent:
            p = r["platform"]
            info = AI_PLATFORMS.get(p, {"name": p.title(), "color": "#64748b"})
            recent_formatted.append({
                "platform": info["name"], "platform_key": p,
                "endpoint": r["endpoint"], "color": info.get("color", "#64748b"),
                "timestamp": str(r["created_at"]),
                "time_ago": _time_ago(r["created_at"]),
            })

        return cors_jsonify({
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
            "note": "Direct Neon PostgreSQL tracking — Railway edition"
        })

    @app.route("/api/v1/ai-tracking/chart", methods=["GET", "OPTIONS"])
    def ai_tracking_chart():
        """Chart-specific endpoint for the 7-day bar chart."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        days = request.args.get("days", 7, type=int)
        chart = get_platform_chart_data(days)
        sorted_platforms = sorted(chart.items(), key=lambda x: x[1]["requests_7d"], reverse=True)
        return cors_jsonify({
            "days": days,
            "platforms": [
                {"key": key, "name": data["name"], "color": data["color"], "requests": data["requests_7d"]}
                for key, data in sorted_platforms
            ],
            "total": sum(d["requests_7d"] for d in chart.values()),
        })

    @app.route("/api/v1/ai-tracking/cumulative", methods=["GET", "OPTIONS"])
    def ai_tracking_cumulative():
        """All-time cumulative totals per platform."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        return cors_jsonify({
            "all_time_total": get_all_time_total(),
            "platforms": get_cumulative_totals(),
        })

    @app.route("/api/v1/ai-tracking/recent", methods=["GET", "OPTIONS"])
    def ai_tracking_recent():
        """Recent activity feed."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        limit = request.args.get("limit", 20, type=int)
        recent = get_recent_activity(min(limit, 100))
        formatted = []
        for r in recent:
            p = r["platform"]
            info = AI_PLATFORMS.get(p, {"name": p.title(), "color": "#64748b"})
            formatted.append({
                "platform": info["name"], "platform_key": p,
                "endpoint": r["endpoint"], "color": info.get("color", "#64748b"),
                "timestamp": str(r["created_at"]),
                "time_ago": _time_ago(r["created_at"]),
            })
        return cors_jsonify({"recent": formatted})

    @app.route("/api/v1/ai-tracking/hourly", methods=["GET", "OPTIONS"])
    def ai_tracking_hourly():
        """Hourly breakdown for the last 24h."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        return cors_jsonify({"hourly": get_hourly_breakdown(24)})

    @app.route("/api/v1/ai-tracking/health", methods=["GET", "OPTIONS"])
    def ai_tracking_health():
        """Health check for the tracking system."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        try:
            r = _execute("SELECT COUNT(*) as total FROM ai_requests", fetch=True)
            p = _execute("SELECT COUNT(*) as total FROM ai_cumulative WHERE total_requests > 0", fetch=True)
            return cors_jsonify({
                "status": "healthy",
                "storage": "neon_postgresql",
                "total_logged": r["total"] if r else 0,
                "platforms_seen": p["total"] if p else 0,
                "tracking_mode": "persistent_neon",
            })
        except Exception as e:
            return cors_jsonify({"status": "error", "error": str(e)}, 500)

    @app.route("/api/v1/ai-tracking/mcp", methods=["GET", "OPTIONS"])
    def ai_tracking_mcp():
        """MCP connection stats — shows which platforms are making real MCP calls."""
        if request.method == "OPTIONS":
            return cors_jsonify({})
        days = request.args.get("days", 7, type=int)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            summary = _execute("""
                SELECT method, COUNT(*) as count, COUNT(DISTINCT platform) as platforms
                FROM mcp_connections WHERE created_at >= %s
                GROUP BY method ORDER BY count DESC
            """, (cutoff,), fetchall=True) or []

            by_platform = _execute("""
                SELECT platform, method, COUNT(*) as count, MAX(created_at) as last_seen
                FROM mcp_connections WHERE created_at >= %s
                GROUP BY platform, method ORDER BY count DESC
            """, (cutoff,), fetchall=True) or []

            recent = _execute("""
                SELECT platform, method, tool_name, user_agent, created_at, status_code
                FROM mcp_connections ORDER BY id DESC LIMIT 20
            """, fetchall=True) or []

            tool_usage = _execute("""
                SELECT tool_name, COUNT(*) as count, COUNT(DISTINCT platform) as platforms
                FROM mcp_connections
                WHERE method = 'tools/call' AND tool_name != '' AND created_at >= %s
                GROUP BY tool_name ORDER BY count DESC
            """, (cutoff,), fetchall=True) or []

            return cors_jsonify({
                "period_days": days,
                "method_summary": summary,
                "by_platform": by_platform,
                "tool_usage": tool_usage,
                "recent_connections": [{
                    "platform": r["platform"], "method": r["method"],
                    "tool_name": r.get("tool_name") or None,
                    "user_agent": (r.get("user_agent") or "")[:80] or None,
                    "timestamp": str(r["created_at"]),
                    "time_ago": _time_ago(r["created_at"]),
                    "status_code": r.get("status_code"),
                } for r in recent],
            })
        except Exception as e:
            return cors_jsonify({"error": str(e)}, 500)

    # ── Legacy compat routes ────────────────────────────────
    @app.route("/ai/tracking", methods=["GET", "OPTIONS"])
    def ai_tracking_compat():
        return ai_tracking_stats()

    @app.route("/api/ai/tracking", methods=["GET", "OPTIONS"])
    def ai_tracking_compat2():
        return ai_tracking_stats()

    if db_ok:
        print("   📊 AI Tracking: Neon PostgreSQL tracking active (Railway)")
        print(f"   📊 AI Tracking: Monitoring {len(AI_PLATFORMS)} platforms")
        print(f"   📊 AI Tracking: SEO/media bots classified separately ({len(SEO_BOTS)} patterns)")
        print(f"   📊 AI Tracking: MCP connection tracking enabled")
    else:
        print("   ⚠️  AI Tracking: Disabled (no DATABASE_URL)")
