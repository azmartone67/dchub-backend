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
import tier_registry

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
    'research_seed_annual': os.environ.get('STRIPE_PRICE_RESEARCH_SEED_ANNUAL', 'price_XXXXX'),
    'founding':             os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),
    'developer_monthly':    os.environ.get('STRIPE_PRICE_DEV_MONTHLY', 'price_XXXXX'),
}

# Stripe Payment Links — fallback if price IDs not configured
# r-founding-canon (2026-08-01): 'founding' now DERIVES from
# routes/_stripe_links.py via tier_registry._stripe_link, the same canon
# migration PR #2102 applied to the lifecycle emails. This map had drifted
# to the pre-r-founder99 link (9B6fZi1c...), and unlike the email modules
# this one is served PUBLICLY and unauthenticated by /api/v2/stripe/config
# below, so the stale link was the one agents and the frontend read.
# Both links resolve to the same Stripe product — "DC Hub Founding Member",
# $99.00/mo recurring (verified against both checkout pages 2026-08-01) —
# so this is link-identity hygiene, NOT a price change and NOT a
# grandfathering event. The win is that the legacy plink is not the one
# carrying lookup_key/nickname → the subscription webhook's _PRICE_ID_PLAN
# map (flask_mcp_endpoints.py) only knows the CANONICAL price id, so
# purchases through the stale link book as plan_to='unknown'.
# _stripe_link returns None if routes/ is unimportable; the `if v` filter
# at /api/v2/stripe/config and the .get() guard in create_checkout_v2 both
# fail closed (omit the CTA / 400) rather than serving a wrong URL.
# Locked by tests/test_stripe_link_canonical.py.
PAYMENT_LINKS = {
    'pro_monthly':        tier_registry._stripe_link('pro'),  # canon (routes/_stripe_links.py); pre-reprice literal retired 2026-08-21 (SH52-103)
    'pro_annual':         'https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07',  # 50%-off one-time annual
    'enterprise_monthly': '',  # TODO: Create in Stripe Dashboard
    'enterprise_annual':  '',  # TODO: Create in Stripe Dashboard
    'research_seed_annual': 'https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e',
    'founding':           tier_registry._stripe_link('founding'),
    'developer_monthly':  'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
}

# Plan hierarchy (higher = more access)
# r32-sweep (2026-05-20): added anonymous + identified entries that
# were missing — same bug class as land_power_usage_limiter. Without
# these keys, dict.get(plan, default) silently fell through to free's
# values, meaning identified users (email-only signups) got the SAME
# data caps as anonymous walk-ins.
# r-tier-derive (2026-07-30): the hand-copied maps drifted a THIRD
# time — 'identified' (r32), 'founding' (r43-H, the Carl Braun
# mis-rank), and now 'starter'/'team' were absent from PLAN_LEVELS
# and all three limit maps below. A paying $9/mo Starter therefore
# fell through every .get() default: access level 0 == free (failing
# even the require_plan('identified') gate), 100 calls/day (advertised
# 500), 50 records/day (advertised 500 — pricing sells "10× the free
# quota"), 1 page/query (advertised 10). These maps are now DERIVED
# from tier_registry — THE canonical tier source — so they cannot
# drift again; the per-tier value rationale lives there. Locked by
# tests/test_tier_consistency.py::test_gating_maps_match_tier_registry.
# The derivation is value-identical for every previously present key
# and only adds the missing tiers ('starter' rank 2, 'team' rank 4).
PLAN_LEVELS = {t: v['rank'] for t, v in tier_registry.TIERS.items()}

# Rate limits per tier (API requests per day) — derived from
# tier_registry.TIER_LIMITS['rate_limit'] (r-tier-derive 2026-07-30;
# see PLAN_LEVELS comment above). 'anon' is a caller alias that
# tier_registry.TIER_LIMITS does not carry.
TIER_RATE_LIMITS = {t: v['rate_limit'] for t, v in tier_registry.TIER_LIMITS.items()}
TIER_RATE_LIMITS['anon'] = TIER_RATE_LIMITS['anonymous']

