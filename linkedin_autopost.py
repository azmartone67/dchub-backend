
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
DC Hub - LinkedIn Auto-Posting Module
======================================
Integrates with LinkedIn Posts API v2 to automatically publish
DC Hub content to the company LinkedIn page.

Add to your Replit backend (main.py) or import as a module.

Setup Requirements:
1. Create LinkedIn App at https://www.linkedin.com/developers/
2. Request "Share on LinkedIn" + "Sign In with LinkedIn using OpenID Connect" products
3. For company page posting: Request "Marketing Developer Platform" access
4. Set environment variables (see CONFIGURATION section below)

Author: DC Hub / dchub.cloud
"""

import os
import json
import time
import logging
import requests
import threading
from datetime import datetime, timedelta
from functools import wraps

# Flask imports (matches your existing backend)
from flask import Blueprint, request, jsonify, redirect, session

# Phase 30C — daily landing URL (LinkedIn renders rich card from this URL's OG)
def _phase30c_landing_url(d=None):
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"  # phase31_canonical_url

logger = logging.getLogger('dchub.linkedin')

# =============================================================================
# CONFIGURATION - Set these as Replit Secrets / Environment Variables
# =============================================================================
LINKEDIN_CLIENT_ID = os.environ.get('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = os.environ.get('LINKEDIN_CLIENT_SECRET', '')
LINKEDIN_REDIRECT_URI = os.environ.get('LINKEDIN_REDIRECT_URI', 'https://dchub.cloud/api/v1/linkedin/callback')
LINKEDIN_ORG_ID = os.environ.get('LINKEDIN_ORG_ID', '')  # Your DC Hub company page org ID
LINKEDIN_ACCESS_TOKEN_ENV = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')  # Direct token from Replit Secrets

# LinkedIn API endpoints
LINKEDIN_AUTH_URL = 'https://www.linkedin.com/oauth/v2/authorization'
LINKEDIN_TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'
LINKEDIN_API_BASE = 'https://api.linkedin.com'
LINKEDIN_API_VERSION = '202502'  # Update as needed

# =============================================================================
# BLUEPRINT - Register this with your Flask app
# =============================================================================
linkedin_auto_bp = Blueprint('linkedin_auto', __name__)


# =============================================================================
# TOKEN STORAGE - Uses your existing PostgreSQL
# =============================================================================
def get_pg_connection():
    """Get PostgreSQL connection via the main connection pool (BG pool for background tasks)."""
    from db_utils import get_bg_db
    wrapper = get_bg_db()
    return wrapper


def init_linkedin_tables():
    """Create LinkedIn tables in PostgreSQL. Call once at startup."""
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS linkedin_tokens (
                id SERIAL PRIMARY KEY,
                token_type VARCHAR(50) DEFAULT 'organization',
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at TIMESTAMP NOT NULL,
                refresh_expires_at TIMESTAMP,
                scopes TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS linkedin_posts (
                id SERIAL PRIMARY KEY,
                post_id VARCHAR(255),
                post_type VARCHAR(50) NOT NULL,
                content_text TEXT NOT NULL,
                article_url TEXT,
                status VARCHAR(50) DEFAULT 'draft',
                scheduled_at TIMESTAMP,
                published_at TIMESTAMP,
                linkedin_response TEXT,
                source_event VARCHAR(100),
                source_data TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                error_message TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS linkedin_post_queue (
                id SERIAL PRIMARY KEY,
                content_text TEXT NOT NULL,
                article_url TEXT,
                post_type VARCHAR(50) DEFAULT 'article',
                priority INTEGER DEFAULT 5,
                scheduled_for TIMESTAMP,
                status VARCHAR(50) DEFAULT 'queued',
                created_at TIMESTAMP DEFAULT NOW(),
                attempts INTEGER DEFAULT 0,
                last_error TEXT
            )
        ''')
        conn.commit()
        logger.info("LinkedIn tables initialized")
    except Exception as e:
        conn.rollback()
        logger.error(f"Error creating LinkedIn tables: {e}")
    finally:
        cur.close()
        conn.close()


# =============================================================================
# TOKEN MANAGEMENT
# =============================================================================
def save_token(access_token, expires_in, refresh_token=None, refresh_expires_in=None, scopes=''):
    """Save LinkedIn OAuth token to PostgreSQL."""
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        refresh_expires_at = None
        if refresh_expires_in:
            refresh_expires_at = datetime.utcnow() + timedelta(seconds=refresh_expires_in)

        # Upsert - keep only one active token
        cur.execute('DELETE FROM linkedin_tokens WHERE token_type = %s', ('organization',))
        cur.execute('''
            INSERT INTO linkedin_tokens (token_type, access_token, refresh_token, expires_at, refresh_expires_at, scopes)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', ('organization', access_token, refresh_token, expires_at, refresh_expires_at, scopes))
        conn.commit()
        logger.info(f"LinkedIn token saved, expires at {expires_at}")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error saving LinkedIn token: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def get_valid_token():
    """Retrieve a valid access token. Checks env var first, then DB."""
    # Priority 1: Direct token from Replit Secrets
    if LINKEDIN_ACCESS_TOKEN_ENV:
        return LINKEDIN_ACCESS_TOKEN_ENV

    # Priority 2: Token from PostgreSQL (from OAuth flow)
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT access_token, refresh_token, expires_at, refresh_expires_at
            FROM linkedin_tokens
            WHERE token_type = 'organization'
            ORDER BY created_at DESC LIMIT 1
        ''')
        row = cur.fetchone()
        if not row:
            logger.warning("No LinkedIn token found in DB or env")
            return None

        access_token, refresh_token, expires_at, refresh_expires_at = row

        # Check if token is still valid (with 5-min buffer)
        if expires_at > datetime.utcnow() + timedelta(minutes=5):
            return access_token

        # Try refresh
        if refresh_token and refresh_expires_at and refresh_expires_at > datetime.utcnow():
            logger.info("Refreshing LinkedIn access token...")
            new_token = refresh_access_token(refresh_token)
            if new_token:
                return new_token

        logger.warning("LinkedIn token expired, re-authorization needed")
        return None
    except Exception as e:
        logger.error(f"Error getting LinkedIn token: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def refresh_access_token(refresh_token):
    """Refresh the LinkedIn access token."""
    try:
        resp = requests.post(LINKEDIN_TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': LINKEDIN_CLIENT_ID,
            'client_secret': LINKEDIN_CLIENT_SECRET,
        })
        if resp.status_code == 200:
            data = resp.json()
            save_token(
                access_token=data['access_token'],
                expires_in=data.get('expires_in', 5184000),
                refresh_token=data.get('refresh_token', refresh_token),
                refresh_expires_in=data.get('refresh_token_expires_in'),
                scopes=data.get('scope', '')
            )
            return data['access_token']
        else:
            logger.error(f"Token refresh failed: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return None


# =============================================================================
# LINKEDIN API HELPERS
# =============================================================================
def linkedin_headers(access_token):
    """Standard headers for LinkedIn API v2 requests."""
    return {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
        'LinkedIn-Version': LINKEDIN_API_VERSION,
    }


def create_text_post(text, access_token=None):
    """Create a text-only post on the DC Hub company page."""
    if not access_token:
        access_token = get_valid_token()
    if not access_token:
        return {'error': 'No valid LinkedIn token'}

    payload = {
        'author': f'urn:li:organization:{LINKEDIN_ORG_ID}',
        'commentary': text,
        'visibility': 'PUBLIC',
        'distribution': {
            'feedDistribution': 'MAIN_FEED',
            'targetEntities': [],
            'thirdPartyDistributionChannels': []
        },
        'lifecycleState': 'PUBLISHED',
    }

    resp = requests.post(
        f'{LINKEDIN_API_BASE}/rest/posts',
        headers=linkedin_headers(access_token),
        json=payload
    )

    result = {
        'status_code': resp.status_code,
        'post_id': resp.headers.get('x-restli-id', ''),
        'response': resp.text
    }

    log_post('text', text, None, result)
    return result


def create_article_post(text, article_url, access_token=None):
    """Create a post with an article link on the DC Hub company page."""
    if not access_token:
        access_token = get_valid_token()
    if not access_token:
        return {'error': 'No valid LinkedIn token'}

    payload = {
        'author': f'urn:li:organization:{LINKEDIN_ORG_ID}',
        'commentary': text,
        'visibility': 'PUBLIC',
        'distribution': {
            'feedDistribution': 'MAIN_FEED',
            'targetEntities': [],
            'thirdPartyDistributionChannels': []
        },
        'content': {
            'article': {
                'source': article_url,
                'title': '',  # LinkedIn auto-pulls from OG tags
                'description': '',  # LinkedIn auto-pulls from OG tags
            }
        },
        'lifecycleState': 'PUBLISHED',
    }

    resp = requests.post(
        f'{LINKEDIN_API_BASE}/rest/posts',
        headers=linkedin_headers(access_token),
        json=payload
    )

    result = {
        'status_code': resp.status_code,
        'post_id': resp.headers.get('x-restli-id', ''),
        'response': resp.text
    }

    log_post('article', text, article_url, result)
    return result


def log_post(post_type, text, article_url, result):
    """Log a published post to PostgreSQL."""
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        status = 'published' if result.get('status_code') == 201 else 'failed'
        cur.execute('''
            INSERT INTO linkedin_posts (post_id, post_type, content_text, article_url, status, published_at, linkedin_response)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (
            result.get('post_id', ''),
            post_type,
            text,
            article_url,
            status,
            datetime.utcnow() if status == 'published' else None,
            json.dumps(result)
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging LinkedIn post: {e}")


# =============================================================================
# CONTENT GENERATION TEMPLATES
# Generates LinkedIn-ready text from DC Hub data events
# =============================================================================

class DCHubContentGenerator:
    """Generates LinkedIn post content from DC Hub platform events."""

    @staticmethod
    def new_deal(deal):
        """Generate post for a new M&A transaction."""
        buyer = deal.get('buyer', 'Unknown')
        seller = deal.get('seller', 'Unknown')
        deal_type = deal.get('deal_type', 'acquisition')
        location = deal.get('location', '')
        capacity = deal.get('capacity_mw', '')

        text = f"New Data Center {deal_type.title()} Tracked\n\n"
        text += f"{buyer} {'acquires' if 'acquis' in deal_type.lower() else 'partners with'} {seller}"
        if location:
            text += f" in {location}"
        if capacity:
            text += f" ({capacity} MW)"
        text += ".\n\n"
        text += "Track the full deal pipeline and 470+ transactions on DC Hub.\n\n"
        text += "#DataCenter #Infrastructure #MandA #DigitalInfrastructure"

        text = text + '\n\n' + _phase30c_landing_url()  # phase34_landing_done

        return {
            'text': text,
            'url': 'https://dchub.cloud/transactions',
            'type': 'article'
        }

    @staticmethod
    def capacity_milestone(milestone):
        """Generate post for capacity pipeline milestones."""
        market = milestone.get('market', '')
        total_mw = milestone.get('total_mw', '')
        projects = milestone.get('project_count', '')

        text = f"Data Center Capacity Update: {market}\n\n"
        if total_mw:
            text += f"Total pipeline: {total_mw} MW across {projects} projects\n"
        text += "\nExplore the full capacity pipeline with interactive maps and filters on DC Hub.\n\n"
        text += "#DataCenter #CapacityPlanning #CloudInfrastructure #DigitalTransformation"

        return {
            'text': text,
            'url': 'https://dchub.cloud/capacity-pipeline',
            'type': 'article'
        }

    @staticmethod
    def facility_count_update(stats):
        """Generate post for facility tracking milestones."""
        total = stats.get('total_facilities', '20,000+')
        countries = stats.get('countries', '140+')

        text = f"DC Hub now tracks {total} data center facilities across {countries} countries.\n\n"
        text += "From hyperscale campuses to edge deployments — the most comprehensive "
        text += "data center intelligence platform available.\n\n"
        text += "Explore the interactive global map:\n\n"
        text += "#DataCenter #Infrastructure #CloudComputing #SiteSelection"

        return {
            'text': text,
            'url': 'https://dchub.cloud',
            'type': 'article'
        }

    @staticmethod
    def weekly_market_digest(data):
        """Generate weekly market summary post."""
        new_deals = data.get('new_deals', 0)
        new_facilities = data.get('new_facilities', 0)
        top_market = data.get('top_market', '')

        text = "DC Hub Weekly Intelligence Brief\n\n"
        if new_deals:
            text += f"This week: {new_deals} new M&A transactions tracked\n"
        if new_facilities:
            text += f"{new_facilities} facilities added to the platform\n"
        if top_market:
            text += f"Hottest market: {top_market}\n"
        text += "\nStay ahead of data center market moves with DC Hub.\n\n"
        text += "#DataCenter #MarketIntelligence #WeeklyDigest #Infrastructure"

        return {
            'text': text,
            'url': 'https://dchub.cloud/transactions',
            'type': 'article'
        }

    @staticmethod
    def news_highlight(article):
        """Generate post sharing a DC Hub news article."""
        title = article.get('title', '')
        summary = article.get('summary', '')
        url = article.get('url', '')

        text = f"{title}\n\n"
        if summary:
            # Truncate to ~200 chars for LinkedIn readability
            text += f"{summary[:200]}{'...' if len(summary) > 200 else ''}\n\n"
        text += "Read more on DC Hub:\n\n"
        text += "#DataCenter #News #Infrastructure"

        return {
            'text': text,
            'url': url or 'https://dchub.cloud/news',
            'type': 'article'
        }

    @staticmethod
    def custom_post(text, url=None):
        """Create a custom post."""
        return {
            'text': text,
            'url': url,
            'type': 'article' if url else 'text'
        }


# =============================================================================
# POST QUEUE & SCHEDULER
# =============================================================================

def queue_post(content_text, article_url=None, post_type='article',
               priority=5, scheduled_for=None):
    """Add a post to the publishing queue."""
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        cur.execute('''
            INSERT INTO linkedin_post_queue (content_text, article_url, post_type, priority, scheduled_for)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        ''', (content_text, article_url, post_type, priority, scheduled_for))
        post_id = cur.fetchone()[0]
        conn.commit()
        logger.info(f"Queued LinkedIn post #{post_id}")
        return post_id
    except Exception as e:
        conn.rollback()
        logger.error(f"Error queuing post: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def process_queue():
    """Process the LinkedIn post queue. Call periodically (e.g., every 15 min)."""
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        now = datetime.utcnow()
        # Get next post that's due
        cur.execute('''
            SELECT id, content_text, article_url, post_type
            FROM linkedin_post_queue
            WHERE status = 'queued'
              AND (scheduled_for IS NULL OR scheduled_for <= %s)
              AND attempts < 3
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
        ''', (now,))

        row = cur.fetchone()
        if not row:
            return None

        post_id, content_text, article_url, post_type = row

        # Mark as processing
        cur.execute("UPDATE linkedin_post_queue SET status = 'processing', attempts = attempts + 1 WHERE id = %s", (post_id,))
        conn.commit()

        # Publish
        if post_type == 'article' and article_url:
            result = create_article_post(content_text, article_url)
        else:
            result = create_text_post(content_text)

        # Update status
        if result.get('status_code') == 201:
            cur.execute("UPDATE linkedin_post_queue SET status = 'published' WHERE id = %s", (post_id,))
            logger.info(f"Published queued post #{post_id}")
        else:
            error_msg = result.get('response', str(result))
            cur.execute("UPDATE linkedin_post_queue SET status = 'queued', last_error = %s WHERE id = %s",
                        (error_msg[:500], post_id))
            logger.warning(f"Failed to publish post #{post_id}: {error_msg[:200]}")

        conn.commit()
        return result

    except Exception as e:
        conn.rollback()
        logger.error(f"Queue processing error: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def start_linkedin_scheduler(interval_minutes=30):
    """Start background thread to process the post queue.
    Matches your existing pattern with news_scheduler and sync threads.
    """
    def _scheduler_loop():
        while True:
            try:
                process_queue()
            except Exception as e:
                logger.error(f"LinkedIn scheduler error: {e}")
            time.sleep(interval_minutes * 60)

    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.name = 'linkedin-scheduler'
    thread.start()
    logger.info(f"LinkedIn post scheduler started (every {interval_minutes} min)")
    return thread


# =============================================================================
# DATA EVENT HOOKS
# Call these from your existing backend when events occur
# =============================================================================

def on_new_deal(deal_data):
    """Hook: Call when a new M&A deal is added to PostgreSQL."""
    content = DCHubContentGenerator.new_deal(deal_data)
    return queue_post(
        content_text=content['text'],
        article_url=content['url'],
        post_type=content['type'],
        priority=3  # High priority for deals
    )


def on_capacity_milestone(milestone_data):
    """Hook: Call when capacity pipeline hits a milestone."""
    content = DCHubContentGenerator.capacity_milestone(milestone_data)
    return queue_post(
        content_text=content['text'],
        article_url=content['url'],
        post_type=content['type'],
        priority=5
    )


def on_weekly_digest():
    """Hook: Call weekly (e.g., every Monday) to post market summary."""
    # Pull stats from your existing database
    try:
        conn = get_pg_connection()
        cur = conn.cursor()

        # Count deals from last 7 days
        cur.execute("""
            SELECT COUNT(*) FROM deals
            WHERE created_at >= NOW() - INTERVAL '7 days'
        """)
        new_deals = cur.fetchone()[0]

        # Count new facilities (adjust table/column names to match yours)
        cur.execute("""
            SELECT COUNT(*) FROM data_centers
            WHERE created_at >= NOW() - INTERVAL '7 days'
        """)
        new_facilities = cur.fetchone()[0]

        cur.close()
        conn.close()

        data = {
            'new_deals': new_deals,
            'new_facilities': new_facilities,
            'top_market': '',  # Could query most active market
        }
        content = DCHubContentGenerator.weekly_market_digest(data)
        return queue_post(
            content_text=content['text'],
            article_url=content['url'],
            post_type=content['type'],
            priority=4,
            scheduled_for=None  # Publish immediately when processed
        )
    except Exception as e:
        logger.error(f"Error generating weekly digest: {e}")
        return None


# =============================================================================
# FLASK ROUTES - OAuth Flow & Admin API
# =============================================================================

@linkedin_auto_bp.route('/api/v1/linkedin/auth')
def linkedin_auth():
    """Step 1: Redirect admin to LinkedIn for OAuth authorization."""
    # Basic admin check - adapt to your auth system
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    # You can add proper admin validation here

    scopes = 'openid profile w_member_social w_organization_social r_organization_social'
    state = os.urandom(16).hex()

    auth_url = (
        f"{LINKEDIN_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={LINKEDIN_CLIENT_ID}"
        f"&redirect_uri={LINKEDIN_REDIRECT_URI}"
        f"&state={state}"
        f"&scope={scopes}"
    )

    return jsonify({
        'auth_url': auth_url,
        'message': 'Visit this URL to authorize DC Hub LinkedIn posting'
    })


@linkedin_auto_bp.route('/api/v1/linkedin/callback')
def linkedin_callback():
    """Step 2: OAuth callback - exchange code for access token."""
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return jsonify({'error': error, 'description': request.args.get('error_description')}), 400

    if not code:
        return jsonify({'error': 'No authorization code received'}), 400

    # Exchange code for token
    resp = requests.post(LINKEDIN_TOKEN_URL, data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': LINKEDIN_REDIRECT_URI,
        'client_id': LINKEDIN_CLIENT_ID,
        'client_secret': LINKEDIN_CLIENT_SECRET,
    })

    if resp.status_code != 200:
        return jsonify({'error': 'Token exchange failed', 'details': resp.text}), 400

    token_data = resp.json()
    save_token(
        access_token=token_data['access_token'],
        expires_in=token_data.get('expires_in', 5184000),  # Default 60 days
        refresh_token=token_data.get('refresh_token'),
        refresh_expires_in=token_data.get('refresh_token_expires_in'),
        scopes=token_data.get('scope', '')
    )

    return jsonify({
        'success': True,
        'message': 'LinkedIn authorization successful! Token saved.',
        'expires_in_days': token_data.get('expires_in', 5184000) // 86400
    })


@linkedin_auto_bp.route('/api/v1/linkedin/status')
def linkedin_status():
    """Check LinkedIn integration status."""
    token = get_valid_token()
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        # Get token info
        cur.execute('''
            SELECT expires_at, scopes, updated_at
            FROM linkedin_tokens
            WHERE token_type = 'organization'
            ORDER BY created_at DESC LIMIT 1
        ''')
        token_row = cur.fetchone()

        # Get queue stats
        cur.execute('''
            SELECT status, COUNT(*)
            FROM linkedin_post_queue
            GROUP BY status
        ''')
        queue_stats = dict(cur.fetchall())

        # Get recent posts
        cur.execute('''
            SELECT post_type, status, published_at, content_text
            FROM linkedin_posts
            ORDER BY created_at DESC LIMIT 5
        ''')
        recent_posts = [{
            'type': r[0], 'status': r[1],
            'published_at': r[2].isoformat() if r[2] else None,
            'text_preview': r[3][:100] + '...' if r[3] and len(r[3]) > 100 else r[3]
        } for r in cur.fetchall()]

        return jsonify({
            'connected': token is not None,
            'token_expires': token_row[0].isoformat() if token_row else None,
            'scopes': token_row[1] if token_row else None,
            'last_token_update': token_row[2].isoformat() if token_row else None,
            'queue': queue_stats,
            'recent_posts': recent_posts,
            'config': {
                'client_id_set': bool(LINKEDIN_CLIENT_ID),
                'client_secret_set': bool(LINKEDIN_CLIENT_SECRET),
                'org_id_set': bool(LINKEDIN_ORG_ID),
                'redirect_uri': LINKEDIN_REDIRECT_URI,
            }
        })
    except Exception as e:
        return jsonify({'error': str(e), 'connected': False})
    finally:
        cur.close()
        conn.close()


@linkedin_auto_bp.route('/api/v1/linkedin/post', methods=['POST'])
def linkedin_post_now():
    """Manually publish or queue a LinkedIn post (admin endpoint)."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    text = data.get('text', '')
    url = data.get('url', '')
    schedule = data.get('schedule')  # ISO datetime string or null
    post_type = data.get('type', 'article' if url else 'text')

    if not text:
        return jsonify({'error': 'Post text is required'}), 400

    if schedule:
        # Queue for later
        scheduled_for = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
        post_id = queue_post(text, url, post_type, scheduled_for=scheduled_for)
        return jsonify({
            'queued': True,
            'queue_id': post_id,
            'scheduled_for': scheduled_for.isoformat()
        })
    else:
        # Publish immediately
        if post_type == 'article' and url:
            result = create_article_post(text, url)
        else:
            result = create_text_post(text)

        success = result.get('status_code') == 201
        return jsonify({
            'published': success,
            'post_id': result.get('post_id', ''),
            'details': result
        }), 201 if success else 400


