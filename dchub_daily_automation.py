"""
DC Hub Daily Automation Engine
===============================
Unified module for all daily automated communications:
  1. Alert Digest Emails (SendGrid) - daily/weekly alert notifications
  2. LinkedIn Auto-Posts - daily deal alerts, weekly market briefs
  3. Daily Market Brief Email - morning intelligence digest to subscribers

Designed for EXTERNAL CRON triggers (no in-process scheduler).
Keeps Replit memory low by running only when triggered.

SETUP:
  1. Upload to Replit workspace root (alongside main.py)
  2. Add to main.py imports and registration (see bottom of file)
  3. Set environment variables (see REQUIRED ENV VARS below)
  4. Set up external cron at cron-job.org or UptimeRobot:
     - Daily 8:00 AM UTC:  POST /api/v1/daily/run?job=all
     - Monday 9:00 AM UTC: POST /api/v1/daily/run?job=weekly_digest

REQUIRED ENV VARS:
  SENDGRID_API_KEY          - SendGrid API key for emails
  SENDGRID_FROM_EMAIL       - Sender email (default: info@dchub.cloud)
  LINKEDIN_ACCESS_TOKEN     - LinkedIn OAuth token
  LINKEDIN_ORG_ID           - LinkedIn organization/company page ID
  DAILY_ADMIN_KEY           - Secret key to authorize cron triggers
"""

import os
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL', 'info@dchub.cloud')
LINKEDIN_ACCESS_TOKEN = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
LINKEDIN_ORG_ID = os.environ.get('LINKEDIN_ORG_ID', '')
LINKEDIN_API_VERSION = '202601'  # Jan 2026 - current active version
DAILY_ADMIN_KEY = os.environ.get('DAILY_ADMIN_KEY', os.environ.get('ADMIN_KEY', ''))

log = logging.getLogger('dchub-daily')

daily_bp = Blueprint('daily_automation', __name__)

_tables_initialized = False

def _ensure_tables():
    """Lazy init — create tables on first use, not at startup."""
    global _tables_initialized
    if _tables_initialized:
        return
    try:
        init_daily_tables()
        _tables_initialized = True
    except Exception as e:
        log.warning(f"Lazy table init failed (non-fatal): {e}")


# ===========================================================================
# UTILITY: SendGrid Email Sender
# ===========================================================================

def send_email(to_email, subject, html_content, text_content=None):
    """Send email via SendGrid API."""
    if not SENDGRID_API_KEY:
        log.warning("SendGrid not configured - email skipped")
        return {'success': False, 'error': 'SendGrid not configured'}

    import urllib.request
    import urllib.error

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": SENDGRID_FROM_EMAIL, "name": "DC Hub"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_content}]
    }
    if text_content:
        payload["content"].insert(0, {"type": "text/plain", "value": text_content})

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.sendgrid.com/v3/mail/send',
        data=data,
        headers={
            'Authorization': f'Bearer {SENDGRID_API_KEY}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return {'success': True, 'status_code': resp.status}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        log.error(f"SendGrid error {e.code}: {body}")
        return {'success': False, 'error': f'HTTP {e.code}', 'details': body}
    except Exception as e:
        log.error(f"SendGrid exception: {e}")
        return {'success': False, 'error': str(e)}


# ===========================================================================
# UTILITY: LinkedIn Post Publisher
# ===========================================================================

def post_to_linkedin(text, article_url=None):
    """Publish a post to LinkedIn company page via Posts API."""
    if not LINKEDIN_ACCESS_TOKEN or not LINKEDIN_ORG_ID:
        log.warning("LinkedIn not configured - post skipped")
        return {'success': False, 'error': 'LinkedIn not configured'}

    import urllib.request
    import urllib.error

    payload = {
        "author": f"urn:li:organization:{LINKEDIN_ORG_ID}",
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False
    }

    # Add article attachment if URL provided
    if article_url:
        payload["content"] = {
            "article": {
                "source": article_url,
                "title": "DC Hub - Data Center Intelligence",
                "description": "Real-time data center market intelligence covering 20,000+ facilities across 140+ countries."
            }
        }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.linkedin.com/rest/posts',
        data=data,
        headers={
            'Authorization': f'Bearer {LINKEDIN_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
            'LinkedIn-Version': LINKEDIN_API_VERSION,
            'X-Restli-Protocol-Version': '2.0.0'
        },
        method='POST'
    )

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        post_id = resp.headers.get('x-restli-id', '')
        return {'success': True, 'status_code': resp.status, 'post_id': post_id}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        log.error(f"LinkedIn error {e.code}: {body}")
        return {'success': False, 'error': f'HTTP {e.code}', 'details': body}
    except Exception as e:
        log.error(f"LinkedIn exception: {e}")
        return {'success': False, 'error': str(e)}


# ===========================================================================
# UTILITY: Data Fetchers (pull from DC Hub's own DB/API)
# ===========================================================================

