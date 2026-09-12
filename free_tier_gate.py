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
#
# ★ 2026-08-10: '/api/v1/map' REMOVED — it was dead configuration that made
# this file lie. The same path is also in ALWAYS_OPEN_PREFIXES below, and the
# middleware checks is_always_open() FIRST and returns before is_gated() is
# ever consulted (see ~line 709). So this entry could never fire, while anyone
# reading the list reasonably concluded the map was Pro/Enterprise-only.
# It is not: measured live, an anonymous caller pulled all 19,969 facilities
# from /api/v1/map?limit=20000, twelve times in a row, unblocked.
#
# DELETED rather than moved out of ALWAYS_OPEN, deliberately. Making the gate
# real here would return 402 to every anonymous visitor and break the public
# SEO map (+1,967% growth) that main.py's r-anonbulk block explicitly protects.
# The map's paywall is FIELD- and PRECISION-based inside that handler — anon/
# free get name + quantized coords, never power_mw / operator / fiber / exact
# location — plus the per-IP bulk cap added alongside this change. That is the
# intended design; this list simply claimed a second, non-existent gate.
GATED_PREFIXES = [
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
    # 2026-07-17: bulk PeeringDB extract. /api/v1/networks/facility/<fac_id>
    # enumerates EVERY network at a facility (687 rows / ~113KB for fac 4024)
    # unauthenticated. Its sibling /api/v1/connectivity/<fac_id> stays open on
    # purpose: that one is a count + [:5] sample + score (~1.5KB), the
    # adoption-first teaser this is the paid depth behind. Prefix is
    # deliberately '/networks/facility' and not '/networks' — /networks/summary
    # is aggregate-only and advertised in ai.json, so it must stay open.
    '/api/v1/networks/facility',
]

# ── 2026-07-10: server-side map SESSION cap ─────────────────────────────────
# The Land+Power map's "1 session/month" cap + layer limit used to live ONLY in
# the browser (localStorage: dchub-map-session-cap.js / anon-wall.js). A flipped
# `dchub_dev_unlock` flag or a client-set `dchub_plan='pro'` bypassed the whole
# thing. We now enforce the SESSION cap server-side, keyed off a durable caller
# identity (API key → JWT user → /24 IP prefix) that no client-set flag can
# change. Enforcement is applied to the PROPRIETARY / metered map data
# endpoints below (returns a real 402 once the cap is exceeded).
#
# Deliberately EXCLUDES the intentional public HIFLD geo surfaces
# (gas-pipelines / substations / transmission) — those stay open (they're
# public-domain government data and are shared with non-map surfaces). Widening
# this list to those feeders is the higher-risk "full lockdown" follow-up that
# the 2026-07-10 sweep deferred pending per-feeder credential-flow verification.
METERED_MAP_PREFIXES = [
    '/api/v1/land-power/data',   # consolidated L&P endpoint
    '/api/v1/power-plants',      # DC Hub discovered power assets (proprietary)
    '/api/v1/fiber/routes',      # proprietary fiber routing
    '/api/v1/fiber/intel',
    '/api/v1/fiber/sources',
    '/api/v1/grid/intelligence',  # proprietary grid headroom/intel
    '/api/v1/capacity-headroom',
    '/api/v1/energy-discovery',
    '/api/energy-discovery',
    '/api/v1/competitive-intel',
    '/api/v1/competitor',
    '/api/v1/site-score',
    '/api/site-score',
    '/api/v1/site-planner',
]

_PAID_PLANS = ('pro', 'enterprise', 'founding')

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
VALUES (%s, CURRENT_DATE, 1, 0, NOW() ON CONFLICT DO NOTHING)
ON CONFLICT (user_id, session_date) DO UPDATE SET
    sessions_used = free_map_usage.sessions_used + 1,
    last_access = NOW();
"""

INCREMENT_TOGGLE_SQL = """
INSERT INTO free_map_usage (user_id, session_date, sessions_used, layer_toggles, last_access)
VALUES (%s, CURRENT_DATE, 0, 1, NOW() ON CONFLICT DO NOTHING)
ON CONFLICT (user_id, session_date) DO UPDATE SET
    layer_toggles = free_map_usage.layer_toggles + 1,
    last_access = NOW();
"""

# 2026-07-10 — combined read for the server-side session gate. Returns
# (active_days_in_30d, is_today_active). One row per calendar day per identity is
# marked (sessions_used capped at 1/day by MARK_SESSION_SQL), so SUM over the
# 30-day window == number of distinct map-active days == "sessions this month".
METERED_USAGE_SQL = """
SELECT COALESCE(SUM(CASE WHEN sessions_used > 0 THEN 1 ELSE 0 END), 0) AS active_days,
       COALESCE(BOOL_OR(session_date = CURRENT_DATE), FALSE)          AS today
