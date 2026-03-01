"""
DC Hub — AI Platform Tracking (Neon PostgreSQL Edition)
========================================================
Drop-in replacement for the SQLite-based ai_tracking.py.
All tracking data persists in Neon across Railway/Replit redeploys.

Exports (same interface as original):
  init_ai_tracking(app)   — registers routes + creates tables
  log_ai_request(...)     — logs a single request to Neon
  detect_platform(ua)     — identifies AI platform from User-Agent
  get_daily_stats(days)   — returns daily stats dict

Routes registered:
  GET /api/v1/ai-tracking/stats   — daily stats (original)
  GET /api/ai/tracking            — full tracking dashboard data
  GET /api/ai/platforms           — cumulative per-platform totals (NEW)
  GET /api/ai/daily-stats         — daily breakdown for charts (NEW)
  GET /api/ai/health-check        — connectivity test (NEW)

Tables used (created if missing):
  ai_cumulative       — lifetime totals per platform
  ai_usage_tracking   — individual request log
  ai_daily_stats      — daily aggregates per platform
"""

import os
import re
import json
import threading
import traceback
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

logger = logging.getLogger('ai_tracking')

# ═════════════════════════════════════════════════════════════
# DATABASE CONNECTION
# ═════════════════════════════════════════════════════════════

def _get_conn():
    """Get a psycopg2 connection using DATABASE_URL (which points to Neon)."""
    import psycopg2
    db_url = os.environ.get('DATABASE_URL', '')
    if not db_url:
        db_url = os.environ.get('NEON_DATABASE_URL', '')
    if not db_url:
        raise Exception("No DATABASE_URL or NEON_DATABASE_URL configured")
    return psycopg2.connect(db_url, connect_timeout=10)