def get_db_connection():
    """Get database connection - uses DATABASE_URL (cleaned by main.py at startup).
    Short-lived connection for daily automation queries."""
    # main.py cleans NEON_DATABASE_URL and sets DATABASE_URL at startup
    db_url = os.environ.get('DATABASE_URL', '')
    if not db_url:
        db_url = os.environ.get('NEON_DATABASE_URL', '')
    
    if db_url and db_url.startswith(('postgresql://', 'postgres://')):
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(db_url, connect_timeout=10)
            conn.autocommit = True
            return conn, 'postgres'
        except Exception as e:
            log.error(f"PostgreSQL connection FAILED: {type(e).__name__}: {e}")

    try:
        import sqlite3
        conn = sqlite3.connect('dchub.db', timeout=5)
        conn.row_factory = sqlite3.Row
        return conn, 'sqlite'
    except Exception as e:
        log.error(f"No database available: {e}")
        return None, None


def fetch_latest_news(limit=10):
    """Get latest news articles from the database."""
    conn, db_type = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        if db_type == 'postgres':
            cur.execute("""
                SELECT title, url, source, published_at, summary, category
                FROM news_articles
                ORDER BY published_at DESC
                LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
                SELECT title, url, source, published_at, summary, category
                FROM news_articles
                ORDER BY published_at DESC
                LIMIT ?
            """, (limit,))
        rows = cur.fetchall()
        if db_type == 'postgres':
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Error fetching news: {e}")
        return []
    finally:
        conn.close()


def fetch_recent_deals(limit=5):
    """Get recent M&A transactions."""
    conn, db_type = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        if db_type == 'postgres':
            cur.execute("""
                SELECT buyer, seller, deal_type, value_usd, market, 
                       mw as capacity_mw, date as announcement_date, notes
                FROM transactions
                ORDER BY date DESC NULLS LAST
                LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
                SELECT buyer, seller, deal_type, value_usd, market, 
                       mw as capacity_mw, date as announcement_date, notes
                FROM transactions
                ORDER BY date DESC
                LIMIT ?
            """, (limit,))
        rows = cur.fetchall()
        if db_type == 'postgres':
            cols = [desc[0] for desc in cur.description]
            results = [dict(zip(cols, row)) for row in rows]
        else:
            results = [dict(row) for row in rows]
        
        # Normalize value_usd from scientific notation strings to float
        for deal in results:
            try:
                val = deal.get('value_usd')
                if val and isinstance(val, str):
                    deal['value_usd'] = float(val)
                elif val:
                    deal['value_usd'] = float(val)
                else:
                    deal['value_usd'] = 0
            except (ValueError, TypeError):
                deal['value_usd'] = 0
            
            # Normalize capacity_mw
            try:
                mw = deal.get('capacity_mw')
                if mw and isinstance(mw, str):
                    deal['capacity_mw'] = float(mw)
                elif mw:
                    deal['capacity_mw'] = float(mw)
                else:
                    deal['capacity_mw'] = 0
            except (ValueError, TypeError):
                deal['capacity_mw'] = 0
        
        return results
    except Exception as e:
        log.error(f"Error fetching deals: {e}")
        return []
    finally:
        conn.close()


def fetch_platform_stats():
    """Get current platform statistics."""
    conn, db_type = get_db_connection()
    if not conn:
        return {'facilities': 20000, 'deals': 473, 'markets': '140+'}
    try:
        cur = conn.cursor()
        stats = {}

        # Facility count
        try:
            cur.execute("SELECT COUNT(*) FROM facilities")
            row = cur.fetchone()
            stats['facilities'] = row[0] if row else 20000
        except:
            stats['facilities'] = 20000

        # Deal count
        try:
            cur.execute("SELECT COUNT(*) FROM transactions")
            row = cur.fetchone()
            stats['deals'] = row[0] if row else 473
        except:
            stats['deals'] = 473

        # Total deal value
        try:
            if db_type == 'postgres':
                cur.execute("""
                    SELECT COALESCE(SUM(CAST(value_usd AS DOUBLE PRECISION)), 0) 
                    FROM transactions 
                    WHERE value_usd IS NOT NULL AND value_usd != '' AND value_usd ~ '^[0-9eE.+\-]+$'
                """)
            else:
                cur.execute("SELECT COALESCE(SUM(CAST(value_usd AS REAL)), 0) FROM transactions WHERE value_usd IS NOT NULL AND value_usd != ''")
            row = cur.fetchone()
            stats['total_value'] = float(row[0]) if row else 0
        except Exception as e:
            log.warning(f"Total value query failed: {e}")
            stats['total_value'] = 0

        stats['markets'] = '140+'
        return stats
    except Exception as e:
        log.error(f"Error fetching stats: {e}")
        return {'facilities': 20000, 'deals': 473, 'markets': '140+'}
    finally:
        conn.close()


