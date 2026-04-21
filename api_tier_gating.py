from internal_auth import is_valid_internal_key
"""
DC Hub API Tier Gating System
==============================
Drop-in module for main.py that adds:
  1. Plan-aware decorators: @require_plan('pro'), @require_plan('enterprise')
  2. API key authentication with tier enforcement
  3. Tiered rate limiting (Free: 100/day, Pro: 10K/day, Enterprise: 100K/day)
  4. Graceful upgrade prompts on 403 responses
  5. Updated Stripe prices/webhooks for Enterprise tier

INSTALLATION:
  1. Copy this file to your Replit project root
  2. In main.py, add: from api_tier_gating import init_tier_gating
  3. After app creation: init_tier_gating(app)
  4. Replace @rate_limit on Pro endpoints with @require_plan('pro')
  5. Replace @rate_limit on Enterprise endpoints with @require_plan('enterprise')
  6. Create Stripe products for Enterprise tier (see STRIPE SETUP below)

STRIPE SETUP:
  In Stripe Dashboard:
  1. Create Product: "DC Hub Enterprise" 
  2. Add Price: $699/month (recurring) → copy price_id → set STRIPE_PRICE_ENTERPRISE_MONTHLY
  3. Add Price: $5,990/year (recurring) → copy price_id → set STRIPE_PRICE_ENTERPRISE_ANNUAL
  4. Create Product: "DC Hub Pro" (if not already created)
  5. Add Price: $199/month → set STRIPE_PRICE_PRO_MONTHLY  
  6. Add Price: $1,590/year → set STRIPE_PRICE_PRO_ANNUAL
  7. Update Payment Links in PAYMENT_LINKS below
"""

import os
import time
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, jsonify, g
from collections import defaultdict
import threading
from db_utils import get_db

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

DB_PATH = "dc_nexus.db"
# -- Public URLs (override via DCHUB_SIGNUP_URL / DCHUB_PRICING_URL) --
SIGNUP_URL  = os.environ.get('DCHUB_SIGNUP_URL',  'https://dchub.cloud/signup')
PRICING_URL = os.environ.get('DCHUB_PRICING_URL', 'https://dchub.cloud/pricing')


# Stripe Price IDs — set these as Replit Secrets after creating products
STRIPE_PRICES_V2 = {
    'free':                 None,
    'pro_monthly':          os.environ.get('STRIPE_PRICE_PRO_MONTHLY', 'price_XXXXX'),
    'pro_annual':           os.environ.get('STRIPE_PRICE_PRO_ANNUAL', 'price_XXXXX'),
    'enterprise_monthly':   os.environ.get('STRIPE_PRICE_ENTERPRISE_MONTHLY', 'price_XXXXX'),
    'enterprise_annual':    os.environ.get('STRIPE_PRICE_ENTERPRISE_ANNUAL', 'price_XXXXX'),
    'founding':             os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),
    'developer_monthly':    os.environ.get('STRIPE_PRICE_DEV_MONTHLY', 'price_XXXXX'),
}

# Stripe Payment Links — fallback if price IDs not configured
PAYMENT_LINKS = {
    'pro_monthly':        'https://buy.stripe.com/dRm7sMbRgcfPg97buiaZi02',
    'pro_annual':         'https://buy.stripe.com/4gM3cwcVk3JjbSR9maaZi01',
    'enterprise_monthly': '',  # TODO: Create in Stripe Dashboard
    'enterprise_annual':  '',  # TODO: Create in Stripe Dashboard
    'founding':           'https://buy.stripe.com/9B6fZi1cCdjT3ml8i6aZi00',
    'developer_monthly':  'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
}

# Plan hierarchy (higher = more access)
PLAN_LEVELS = {
    'free': 0,
    'founding': 1,  # Founding members get Pro access
    'developer': 2,  # Developer tier - $49/mo, 1000 calls/day
    'pro': 3,
    'enterprise': 4,
    'admin': 99,
}

# Rate limits per tier (API requests per day)
TIER_RATE_LIMITS = {
    'free':       10,
    'founding':   1000,
    'developer':  1000,
    'pro':        5000,
    'enterprise': 100000,
    'admin':      999999,
}

# ── NEW: Per-day unique record caps (prevents dataset vacuuming) ──
# This is the HARD ceiling on how many data records (facilities, deals,
# pipeline entries, etc.) a user can retrieve in a single calendar day
# across ALL endpoints (REST + MCP). No amount of pagination or delay
# will bypass this.
TIER_DAILY_RECORD_CAPS = {
    'anon':       50,
    'free':       50,
    'founding':   500,
    'developer':  500,
    'pro':        5000,
    'enterprise': 999999,
    'admin':      999999,
}

# ── NEW: Max pages per paginated query ──
TIER_PAGE_CAPS = {
    'anon':       1,
    'free':       2,
    'founding':   10,
    'developer':  10,
    'pro':        50,
    'enterprise': 999,
    'admin':      999,
}

# ── NEW: Max results per single search/list query ──
TIER_SEARCH_LIMITS = {
    'anon':       25,
    'free':       50,
    'founding':   100,
    'developer':  100,
    'pro':        500,
    'enterprise': 1000,
    'admin':      9999,
}

# ── NEW: MCP per-tool result limits (facility/search arrays) ──
MCP_TIER_RESULT_LIMITS = {
    'free':       5,
    'founding':   25,
    'developer':  25,
    'pro':        100,
    'enterprise': 999,
    'admin':      999,
}