@linkedin_auto_bp.route('/api/v1/linkedin/queue', methods=['GET'])
def linkedin_queue_list():
    """View the post queue."""
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT id, content_text, article_url, post_type, priority,
                   scheduled_for, status, created_at, attempts, last_error
            FROM linkedin_post_queue
            ORDER BY
                CASE status WHEN 'queued' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END,
                priority ASC, created_at ASC
            LIMIT 50
        ''')
        posts = [{
            'id': r[0],
            'text_preview': r[1][:120] + '...' if len(r[1]) > 120 else r[1],
            'article_url': r[2],
            'type': r[3],
            'priority': r[4],
            'scheduled_for': r[5].isoformat() if r[5] else None,
            'status': r[6],
            'created_at': r[7].isoformat(),
            'attempts': r[8],
            'last_error': r[9]
        } for r in cur.fetchall()]

        return jsonify({'queue': posts, 'count': len(posts)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@linkedin_auto_bp.route('/api/v1/linkedin/queue/<int:queue_id>', methods=['DELETE'])
def linkedin_queue_delete(queue_id):
    """Remove a post from the queue."""
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM linkedin_post_queue WHERE id = %s AND status = 'queued'", (queue_id,))
        deleted = cur.rowcount
        conn.commit()
        return jsonify({'deleted': deleted > 0})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@linkedin_auto_bp.route('/api/v1/linkedin/history', methods=['GET'])
def linkedin_post_history():
    """View published post history."""
    limit = request.args.get('limit', 20, type=int)
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT id, post_id, post_type, content_text, article_url,
                   status, published_at, created_at, error_message
            FROM linkedin_posts
            ORDER BY created_at DESC
            LIMIT %s
        ''', (min(limit, 100),))
        posts = [{
            'id': r[0],
            'linkedin_post_id': r[1],
            'type': r[2],
            'text': r[3],
            'article_url': r[4],
            'status': r[5],
            'published_at': r[6].isoformat() if r[6] else None,
            'created_at': r[7].isoformat(),
            'error': r[8]
        } for r in cur.fetchall()]

        return jsonify({'posts': posts, 'count': len(posts)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# =============================================================================
# INTEGRATION INSTRUCTIONS
# =============================================================================
"""
HOW TO ADD TO YOUR main.py ON REPLIT
====================================

1. Upload this file to your Replit project as `linkedin_autopost.py`

2. Add these lines to your main.py:

    # --- Near the top with other imports ---
    from linkedin_autopost import (
        linkedin_bp, init_linkedin_tables, start_linkedin_scheduler,
        on_new_deal, on_weekly_digest
    )

    # --- After app = Flask(__name__) ---
    app.register_blueprint(linkedin_bp)

    # --- In your startup / init section ---
    init_linkedin_tables()
    start_linkedin_scheduler(interval_minutes=30)

3. Set these Replit Secrets (environment variables):
    LINKEDIN_CLIENT_ID=your_client_id
    LINKEDIN_CLIENT_SECRET=your_client_secret
    LINKEDIN_REDIRECT_URI=https://dchub.cloud/api/v1/linkedin/callback
    LINKEDIN_ORG_ID=your_company_page_org_id

4. Add the callback route to your Cloudflare mcp-proxy Worker
   (it should already proxy /api/* to Replit)

5. Get your LinkedIn Org ID:
   Go to your DC Hub LinkedIn company page → URL will be:
   https://www.linkedin.com/company/XXXXXXX/
   The XXXXXXX is your org ID

6. LinkedIn Developer App Setup:
   a. Go to https://www.linkedin.com/developers/
   b. Create app (or use existing)
   c. Under Products, request:
      - "Share on LinkedIn"
      - "Sign In with LinkedIn using OpenID Connect"
      - "Marketing Developer Platform" (for company page posting)
   d. Under Auth, add redirect URL:
      https://dchub.cloud/api/v1/linkedin/callback
   e. Copy Client ID and Client Secret to Replit Secrets

7. First-time authorization:
   Visit: https://dchub.cloud/api/v1/linkedin/auth
   Click the auth_url → authorize → token saves automatically

8. Optional: Wire up event hooks in your existing code:
   - In your deal ingestion code: call on_new_deal(deal_data)
   - Set up a weekly cron: call on_weekly_digest()

API ENDPOINTS SUMMARY
=====================
GET  /api/v1/linkedin/auth       → Get OAuth authorization URL
GET  /api/v1/linkedin/callback   → OAuth callback (automatic)
GET  /api/v1/linkedin/status     → Check connection & queue status
POST /api/v1/linkedin/post       → Publish or schedule a post
GET  /api/v1/linkedin/queue      → View post queue
DEL  /api/v1/linkedin/queue/<id> → Remove queued post
GET  /api/v1/linkedin/history    → View published post history

POST /api/v1/linkedin/post BODY EXAMPLES
=========================================
Immediate publish:
{
    "text": "New data center tracked in Dallas, TX...",
    "url": "https://dchub.cloud/facilities/12345",
    "type": "article"
}

Schedule for later:
{
    "text": "Weekly market update...",
    "url": "https://dchub.cloud/transactions",
    "schedule": "2026-02-17T14:00:00Z"
}

Text-only post:
{
    "text": "Excited to announce DC Hub now tracks 20,000+ facilities!",
    "type": "text"
}
"""