def _execute(sql, params=None, fetch=False, fetchall=False):
    """Execute SQL with auto-reconnect. Returns dict rows."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if fetchall:
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            conn.commit()
            conn.close()
            return [dict(zip(cols, r)) for r in rows]
        elif fetch:
            cols = [d[0] for d in cur.description] if cur.description else []
            row = cur.fetchone()
            conn.commit()
            conn.close()
            return dict(zip(cols, row)) if row else None
        else:
            conn.commit()
            conn.close()
            return None
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        raise e


def _cors_json(data, status=200):
    """Return JSON response with CORS headers."""
    from flask import jsonify, make_response
    resp = make_response(jsonify(data), status)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key'
    resp.headers['Cache-Control'] = 'public, max-age=30'
    return resp


# ═════════════════════════════════════════════════════════════
# PLATFORM DETECTION
# ═════════════════════════════════════════════════════════════

# Patterns: (compiled_regex, platform_name)
_PLATFORM_PATTERNS = [
    (re.compile(r'ChatGPT|GPTBot|chatgpt-user|OpenAI', re.I), 'chatgpt'),
    (re.compile(r'Claude|Anthropic|claude-web|ClaudeBot', re.I), 'claude'),
    (re.compile(r'Perplexity|PerplexityBot', re.I), 'perplexity'),
    (re.compile(r'Gemini|Google-Extended|GoogleOther|google-gemini', re.I), 'gemini'),
    (re.compile(r'Grok|xAI', re.I), 'grok'),
    (re.compile(r'CopilotBot|Copilot|BingBot|bingbot', re.I), 'copilot'),
    (re.compile(r'DeepSeek|deepseek', re.I), 'deepseek'),
    (re.compile(r'Meta-ExternalAgent|Meta-AI|FacebookBot', re.I), 'meta'),
    (re.compile(r'Cohere|cohere-ai', re.I), 'cohere'),
    (re.compile(r'Groq|groq', re.I), 'groq'),
    (re.compile(r'Mistral|mistral', re.I), 'mistral'),
    (re.compile(r'HuggingFace|huggingface', re.I), 'huggingface'),
    (re.compile(r'Cursor|cursor/', re.I), 'cursor'),
    (re.compile(r'Windsurf|codeium', re.I), 'windsurf'),
    (re.compile(r'Claude-Code|claude_code', re.I), 'claude_code'),
    (re.compile(r'mcp-remote|mcp-client|streamable-http', re.I), 'mcp'),
    # SEO / crawlers (tracked separately)
    (re.compile(r'Googlebot|AhrefsBot|SemrushBot|DotBot|MJ12bot|YandexBot', re.I), 'seo_bot'),
    (re.compile(r'newspaper|scrapy|curl|wget|python-requests', re.I), 'media_crawler'),
]


def detect_platform(user_agent):
    """Identify AI platform from User-Agent string."""
    if not user_agent:
        return 'direct'
    for pattern, name in _PLATFORM_PATTERNS:
        if pattern.search(user_agent):
            return name
    # Check for generic AI indicators
    if re.search(r'bot|ai|agent|crawler|spider', user_agent, re.I):
        return 'unknown_ai'
    return 'direct'


# ═════════════════════════════════════════════════════════════
# LOGGING — Write to Neon
# ═════════════════════════════════════════════════════════════

# Buffer for batch writes (reduces DB round-trips)
_log_buffer = []
_log_lock = threading.Lock()
_BUFFER_SIZE = 5  # Flush every N requests
_FLUSH_INTERVAL = 30  # Or every N seconds

_last_flush = datetime.now(timezone.utc)


def log_ai_request(platform='direct', endpoint='', user_agent='',
                   ip_address='', status_code=200, response_ms=0):
    """Log a single AI platform request. Buffers and batch-writes to Neon."""
    global _last_flush

    entry = {
        'platform': platform,
        'endpoint': endpoint,
        'user_agent': (user_agent or '')[:500],
        'ip_address': (ip_address or '')[:45],
        'status_code': status_code,
        'response_ms': response_ms,
        'tracked_at': datetime.now(timezone.utc),
    }

    with _log_lock:
        _log_buffer.append(entry)
        should_flush = (
            len(_log_buffer) >= _BUFFER_SIZE or
            (datetime.now(timezone.utc) - _last_flush).total_seconds() > _FLUSH_INTERVAL
        )

    if should_flush:
        _flush_buffer()


def _flush_buffer():
    """Write buffered requests to Neon in a single transaction."""
    global _last_flush

    with _log_lock:
        if not _log_buffer:
            return
        entries = _log_buffer.copy()
        _log_buffer.clear()
        _last_flush = datetime.now(timezone.utc)

    try:
        conn = _get_conn()
        cur = conn.cursor()

        # 1. Insert individual tracking rows
        for e in entries:
            cur.execute("""
                INSERT INTO ai_usage_tracking 
                    (platform, endpoint, user_agent, ip_address, status_code, response_ms, tracked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (e['platform'], e['endpoint'], e['user_agent'],
                  e['ip_address'], e['status_code'], e['response_ms'],
                  e['tracked_at']))

        # 2. Update cumulative totals
        platform_counts = {}
        for e in entries:
            platform_counts[e['platform']] = platform_counts.get(e['platform'], 0) + 1

        for platform, count in platform_counts.items():
            cur.execute("""
                INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (platform) DO UPDATE SET
                    total_requests = ai_cumulative.total_requests + %s,
                    last_seen = NOW()
            """, (platform, count, count))

        # 3. Update daily stats
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        for platform, count in platform_counts.items():
            cur.execute("""
                INSERT INTO ai_daily_stats (date, platform, request_count)
                VALUES (%s, %s, %s)
                ON CONFLICT (date, platform) DO UPDATE SET
                    request_count = ai_daily_stats.request_count + %s
            """, (today, platform, count, count))

        conn.commit()
        conn.close()
        logger.debug(f"[AI Tracking] Flushed {len(entries)} requests to Neon ({len(platform_counts)} platforms)")

    except Exception as e:
        logger.error(f"[AI Tracking] Flush failed: {e}")
        # Put entries back in buffer for retry
        with _log_lock:
            _log_buffer.extend(entries)


# ═════════════════════════════════════════════════════════════
# QUERY FUNCTIONS
# ═════════════════════════════════════════════════════════════

def get_daily_stats(days=7):
    """Return daily stats as a dict: { 'YYYY-MM-DD': { platform: count, ... }, ... }"""
    try:
        rows = _execute("""
            SELECT date::text, platform, request_count
            FROM ai_daily_stats
            WHERE date >= CURRENT_DATE - INTERVAL '%s days'
            ORDER BY date ASC
        """ % int(days), fetchall=True)

        result = {}
        for r in rows:
            d = r['date']
            if d not in result:
                result[d] = {'total': 0}
            result[d][r['platform']] = r['request_count']
            result[d]['total'] += r['request_count']
        return result
    except Exception as e:
        logger.error(f"[AI Tracking] get_daily_stats error: {e}")
        return {}


# ═════════════════════════════════════════════════════════════
# TABLE CREATION
# ═════════════════════════════════════════════════════════════