# Plan display info
PLAN_INFO = {
    'free': {
        'name': 'Free',
        'price_monthly': 0,
        'price_annual': 0,
        'rate_limit': 10,
        'tagline': 'Headline stats, news, and AI discovery (10 calls/day)',
        'show_in_gate': True,
        'features': {
            'headline_stats': True,
            'news_feed': True,
            'ai_discovery': True,
            'market_list': True,
            'facility_search': False,
            'deal_database': False,
            'pipeline_tracker': False,
            'energy_data': False,
            'connectivity_score': False,
            'site_analysis': False,
            'market_compare': False,
            'pdf_reports': False,
            'ai_brain': False,
            'grid_monitoring': False,
            'land_power': False,
            'api_key': False,
            'priority_support': False,
        }
    },
    'developer': {
        'name': 'Developer',
        'price_monthly': 49,
        'price_annual': 390,
        'rate_limit': 1000,
        'tagline': 'Full facility DB + M&A + pipeline + energy (1,000 calls/day)',
        'show_in_gate': True,
        'features': {
            'headline_stats': True,
            'news_feed': True,
            'ai_discovery': True,
            'market_list': True,
            'facility_search': True,
            'deal_database': True,
            'pipeline_tracker': True,
            'energy_data': True,
            'connectivity_score': True,
            'site_analysis': False,
            'market_compare': False,
            'pdf_reports': False,
            'ai_brain': False,
            'grid_monitoring': False,
            'land_power': False,
            'api_key': True,
            'priority_support': False,
        }
    },
    'pro': {
        'name': 'Pro',
        'price_monthly': 199,
        'price_annual': 1590,
        'rate_limit': 10000,
        'tagline': 'Developer + market compare + PDF reports (10,000 calls/day)',
        'show_in_gate': True,
        'features': {
            'headline_stats': True,
            'news_feed': True,
            'ai_discovery': True,
            'market_list': True,
            'facility_search': True,
            'deal_database': True,
            'pipeline_tracker': True,
            'energy_data': True,
            'connectivity_score': True,
            'site_analysis': False,
            'market_compare': True,
            'pdf_reports': True,
            'ai_brain': False,
            'grid_monitoring': False,
            'land_power': False,
            'api_key': True,
            'priority_support': True,
        }
    },
    'enterprise': {
        'name': 'Enterprise',
        'price_monthly': 699,
        'price_annual': 5990,
        'rate_limit': 100000,
        'tagline': 'Pro + AI Brain + grid monitoring + land/power (100,000 calls/day)',
        'show_in_gate': True,
        'features': {
            'headline_stats': True,
            'news_feed': True,
            'ai_discovery': True,
            'market_list': True,
            'facility_search': True,
            'deal_database': True,
            'pipeline_tracker': True,
            'energy_data': True,
            'connectivity_score': True,
            'site_analysis': True,
            'market_compare': True,
            'pdf_reports': True,
            'ai_brain': True,
            'grid_monitoring': True,
            'land_power': True,
            'api_key': True,
            'priority_support': True,
        }
    },
}


# ═══════════════════════════════════════════════════════════════

def _build_gate_plans() -> dict:
    """Build anon plan summary from PLAN_INFO. Single source of truth."""
    out = {}
    for key, info in PLAN_INFO.items():
        if not info.get('show_in_gate', True): continue
        if info.get('price_monthly', 0) == 0:
            out[key] = f"Sign up at {SIGNUP_URL} for {info.get('rate_limit',0)} free API calls/day"
        else:
            tag = info.get('tagline', info.get('name', key))
            out[key] = f"${info['price_monthly']}/mo — {tag}"
    return out

#  API KEY TABLE
# ═══════════════════════════════════════════════════════════════

def init_api_keys_table():
    """Create/migrate the api_keys table with tier support."""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            user_id TEXT,
            email TEXT,
            plan TEXT DEFAULT 'free',
            name TEXT DEFAULT 'Default',
            calls_today INTEGER DEFAULT 0,
            calls_total INTEGER DEFAULT 0,
            last_used TEXT,
            last_reset_date TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            expires_at TEXT
        )
    """)

    conn.commit()
    conn.close()

    for col_sql in [
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_reset_date TEXT",
    ]:
        try:
            mc = get_db()
            mc.execute(col_sql)
            mc.commit()
            mc.close()
        except:
            try: mc.close()
            except: pass

    print("  ✅ api_keys table ready (with tier support)")


def generate_api_key(user_id, email, plan='free', name='Default'):
    """Generate a new API key for a user, return the raw key (only shown once)."""
    raw_key = f"dchub_{plan[:2]}_{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:16]

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO api_keys (key_hash, key_prefix, user_id, email, plan, name, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (key_hash, key_prefix, user_id, email, plan, name, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

    return raw_key



def validate_api_key(api_key):
    """Validate API key and return user info. BUG-005 FIX: Direct psycopg2 connection.
    
    UPDATED: Added NEON_DATABASE_URL as primary fallback — Railway DATABASE_URL
    sometimes points to wrong Neon DB (helium vs ep-old-waterfall).
    """
    if not api_key:
        return None
    
    import psycopg2
    conn = None
    try:
        database_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL') or os.environ.get('DATABASE_READ_URL')
        if not database_url:
            print("[BUG-005] No DATABASE_URL or NEON_DATABASE_URL found")
            return None
        
        conn = psycopg2.connect(database_url, connect_timeout=5)
        cur = conn.cursor()
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        cur.execute("""
            SELECT u.id, u.email, u.plan, u.role, ak.rate_limit_tier
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key_hash = %s AND ak.is_active = 1
            LIMIT 1
        """, (key_hash,))
        row = cur.fetchone()
        cur.close()
        
        if row:
            return {
                'user_id': row[0],
                'email': row[1],
                'plan': row[2],
                'role': row[3],
                'rate_limit_tier': row[4] or row[2]  # fallback to plan
            }
        return None
    except Exception as e:
        print(f"[BUG-005] validate_api_key error: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def get_user_plan(user_id=None, email=None):
    """Get a user's current plan from Neon — direct psycopg2 (bypasses db_utils)."""
    if not user_id and not email:
        return 'free'

    import psycopg2
    db_url = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL', '')
    if not db_url:
        return 'free'

    conn = None
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        c = conn.cursor()
        row = None

        if user_id:
            c.execute("SELECT plan, subscription_status, role FROM users WHERE id = %s", (str(user_id),))
            row = c.fetchone()
        if not row and email:
            c.execute("SELECT plan, subscription_status, role FROM users WHERE email = %s", (email,))
            row = c.fetchone()
        if not row and user_id and isinstance(user_id, str) and '@' in str(user_id):
            c.execute("SELECT plan, subscription_status, role FROM users WHERE email = %s", (user_id,))
            row = c.fetchone()

        if not row:
            return 'free'

        plan_val = row[0] or 'free'
        status_val = row[1] or ''
        role_val = row[2] or ''

        if role_val == 'admin':
            return 'admin'
        if status_val in ('canceled', 'unpaid'):
            return 'free'
        return plan_val

    except Exception as e:
        import logging
        logging.warning(f"get_user_plan error: {e}")
        return 'free'
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def user_has_access(user_plan, required_plan):
    """Check if a user's plan meets the required access level."""
    user_level = PLAN_LEVELS.get(user_plan, 0)
    required_level = PLAN_LEVELS.get(required_plan, 0)
    return user_level >= required_level


