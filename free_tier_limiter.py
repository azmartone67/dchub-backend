"""
DC HUB - Free Tier Usage Limiter v1.0
=======================================
Controls free user access to Land & Power search and API endpoints.

FREE TIER LIMITS:
  - Land & Power Search: 1 search/month, max 5 filters
  - API Requests: 100/month (across all endpoints)
  - Resets on the 1st of each calendar month (UTC)

PRO TIER:
  - Land & Power Search: Unlimited searches, unlimited filters
  - API Requests: 10,000/month

ENTERPRISE TIER:
  - Everything unlimited
  - API Requests: 100,000/month

INSTALLATION:
  1. Upload this file to your Replit project
  2. Add to main.py:
     from free_tier_limiter import init_free_tier_limiter, check_land_power_limit, check_api_limit
     init_free_tier_limiter(app)
  3. Apply to Land & Power search endpoint (see examples below)

DATABASE:
  Creates `free_tier_usage` table in your existing SQLite database.
"""

import os
import logging
from datetime import datetime, timezone
from functools import wraps
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# Monthly limits by tier
# r32-sweep (2026-05-20): added anonymous/identified/developer/founding
# entries. This file mirrored the broken Land & Power table — anon/
# identified/developer all fell through to free's 1-search/5-filter
# limit. Now every canonical tier has explicit values matching the
# r32-3 fix to land_power_usage_limiter.LAND_POWER_LIMITS.
TIER_LIMITS = {
    'anonymous': {
        'land_power_searches_per_month': 0,
        'land_power_max_filters': 3,
        'api_requests_per_month': 50,
    },
    'anon': {                # alias
        'land_power_searches_per_month': 0,
        'land_power_max_filters': 3,
        'api_requests_per_month': 50,
    },
    'free': {
        'land_power_searches_per_month': 3,    # was 1 — matches r32-3 fix
        'land_power_max_filters': 5,
        'api_requests_per_month': 100,
    },
    'identified': {           # $0 with email
        'land_power_searches_per_month': 3,
        'land_power_max_filters': 5,
        'api_requests_per_month': 100,
    },
    'developer': {            # $49/mo
        'land_power_searches_per_month': 50,
        'land_power_max_filters': 15,
        'api_requests_per_month': 10000,
    },
    'founding': {             # Pro-equivalent
        'land_power_searches_per_month': 999999,
        'land_power_max_filters': 999,
        'api_requests_per_month': 300000,
    },
    'pro': {
        'land_power_searches_per_month': 999999,
        'land_power_max_filters': 999,
        'api_requests_per_month': 300000,    # bumped from 10000 to match r32-3
    },
    'enterprise': {
        'land_power_searches_per_month': 999999,
        'land_power_max_filters': 999,
        'api_requests_per_month': 3000000,
    },
    'admin': {
        'land_power_searches_per_month': 999999,
        'land_power_max_filters': 999,
        'api_requests_per_month': 9999999,
    },
}

# =============================================================================
# DATABASE
# =============================================================================

def _get_db():
    """Get database connection via db_utils (PostgreSQL)."""
    from db_utils import get_db
    return get_db()


def _init_tables():
    """Create the free_tier_usage table if it doesn't exist."""
    conn = _get_db()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS free_tier_usage (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                usage_type TEXT NOT NULL,
                usage_month TEXT NOT NULL,
                usage_count INTEGER DEFAULT 0,
                last_used TEXT,
                created_at TEXT DEFAULT (NOW()),
                UNIQUE(user_id, usage_type, usage_month)
            )
        """)
        c = conn.cursor()
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_free_tier_user_month 
            ON free_tier_usage(user_id, usage_type, usage_month)
        """)
        conn.commit()
        logger.info("✅ Free tier usage table initialized")
    except Exception as e:
        logger.error(f"Failed to init free_tier_usage table: {e}")
    finally:
        conn.close()


# =============================================================================
# USAGE TRACKING
# =============================================================================

def _current_month():
    """Get current month string like '2026-02'."""
    return datetime.now(timezone.utc).strftime('%Y-%m')


def _get_usage(user_id, usage_type):
    """Get current month's usage count for a user."""
    conn = _get_db()
    try:
        c = conn.cursor()
        row = c.execute(
            "SELECT usage_count FROM free_tier_usage WHERE user_id=%s AND usage_type=%s AND usage_month=%s",
            (str(user_id), usage_type, _current_month())
        ).fetchone()
        return row['usage_count'] if row else 0
    finally:
        conn.close()