def _ensure_tables():
    """Create tracking tables in Neon if they don't exist."""
    try:
        conn = _get_conn()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_cumulative (
                platform TEXT PRIMARY KEY,
                total_requests BIGINT DEFAULT 0,
                first_seen TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage_tracking (
                id BIGSERIAL PRIMARY KEY,
                platform TEXT NOT NULL,
                endpoint TEXT,
                user_agent TEXT,
                ip_address TEXT,
                status_code INTEGER DEFAULT 200,
                response_ms INTEGER DEFAULT 0,
                tracked_at TIMESTAMPTZ DEFAULT NOW(),
                timestamp TEXT
            )
        """)

        # Index for fast date queries
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_usage_tracked_at 
            ON ai_usage_tracking (tracked_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_usage_platform 
            ON ai_usage_tracking (platform)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_daily_stats (
                date DATE NOT NULL,
                platform TEXT NOT NULL,
                request_count INTEGER DEFAULT 0,
                PRIMARY KEY (date, platform)
            )
        """)

        conn.commit()
        conn.close()
        logger.info("[AI Tracking] ✅ Neon tables verified/created")
    except Exception as e:
        logger.error(f"[AI Tracking] Table creation error: {e}")


# ═════════════════════════════════════════════════════════════
# BACKGROUND FLUSH THREAD
# ═════════════════════════════════════════════════════════════

def _start_flush_thread():
    """Periodically flush the log buffer to Neon."""
    def _flush_loop():
        while True:
            try:
                import time
                time.sleep(_FLUSH_INTERVAL)
                _flush_buffer()
            except Exception as e:
                logger.error(f"[AI Tracking] Flush thread error: {e}")
                import time
                time.sleep(10)

    t = threading.Thread(target=_flush_loop, daemon=True, name='ai-tracking-flush')
    t.start()
    logger.info("[AI Tracking] ✅ Background flush thread started")


# ═════════════════════════════════════════════════════════════
# FLASK ROUTE REGISTRATION
# ═════════════════════════════════════════════════════════════