# ═══════════════════════════════════════════════════════════════
#  DECORATORS
# ═══════════════════════════════════════════════════════════════

def require_plan(min_plan='pro'):
    """
    Decorator that enforces a minimum plan level.
    Checks web session cookies, JWT Bearer tokens, AND API keys.
    
    Usage:
        @app.route('/api/v1/deals')
        @require_plan('pro')
        def get_deals():
            ...
    
    Authentication flow:
        1. Check for web session cookies (dchub.cloud logged-in users)
        2. Check for JWT Bearer token in Authorization header
        3. Check for API key (X-API-Key header or %sapi_key= param)
        4. If neither, return 401 with upgrade prompt
        5. If found but wrong tier, return 403 with upgrade prompt
        6. On ANY error, fail closed (503)
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user_plan = 'free'
            auth_method = None

            try:
                # ── STEP 0: Check for internal bypass ──────────────────
                internal_key = request.headers.get('X-Internal-Key', '')
                if internal_key == 'dchub-internal-sync-2026':
                    return f(*args, **kwargs)
                
                # ── STEP 1: Check web session cookies ──────────────────
                # dchub.cloud frontend may store JWT in a cookie after
                # Google OAuth login. Check common cookie names.
                session_token = (
                    request.cookies.get('session_token') or
                    request.cookies.get('dchub_session') or
                    request.cookies.get('dchub_token') or
                    request.cookies.get('token')
                )
                if session_token:
                    decode_jwt = _get_decode_jwt()
                    if decode_jwt:
                        try:
                            payload = decode_jwt(session_token)
                            if payload:
                                uid = (payload.get('user_id') or
                                       payload.get('sub') or
                                       payload.get('email'))
                                user_plan = get_user_plan(
                                    user_id=uid,
                                    email=payload.get('email')
                                )
                                auth_method = 'web_session'
                                request.user = payload
                                request.user_plan = user_plan
                        except Exception:
                            pass  # Fall through to next auth method

                # ── STEP 2: Check JWT Bearer token ─────────────────────
                if not auth_method:
                    auth_header = request.headers.get('Authorization')
                    if auth_header and auth_header.startswith('Bearer '):
                        decode_jwt = _get_decode_jwt()
                        if decode_jwt:
                            token = auth_header.split(' ', 1)[1]
                            payload = decode_jwt(token)
                            if payload:
                                uid = (payload.get('user_id') or
                                       payload.get('sub') or
                                       payload.get('email'))
                                user_plan = get_user_plan(
                                    user_id=uid,
                                    email=payload.get('email')
                                )
                                auth_method = 'jwt'
                                request.user = payload
                                request.user_plan = user_plan

                # ── STEP 3: Check API Key ──────────────────────────────
                if not auth_method:
                    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
                    # Also accept Authorization: Bearer dchub_... (JWT decode in STEP 2
                    # failed because dchub_ keys are not JWTs — treat as API key here).
                    if not api_key:
                        _auth_h = request.headers.get('Authorization', '')
                        if _auth_h.startswith('Bearer ') and _auth_h[7:].startswith('dchub_'):
                            api_key = _auth_h[7:].strip()
                    if api_key:
                        info = validate_api_key(api_key)  # Returns dict or None (NOT tuple)
                        if not info or not isinstance(info, dict):
                            # Check AI Wars verification keys (inline to avoid circular import)
                            AI_WARS_KEYS_TIER = {
                                "dchub_chatgpt_2026_verify", "dchub_grok_2026_verify",
                                "dchub_gemini_2026_verify", "dchub_perplexity_2026_verify",
                                "dchub_mistral_2026_verify", "dchub_claude_2026_verify",
                                "dchub_copilot_2026_verify", "dchub_meta_2026_verify",
                                "dchub_poe_2026_verify", "dchub_openrouter_2026_verify",
                                "dchub_pi_2026_verify", "dchub_phind_2026_verify",
                                "dchub_nvidia_2026_verify"
                            }
                            _ai_key = request.headers.get('X-API-Key') or request.args.get('api_key') or (request.headers.get('Authorization', '')[7:].strip() if request.headers.get('Authorization', '').startswith('Bearer ') else '')
                            if _ai_key in AI_WARS_KEYS_TIER:
                                auth_method = 'api_key'
                                request.api_key_info = {'plan': 'pro', 'key': _ai_key}
                                request.user_plan = 'pro'

                            if not auth_method:
                                return jsonify({
                                    'success': False,
                                    'error': 'invalid_api_key',
                                    'message': 'Invalid or inactive API key',
                                    'get_key_url': 'https://dchub.cloud/dashboard.html#api-keys',
                                }), 401, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

                        user_plan = info.get('plan', 'free')
                        auth_method = 'api_key'
                        request.api_key_info = info
                        request.user_plan = user_plan

                # ── STEP 4: No auth at all ─────────────────────────────
                if not auth_method:
                    return jsonify({
                        'success': False,
                        'error': 'plan_required',
                        'message': f'This endpoint requires a {min_plan.title()} plan or higher.',
                        'plans': _build_gate_plans(),
                        'signup_url': SIGNUP_URL,
                        'pricing_url': PRICING_URL,
                        'free_alternative': _get_free_alternative(request.path),
                    }), 403, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

                # ── STEP 5: Check tier level ───────────────────────────
                if not user_has_access(user_plan, min_plan):
                    return jsonify({
                        'success': False,
                        'error': 'plan_upgrade_required',
                        'message': f'This endpoint requires {min_plan.title()} plan. You are on {user_plan.title()}.',
                        'current_plan': user_plan,
                        'required_plan': min_plan,
                        'upgrade_url': 'https://dchub.cloud/pricing',
                        'free_alternative': _get_free_alternative(request.path),
                    }), 403, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

                # ── STEP 6: Authorized — add headers and proceed ───────
                plan_limit = TIER_RATE_LIMITS.get(user_plan, 100)
                g.user_tier = user_plan
                g.tier_headers = {
                    'X-RateLimit-Limit': str(plan_limit),
                    'X-RateLimit-Tier': user_plan,
                    'X-Auth-Method': auth_method,
                    'X-Powered-By': 'DC Hub Nexus',
                }

                return f(*args, **kwargs)

            except Exception as e:
                # FAIL CLOSED — never leak data on errors
                import logging
                logging.getLogger(__name__).error(
                    f"Tier gating error on {request.path}: {e}"
                )
                return jsonify({
                    'error': 'Authentication service unavailable',
                    'success': False,
                }), 503

        return decorated
    return decorator


def _get_free_alternative(path):
    """Suggest a free endpoint the user can use instead."""
    alternatives = {
        '/api/v1/facilities': '/api/v1/stats (aggregate stats, no auth required)',
        '/api/v1/map': '/api/v1/stats (facility counts by region)',
        '/api/v1/search': '/api/v1/stats (summary statistics)',
        '/api/v1/deals': '/api/ai/query%stype=stats (aggregate deal count)',
        '/api/deals': '/api/ai/query%stype=stats (aggregate deal count)',
        '/api/v1/transactions': '/api/ai/query%stype=stats (aggregate deal count)',
        '/api/v1/pipeline': '/api/ai/query%stype=stats (capacity pipeline total)',
        '/api/v1/markets/': '/api/v1/markets/list (market list, free)',
        '/api/v1/connectivity': '/api/v1/stats (basic connectivity info)',
        '/api/v1/energy': '/api/grid/supported-isos (ISO list, free)',
        '/api/brain/': '/api/ai/query%stype=stats (basic stats, free)',
        '/api/reports/generate': '/api/market-report (JSON summary, free)',
    }
    for prefix, alt in alternatives.items():
        if path.startswith(prefix):
            return alt
    return '/api/v1/stats (free, no auth required)'


FACILITY_TIER_LIMITS = {
    'anon': 50, 'free': 200, 'founding': 500, 'developer': 500,
    'pro': 2000, 'enterprise': 9999, 'admin': 9999,
}
FACILITY_VISIBLE_FIELDS = {
    'anon': {'name', 'city', 'country', 'latitude', 'longitude', 'status', 'slug'},
    'free': {'name', 'city', 'country', 'latitude', 'longitude', 'status', 'slug',
             'provider', 'operator', 'region', 'market'},
}

def get_request_tier():
    """Non-blocking tier detection from JWT/API key/cookie. Returns plan or 'anon'."""
    try:
        from flask import request
        internal_key = request.headers.get('X-Internal-Key', '')
        if internal_key == 'dchub-internal-sync-2026':
            return 'admin'
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            decode_jwt = _get_decode_jwt()
            if decode_jwt:
                payload = decode_jwt(auth_header.split(' ', 1)[1])
                if payload:
                    uid = payload.get('user_id') or payload.get('sub') or payload.get('email')
                    return get_user_plan(user_id=uid, email=payload.get('email')) or 'free'
        api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
        # Also accept Authorization: Bearer dchub_... (JWT decode already failed above)
        if not api_key:
            _auth_h2 = request.headers.get('Authorization', '')
            if _auth_h2.startswith('Bearer ') and _auth_h2[7:].startswith('dchub_'):
                api_key = _auth_h2[7:].strip()
        if api_key:
            info = validate_api_key(api_key)  # Returns dict or None (NOT tuple)
            if info and isinstance(info, dict):
                role = info.get('role', '')
                if role == 'admin':
                    return 'admin'
                return info.get('plan', 'free')
        session_token = (request.cookies.get('session_token') or request.cookies.get('dchub_session') or
                         request.cookies.get('dchub_token') or request.cookies.get('token'))
        if session_token:
            decode_jwt = _get_decode_jwt()
            if decode_jwt:
                payload = decode_jwt(session_token)
                if payload:
                    uid = payload.get('user_id') or payload.get('sub') or payload.get('email')
                    return get_user_plan(user_id=uid, email=payload.get('email')) or 'free'
    except Exception:
        pass
    return 'anon'

def gate_facilities_response(facilities, plan, total_in_db=None):
    """Shape facility response based on plan: row limits + field stripping + upgrade CTA."""
    plan = (plan or 'anon').lower()
    limit = FACILITY_TIER_LIMITS.get(plan, 50)
    total = total_in_db or len(facilities)
    gated = facilities[:limit]
    visible = FACILITY_VISIBLE_FIELDS.get(plan)
    if visible:
        gated = [{k: v for k, v in f.items() if k in visible} if isinstance(f, dict) else f for f in gated]
    result = {'success': True, 'data': gated, 'count': len(gated), 'total_available': total, 'plan': plan}
    if len(facilities) > limit or len(facilities) < total:
        if plan == 'anon':
            result['upgrade'] = {'message': f'Showing {len(gated)} of {total:,}. Sign up free for more.',
                                 'url': 'https://dchub.cloud/login.html'}
        elif plan == 'free':
            result['upgrade'] = {'message': f'Showing {len(gated)} of {total:,}. Upgrade to Pro for full access.',
                                 'url': 'https://dchub.cloud/pricing'}
    return result


# ═══════════════════════════════════════════════════════════════
#  DAILY RECORD USAGE TRACKING (Neon-backed)
# ═══════════════════════════════════════════════════════════════
#
# Tracks how many data records each user has been served per day.
# Enforces TIER_DAILY_RECORD_CAPS across REST API + MCP.
# user_key = user_id (JWT) | API key prefix | IP address (anon)
# ═══════════════════════════════════════════════════════════════

import logging as _tg_logging
_tg_logger = _tg_logging.getLogger('tier_gating')

# In-memory cache to reduce DB hits. Reset on deploy.
# Structure: {user_key: {'date': 'YYYY-MM-DD', 'records': N, 'hits': N}}
_daily_record_cache = {}


def _get_pg_conn_for_tracking():
    """Get a Neon connection for record tracking. Returns (conn, return_fn)."""
    try:
        from main import get_pg_connection, return_pg_connection
        conn = get_pg_connection()
        return conn, return_pg_connection
    except Exception:
        pass
    try:
        from db_utils import get_db
        conn = get_db()
        return conn, lambda c, **kw: c.close()
    except Exception:
        return None, None


def check_daily_record_budget(user_key, tier, records_requested=0):
    """
    Check if a user has remaining daily record budget.
    
    Args:
        user_key: user_id, API key prefix, or IP address
        tier: user's plan tier
        records_requested: how many records this request will serve
    
    Returns:
        (allowed: bool, records_remaining: int, records_used: int, cap: int)
    """
    from datetime import date
    today = date.today().isoformat()
    cap = TIER_DAILY_RECORD_CAPS.get(tier, 50)
    
    # Check in-memory cache first
    cached = _daily_record_cache.get(user_key)
    if cached and cached['date'] == today:
        used = cached['records']
        remaining = max(0, cap - used)
        return (remaining >= records_requested), remaining, used, cap
    
    # Fetch from Neon
    conn, return_fn = _get_pg_conn_for_tracking()
    if not conn:
        # DB unavailable — allow through but log
        _tg_logger.warning(f"Record tracking DB unavailable for {user_key}")
        return True, cap, 0, cap
    
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT records_served FROM daily_record_usage WHERE user_key = %s AND usage_date = %s",
            (user_key, today)
        )
        row = cur.fetchone()
        used = row[0] if row else 0
        cur.close()
        
        # Update cache
        _daily_record_cache[user_key] = {'date': today, 'records': used, 'hits': 0}
        
        # Cleanup stale cache entries periodically
        if len(_daily_record_cache) > 5000:
            stale = [k for k, v in _daily_record_cache.items() if v['date'] != today]
            for k in stale:
                _daily_record_cache.pop(k, None)
        
        remaining = max(0, cap - used)
        return (remaining >= records_requested), remaining, used, cap
    except Exception as e:
        _tg_logger.error(f"Record budget check error: {e}")
        return True, cap, 0, cap
    finally:
        if conn and return_fn:
            try:
                return_fn(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def increment_daily_records(user_key, tier, records_count, endpoint=''):
    """
    Increment the daily record counter for a user.
    Called AFTER serving records to the user.
    
    Args:
        user_key: user_id, API key prefix, or IP address
        tier: user's plan tier
        records_count: number of records just served
        endpoint: which endpoint served them (for analytics)
    """
    from datetime import date
    today = date.today().isoformat()
    
    # Update in-memory cache immediately
    cached = _daily_record_cache.get(user_key)
    if cached and cached['date'] == today:
        cached['records'] += records_count
        cached['hits'] += 1
    else:
        _daily_record_cache[user_key] = {'date': today, 'records': records_count, 'hits': 1}
    
    # Persist to Neon (fire-and-forget pattern)
    conn, return_fn = _get_pg_conn_for_tracking()
    if not conn:
        return
    
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO daily_record_usage (user_key, usage_date, records_served, endpoint_hits, tier, last_endpoint, last_access)
            VALUES (%s, %s, %s, 1, %s, %s, NOW())
            ON CONFLICT (user_key, usage_date) DO UPDATE SET
                records_served = daily_record_usage.records_served + %s,
                endpoint_hits = daily_record_usage.endpoint_hits + 1,
                tier = %s,
                last_endpoint = %s,
                last_access = NOW()
        """, (user_key, today, records_count, tier, endpoint, records_count, tier, endpoint))
        conn.commit()
        cur.close()
    except Exception as e:
        _tg_logger.error(f"Record increment error: {e}")
    finally:
        if conn and return_fn:
            try:
                return_fn(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def get_user_key_from_request():
    """
    Extract a stable user identifier from the current request.
    Priority: user_id (JWT) > API key prefix > IP address.
    Returns: (user_key, tier)
    """
    from flask import request
    
    tier = get_request_tier()
    
    # Try JWT user_id
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer ') and not auth_header[7:].startswith('dchub_'):
        decode_jwt = _get_decode_jwt()
        if decode_jwt:
            try:
                payload = decode_jwt(auth_header.split(' ', 1)[1])
                if payload and payload.get('user_id'):
                    return f"uid:{payload['user_id']}", tier
            except Exception:
                pass
    
    # Try API key prefix
    api_key = request.headers.get('X-API-Key', '') or request.args.get('api_key', '')
    if not api_key:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer ') and auth[7:].startswith('dchub_'):
            api_key = auth[7:]
    if api_key and api_key.startswith('dchub_'):
        return f"key:{api_key[:16]}", tier
    
    # Try session cookies
    for cookie_name in ('session_token', 'dchub_session', 'dchub_token', 'token'):
        session_token = request.cookies.get(cookie_name)
        if session_token:
            decode_jwt = _get_decode_jwt()
            if decode_jwt:
                try:
                    payload = decode_jwt(session_token)
                    if payload and payload.get('user_id'):
                        return f"uid:{payload['user_id']}", tier
                except Exception:
                    pass
    
    # Fallback: IP address
    ip = request.remote_addr or 'unknown'
    return f"ip:{ip}", tier


def enforce_page_cap(requested_page, tier):
    """
    Enforce max page cap for a tier. Returns the capped page number.
    If requested page exceeds cap, returns None (caller should return 403).
    """
    cap = TIER_PAGE_CAPS.get(tier, 1)
    if requested_page > cap:
        return None
    return requested_page


def enforce_search_limit(requested_limit, tier):
    """Cap the per-query result limit based on tier."""
    max_limit = TIER_SEARCH_LIMITS.get(tier, 25)
    return min(requested_limit, max_limit)


def build_record_cap_error(user_key, tier, records_used, cap):
    """Build a standardized 429 error response for record cap exceeded."""
    from flask import jsonify
    
    upgrade_msg = {
        'anon': 'Sign up free at dchub.cloud to get 50 records/day.',
        'free': 'Upgrade to Developer ($49/mo) for 500 records/day.',
        'founding': 'Upgrade to Developer ($49/mo) for 500 records/day.',
        'developer': 'Upgrade to Pro ($199/mo) for 5,000 records/day.',
        'pro': 'Contact us for Enterprise access with unlimited records.',
    }
    
    return jsonify({
        'success': False,
        'error': 'daily_record_limit_reached',
        'message': f'Daily record limit reached ({records_used:,}/{cap:,} records served today on {tier} plan).',
        'records_used': records_used,
        'daily_cap': cap,
        'resets': 'midnight UTC',
        'upgrade': {
            'message': upgrade_msg.get(tier, 'Contact sales for higher limits.'),
            'url': 'https://dchub.cloud/pricing',
            'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c' if tier in ('free', 'founding', 'anon') else '',
        }
    }), 429, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}


