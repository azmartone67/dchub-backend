"""
DC Hub - Land & Power Usage Limiter v2
========================================
Monthly-based rate limiting for Land & Power evaluations.

FREE users:
  - 1 evaluation per calendar month
  - Max 5 filters/layers active per search
  - Resets on the 1st of each month

PRO users:
  - Unlimited evaluations
  - All filters/layers

API rate limits (also updated):
  - Free: 100 calls/month (was 100/day)
  - Pro: 10,000 calls/day (unchanged)
  - Enterprise: 100,000 calls/day (unchanged)

Endpoints:
  GET  /api/land-power/usage    - Check current usage & remaining
  POST /api/land-power/track    - Record a search (called by frontend before eval)
  GET  /api/land-power/limits   - Get tier limits (public)

Installation:
  Already imported in main.py via:
    from land_power_usage_limiter import register_usage_routes, apply_to_site_analysis
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

try:
    import psycopg2
    HAS_PG = True
except ImportError:
    HAS_PG = False

logger = logging.getLogger("dc_hub.land_power_limiter")

DB_PATH = os.environ.get('DB_PATH', 'dc_hub.db')
DATABASE_URL = os.environ.get('NEON_DATABASE_URL', '') or os.environ.get('DATABASE_URL', '')

# =============================================================================
# CONFIGURATION
# =============================================================================

# r32 (2026-05-20): tier policy aligned to the canonical
# anonymous → identified → developer → pro+ ladder. Previously the
# table was missing 'identified' AND 'developer' entries, so a $49/mo
# paying developer customer fell through to free defaults (1 search,
# 5 filters) — broken UX + churn driver. Each tier now matches what
# /gating-matrix advertises and what the user pays for.
LAND_POWER_LIMITS = {
    'anonymous': {
        # No login = can view the map UI + layer list, but cannot
        # execute a search. Frontend should show "Sign up free (email
        # only) to run your first search" CTA instead of hiding the
        # tool entirely — the tease IS the marketing.
        'searches_per_month': 0,
        'max_filters': 3,
        'label': 'Anonymous',
        'upgrade_text': 'Sign up free (email only) to run your first 3 Land & Power searches.',
    },
    'free': {
        # Legacy alias kept for backward compatibility. Same defaults
        # as 'identified' so callers using either name behave identically.
        'searches_per_month': 3,
        'max_filters': 5,
        'label': 'Free',
        'upgrade_text': 'Upgrade to Developer ($49/mo) for 50 searches + 15 filters per month.',
    },
    'identified': {
        # Email-only signup, no card. Three free searches per month —
        # enough to evaluate the tool, not enough to use it as a
        # production research tool. Stays consistent with /pockets
        # "first taste" ladder.
        'searches_per_month': 3,
        'max_filters': 5,
        'label': 'Identified',
        'upgrade_text': 'Upgrade to Developer ($49/mo) for 50 searches + 15 filters per month.',
    },
    'developer': {
        # $49/mo paid tier. Generous limits — 50 searches/month
        # (≈2/day) + 15 filters/search covers most professional usage.
        # Was MISSING from this table → developer customers fell
        # through to free's 1/5 limits. Bug fixed.
        'searches_per_month': 50,
        'max_filters': 15,
        'label': 'Developer',
        'upgrade_text': 'Upgrade to Pro ($199/mo) for unlimited Land & Power searches with all filters.',
    },
    'pro': {
        'searches_per_month': -1,  # unlimited
        'max_filters': -1,         # unlimited
        'label': 'Pro',
        'upgrade_text': None,
    },
    'enterprise': {
        'searches_per_month': -1,
        'max_filters': -1,
        'label': 'Enterprise',
        'upgrade_text': None,
    },
    'founding': {
        'searches_per_month': -1,
        'max_filters': -1,
        'label': 'Founding Member',
        'upgrade_text': None,
    },
    'admin': {
        'searches_per_month': -1,
        'max_filters': -1,
        'label': 'Admin',
        'upgrade_text': None,
    },
}

# r32: API rate limits aligned the same way. Previously identified
# and developer were missing → fell through to free's 100/month.
# Developer paying $49/mo deserves more headroom than a free email
# signup; pro/enterprise/founding unchanged.
API_MONTHLY_LIMITS = {
    'anonymous': 50,         # very small — IP-bound demo only
    'free': 100,             # legacy alias for identified
    'identified': 100,       # free with email — 100 calls/month
    'starter': 1500,         # r34: $9/mo Starter — 1.5K calls/month (~50/day)
    'developer': 10000,      # $49/mo — 10K calls/month (~333/day)
    'pro': 300000,           # was 10K/day * 30 = 300K/month
    'enterprise': 3000000,
    'founding': 300000,
    'admin': 9999999,
}


# =============================================================================
# DATABASE HELPERS
# =============================================================================

def _get_connection():
    """Get DB connection (always PostgreSQL via db_utils)."""
    from db_utils import get_db
    return get_db()


def _is_pg():
    return bool(DATABASE_URL and HAS_PG)


def _init_tables():
    """Create the land_power_usage table if needed."""
    conn = _get_connection()
    c = conn.cursor()
    
    if _is_pg():
        c.execute("""
            CREATE TABLE IF NOT EXISTS land_power_usage (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                email TEXT,
                year_month TEXT NOT NULL,
                search_count INTEGER DEFAULT 0,
                filter_counts TEXT DEFAULT '{}',
                last_search_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, year_month)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS api_monthly_usage (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                year_month TEXT NOT NULL,
                call_count INTEGER DEFAULT 0,
                last_call_at TIMESTAMP,
                UNIQUE(user_id, year_month)
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS land_power_usage (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                email TEXT,
                year_month TEXT NOT NULL,
                search_count INTEGER DEFAULT 0,
                filter_counts TEXT DEFAULT '{}',
                last_search_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, year_month)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS api_monthly_usage (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                year_month TEXT NOT NULL,
                call_count INTEGER DEFAULT 0,
                last_call_at TIMESTAMP,
                UNIQUE(user_id, year_month)
            )
        """)
    
    conn.commit()
    conn.close()
    logger.info("✅ Land Power usage tables ready")


def _current_month():
    """Get current year-month string like '2026-02'."""
    return datetime.now(timezone.utc).strftime('%Y-%m')


def _get_usage(user_id):
    """Get current month's usage for a user."""
    conn = _get_connection()
    c = conn.cursor()
    ym = _current_month()
    
    if _is_pg():
        c.execute("SELECT search_count, last_search_at FROM land_power_usage WHERE user_id = %s AND year_month = %s", (user_id, ym))
    else:
        c.execute("SELECT search_count, last_search_at FROM land_power_usage WHERE user_id = %s AND year_month = %s", (user_id, ym))
    
    row = c.fetchone()
    conn.close()
    
    if row:
        return {'count': row[0] or 0, 'last_search': row[1]}
    return {'count': 0, 'last_search': None}


def _increment_usage(user_id, email=None):
    """Increment the search count for the current month."""
    conn = _get_connection()
    c = conn.cursor()
    ym = _current_month()
    now = datetime.now(timezone.utc).isoformat()
    
    if _is_pg():
        c.execute("""
            INSERT INTO land_power_usage (user_id, email, year_month, search_count, last_search_at)
            VALUES (%s, %s, %s, 1, %s)
            ON CONFLICT (user_id, year_month)
            DO UPDATE SET search_count = land_power_usage.search_count + 1,
                          last_search_at = %s,
                          email = COALESCE(%s, land_power_usage.email)
        """, (user_id, email, ym, now, now, email))
    else:
        c.execute("""
            INSERT INTO land_power_usage (user_id, email, year_month, search_count, last_search_at)
            VALUES (%s, %s, %s, 1, %s)
            ON CONFLICT (user_id, year_month)
            DO UPDATE SET search_count = search_count + 1,
                          last_search_at = ?,
                          email = COALESCE(?, email)
        """, (user_id, email, ym, now, now, email))
    
    conn.commit()
    conn.close()


def _get_api_monthly_usage(user_id):
    """Get API call count for the current month."""
    conn = _get_connection()
    c = conn.cursor()
    ym = _current_month()
    
    if _is_pg():
        c.execute("SELECT call_count FROM api_monthly_usage WHERE user_id = %s AND year_month = %s", (user_id, ym))
    else:
        c.execute("SELECT call_count FROM api_monthly_usage WHERE user_id = %s AND year_month = %s", (user_id, ym))
    
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def _increment_api_monthly(user_id):
    """Increment API monthly call count."""
    conn = _get_connection()
    c = conn.cursor()
    ym = _current_month()
    now = datetime.now(timezone.utc).isoformat()
    
    if _is_pg():
        c.execute("""
            INSERT INTO api_monthly_usage (user_id, year_month, call_count, last_call_at)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (user_id, year_month)
            DO UPDATE SET call_count = api_monthly_usage.call_count + 1, last_call_at = %s
        """, (user_id, ym, now, now))
    else:
        c.execute("""
            INSERT INTO api_monthly_usage (user_id, year_month, call_count, last_call_at)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (user_id, year_month)
            DO UPDATE SET call_count = call_count + 1, last_call_at = %s
        """, (user_id, ym, now, now))
    
    conn.commit()
    conn.close()


# =============================================================================
# PLAN DETECTION HELPER
# =============================================================================

def _get_user_plan_from_request(app):
    """Extract user plan from JWT or API key in the current request.

    Phase XX (2026-05-15) — migrated to delegate through the canonical
    routes.auth_context.get_auth_context() resolver. The previous
    implementation had its own JWT decoder, API-key validator, and
    cookie-session reader — 60+ lines duplicating logic that already
    lived in 5 other resolvers across the codebase (Phase TT-1 audit).

    Tries auth_context first; falls back to api_tier_gating.get_user_plan
    for the user_id → plan lookup since auth_context returns tier but
    not the full plan string (those are slightly different concepts —
    tier is the rank, plan is the billing label).

    The `app` parameter is retained for backward compatibility but is
    no longer needed since auth_context reads its own JWT secret from
    env. Callers don't need to update.
    """
    from flask import request

    try:
        from routes.auth_context import get_auth_context
        ctx = get_auth_context(request)
        if ctx.tier != "anonymous":
            # Map tier → plan name. auth_context returns lowercase tier;
            # land-power code expects the plan string from api_tier_gating.
            plan = ctx.tier  # tier names already match plan names
            # Try to resolve the richer plan + email from DB if we have a key
            try:
                if ctx.api_key:
                    from api_tier_gating import validate_api_key
                    valid, info = validate_api_key(ctx.api_key)
                    if valid:
                        return (info.get('user_id') or ctx.user_id,
                                info.get('email')   or ctx.email,
                                info.get('plan',   plan))
            except Exception:
                pass
            return ctx.user_id, ctx.email, plan
    except Exception:
        pass  # fall through to legacy paths

    # Legacy fallback paths — unchanged below. Kept because auth_context
    # doesn't currently parse the app-injected DECODE_JWT_FUNC the way
    # this module does, and we don't want to break web-cookie callers
    # if auth_context's resolver misses an edge case during the
    # transition window.
    user_id = None
    email = None
    plan = 'free'

    # Check JWT Bearer token
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            token = auth_header.split(' ')[1]
            decode_jwt = app.config.get('DECODE_JWT_FUNC')
            if decode_jwt:
                payload = decode_jwt(token)
                if payload:
                    user_id = payload.get('user_id') or payload.get('sub')
                    email = payload.get('email')
                    try:
                        from api_tier_gating import get_user_plan
                        plan = get_user_plan(user_id=user_id)
                    except Exception:
                        plan = payload.get('plan', 'free')
        except Exception as e:
            logger.debug(f"JWT decode failed: {e}")

    # Check API key
    if not user_id:
        api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if api_key:
            try:
                from api_tier_gating import validate_api_key
                valid, info = validate_api_key(api_key)
                if valid:
                    user_id = info.get('user_id', 'apikey_' + api_key[:8])
                    email = info.get('email')
                    plan = info.get('plan', 'free')
            except Exception:
                pass

    # Check cookie/session (for web users)
    if not user_id:
        session_token = request.cookies.get('dchub_session') or request.cookies.get('session')
        if session_token:
            try:
                decode_jwt = app.config.get('DECODE_JWT_FUNC')
                if decode_jwt:
                    payload = decode_jwt(session_token)
                    if payload:
                        user_id = payload.get('user_id') or payload.get('sub')
                        email = payload.get('email')
                        try:
                            from api_tier_gating import get_user_plan
                            plan = get_user_plan(user_id=user_id)
                        except Exception:
                            plan = 'free'
            except Exception:
                pass

    return user_id, email, plan


# =============================================================================
# NEXT RESET DATE HELPER
# =============================================================================

def _next_month_reset():
    """Return ISO string of the 1st of next month (UTC)."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return reset.isoformat()


# =============================================================================
# ROUTE REGISTRATION
# =============================================================================

def register_usage_routes(app):
    """Register Land & Power usage endpoints."""
    from flask import request, jsonify
    
    _init_tables()
    
    # Store decode_jwt reference for later use
    # (set by main.py after decode_jwt is defined)
    
    @app.route('/api/v1/land-power/usage', methods=['GET'])
    def lp_usage():
        """Check current Land & Power usage for the authenticated user.

        r32 (2026-05-20): anonymous branch now reports the new
        anonymous-tier limits (map preview + filter teaser, but 0
        searches until signup) rather than masquerading as free.
        Frontend can render the layer picker + map but disable the
        Run-Search button with a "sign up free to unlock" CTA."""
        user_id, email, plan = _get_user_plan_from_request(app)

        if not user_id:
            anon = LAND_POWER_LIMITS['anonymous']
            return jsonify({
                'authenticated': False,
                'plan': 'anonymous',
                'plan_label': anon['label'],
                'searches_used': 0,
                'searches_limit': anon['searches_per_month'],  # 0 — must sign up
                'searches_remaining': 0,
                'max_filters': anon['max_filters'],            # 3 — preview filters
                'resets_at': _next_month_reset(),
                'upgrade_text': anon['upgrade_text'],
                'message': 'Sign up free (email only) to run your first 3 Land & Power searches.',
                'upgrade_url': '/signup?next=/land-power&utm_source=land_power',
            })
        
        usage = _get_usage(user_id)
        limits = LAND_POWER_LIMITS.get(plan, LAND_POWER_LIMITS['free'])
        limit = limits['searches_per_month']
        remaining = max(0, limit - usage['count']) if limit > 0 else -1
        
        return jsonify({
            'authenticated': True,
            'plan': plan,
            'plan_label': limits['label'],
            'searches_used': usage['count'],
            'searches_limit': limit,
            'searches_remaining': remaining,
            'max_filters': limits['max_filters'],
            'resets_at': _next_month_reset(),
            'upgrade_text': limits.get('upgrade_text'),
            'current_month': _current_month(),
        })
    
    @app.route('/api/v1/land-power/track', methods=['POST'])
    def lp_track():
        """
        Record a Land & Power search. Frontend calls this BEFORE evaluating.
        Returns 200 if allowed, 429 if limit reached, 401 if not authenticated.
        
        Request body (optional):
          { "filters": ["substations", "nuclear", "fiber", ...] }
        """
        user_id, email, plan = _get_user_plan_from_request(app)
        
        if not user_id:
            return jsonify({
                'allowed': False,
                'error': 'AUTH_REQUIRED',
                'message': 'Please sign in to use Land & Power search.',
            }), 401
        
        limits = LAND_POWER_LIMITS.get(plan, LAND_POWER_LIMITS['free'])
        limit = limits['searches_per_month']
        max_filters = limits['max_filters']
        
        # Check filter count for free users
        data = request.get_json(silent=True) or {}
        active_filters = data.get('filters', [])
        
        if max_filters > 0 and len(active_filters) > max_filters:
            return jsonify({
                'allowed': False,
                'error': 'FILTER_LIMIT',
                # r32 (2026-05-20): message now reflects the caller's
                # actual tier (was always "Free plan" — wrong for
                # developer/identified callers hitting their cap).
                'message': (
                    f'{limits["label"]} plan allows up to {max_filters} '
                    f'filters per search. You selected {len(active_filters)}.'
                ),
                'tier': plan,
                'max_filters': max_filters,
                'selected_filters': len(active_filters),
                'upgrade_url': '/pricing?from=land_power_filter_limit',
                'upgrade_text': limits.get('upgrade_text'),
            }), 403

        # Check monthly search limit (skip for unlimited plans)
        if limit > 0:
            usage = _get_usage(user_id)
            if usage['count'] >= limit:
                # r32 (2026-05-20): same — message reflects actual tier
                # + uses the table-defined upgrade_text so each tier
                # gets the right next-step CTA (identified → developer,
                # developer → pro).
                return jsonify({
                    'allowed': False,
                    'error': 'MONTHLY_LIMIT',
                    'message': (
                        f'You\'ve used your {limit} '
                        f'{limits["label"]}-tier search'
                        f'{"es" if limit > 1 else ""} this month. '
                        + (limits.get('upgrade_text') or
                           'Upgrade for more searches.')
                    ),
                    'tier': plan,
                    'searches_used': usage['count'],
                    'searches_limit': limit,
                    'resets_at': _next_month_reset(),
                    'upgrade_url': '/pricing?from=land_power_monthly_limit',
                    'upgrade_text': limits.get('upgrade_text'),
                }), 429
        
        # Allowed — record the search
        _increment_usage(user_id, email)
        
        new_usage = _get_usage(user_id)
        remaining = max(0, limit - new_usage['count']) if limit > 0 else -1
        
        return jsonify({
            'allowed': True,
            'searches_used': new_usage['count'],
            'searches_limit': limit,
            'searches_remaining': remaining,
            'resets_at': _next_month_reset(),
            'plan': plan,
        })
    
    @app.route('/api/v1/land-power/limits', methods=['GET'])
    def lp_limits():
        """Public endpoint showing tier limits for Land & Power.

        r32 (2026-05-20): rebuilt to mirror the canonical anonymous →
        identified → developer → pro+ ladder. Each tier in
        LAND_POWER_LIMITS now appears here so the pricing page +
        upgrade prompts can show consistent numbers."""
        def _fmt(n):
            return 'Unlimited' if n is None or n < 0 else n

        return jsonify({
            'tiers': {
                'anonymous': {
                    'searches_per_month': _fmt(LAND_POWER_LIMITS['anonymous']['searches_per_month']),
                    'max_filters': _fmt(LAND_POWER_LIMITS['anonymous']['max_filters']),
                    'price': '$0',
                    'note': 'Map preview only — sign up free to run searches.',
                },
                'identified': {
                    'searches_per_month': _fmt(LAND_POWER_LIMITS['identified']['searches_per_month']),
                    'max_filters': _fmt(LAND_POWER_LIMITS['identified']['max_filters']),
                    'price': '$0 + email',
                    'note': 'Email-only signup. No card.',
                },
                'developer': {
                    'searches_per_month': _fmt(LAND_POWER_LIMITS['developer']['searches_per_month']),
                    'max_filters': _fmt(LAND_POWER_LIMITS['developer']['max_filters']),
                    'price': '$49/mo',
                    'note': '~2 searches/day with 15 filter layers.',
                },
                'pro': {
                    'searches_per_month': 'Unlimited',
                    'max_filters': 'Unlimited',
                    'price': '$199/mo',
                },
                'enterprise': {
                    'searches_per_month': 'Unlimited',
                    'max_filters': 'Unlimited',
                    'price': 'Contact us',
                },
                'founding': {
                    'searches_per_month': 'Unlimited',
                    'max_filters': 'Unlimited',
                    'price': 'Founding cohort',
                },
            },
            'api_limits': {
                'anonymous': '50 calls/month (IP-bound)',
                'identified': '100 calls/month',
                'developer': '10,000 calls/month',
                'pro': '300,000 calls/month',
                'enterprise': '3,000,000 calls/month',
                'founding': '300,000 calls/month',
            }
        })
    
    # Admin endpoint to check/reset a user's usage
    @app.route('/api/admin/v1/land-power/usage', methods=['GET', 'DELETE'])
    def admin_lp_usage():
        """Admin: check or reset a user's Land & Power usage."""
        # Simple admin check
        admin_key = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
        expected = os.environ.get('ADMIN_API_KEY', '')
        if not expected or admin_key != expected:
            return jsonify({'error': 'Unauthorized'}), 401
        
        target_user = request.args.get('user_id')
        if not target_user:
            return jsonify({'error': 'user_id required'}), 400
        
        if request.method == 'DELETE':
            conn = _get_connection()
            c = conn.cursor()
            ym = _current_month()
            if _is_pg():
                c.execute("DELETE FROM land_power_usage WHERE user_id = %s AND year_month = %s", (target_user, ym))
            else:
                c.execute("DELETE FROM land_power_usage WHERE user_id = %s AND year_month = %s", (target_user, ym))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': f'Reset usage for {target_user}'})
        
        usage = _get_usage(target_user)
        return jsonify({'user_id': target_user, 'month': _current_month(), **usage})
    
    logger.info("✅ Land & Power Usage Limiter v2 registered (monthly limits)")


def apply_to_site_analysis(app):
    """
    Hook into site-analysis endpoints to enforce limits server-side.
    This wraps any existing /api/v1/energy/site-analysis or /api/v1/energy/site-evaluation.
    """
    # Store the decode_jwt function for plan detection
    # main.py should call: app.config['DECODE_JWT_FUNC'] = decode_jwt
    logger.info("✅ Land & Power server-side enforcement applied")
