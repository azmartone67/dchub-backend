
# ============================================================================
# Phase 32D — MANUAL INTEGRATION NEEDED
# ============================================================================
# When building post text, append the daily landing URL so LinkedIn renders
# the rich card preview:
#
#     text += "\n\n" + _phase30c_landing_url()  # phase32_landing_appended
#
# Place this RIGHT BEFORE the call that publishes the post (e.g.
# linkedin.publish(text=text), client.share(message=text), etc.)
# ============================================================================
"""
DC Hub — LinkedIn Auto-Poster
===============================
Handles OAuth 2.0, token management, and automated posting
to the DC Hub LinkedIn company page.

Setup:
  1. Set Railway env vars: LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET
  2. Set LINKEDIN_COMPANY_ID (your LinkedIn company page URN number)
  3. Add redirect URL in LinkedIn app: https://dchub.cloud/api/linkedin/callback
  4. Deploy, then visit https://dchub.cloud/api/linkedin/auth to authorize once
  5. Posts auto-trigger weekly, or manually via /api/linkedin/post

Integration in main.py:
  from linkedin_poster import register_linkedin_routes
  register_linkedin_routes(app)

Endpoints:
  GET  /api/linkedin/auth       — Start OAuth flow (visit once to authorize)
  GET  /api/linkedin/callback   — OAuth callback (LinkedIn redirects here)
  POST /api/linkedin/post       — Manually trigger a post (admin-only)
  GET  /api/linkedin/status     — Check token status and last post
  POST /api/linkedin/auto-post  — Trigger the weekly auto-post

Tables (created automatically):
  linkedin_tokens   — OAuth tokens (access_token, refresh_token, expiry)
  linkedin_posts    — Post history log
"""

import os
import json
import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# Phase 30C — daily landing URL (LinkedIn renders rich card from this URL's OG)
def _phase30c_landing_url(d=None):
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"  # phase31_canonical_url

logger = logging.getLogger('linkedin')

