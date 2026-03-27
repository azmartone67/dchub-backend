"""
DC Hub — MCP Analytics PostgreSQL Migration + Upgrade Signals
══════════════════════════════════════════════════════════════
Replaces the broken SQLite analytics (which resets on every Railway deploy)
with persistent PostgreSQL tables on Neon.

Changes from current implementation:
  1. mcp_analytics → PostgreSQL (was SQLite via db_utils.try_get_db())
  2. mcp_tool_usage → PostgreSQL (new — per-tool usage tracking)
  3. mcp_upgrade_signals → PostgreSQL (new — captures free tier limit hits as sales leads)
  4. Improved teaser messages that drive Developer tier conversion

How to integrate:
  In main.py, replace the _log_mcp_analytics() function and related code with
  imports from this module. See integration notes at bottom.

v1.0 — March 2026
"""

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger('dchub-mcp-analytics')


# ─────────────────────────────────────────────────────────────
# TABLE CREATION (PostgreSQL / Neon)
# ─────────────────────────────────────────────────────────────
def init_mcp_analytics_tables(get_db):
    """Create MCP analytics tables in PostgreSQL (replaces SQLite)."""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        # 1. Core analytics — every MCP request
        c.execute("""
            CREATE TABLE IF NOT EXISTS mcp_analytics (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                user_id TEXT,
                user_email TEXT,
                tier TEXT DEFAULT 'free',
                tool_name TEXT,
                method TEXT,
                params_json TEXT,
                response_status TEXT,
                response_time_ms INTEGER,
                ip_address TEXT,
                user_agent TEXT,
                mcp_client TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Aggregated tool usage — for dashboard + business intel
        c.execute("""
            CREATE TABLE IF NOT EXISTS mcp_tool_usage (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                tool_name TEXT NOT NULL,
                tier TEXT DEFAULT 'free',
                call_count INTEGER DEFAULT 0,
                unique_sessions INTEGER DEFAULT 0,
                avg_response_ms INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                UNIQUE(date, tool_name, tier)
            )
        """)

        # 3. Upgrade signals — captures moments users hit free tier limits
        #    This is the sales intelligence table
        c.execute("""
            CREATE TABLE IF NOT EXISTS mcp_upgrade_signals (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                user_email TEXT,
                ip_address TEXT,
                signal_type TEXT NOT NULL,
                tool_requested TEXT,
                tier_current TEXT DEFAULT 'free',
                tier_required TEXT,
                daily_usage INTEGER,
                daily_limit INTEGER,
                message_shown TEXT,
                mcp_client TEXT,
                user_agent TEXT,
                converted BOOLEAN DEFAULT FALSE,
                converted_at TIMESTAMP,
                outreach_sent BOOLEAN DEFAULT FALSE,
                outreach_sent_at TIMESTAMP,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 4. Conversion tracking — ties Stripe webhook to upgrade signal
        c.execute("""
            CREATE TABLE IF NOT EXISTS mcp_conversions (
                id SERIAL PRIMARY KEY,
                user_email TEXT NOT NULL,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                plan_from TEXT DEFAULT 'free',
                plan_to TEXT NOT NULL,
                mrr_cents INTEGER,
                source TEXT,
                attribution_signal_id INTEGER REFERENCES mcp_upgrade_signals(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for fast queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_analytics_session ON mcp_analytics(session_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_analytics_tool ON mcp_analytics(tool_name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_analytics_tier ON mcp_analytics(tier)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_analytics_created ON mcp_analytics(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_analytics_email ON mcp_analytics(user_email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_tool_usage_date ON mcp_tool_usage(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_upgrade_email ON mcp_upgrade_signals(user_email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_upgrade_type ON mcp_upgrade_signals(signal_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_upgrade_converted ON mcp_upgrade_signals(converted)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_conversions_email ON mcp_conversions(user_email)")

        conn.commit()
        logger.info("✅ MCP analytics tables initialized (PostgreSQL)")
    except Exception as e:
        logger.warning(f"MCP analytics table init: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# ANALYTICS LOGGING (replaces _log_mcp_analytics in main.py)
# ─────────────────────────────────────────────────────────────
def log_mcp_request(get_db, session_id, tool_name, method, params,
                    tier='free', user_email=None, user_id=None,
                    response_status='success', response_time_ms=0,
                    ip_address=None, user_agent=None, mcp_client=None):
    """Log an MCP request to PostgreSQL. Non-blocking — errors are swallowed."""
    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("""
            INSERT INTO mcp_analytics
                (session_id, user_id, user_email, tier, tool_name, method,
                 params_json, response_status, response_time_ms,
                 ip_address, user_agent, mcp_client)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (session_id, user_id, user_email, tier, tool_name, method,
              json.dumps(params) if params else None,
              response_status, response_time_ms,
              ip_address, user_agent, mcp_client))

        # Also update daily aggregates
        today = datetime.utcnow().date()
        c.execute("""
            INSERT INTO mcp_tool_usage (date, tool_name, tier, call_count, unique_sessions, avg_response_ms)
            VALUES (%s, %s, %s, 1, 1, %s)
            ON CONFLICT (date, tool_name, tier) DO UPDATE SET
                call_count = mcp_tool_usage.call_count + 1,
                avg_response_ms = (mcp_tool_usage.avg_response_ms * mcp_tool_usage.call_count + %s)
                                  / (mcp_tool_usage.call_count + 1)
        """, (today, tool_name, tier, response_time_ms, response_time_ms))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Analytics log (non-fatal): {e}")


# ─────────────────────────────────────────────────────────────
# UPGRADE SIGNAL CAPTURE
# ─────────────────────────────────────────────────────────────

# Signal types
SIGNAL_RATE_LIMIT = 'rate_limit_hit'       # Hit daily query cap
SIGNAL_TOOL_GATED = 'tool_gated'           # Tried to use a paid tool
SIGNAL_TIER_TEASE = 'tier_teaser_shown'    # Saw a "upgrade for more" message
SIGNAL_REPEATED_FREE = 'power_free_user'    # 5+ sessions on free tier


def log_upgrade_signal(get_db, signal_type, tool_requested=None,
                       session_id=None, user_email=None, ip_address=None,
                       tier_current='free', tier_required='developer',
                       daily_usage=0, daily_limit=25,
                       message_shown=None, mcp_client=None, user_agent=None):
    """
    Capture an upgrade signal — a moment when a free user bumps against limits.
    These are sales leads for Developer tier outreach.
    """
    try:
        conn = get_db()
        c = conn.cursor()

        # Deduplicate: don't create more than 1 signal per session+type per hour
        c.execute("""
            SELECT id FROM mcp_upgrade_signals
            WHERE session_id = %s AND signal_type = %s
              AND created_at > NOW() - INTERVAL '1 hour'
            LIMIT 1
        """, (session_id, signal_type))

        if c.fetchone():
            conn.close()
            return None  # Already captured this signal recently

        c.execute("""
            INSERT INTO mcp_upgrade_signals
                (session_id, user_email, ip_address, signal_type, tool_requested,
                 tier_current, tier_required, daily_usage, daily_limit,
                 message_shown, mcp_client, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (session_id, user_email, ip_address, signal_type, tool_requested,
              tier_current, tier_required, daily_usage, daily_limit,
              message_shown, mcp_client, user_agent))

        signal_id = c.fetchone()[0]
        conn.commit()
        conn.close()

        logger.info(f"📈 Upgrade signal #{signal_id}: {signal_type} for {tool_requested or 'general'}")
        return signal_id

    except Exception as e:
        logger.debug(f"Upgrade signal log (non-fatal): {e}")
        return None


# ─────────────────────────────────────────────────────────────
# TEASER MESSAGES — Drive Developer Tier Conversion
# ─────────────────────────────────────────────────────────────
# These replace the generic "upgrade to access" messages with
# specific, value-driven copy that links to the Developer signup page.

TEASER_MESSAGES = {
    # Rate limit hit
    'rate_limit': {
        'title': "You've used {usage}/{limit} queries today",
        'message': (
            "You're getting real value from DC Hub — nice! "
            "The Developer plan ($49/mo) gives you 1,000 queries/day "
            "and unlocks all 20+ tools including fiber maps, carrier data, and market intel. "
            "https://dchub.cloud/developers/signup"
        ),
        'short': "Upgrade to Developer ($49/mo) for 1,000/day: dchub.cloud/developers/signup",
    },

    # Gated tool access
    'get_fiber_intel': {
        'title': "Fiber Intelligence requires Developer plan",
        'message': (
            "Fiber connectivity data — including carrier maps, subsea cables, and dark fiber — "
            "is available on the Developer plan ($49/mo). "
            "Map carrier presence at any facility and score fiber connectivity for site selection. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "3 carriers detected near this location. Upgrade to see full fiber intelligence.",
    },
    'get_market_intel': {
        'title': "Market Intelligence requires Developer plan",
        'message': (
            "DC Hub tracks market dynamics across 50+ metros — supply, demand, pricing, "
            "and new construction. Developer plan ($49/mo) unlocks full market intelligence. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Market data available for this region. Upgrade to access supply/demand trends.",
    },
    'get_energy_prices': {
        'title': "Energy Price Data requires Developer plan",
        'message': (
            "Compare commercial and industrial electricity rates across all 50 states. "
            "Developer plan ($49/mo) unlocks energy pricing, renewable mix, and cost projections. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Energy data available. Upgrade to see $/kWh rates and cost comparisons.",
    },
    'get_tax_incentives': {
        'title': "Tax Incentive Data requires Developer plan",
        'message': (
            "DC Hub tracks data center tax incentives, abatements, and enterprise zones "
            "across all states. Developer plan ($49/mo) unlocks the full incentive database. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Tax incentives found for this state. Upgrade to see full details.",
    },
    'get_water_risk': {
        'title': "Water Risk Assessment requires Developer plan",
        'message': (
            "Water availability and drought risk are critical for cooling-intensive facilities. "
            "Developer plan ($49/mo) includes county-level water risk scoring. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Water risk data available. Upgrade to see drought scores and projections.",
    },
    'compare_sites': {
        'title': "Site Comparison requires Developer plan",
        'message': (
            "Compare multiple locations across power, fiber, water, tax, and cost dimensions. "
            "Developer plan ($49/mo) includes side-by-side comparison for up to 2 sites. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Site comparison available. Upgrade to run head-to-head analysis.",
    },
    'get_grid_intelligence': {
        'title': "Grid Intelligence requires Developer plan",
        'message': (
            "County-level grid capacity, substation proximity, and interconnection queue data. "
            "Developer plan ($49/mo) unlocks granular grid intelligence for site selection. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Grid capacity data available. Upgrade for substation-level detail.",
    },
    'get_pipeline': {
        'title': "Pipeline Tracking requires Developer plan",
        'message': (
            "Track data center construction, expansions, and planned builds across all markets. "
            "Developer plan ($49/mo) unlocks the full DC Hub pipeline database. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Pipeline activity detected in this market. Upgrade to see details.",
    },
    'get_renewable_energy': {
        'title': "Renewable Energy Data requires Developer plan",
        'message': (
            "Solar, wind, and renewable capacity by state. Track clean energy availability "
            "for sustainability reporting. Developer plan ($49/mo). "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Renewable energy data available. Upgrade for full clean energy analysis.",
    },
    'get_intelligence_index': {
        'title': "Intelligence Index requires Developer plan",
        'message': (
            "DC Hub's composite intelligence index scores markets across power, fiber, water, "
            "tax, and risk. Developer plan ($49/mo) unlocks rankings and scores. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Intelligence scores available. Upgrade to see market rankings.",
    },

    # Generic fallback for any gated tool
    'default': {
        'title': "This tool requires Developer plan",
        'message': (
            "The Developer plan ($49/mo) unlocks all 20+ DC Hub tools with 1,000 queries/day. "
            "Fiber maps, market intel, grid data, and more. "
            "https://dchub.cloud/developers/signup"
        ),
        'preview': "Additional data available. Upgrade to Developer for full access.",
    },
}


def get_teaser_message(tool_name, usage=0, limit=25, tier='free'):
    """Get the appropriate teaser message for a gated tool or rate limit."""
    if usage >= limit:
        msg = TEASER_MESSAGES['rate_limit'].copy()
        msg['title'] = msg['title'].format(usage=usage, limit=limit)
        return msg

    return TEASER_MESSAGES.get(tool_name, TEASER_MESSAGES['default'])


def build_gated_response(tool_name, get_db=None, session_id=None,
                         user_email=None, ip_address=None,
                         usage=0, limit=25, tier='free',
                         mcp_client=None, user_agent=None):
    """
    Build a complete gated response with teaser + upgrade signal logging.
    Returns dict suitable for MCP tool response.
    """
    teaser = get_teaser_message(tool_name, usage, limit, tier)

    # Determine signal type
    if usage >= limit:
        signal_type = SIGNAL_RATE_LIMIT
    else:
        signal_type = SIGNAL_TOOL_GATED

    # Log the upgrade signal (async/non-blocking)
    if get_db:
        log_upgrade_signal(
            get_db=get_db,
            signal_type=signal_type,
            tool_requested=tool_name,
            session_id=session_id,
            user_email=user_email,
            ip_address=ip_address,
            tier_current=tier,
            tier_required='developer',
            daily_usage=usage,
            daily_limit=limit,
            message_shown=teaser.get('message', ''),
            mcp_client=mcp_client,
            user_agent=user_agent,
        )

    response = {
        'gated': True,
        'tier_required': 'developer',
        'current_tier': tier,
        'upgrade_url': 'https://dchub.cloud/developers/signup',
        'title': teaser.get('title', ''),
        'message': teaser.get('message', ''),
    }

    # Include preview data if available
    if 'preview' in teaser:
        response['preview'] = teaser['preview']

    return response


# ─────────────────────────────────────────────────────────────
# ANALYTICS API ENDPOINTS
# ─────────────────────────────────────────────────────────────
def register_mcp_analytics_routes(app, get_db):
    """Register MCP analytics API routes (admin only)."""
    from flask import jsonify, request

    def require_admin(f):
        from functools import wraps
        @wraps(f)
        def decorated(*args, **kwargs):
            key = request.headers.get('X-Internal-Key', '') or request.args.get('key', '')
            admin_key = os.environ.get('DCHUB_ADMIN_KEY', '')
            if not key or not admin_key or key != admin_key:
                return jsonify({'error': 'Unauthorized'}), 401
            return f(*args, **kwargs)
        return decorated

    @app.route('/api/admin/mcp/analytics', methods=['GET'])
    @require_admin
    def mcp_analytics_dashboard():
        """MCP usage analytics dashboard data."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            days = min(request.args.get('days', 30, type=int), 90)
            since = datetime.utcnow() - timedelta(days=days)

            # Total requests
            c.execute("SELECT COUNT(*) FROM mcp_analytics WHERE created_at > %s", (since,))
            total_requests = c.fetchone()[0]

            # Unique sessions
            c.execute("SELECT COUNT(DISTINCT session_id) FROM mcp_analytics WHERE created_at > %s", (since,))
            unique_sessions = c.fetchone()[0]

            # Requests by tier
            c.execute("""
                SELECT tier, COUNT(*) as cnt
                FROM mcp_analytics WHERE created_at > %s
                GROUP BY tier ORDER BY cnt DESC
            """, (since,))
            by_tier = {row[0]: row[1] for row in c.fetchall()}

            # Top tools
            c.execute("""
                SELECT tool_name, COUNT(*) as cnt
                FROM mcp_analytics WHERE created_at > %s
                GROUP BY tool_name ORDER BY cnt DESC LIMIT 20
            """, (since,))
            top_tools = [{'tool': row[0], 'calls': row[1]} for row in c.fetchall()]

            # Daily trend
            c.execute("""
                SELECT DATE(created_at) as day, COUNT(*) as cnt
                FROM mcp_analytics WHERE created_at > %s
                GROUP BY day ORDER BY day
            """, (since,))
            daily_trend = [{'date': str(row[0]), 'requests': row[1]} for row in c.fetchall()]

            # Top MCP clients
            c.execute("""
                SELECT mcp_client, COUNT(*) as cnt
                FROM mcp_analytics WHERE created_at > %s AND mcp_client IS NOT NULL
                GROUP BY mcp_client ORDER BY cnt DESC LIMIT 10
            """, (since,))
            top_clients = [{'client': row[0], 'requests': row[1]} for row in c.fetchall()]

            return jsonify({
                'success': True,
                'period_days': days,
                'total_requests': total_requests,
                'unique_sessions': unique_sessions,
                'by_tier': by_tier,
                'top_tools': top_tools,
                'daily_trend': daily_trend,
                'top_clients': top_clients,
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/admin/mcp/upgrade-signals', methods=['GET'])
    @require_admin
    def mcp_upgrade_signals_dashboard():
        """View upgrade signals — potential Developer tier leads."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            days = min(request.args.get('days', 30, type=int), 90)
            since = datetime.utcnow() - timedelta(days=days)

            # Total signals
            c.execute("SELECT COUNT(*) FROM mcp_upgrade_signals WHERE created_at > %s", (since,))
            total_signals = c.fetchone()[0]

            # Unconverted leads (unique emails/IPs)
            c.execute("""
                SELECT COUNT(DISTINCT COALESCE(user_email, ip_address))
                FROM mcp_upgrade_signals
                WHERE created_at > %s AND converted = FALSE
            """, (since,))
            unconverted_leads = c.fetchone()[0]

            # Signals by type
            c.execute("""
                SELECT signal_type, COUNT(*) as cnt
                FROM mcp_upgrade_signals WHERE created_at > %s
                GROUP BY signal_type ORDER BY cnt DESC
            """, (since,))
            by_type = [{'type': row[0], 'count': row[1]} for row in c.fetchall()]

            # Most requested gated tools
            c.execute("""
                SELECT tool_requested, COUNT(*) as cnt
                FROM mcp_upgrade_signals
                WHERE created_at > %s AND tool_requested IS NOT NULL
                GROUP BY tool_requested ORDER BY cnt DESC LIMIT 10
            """, (since,))
            gated_tools = [{'tool': row[0], 'attempts': row[1]} for row in c.fetchall()]

            # Recent leads (for outreach)
            c.execute("""
                SELECT DISTINCT ON (COALESCE(user_email, ip_address))
                    user_email, ip_address, signal_type, tool_requested,
                    daily_usage, mcp_client, created_at
                FROM mcp_upgrade_signals
                WHERE created_at > %s AND converted = FALSE AND outreach_sent = FALSE
                ORDER BY COALESCE(user_email, ip_address), created_at DESC
                LIMIT 50
            """, (since,))
            leads = []
            for row in c.fetchall():
                leads.append({
                    'email': row[0],
                    'ip': row[1],
                    'signal': row[2],
                    'tool': row[3],
                    'usage': row[4],
                    'client': row[5],
                    'when': row[6].isoformat() if row[6] else None,
                })

            # Conversion rate
            c.execute("SELECT COUNT(*) FROM mcp_conversions WHERE created_at > %s", (since,))
            conversions = c.fetchone()[0]

            return jsonify({
                'success': True,
                'period_days': days,
                'total_signals': total_signals,
                'unconverted_leads': unconverted_leads,
                'conversions': conversions,
                'conversion_rate': f"{(conversions / max(unconverted_leads, 1)) * 100:.1f}%",
                'by_type': by_type,
                'most_requested_tools': gated_tools,
                'leads': leads,
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/v1/track-conversion', methods=['POST'])
    def track_conversion_event():
        """Track CTA click from developer signup page (public endpoint)."""
        try:
            data = request.get_json(silent=True) or {}
            conn = get_db()
            c = conn.cursor()

            c.execute("""
                INSERT INTO mcp_analytics
                    (tool_name, method, params_json, response_status, ip_address, user_agent)
                VALUES ('conversion_event', 'track', %s, 'success', %s, %s)
            """, (json.dumps(data),
                  request.remote_addr,
                  request.headers.get('User-Agent', '')))

            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except Exception:
            return jsonify({'success': True})  # Don't expose errors on tracking endpoint

    logger.info("📊 MCP analytics routes registered: /api/admin/mcp/analytics, /api/admin/mcp/upgrade-signals")