def fetch_alert_subscribers(frequency='daily'):
    """Get users with active alerts for the given frequency."""
    conn, db_type = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        # Try alerts_v2 table first, fall back to user_alerts
        try:
            if db_type == 'postgres':
                cur.execute("""
                    SELECT DISTINCT a.user_id, u.email, u.name
                    FROM alerts_v2 a
                    JOIN users u ON a.user_id = u.id
                    WHERE a.is_active = TRUE AND a.frequency = %s AND u.email IS NOT NULL
                """, (frequency,))
            else:
                cur.execute("""
                    SELECT DISTINCT a.user_id, u.email, u.name
                    FROM alerts_v2 a
                    JOIN users u ON a.user_id = u.id
                    WHERE a.is_active = 1 AND a.frequency = ? AND u.email IS NOT NULL
                """, (frequency,))
        except:
            # Fall back to user_alerts table
            if db_type == 'postgres':
                cur.execute("""
                    SELECT DISTINCT a.user_id, u.email, u.name
                    FROM user_alerts a
                    JOIN users u ON a.user_id = u.id
                    WHERE a.is_active = TRUE AND u.email IS NOT NULL
                """)
            else:
                cur.execute("""
                    SELECT DISTINCT a.user_id, u.email, u.name
                    FROM user_alerts a
                    JOIN users u ON a.user_id = u.id
                    WHERE a.is_active = 1 AND u.email IS NOT NULL
                """)

        rows = cur.fetchall()
        if db_type == 'postgres':
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Error fetching subscribers: {e}")
        return []
    finally:
        conn.close()


def fetch_market_brief_subscribers():
    """Get users subscribed to the daily market brief."""
    conn, db_type = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        # Check for market_brief_subscribers table, fall back to pro users
        try:
            if db_type == 'postgres':
                cur.execute("""
                    SELECT email, name FROM market_brief_subscribers
                    WHERE is_active = TRUE AND email IS NOT NULL
                """)
            else:
                cur.execute("""
                    SELECT email, name FROM market_brief_subscribers
                    WHERE is_active = 1 AND email IS NOT NULL
                """)
        except:
            # Fall back to users with pro plan
            if db_type == 'postgres':
                cur.execute("""
                    SELECT email, name FROM users
                    WHERE plan IN ('pro', 'enterprise') AND email IS NOT NULL
                """)
            else:
                cur.execute("""
                    SELECT email, name FROM users
                    WHERE plan IN ('pro', 'enterprise') AND email IS NOT NULL
                """)

        rows = cur.fetchall()
        if db_type == 'postgres':
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Error fetching market brief subscribers: {e}")
        return []
    finally:
        conn.close()


# ===========================================================================
# JOB 1: ALERT DIGEST EMAILS
# ===========================================================================

def generate_alert_digest_html(news, deals, stats):
    """Generate HTML for the daily alert digest email."""
    today = datetime.now(timezone.utc).strftime('%B %d, %Y')

    news_items = ""
    for article in news[:8]:
        title = article.get('title', 'Untitled')
        url = article.get('url', '#')
        source = article.get('source', 'Unknown')
        news_items += f"""
        <tr>
          <td style="padding: 12px 0; border-bottom: 1px solid #334155;">
            <a href="{url}" style="color: #10b981; text-decoration: none; font-weight: 600; font-size: 14px;">{title}</a>
            <br><span style="color: #64748b; font-size: 12px;">{source}</span>
          </td>
        </tr>"""

    deal_items = ""
    for deal in deals[:5]:
        buyer = deal.get('buyer', 'Unknown')
        seller = deal.get('seller', 'Unknown')
        dtype = deal.get('deal_type', 'Transaction')
        value = deal.get('value_usd', 0)
        market = deal.get('market', '')
        notes = deal.get('notes', '')
        value_str = f"${value/1e9:.1f}B" if value >= 1e9 else f"${value/1e6:.0f}M" if value >= 1e6 else "Undisclosed"
        headline = notes if (notes and len(notes) > 20) else f"{buyer} → {seller}"
        deal_items += f"""
        <tr>
          <td style="padding: 10px 0; border-bottom: 1px solid #334155;">
            <span style="color: #f1f5f9; font-weight: 600; font-size: 13px;">{headline}</span>
            <br><span style="color: #10b981; font-size: 12px;">{dtype} • {value_str} {f'• {market}' if market else ''}</span>
          </td>
        </tr>"""

    facility_count = f"{stats.get('facilities', 20000):,}"
    deal_count = stats.get('deals', 473)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
    <body style="margin: 0; padding: 0; background: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
      <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
        <!-- Header -->
        <div style="text-align: center; padding: 30px 0 20px;">
          <h1 style="color: #10b981; font-size: 28px; margin: 0;">⚡ DC Hub</h1>
          <p style="color: #94a3b8; font-size: 14px; margin: 8px 0 0;">Daily Intelligence Digest • {today}</p>
        </div>

        <!-- Stats Bar -->
        <div style="background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 20px; text-align: center;">
          <span style="color: #10b981; font-weight: 700; font-size: 18px;">{facility_count}</span>
          <span style="color: #94a3b8; font-size: 13px;"> facilities tracked</span>
          <span style="color: #334155; margin: 0 8px;">|</span>
          <span style="color: #10b981; font-weight: 700; font-size: 18px;">{deal_count}</span>
          <span style="color: #94a3b8; font-size: 13px;"> deals tracked</span>
        </div>

        <!-- Latest News -->
        <div style="background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px;">
          <h2 style="color: #f1f5f9; font-size: 18px; margin: 0 0 12px;">📰 Latest News</h2>
          <table style="width: 100%; border-collapse: collapse;">
            {news_items if news_items else '<tr><td style="color: #64748b; padding: 12px 0;">No new articles today</td></tr>'}
          </table>
        </div>

        <!-- Recent Deals -->
        <div style="background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px;">
          <h2 style="color: #f1f5f9; font-size: 18px; margin: 0 0 12px;">💰 Recent Transactions</h2>
          <table style="width: 100%; border-collapse: collapse;">
            {deal_items if deal_items else '<tr><td style="color: #64748b; padding: 12px 0;">No new transactions today</td></tr>'}
          </table>
        </div>

        <!-- CTA -->
        <div style="text-align: center; margin: 30px 0;">
          <a href="https://dchub.cloud" style="display: inline-block; background: #10b981; color: white; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 16px;">
            Explore DC Hub →
          </a>
        </div>

        <!-- Footer -->
        <div style="text-align: center; padding: 20px 0; border-top: 1px solid #334155;">
          <p style="color: #64748b; font-size: 12px; margin: 0;">
            DC Hub • Data Center Intelligence Platform<br>
            Tracking {facility_count}+ facilities across 140+ countries<br>
            <a href="https://dchub.cloud" style="color: #10b981;">Manage Alerts</a> •
            <a href="https://dchub.cloud" style="color: #10b981;">Unsubscribe</a>
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def run_alert_digests(frequency='daily'):
    """Send alert digest emails to all subscribers."""
    results = {'job': 'alert_digests', 'frequency': frequency, 'sent': 0, 'failed': 0, 'errors': []}

    subscribers = fetch_alert_subscribers(frequency)
    if not subscribers:
        results['message'] = 'No subscribers found'
        return results

    news = fetch_latest_news(limit=10)
    deals = fetch_recent_deals(limit=5)
    stats = fetch_platform_stats()
    html = generate_alert_digest_html(news, deals, stats)

    subject = f"DC Hub {frequency.title()} Digest — {datetime.now(timezone.utc).strftime('%b %d, %Y')}"

    for sub in subscribers:
        email = sub.get('email')
        if not email:
            continue
        result = send_email(email, subject, html)
        if result.get('success'):
            results['sent'] += 1
        else:
            results['failed'] += 1
            results['errors'].append({'email': email, 'error': result.get('error', 'Unknown')})

    log.info(f"Alert digests ({frequency}): {results['sent']} sent, {results['failed']} failed")
    return results