def build_page_cap_error(requested_page, tier, cap):
    """Build a standardized 403 error response for page cap exceeded."""
    from flask import jsonify
    
    return jsonify({
        'success': False,
        'error': 'page_limit_exceeded',
        'message': f'Page {requested_page} exceeds the {cap}-page limit on your {tier} plan. Use filters to narrow results.',
        'max_page': cap,
        'current_plan': tier,
        'tip': 'Use country, state, operator, or min_mw filters to get targeted results within your page limit.',
        'upgrade_url': 'https://dchub.cloud/pricing',
    }), 403, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

def add_tier_headers(response):
    """Add rate limit and tier headers to response."""
    headers = getattr(g, 'tier_headers', None)
    if headers:
        for k, v in headers.items():
            response.headers[k] = v
    return response


# ═══════════════════════════════════════════════════════════════
#  UPDATED STRIPE INTEGRATION
# ═══════════════════════════════════════════════════════════════

def register_stripe_v2_routes(app):
    """Register updated Stripe routes with Enterprise support."""

    @app.route('/api/v2/stripe/config', methods=['GET'])
    def stripe_config_v2():
        """Get Stripe config with all tier pricing."""
        return jsonify({
            'publishableKey': os.environ.get('STRIPE_PUBLISHABLE_KEY', ''),
            'configured': bool(os.environ.get('STRIPE_SECRET_KEY')),
            'plans': PLAN_INFO,
            'payment_links': {k: v for k, v in PAYMENT_LINKS.items() if v},
        })

    @app.route('/api/v2/stripe/create-checkout', methods=['POST'])
    def create_checkout_v2():
        """Create checkout session — supports Free/Pro/Enterprise."""
        try:
            import stripe
        except ImportError:
            return jsonify({'error': 'Stripe not available'}), 503

        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
        if not stripe.api_key:
            return jsonify({'error': 'Stripe not configured'}), 503

        data = request.get_json() or {}
        plan = data.get('plan', 'pro_monthly')

        # Map plan to Stripe price ID
        price_id = STRIPE_PRICES_V2.get(plan)

        if not price_id or price_id.startswith('price_XXXXX'):
            # Fall back to payment links
            link = PAYMENT_LINKS.get(plan)
            if link:
                return jsonify({'redirect': True, 'url': link})
            return jsonify({'error': f'Plan {plan} not configured in Stripe'}), 400, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

        # Get user email from JWT or request body
        email = data.get('email', '')
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            decode_jwt = _get_decode_jwt()
            if decode_jwt:
                token = auth_header.split(' ')[1]
                payload = decode_jwt(token)
                if payload:
                    email = email or payload.get('email', '')

        try:
            session = stripe.checkout.Session.create(
                customer_email=email or None,
                payment_method_types=['card'],
                line_items=[{'price': price_id, 'quantity': 1}],
                mode='subscription',
                success_url=f'https://dchub.cloud/dashboard.html%spayment=success&plan={plan}',
                cancel_url='https://dchub.cloud/pricing%spayment=cancelled',
                metadata={'plan': plan, 'email': email},
                allow_promotion_codes=True,
            )
            return jsonify({'sessionId': session.id, 'url': session.url})
        except Exception as e:
            print(f"Stripe checkout error: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/v2/stripe/webhook', methods=['POST'])
    def stripe_webhook_v2():
        """Handle Stripe webhooks with Enterprise tier mapping."""
        try:
            import stripe
        except ImportError:
            return jsonify({'error': 'Stripe not available'}), 503

        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
        payload = request.get_data(as_text=True)
        sig_header = request.headers.get('Stripe-Signature')
        webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

        if webhook_secret:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            except Exception as e:
                return jsonify({'error': str(e)}), 400, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}
        else:
            import json
            event = json.loads(payload)

        event_type = event.get('type', '')
        data = event.get('data', {}).get('object', {})

        print(f"💳 Stripe v2 webhook: {event_type}")

        if event_type == 'checkout.session.completed':
            _handle_checkout_v2(data)
        elif event_type == 'customer.subscription.updated':
            _handle_sub_updated_v2(data)
        elif event_type == 'customer.subscription.deleted':
            _handle_sub_deleted_v2(data)

        return jsonify({'received': True})

    print("  ✅ Stripe v2 routes registered (with Enterprise tier)")