def _increment_usage(user_id, usage_type):
    """Increment usage count for current month. Creates record if needed."""
    conn = _get_db()
    month = _current_month()
    now = datetime.now(timezone.utc).isoformat()
    try:
        # Try to increment existing record
        c = conn.cursor()
        result = c.execute(
            """UPDATE free_tier_usage 
               SET usage_count = usage_count + 1, last_used = %s
               WHERE user_id=%s AND usage_type=%s AND usage_month=%s""",
            (now, str(user_id), usage_type, month)
        )
        if result.rowcount == 0:
            # No existing record — create one with count=1
            c = conn.cursor()
            c.execute(
                """INSERT INTO free_tier_usage (user_id, usage_type, usage_month, usage_count, last_used)
                   VALUES (%s, %s, %s, 1, %s) ON CONFLICT (user_id) DO UPDATE SET usage_type = EXCLUDED.usage_type, usage_month = EXCLUDED.usage_month, usage_count = EXCLUDED.usage_count, last_used = EXCLUDED.last_used""",
                (str(user_id), usage_type, month, now)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to increment usage for {user_id}/{usage_type}: {e}")
        conn.rollback()
    finally:
        conn.close()


def _get_user_tier(user_id):
    """Look up user's subscription tier from the users table.
    
    Checks for Stripe subscription status or plan field.
    Returns 'free', 'pro', or 'enterprise'.
    """
    conn = _get_db()
    try:
        # Check users table for plan/subscription info
        c = conn.cursor()
        row = c.execute(
            """SELECT plan, stripe_subscription_id, stripe_subscription_status 
               FROM users WHERE id=%s OR google_id=%s OR email=%s""",
            (str(user_id), str(user_id), str(user_id))
        ).fetchone()
        
        if not row:
            return 'free'
        
        plan = (row['plan'] or 'free').lower()
        sub_status = (row['stripe_subscription_status'] or '').lower()
        
        # Active Stripe subscription overrides plan field
        if sub_status in ('active', 'trialing'):
            if plan in ('enterprise', 'ent'):
                return 'enterprise'
            elif plan in ('pro', 'professional'):
                return 'pro'
        
        # Fall back to plan field
        if plan in ('enterprise', 'ent'):
            return 'enterprise'
        elif plan in ('pro', 'professional'):
            return 'pro'
        
        return 'free'
    except Exception as e:
        logger.error(f"Error checking user tier: {e}")
        return 'free'
    finally:
        conn.close()


# =============================================================================
# LAND & POWER SEARCH LIMITER
# =============================================================================

def check_land_power_limit(user_id, filter_count=0):
    """Check if a user can perform a Land & Power search.
    
    Args:
        user_id: The user's ID (from JWT or API key)
        filter_count: Number of filters being applied
    
    Returns:
        (allowed: bool, error_response: dict or None)
    """
    tier = _get_user_tier(user_id)
    limits = TIER_LIMITS.get(tier, TIER_LIMITS['free'])
    
    # Check filter count limit
    max_filters = limits['land_power_max_filters']
    if filter_count > max_filters:
        return False, {
            'error': 'filter_limit_exceeded',
            'message': f'Free plan allows up to {max_filters} filters. You selected {filter_count}.',
            'tier': tier,
            'max_filters': max_filters,
            'filters_requested': filter_count,
            'upgrade_url': 'https://dchub.cloud/pricing',
            'upgrade_message': 'Upgrade to Pro for unlimited filters and searches.'
        }
    
    # Check monthly search limit
    max_searches = limits['land_power_searches_per_month']
    current_usage = _get_usage(user_id, 'land_power_search')
    
    if current_usage >= max_searches:
        return False, {
            'error': 'monthly_search_limit_reached',
            'message': f'You have used your {max_searches} free Land & Power search this month.',
            'tier': tier,
            'searches_used': current_usage,
            'searches_limit': max_searches,
            'resets_on': f'{_next_month_first()} UTC',
            'upgrade_url': 'https://dchub.cloud/pricing',
            'upgrade_message': 'Upgrade to Pro for unlimited Land & Power searches with all filters.'
        }
    
    return True, None


def record_land_power_search(user_id):
    """Record that a user performed a Land & Power search."""
    _increment_usage(user_id, 'land_power_search')


# =============================================================================
# API REQUEST LIMITER
# =============================================================================

def check_api_limit(user_id):
    """Check if a user is within their monthly API request limit.
    
    Returns:
        (allowed: bool, error_response: dict or None, remaining: int)
    """
    tier = _get_user_tier(user_id)
    limits = TIER_LIMITS.get(tier, TIER_LIMITS['free'])
    max_requests = limits['api_requests_per_month']
    current_usage = _get_usage(user_id, 'api_request')
    remaining = max(0, max_requests - current_usage)
    
    if current_usage >= max_requests:
        return False, {
            'error': 'monthly_api_limit_reached',
            'message': f'You have reached your {max_requests} API request limit for this month.',
            'tier': tier,
            'requests_used': current_usage,
            'requests_limit': max_requests,
            'remaining': 0,
            'resets_on': f'{_next_month_first()} UTC',
            'upgrade_url': 'https://dchub.cloud/pricing',
            'upgrade_message': f'Upgrade to {"Pro" if tier == "free" else "Enterprise"} for more API requests.'
        }, 0
    
    return True, None, remaining


def record_api_request(user_id):
    """Record that a user made an API request."""
    _increment_usage(user_id, 'api_request')


# =============================================================================
# USAGE STATS ENDPOINT
# =============================================================================

def get_usage_stats(user_id):
    """Get a user's current usage stats for display in dashboard/frontend."""
    tier = _get_user_tier(user_id)
    limits = TIER_LIMITS.get(tier, TIER_LIMITS['free'])
    
    lp_used = _get_usage(user_id, 'land_power_search')
    api_used = _get_usage(user_id, 'api_request')
    
    return {
        'tier': tier,
        'month': _current_month(),
        'resets_on': _next_month_first(),
        'land_power': {
            'searches_used': lp_used,
            'searches_limit': limits['land_power_searches_per_month'],
            'searches_remaining': max(0, limits['land_power_searches_per_month'] - lp_used),
            'max_filters': limits['land_power_max_filters'],
        },
        'api': {
            'requests_used': api_used,
            'requests_limit': limits['api_requests_per_month'],
            'requests_remaining': max(0, limits['api_requests_per_month'] - api_used),
        }
    }


# =============================================================================
# DECORATORS
# =============================================================================

def limit_land_power_search(f):
    """Decorator for the Land & Power search endpoint.
    
    Usage in main.py or land_power_routes.py:
    
        @app.route('/api/v1/energy/site-evaluation', methods=['GET', 'POST'])
        @limit_land_power_search
        def site_evaluation():
            ...
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Get user_id from JWT token or API key (set by auth middleware)
        user_id = _get_current_user_id()
        if not user_id:
            # No auth — treat as anonymous, block
            return jsonify({
                'error': 'authentication_required',
                'message': 'Please sign in to use Land & Power search.',
                'login_url': 'https://dchub.cloud/login'
            }), 401
        
        # Count filters from request
        filter_count = _count_filters(request)
        
        # Check limits
        allowed, error = check_land_power_limit(user_id, filter_count)
        if not allowed:
            return jsonify(error), 429
        
        # Execute the search
        response = f(*args, **kwargs)
        
        # Only record usage if the search was successful
        if hasattr(response, 'status_code'):
            status = response.status_code
        elif isinstance(response, tuple):
            status = response[1] if len(response) > 1 else 200
        else:
            status = 200
        
        if 200 <= status < 300:
            record_land_power_search(user_id)
        
        return response
    return wrapper


def limit_api_requests(f):
    """Decorator to enforce monthly API request limits.

    Usage:
        @app.route('/api/v1/facilities')
        @limit_api_requests
        def get_facilities():
            ...
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Round 25 (2026-05-23): bypass localhost AND known internal
        # probe UAs. The site-probe + security-audit detectors hit
        # localhost:8080 with custom UAs and were eating the anonymous
        # monthly quota. 14/15 probes failed as 429 in round 24.
        _raw_ip = request.remote_addr or ''
        if _raw_ip in ('127.0.0.1', '::1', 'localhost'):
            return f(*args, **kwargs)
        _ua = (request.headers.get('User-Agent') or '').lower()
        _INTERNAL_UA_MARKERS = (
            'dc-brain-site-probe', 'dc-security-audit',
            'dchubhealer', 'dchub-brain', 'brain-radar',
            'dchub-selfheal', 'dchub-scheduler', 'uptimerobot',
        )
        if any(m in _ua for m in _INTERNAL_UA_MARKERS):
            return f(*args, **kwargs)
        user_id = _get_current_user_id()
        if not user_id:
            # Anonymous API calls — check by IP
            user_id = f"anon_{request.remote_addr}"
        
        allowed, error, remaining = check_api_limit(user_id)
        if not allowed:
            resp = jsonify(error)
            resp.status_code = 429
            resp.headers['X-RateLimit-Limit'] = str(TIER_LIMITS[error.get('tier', 'free')]['api_requests_per_month'])
            resp.headers['X-RateLimit-Remaining'] = '0'
            resp.headers['X-RateLimit-Reset'] = _next_month_first()
            return resp
        
        # Execute the request
        response = f(*args, **kwargs)
        
        # Record usage
        record_api_request(user_id)
        
        # Add rate limit headers to response
        tier = _get_user_tier(user_id) if not user_id.startswith('anon_') else 'free'
        limit = TIER_LIMITS.get(tier, TIER_LIMITS['free'])['api_requests_per_month']
        
        if hasattr(response, 'headers'):
            response.headers['X-RateLimit-Limit'] = str(limit)
            response.headers['X-RateLimit-Remaining'] = str(remaining - 1)
            response.headers['X-RateLimit-Reset'] = _next_month_first()
            response.headers['X-RateLimit-Tier'] = tier
        
        return response
    return wrapper


# =============================================================================
# HELPERS
# =============================================================================

def _get_current_user_id():
    """Extract user ID from the current request context.
    
    Checks (in order):
    1. Flask g.user_id (set by existing auth middleware)
    2. JWT Bearer token in Authorization header
    3. X-API-Key header
    4. api_key query parameter
    """
    # Check Flask g context (set by existing auth middleware)
    if hasattr(g, 'user_id') and g.user_id:
        return str(g.user_id)
    
    if hasattr(g, 'user') and g.user:
        user = g.user
        if isinstance(user, dict):
            return str(user.get('id') or user.get('user_id') or user.get('sub', ''))
        return str(getattr(user, 'id', ''))
    
    # Check JWT token
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            from flask import current_app
            import jwt as pyjwt
            token = auth_header.split(' ')[1]
            secret = os.environ.get('JWT_SECRET', current_app.config.get('JWT_SECRET', ''))
            payload = pyjwt.decode(token, secret, algorithms=['HS256'])
            return str(payload.get('sub') or payload.get('user_id') or payload.get('id', ''))
        except Exception:
            pass
    
    # Check API key
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if api_key:
        conn = _get_db()
        try:
            c = conn.cursor()
            row = c.execute(
                "SELECT user_id FROM api_keys WHERE key_value=%s AND is_active=1",
                (api_key,)
            ).fetchone()
            if row:
                return str(row['user_id'])
        except Exception:
            pass
        finally:
            conn.close()
    
    return None


def _count_filters(req):
    """Count the number of filters in a Land & Power search request.
    
    Counts non-empty query parameters that represent filters.
    Excludes meta params like 'page', 'limit', 'format', 'api_key'.
    """
    meta_params = {'page', 'limit', 'offset', 'format', 'api_key', 'callback', 'token'}
    
    if req.method == 'POST' and req.is_json:
        data = req.get_json(silent=True) or {}
        filters = data.get('filters', data.get('layers', {}))
        if isinstance(filters, dict):
            return sum(1 for v in filters.values() if v not in (None, '', False, []))
        elif isinstance(filters, list):
            return len(filters)
        # Count top-level non-meta keys
        return sum(1 for k, v in data.items() if k not in meta_params and v not in (None, '', False, []))
    
    # GET request — count query params
    count = 0
    for key, value in req.args.items():
        if key not in meta_params and value:
            count += 1
    return count


def _next_month_first():
    """Get the first day of next month as ISO string."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        return f"{now.year + 1}-01-01T00:00:00Z"
    else:
        return f"{now.year}-{now.month + 1:02d}-01T00:00:00Z"


# =============================================================================
# FLASK INIT & ROUTES
# =============================================================================

def init_free_tier_limiter(app):
    """Initialize the free tier limiter.
    
    Call this from main.py:
        from free_tier_limiter import init_free_tier_limiter
        init_free_tier_limiter(app)
    """
    _init_tables()
    
    # Register usage stats endpoint
    @app.route('/api/v1/usage/stats', methods=['GET'])
    def usage_stats():
        """Get current user's usage stats."""
        user_id = _get_current_user_id()
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
        
        stats = get_usage_stats(user_id)
        stats['success'] = True
        return jsonify(stats)
    
    @app.route('/api/v1/usage/check-land-power', methods=['GET'])
    def check_land_power_availability():
        """Check if user can perform a Land & Power search (for frontend gating)."""
        user_id = _get_current_user_id()
        if not user_id:
            return jsonify({
                'can_search': False,
                'reason': 'not_authenticated',
                'message': 'Please sign in to use Land & Power search.'
            })
        
        tier = _get_user_tier(user_id)
        limits = TIER_LIMITS.get(tier, TIER_LIMITS['free'])
        used = _get_usage(user_id, 'land_power_search')
        max_searches = limits['land_power_searches_per_month']
        
        can_search = used < max_searches
        
        return jsonify({
            'can_search': can_search,
            'tier': tier,
            'searches_used': used,
            'searches_limit': max_searches,
            'searches_remaining': max(0, max_searches - used),
            'max_filters': limits['land_power_max_filters'],
            'resets_on': _next_month_first(),
            'upgrade_url': 'https://dchub.cloud/pricing' if not can_search else None
        })
    
    # Admin endpoint for checking any user's usage
    @app.route('/api/admin/usage/<user_id>', methods=['GET'])
    def admin_usage_stats(user_id):
        """Admin: Get any user's usage stats."""
        # Verify admin
        current_user = _get_current_user_id()
        if not current_user:
            return jsonify({'error': 'Auth required'}), 401
        
        # Simple admin check — adjust to match your admin logic
        conn = _get_db()
        try:
            c = conn.cursor()
            row = c.execute(
                "SELECT is_admin FROM users WHERE id=%s OR google_id=%s",
                (current_user, current_user)
            ).fetchone()
            if not row or not row['is_admin']:
                return jsonify({'error': 'Admin access required'}), 403
        finally:
            conn.close()
        
        stats = get_usage_stats(user_id)
        stats['success'] = True
        return jsonify(stats)
    
    # Admin: Reset a user's usage (e.g., for support)
    @app.route('/api/admin/usage/<user_id>/reset', methods=['POST'])
    def admin_reset_usage(user_id):
        """Admin: Reset a user's monthly usage."""
        current_user = _get_current_user_id()
        if not current_user:
            return jsonify({'error': 'Auth required'}), 401
        
        conn = _get_db()
        try:
            c = conn.cursor()
            row = c.execute(
                "SELECT is_admin FROM users WHERE id=%s OR google_id=%s",
                (current_user, current_user)
            ).fetchone()
            if not row or not row['is_admin']:
                return jsonify({'error': 'Admin access required'}), 403
            
            month = _current_month()
            c = conn.cursor()
            c.execute(
                "DELETE FROM free_tier_usage WHERE user_id=%s AND usage_month=%s",
                (str(user_id), month)
            )
            conn.commit()
            return jsonify({'success': True, 'message': f'Usage reset for user {user_id} for {month}'})
        finally:
            conn.close()
    
    logger.info("✅ Free Tier Limiter initialized")
    logger.info(f"   Free: {TIER_LIMITS['free']['land_power_searches_per_month']} L&P search/mo, "
                f"{TIER_LIMITS['free']['land_power_max_filters']} filters, "
                f"{TIER_LIMITS['free']['api_requests_per_month']} API calls/mo")
    logger.info(f"   Pro:  Unlimited L&P, {TIER_LIMITS['pro']['api_requests_per_month']:,} API calls/mo")
    logger.info(f"   Enterprise: Unlimited L&P, {TIER_LIMITS['enterprise']['api_requests_per_month']:,} API calls/mo")
    
    return {
        'check_land_power_limit': check_land_power_limit,
        'check_api_limit': check_api_limit,
        'record_land_power_search': record_land_power_search,
        'record_api_request': record_api_request,
        'get_usage_stats': get_usage_stats,
    }