# ===========================================================================
# JOB 2: LINKEDIN AUTO-POST
# ===========================================================================

def generate_daily_linkedin_post():
    """Generate a daily LinkedIn post from DC Hub data."""
    news = fetch_latest_news(limit=3)
    deals = fetch_recent_deals(limit=1)
    stats = fetch_platform_stats()

    today = datetime.now(timezone.utc).strftime('%B %d, %Y')
    facility_count = f"{stats.get('facilities', 20000):,}"

    # If there's a recent deal, lead with that
    if deals:
        deal = deals[0]
        buyer = deal.get('buyer', 'Unknown')
        seller = deal.get('seller', 'Unknown')
        dtype = deal.get('deal_type', 'Transaction')
        value = deal.get('value_usd', 0)
        market = deal.get('market', '')
        capacity = deal.get('capacity_mw', 0)
        notes = deal.get('notes', '')

        value_str = f"${value/1e9:.1f}B" if value >= 1e9 else f"${value/1e6:.0f}M" if value >= 1e6 else ""
        capacity_str = f"⚡ {capacity:.0f} MW" if capacity else ""

        # Use notes as headline if available (often cleaner than parsed buyer/seller)
        if notes and len(notes) > 20:
            headline = notes
        else:
            headline = f"{dtype.title()}: {buyer} → {seller} {f'for {value_str}' if value_str else ''}"

        post = f"""🏢 Data Center Deal Alert

{headline}
{f'💰 {value_str}' if value_str else ''}
{'📍 ' + market if market else ''}
{capacity_str}

DC Hub tracks {facility_count}+ facilities across 140+ countries with real-time M&A intelligence.

📊 Full M&A tracker: dchub.cloud/transactions
🔍 Explore: dchub.cloud

#DataCenter #Infrastructure #DigitalInfrastructure #RealEstate #CloudComputing"""

    # Otherwise, lead with top news
    elif news:
        headlines = "\n".join([f"📰 {a.get('title', '')}" for a in news[:3]])
        post = f"""📊 Data Center Market Update — {today}

{headlines}

DC Hub tracks {facility_count}+ data center facilities across 140+ countries. Get real-time market intelligence, M&A tracking, and AI-powered insights.

🔍 Explore: dchub.cloud
📡 Free API access: dchub.cloud/signup

#DataCenter #Infrastructure #DigitalInfrastructure #CloudComputing #AI"""

    # Fallback: platform stats
    else:
        post = f"""⚡ DC Hub Market Intelligence

Tracking {facility_count}+ data center facilities across 140+ countries.

What we monitor:
→ Real-time M&A transactions
→ Construction pipeline & capacity
→ Power infrastructure & grid data
→ News from 40+ industry sources

Built for investors, operators, and analysts who need real-time data center intelligence.

🔍 Start exploring: dchub.cloud

#DataCenter #Infrastructure #DigitalInfrastructure #CloudComputing"""

    return post.strip()


