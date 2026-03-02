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

logger = logging.getLogger('linkedin')

# ── Config ───────────────────────────────────────────────────
LINKEDIN_CLIENT_ID = os.environ.get('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = os.environ.get('LINKEDIN_CLIENT_SECRET', '')
LINKEDIN_COMPANY_ID = os.environ.get('LINKEDIN_COMPANY_ID', '')  # Numeric ID from company page URL
LINKEDIN_REDIRECT_URI = os.environ.get('LINKEDIN_REDIRECT_URI', 'https://dchub.cloud/api/linkedin/callback')
LINKEDIN_SCOPES = 'openid profile w_member_social'

# LinkedIn API endpoints
AUTH_URL = 'https://www.linkedin.com/oauth/v2/authorization'
TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'
POSTS_URL = 'https://api.linkedin.com/rest/posts'
USERINFO_URL = 'https://api.linkedin.com/v2/userinfo'

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
                id BIGSERIAL PRIMARY KEY,
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
        VALUES (1, %s, %s, %s, %s, %s, NOW())
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
    """Get a valid access token, refreshing if needed."""
    import requests as req

    token = _get_token()
    if not token or not token.get('access_token'):
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

def post_to_linkedin(text, link_url=None, link_title=None, link_desc=None):
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
        'LinkedIn-Version': '202502',
    }

    try:
        resp = req.post(POSTS_URL, json=payload, headers=headers)

        if resp.status_code in (200, 201):
            post_urn = resp.headers.get('x-restli-id', resp.headers.get('X-LinkedIn-Id', ''))
            # Log success
            _execute("""
                INSERT INTO linkedin_posts (post_urn, content, post_type, status)
                VALUES (%s, %s, %s, 'success')
            """, (post_urn, text[:500], 'manual'))
            return True, {'post_urn': post_urn, 'status': 'published'}
        else:
            error_msg = resp.text[:500]
            # Log failure
            _execute("""
                INSERT INTO linkedin_posts (content, post_type, status, error)
                VALUES (%s, %s, 'failed', %s)
            """, (text[:500], 'manual', error_msg))
            return False, {'error': error_msg, 'status_code': resp.status_code}

    except Exception as e:
        error_msg = str(e)
        _execute("""
            INSERT INTO linkedin_posts (content, post_type, status, error)
            VALUES (%s, 'manual', 'failed', %s)
        """, (text[:500], error_msg))
        return False, {'error': error_msg}


def generate_weekly_post():
    """Generate a weekly LinkedIn post using live AI analytics data."""
    try:
        # Pull data from our weekly digest
        from ai_weekly_digest import generate_weekly_digest
        digest = generate_weekly_digest()
    except Exception as e:
        logger.error(f"[LinkedIn] Could not generate digest: {e}")
        return None

    s = digest.get('summary', {})
    platforms = digest.get('platforms', [])

    total_week = s.get('ai_requests_this_week', 0)
    total_all = s.get('total_all_time', 0)
    wow = s.get('wow_change_pct', 0)
    active = s.get('active_ai_platforms', 0)
    mcp = s.get('mcp_requests', 0)

    # Build platform list
    top_platforms = [p['platform'] for p in platforms[:5] if p['this_week'] > 0]
    platform_str = ', '.join(top_platforms) if top_platforms else 'multiple AI platforms'

    # Dynamic messaging based on data
    wow_msg = ""
    if wow > 0:
        wow_msg = f"That's up {wow:.0f}% from last week. "
    elif wow < 0:
        wow_msg = ""  # Don't highlight negative changes

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

    return post_text


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
        text = data.get('text', '').strip()

        if not text:
            return _cors_json({'error': 'Missing "text" field'}, 400)

        link_url = data.get('link_url')
        link_title = data.get('link_title')
        link_desc = data.get('link_desc')

        success, result = post_to_linkedin(text, link_url, link_title, link_desc)
        return _cors_json(result, 200 if success else 500)

    # ── POST /api/linkedin/auto-post — Weekly auto-post ──────
    @app.route('/api/linkedin/auto-post', methods=['POST', 'OPTIONS'])
    def linkedin_auto_post():
        """Trigger the weekly auto-generated post."""
        if request.method == 'OPTIONS':
            return _cors_json({})

        if not _check_admin(request):
            return _cors_json({'error': 'Unauthorized'}, 401)

        text = generate_weekly_post()
        if not text:
            return _cors_json({'error': 'Could not generate post — digest data unavailable'}, 500)

        success, result = post_to_linkedin(
            text,
            link_url='https://dchub.cloud/ai-analytics',
            link_title='AI Agent Analytics — DC Hub',
            link_desc='Live dashboard tracking AI platform usage across 20,000+ data center facilities.'
        )

        if success:
            # Log as auto-post
            _execute("""
                UPDATE linkedin_posts 
                SET post_type = 'auto_weekly' 
                WHERE id = (SELECT MAX(id) FROM linkedin_posts)
            """)

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

        return _cors_json({
            'configured': bool(LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET),
            'authorized': has_token,
            'company_id': LINKEDIN_COMPANY_ID or 'not set',
            'token_expires_in_days': remaining,
            'total_posts': count.get('cnt', 0) if count else 0,
            'last_post': last_post,
        })

    # ── Weekly auto-post scheduler ───────────────────────────
    def _weekly_scheduler():
        """Background thread that auto-posts every Monday at 9am UTC."""
        while True:
            try:
                now = datetime.now(timezone.utc)
                # Monday = 0, post at 9:00 UTC (4am ET / 1am PT)
                if now.weekday() == 0 and now.hour == 9 and now.minute < 5:
                    # Check if we already posted today
                    today_post = _execute("""
                        SELECT COUNT(*) as cnt FROM linkedin_posts
                        WHERE post_type = 'auto_weekly'
                          AND posted_at::date = CURRENT_DATE
                    """, fetch=True)

                    if today_post and today_post.get('cnt', 0) == 0:
                        logger.info("[LinkedIn] Auto-posting weekly update...")
                        text = generate_weekly_post()
                        if text:
                            success, result = post_to_linkedin(
                                text,
                                link_url='https://dchub.cloud/ai-analytics',
                                link_title='AI Agent Analytics — DC Hub',
                                link_desc='Live AI platform tracking dashboard.'
                            )
                            if success:
                                _execute("""
                                    UPDATE linkedin_posts 
                                    SET post_type = 'auto_weekly'
                                    WHERE id = (SELECT MAX(id) FROM linkedin_posts)
                                """)
                                logger.info(f"[LinkedIn] ✅ Weekly auto-post published")
                            else:
                                logger.error(f"[LinkedIn] Auto-post failed: {result}")
                        else:
                            logger.warning("[LinkedIn] Auto-post skipped — no digest data")

                _time.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"[LinkedIn] Scheduler error: {e}")
                _time.sleep(600)

    # Start scheduler thread
    t = threading.Thread(target=_weekly_scheduler, daemon=True, name='linkedin-scheduler')
    t.start()

    logger.info("[LinkedIn] ✅ Routes registered:")
    logger.info("  GET  /api/linkedin/auth")
    logger.info("  GET  /api/linkedin/callback")
    logger.info("  POST /api/linkedin/post")
    logger.info("  POST /api/linkedin/auto-post")
    logger.info("  GET  /api/linkedin/status")
    logger.info(f"  Company: {LINKEDIN_COMPANY_ID or 'NOT SET'}")
    logger.info(f"  Auto-post: Every Monday 9:00 UTC")
