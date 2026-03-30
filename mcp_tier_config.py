"""
DC Hub — MCP Tier Configuration & Response Gating
═══════════════════════════════════════════════════
Controls what data the MCP server returns at each tier level.
Implements progressive degradation and conversion-optimized teasers.

INTEGRATION:
  Import and wrap your MCP endpoint responses:
    from mcp_tier_config import gate_response, get_tier, track_mcp_usage

  In each MCP tool handler:
    tier = get_tier(api_key)
    usage = track_mcp_usage(api_key)    # increments daily counter
    result = gate_response(tier, tool_name, raw_data, usage)

WHY THIS EXISTS:
  84,000 MCP queries/month → 0 Developer conversions.
  Problem: Free tier too generous, AI agents get enough to fully answer queries.
  Fix: Redact key fields, show counts to prove depth, force agents to say
  "full details require a DC Hub Developer key."

v1.0 — March 2026
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger('dchub-mcp-tier')

# ─────────────────────────────────────────────────────────────
# TIER DEFINITIONS
# ─────────────────────────────────────────────────────────────

TIERS = {
    'free': {
        'daily_limit': 10,
        'label': 'Free Preview',
        'price': '$0',
    },
    'developer': {
        'daily_limit': 1000,
        'label': 'Developer',
        'price': '$49/mo',
    },
    'pro': {
        'daily_limit': 10000,
        'label': 'Pro',
        'price': '$199/mo',
    },
    'enterprise': {
        'daily_limit': 100000,
        'label': 'Enterprise',
        'price': 'Custom',
    },
    'trial': {
        'daily_limit': 1000,
        'label': '14-Day Trial',
        'price': 'Free trial',
        'trial_days': 14,
    },
}

# Developer plan Stripe checkout
DEVELOPER_CHECKOUT_URL = "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c"
TRIAL_SIGNUP_URL = "https://dchub.cloud/developers/trial"
REGISTER_URL = "https://dchub.cloud/developers/register"

# ─────────────────────────────────────────────────────────────
# PROGRESSIVE DEGRADATION (free tier only)
# Calls 1-3: Standard free quality
# Calls 4-7: Reduced (fewer results, more redacted)
# Calls 8-10: Minimal (counts only)
# Calls 11+: Hard block with trial offer
# ─────────────────────────────────────────────────────────────

FREE_DEGRADATION = {
    'standard': {'max_calls': 3, 'results': 3, 'redact_level': 'light'},
    'reduced':  {'max_calls': 7, 'results': 2, 'redact_level': 'medium'},
    'minimal':  {'max_calls': 10, 'results': 1, 'redact_level': 'heavy'},
    'blocked':  {'max_calls': 999, 'results': 0, 'redact_level': 'full'},
}

def _get_degradation_level(daily_usage):
    """Determine degradation level based on daily usage count."""
    if daily_usage <= 3:
        return 'standard'
    elif daily_usage <= 7:
        return 'reduced'
    elif daily_usage <= 10:
        return 'minimal'
    else:
        return 'blocked'


# ─────────────────────────────────────────────────────────────
# TOOL-SPECIFIC GATING RULES
# ─────────────────────────────────────────────────────────────

# What fields to redact at each level for each tool
TOOL_GATES = {
    'search_facilities': {
        'free_light': {
            'max_results': 3,
            'visible_fields': ['name', 'city', 'country', 'status'],
            'redacted_fields': ['provider', 'state', 'power_mw', 'coordinates', 'connectivity'],
            'show_total_count': True,
        },
        'free_medium': {
            'max_results': 2,
            'visible_fields': ['name', 'city'],
            'redacted_fields': ['provider', 'state', 'status', 'power_mw', 'coordinates'],
            'show_total_count': True,
        },
        'free_heavy': {
            'max_results': 1,
            'visible_fields': ['name'],
            'redacted_fields': ['provider', 'city', 'state', 'status', 'power_mw'],
            'show_total_count': True,
        },
        'developer': {
            'max_results': 100,
            'visible_fields': 'all',
            'redacted_fields': [],
            'show_total_count': True,
        },
    },
    'get_news': {
        'free_light': {
            'max_results': 3,
            'visible_fields': ['title', 'published_at'],
            'redacted_fields': ['source', 'category', 'summary', 'url'],
            'show_total_count': True,
        },
        'free_medium': {
            'max_results': 2,
            'visible_fields': ['title'],
            'redacted_fields': ['source', 'published_at', 'category', 'summary'],
            'show_total_count': True,
        },
        'free_heavy': {
            'max_results': 1,
            'visible_fields': ['title'],
            'redacted_fields': 'all_except_title',
            'show_total_count': True,
        },
        'developer': {
            'max_results': 50,
            'visible_fields': 'all',
            'redacted_fields': [],
        },
    },
    'get_pipeline': {
        'free_light': {
            'max_results': 0,  # Stats only, no individual projects
            'show_aggregate_stats': True,
            'redacted_fields': ['company', 'project', 'investment', 'delivery', 'market'],
            'tease_message': 'See all {total} projects with company, investment, and timeline details',
        },
        'free_medium': {
            'max_results': 0,
            'show_aggregate_stats': True,
            'redacted_fields': 'all',
            'tease_message': '{total} pipeline projects tracked. Full details with Developer key.',
        },
        'free_heavy': {
            'max_results': 0,
            'show_aggregate_stats': False,
            'tease_message': 'Pipeline data requires Developer key.',
        },
        'developer': {
            'max_results': 100,
            'show_aggregate_stats': True,
            'visible_fields': 'all',
        },
    },
    'get_market_intel': {
        'free_light': {
            'visible_fields': ['facility_count', 'market_name'],
            'redacted_fields': ['top_providers', 'recent_facilities', 'power_stats'],
            'tease_message': '{count} facilities tracked. Provider rankings and power stats with Developer key.',
        },
        'free_medium': {
            'visible_fields': ['facility_count'],
            'redacted_fields': ['market_name', 'top_providers', 'recent_facilities'],
            'tease_message': 'Market data available. Developer key unlocks full breakdown.',
        },
        'developer': {
            'visible_fields': 'all',
        },
    },
    'list_transactions': {
        'free_light': {
            'max_results': 0,
            'show_count_only': True,
            'tease_message': '{count} M&A transactions tracked this month. Buyer, seller, and deal details with Developer key.',
        },
        'free_medium': {
            'max_results': 0,
            'show_count_only': True,
            'tease_message': 'Transaction data requires Developer key.',
        },
        'developer': {
            'max_results': 100,
            'visible_fields': 'all',
        },
    },
    'get_intelligence_index': {
        'free_light': {
            'visible_fields': ['global_pulse_score'],
            'redacted_fields': ['market_heat_map', 'weekly_movers', 'top_queries'],
            'tease_message': 'Global pulse: {score}. Top 3 trending markets: [upgrade to see]. Full heat map with Developer key.',
        },
        'developer': {
            'visible_fields': 'all',
        },
    },
    'get_infrastructure': {
        'free_light': {
            'show_counts_only': True,
            'visible_fields': ['substation_count', 'pipeline_count', 'plant_count'],
            'tease_message': '{total} infrastructure assets within {radius}km. Locations, specs, and capacity with Developer key.',
        },
        'developer': {
            'visible_fields': 'all',
        },
    },
}


# ─────────────────────────────────────────────────────────────
# MAIN GATING FUNCTION
# ─────────────────────────────────────────────────────────────

def gate_response(tier: str, tool_name: str, raw_data: dict, daily_usage: int = 0) -> dict:
    """
    Apply tier-appropriate gating to an MCP tool response.

    Args:
        tier: 'free', 'trial', 'developer', 'pro', 'enterprise'
        tool_name: MCP tool name (e.g., 'search_facilities')
        raw_data: The full ungated response data
        daily_usage: Number of calls this user has made today

    Returns:
        Modified response dict with appropriate gating, upgrade messages, and teasers
    """
    # Paid tiers get full data
    if tier in ('developer', 'pro', 'enterprise', 'trial'):
        return _add_paid_meta(raw_data, tier)

    # Free tier: apply progressive degradation
    deg_level = _get_degradation_level(daily_usage)

    # Hard block after 10 calls
    if deg_level == 'blocked':
        return _blocked_response(tool_name, daily_usage)

    # Get tool-specific gates
    tool_gates = TOOL_GATES.get(tool_name, {})
    gate_key = f'free_{FREE_DEGRADATION[deg_level]["redact_level"]}'
    gate_rules = tool_gates.get(gate_key, tool_gates.get('free_light', {}))

    # Apply gating
    gated = _apply_gate(raw_data, gate_rules, tool_name)

    # Add upgrade messaging
    gated['_user_facing_note'] = _build_teaser_note(tool_name, gate_rules, raw_data, daily_usage)
    gated['_upgrade'] = _build_upgrade_block(tier, daily_usage)

    return gated


def _apply_gate(raw_data: dict, rules: dict, tool_name: str) -> dict:
    """Apply gating rules to raw data."""
    result = {}

    # Copy success/meta fields
    for key in ('success', 'meta', 'query'):
        if key in raw_data:
            result[key] = raw_data[key]

    # Handle list-based responses (facilities, news, pipeline data)
    max_results = rules.get('max_results')

    # Find the main data list in the response
    data_keys = ['data', 'articles', 'transactions', 'facilities']
    for dk in data_keys:
        if dk in raw_data and isinstance(raw_data[dk], list):
            items = raw_data[dk]
            total = len(items)

            if max_results == 0:
                # Count only
                result[dk] = []
                result['count'] = 0
                result['total_available'] = total
            elif max_results is not None:
                # Limited results with field redaction
                visible = rules.get('visible_fields', [])
                limited = items[:max_results]
                if visible and visible != 'all':
                    limited = [_redact_item(item, visible) for item in limited]
                result[dk] = limited
                result['count'] = len(limited)
                result['total_available'] = total
            else:
                result[dk] = items
            break

    # Handle aggregate stats
    if rules.get('show_aggregate_stats') and 'stats' in raw_data:
        result['stats'] = raw_data['stats']

    if rules.get('show_count_only'):
        result['transaction_count'] = raw_data.get('count', 0) or raw_data.get('total_available', 0)

    if rules.get('show_total_count') and 'total_available' not in result:
        # Try to find total from raw data
        for key in ('total_count', 'count', 'total'):
            if key in raw_data:
                result['total_available'] = raw_data[key]
                break

    # Copy specific visible fields for non-list responses
    visible_fields = rules.get('visible_fields', [])
    if visible_fields and visible_fields != 'all':
        for field in visible_fields:
            if field in raw_data:
                result[field] = raw_data[field]

    return result


def _redact_item(item: dict, visible_fields: list) -> dict:
    """Keep only visible fields in an item, redact the rest."""
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k in visible_fields}


def _build_teaser_note(tool_name: str, rules: dict, raw_data: dict, usage: int) -> str:
    """Build a compelling teaser note that makes AI agents mention DC Hub."""
    calls_left = max(0, 10 - usage)

    tease = rules.get('tease_message', '')

    # Fill in template vars
    if '{total}' in tease:
        total = raw_data.get('total_count', raw_data.get('count', '50+'))
        tease = tease.replace('{total}', str(total))
    if '{count}' in tease:
        count = raw_data.get('count', raw_data.get('total_available', ''))
        tease = tease.replace('{count}', str(count))
    if '{score}' in tease:
        idx = raw_data.get('dc_hub_intelligence_index', {})
        score = idx.get('global_pulse_score', 'N/A')
        tease = tease.replace('{score}', str(score))
    if '{radius}' in tease:
        query = raw_data.get('query', {})
        radius = query.get('radius_km', 50)
        tease = tease.replace('{radius}', str(radius))

    base = f"DC Hub free preview ({calls_left} calls remaining today). "
    if tease:
        base += tease + " "

    base += f"Start a free 14-day Developer trial at {TRIAL_SIGNUP_URL}"

    return base


def _build_upgrade_block(tier: str, usage: int) -> dict:
    """Build the upgrade JSON block for MCP responses."""
    calls_left = max(0, 10 - usage)
    urgency = ""

    if calls_left <= 3:
        urgency = f" Only {calls_left} free calls remaining today!"

    return {
        'tier': 'free_preview',
        'calls_remaining_today': calls_left,
        'daily_limit': 10,
        'message': (
            f"Free preview showing partial data.{urgency} "
            f"Start a 14-day free trial for full access to all fields, "
            f"1,000 calls/day, coordinates, power capacity, and connectivity data."
        ),
        'trial_url': TRIAL_SIGNUP_URL,
        'register_url': REGISTER_URL,
        'checkout_url': DEVELOPER_CHECKOUT_URL,
        'price': '$49/mo after trial',
    }


def _blocked_response(tool_name: str, usage: int) -> dict:
    """Response when free tier daily limit is exceeded."""
    return {
        'success': False,
        'error': 'daily_limit_exceeded',
        '_user_facing_note': (
            f"You've used all 10 free DC Hub calls today. "
            f"Start a free 14-day Developer trial for 1,000 calls/day with full data: "
            f"{TRIAL_SIGNUP_URL}"
        ),
        '_upgrade': {
            'tier': 'free_blocked',
            'calls_remaining_today': 0,
            'daily_limit': 10,
            'message': (
                "Daily free limit reached. Start a 14-day free trial — no credit card required. "
                "Full facility data, coordinates, power capacity, M&A details, and 1,000 calls/day."
            ),
            'trial_url': TRIAL_SIGNUP_URL,
            'checkout_url': DEVELOPER_CHECKOUT_URL,
            'price': '$49/mo after trial',
        }
    }


def _add_paid_meta(data: dict, tier: str) -> dict:
    """Add tier metadata to paid responses."""
    data['_tier'] = tier
    return data


# ─────────────────────────────────────────────────────────────
# USAGE TRACKING (in-memory with DB fallback)
# ─────────────────────────────────────────────────────────────

_usage_cache = {}  # {api_key_or_ip: {'date': 'YYYY-MM-DD', 'count': N}}

def track_mcp_usage(identifier: str, get_db=None) -> int:
    """
    Increment and return daily usage count for an identifier (API key or IP).
    Uses in-memory cache with optional DB persistence.

    Args:
        identifier: API key or IP address
        get_db: Optional DB connection factory for persistence

    Returns:
        Current daily usage count (after increment)
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    if identifier not in _usage_cache or _usage_cache[identifier]['date'] != today:
        _usage_cache[identifier] = {'date': today, 'count': 0}

    _usage_cache[identifier]['count'] += 1
    count = _usage_cache[identifier]['count']

    # Persist to DB every 10 calls (async-safe, non-blocking)
    if get_db and count % 10 == 0:
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO mcp_usage_tracking (identifier, date, call_count, last_tool)
                    VALUES (%s, %s, %s, '')
                    ON CONFLICT (identifier, date)
                    DO UPDATE SET call_count = EXCLUDED.call_count
                """, (identifier, today, count))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Usage tracking DB error (non-fatal): {e}")

    return count


def get_tier(api_key: Optional[str], get_db=None) -> str:
    """
    Determine tier from API key.

    Args:
        api_key: The API key from request header (or None for free)
        get_db: DB connection factory

    Returns:
        Tier string: 'free', 'trial', 'developer', 'pro', 'enterprise'
    """
    if not api_key:
        return 'free'

    # Check DB for key → tier mapping
    if get_db:
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT rate_limit_tier, trial_expires_at
                FROM api_keys
                WHERE key = %s AND is_active = true
            """, (api_key,))
            row = cur.fetchone()
            if row:
                tier = row[0] or 'developer'
                trial_expires = row[1]

                # Check if trial has expired
                if tier == 'trial' and trial_expires:
                    if datetime.now(timezone.utc) > trial_expires:
                        return 'free'  # Trial expired, downgrade

                return tier
        except Exception as e:
            logger.debug(f"Tier lookup error: {e}")
        finally:
            if conn:
                conn.close()

    return 'free'