def _map_stripe_plan_to_tier(plan_key):
    """Map Stripe plan metadata to database tier name."""
    mapping = {
        'pro_monthly': 'pro',
        'pro_annual': 'pro',
        'enterprise_monthly': 'enterprise',
        'enterprise_annual': 'enterprise',
        'founding': 'founding',
    }
    return mapping.get(plan_key, 'pro')


def _handle_checkout_v2(session):
    """Handle checkout completion — set correct tier."""
    email = session.get('customer_email', '').lower()
    metadata = session.get('metadata', {})
    plan_key = metadata.get('plan', 'pro_monthly')
    tier = _map_stripe_plan_to_tier(plan_key)
    customer_id = session.get('customer', '')

    conn = get_db()
    c = conn.cursor()

    if email:
        c.execute("""
            UPDATE users SET plan = %s, stripe_customer_id = %s, subscription_status = 'active'
            WHERE email = %s
        """, (tier, customer_id, email))

    conn.commit()
    conn.close()
    print(f"✅ User upgraded to {tier}: {email}")

    # Auto-generate API key for Pro/Enterprise users
    if tier in ('pro', 'enterprise', 'founding'):
        try:
            _auto_provision_api_key(email, tier)
        except Exception as e:
            print(f"⚠️ Auto-provision API key failed: {e}")