def generate_weekly_linkedin_post():
    """Generate a weekly market brief LinkedIn post."""
    stats = fetch_platform_stats()
    deals = fetch_recent_deals(limit=3)
    news = fetch_latest_news(limit=5)

    facility_count = f"{stats.get('facilities', 20000):,}"
    deal_count = stats.get('deals', 473)
    total_value = stats.get('total_value', 0)
    total_value_str = f"${total_value/1e9:.1f}B" if total_value >= 1e9 else f"${total_value/1e6:.0f}M"

    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%b %d')
    week_end = datetime.now(timezone.utc).strftime('%b %d, %Y')

    deal_lines = ""
    for i, deal in enumerate(deals[:3], 1):
        buyer = deal.get('buyer', 'Unknown')
        seller = deal.get('seller', 'Unknown')
        value = deal.get('value_usd', 0)
        value_str = f"${value/1e9:.1f}B" if value >= 1e9 else f"${value/1e6:.0f}M" if value >= 1e6 else "Undisclosed"
        deal_lines += f"\n{i}. {buyer} → {seller} ({value_str})"

    post = f"""📊 Weekly Data Center Market Brief — {week_start} to {week_end}

Key platform metrics:
✅ {facility_count} facilities tracked globally
💰 {deal_count} total transactions ({total_value_str}+)
🌍 Coverage: 140+ countries

Notable recent deals:{deal_lines if deal_lines else chr(10) + 'No new deals this week'}

DC Hub provides the most comprehensive data center intelligence platform available — free API access, AI-powered insights, and real-time market data.

📊 Full report: dchub.cloud
🤖 AI Agent: dchub.cloud/api

#DataCenter #Infrastructure #WeeklyBrief #DigitalInfrastructure #MarketIntelligence"""

    return post.strip()


def run_linkedin_post(post_type='daily'):
    """Generate and publish a LinkedIn post."""
    results = {'job': 'linkedin_post', 'type': post_type, 'published': False}

    if post_type == 'weekly':
        text = generate_weekly_linkedin_post()
    else:
        text = generate_daily_linkedin_post()

    results['post_text'] = text
    result = post_to_linkedin(text, article_url='https://dchub.cloud')

    results['published'] = result.get('success', False)
    results['post_id'] = result.get('post_id', '')
    results['details'] = result

    if result.get('success'):
        log.info(f"LinkedIn {post_type} post published: {result.get('post_id', '')}")
        # Log to DB
        _log_linkedin_post(post_type, text, result.get('post_id', ''))
    else:
        log.error(f"LinkedIn {post_type} post failed: {result.get('error', 'Unknown')}")

    return results


def _log_linkedin_post(post_type, text, post_id):
    """Log LinkedIn post to database for tracking."""
    conn, db_type = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Ensure table exists
        if db_type == 'postgres':
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_posts (
                    id SERIAL PRIMARY KEY,
                    post_type VARCHAR(50),
                    content TEXT,
                    linkedin_post_id VARCHAR(255),
                    posted_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute(
                "INSERT INTO linkedin_posts (post_type, content, linkedin_post_id) VALUES (%s, %s, %s)",
                (post_type, text[:2000], post_id)
            )
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_type TEXT,
                    content TEXT,
                    linkedin_post_id TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "INSERT INTO linkedin_posts (post_type, content, linkedin_post_id) VALUES (?, ?, ?)",
                (post_type, text[:2000], post_id)
            )
            conn.commit()
    except Exception as e:
        log.warning(f"Failed to log LinkedIn post: {e}")
    finally:
        conn.close()


# ===========================================================================
# JOB 3: DAILY MARKET BRIEF EMAIL
# ===========================================================================