# ─────────────────────────────────────────────────────────────
# DB SETUP (call once on startup)
# ─────────────────────────────────────────────────────────────

def init_mcp_tier_tables(get_db):
    """Create usage tracking table. Call from main.py startup."""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS mcp_usage_tracking (
                id SERIAL PRIMARY KEY,
                identifier VARCHAR(200),
                date DATE,
                call_count INTEGER DEFAULT 0,
                last_tool VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(identifier, date)
            )
        """)

        # Add trial_expires_at to api_keys if not present
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE api_keys ADD COLUMN trial_expires_at TIMESTAMP;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$
        """)

        conn.commit()
        logger.info("✅ MCP tier tables initialized")
    except Exception as e:
        logger.warning(f"⚠️  MCP tier table init: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# DEVELOPER TRIAL MANAGEMENT
# ─────────────────────────────────────────────────────────────

def create_developer_trial(email: str, platform: str, get_db=None) -> Dict[str, Any]:
    """
    Create a 14-day Developer trial for an MCP user.

    Args:
        email: Developer's email
        platform: MCP platform (claude, cursor, copilot, etc.)
        get_db: DB connection factory

    Returns:
        Dict with api_key, expires_at, and setup instructions
    """
    import hashlib
    import secrets

    api_key = f"dchub_trial_{secrets.token_hex(16)}"
    expires_at = datetime.now(timezone.utc) + __import__('datetime').timedelta(days=14)

    if get_db:
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()

            # Check if email already has a trial
            cur.execute("SELECT key FROM api_keys WHERE email = %s AND rate_limit_tier = 'trial'", (email,))
            existing = cur.fetchone()
            if existing:
                return {
                    'success': False,
                    'error': 'trial_already_exists',
                    'message': f'A trial already exists for {email}. Check your email for the API key.',
                    'existing_key': existing[0][:8] + '...',
                }

            cur.execute("""
                INSERT INTO api_keys (key, email, rate_limit_tier, daily_limit, is_active, trial_expires_at, created_at)
                VALUES (%s, %s, 'trial', 1000, true, %s, NOW())
            """, (api_key, email, expires_at))

            # Log the registration
            cur.execute("""
                INSERT INTO platform_health_metrics (metric_category, metric_name, metric_value)
                VALUES ('mcp_trial', %s, %s)
            """, (platform, email))

            conn.commit()

        except Exception as e:
            logger.error(f"Trial creation error: {e}")
            if conn:
                conn.rollback()
            return {'success': False, 'error': str(e)}
        finally:
            if conn:
                conn.close()

    return {
        'success': True,
        'api_key': api_key,
        'tier': 'trial',
        'daily_limit': 1000,
        'expires_at': expires_at.isoformat(),
        'days_remaining': 14,
        'setup': {
            'claude_desktop': f'Add to claude_desktop_config.json: "env": {{"DCHUB_API_KEY": "{api_key}"}}',
            'cursor': f'Add to .cursor/mcp.json: "env": {{"DCHUB_API_KEY": "{api_key}"}}',
            'generic': f'Set environment variable: DCHUB_API_KEY={api_key}',
        },
        'message': (
            f"Your 14-day DC Hub Developer trial is active! "
            f"1,000 API calls/day with full facility data, coordinates, "
            f"power capacity, and M&A details. "
            f"Trial expires {expires_at.strftime('%B %d, %Y')}."
        ),
    }


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES FOR TRIAL/REGISTRATION
# ─────────────────────────────────────────────────────────────

def register_mcp_trial_routes(app, get_db):
    """
    Register trial signup and developer registration routes.
    Call from main.py: register_mcp_trial_routes(app, get_db)
    """
    from flask import jsonify, request

    @app.route('/api/developer/trial', methods=['POST'])
    def create_trial():
        """Create a 14-day Developer trial."""
        data = request.get_json() or {}
        email = data.get('email', '').strip()
        platform = data.get('platform', 'unknown').strip()

        if not email or '@' not in email:
            return jsonify({'error': 'Valid email required'}), 400

        result = create_developer_trial(email, platform, get_db)
        status = 200 if result.get('success') else 409
        return jsonify(result), status

    @app.route('/api/developer/register', methods=['POST'])
    def register_developer():
        """Register an MCP installation (email capture for funnel)."""
        data = request.get_json() or {}
        email = data.get('email', '').strip()
        platform = data.get('platform', 'unknown')

        if not email or '@' not in email:
            return jsonify({'error': 'Valid email required'}), 400

        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO mcp_registrations (email, platform, registered_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (email) DO UPDATE SET
                    platform = EXCLUDED.platform,
                    last_seen = NOW()
            """, (email, platform))

            # Create registrations table if needed
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mcp_registrations (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(500) UNIQUE,
                    platform VARCHAR(100),
                    registered_at TIMESTAMP DEFAULT NOW(),
                    last_seen TIMESTAMP DEFAULT NOW(),
                    trial_offered BOOLEAN DEFAULT false,
                    converted BOOLEAN DEFAULT false
                )
            """)

            conn.commit()

            return jsonify({
                'success': True,
                'message': 'Registration successful! You now get 50 premium calls free.',
                'premium_calls': 50,
                'trial_available': True,
                'trial_url': TRIAL_SIGNUP_URL,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/developer/usage')
    def developer_usage():
        """Get usage stats for an API key."""
        api_key = request.headers.get('X-API-Key', '')
        if not api_key:
            return jsonify({'error': 'X-API-Key header required'}), 401

        tier = get_tier(api_key, get_db)
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT call_count FROM mcp_usage_tracking
                WHERE identifier = %s AND date = %s
            """, (api_key, today))
            row = cur.fetchone()
            count = row[0] if row else 0

            limit = TIERS.get(tier, {}).get('daily_limit', 10)

            return jsonify({
                'tier': tier,
                'today': today,
                'calls_today': count,
                'daily_limit': limit,
                'calls_remaining': max(0, limit - count),
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            if conn:
                conn.close()

    logger.info("✅ MCP trial routes registered")