def _auto_provision_api_key(email, plan):
    """Auto-generate an API key when a user subscribes."""
    conn = get_db()
    
    c = conn.cursor()

    # Find user
    c.execute("SELECT id FROM users WHERE email = %s", (email,))
    user = c.fetchone()
    if not user:
        conn.close()
        return

    user_id = user['id']

    # Check if they already have a key
    c.execute("SELECT id FROM api_keys WHERE user_id = %s AND is_active = 1", (user_id,))
    existing = c.fetchone()

    if existing:
        # Upgrade existing key's plan
        c.execute("UPDATE api_keys SET plan = %s WHERE user_id = %s AND is_active = 1", (plan, user_id))
        conn.commit()
        conn.close()
        print(f"  ↑ Upgraded existing API key to {plan} for {email}")
    else:
        conn.close()
        key = generate_api_key(user_id, email, plan, f'{plan.title()} API Key')
        print(f"  🔑 New {plan} API key generated for {email}: {key[:16]}...")


def _handle_sub_updated_v2(subscription):
    """Handle subscription changes — map price to tier."""
    customer_id = subscription.get('customer', '')
    status = subscription.get('status', '')

    # Try to determine the plan from the price
    items = subscription.get('items', {}).get('data', [])
    plan = 'pro'  # default
    for item in items:
        price_id = item.get('price', {}).get('id', '')
        for plan_key, stripe_price in STRIPE_PRICES_V2.items():
            if price_id == stripe_price:
                plan = _map_stripe_plan_to_tier(plan_key)
                break

    conn = get_db()
    c = conn.cursor()

    if status in ('active', 'trialing'):
        c.execute("""
            UPDATE users SET plan = %s, subscription_status = %s WHERE stripe_customer_id = %s
        """, (plan, status, customer_id))
        # Also upgrade API keys
        c.execute("SELECT id FROM users WHERE stripe_customer_id = %s", (customer_id,))
        user = c.fetchone()
        if user:
            c.execute("UPDATE api_keys SET plan = %s WHERE user_id = %s AND is_active = 1",
                      (plan, user[0]))
    elif status in ('past_due', 'unpaid'):
        c.execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
    elif status == 'canceled':
        c.execute("""
            UPDATE users SET plan = 'free', subscription_status = 'canceled' 
            WHERE stripe_customer_id = %s
        """, (customer_id,))
        # Downgrade API keys
        c.execute("SELECT id FROM users WHERE stripe_customer_id = %s", (customer_id,))
        user = c.fetchone()
        if user:
            c.execute("UPDATE api_keys SET plan = 'free' WHERE user_id = %s AND is_active = 1",
                      (user[0],))

    conn.commit()
    conn.close()
    print(f"📝 Subscription updated: customer={customer_id}, status={status}, plan={plan}")