# ── Per-day unique record caps (prevents dataset vacuuming) ──
# Derived from tier_registry.TIER_LIMITS['record_cap'].
TIER_DAILY_RECORD_CAPS = {t: v['record_cap'] for t, v in tier_registry.TIER_LIMITS.items()}
TIER_DAILY_RECORD_CAPS['anon'] = TIER_DAILY_RECORD_CAPS['anonymous']

# ── Max pages per paginated query ──
# Derived from tier_registry.TIER_LIMITS['page_cap'].
TIER_PAGE_CAPS = {t: v['page_cap'] for t, v in tier_registry.TIER_LIMITS.items()}
TIER_PAGE_CAPS['anon'] = TIER_PAGE_CAPS['anonymous']

# ── Max results per single search/list query ──
# Deliberately NOT derived from tier_registry — this map is its own
# scale (per-query result width, not a daily quota) and has no
# TIER_LIMITS column. That also means r-tier-derive could not fix it:
# r-starter-sweep (2026-07-30) adds the keys by hand instead, because
# a missing key fell through enforce_search_limit's .get(tier, 25) to
# the ANONYMOUS cap. starter=100 joins the identified/founding/
# developer plateau (owner-confirmed in the PR); team==pro and
# research_seed==enterprise per tier_registry equivalence.
TIER_SEARCH_LIMITS = {
    'anonymous':  25,
    'anon':       25,
    'free':       50,
    'identified': 100,
    'starter':    100,
    'founding':   100,
    'developer':  100,
    'pro':        500,
    'team':       500,
    'enterprise': 1000,
    'research_seed': 1000,
    'admin':      9999,
}

# ── MCP per-tool result limits (facility/search arrays) ──
# r32-sweep: identified matches developer's 25 for FREE tools (the
# email-gated ones like get_news) so a free-with-email signup actually
# notices something improved. Paid-only tools still gated by
# PAID_ONLY_TOOLS in mcp_upgrade_gate.py.
# Deliberately NOT derived from tier_registry: registry mcp_results is
# a different, looser scale (it says anonymous 5; this map says 3), so
# r-tier-derive could not cover it. r-starter-sweep (2026-07-30) adds
# starter=20 by hand — between identified 15 and developer 25, and
# equal to api_data_protection's starter max_results_per_response=20,
# so both response-width caps agree (owner-confirmed in the PR).
# team==pro, research_seed==enterprise per tier_registry equivalence.
MCP_TIER_RESULT_LIMITS = {
    'anonymous':  3,
    'anon':       3,
    'free':       5,
    'identified': 15,
    'starter':    20,
    'founding':   25,
    'developer':  25,
    'pro':        100,
    'team':       100,
    'enterprise': 999,
    'research_seed': 999,
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
        'price_monthly': 299,   # audit #9: was 199 — canonical is tier_registry PRICES['pro']=299 (r-reprice 2026-06-19)
        'price_annual': 2392,   # preserves the prior ~8x annual ratio at the new $299 anchor
        'rate_limit': 5000,     # audit #9 follow-up: was 10000 — enforced limit is tier_registry TIER_LIMITS['pro']['rate_limit']=5000; no public surface sells 10k (pricing.html quotes the 2,000/day MCP lane)
        'tagline': 'Developer + market compare + PDF reports (5,000 calls/day)',
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


def generate_api_key(user_id, email=None, plan='free', name='Default'):
    """Generate a new API key for a user, return the raw key (only shown once).

    r43-H (2026-05-28): FIXED — the old INSERT referenced a non-existent
    `email` column, so this errored on EVERY call ("column \"email\" of
    relation \"api_keys\" does not exist"). That silently broke every
    auto-provision path through here (likely why several paid accounts
    had no key). Now matches the live schema / the Stripe webhook's
    working INSERT exactly. `email` is kept as an (unused) param for
    backwards-compat with existing callers. rate_limit_tier uses the
    EFFECTIVE api tier (founding→pro, research_seed→enterprise) so the
    key's quota matches the plan's benefits.
    """
    try:
        import tier_registry
        api_tier = tier_registry.api_tier(plan)
    except Exception:
        api_tier = {'founding': 'pro', 'research_seed': 'enterprise'}.get(plan, plan)
    _prefix_map = {'developer': 'dchub_dev_', 'pro': 'dchub_pro_', 'enterprise': 'dchub_ent_'}
    key_prefix_str = _prefix_map.get(api_tier, 'dchub_dev_')
    raw_key = key_prefix_str + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:16]

    conn = get_db()
    c = conn.cursor()
    # Same column set the webhook (handle_checkout_completed) uses — known
    # to match the live api_keys table. NO `email` column exists.
    c.execute("""
        INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions,
                              rate_limit_tier, is_active, created_at, usage_count,
                              plan, calls_today, calls_total)
        VALUES (%s, %s, %s, %s, '["read","write"]', %s, 1, %s, 0, %s, 0, 0)
    """, (user_id, key_hash, key_prefix, name, api_tier,
          datetime.now(timezone.utc).isoformat(), plan))
    conn.commit()
    conn.close()

    return raw_key