def init_ai_tracking(app):
    """Register all AI tracking routes and initialize tables."""

    # Create tables on startup
    _ensure_tables()

    # Start background flush
    _start_flush_thread()

    # ── GET /api/v1/ai-tracking/stats (original endpoint) ────
    @app.route('/api/v1/ai-tracking/stats', methods=['GET', 'OPTIONS'])
    def ai_tracking_stats_v1():
        if request.method == 'OPTIONS':
            return _cors_json({})
        try:
            days = request.args.get('days', 7, type=int)
            stats = get_daily_stats(days)
            return _cors_json({'success': True, 'stats': stats})
        except Exception as e:
            return _cors_json({'success': False, 'error': str(e)})

    # ── GET /api/ai/tracking (dashboard aggregate) ───────────
    @app.route('/api/ai/tracking', methods=['GET', 'OPTIONS'])
    def ai_tracking_dashboard():
        if request.method == 'OPTIONS':
            return _cors_json({})
        try:
            # Cumulative totals
            cumulative = _execute("""
                SELECT platform, total_requests, 
                       first_seen::text, last_seen::text
                FROM ai_cumulative
                ORDER BY total_requests DESC
            """, fetchall=True) or []

            total_all = sum(r.get('total_requests', 0) for r in cumulative)

            # Today's total
            today_row = _execute("""
                SELECT COALESCE(SUM(request_count), 0) as today_total
                FROM ai_daily_stats
                WHERE date = CURRENT_DATE
            """, fetch=True)
            today_total = today_row.get('today_total', 0) if today_row else 0

            # Active platforms (seen in last 7 days, excluding bots)
            non_ai = ('direct', 'seo_bot', 'media_crawler', 'unknown_ai', 'mcp-remote-fallback-test')
            ai_platforms = [r for r in cumulative if r['platform'] not in non_ai]
            active_count = 0
            for p in ai_platforms:
                if p.get('last_seen'):
                    try:
                        last = datetime.fromisoformat(p['last_seen'].replace('+00:00', '+00:00'))
                        if (datetime.now(timezone.utc) - last).days < 7:
                            active_count += 1
                    except Exception:
                        pass

            # 7-day per-platform breakdown
            weekly = _execute("""
                SELECT platform, SUM(request_count) as week_total
                FROM ai_daily_stats
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY platform
                ORDER BY week_total DESC
            """, fetchall=True) or []
            weekly_map = {r['platform']: r['week_total'] for r in weekly}

            # Build platforms dict for frontend
            platforms_dict = {}
            for r in cumulative:
                platforms_dict[r['platform']] = {
                    'total_requests': r.get('total_requests', 0),
                    'requests_7d': weekly_map.get(r['platform'], 0),
                    'first_seen': r.get('first_seen'),
                    'last_seen': r.get('last_seen'),
                }

            # Recent activity (last 25 requests)
            recent = _execute("""
                SELECT platform, endpoint, tracked_at::text
                FROM ai_usage_tracking
                WHERE tracked_at >= NOW() - INTERVAL '24 hours'
                ORDER BY tracked_at DESC
                LIMIT 25
            """, fetchall=True) or []

            return _cors_json({
                'total_requests_all_time': total_all,
                'total_requests_today': today_total,
                'platforms_active': active_count,
                'platforms_total': len(ai_platforms),
                'platforms': platforms_dict,
                'recent_activity': recent,
            })

        except Exception as e:
            logger.error(f"[AI Tracking] Dashboard error: {e}")
            return _cors_json({
                'total_requests_all_time': 0,
                'total_requests_today': 0,
                'platforms_active': 0,
                'platforms': {},
                'error': str(e)
            })

    # ── GET /api/ai/platforms (cumulative totals) ────────────
    @app.route('/api/ai/platforms', methods=['GET', 'OPTIONS'])
    def ai_platforms_list():
        """Cumulative per-platform totals from ai_cumulative."""
        if request.method == 'OPTIONS':
            return _cors_json({})
        try:
            rows = _execute("""
                SELECT platform, total_requests,
                       first_seen::text, last_seen::text
                FROM ai_cumulative
                ORDER BY total_requests DESC
            """, fetchall=True) or []
            return _cors_json(rows)
        except Exception as e:
            return _cors_json({'error': str(e), 'platforms': []}, 500)

    # ── GET /api/ai/daily-stats (chart data) ─────────────────
    @app.route('/api/ai/daily-stats', methods=['GET', 'OPTIONS'])
    def ai_daily_stats_endpoint():
        """Daily breakdown by platform for charts.
        
        Query params:
          ?days=14  (default 14, max 90)
          ?since=2026-02-20
        """
        if request.method == 'OPTIONS':
            return _cors_json({})
        try:
            days = min(request.args.get('days', 14, type=int), 90)
            since = request.args.get('since')

            if since:
                rows = _execute("""
                    SELECT date::text, platform, request_count
                    FROM ai_daily_stats
                    WHERE date >= %s::date
                    ORDER BY date ASC, request_count DESC
                """, (since,), fetchall=True) or []
            else:
                rows = _execute("""
                    SELECT date::text, platform, request_count
                    FROM ai_daily_stats
                    WHERE date >= CURRENT_DATE - INTERVAL '%s days'
                    ORDER BY date ASC, request_count DESC
                """ % days, fetchall=True) or []

            # Pivot into { date, chatgpt: N, claude: N, total: N } format
            daily = {}
            for r in rows:
                d = r['date']
                if d not in daily:
                    daily[d] = {'date': d, 'total': 0}
                daily[d][r['platform']] = r['request_count']
                daily[d]['total'] += r['request_count']

            result = sorted(daily.values(), key=lambda x: x['date'])
            return _cors_json(result)

        except Exception as e:
            return _cors_json({'error': str(e), 'data': []}, 500)

    # ── GET /api/ai/health-check ─────────────────────────────
    @app.route('/api/ai/health-check', methods=['GET', 'OPTIONS'])
    def ai_health_check():
        """Quick test for dashboard connectivity."""
        if request.method == 'OPTIONS':
            return _cors_json({})
        result = {
            'status': 'ok',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'database': 'unknown',
            'tables': {},
            'buffer_size': len(_log_buffer),
        }
        try:
            for table in ['ai_cumulative', 'ai_usage_tracking', 'ai_daily_stats']:
                try:
                    row = _execute(f"SELECT COUNT(*) as cnt FROM {table}", fetch=True)
                    result['tables'][table] = row['cnt'] if row else 0
                except Exception:
                    result['tables'][table] = 'missing'
            result['database'] = 'neon_connected'
        except Exception as e:
            result['status'] = 'error'
            result['database'] = str(e)
        return _cors_json(result)

    # Import request for route handlers
    from flask import request

    logger.info("[AI Tracking] ✅ Neon-backed tracking registered:")
    logger.info("  /api/v1/ai-tracking/stats")
    logger.info("  /api/ai/tracking")
    logger.info("  /api/ai/platforms")
    logger.info("  /api/ai/daily-stats")
    logger.info("  /api/ai/health-check")
