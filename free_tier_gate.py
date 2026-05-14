"""
DC Hub — Free Tier Map Gate v4 (JWT + Account-Based)
=====================================================
Uses JWT decode to identify users (matching main.py's auth system).
Previous versions queried session_token column which DOES NOT EXIST
in the users table — that's why every user was treated as unauthenticated.

Free accounts: 1 map session + 5 layer toggles, then upgrade wall.
Pro/Enterprise: unlimited.

Usage in main.py:
    from free_tier_gate import init_free_tier_gate
    init_free_tier_gate(app, get_pg_connection)

Env vars:
    FREE_MAP_SESSIONS=1
    FREE_LAYER_TOGGLES=5
    FREE_TIER_GATE_ENABLED=true
"""

import os
import logging
from datetime import timedelta
from flask import request, jsonify, g

logger = logging.getLogger('free_tier_gate')

# ── Configuration ──────────────────────────────────────────────────────────
FREE_MAP_SESSIONS  = int(os.environ.get('FREE_MAP_SESSIONS', '1'))
FREE_LAYER_TOGGLES = int(os.environ.get('FREE_LAYER_TOGGLES', '5'))
GATE_ENABLED = os.environ.get('FREE_TIER_GATE_ENABLED', 'true').lower() == 'true'

# Endpoints gated for free users (Pro/Enterprise only)
GATED_PREFIXES = [
    '/api/v1/map',
    '/api/v1/substations',
    '/api/v1/power-plants',
    '/api/v1/transmission',

    '/api/v1/fiber',
    '/api/v1/site-score',
    '/api/v1/layers',
    '/api/v1/capacity-headroom',
    '/api/v1/energy-discovery',
    '/api/v1/competitive-intel',
    '/api/site-score',
    '/api/risk',
    '/api/v1/site-planner',
    '/api/v1/competitor',
]

# Never gated
ALWAYS_OPEN_PREFIXES = [
    '/api/v1/fiber/metro',
    '/api/v1/auth',
    '/api/v1/usage-status',
    '/api/v1/map',
    '/api/v1/map/register-load',
    '/api/v1/map/layer-toggle',
    '/api/v1/testimonials',
    '/api/v1/news',
    '/api/v1/stats',
    '/api/v1/facilities',
    '/api/v1/circuit-status',
    '/api/nav-config',
    '/api/agents',
    '/api/jobs',
    '/api/admin',
    '/api/energy-discovery',
    '/api/scheduler',
    '/mcp',
    '/health',
]

# ── SQL ────────────────────────────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS free_map_usage (
    user_id TEXT NOT NULL,
    session_date DATE DEFAULT CURRENT_DATE,
    sessions_used INTEGER DEFAULT 0,
    layer_toggles INTEGER DEFAULT 0,
    last_access TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, session_date)
);
CREATE INDEX IF NOT EXISTS idx_free_map_usage_user ON free_map_usage(user_id);
"""

GET_USAGE_SQL = """
SELECT COALESCE(SUM(sessions_used), 0) AS sessions,
       COALESCE(SUM(layer_toggles), 0) AS toggles
FROM free_map_usage
WHERE user_id = %s
  AND session_date > CURRENT_DATE - INTERVAL '30 days';
"""

INCREMENT_SESSION_SQL = """
INSERT INTO free_map_usage (user_id, session_date, sessions_used, layer_toggles, last_access)
VALUES (%s, CURRENT_DATE, 1, 0, NOW())
ON CONFLICT (user_id, session_date) DO UPDATE SET
    sessions_used = free_map_usage.sessions_used + 1,
    last_access = NOW();
"""

INCREMENT_TOGGLE_SQL = """
INSERT INTO free_map_usage (user_id, session_date, sessions_used, layer_toggles, last_access)
VALUES (%s, CURRENT_DATE, 0, 1, NOW())
ON CONFLICT (user_id, session_date) DO UPDATE SET
    layer_toggles = free_map_usage.layer_toggles + 1,
    last_access = NOW();