FROM free_map_usage
WHERE user_id = %s
  AND session_date > CURRENT_DATE - INTERVAL '30 days';
"""

# Idempotent "mark today active" — sessions_used stays 1 for the day no matter
# how many feeder calls the map fires, so a single visit burns exactly one
# session. Only the FIRST hit of a new day inserts; repeats just touch
# last_access (they hit the not-today branch of the gate rarely).
MARK_SESSION_SQL = """
INSERT INTO free_map_usage (user_id, session_date, sessions_used, layer_toggles, last_access)
VALUES (%s, CURRENT_DATE, 1, 0, NOW() ON CONFLICT DO NOTHING)
ON CONFLICT (user_id, session_date) DO UPDATE SET
    last_access = NOW();
"""


# ── Helpers ────────────────────────────────────────────────────────────────

def is_gated(path):
    # /api/v1/fiber/route-plan: OPEN GEO/demo surface (diverse fibre route
    # planner, routes/fiber_route_plan.py) — indicative OSRM corridors + unit
    # rates, no proprietary data. Documented as open-to-any-address; without
    # this exemption the '/api/v1/fiber' GATED_PREFIX 401'd it.
    # /api/v1/fiber/cluster-latency: OPEN like route-plan (routes/
    # cluster_latency.py) — deterministic physics screen + inferred
    # enrichment, no proprietary bulk data; adoption-first.
    if any(path.startswith(p) for p in ['/api/v1/fiber/summary', '/api/v1/fiber/coverage', '/api/v1/fiber/nearby', '/api/v1/fiber/routes/public', '/api/v1/fiber/route-plan', '/api/v1/fiber/cluster-latency', '/api/v1/subsea/', '/api/v1/carriers', '/api/jobs/', '/api/admin/mcp/', '/api/v1/track-conversion']):
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
        if row:
            tier, status, email = row
            cur.close()
            if status and status != 'active':
                return None
            plan = _PAID_KEY_TIERS.get((tier or 'free').lower())
            if not plan:
                return None  # free-tier key — not a bypass
            return {'id': f'apikey:{api_key[:18]}', 'email': email or '',
                    'plan': plan, 'via': 'api_key'}
        # 2026-07-01: TWO-key-systems gap — this gate only knew mcp_dev_keys,
        # so a paying customer's DASHBOARD key (api_keys table + users.plan,
        # e.g. the dchub_pro_… keys we email out) got 401 on every map data
        # endpoint. Mirror util.tier_gate.resolve_tier's cross-table fallback:
        # dual key_hash match (standard keys store sha256(key); partner/QA
        # keys store the RAW string), best plan across rate_limit_tier /
        # ak.plan / users.plan. api_keys.is_active is INTEGER — compare = 1.
        import hashlib as _hl
        key_hash = _hl.sha256(api_key.encode('utf-8')).hexdigest()
        cur.execute(
            """SELECT ak.rate_limit_tier, ak.plan, u.plan, u.email
                 FROM api_keys ak
                 LEFT JOIN users u ON u.id = ak.user_id
                WHERE ak.key_hash IN (%s, %s)
                  AND (ak.is_active IS NULL OR ak.is_active = 1)
                LIMIT 1""",
            (key_hash, api_key))
        arow = cur.fetchone()
        cur.close()
        if not arow:
            return None
        plan = None
        for _p in (arow[2], arow[1], arow[0]):  # users.plan wins, then ak.plan, then tier
            plan = plan or _PAID_KEY_TIERS.get((_p or '').lower().strip())
        if not plan:
            return None  # free-tier key — not a bypass
        return {'id': f'apikey:{api_key[:18]}', 'email': arow[3] or '',
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


# ── 2026-07-10: server-side session-cap primitives ──────────────────────────

def _client_ip_prefix():
    """/24 of the caller's IPv4 (or first 4 hextets of IPv6). Stable per
    network, and — unlike localStorage — not something the client can spoof
    past Cloudflare, which sets CF-Connecting-IP from the real edge socket."""
    ip = (request.headers.get('CF-Connecting-IP')
          or request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
          or request.remote_addr or '')
    if not ip:
        return ''
    if ':' in ip:
        return ':'.join(ip.split(':')[:4])
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3])
    return ip[:24]


def _resolve_caller(get_db_conn):
    """Returns (is_privileged, identity).

    is_privileged → True for paid keys/plans, internal MCP, admin radar, and
    loopback self-calls — these are never session-capped.

    identity → a durable, un-spoofable string for the counter when NOT
    privileged: 'key:…' (any API key), 'user:…' (logged-in free JWT), or
    'ip:…' (/24 fallback). Does at most ONE DB lookup (key OR JWT, not both).
    Fails OPEN: on any error returns (False, ip-identity) so the map never
    breaks — worst case a transient error lets a call through uncounted."""
    # 1. Internal / loopback / admin → privileged, no cap.
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(request.headers.get('X-Internal-Key')):
            return True, None
    except Exception:
        pass
    try:
        # '::ffff:127.0.0.1' is what a dual-stack ([::]) listener reports for an
        # IPv4 loopback connect — without it, a server-to-server localhost call
        # (e.g. routes/radar.py _internal) is metered as an anonymous session
        # and 402s after FREE_MAP_SESSIONS days.
        if request.remote_addr in ('127.0.0.1', '::1', 'localhost',
                                   '::ffff:127.0.0.1'):
            return True, None
    except Exception:
        pass
    _admin = os.environ.get('DCHUB_ADMIN_KEY', '')
    if _admin and request.headers.get('X-Admin-Key') == _admin:
        return True, None

    # 2. API key (X-API-Key / ?api_key / Bearer dchub_…) — paid key bypasses,
    #    any other key still gives a stable identity to count.
    auth = request.headers.get('Authorization', '') or ''
    key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if not key and auth.startswith('Bearer '):
        _t = auth[7:]
        if _t.startswith('dchub_'):
            key = _t
    if key:
        try:
            u = _user_from_api_key(key, get_db_conn)
            if u and u.get('plan') in _PAID_PLANS:
                return True, None
        except Exception:
            pass
        return False, 'key:' + key[:24]

    # 3. Logged-in JWT — paid plan bypasses, free plan gives a stable user id.
    if auth:
        try:
            u = get_user_from_jwt(auth, get_db_conn)
            if u:
                if u.get('plan') in _PAID_PLANS:
                    return True, None
                if u.get('id'):
                    return False, 'user:' + str(u['id'])
        except Exception:
            pass

    # 4. Anonymous browser — key off the /24 IP prefix.
    pfx = _client_ip_prefix()
    return False, ('ip:' + pfx if pfx else None)


def _metered_usage(identity, get_db_conn):
    """(active_days_in_30d, is_today_active) for identity, or None on error."""
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(METERED_USAGE_SQL, (identity,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return 0, False
        return int(row[0] or 0), bool(row[1])
    except Exception as e:
        logger.error(f"metered usage read error: {e}")
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


def _mark_session_today(identity, get_db_conn):
    """Idempotently mark today as an active session-day for identity."""
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(MARK_SESSION_SQL, (identity,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"mark session error: {e}")
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


def _metered_402():
    payload = {
        'error': 'upgrade_required',
        'gate': 'map_session_cap',
        'message': (f"You've used your free map session"
                    f"{'s' if FREE_MAP_SESSIONS != 1 else ''} for this period "
                    f"({FREE_MAP_SESSIONS}/30 days). Upgrade for unlimited map access."),
        'sessions_limit': FREE_MAP_SESSIONS,
        'upgrade_url': 'https://dchub.cloud/pricing?utm_source=map_session_cap',
    }
    # 2026-07-10 (issue #1551 "agent path blocked"): this 402 fronts metered
    # API surfaces (e.g. /api/v1/grid/intelligence/*) for ANONYMOUS callers
    # and used to carry only a human pricing URL — an AI agent hitting it had
    # no machine-readable next step. Attach the shared claim_free_key +
    # email_capture coaching (same envelope as the MCP tool gate and
    # /api/v1/market-brief/all). Keyed callers PASS this gate (verified live:
    # anon 402 -> POST /api/v1/keys/claim 200 -> keyed retry 200), so the
    # coached retry genuinely unlocks. Fail-safe: build_agent_coaching
    # returns {} on any error, so the 402 shape never breaks.
    try:
        from routes.email_capture import build_agent_coaching
        payload.update(build_agent_coaching(
            'metered_map_api',
            'Retry this request with header X-API-Key: <api_key>'))
        # Keep the utm-tagged map-cap pricing URL (coaching's generic
        # /pricing would otherwise clobber the attribution).
        payload['upgrade_url'] = 'https://dchub.cloud/pricing?utm_source=map_session_cap'
    except Exception:
        pass
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp, 402


def _metered_session_gate(get_db_conn):
    """Server-side map SESSION cap. Returns a 402 response tuple when a
    non-privileged caller has exceeded FREE_MAP_SESSIONS active days in the
    trailing 30 days, else None. Only fires on GET to METERED_MAP_PREFIXES.
    Wrapped by the caller in try/except; also fails open internally so a DB
    hiccup can never wall the map."""
    if request.method != 'GET':
        return None
    path = request.path
    # r-watchdog-402 (2026-07-14): the map-metering sweep added the whole
    # '/api/energy-discovery' prefix, which also caught the MONITORING endpoints
    # (status/health) — giving the CI ingest-watchdog intermittent 402s that
    # auto-filed GH issues (#1569/#1605). Never meter the non-proprietary monitors;
    # the proprietary /export/* (KMZ) + map DATA endpoints stay metered.
    if path in ('/api/energy-discovery/status', '/api/energy-discovery/health'):
        return None
    if not any(path == p or path.startswith(p + '/') for p in METERED_MAP_PREFIXES):
        return None
    priv, identity = _resolve_caller(get_db_conn)
    if priv or not identity:
        return None                      # paid/internal/loopback, or unidentifiable → allow
    usage = _metered_usage(identity, get_db_conn)
    if usage is None:
        return None                      # DB error → fail open
    active_days, today = usage
    if not today and active_days >= FREE_MAP_SESSIONS:
        return _metered_402()            # a NEW day beyond the allowance → wall
    if not today:
        _mark_session_today(identity, get_db_conn)  # first hit today → burn one session
    return None                          # within allowance (or already active today) → allow


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
        # r42l (2026-05-26): also bypass on X-Admin-Key match. Otherwise
        # admin endpoints (/api/v1/dcpi/recompute, /api/v1/reports/monthly/archive,
        # etc.) get blocked by the gate before the endpoint's own admin
        # check runs — admin can't even reach the route to call it.
        _admin_key = os.environ.get("DCHUB_ADMIN_KEY", "")
        if _admin_key and request.headers.get("X-Admin-Key") == _admin_key:
            return None
        # 2026-07-10: server-side map SESSION cap. Runs BEFORE the referer
        # bypass below so the metered/proprietary map layers are counted even
        # when the CF worker injects a dchub.cloud Referer. Fully fail-open:
        # any error here must NEVER take the map down, so a broad except lets
        # the request through unchanged.
        try:
            _cap = _metered_session_gate(get_db_conn)
            if _cap is not None:
                return _cap
        except Exception as _e:
            logger.error(f"metered session gate error (failing open): {_e}")
        # Phase ZZZZZ-round22c (2026-05-23): /land-power map endpoint
        # bypass. The user's main complaint was 401/403 errors on map
        # data. enforce_free_tier runs BEFORE any route decorator and
        # returns 401 here for unauthenticated callers, even on
        # explicitly-public map data endpoints. Mirror the same path
        # whitelist + dchub.cloud Origin check from main.py /
        # api_tier_gating's require_plan stub. GET-only - keeps writes
        # gated.
        if request.method == 'GET':
            origin = (request.headers.get('Origin', '')
                       or request.headers.get('Referer', ''))
            if 'dchub.cloud' in origin:
                _MAP_BYPASS_PATHS = (
                    '/api/v1/gas-pipelines',
                    '/api/v1/infrastructure/substations',
                    '/api/v1/infrastructure/transmission',
                    '/api/v1/infrastructure/power-plants',
                    # r66 (2026-06-02): fiber/permits/properties removed — proprietary
                    # data now Pro-gated at the route (@_infra_require_plan). See the
                    # matching note in api_tier_gating._MAP_BYPASS_PATHS.
                    '/api/v1/infrastructure/nearby',
                    '/api/v1/infrastructure/summary',
                    '/api/v1/energy/power-plants',
                    '/api/v1/energy/power-plants/nearby',
                    '/api/v1/energy/rto/demand',
                    '/api/v1/energy/rto/fuelmix',
                    '/api/v1/energy/naturalgas/price',
                    '/api/v1/energy/retail/rates',
                    '/api/v1/energy/gas-storage',
                    '/api/v1/fiber/routes',
                    '/api/v1/fiber/sources',
                    '/api/v1/fiber/intel',
                    # FCC BDC provider footprints (2026-06-13): public-domain
                    # government data (anyone can pull it from the FCC), so the
                    # map page may load it like substations/transmission.
                    '/api/v1/fiber/providers',
                    '/api/v1/fiber/footprint',
                    # 2026-07-17: removed /api/v1/connectivity/{ixps,facilities,score}
                    # here and in api_tier_gating._MAP_BYPASS_PATHS (kept in sync).
                    # NOTHING serves those paths — not main.py, not any blueprint
                    # (routes/connectivity_score.py serves /api/infrastructure/
                    # connectivity/score, a different path). Verified 404 in prod.
                    # They were no-ops regardless: no '/api/v1/connectivity'
                    # GATED_PREFIX -> is_gated()=False -> already allowed. Their
                    # frontend callers (js/energy-enhancement-v3.js) have been
                    # silently 404ing; fixing that is a frontend change, not this.
                    # r-grid-catchall (2026-08-08): /api/v1/grid/overview and
                    # /api/v1/grid/status have NO handler. They reached the
                    # /api/v1/grid/<iso> converter, which forwarded them as REGIONS —
                    # and because these two were bypass-listed they skipped the plan
                    # gate and answered 200 with a fabricated region ("STATUS",
                    # "OVERVIEW"). The converter now 404s an unknown region, so these
                    # entries describe nothing. Removed rather than left as dead config.
                    # 2026-06-25: removed markets/compare + pipeline/summary here to SYNC with
                    # api_tier_gating._MAP_BYPASS_PATHS (r36 removed them there). No-op in this layer
                    # anyway (neither matches a GATED_PREFIX -> is_gated()=False -> already allowed);
                    # pipeline/summary is Pro (api_tier_gating + worker force-origin), markets/compare freemium.
                    '/api/v1/oilgas/search',
                    '/api/v1/deals',
                    '/api/v1/power-plants/nearby',
                    '/api/v2/infrastructure/hifld/transmission',
                    '/api/v2/connectivity/ixps',
                    '/api/v2/risk/active-fires',
                    '/api/v2/risk/fire-history',
                    '/api/facilities',
                    '/api/deals',
                    '/api/grid/demand',
                    '/api/grid/prices',
                    '/api/grid/all-isos',
                    '/api/grid/supported-isos',
                    '/api/discovery/facilities',
                    '/api/epa/facilities',
                    '/api/renewable/solar',
                    '/api/renewable/wind',
                    '/api/renewable/combined',
                    '/api/site-score',
                    '/api/carbon/intensity',
                    '/api/risk/assessment',
                    '/api/auth/me',
                )
                if any(path == p or path.startswith(p + '/')
                       for p in _MAP_BYPASS_PATHS):
                    return None
        if is_always_open(path):
            return None
        if not is_gated(path):
            return None

        # r41-internal-bypass (2026-05-25): brain radar, scheduler, MCP
        # server backend probes ship X-Internal-Key. Previously the gate
        # only honored JWT or X-API-Key, so every brain-radar probe to
        # /api/v1/fiber/intel et al got 401 — radar went blind to those
        # endpoints. Now we accept the canonical internal-key (validated
        # against DCHUB_INTERNAL_KEY / DCHUB_SYNC_KEY / INTERNAL_WORKER_SECRET
        # env vars via internal_auth.is_valid_internal_key, constant-time
        # compare). Lazy import to avoid a circular dep at module load.
        try:
            from internal_auth import is_valid_internal_key
            if is_valid_internal_key(request.headers.get('X-Internal-Key')):
                return None
        except Exception:
            # If internal_auth can't be imported, fall through to the
            # regular auth path — never block a real user on this.
            pass

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

        # 2026-07-10: keyed off the same durable identity + idempotent per-day
        # accounting as the server-side gate, so the client UX counter matches
        # what the server actually enforces and a load never double-burns a
        # session. Fails open (never 500s the map boot).
        priv, identity = _resolve_caller(get_db_conn)
        if priv:
            return jsonify({'status': 'ok', 'unlimited': True})
        if not identity:
            return jsonify({'status': 'ok', 'sessions_limit': FREE_MAP_SESSIONS})

        usage = _metered_usage(identity, get_db_conn)
        if usage is None:
            return jsonify({'status': 'ok'})          # DB error → fail open
        active_days, today = usage
        if not today and active_days >= FREE_MAP_SESSIONS:
            return jsonify({
                'status': 'limit_reached',
                'message': 'Free map session already used for this period.',
                'sessions_used': active_days,
                'sessions_limit': FREE_MAP_SESSIONS,
                'upgrade_url': 'https://dchub.cloud/pricing'
            }), 403
        if not today:
            _mark_session_today(identity, get_db_conn)
        return jsonify({
            'status': 'ok',
            'plan': 'free',
            'sessions_used': active_days if today else active_days + 1,
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
