"""
DC Hub — Server-Side Free Tier Gate Middleware
================================================
Drop-in Flask middleware that enforces free-tier limits on map API endpoints
using IP-based tracking in Neon PostgreSQL (NOT localStorage).

Closes the incognito / clear-cookies bypass.

Usage in your main.py:
    from free_tier_gate import init_free_tier_gate
    init_free_tier_gate(app, get_db_connection)

Where get_db_connection() returns a psycopg2 connection to Neon.

Env vars:
    FREE_MAP_LOADS=3              # map page loads per 30 days for free users
    FREE_SEARCHES=1               # searches per 30 days for free users
    FREE_TIER_GATE_ENABLED=true   # kill switch (set to false to disable)
"""

import os
import logging
from datetime import timedelta
from flask import request, jsonify, g

logger = logging.getLogger('free_tier_gate')

# ── Configuration ──────────────────────────────────────────────────────────
FREE_MAP_LOADS_PER_MONTH = int(os.environ.get('FREE_MAP_LOADS', '3'))
FREE_SEARCHES_PER_MONTH  = int(os.environ.get('FREE_SEARCHES', '1'))
GATE_ENABLED = os.environ.get('FREE_TIER_GATE_ENABLED', 'true').lower() == 'true'

# Endpoints that require pro/enterprise — add ALL your map data routes
GATED_PREFIXES = [
    '/api/v1/map',
    '/api/v1/substations',
    '/api/v1/power-plants',
    '/api/v1/transmission',
    '/api/v1/gas-pipelines',
    '/api/v1/fiber',
    '/api/v1/site-score',
    '/api/v1/layers',
    '/api/v1/capacity-headroom',
    '/api/v1/energy-discovery',
    '/api/v1/competitive-intel',
]

# Endpoints that are always open (never gated)
ALWAYS_OPEN_PREFIXES = [
    '/api/v1/auth',
    '/api/v1/usage-status',
    '/api/v1/map/register-load',
    '/api/v1/testimonials',
    '/api/v1/news',
    '/api/v1/stats',
    '/api/v1/facilities',
    '/api/v1/circuit-status',
    '/api/nav-config',
    '/api/agents',
    '/mcp',
    '/health',
]


# ── SQL ────────────────────────────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS free_usage (
    ip_address VARCHAR(45) PRIMARY KEY,
    first_access TIMESTAMPTZ DEFAULT NOW(),
    map_loads INTEGER DEFAULT 0,
    searches INTEGER DEFAULT 0,
    last_access TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_free_usage_last_access ON free_usage(last_access);
"""

UPSERT_SQL = """
INSERT INTO free_usage (ip_address, first_access, map_loads, searches, last_access)
VALUES (%s, NOW(), %s, %s, NOW())
ON CONFLICT (ip_address) DO UPDATE SET
    map_loads = CASE
        WHEN free_usage.first_access < NOW() - INTERVAL '30 days'
        THEN EXCLUDED.map_loads
        ELSE free_usage.map_loads + EXCLUDED.map_loads
    END,
    searches = CASE
        WHEN free_usage.first_access < NOW() - INTERVAL '30 days'
        THEN EXCLUDED.searches
        ELSE free_usage.searches + EXCLUDED.searches
    END,
    first_access = CASE
        WHEN free_usage.first_access < NOW() - INTERVAL '30 days'
        THEN NOW()
        ELSE free_usage.first_access
    END,
    last_access = NOW()
RETURNING map_loads, searches, first_access;
"""

GET_USAGE_SQL = """
SELECT map_loads, searches, first_access
FROM free_usage
WHERE ip_address = %s
  AND first_access > NOW() - INTERVAL '30 days';
"""


# ── Helpers ────────────────────────────────────────────────────────────────

def get_client_ip():
    """Get real client IP behind Cloudflare."""
    return (
        request.headers.get('CF-Connecting-IP') or
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.remote_addr
    )


def is_gated_endpoint(path):
    return any(path.startswith(p) for p in GATED_PREFIXES)


def is_always_open(path):
    return any(path.startswith(p) for p in ALWAYS_OPEN_PREFIXES)


def validate_user_token(token, get_db_conn):
    """
    Validate auth token and return user dict with 'plan' field.
    ─────────────────────────────────────────────────────────
    ** ADJUST THIS to match your actual auth table/columns **
    ─────────────────────────────────────────────────────────
    """
    if not token:
        return None
    if token.startswith('Bearer '):
        token = token[7:]
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, email, plan
            FROM users
            WHERE session_token = %s
              AND session_expires > NOW()
        """, (token,))
        row = cur.fetchone()
        cur.close()
        if row:
            return {'id': row[0], 'email': row[1], 'plan': row[2]}
        return None
    except Exception as e:
        logger.error(f"Token validation error: {e}")
        return None