def generate_market_brief_html():
    """Generate the daily market brief newsletter HTML."""
    today = datetime.now(timezone.utc).strftime('%B %d, %Y')
    news = fetch_latest_news(limit=10)
    deals = fetch_recent_deals(limit=5)
    stats = fetch_platform_stats()

    facility_count = f"{stats.get('facilities', 20000):,}"

    # Build news section
    news_html = ""
    for i, article in enumerate(news[:10]):
        title = article.get('title', 'Untitled')
        url = article.get('url', '#')
        source = article.get('source', 'Unknown')
        summary = article.get('summary', '')
        if summary and len(summary) > 150:
            summary = summary[:150] + '...'

        news_html += f"""
        <div style="padding: 14px 0; {'border-bottom: 1px solid #334155;' if i < 9 else ''}">
          <a href="{url}" style="color: #10b981; text-decoration: none; font-size: 15px; font-weight: 600; line-height: 1.4;">{title}</a>
          <div style="color: #64748b; font-size: 12px; margin-top: 4px;">
            {source}
          </div>
          {f'<div style="color: #94a3b8; font-size: 13px; margin-top: 6px; line-height: 1.4;">{summary}</div>' if summary else ''}
        </div>"""

    # Build deals section
    deals_html = ""
    for deal in deals[:5]:
        buyer = deal.get('buyer', 'Unknown')
        seller = deal.get('seller', 'Unknown')
        dtype = deal.get('deal_type', 'Transaction')
        value = deal.get('value_usd', 0)
        market = deal.get('market', '')
        capacity = deal.get('capacity_mw', 0)
        notes = deal.get('notes', '')
        value_str = f"${value/1e9:.1f}B" if value >= 1e9 else f"${value/1e6:.0f}M" if value >= 1e6 else "Undisclosed"
        headline = notes if (notes and len(notes) > 20) else f"{buyer} → {seller}"

        deals_html += f"""
        <div style="padding: 10px 0; border-bottom: 1px solid #334155;">
          <div style="color: #f1f5f9; font-size: 14px; font-weight: 600;">{headline}</div>
          <div style="color: #10b981; font-size: 12px; margin-top: 2px;">
            {dtype.title()} • {value_str}
            {f'• {market}' if market else ''}
            {f'• {capacity:.0f} MW' if capacity else ''}
          </div>
        </div>"""

    deals_section = ""
    if deals_html:
        deals_section = f"""
        <div style="background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px;">
          <h2 style="color: #f1f5f9; font-size: 18px; margin: 0 0 8px;">
            Deal Activity
          </h2>
          {deals_html}
          <div style="text-align: center; margin-top: 16px;">
            <a href="https://dchub.cloud/transactions" style="color: #10b981; font-size: 13px; text-decoration: none;">
              View all transactions
            </a>
          </div>
        </div>"""

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
    <body style="margin: 0; padding: 0; background: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
      <div style="max-width: 640px; margin: 0 auto; padding: 20px;">
        <!-- Header -->
        <div style="text-align: center; padding: 30px 0 10px;">
          <h1 style="color: #10b981; font-size: 26px; margin: 0;">DC Hub Daily Brief</h1>
          <p style="color: #94a3b8; font-size: 13px; margin: 8px 0 0;">
            Data Center Market Intelligence • {today}
          </p>
        </div>

        <!-- Quick Stats -->
        <div style="background: linear-gradient(135deg, #1e293b, #0f2a1e); border: 1px solid #334155; border-radius: 12px; padding: 20px; margin: 20px 0; text-align: center;">
          <table style="width: 100%; border-collapse: collapse;">
            <tr>
              <td style="text-align: center; padding: 8px;">
                <div style="color: #10b981; font-size: 22px; font-weight: 700;">{facility_count}</div>
                <div style="color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;">Facilities</div>
              </td>
              <td style="text-align: center; padding: 8px; border-left: 1px solid #334155;">
                <div style="color: #10b981; font-size: 22px; font-weight: 700;">{stats.get('deals', 473)}</div>
                <div style="color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;">Deals</div>
              </td>
              <td style="text-align: center; padding: 8px; border-left: 1px solid #334155;">
                <div style="color: #10b981; font-size: 22px; font-weight: 700;">140+</div>
                <div style="color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;">Countries</div>
              </td>
            </tr>
          </table>
        </div>

        <!-- Top Headlines -->
        <div style="background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px;">
          <h2 style="color: #f1f5f9; font-size: 18px; margin: 0 0 8px; display: flex; align-items: center;">
            Top Headlines
          </h2>
          {news_html if news_html else '<p style="color: #64748b;">No new articles today</p>'}
        </div>

        <!-- Deal Activity -->
        {deals_section}

        <!-- CTA -->
        <div style="text-align: center; margin: 30px 0 20px;">
          <a href="https://dchub.cloud" style="display: inline-block; background: linear-gradient(135deg, #10b981, #059669); color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px;">
            Open DC Hub Dashboard &amp;rarr;
          </a>
        </div>

        <!-- AI Tools -->
        <div style="background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 20px; text-align: center;">
          <p style="color: #94a3b8; font-size: 13px; margin: 0 0 8px;">Try DC Hub AI</p>
          <a href="https://dchub.cloud/api" style="color: #10b981; font-size: 13px; text-decoration: none; margin: 0 8px;">API</a>
          <span style="color: #334155;">•</span>
          <a href="https://dchub.cloud" style="color: #10b981; font-size: 13px; text-decoration: none; margin: 0 8px;">Chat</a>
          <span style="color: #334155;">•</span>
          <a href="https://dchub.cloud/map" style="color: #10b981; font-size: 13px; text-decoration: none; margin: 0 8px;">Map</a>
        </div>

        <!-- Footer -->
        <div style="text-align: center; padding: 20px 0; border-top: 1px solid #334155;">
          <p style="color: #64748b; font-size: 11px; margin: 0; line-height: 1.6;">
            DC Hub — The Data Center Intelligence Platform<br>
            {facility_count}+ facilities • 140+ countries • Real-time data<br><br>
            <a href="https://dchub.cloud" style="color: #10b981;">Dashboard</a> •
            <a href="https://dchub.cloud" style="color: #10b981;">Manage Preferences</a> •
            <a href="https://dchub.cloud" style="color: #10b981;">Unsubscribe</a>
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def run_market_brief():
    """Send daily market brief email to all subscribers."""
    results = {'job': 'market_brief', 'sent': 0, 'failed': 0, 'errors': []}

    subscribers = fetch_market_brief_subscribers()
    if not subscribers:
        results['message'] = 'No market brief subscribers found'
        return results

    html = generate_market_brief_html()
    today = datetime.now(timezone.utc).strftime('%b %d, %Y')
    subject = f"⚡ DC Hub Daily Brief — {today}"

    for sub in subscribers:
        email = sub.get('email')
        if not email:
            continue
        result = send_email(email, subject, html)
        if result.get('success'):
            results['sent'] += 1
        else:
            results['failed'] += 1
            results['errors'].append({'email': email, 'error': result.get('error', 'Unknown')})

    log.info(f"Market brief: {results['sent']} sent, {results['failed']} failed")
    return results