def validate_api_key(api_key):
    """Validate API key and return user info. BUG-005 FIX: Direct psycopg2 connection.

    UPDATED: Added NEON_DATABASE_URL as primary fallback — Railway DATABASE_URL
    sometimes points to wrong Neon DB (helium vs ep-old-waterfall).

    Phase BC-CRITICAL (2026-06-07): added dch_trial_* branch up-front. Without
    this, auto-minted trial keys (from mcp_high_intent_claim, state_visitor_claim,
    /api/v1/keys/auto-trial) 401'd on every @require_plan endpoint that imports
    this module — killing the entire post-mint UX described in the trial-key
    welcome message.
    """
    if not api_key:
        return None

    # Trial key branch — auto_trial_keys table, not api_keys.
    if api_key.startswith('dch_trial_'):
        try:
            from routes.auto_trial import validate_trial_key
            valid, _reason = validate_trial_key(api_key)
            if not valid:
                return None
            return {
                'user_id': api_key,
                'email': None,
                'plan': 'identified',
                'role': 'trial',
                'rate_limit_tier': 'identified',
            }
        except Exception:
            return None

    # r-onboarding-fix (2026-07-03, defect #9): dch_live_ MCP keys live in
    # mcp_dev_keys (keyed by api_key + email), NOT api_keys. Without this branch a
    # PAYING MCP customer's key resolves to None here -> treated as free/anonymous
    # on any Flask @require_plan path. Today the Node worker validates via
    # /keys/validate; this makes the Flask fallback agree instead of walling payers.
    if api_key.startswith('dch_live_'):
        import psycopg2 as _pg
        _c = None
        try:
            _url = (os.environ.get('NEON_DATABASE_URL')
                    or os.environ.get('DATABASE_URL')
                    or os.environ.get('DATABASE_READ_URL'))
            if not _url:
                return None
            _c = _pg.connect(_url, connect_timeout=5)
            _cur = _c.cursor()
            _cur.execute(
                "SELECT email, COALESCE(tier,'free'), COALESCE(status,'active') "
                "FROM mcp_dev_keys WHERE api_key = %s LIMIT 1", (api_key,))
            _r = _cur.fetchone()
            _cur.close()
            if not _r or _r[2] != 'active':
                return None
            _email = _r[0]
            _tier = (_r[1] or 'free').lower()
            # map mcp_dev_keys.tier -> a plan the @require_plan gates understand
            _plan = {'paid': 'pro', 'pro': 'pro', 'developer': 'developer',
                     'founding': 'founding', 'enterprise': 'enterprise',
                     'starter': 'starter'}.get(_tier, 'free')
            return {
                'user_id': api_key,
                'email': _email,
                'plan': _plan,
                'role': 'mcp',
                'rate_limit_tier': _plan,
            }
        except Exception as _e:
            print(f"[defect9] dch_live_ validate error: {str(_e)[:120]}")
            return None
        finally:
            if _c:
                try:
                    _c.close()
                except Exception:
                    pass

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
        # r73-b (2026-05-26): try BOTH the SHA256-hashed lookup (this
        # module's original contract) AND the raw-key lookup (the
        # legacy main.py:10656 + routes/partner_key_issuer pattern).
        # The codebase has two storage conventions in api_keys.key_hash
        # depending on which mint path was used:
        #   - api_tier_gating.generate_api_key:  stores SHA256 hash
        #   - main.py:10656 INSERT (auth_handler): stores raw key_str
        #   - routes/partner_key_issuer (r72):    stores raw key_str
        # Trying both lets ANY mint path produce a working key without
        # forcing a one-shot DB migration. Hashed lookup first (covers
        # the older keys); raw fallback covers the newer partner keys.
        cur.execute("""
            SELECT u.id, u.email, u.plan, u.role, ak.rate_limit_tier
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key_hash = %s AND ak.is_active = 1
            LIMIT 1
        """, (key_hash,))
        row = cur.fetchone()
        if not row:
            # Fallback: lookup by RAW key_str (legacy + partner-key path)
            cur.execute("""
                SELECT u.id, u.email, u.plan, u.role, ak.rate_limit_tier
                FROM api_keys ak
                JOIN users u ON ak.user_id = u.id
                WHERE ak.key_hash = %s AND ak.is_active = 1
                LIMIT 1
            """, (api_key,))
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
                if is_valid_internal_key(internal_key):
                    return f(*args, **kwargs)

                # ── STEP 0b (Phase ZZZZZ-round22): map endpoint bypass ─
                # Read-only geographic map data — same logic as the early
                # require_plan stub in main.py. Keeps /land-power
                # rendering for any browser session on dchub.cloud, even
                # if the JWT auto-injection didn't reach the call site.
                # Mirrored here because some routes
                # (e.g. /api/v1/infrastructure/substations) use this
                # require_plan directly via @_infra_require_plan.
                _MAP_BYPASS_PATHS = (
                    '/api/v1/gas-pipelines',
                    '/api/v1/infrastructure/substations',
                    '/api/v1/infrastructure/transmission',
                    '/api/v1/infrastructure/power-plants',
                    # r66 (2026-06-02): fiber/permits/properties REMOVED from the
                    # referer-bypass + given @_infra_require_plan('pro') on their
                    # routes. These return proprietary data (fiber: 500 carrier
                    # routes w/ fiber_count; permits: developer+MW; properties:
                    # price_per_acre) and were leaking to any anon caller because
                    # the CF worker injects a dchub.cloud Referer on every request
                    # (see reference_dchub_cf_injects_referer). The map's own fiber
                    # preview uses the capped /api/fiber/routes (still open, 20-row
                    # teaser); MCP keeps full access via X-Internal-Key (STEP 0).
                    '/api/v1/infrastructure/nearby',
                    '/api/v1/infrastructure/summary',
                    '/api/v1/energy/power-plants',
                    '/api/v1/energy/power-plants/nearby',
                    # 2026-07-15: non-/energy/ alias of the same route -- the
                    # land-power map (js/dchub-infrastructure.js) calls this path
                    # keyless; keep it bypassed so the power-plant layer still
                    # renders now that the route carries @require_plan('pro').
                    '/api/v1/power-plants/nearby',
                    '/api/v1/energy/rto/demand',
                    '/api/v1/energy/rto/fuelmix',
                    '/api/v1/energy/naturalgas/price',
                    '/api/v1/energy/retail/rates',
                    '/api/v1/energy/gas-storage',
                    '/api/v1/fiber/routes',
                    '/api/v1/fiber/sources',
                    # 2026-07-17: removed /api/v1/connectivity/{ixps,facilities,score}
                    # here and in free_tier_gate._MAP_BYPASS_PATHS (kept in sync).
                    # No route serves those paths in either main.py or a blueprint;
                    # verified 404 in prod. See the fuller note in free_tier_gate.
                    # r-grid-catchall (2026-08-08): /api/v1/grid/overview and
                    # /api/v1/grid/status have NO handler. They reached the
                    # /api/v1/grid/<iso> converter, which forwarded them as REGIONS —
                    # and because these two were bypass-listed they skipped the plan
                    # gate and answered 200 with a fabricated region ("STATUS",
                    # "OVERVIEW"). The converter now 404s an unknown region, so these
                    # entries describe nothing. Removed rather than left as dead config.
                    # 2026-07-15: legacy CAISO map proxies
                    # (js/gridstatus-integration.js) -- bypassed for the
                    # dchub.cloud map now that both routes carry
                    # @require_plan('pro').
                    '/api/v1/grid/caiso/fuelmix',
                    '/api/v1/grid/caiso/demand',
                    # r36 (2026-05-31): removed markets/compare + v1/deals +
                    # pipeline/summary (+ dead /api/deals) from this SECOND
                    # _MAP_BYPASS_PATHS too. This is the api_tier_gating /
                    # _real_require_plan copy that pipeline/summary actually
                    # routes through (via deals_routes._lazy_require_plan), which
                    # is why cleaning only main.py's list left it Referer-bypassable.
                    # markets/compare + deals are now freemium (@protect_data, no
                    # @require_plan); pipeline/summary stays Pro but is no longer
                    # served to anon scrapers via the CF-injected dchub.cloud Referer.
                    # See reference_dchub_cf_injects_referer.
                    '/api/v1/oilgas/search',
                    '/api/facilities',
                    '/api/grid/demand',
                    '/api/grid/prices',
                    '/api/grid/all-isos',
                    '/api/discovery/facilities',
                    '/api/epa/facilities',
                    '/api/renewable/solar',
                    '/api/renewable/wind',
                    '/api/renewable/combined',
                    '/api/site-score',
                    '/api/carbon/intensity',
                    '/api/risk/assessment',
                    '/api/v2/risk/active-fires',
                    '/api/auth/me',
                )
                if request.method == 'GET':
                    origin = (request.headers.get('Origin', '')
                              or request.headers.get('Referer', ''))
                    if 'dchub.cloud' in origin:
                        rp = request.path or ''
                        if any(rp == p or rp.startswith(p + '/')
                                for p in _MAP_BYPASS_PATHS):
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
                            # AI Wars verification keys — env-driven, NO hardcoded
                            # fallback (2026-07-25 security close; the strings were
                            # public in this repo = free PRO to anyone). Unset env
                            # => empty set => leaked strings grant nothing. Set
                            # DCHUB_AI_WARS_KEYS in Railway with fresh values to re-enable.
                            AI_WARS_KEYS_TIER = {k.strip() for k in
                                os.environ.get("DCHUB_AI_WARS_KEYS", "").split(",") if k.strip()}
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
                    return jsonify(_rich_gate_response(
                        path=request.path,
                        min_plan=min_plan,
                        current_tier='free',
                        error_code='plan_required',
                    )), 403, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

                # ── STEP 5: Check tier level ───────────────────────────
                if not user_has_access(user_plan, min_plan):
                    return jsonify(_rich_gate_response(
                        path=request.path,
                        min_plan=min_plan,
                        current_tier=user_plan,
                        error_code='plan_upgrade_required',
                        user_id=getattr(request, 'api_key_info', {}).get('key') if hasattr(request, 'api_key_info') else None,
                    )), 403, {'Cache-Control': 'private, no-store, max-age=0', 'Surrogate-Control': 'no-store', 'Pragma': 'no-cache'}

                # ── STEP 6: Authorized — add headers and proceed ───────
                plan_limit = TIER_RATE_LIMITS.get(user_plan, 100)
                g.user_tier = user_plan
                g.tier_headers = {
                    'X-RateLimit-Limit': str(plan_limit),
                    'X-RateLimit-Tier': user_plan,
                    'X-Auth-Method': auth_method,
                    'X-Powered-By': 'DC Hub',
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


# ═══════════════════════════════════════════════════════════════
# Phase X (2026-05-12): rich paywall response for REST endpoints
# ═══════════════════════════════════════════════════════════════
#
# Trigger: MCP funnel showed 8,172 upgrade signals in 7 days but only 1
# conversion in 30 days. Root cause: the inline paywall response above
# omits every field that drives conversion — no human_message markdown,
# no one_click_upgrade_url Stripe link, no call-count escalation,
# no discount codes. MCP tool responses (which DO use
# build_paywall_response) get the full conversion-optimized envelope,
# but REST endpoints get a minimal `{error, plans, pricing_url}`.
# Many MCP tools internally call the REST endpoints, so the dumb
# paywall was overriding the rich one for the most common call path.
#
# This wrapper composes build_paywall_response() (the canonical
# conversion-optimized envelope) into the legacy gate shape. We keep
# the legacy fields (`success`, `error`, `plans`, `pricing_url`,
# `signup_url`, `free_alternative`) for back-compat with any client
# that depends on them, AND we add the missing rich fields
# (`human_message`, `one_click_upgrade_url`, `upgrade_url` with
# attribution, `tier_signal`).
#
# Net effect: every 403 from /api/v1/pipeline, /api/v1/grid-intelligence,
# /api/v1/fiber-intel, etc. now carries the same conversion language
# the MCP tools do. AI agents render it; humans see the Stripe link;
# the funnel actually closes.
def _path_to_tool_name(path: str) -> str:
    """Derive a stable tool-name slug from a REST path. Used by
       build_paywall_response for tier escalation + attribution params.

       Examples:
         /api/v1/pipeline?market=ashburn         → 'pipeline'
         /api/v1/grid-intelligence                → 'grid_intelligence'
         /api/v1/fiber/metro/ashburn              → 'fiber_metro'
         /api/v1/markets/chicago                  → 'markets_chicago'
    """
    if not path: return 'unknown'
    p = path.split('?', 1)[0].rstrip('/')
    # strip /api/v1/ prefix and replace path separators with underscores
    if p.startswith('/api/v1/'):
        p = p[len('/api/v1/'):]
    elif p.startswith('/api/'):
        p = p[len('/api/'):]
    # /pipeline → pipeline; /fiber/metro/ashburn → fiber_metro (drop value tail)
    parts = [seg for seg in p.split('/') if seg]
    # drop trailing path-parameter values (e.g. specific market slug)
    if len(parts) > 1 and len(parts[-1]) < 30 and any(c.islower() for c in parts[-1]):
        # heuristic: last segment is a value if previous is a noun like "metro"
        if parts[-2] in ('metro', 'state', 'country', 'market', 'id'):
            parts = parts[:-1]
    name = '_'.join(parts).replace('-', '_')
    return name or 'unknown'


def _rich_gate_response(path: str, min_plan: str,
                        current_tier: str = 'free',
                        error_code: str = 'plan_required',
                        user_id: str | None = None) -> dict:
    """Compose build_paywall_response() with the legacy gate envelope.
       Returns a single dict ready for jsonify()."""
    try:
        from utils.paywall_response import build_paywall_response
    except Exception:
        build_paywall_response = None

    tool_name = _path_to_tool_name(path)
    base = {
        'success': False,
        'error': error_code,
        'message': f'This endpoint requires a {min_plan.title()} plan or higher.',
        'plans': _build_gate_plans(),
        'signup_url': SIGNUP_URL,
        'pricing_url': PRICING_URL,
        'free_alternative': _get_free_alternative(path),
        'current_plan': current_tier,
        'required_plan': min_plan,
        'tool': tool_name,
    }

    # Merge in the rich conversion fields. If paywall_response import
    # ever breaks, the legacy envelope still goes out.
    if build_paywall_response is not None:
        try:
            rich = build_paywall_response(
                tool_name=tool_name,
                user_id=user_id,
                current_tier=current_tier,
                error_code=error_code,
            )
            # Rich envelope wins for fields it sets; legacy fields fill
            # anything the rich envelope omits.
            for k, v in rich.items():
                if k == 'error':
                    continue  # keep legacy error_code
                base[k] = v
        except Exception as e:
            base['_paywall_build_error'] = str(e)[:200]

    return base


def _get_free_alternative(path):
    """Suggest a free endpoint the user can use instead."""
    alternatives = {
        '/api/v1/facilities': '/api/v1/stats (aggregate stats, no auth required)',
        '/api/v1/map': '/api/v1/stats (facility counts by region)',
        '/api/v1/search': '/api/v1/stats (summary statistics)',
        '/api/v1/deals': '/api/ai/query?type=stats (aggregate deal count)',
        '/api/deals': '/api/ai/query?type=stats (aggregate deal count)',
        '/api/v1/transactions': '/api/ai/query?type=stats (aggregate deal count)',
        '/api/v1/pipeline': '/api/ai/query?type=stats (capacity pipeline total)',
        '/api/v1/markets/': '/api/v1/markets/list (market list, free)',
        '/api/v1/connectivity': '/api/v1/stats (basic connectivity info)',
        '/api/v1/energy': '/api/grid/supported-isos (ISO list, free)',
        '/api/brain/': '/api/ai/query?type=stats (basic stats, free)',
        '/api/reports/generate': '/api/market-report (JSON summary, free)',
    }
    for prefix, alt in alternatives.items():
        if path.startswith(prefix):
            return alt
    return '/api/v1/stats (free, no auth required)'


FACILITY_TIER_LIMITS = {
    'anon': 50, 'free': 200, 'founding': 500, 'developer': 500,
    'pro': 2000, 'enterprise': 9999, 'research_seed': 9999, 'developer': 1000, 'admin': 9999,
}
FACILITY_VISIBLE_FIELDS = {
    # 'state' added 2026-06-23: basic public geo (same class as city), used by the
    # map + /database — was being stripped from anon/free, leaving blank state cols.
    'anon': {'name', 'city', 'state', 'country', 'latitude', 'longitude', 'status', 'slug'},
    'free': {'name', 'city', 'state', 'country', 'latitude', 'longitude', 'status', 'slug',
             'provider', 'operator', 'region', 'market'},
}
# All free-class tiers share 'free's preview (no MW/details). claim_free_key mints
# 'identified' and the anon auto-trial mints 'trial'/'trial_taste' — without explicit
# keys these fall to None=full. Per the advertised table, full facility data (incl
# MW) starts at Starter; Free/Identified/Trial = preview only.
for _ft in ('identified', 'trial', 'trial_taste'):
    FACILITY_VISIBLE_FIELDS[_ft] = FACILITY_VISIBLE_FIELDS['free']

def get_request_tier():
    """Non-blocking tier detection from JWT/API key/cookie. Returns plan or 'anon'."""
    try:
        from flask import request
        internal_key = request.headers.get('X-Internal-Key', '')
        if is_valid_internal_key(internal_key):
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
            # r-fix10 (2026-06-27): api_keys does NOT hold mcp_dev_keys (dch_* keys),
            # so a paying Developer/Pro/Enterprise on an MCP key fell through to 'anon'
            # and got thin facility data on the REST API. Resolve via the canonical
            # mcp_dev_keys mapper (paid→developer, enterprise→enterprise; free/identified
            # →None so they stay preview). Full facility DATA is developer-class, so this
            # delivers the advertised "full DB via API" without over-granting pro-only
            # deliverables (those gate on mcp_gatekeeper separately).
            try:
                from api_data_protection import _resolve_key_tier as _rkt
                _mt = _rkt(api_key)
                if _mt:
                    return _mt
            except Exception:
                pass
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


# Phase WW (2026-05-15): get_user_key_from_request() removed.
# No production callers — only references were the function's own
# definition + the auth_context.py docstring listing it as one of
# the 5 legacy resolvers. Cross-file grep confirmed zero importers.
# routes/auth_context.get_auth_context() is the canonical replacement
# for any future caller that needs "who is this request from".
# 47 LOC removed.


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
    
    # ★2026-09-02: every cap and price here is a registry read. The literal
    # dict it replaces said "Pro ($199/mo)" — a price retired 2026-06-19 —
    # and hand-typed record caps that TIER_LIMITS already owns.
    try:
        import tier_registry as _tr

        def _rc(t):
            return int(_tr.TIER_LIMITS[t]['record_cap'])

        def _pr(t):
            return int(_tr.price(t) or 0)

        _dev = f"Upgrade to Developer (${_pr('developer')}/mo) for {_rc('developer'):,} records/day."
        upgrade_msg = {
            'anon': f"Sign up free at dchub.cloud to get {_rc('free'):,} records/day.",
            'free': _dev,
            'founding': _dev,
            'developer': f"Upgrade to Pro (${_pr('pro')}/mo) for {_rc('pro'):,} records/day.",
            'pro': 'Contact us for Enterprise access with unlimited records.',
        }
    except Exception:
        upgrade_msg = {}
    
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
                success_url=f'https://dchub.cloud/dashboard.html?payment=success&plan={plan}',
                cancel_url='https://dchub.cloud/pricing?payment=cancelled',
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
        'research_seed_annual': 'research_seed',  # NEW: research-institution tier (NLR et al.)
        'developer_monthly': 'developer',         # also missing previously
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
    if tier in ('pro', 'enterprise', 'founding', 'research_seed', 'developer'):
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

    # r43-H: cursor-agnostic — get_db() may return tuple OR dict rows.
    user_id = user['id'] if isinstance(user, dict) else user[0]

    # Effective api tier for the key's rate-limit (founding→pro, etc.)
    try:
        import tier_registry
        api_tier = tier_registry.api_tier(plan)
    except Exception:
        api_tier = {'founding': 'pro', 'research_seed': 'enterprise'}.get(plan, plan)

    # Check if they already have a key
    c.execute("SELECT id FROM api_keys WHERE user_id = %s AND is_active = 1", (user_id,))
    existing = c.fetchone()

    if existing:
        # r43-H: bump BOTH plan and rate_limit_tier so quota matches benefits
        c.execute("UPDATE api_keys SET plan = %s, rate_limit_tier = %s "
                  "WHERE user_id = %s AND is_active = 1", (plan, api_tier, user_id))
        conn.commit()
        conn.close()
        print(f"  ↑ Upgraded existing API key to {plan} (tier {api_tier}) for {email}")
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


# Phase UU-2 (2026-05-15): removed dead register_api_key_routes() —
# all 5 @app.route handlers were shadows of routes already registered by
# api_monetization.monetization_bp. Caller at line 1529 was already
# stripped in the same PR. See git log for the deleted function body.

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

    # 4. (Removed in Phase UU-2, 2026-05-15) — register_api_key_routes()
    #    used to register /api/v2/keys, /api/v2/usage, /api/v2/plans as
    #    direct @app.route handlers. These were shadow-duplicates of the
    #    same paths registered by api_monetization.monetization_bp (the
    #    canonical blueprint). Shadow registration left Flask non-
    #    deterministic about which version served a request — fixed by
    #    deleting register_api_key_routes + this caller. See PR #194.

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