def _handle_sub_deleted_v2(subscription):
    """Handle subscription deletion — downgrade to free."""
    customer_id = subscription.get('customer', '')

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE users SET plan = 'free', subscription_status = 'canceled' 
        WHERE stripe_customer_id = %s
    """, (customer_id,))
    # Downgrade API keys
    c.execute("SELECT id FROM users WHERE stripe_customer_id = %s", (customer_id,))
    user = c.fetchone()
    if user:
        c.execute("UPDATE api_keys SET plan = 'free' WHERE user_id = %s AND is_active = 1",
                  (user[0],))
    conn.commit()
    conn.close()
    print(f"❌ Subscription canceled: customer={customer_id}")


# ═══════════════════════════════════════════════════════════════
#  API KEY MANAGEMENT ROUTES
# ═══════════════════════════════════════════════════════════════

def register_api_key_routes(app):
    """Register API key management endpoints."""

    def _auth_from_jwt():
        """Extract user payload from JWT Bearer token. Returns (payload, error_response)."""
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None, (jsonify({'error': 'Auth required'}), 401, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'})
        decode_jwt = _get_decode_jwt()
        if not decode_jwt:
            return None, (jsonify({'error': 'Auth system not available'}), 503)
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return None, (jsonify({'error': 'Invalid or expired token'}), 401, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'})
        return payload, None

    @app.route('/api/v2/keys', methods=['GET'])
    def list_api_keys():
        """List user's API keys (requires auth)."""
        payload, err = _auth_from_jwt()
        if err:
            return err

        user_id = payload.get('user_id')
        conn = get_db()
        
        c = conn.cursor()
        c.execute("""
            SELECT id, key_prefix, plan, name, calls_today, calls_total, last_used, is_active, created_at
            FROM api_keys WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,))
        cols = ['id', 'key_prefix', 'plan', 'name', 'calls_today', 'calls_total', 'last_used', 'is_active', 'created_at']
        keys = [dict(r.items()) if hasattr(r, 'items') else dict(zip(cols, r)) for r in c.fetchall()]
        conn.close()

        return jsonify({'success': True, 'keys': keys})

    @app.route('/api/v2/keys', methods=['POST'])
    def create_api_key():
        """Generate a new API key (requires auth, plan determines tier)."""
        payload, err = _auth_from_jwt()
        if err:
            return err

        user_id = payload.get('user_id')
        email = payload.get('email', '')
        plan = get_user_plan(user_id=user_id)

        data = request.get_json() or {}
        name = data.get('name', 'API Key')

        # Limit: 3 keys for free, 10 for pro, 25 for enterprise
        limits = {'free': 3, 'founding': 10, 'pro': 10, 'enterprise': 25}
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = %s AND is_active = 1", (user_id,))
        count = c.fetchone()[0]
        conn.close()

        if count >= limits.get(plan, 3):
            return jsonify({
                'error': f'Key limit reached ({limits.get(plan, 3)} keys on {plan} plan)',
                'upgrade_url': 'https://dchub.cloud/pricing',
            }), 403, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

        raw_key = generate_api_key(user_id, email, plan, name)

        return jsonify({
            'success': True,
            'key': raw_key,
            'plan': plan,
            'rate_limit': TIER_RATE_LIMITS.get(plan, 100),
            'warning': 'Save this key now — it will not be shown again.',
        })

    @app.route('/api/v2/keys/<int:key_id>', methods=['DELETE'])
    def revoke_api_key(key_id):
        """Revoke an API key."""
        payload, err = _auth_from_jwt()
        if err:
            return err

        user_id = payload.get('user_id')
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE api_keys SET is_active = 0 WHERE id = %s AND user_id = %s", (key_id, user_id))
        conn.commit()
        affected = c.rowcount
        conn.close()

        if affected:
            return jsonify({'success': True, 'message': 'API key revoked'})
        return jsonify({'error': 'Key not found'}), 404, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

    @app.route('/api/v2/usage', methods=['GET'])
    def get_api_usage():
        """Get API usage stats for current user."""
        payload, err = _auth_from_jwt()
        if err:
            return err

        user_id = payload.get('user_id')
        plan = get_user_plan(user_id=user_id)

        conn = get_db()
        
        c = conn.cursor()
        c.execute("""
            SELECT SUM(calls_today) as today, SUM(calls_total) as total
            FROM api_keys WHERE user_id = %s AND is_active = 1
        """, (user_id,))
        row = c.fetchone()
        if row and hasattr(row, 'items'):
            usage = dict(row.items())
        elif row and not isinstance(row, dict):
            usage = {'today': row[0], 'total': row[1]}
        elif row:
            usage = dict(row)
        else:
            usage = {'today': 0, 'total': 0}
        conn.close()

        limit = TIER_RATE_LIMITS.get(plan, 100)

        return jsonify({
            'success': True,
            'plan': plan,
            'calls_today': usage.get('today') or 0,
            'calls_total': usage.get('total') or 0,
            'daily_limit': limit,
            'remaining_today': max(0, limit - (usage.get('today') or 0)),
            'usage_pct': round(((usage.get('today') or 0) / limit) * 100, 1) if limit else 0,
        })

    @app.route('/api/v2/plans', methods=['GET'])
    def get_plans():
        """Get all available plans with features and pricing."""
        return jsonify({
            'success': True,
            'plans': PLAN_INFO,
            'payment_links': {k: v for k, v in PAYMENT_LINKS.items() if v},
        })

    print("  ✅ API Key management routes registered")


# ═══════════════════════════════════════════════════════════════
#  SHARED DECODE_JWT REFERENCE (set at init time, avoids circular import)
# ═══════════════════════════════════════════════════════════════

_decode_jwt_fn = None  # Set by init_tier_gating()

def _get_decode_jwt():
    """Get the decode_jwt function (passed in at init to avoid circular import)."""
    global _decode_jwt_fn
    if _decode_jwt_fn:
        return _decode_jwt_fn
    # Fallback: try lazy import (works if file is literally named main.py and importable)
    try:
        import main
        return main.decode_jwt
    except:
        return None


# ═══════════════════════════════════════════════════════════════
#  INITIALIZATION
# ═══════════════════════════════════════════════════════════════

def init_tier_gating(app, decode_jwt_func=None):
    """
    Initialize the full tier gating system.
    Call this after creating your Flask app.
    
    Usage in main.py:
        from api_tier_gating import init_tier_gating, require_plan
        init_tier_gating(app, decode_jwt_func=decode_jwt)
    """
    global _decode_jwt_fn

    print("\n🔐 Initializing API Tier Gating...")

    # Store decode_jwt reference to avoid circular imports
    if decode_jwt_func:
        _decode_jwt_fn = decode_jwt_func
        print("  ✅ JWT decoder linked (no circular import)")

    # 1. Create tables
    init_api_keys_table()

    # 2. Register after_request handler for tier headers
    app.after_request(add_tier_headers)

    # 3. Register Stripe v2 routes
    register_stripe_v2_routes(app)

    # 4. Register API key management routes
    register_api_key_routes(app)

    # 5. Add plan migration for users table
    try:
        conn = get_db()
        c = conn.cursor()
        conn.close()
    except:
        pass

    print("🔐 Tier Gating: Ready")
    print(f"   Free:       {TIER_RATE_LIMITS['free']:,} calls/day, {TIER_DAILY_RECORD_CAPS['free']:,} records/day, {TIER_PAGE_CAPS['free']} pages max")
    print(f"   Developer:  {TIER_RATE_LIMITS['developer']:,} calls/day, {TIER_DAILY_RECORD_CAPS['developer']:,} records/day, {TIER_PAGE_CAPS['developer']} pages max")
    print(f"   Pro:        {TIER_RATE_LIMITS['pro']:,} calls/day, {TIER_DAILY_RECORD_CAPS['pro']:,} records/day, {TIER_PAGE_CAPS['pro']} pages max")
    print(f"   Enterprise: {TIER_RATE_LIMITS['enterprise']:,} calls/day, {TIER_DAILY_RECORD_CAPS['enterprise']:,} records/day, unlimited pages")
    print("   Decorators: @require_plan('pro'), @require_plan('enterprise')")
    print()