# ===========================================================================
# FLASK ROUTES (triggered by external cron)
# ===========================================================================

def _check_admin_auth():
    """Verify admin key from request."""
    if not DAILY_ADMIN_KEY:
        return True  # No key configured, allow all (dev mode)
    key = request.args.get('key') or request.json.get('admin_key', '') if request.is_json else ''
    if not key:
        key = request.headers.get('X-Admin-Key', '')
    return key == DAILY_ADMIN_KEY


@daily_bp.route('/api/v1/daily/run', methods=['POST', 'GET'])
def run_daily_jobs():
    """
    Main cron endpoint. Trigger with:
      POST /api/v1/daily/run?job=all&key=YOUR_ADMIN_KEY
    
    Jobs: all, alert_digest, linkedin_daily, linkedin_weekly, market_brief
    """
    if not _check_admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    _ensure_tables()
    job = request.args.get('job', 'all')
    results = {'timestamp': datetime.now(timezone.utc).isoformat(), 'jobs': {}}

    try:
        if job in ('all', 'alert_digest'):
            results['jobs']['alert_digest'] = run_alert_digests('daily')

        if job in ('all', 'linkedin_daily'):
            results['jobs']['linkedin_daily'] = run_linkedin_post('daily')

        if job in ('all', 'market_brief'):
            results['jobs']['market_brief'] = run_market_brief()

        if job == 'linkedin_weekly':
            results['jobs']['linkedin_weekly'] = run_linkedin_post('weekly')

        if job == 'weekly_digest':
            results['jobs']['alert_digest_weekly'] = run_alert_digests('weekly')
            results['jobs']['linkedin_weekly'] = run_linkedin_post('weekly')

    except Exception as e:
        results['error'] = str(e)
        results['traceback'] = traceback.format_exc()
        log.error(f"Daily job error: {e}")

    return jsonify(results)


@daily_bp.route('/api/v1/daily/status', methods=['GET'])
def daily_status():
    """Check configuration status of all daily automation systems."""
    # Debug: show what DB URLs the module sees
    raw_db_url = os.environ.get('DATABASE_URL', '')
    raw_neon_url = os.environ.get('NEON_DATABASE_URL', '')
    db_url_type = 'postgres' if raw_db_url.startswith(('postgresql://', 'postgres://')) else ('set-but-not-pg' if raw_db_url else 'empty')
    neon_url_type = 'postgres' if raw_neon_url.startswith(('postgresql://', 'postgres://')) else ('set-but-not-pg' if raw_neon_url else 'empty')
    
    conn, db_type = get_db_connection()
    db_ok = conn is not None
    if conn:
        conn.close()

    # Check LinkedIn token validity
    linkedin_ok = bool(LINKEDIN_ACCESS_TOKEN and LINKEDIN_ORG_ID)

    # Check SendGrid
    sendgrid_ok = bool(SENDGRID_API_KEY)

    # Check recent posts
    recent_posts = []
    if db_ok:
        conn2, db_type2 = get_db_connection()
        if conn2:
            try:
                cur = conn2.cursor()
                try:
                    if db_type2 == 'postgres':
                        cur.execute("SELECT post_type, posted_at, linkedin_post_id FROM linkedin_posts ORDER BY posted_at DESC LIMIT 5")
                    else:
                        cur.execute("SELECT post_type, posted_at, linkedin_post_id FROM linkedin_posts ORDER BY posted_at DESC LIMIT 5")
                    rows = cur.fetchall()
                    if db_type2 == 'postgres':
                        cols = [desc[0] for desc in cur.description]
                        recent_posts = [dict(zip(cols, row)) for row in rows]
                    else:
                        recent_posts = [dict(row) for row in rows]
                except:
                    pass
            finally:
                conn2.close()

    return jsonify({
        'status': 'operational' if (linkedin_ok and sendgrid_ok and db_ok) else 'degraded',
        'config': {
            'sendgrid': sendgrid_ok,
            'sendgrid_from': SENDGRID_FROM_EMAIL if sendgrid_ok else None,
            'linkedin': linkedin_ok,
            'linkedin_org_id': LINKEDIN_ORG_ID if linkedin_ok else None,
            'linkedin_api_version': LINKEDIN_API_VERSION,
            'database': db_ok,
            'db_type': db_type if db_ok else None,
            'admin_key_set': bool(DAILY_ADMIN_KEY),
            'db_debug': {
                'DATABASE_URL': db_url_type,
                'DATABASE_URL_prefix': raw_db_url[:30] + '...' if len(raw_db_url) > 30 else raw_db_url,
                'NEON_DATABASE_URL': neon_url_type,
            },
        },
        'recent_linkedin_posts': recent_posts,
        'cron_setup': {
            'daily_8am': 'POST /api/v1/daily/run?job=all&key=YOUR_KEY',
            'monday_9am': 'POST /api/v1/daily/run?job=weekly_digest&key=YOUR_KEY',
        }
    })