def check_free_usage(ip, get_db_conn, usage_type='map_load'):
    """Check and increment free-tier usage. Returns (allowed, usage_info)."""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        map_inc    = 1 if usage_type == 'map_load' else 0
        search_inc = 1 if usage_type == 'search'   else 0
        cur.execute(UPSERT_SQL, (ip, map_inc, search_inc))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row:
            current_map_loads = row[0]
            current_searches  = row[1]
            first_access      = row[2]
            limit   = FREE_MAP_LOADS_PER_MONTH if usage_type == 'map_load' else FREE_SEARCHES_PER_MONTH
            current = current_map_loads if usage_type == 'map_load' else current_searches
            return current <= limit, {
                'map_loads': current_map_loads,
                'searches':  current_searches,
                'limit_map':    FREE_MAP_LOADS_PER_MONTH,
                'limit_search': FREE_SEARCHES_PER_MONTH,
                'resets': str(first_access + timedelta(days=30)),
            }
        return True, {}
    except Exception as e:
        logger.error(f"Free usage check error: {e}")
        return True, {}   # fail open


def upgrade_response(usage_info, message="Free tier limit reached"):
    return jsonify({
        'error': 'upgrade_required',
        'message': message,
        'upgrade_url': 'https://dchub.cloud/pricing',
        'usage': usage_info,
    }), 403


# ── Flask Integration ──────────────────────────────────────────────────────

def init_free_tier_gate(app, get_db_conn):
    """
    Initialize the free tier gate on your Flask app.

    Args:
        app:          Flask application instance
        get_db_conn:  Callable returning a psycopg2 connection
    """

    # Auto-create table on startup
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        cur.close()
        logger.info("free_usage table ready")
    except Exception as e:
        logger.error(f"Failed to create free_usage table: {e}")

    # ── Before-request middleware ──
    @app.before_request
    def enforce_free_tier():
        if not GATE_ENABLED:
            return None

        path = request.path

        if is_always_open(path):
            return None

        if not is_gated_endpoint(path):
            return None

        # Authenticated user?
        token = request.headers.get('Authorization')
        user  = validate_user_token(token, get_db_conn)

        if user and user.get('plan') in ('pro', 'enterprise'):
            g.current_user = user
            return None

        # Free or anonymous — check IP quota
        ip = get_client_ip()
        allowed, usage_info = check_free_usage(ip, get_db_conn, usage_type='map_load')

        if not allowed:
            logger.info(f"Free tier limit hit for IP {ip} on {path}")
            return upgrade_response(
                usage_info,
                message=f"You've used your {FREE_MAP_LOADS_PER_MONTH} free map views this month. "
                        f"Upgrade to Pro for unlimited access."
            )
        return None

    # ── GET /api/v1/usage-status — check usage without incrementing ──
    # CRITICAL: Must return Cache-Control: no-store so Cloudflare Worker
    # doesn't cache per-IP usage data in Cache API (all users would see same count)
    @app.route('/api/v1/usage-status')
    def usage_status():
        token = request.headers.get('Authorization')
        user  = validate_user_token(token, get_db_conn)
        if user and user.get('plan') in ('pro', 'enterprise'):
            resp = jsonify({'plan': user['plan'], 'unlimited': True})
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
            return resp

        ip = get_client_ip()
        try:
            conn = get_db_conn()
            cur  = conn.cursor()
            cur.execute(GET_USAGE_SQL, (ip,))
            row = cur.fetchone()
            cur.close()
            if row:
                resp = jsonify({
                    'plan': 'free',
                    'unlimited': False,
                    'map_loads_used':  row[0],
                    'map_loads_limit': FREE_MAP_LOADS_PER_MONTH,
                    'searches_used':   row[1],
                    'searches_limit':  FREE_SEARCHES_PER_MONTH,
                    'resets': str(row[2] + timedelta(days=30)),
                })
                resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
                return resp
            resp = jsonify({
                'plan': 'free',
                'unlimited': False,
                'map_loads_used': 0,
                'map_loads_limit': FREE_MAP_LOADS_PER_MONTH,
                'searches_used': 0,
                'searches_limit': FREE_SEARCHES_PER_MONTH,
            })
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
            return resp
        except Exception as e:
            logger.error(f"Usage status error: {e}")
            return jsonify({'plan': 'free', 'unlimited': False}), 500

    # ── POST /api/v1/map/register-load — frontend calls on each page load ──
    @app.route('/api/v1/map/register-load', methods=['POST', 'OPTIONS'])
    def register_map_load():
        if request.method == 'OPTIONS':
            return '', 204

        token = request.headers.get('Authorization')
        user  = validate_user_token(token, get_db_conn)
        if user and user.get('plan') in ('pro', 'enterprise'):
            return jsonify({'status': 'ok', 'plan': user['plan'], 'unlimited': True})

        ip = get_client_ip()
        allowed, usage_info = check_free_usage(ip, get_db_conn, usage_type='map_load')
        if not allowed:
            return jsonify({
                'status': 'limit_reached',
                'message': f"You've used your {FREE_MAP_LOADS_PER_MONTH} free map views this month.",
                'usage': usage_info,
                'upgrade_url': 'https://dchub.cloud/pricing'
            }), 403

        return jsonify({
            'status': 'ok',
            'plan': 'free',
            'unlimited': False,
            'map_loads_used':  usage_info.get('map_loads', 0),
            'map_loads_limit': FREE_MAP_LOADS_PER_MONTH,
        })

    logger.info(
        f"Free tier gate initialized: {FREE_MAP_LOADS_PER_MONTH} map loads/mo, "
        f"{FREE_SEARCHES_PER_MONTH} searches/mo, "
        f"gating {len(GATED_PREFIXES)} endpoint prefixes"
    )