"""


# ── Helpers ────────────────────────────────────────────────────────────────

def is_gated(path):
    if any(path.startswith(p) for p in ['/api/v1/fiber/summary', '/api/v1/fiber/coverage', '/api/v1/fiber/nearby', '/api/v1/subsea/', '/api/v1/carriers', '/api/jobs/', '/api/admin/mcp/', '/api/v1/track-conversion']):
        return False
    return any(path.startswith(p) for p in GATED_PREFIXES)

def is_always_open(path):
    return any(path.startswith(p) for p in ALWAYS_OPEN_PREFIXES)

def get_user_from_jwt(token, get_db_conn):
    """
    Decode JWT and look up user plan from database.
    Uses the same JWT_SECRET and decode_jwt as main.py.
    """
    if not token:
        return None
    if token.startswith('Bearer '):
        token = token[7:]

    # Decode JWT using main.py's decode_jwt function
    try:
        from main import decode_jwt
        payload = decode_jwt(token)
        if not payload:
            return None
    except Exception as e:
        logger.error(f"JWT decode error: {e}")
        return None

    user_id = payload.get('user_id')
    email = payload.get('email')
    if not user_id:
        return None

    # Look up current plan from database
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, email, plan FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            return {'id': str(row[0]), 'email': row[1], 'plan': row[2] or 'free'}
        # User in JWT but not in DB — use JWT data
        return {'id': str(user_id), 'email': email or '', 'plan': 'free'}
    except Exception as e:
        logger.error(f"User lookup error: {e}")
        # Fall back to JWT data if DB fails
        return {'id': str(user_id), 'email': email or '', 'plan': payload.get('role', 'free')}
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


# Phase TT (2026-05-14): map mcp_dev_keys.tier values onto the web
# user `plan` vocabulary the gate checks (PAID_PLANS). Free keys are
# intentionally NOT here — a free key is not a paid bypass.
_PAID_KEY_TIERS = {
    'pro': 'pro', 'paid': 'pro', 'developer': 'pro',
    'enterprise': 'enterprise', 'ent': 'enterprise',
    'founding': 'founding',
}


def _user_from_api_key(api_key, get_db_conn):
    """Resolve an X-API-Key to a pseudo-user dict IF it's an active
    paid (pro/enterprise/founding) dev key. Returns None for unknown,
    inactive, or free-tier keys — the gate then falls through to its
    normal JWT / free-session logic. Best-effort: any DB error → None."""
    api_key = (api_key or '').strip()
    if not api_key:
        return None
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT tier, status, email FROM mcp_dev_keys WHERE api_key = %s",
            (api_key,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        tier, status, email = row
        if status and status != 'active':
            return None
        plan = _PAID_KEY_TIERS.get((tier or 'free').lower())
        if not plan:
            return None  # free-tier key — not a bypass
        return {'id': f'apikey:{api_key[:18]}', 'email': email or '',
                'plan': plan, 'via': 'api_key'}
    except Exception as e:
        logger.error(f"API-key auth lookup error: {e}")
        return None
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def get_usage(user_id, get_db_conn):
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(GET_USAGE_SQL, (user_id,))
        row = cur.fetchone()
        cur.close()
        return {'sessions': row[0], 'toggles': row[1]} if row else {'sessions': 0, 'toggles': 0}
    except Exception as e:
        logger.error(f"Usage check error: {e}")
        return {'sessions': 0, 'toggles': 0}
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

def increment_usage(user_id, usage_type, get_db_conn):
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        sql = INCREMENT_SESSION_SQL if usage_type == 'session' else INCREMENT_TOGGLE_SQL
        cur.execute(sql, (user_id,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Usage increment error: {e}")
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


# ── Flask Integration ──────────────────────────────────────────────────────

def init_free_tier_gate(app, get_db_conn):
    # Auto-create table
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        cur.close()
        logger.info("free_map_usage table ready")
    except Exception as e:
        logger.error(f"Failed to create free_map_usage table: {e}")
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    PAID_PLANS = ('pro', 'enterprise', 'founding')

    @app.before_request
    def enforce_free_tier():
        if not GATE_ENABLED:
            return None
        path = request.path
        # Bypass internal MCP server calls
        if request.headers.get("X-Internal-Key") in (os.environ.get("DCHUB_INTERNAL_KEY", ""), os.environ.get("DCHUB_SYNC_KEY", "")):
            return None
        if is_always_open(path):
            return None
        if not is_gated(path):
            return None

        token = request.headers.get('Authorization')
        user = get_user_from_jwt(token, get_db_conn)

        # Phase TT (2026-05-14): also accept a paid API key. The gate
        # used to honor ONLY a logged-in web JWT, so pro / enterprise
        # customers hitting the Land & Power surface via X-API-Key got
        # 401 even with a valid key. An X-API-Key that resolves to a
        # paid tier is now a first-class auth path.
        if not user:
            _api_key = (request.headers.get('X-API-Key')
                        or request.args.get('api_key'))
            if _api_key:
                user = _user_from_api_key(_api_key, get_db_conn)

        if not user:
            return jsonify({
                'error': 'authentication_required',
                'message': 'Sign in (or pass a Pro/Enterprise X-API-Key) to access the Land & Power map.',
                'login_url': 'https://dchub.cloud/login?redirect=/land-power-map'
            }), 401

        if user.get('plan') in PAID_PLANS:
            g.current_user = user
            return None

        # Free user — check usage
        usage = get_usage(user['id'], get_db_conn)
        if usage['sessions'] > FREE_MAP_SESSIONS:
            return jsonify({
                'error': 'upgrade_required',
                'message': 'Your free map session has been used. Upgrade to Pro for unlimited access.',
                'upgrade_url': 'https://dchub.cloud/pricing'
            }), 403

        return None

    # ── GET /api/v1/usage-status ──
    @app.route('/api/v1/usage-status')
    def usage_status():
        token = request.headers.get('Authorization')
        user = get_user_from_jwt(token, get_db_conn)

        if not user:
            resp = jsonify({
                'authenticated': False,
                'message': 'Sign in to access the map',
                'login_url': 'https://dchub.cloud/login?redirect=/land-power-map'
            })
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
            return resp

        if user.get('plan') in PAID_PLANS:
            resp = jsonify({
                'authenticated': True,
                'plan': user['plan'],
                'unlimited': True
            })
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
            return resp

        usage = get_usage(user['id'], get_db_conn)
        resp = jsonify({
            'authenticated': True,
            'plan': 'free',
            'unlimited': False,
            'sessions_used': usage['sessions'],
            'sessions_limit': FREE_MAP_SESSIONS,
            'layer_toggles_used': usage['toggles'],
            'layer_toggles_limit': FREE_LAYER_TOGGLES,
        })
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    # ── POST /api/v1/map/register-load ──
    @app.route('/api/v1/map/register-load', methods=['POST', 'OPTIONS'])
    def register_map_load():
        if request.method == 'OPTIONS':
            return '', 204

        token = request.headers.get('Authorization')
        user = get_user_from_jwt(token, get_db_conn)

        if not user:
            return jsonify({'error': 'authentication_required'}), 401

        if user.get('plan') in PAID_PLANS:
            return jsonify({'status': 'ok', 'plan': user['plan'], 'unlimited': True})

        usage = get_usage(user['id'], get_db_conn)
        if usage['sessions'] >= FREE_MAP_SESSIONS:
            return jsonify({
                'status': 'limit_reached',
                'message': 'Free map session already used.',
                'upgrade_url': 'https://dchub.cloud/pricing'
            }), 403

        increment_usage(user['id'], 'session', get_db_conn)
        return jsonify({
            'status': 'ok',
            'plan': 'free',
            'sessions_used': usage['sessions'] + 1,
            'sessions_limit': FREE_MAP_SESSIONS,
        })

    # ── POST /api/v1/map/layer-toggle ──
    @app.route('/api/v1/map/layer-toggle', methods=['POST', 'OPTIONS'])
    def register_layer_toggle():
        if request.method == 'OPTIONS':
            return '', 204

        token = request.headers.get('Authorization')
        user = get_user_from_jwt(token, get_db_conn)

        if not user:
            return jsonify({'error': 'authentication_required'}), 401

        if user.get('plan') in PAID_PLANS:
            return jsonify({'status': 'ok', 'unlimited': True})

        increment_usage(user['id'], 'toggle', get_db_conn)
        return jsonify({'status': 'ok'})

    logger.info(
        f"Free tier gate v4 initialized (JWT + account-based): "
        f"{FREE_MAP_SESSIONS} sessions/mo, {FREE_LAYER_TOGGLES} layers/session, "
        f"gating {len(GATED_PREFIXES)} endpoint prefixes"
    )