@daily_bp.route('/api/v1/daily/test', methods=['POST'])
def test_daily_systems():
    """Test individual systems without sending to real subscribers."""
    if not _check_admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    system = request.args.get('system', 'all')
    test_email = request.args.get('email', '')
    results = {'timestamp': datetime.now(timezone.utc).isoformat(), 'tests': {}}

    # Test SendGrid
    if system in ('all', 'email') and test_email:
        html = generate_market_brief_html()
        result = send_email(test_email, '🧪 DC Hub Test Email', html)
        results['tests']['sendgrid'] = result

    # Test LinkedIn (preview only, don't post)
    if system in ('all', 'linkedin'):
        daily_text = generate_daily_linkedin_post()
        weekly_text = generate_weekly_linkedin_post()
        results['tests']['linkedin_daily_preview'] = daily_text
        results['tests']['linkedin_weekly_preview'] = weekly_text
        results['tests']['linkedin_config'] = {
            'token_set': bool(LINKEDIN_ACCESS_TOKEN),
            'org_id': LINKEDIN_ORG_ID,
            'api_version': LINKEDIN_API_VERSION
        }

    # Test LinkedIn actual post
    if system == 'linkedin_live':
        result = run_linkedin_post('daily')
        results['tests']['linkedin_live'] = result

    # Test data fetchers
    if system in ('all', 'data'):
        results['tests']['news'] = {'count': len(fetch_latest_news(5)), 'sample': fetch_latest_news(2)}
        results['tests']['deals'] = {'count': len(fetch_recent_deals(5)), 'sample': fetch_recent_deals(2)}
        results['tests']['stats'] = fetch_platform_stats()
        results['tests']['alert_subscribers'] = {'count': len(fetch_alert_subscribers('daily'))}
        results['tests']['brief_subscribers'] = {'count': len(fetch_market_brief_subscribers())}

    return jsonify(results)


@daily_bp.route('/api/v1/daily/preview/<post_type>', methods=['GET'])
def preview_content(post_type):
    """Preview generated content without sending/posting."""
    if post_type == 'linkedin_daily':
        return jsonify({'type': 'linkedin_daily', 'text': generate_daily_linkedin_post()})
    elif post_type == 'linkedin_weekly':
        return jsonify({'type': 'linkedin_weekly', 'text': generate_weekly_linkedin_post()})
    elif post_type == 'market_brief':
        html = generate_market_brief_html()
        return html, 200, {'Content-Type': 'text/html'}
    elif post_type == 'alert_digest':
        news = fetch_latest_news(10)
        deals = fetch_recent_deals(5)
        stats = fetch_platform_stats()
        html = generate_alert_digest_html(news, deals, stats)
        return html, 200, {'Content-Type': 'text/html'}
    else:
        return jsonify({'error': f'Unknown type: {post_type}'}), 404


# ===========================================================================
# INIT TABLE FOR SUBSCRIBER MANAGEMENT
# ===========================================================================

def init_daily_tables():
    """Create tables needed for daily automation. Non-blocking with short timeout."""
    conn = None
    try:
        conn, db_type = get_db_connection()
        if not conn:
            log.warning("Daily Automation: No DB connection for table init — will retry on first use")
            return
        cur = conn.cursor()
        if db_type == 'postgres':
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_brief_subscribers (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    name VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    subscribed_at TIMESTAMP DEFAULT NOW(),
                    unsubscribed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_posts (
                    id SERIAL PRIMARY KEY,
                    post_type VARCHAR(50),
                    content TEXT,
                    linkedin_post_id VARCHAR(255),
                    posted_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_automation_log (
                    id SERIAL PRIMARY KEY,
                    job_name VARCHAR(100),
                    status VARCHAR(50),
                    details JSONB,
                    ran_at TIMESTAMP DEFAULT NOW()
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_brief_subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT,
                    is_active INTEGER DEFAULT 1,
                    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    unsubscribed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_type TEXT,
                    content TEXT,
                    linkedin_post_id TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_automation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT,
                    status TEXT,
                    details TEXT,
                    ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        log.info("Daily automation tables initialized")
    except Exception as e:
        log.warning(f"Daily table init (non-fatal): {e}")
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


# ===========================================================================
# REGISTRATION (add to main.py)
# ===========================================================================

def register_daily_automation(app):
    """
    Register daily automation blueprint.
    
    Add to main.py:
    
        from dchub_daily_automation import register_daily_automation
        register_daily_automation(app)
    """
    init_daily_tables()
    app.register_blueprint(daily_bp)
    print("📧 Daily Automation Engine: ✅ Registered")
    print("   📍 POST /api/v1/daily/run?job=all        — Trigger all daily jobs")
    print("   📍 GET  /api/v1/daily/status              — Check config status")
    print("   📍 POST /api/v1/daily/test?system=all     — Test systems")
    print("   📍 GET  /api/v1/daily/preview/<type>      — Preview content")
    return daily_bp