# ── Config ───────────────────────────────────────────────────
LINKEDIN_CLIENT_ID     = os.environ.get('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = os.environ.get('LINKEDIN_CLIENT_SECRET', '')
LINKEDIN_COMPANY_ID    = os.environ.get('LINKEDIN_COMPANY_ID', '')
LINKEDIN_REDIRECT_URI  = os.environ.get('LINKEDIN_REDIRECT_URI', 'https://dchub.cloud/api/linkedin/callback')
LINKEDIN_SCOPES        = 'openid profile w_member_social w_organization_social'
# Direct token from Railway env — used as fallback if DB has no token yet
LINKEDIN_ACCESS_TOKEN_ENV = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')

# LinkedIn API endpoints
AUTH_URL      = 'https://www.linkedin.com/oauth/v2/authorization'
TOKEN_URL     = 'https://www.linkedin.com/oauth/v2/accessToken'
POSTS_URL     = 'https://api.linkedin.com/rest/posts'
USERINFO_URL  = 'https://api.linkedin.com/v2/userinfo'

# Admin key for protected endpoints (set in Railway env)
ADMIN_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')


# ── Database helpers ─────────────────────────────────────────

def _get_conn():
    import psycopg2
    db_url = os.environ.get('NEON_DATABASE_URL', '') or os.environ.get('DATABASE_URL', '')
    return psycopg2.connect(db_url, connect_timeout=10)


def _execute(sql, params=None, fetch=False, fetchall=False):
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


def _ensure_tables():
    """Create LinkedIn tables if they don't exist."""
    try:
        _execute("""
            CREATE TABLE IF NOT EXISTS linkedin_tokens (
                id INTEGER PRIMARY KEY DEFAULT 1,
                access_token TEXT,
                refresh_token TEXT,
                expires_at TIMESTAMPTZ,
                member_urn TEXT,
                company_urn TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT single_row CHECK (id = 1)
            )
        """)
        _execute("""
            CREATE TABLE IF NOT EXISTS linkedin_posts (
                id SERIAL PRIMARY KEY,
                post_urn TEXT,
                content TEXT,
                post_type TEXT DEFAULT 'manual',
                status TEXT DEFAULT 'success',
                error TEXT,
                posted_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        logger.info("[LinkedIn] ✅ Tables verified/created")
    except Exception as e:
        logger.error(f"[LinkedIn] Table creation error: {e}")


# ── Token Management ─────────────────────────────────────────

def _get_token():
    """Get the stored access token, or None if expired/missing."""
    row = _execute("""
        SELECT access_token, refresh_token, expires_at, member_urn, company_urn
        FROM linkedin_tokens WHERE id = 1
    """, fetch=True)
    if not row:
        return None
    return row


def _save_token(access_token, refresh_token=None, expires_in=5184000, member_urn=None):
    """Save or update the OAuth token in Neon."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    company_urn = f"urn:li:organization:{LINKEDIN_COMPANY_ID}" if LINKEDIN_COMPANY_ID else None

    _execute("""
        INSERT INTO linkedin_tokens (id, access_token, refresh_token, expires_at, member_urn, company_urn, updated_at)
        VALUES (1, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
        ON CONFLICT (id) DO UPDATE SET
            access_token = %s,
            refresh_token = COALESCE(%s, linkedin_tokens.refresh_token),
            expires_at = %s,
            member_urn = COALESCE(%s, linkedin_tokens.member_urn),
            company_urn = COALESCE(%s, linkedin_tokens.company_urn),
            updated_at = NOW()
    """, (access_token, refresh_token, expires_at, member_urn, company_urn,
          access_token, refresh_token, expires_at, member_urn, company_urn))

    logger.info(f"[LinkedIn] Token saved, expires {expires_at.isoformat()}")


def _get_valid_token():
    """Get a valid access token, refreshing if needed.
    Falls back to LINKEDIN_ACCESS_TOKEN env var if DB has no token yet.
    """
    import requests as req

    token = _get_token()
    if not token or not token.get('access_token'):
        # Fallback: use token from Railway env var (seed it into DB for next time)
        if LINKEDIN_ACCESS_TOKEN_ENV:
            logger.info("[LinkedIn] No DB token found — using LINKEDIN_ACCESS_TOKEN env var")
            try:
                _save_token(LINKEDIN_ACCESS_TOKEN_ENV, expires_in=5184000)
            except Exception as e:
                logger.warning(f"[LinkedIn] Could not seed env token to DB: {e}")
            return LINKEDIN_ACCESS_TOKEN_ENV
        return None

    # Check expiry (refresh if within 24h of expiring)
    expires_at = token.get('expires_at')
    if expires_at and expires_at < datetime.now(timezone.utc) + timedelta(hours=24):
        # Try refresh
        refresh = token.get('refresh_token')
        if refresh:
            try:
                resp = req.post(TOKEN_URL, data={
                    'grant_type': 'refresh_token',
                    'refresh_token': refresh,
                    'client_id': LINKEDIN_CLIENT_ID,
                    'client_secret': LINKEDIN_CLIENT_SECRET,
                })
                if resp.status_code == 200:
                    data = resp.json()
                    _save_token(
                        access_token=data['access_token'],
                        refresh_token=data.get('refresh_token', refresh),
                        expires_in=data.get('expires_in', 5184000),
                    )
                    return data['access_token']
                else:
                    logger.error(f"[LinkedIn] Token refresh failed: {resp.text}")
                    return None
            except Exception as e:
                logger.error(f"[LinkedIn] Token refresh error: {e}")
                return None
        else:
            logger.error("[LinkedIn] Token expired and no refresh token available")
            return None

    return token['access_token']


# ── Posting ──────────────────────────────────────────────────

def post_to_linkedin(text, link_url=None, link_title=None, link_desc=None, image_bytes=None):

    # Phase 194: optional image upload (UGC POST media asset flow)
    _image_urn = None
    if image_bytes is not None:
        try:
            import os, requests
            _token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
            _company = os.environ.get("LINKEDIN_COMPANY_ID", "").strip()
            if _token and _company:
                _author = f"urn:li:organization:{_company}"
                _h = {"Authorization": f"Bearer {_token}", "X-Restli-Protocol-Version": "2.0.0"}
                # Register upload
                _reg = requests.post(
                    "https://api.linkedin.com/v2/assets?action=registerUpload",
                    headers={**_h, "Content-Type": "application/json"},
                    json={"registerUploadRequest": {
                        "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                        "owner": _author,
                        "serviceRelationships": [{"relationshipType":"OWNER","identifier":"urn:li:userGeneratedContent"}],
                    }},
                    timeout=15,
                )
                if _reg.status_code in (200, 201):
                    _rj = _reg.json()["value"]
                    _upload_url = _rj["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
                    _image_urn = _rj["asset"]
                    requests.put(_upload_url, headers={"Authorization": f"Bearer {_token}"}, data=image_bytes, timeout=30)
        except Exception as _e:
            import logging as _l; _l.getLogger("linkedin").warning(f"phase194 image upload err: {_e}")

    """Post content to the DC Hub LinkedIn company page.
    
    Returns: (success: bool, result: dict)
    """
    import requests as req

    access_token = _get_valid_token()
    if not access_token:
        return False, {'error': 'No valid LinkedIn token. Visit /api/linkedin/auth to authorize.'}

    # Determine author URN — company page or personal
    token_data = _get_token()
    if LINKEDIN_COMPANY_ID:
        author = f"urn:li:organization:{LINKEDIN_COMPANY_ID}"
    elif token_data and token_data.get('member_urn'):
        author = token_data['member_urn']
    else:
        return False, {'error': 'No company ID or member URN configured.'}

    # Build the post payload
    payload = {
        "author": author,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED",
    }

    # Add article attachment if link provided
    if link_url:
        payload["content"] = {
            "article": {
                "source": link_url,
                "title": link_title or "DC Hub",
                "description": link_desc or "Data Center Intelligence for the AI Era"
            }
        }

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
        'LinkedIn-Version': '202601',
    }

    try:
        resp = req.post(POSTS_URL, json=payload, headers=headers)

        if resp.status_code in (200, 201):
            post_urn = resp.headers.get('x-restli-id', resp.headers.get('X-LinkedIn-Id', ''))
            # Log success
            _execute("""
                INSERT INTO linkedin_posts (post_urn, content, post_type, status)
                VALUES (%s, %s, %s, 'success') ON CONFLICT DO NOTHING
            """, (post_urn, text[:500], 'manual'))
            return True, {'post_urn': post_urn, 'status': 'published'}
        else:
            error_msg = resp.text[:500]
            # Log failure
            _execute("""
                INSERT INTO linkedin_posts (content, post_type, status, error)
                VALUES (%s, %s, 'failed', %s) ON CONFLICT DO NOTHING
            """, (text[:500], 'manual', error_msg))
            return False, {'error': error_msg, 'status_code': resp.status_code}

    except Exception as e:
        error_msg = str(e)
        _execute("""
            INSERT INTO linkedin_posts (content, post_type, status, error)
            VALUES (%s, 'manual', 'failed', %s) ON CONFLICT DO NOTHING
        """, (text[:500], error_msg))
        return False, {'error': error_msg}


def generate_weekly_post():
    """Generate a weekly LinkedIn post using live AI analytics data."""
    try:
        from ai_weekly_digest import generate_weekly_digest
        digest = generate_weekly_digest()
    except Exception as e:
        logger.error(f"[LinkedIn] Could not generate digest: {e}")
        return None, None, None

    s = digest.get('summary', {})
    platforms = digest.get('platforms', [])

    total_week = s.get('ai_requests_this_week', 0)
    total_all = s.get('total_all_time', 0)
    wow = s.get('wow_change_pct', 0)
    active = s.get('active_ai_platforms', 0)
    mcp = s.get('mcp_requests', 0)

    top_platforms = [p['platform'] for p in platforms[:5] if p['this_week'] > 0]
    platform_str = ', '.join(top_platforms) if top_platforms else 'multiple AI platforms'

    wow_msg = f"That's up {wow:.0f}% from last week. " if wow > 0 else ""

    highlights = digest.get('highlights', [])
    highlight_str = ""
    if highlights:
        highlight_str = "\n\n" + "\n".join(f"→ {h}" for h in highlights[:3])

    post_text = f"""This week on DC Hub: {total_week:,} AI agent requests across {active} platforms.

{wow_msg}{platform_str} — all pulling live data center intelligence from our 20,000+ facility database.{highlight_str}

MCP developer integrations: {mcp:,} requests this week.

All-time total: {total_all:,} AI agent requests.

See it live → dchub.cloud/ai-analytics

#DataCenters #AI #MCP #DataCenterIntelligence"""

    return post_text, 'https://dchub.cloud/ai-analytics', 'AI Agent Analytics — DC Hub'


def generate_deals_post():
    """Generate a LinkedIn post about recent M&A deals."""
    import requests as req
    try:
        # Phase ZZZZZ-round5 (2026-05-23): '%s'→'?' URL bug — was hitting
        # /api/v1/deals%slimit=5 (literal '%s') which 404'd. Fixed.
        resp = req.get('https://dchub.cloud/api/v1/deals?limit=5', timeout=15)
        data = resp.json()
        deals = data.get('deals', data.get('data', []))
        if not deals:
            return None, None, None
    except Exception as e:
        logger.error(f"[LinkedIn] Could not fetch deals: {e}")
        return None, None, None

    items = deals[:3]
    deal_lines = []
    for d in items:
        title = d.get('title', d.get('name', 'Deal'))
        buyer = d.get('buyer', '')
        value = d.get('value', d.get('price', ''))
        line = f"→ {title}"
        if buyer:
            line += f" — {buyer}"
        if value:
            line += f" ({value})"
        deal_lines.append(line)

    total_tracked = data.get('total', data.get('total_count', '975+'))

    post_text = f"""Latest data center M&A activity tracked by DC Hub:

{chr(10).join(deal_lines)}

DC Hub now tracks {total_tracked} data center transactions worth $324B+ — the most comprehensive M&A database in the industry.

Explore all deals → dchub.cloud/transactions

#DataCenters #MergersAndAcquisitions #Infrastructure #RealEstate"""

    return post_text, 'https://dchub.cloud/transactions', 'Data Center M&A Tracker — DC Hub'


def generate_news_post():
    """Generate a LinkedIn post about latest industry news."""
    import requests as req
    try:
        # Phase ZZZZZ-round5 (2026-05-23): '%s'→'?' URL bug. Same root
        # cause as the /api/v1/deals fix above.
        resp = req.get('https://dchub.cloud/api/news?limit=5', timeout=15)
        data = resp.json()
        articles = data.get('articles', data.get('data', []))
        if not articles:
            return None, None, None
    except Exception as e:
        logger.error(f"[LinkedIn] Could not fetch news: {e}")
        return None, None, None

    items = articles[:3]
    news_lines = []
    for a in items:
        title = a.get('title', a.get('headline', 'Article'))
        source = a.get('source', '')
        line = f"→ {title}"
        if source:
            line += f" ({source})"
        news_lines.append(line)

    post_text = f"""What's happening in data centers right now:

{chr(10).join(news_lines)}

DC Hub aggregates news from 30+ industry sources, updated every 3 minutes. Stay ahead of the market.

Read more → dchub.cloud/news

#DataCenters #InfrastructureNews #CloudComputing #DigitalInfrastructure"""

    return post_text, 'https://dchub.cloud/news', 'Data Center News — DC Hub'


def generate_market_post():
    """Generate a LinkedIn post about market intelligence."""
    import requests as req
    markets = ['Northern Virginia', 'Dallas', 'Phoenix', 'Chicago', 'Atlanta']
    import random
    market = random.choice(markets)

    try:
        resp = req.get(f'https://dchub.cloud/api/market-report%smarket={market.replace(" ", "+")}', timeout=15)
        data = resp.json()
        m = data.get('market', data.get('data', data))
    except Exception as e:
        logger.error(f"[LinkedIn] Could not fetch market data: {e}")
        return None, None, None

    stats = []
    if m.get('vacancy_rate') is not None:
        stats.append(f"→ Vacancy rate: {m['vacancy_rate']}%")
    if m.get('total_mw') or m.get('inventory'):
        stats.append(f"→ Total inventory: {m.get('total_mw', m.get('inventory'))} MW")
    if m.get('facilities_count'):
        stats.append(f"→ Tracked facilities: {m['facilities_count']}")
    if m.get('avg_price') or m.get('pricing'):
        stats.append(f"→ Avg pricing: {m.get('avg_price', m.get('pricing'))}")

    if not stats:
        stats.append(f"→ Market tracked with real-time data")

    post_text = f"""Market spotlight: {market}

{chr(10).join(stats)}

DC Hub tracks vacancy, pricing, inventory, and absorption across 35+ major markets worldwide. Real data for real decisions.

Explore markets → dchub.cloud/market-intelligence

#DataCenters #MarketIntelligence #Colocation #SiteSelection"""

    return post_text, 'https://dchub.cloud/market-intelligence', f'{market} Market Intelligence — DC Hub'


def generate_mcp_post():
    """Generate a LinkedIn post about MCP/developer adoption."""
    try:
        from ai_weekly_digest import generate_weekly_digest
        digest = generate_weekly_digest()
    except Exception as e:
        logger.error(f"[LinkedIn] Could not generate digest for MCP: {e}")
        return None, None, None

    s = digest.get('summary', {})
    mcp = s.get('mcp_requests', 0)
    total_all = s.get('total_all_time', 0)
    active = s.get('active_ai_platforms', 0)

    post_text = f"""AI agents don't Google. They query APIs.

DC Hub's MCP server is now live across Cursor, VS Code, Windsurf, and Claude Desktop — giving AI coding assistants direct access to 20,000+ data center facilities.

This week: {mcp:,} MCP requests from developer tools.
Total AI agent requests: {total_all:,} across {active} platforms.

One JSON config. Zero API keys for the free tier. 100 requests/day.

Set it up in 30 seconds → dchub.cloud/connect

#MCP #AI #DeveloperTools #DataCenters #APIFirst"""

    return post_text, 'https://dchub.cloud/connect', 'Connect to DC Hub MCP Server'


def generate_pipeline_post():
    """Generate a LinkedIn post about new facilities / pipeline."""
    import requests as req
    try:
        resp = req.get('https://dchub.cloud/api/ai/query?type=facilities&limit=3&sort=newest', timeout=15)
        data = resp.json()
        facilities = data.get('data', [])
    except Exception as e:
        logger.error(f"[LinkedIn] Could not fetch facilities: {e}")
        facilities = []

    if facilities:
        fac_lines = []
        for f in facilities[:3]:
            name = f.get('name', f.get('facility_name', 'New Facility'))
            city = f.get('city', '')
            power = f.get('power_mw', f.get('power_capacity', ''))
            line = f"→ {name}"
            if city:
                line += f" — {city}"
            if power:
                line += f" ({power} MW)"
            fac_lines.append(line)
        fac_str = chr(10).join(fac_lines)
    else:
        fac_str = "→ New facilities added daily across 140+ countries"

    post_text = f"""DC Hub's facility database keeps growing.

{fac_str}

Now tracking 20,000+ data center facilities across 140+ countries — the largest independent database in the industry.

Every facility includes location, power capacity, operator, connectivity, and more.

Explore the database → dchub.cloud/assets

#DataCenters #Infrastructure #CloudComputing #DigitalInfrastructure"""

    return post_text, 'https://dchub.cloud/assets', 'Data Center Asset Explorer — DC Hub'


# ── Post Topic Rotation ──────────────────────────────────────

# Mon/Wed/Fri rotation: 6 topics across 3 slots per week = each topic every 2 weeks
POST_SCHEDULE = {
    0: [  # Monday — lead with analytics or deals
        ('ai_analytics', generate_weekly_post),
        ('deals', generate_deals_post),
    ],
    2: [  # Wednesday — mid-week insights
        ('news', generate_news_post),
        ('market_intel', generate_market_post),
    ],
    4: [  # Friday — developer/growth focus
        ('mcp_adoption', generate_mcp_post),
        ('pipeline', generate_pipeline_post),
    ],
}


# Phase ZZZZZ-round4 (2026-05-23): 4-style rotation
# Same topic, 4 different presentation styles — prevents the LinkedIn algo
# from down-ranking us for "repetitive content" and gives the same audience
# a fresh hook every ~6 weeks (4 styles × week-based rotation across 6 topics).
#
# A style is applied as a HEADER (replaces the lead line) and an optional
# FOOTER (appended before the CTA). The body of the post is left intact so
# we don't lose the live data.
POST_STYLES = {
    'data': {
        'header': None,  # No header — the existing posts already lead with numbers
        'footer': None,
    },
    'narrative': {
        'header': "Story of the week:",
        'footer': "That's the kind of momentum a 20,000-facility database surfaces in real time.",
    },
    'listicle': {
        'header': "3 things data-center watchers should know this week:",
        'footer': "Each of these is queryable via the DC Hub API — links below.",
    },
    'contrarian': {
        'header': "Most reports treat the data-center market as static.\nThe numbers say otherwise:",
        'footer': "If your dashboard still shows last quarter's snapshot, you're behind.",
    },
}

STYLE_ORDER = ['data', 'narrative', 'listicle', 'contrarian']


def _pick_style(week_num: int) -> str:
    """Pick this week's style. Cycles through STYLE_ORDER on a week basis."""
    return STYLE_ORDER[week_num % len(STYLE_ORDER)]


def _apply_style(post_text: str, style: str) -> str:
    """Wrap a post's body with the chosen style's header/footer."""
    if not post_text:
        return post_text
    cfg = POST_STYLES.get(style, POST_STYLES['data'])
    header, footer = cfg.get('header'), cfg.get('footer')

    body = post_text
    if header:
        # Drop the original first line (which was the lead) and substitute.
        # Heuristic: original first line ends at the first '\n\n'. Keep
        # everything from the first '\n\n' onward.
        parts = body.split('\n\n', 1)
        if len(parts) == 2:
            body = f"{header}\n\n{parts[1]}"
        else:
            body = f"{header}\n\n{body}"
    if footer:
        # Insert footer right before the CTA line (heuristic: "See it live →"
        # or "Explore all" or any line beginning with the arrow).
        cta_markers = ['See it live →', 'Explore', 'Learn more', '→ dchub']
        inserted = False
        for marker in cta_markers:
            if marker in body:
                body = body.replace(marker, f"{footer}\n\n{marker}", 1)
                inserted = True
                break
        if not inserted:
            # Fallback: prepend footer to the hashtag line if it exists,
            # otherwise just append.
            if '#' in body:
                idx = body.rfind('\n')
                body = body[:idx] + f"\n\n{footer}\n" + body[idx:]
            else:
                body = f"{body}\n\n{footer}"
    return body


def _get_todays_topic():
    """Pick today's topic, alternating within each day's options."""
    now = datetime.now(timezone.utc)
    weekday = now.weekday()

    options = POST_SCHEDULE.get(weekday)
    if not options:
        return None, None

    # Use ISO week number to alternate: even weeks = first option, odd weeks = second
    week_num = now.isocalendar()[1]
    idx = week_num % len(options)
    return options[idx]


def generate_scheduled_post():
    """Generate the appropriate post for today's schedule with this week's style."""
    topic_name, generator = _get_todays_topic()
    if not generator:
        return None, None, None, None

    result = generator()
    if result and len(result) == 3:
        text, link_url, link_title = result
        # Phase ZZZZZ-round4: apply this week's writing style
        week_num = datetime.now(timezone.utc).isocalendar()[1]
        style = _pick_style(week_num)
        text = _apply_style(text, style)
        logger.info(f"[LinkedIn] topic={topic_name} style={style} (week {week_num})")
        return topic_name, text, link_url, link_title

    return None, None, None, None


# ── Admin Auth Check ─────────────────────────────────────────

def _check_admin(request):
    """Check if request has valid admin authorization."""
    key = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
    if not ADMIN_KEY:
        return True  # No admin key set = allow (for initial setup)
    return key == ADMIN_KEY


# ── Route Registration ───────────────────────────────────────

def register_linkedin_routes(app):
    """Register all LinkedIn-related routes."""
    from flask import request, redirect, jsonify, make_response

    _ensure_tables()

    def _cors_json(data, status=200):
        resp = make_response(jsonify(data), status)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

    # ── GET /api/linkedin/auth — Start OAuth ─────────────────
    @app.route('/api/linkedin/auth', methods=['GET'])
    def linkedin_auth_start():
        """Redirect to LinkedIn OAuth consent screen."""
        if not LINKEDIN_CLIENT_ID:
            return _cors_json({'error': 'LINKEDIN_CLIENT_ID not configured'}, 500)

        import secrets
        state = secrets.token_urlsafe(16)

        params = {
            'response_type': 'code',
            'client_id': LINKEDIN_CLIENT_ID,
            'redirect_uri': LINKEDIN_REDIRECT_URI,
            'scope': LINKEDIN_SCOPES,
            'state': state,
        }
        url = f"{AUTH_URL}?{urlencode(params)}"
        return redirect(url)

    # ── GET /api/linkedin/callback — OAuth callback ──────────
    @app.route('/api/linkedin/callback', methods=['GET'])
    def linkedin_auth_callback():
        """Handle LinkedIn OAuth callback, exchange code for token."""
        import requests as req

        code = request.args.get('code')
        error = request.args.get('error')

        if error:
            return _cors_json({'error': error, 'description': request.args.get('error_description')}, 400)

        if not code:
            return _cors_json({'error': 'No authorization code received'}, 400)

        # Exchange code for token
        try:
            resp = req.post(TOKEN_URL, data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': LINKEDIN_REDIRECT_URI,
                'client_id': LINKEDIN_CLIENT_ID,
                'client_secret': LINKEDIN_CLIENT_SECRET,
            })

            if resp.status_code != 200:
                return _cors_json({'error': 'Token exchange failed', 'details': resp.text}, 400)

            data = resp.json()
            access_token = data['access_token']
            refresh_token = data.get('refresh_token')
            expires_in = data.get('expires_in', 5184000)  # Default 60 days

            # Get member URN
            member_urn = None
            try:
                me_resp = req.get(USERINFO_URL, headers={
                    'Authorization': f'Bearer {access_token}'
                })
                if me_resp.status_code == 200:
                    me_data = me_resp.json()
                    member_urn = me_data.get('sub')
                    if member_urn and not member_urn.startswith('urn:'):
                        member_urn = f"urn:li:person:{member_urn}"
            except Exception as e:
                logger.warning(f"[LinkedIn] Could not fetch member info: {e}")

            _save_token(access_token, refresh_token, expires_in, member_urn)

            return f"""
            <html><body style="font-family:sans-serif;background:#0a0a0a;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;">
            <div style="text-align:center;">
                <h1 style="color:#00ff88;">✅ LinkedIn Connected!</h1>
                <p>DC Hub can now post to LinkedIn.</p>
                <p style="color:#888;">Token expires in {expires_in // 86400} days.</p>
                <p style="color:#888;">Member: {member_urn or 'unknown'}</p>
                <p style="color:#888;">Company: urn:li:organization:{LINKEDIN_COMPANY_ID}</p>
                <a href="/api/linkedin/status" style="color:#00ff88;">Check Status →</a>
            </div>
            </body></html>
            """

        except Exception as e:
            return _cors_json({'error': str(e)}, 500)

    # ── POST /api/linkedin/post — Manual post ────────────────
    @app.route('/api/linkedin/post', methods=['POST', 'OPTIONS'])
    def linkedin_manual_post():
        """Manually trigger a LinkedIn post. Requires admin key."""
        if request.method == 'OPTIONS':
            return _cors_json({})

        if not _check_admin(request):
            return _cors_json({'error': 'Unauthorized'}, 401)

        data = request.get_json() or {}
        # Accept both "text" and "content" for convenience
        text = (data.get('text') or data.get('content') or '').strip()
        text = text + '\n\n' + _phase30c_landing_url()  # phase34_landing_done

        if not text:
            return _cors_json({'error': 'Missing "text" or "content" field'}, 400)

        link_url = data.get('link_url')
        link_title = data.get('link_title')
        link_desc = data.get('link_desc')

        success, result = post_to_linkedin(text, link_url, link_title, link_desc)
        return _cors_json(result, 200 if success else 500)

    # ── POST /api/linkedin/auto-post — Scheduled auto-post ────
    @app.route('/api/linkedin/auto-post', methods=['POST', 'OPTIONS'])
    def linkedin_auto_post():
        """Trigger the scheduled auto-post (rotates topics Mon/Wed/Fri)."""
        if request.method == 'OPTIONS':
            return _cors_json({})

        if not _check_admin(request):
            return _cors_json({'error': 'Unauthorized'}, 401)

        # Allow forcing a specific topic
        force_topic = (request.get_json() or {}).get('topic') if request.is_json else request.args.get('topic')

        if force_topic:
            generators = {
                'ai_analytics': generate_weekly_post,
                'deals': generate_deals_post,
                'news': generate_news_post,
                'market_intel': generate_market_post,
                'mcp_adoption': generate_mcp_post,
                'pipeline': generate_pipeline_post,
            }
            gen = generators.get(force_topic)
            if not gen:
                return _cors_json({'error': f'Unknown topic: {force_topic}', 'available': list(generators.keys())}, 400)
            result = gen()
            if result and len(result) == 3:
                topic_name, text, link_url = force_topic, result[0], result[1]
                link_title = result[2]
            else:
                return _cors_json({'error': f'Could not generate {force_topic} post'}, 500)
        else:
            topic_name, text, link_url, link_title = generate_scheduled_post()

        if not text:
            return _cors_json({'error': 'Could not generate post — no data available for today\'s topic'}, 500)

        success, result = post_to_linkedin(
            text,
            link_url=link_url,
            link_title=link_title,
            link_desc='Data Center Intelligence for the AI Era — DC Hub'
        )

        if success:
            _execute("""
                UPDATE linkedin_posts 
                SET post_type = %s 
                WHERE id = (SELECT MAX(id) FROM linkedin_posts)
            """, (f'auto_{topic_name}',))

        result['topic'] = topic_name
        return _cors_json(result, 200 if success else 500)

    # ── GET /api/linkedin/status — Check status ──────────────
    @app.route('/api/linkedin/status', methods=['GET', 'OPTIONS'])
    def linkedin_status():
        if request.method == 'OPTIONS':
            return _cors_json({})

        token = _get_token()
        has_token = bool(token and token.get('access_token'))

        expires_at = token.get('expires_at') if token else None
        if expires_at:
            remaining = (expires_at - datetime.now(timezone.utc)).days
        else:
            remaining = None

        # Last post
        last_post = _execute("""
            SELECT post_urn, content, post_type, status, error, posted_at::text
            FROM linkedin_posts ORDER BY posted_at DESC LIMIT 1
        """, fetch=True)

        # Post count
        count = _execute("SELECT COUNT(*) as cnt FROM linkedin_posts", fetch=True)

        # Next scheduled topic
        topic_name, _ = _get_todays_topic()
        next_days = {0: 'Monday', 2: 'Wednesday', 4: 'Friday'}
        now = datetime.now(timezone.utc)
        next_day = None
        for d in sorted(next_days.keys()):
            if d >= now.weekday():
                next_day = next_days[d]
                break
        if not next_day:
            next_day = next_days[0]  # Next Monday

        return _cors_json({
            'configured': bool(LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET),
            'authorized': has_token,
            'company_id': LINKEDIN_COMPANY_ID or 'not set',
            'token_expires_in_days': remaining,
            'total_posts': count.get('cnt', 0) if count else 0,
            'last_post': last_post,
            'schedule': 'Mon/Wed/Fri 14:00 UTC',
            'todays_topic': topic_name,
            'next_post_day': next_day,
            'topics': ['ai_analytics', 'deals', 'news', 'market_intel', 'mcp_adoption', 'pipeline'],
        })

    # ── Mon/Wed/Fri auto-post scheduler ────────────────────────
    def _scheduled_poster():
        """Background thread that auto-posts Mon/Wed/Fri at 14:00 UTC (9am ET / 7am PT)."""
        while True:
            try:
                now = datetime.now(timezone.utc)
                # Mon=0, Wed=2, Fri=4 — post at 14:00 UTC (peak LinkedIn engagement)
                if now.weekday() in (0, 2, 4) and now.hour == 14 and now.minute < 5:
                    # Check if we already posted today
                    today_post = _execute("""
                        SELECT COUNT(*) as cnt FROM linkedin_posts
                        WHERE post_type LIKE 'auto_%%'
                          AND posted_at::date = CURRENT_DATE
                          AND status = 'success'
                    """, fetch=True)

                    if today_post and today_post.get('cnt', 0) == 0:
                        topic_name, text, link_url, link_title = generate_scheduled_post()
                        if text:
                            logger.info(f"[LinkedIn] Auto-posting: {topic_name}")
                            success, result = post_to_linkedin(
                                text,
                                link_url=link_url,
                                link_title=link_title,
                                link_desc='Data Center Intelligence for the AI Era — DC Hub'
                            )
                            if success:
                                _execute("""
                                    UPDATE linkedin_posts 
                                    SET post_type = %s
                                    WHERE id = (SELECT MAX(id) FROM linkedin_posts)
                                """, (f'auto_{topic_name}',))
                                logger.info(f"[LinkedIn] ✅ Auto-post published: {topic_name}")
                            else:
                                logger.error(f"[LinkedIn] Auto-post failed: {result}")
                        else:
                            logger.warning("[LinkedIn] Auto-post skipped — no data for today's topic")

                _time.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"[LinkedIn] Scheduler error: {e}")
                _time.sleep(600)

    # ── POST /api/linkedin/seed-token — Bootstrap token from env ─
    @app.route('/api/linkedin/seed-token', methods=['POST', 'OPTIONS'])
    def linkedin_seed_token():
        """
        Seed the LINKEDIN_ACCESS_TOKEN env var into the Neon DB so the
        poster can use it. Call this once after setting the env var in Railway.
        Requires admin key.

        Body (optional):
          { "access_token": "...", "expires_in": 5184000 }
        If body is empty, uses LINKEDIN_ACCESS_TOKEN env var automatically.
        """
        if request.method == 'OPTIONS':
            return _cors_json({})

        if not _check_admin(request):
            return _cors_json({'error': 'Unauthorized'}, 401)

        data = request.get_json() or {}
        token_to_save = data.get('access_token') or LINKEDIN_ACCESS_TOKEN_ENV
        expires_in   = int(data.get('expires_in', 5184000))  # default 60 days

        if not token_to_save:
            return _cors_json({
                'error': 'No token provided and LINKEDIN_ACCESS_TOKEN env var is not set',
                'hint': 'Set LINKEDIN_ACCESS_TOKEN in Railway env vars and redeploy, then call this endpoint again'
            }, 400)

        try:
            _save_token(token_to_save, expires_in=expires_in)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            return _cors_json({
                'success': True,
                'message': 'Token seeded into Neon DB — LinkedIn posting is now active',
                'expires_at': expires_at.isoformat(),
                'expires_in_days': expires_in // 86400,
                'source': 'env_var' if not data.get('access_token') else 'request_body',
            })
        except Exception as e:
            return _cors_json({'error': str(e)}, 500)

    # Start scheduler thread
    t = threading.Thread(target=_scheduled_poster, daemon=True, name='linkedin-scheduler')
    t.start()

    # Auto-seed token from env if DB has no token yet
    try:
        existing = _get_token()
        if (not existing or not existing.get('access_token')) and LINKEDIN_ACCESS_TOKEN_ENV:
            _save_token(LINKEDIN_ACCESS_TOKEN_ENV, expires_in=5184000)
            logger.info("[LinkedIn] ✅ Auto-seeded LINKEDIN_ACCESS_TOKEN from env into Neon DB")
    except Exception as e:
        logger.warning(f"[LinkedIn] Could not auto-seed token: {e}")

    logger.info("[LinkedIn] ✅ Routes registered:")
    logger.info("  GET  /api/linkedin/auth")
    logger.info("  GET  /api/linkedin/callback")
    logger.info("  POST /api/linkedin/post       (accepts 'text' or 'content' field)")
    logger.info("  POST /api/linkedin/auto-post")
    logger.info("  POST /api/linkedin/seed-token (bootstrap token from env)")
    logger.info("  GET  /api/linkedin/status")
    logger.info(f"  Company: {LINKEDIN_COMPANY_ID or 'NOT SET'}")
    logger.info(f"  Schedule: Mon/Wed/Fri 14:00 UTC")
    logger.info(f"  Topics: ai_analytics, deals, news, market_intel, mcp_adoption, pipeline")
