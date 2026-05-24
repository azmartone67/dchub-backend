"""
DC Hub Alert Processor
======================
Background job that scans news/facilities and sends email notifications
when alert conditions are matched.

Run Options:
1. Cron job: python alert_processor.py
2. API endpoint: POST /api/v1/alerts/process (for manual trigger)
3. Scheduled task in Replit

Email Providers Supported:
- SendGrid (recommended)
- Mailgun
- SMTP (Gmail, etc.)
"""

from flask import Blueprint, request, jsonify
import json
import os
import re
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from collections import defaultdict
from db_utils import get_db

alert_processor_bp = Blueprint('alert_processor', __name__)

# =============================================================================
# Configuration
# =============================================================================

EMAIL_PROVIDER = os.environ.get('EMAIL_PROVIDER', 'sendgrid')
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
MAILGUN_API_KEY = os.environ.get('MAILGUN_API_KEY', '')
MAILGUN_DOMAIN = os.environ.get('MAILGUN_DOMAIN', '')
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')

FROM_EMAIL = os.environ.get('FROM_EMAIL', 'info@dchub.cloud')
FROM_NAME = os.environ.get('FROM_NAME', 'DC Hub Alerts')

# =============================================================================
# Database Helpers
# =============================================================================

def get_alert_db():
    """Get database connection for alerts (dchub.db)."""
    return get_db('dchub.db')

def get_content_db():
    """Get database connection for content (dc_nexus.db)."""
    db_path = 'dc_nexus.db' if os.path.exists('dc_nexus.db') else 'dchub.db'
    return get_db(db_path)

def init_alert_log_db():
    """Initialize alert log table for tracking sent notifications."""
    db = get_db('dchub.db')
    cursor = db.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_notifications (
            id SERIAL PRIMARY KEY,
            alert_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            matches_json TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'sent',
            FOREIGN KEY (alert_id) REFERENCES simple_alerts(id)
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_notif_alert ON alert_notifications(alert_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_notif_email ON alert_notifications(email)')
    
    db.commit()
    db.close()
    print("✅ Alert notifications log initialized")

# Initialize on import
init_alert_log_db()

# =============================================================================
# Email Sending Functions
# =============================================================================

def send_email_sendgrid(to_email, subject, html_content, text_content=None):
    """Send email via SendGrid API."""
    if not SENDGRID_API_KEY:
        print("⚠️ SendGrid API key not configured")
        return False, "SendGrid API key not configured"
    
    url = "https://api.sendgrid.com/v3/mail/send"
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/html", "value": html_content}
        ]
    }
    
    if text_content:
        data["content"].insert(0, {"type": "text/plain", "value": text_content})
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        if response.status_code in (200, 202):
            return True, "Sent via SendGrid"
        else:
            return False, f"SendGrid error: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"SendGrid exception: {str(e)}"

def send_email_mailgun(to_email, subject, html_content, text_content=None):
    """Send email via Mailgun API."""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        print("⚠️ Mailgun credentials not configured")
        return False, "Mailgun credentials not configured"
    
    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    
    data = {
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": to_email,
        "subject": subject,
        "html": html_content
    }
    
    if text_content:
        data["text"] = text_content
    
    try:
        response = requests.post(
            url, 
            auth=("api", MAILGUN_API_KEY),
            data=data,
            timeout=10
        )
        if response.status_code == 200:
            return True, "Sent via Mailgun"
        else:
            return False, f"Mailgun error: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"Mailgun exception: {str(e)}"

def send_email_smtp(to_email, subject, html_content, text_content=None):
    """Send email via SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️ SMTP credentials not configured")
        return False, "SMTP credentials not configured"
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg['To'] = to_email
        
        if text_content:
            msg.attach(MIMEText(text_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())
        
        return True, "Sent via SMTP"
    except Exception as e:
        return False, f"SMTP exception: {str(e)}"

def send_email(to_email, subject, html_content, text_content=None):
    """Send email using configured provider."""
    if EMAIL_PROVIDER == 'sendgrid':
        return send_email_sendgrid(to_email, subject, html_content, text_content)
    elif EMAIL_PROVIDER == 'mailgun':
        return send_email_mailgun(to_email, subject, html_content, text_content)
    elif EMAIL_PROVIDER == 'smtp':
        return send_email_smtp(to_email, subject, html_content, text_content)
    else:
        return False, f"Unknown email provider: {EMAIL_PROVIDER}"

# =============================================================================
# Content Fetchers
# =============================================================================

def get_recent_news(hours=24):
    """Fetch recent news articles from the database."""
    db = get_content_db()
    
    # Try announcements table first (DC Hub schema)
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        c = db.cursor()
        news = c.execute('''
            SELECT id, title, summary as description, source, source_url as url, 
                   published_date as published_at, content
            FROM announcements 
            WHERE published_date > %s OR discovered_at > %s
            ORDER BY published_date DESC
            LIMIT 100
        ''', (cutoff.isoformat(), cutoff.isoformat())).fetchall()
        
        if news:
            db.close()
            return [dict(n) for n in news]
    except Exception:
        pass
    
    # Fallback: try news table (alternate schema)
    try:
        c = db.cursor()
        news = c.execute('''
            SELECT id, title, description, source, url, published_at, content
            FROM news 
            WHERE published_at > %s OR created_at > %s
            ORDER BY published_at DESC
            LIMIT 100
        ''', (cutoff.isoformat(), cutoff.isoformat())).fetchall()
        
        db.close()
        return [dict(n) for n in news]
    except Exception:
        db.close()
        return []

def get_recent_facilities(hours=24):
    """Fetch recently added/updated facilities."""
    db = get_content_db()
    
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        c = db.cursor()
        facilities = c.execute('''
            SELECT id, name, provider as operator, city, state, country, power_mw as capacity_mw, status
            FROM facilities 
            WHERE last_updated > %s OR first_seen > %s
            ORDER BY last_updated DESC
            LIMIT 100
        ''', (cutoff.isoformat(), cutoff.isoformat())).fetchall()
        
        db.close()
        return [dict(f) for f in facilities]
    except Exception:
        db.close()
        return []

def get_recent_pipeline(hours=24):
    """Fetch recently updated pipeline projects."""
    db = get_content_db()
    
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        c = db.cursor()
        projects = c.execute('''
            SELECT id, notes as name, operator, market, region, capacity_mw, status, announcement_date
            FROM capacity_pipeline 
            WHERE created_at > %s OR announcement_date > %s
            ORDER BY created_at DESC
            LIMIT 100
        ''', (cutoff.isoformat(), cutoff.isoformat())).fetchall()
        
        db.close()
        return [dict(p) for p in projects]
    except Exception:
        db.close()
        return []

# =============================================================================
# Alert Matching Functions
# =============================================================================

def match_operator_watch(alert_config, news, facilities, pipeline):
    """Match operator watch alerts."""
    operators = [op.lower() for op in alert_config.get('operators', [])]
    matches = []
    
    # Check news
    for item in news:
        text = f"{item.get('title', '')} {item.get('description', '')} {item.get('content', '')}".lower()
        for op in operators:
            if op in text:
                matches.append({
                    'type': 'news',
                    'operator': op,
                    'title': item.get('title', 'Untitled'),
                    'url': item.get('url', ''),
                    'source': item.get('source', 'Unknown'),
                    'date': item.get('published_at', '')
                })
                break
    
    # Check facilities
    for item in facilities:
        facility_op = (item.get('operator') or '').lower()
        for op in operators:
            if op in facility_op:
                matches.append({
                    'type': 'facility',
                    'operator': item.get('operator', ''),
                    'name': item.get('name', ''),
                    'location': f"{item.get('city', '')}, {item.get('state', '')}",
                    'capacity_mw': item.get('capacity_mw')
                })
                break
    
    # Check pipeline
    for item in pipeline:
        pipeline_op = (item.get('operator') or '').lower()
        for op in operators:
            if op in pipeline_op:
                matches.append({
                    'type': 'pipeline',
                    'operator': item.get('operator', ''),
                    'name': item.get('name', ''),
                    'market': item.get('market', ''),
                    'capacity_mw': item.get('capacity_mw'),
                    'status': item.get('status', '')
                })
                break
    
    return matches

def match_market_watch(alert_config, news, facilities, pipeline):
    """Match market watch alerts."""
    markets = [m.lower() for m in alert_config.get('markets', [])]
    matches = []
    
    # Check news
    for item in news:
        text = f"{item.get('title', '')} {item.get('description', '')} {item.get('content', '')}".lower()
        for market in markets:
            if market in text:
                matches.append({
                    'type': 'news',
                    'market': market,
                    'title': item.get('title', 'Untitled'),
                    'url': item.get('url', ''),
                    'source': item.get('source', 'Unknown')
                })
                break
    
    # Check facilities
    for item in facilities:
        location = f"{item.get('city', '')} {item.get('state', '')} {item.get('country', '')}".lower()
        for market in markets:
            if market in location:
                matches.append({
                    'type': 'facility',
                    'market': market,
                    'name': item.get('name', ''),
                    'operator': item.get('operator', ''),
                    'capacity_mw': item.get('capacity_mw')
                })
                break
    
    # Check pipeline
    for item in pipeline:
        pipeline_market = (item.get('market') or '').lower()
        for market in markets:
            if market in pipeline_market:
                matches.append({
                    'type': 'pipeline',
                    'market': item.get('market', ''),
                    'name': item.get('name', ''),
                    'operator': item.get('operator', ''),
                    'capacity_mw': item.get('capacity_mw')
                })
                break
    
    return matches

def match_capacity_threshold(alert_config, news, facilities, pipeline):
    """Match capacity threshold alerts."""
    min_mw = alert_config.get('min_mw', 0)
    max_mw = alert_config.get('max_mw', float('inf'))
    matches = []
    
    # Check facilities
    for item in facilities:
        capacity = item.get('capacity_mw') or 0
        if min_mw <= capacity <= max_mw:
            matches.append({
                'type': 'facility',
                'name': item.get('name', ''),
                'operator': item.get('operator', ''),
                'capacity_mw': capacity,
                'location': f"{item.get('city', '')}, {item.get('state', '')}"
            })
    
    # Check pipeline
    for item in pipeline:
        capacity = item.get('capacity_mw') or 0
        if min_mw <= capacity <= max_mw:
            matches.append({
                'type': 'pipeline',
                'name': item.get('name', ''),
                'operator': item.get('operator', ''),
                'capacity_mw': capacity,
                'market': item.get('market', ''),
                'status': item.get('status', '')
            })
    
    # Check news for MW mentions
    mw_pattern = re.compile(r'(\d+(%s:,\d+)%s(%s:\.\d+)%s)\s*(%s:MW|megawatt)', re.IGNORECASE)
    for item in news:
        text = f"{item.get('title', '')} {item.get('description', '')} {item.get('content', '')}"
        mw_matches = mw_pattern.findall(text)
        for mw_str in mw_matches:
            mw = float(mw_str.replace(',', ''))
            if min_mw <= mw <= max_mw:
                matches.append({
                    'type': 'news',
                    'title': item.get('title', 'Untitled'),
                    'capacity_mw': mw,
                    'url': item.get('url', ''),
                    'source': item.get('source', 'Unknown')
                })
                break
    
    return matches

def match_keyword_watch(alert_config, news, facilities, pipeline):
    """Match keyword watch alerts."""
    keywords = [kw.lower() for kw in alert_config.get('keywords', [])]
    matches = []
    
    # Check news
    for item in news:
        text = f"{item.get('title', '')} {item.get('description', '')} {item.get('content', '')}".lower()
        matched_keywords = [kw for kw in keywords if kw in text]
        if matched_keywords:
            matches.append({
                'type': 'news',
                'keywords': matched_keywords,
                'title': item.get('title', 'Untitled'),
                'url': item.get('url', ''),
                'source': item.get('source', 'Unknown')
            })
    
    return matches

def process_alert(alert, news, facilities, pipeline):
    """Process a single alert and return matches."""
    alert_type = alert['alert_type']
    config = json.loads(alert['config']) if isinstance(alert['config'], str) else alert['config']
    
    if alert_type == 'operator_watch':
        return match_operator_watch(config, news, facilities, pipeline)
    elif alert_type == 'market_watch':
        return match_market_watch(config, news, facilities, pipeline)
    elif alert_type == 'capacity_threshold':
        return match_capacity_threshold(config, news, facilities, pipeline)
    elif alert_type == 'keyword_watch':
        return match_keyword_watch(config, news, facilities, pipeline)
    else:
        return []

# =============================================================================
# Email Template
# =============================================================================

def generate_email_html(alert_name, alert_type, matches):
    """Generate HTML email for alert notification."""
    type_icons = {
        'operator_watch': '🏢',
        'market_watch': '📍',
        'capacity_threshold': '⚡',
        'keyword_watch': '🔍'
    }
    
    icon = type_icons.get(alert_type, '🔔')
    
    matches_html = ""
    for match in matches[:10]:  # Limit to 10 matches per email
        match_type = match.get('type', 'item')
        
        if match_type == 'news':
            title = match.get('title', 'Untitled')
            url = match.get('url', '#')
            source = match.get('source', 'Unknown')
            matches_html += f'''
            <div style="padding: 12px; background: #1e293b; border-radius: 8px; margin-bottom: 8px;">
                <div style="color: #10b981; font-size: 12px; margin-bottom: 4px;">📰 NEWS</div>
                <a href="{url}" style="color: #f1f5f9; text-decoration: none; font-weight: 600;">{title}</a>
                <div style="color: #94a3b8; font-size: 12px; margin-top: 4px;">Source: {source}</div>
            </div>
            '''
        elif match_type == 'facility':
            name = match.get('name', 'Unknown')
            operator = match.get('operator', '')
            location = match.get('location', '')
            capacity = match.get('capacity_mw', '')
            matches_html += f'''
            <div style="padding: 12px; background: #1e293b; border-radius: 8px; margin-bottom: 8px;">
                <div style="color: #3b82f6; font-size: 12px; margin-bottom: 4px;">🏭 FACILITY</div>
                <div style="color: #f1f5f9; font-weight: 600;">{name}</div>
                <div style="color: #94a3b8; font-size: 13px;">{operator} • {location}</div>
                {f'<div style="color: #10b981; font-size: 12px; margin-top: 4px;">{capacity} MW</div>' if capacity else ''}
            </div>
            '''
        elif match_type == 'pipeline':
            name = match.get('name', 'Unknown')
            operator = match.get('operator', '')
            market = match.get('market', '')
            capacity = match.get('capacity_mw', '')
            status = match.get('status', '')
            matches_html += f'''
            <div style="padding: 12px; background: #1e293b; border-radius: 8px; margin-bottom: 8px;">
                <div style="color: #f59e0b; font-size: 12px; margin-bottom: 4px;">🚀 PIPELINE</div>
                <div style="color: #f1f5f9; font-weight: 600;">{name}</div>
                <div style="color: #94a3b8; font-size: 13px;">{operator} • {market}</div>
                <div style="display: flex; gap: 12px; margin-top: 4px;">
                    {f'<span style="color: #10b981; font-size: 12px;">{capacity} MW</span>' if capacity else ''}
                    {f'<span style="color: #94a3b8; font-size: 12px;">{status}</span>' if status else ''}
                </div>
            </div>
            '''
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 0; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <!-- Header -->
            <div style="text-align: center; padding: 20px 0;">
                <h1 style="color: #f1f5f9; margin: 0; font-size: 24px;">
                    {icon} DC Hub Alert
                </h1>
            </div>
            
            <!-- Alert Info -->
            <div style="background: #334155; border-radius: 12px; padding: 20px; margin-bottom: 20px;">
                <h2 style="color: #f1f5f9; margin: 0 0 8px 0; font-size: 18px;">{alert_name}</h2>
                <p style="color: #94a3b8; margin: 0; font-size: 14px;">
                    {len(matches)} new match{'es' if len(matches) != 1 else ''} found
                </p>
            </div>
            
            <!-- Matches -->
            <div style="margin-bottom: 20px;">
                {matches_html}
            </div>
            
            {f'<p style="color: #64748b; font-size: 12px; text-align: center;">+ {len(matches) - 10} more matches</p>' if len(matches) > 10 else ''}
            
            <!-- CTA -->
            <div style="text-align: center; margin: 30px 0;">
                <a href="https://dchub.cloud" style="display: inline-block; background: #10b981; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600;">
                    View on DC Hub →
                </a>
            </div>
            
            <!-- Footer -->
            <div style="text-align: center; padding: 20px 0; border-top: 1px solid #334155;">
                <p style="color: #64748b; font-size: 12px; margin: 0;">
                    You're receiving this because you set up an alert on DC Hub.<br>
                    <a href="https://dchub.cloud" style="color: #10b981;">Manage your alerts</a>
                </p>
            </div>
        </div>
    </body>
    </html>
    '''
    
    return html

def generate_email_text(alert_name, matches):
    """Generate plain text email for alert notification."""
    text = f"DC Hub Alert: {alert_name}\n"
    text += f"{len(matches)} new match{'es' if len(matches) != 1 else ''} found\n\n"
    
    for match in matches[:10]:
        match_type = match.get('type', 'item')
        if match_type == 'news':
            text += f"📰 NEWS: {match.get('title', 'Untitled')}\n"
            text += f"   {match.get('url', '')}\n\n"
        elif match_type == 'facility':
            text += f"🏭 FACILITY: {match.get('name', 'Unknown')}\n"
            text += f"   {match.get('operator', '')} - {match.get('location', '')}\n\n"
        elif match_type == 'pipeline':
            text += f"🚀 PIPELINE: {match.get('name', 'Unknown')}\n"
            text += f"   {match.get('operator', '')} - {match.get('market', '')}\n\n"
    
    text += "\nView more at: https://dchub.cloud"
    
    return text

# =============================================================================
# Main Processing Functions
# =============================================================================

def should_process_alert(alert, frequency_filter=None):
    """Check if an alert should be processed based on frequency."""
    if not alert['is_active']:
        return False
    
    frequency = alert['frequency']
    
    if frequency_filter and frequency != frequency_filter:
        return False
    
    last_triggered = alert.get('last_triggered')
    if not last_triggered:
        return True
    
    try:
        last_dt = datetime.fromisoformat(last_triggered)
    except:
        return True
    
    now = datetime.now()
    
    if frequency == 'immediate':
        # Process every run (but dedupe by matches)
        return True
    elif frequency == 'daily':
        return (now - last_dt) >= timedelta(hours=23)
    elif frequency == 'weekly':
        return (now - last_dt) >= timedelta(days=6, hours=23)
    
    return True

def process_all_alerts(frequency_filter=None, dry_run=False):
    """Process all active alerts and send notifications."""
    print(f"\n{'='*50}")
    print(f"DC Hub Alert Processor - {datetime.now().isoformat()}")
    print(f"{'='*50}")
    
    # Fetch recent content
    print("\n📥 Fetching recent content...")
    news = get_recent_news(hours=168)  # 7 days
    facilities = get_recent_facilities(hours=168)
    pipeline = get_recent_pipeline(hours=168)
    
    print(f"   News articles: {len(news)}")
    print(f"   Facilities: {len(facilities)}")
    print(f"   Pipeline projects: {len(pipeline)}")
    
    if not any([news, facilities, pipeline]):
        print("⚠️ No recent content to process")
        return {'processed': 0, 'notifications_sent': 0, 'errors': []}
    
    # Fetch all active alerts
    db = get_alert_db()
    c = db.cursor()
    alerts = c.execute('''
        SELECT * FROM simple_alerts 
        WHERE is_active = 1
        ORDER BY email, id
    ''').fetchall()
    
    print(f"\n📋 Found {len(alerts)} active alerts")
    
    results = {
        'processed': 0,
        'notifications_sent': 0,
        'matches_found': 0,
        'errors': [],
        'details': []
    }
    
    # Group by email for batching
    email_alerts = defaultdict(list)
    for alert in alerts:
        if should_process_alert(dict(alert), frequency_filter):
            email_alerts[alert['email']].append(dict(alert))
    
    print(f"   Alerts to process: {sum(len(a) for a in email_alerts.values())}")
    
    # Process each email's alerts
    for email, user_alerts in email_alerts.items():
        print(f"\n👤 Processing alerts for: {email}")
        
        for alert in user_alerts:
            alert_id = alert['id']
            alert_name = alert['name']
            alert_type = alert['alert_type']
            
            print(f"   🔔 {alert_name} ({alert_type})")
            
            # Find matches
            matches = process_alert(alert, news, facilities, pipeline)
            results['processed'] += 1
            
            if not matches:
                print(f"      No matches")
                continue
            
            print(f"      ✅ {len(matches)} matches found")
            results['matches_found'] += len(matches)
            
            if dry_run:
                print(f"      [DRY RUN] Would send email")
                results['details'].append({
                    'alert_id': alert_id,
                    'email': email,
                    'matches': len(matches),
                    'status': 'dry_run'
                })
                continue
            
            # Generate and send email
            html_content = generate_email_html(alert_name, alert_type, matches)
            text_content = generate_email_text(alert_name, matches)
            subject = f"🔔 DC Hub Alert: {alert_name} ({len(matches)} matches)"
            
            success, message = send_email(email, subject, html_content, text_content)
            
            if success:
                print(f"      📧 Email sent!")
                results['notifications_sent'] += 1
                
                # Update alert last_triggered
                c = db.cursor()
                c.execute('''
                    UPDATE simple_alerts 
                    SET last_triggered = %s, trigger_count = trigger_count + 1
                    WHERE id = %s
                ''', (datetime.now().isoformat(), alert_id))
                
                # Log notification
                c = db.cursor()
                c.execute('''
                    INSERT INTO alert_notifications (alert_id, email, matches_json, status)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                ''', (alert_id, email, json.dumps(matches[:10]), 'sent'))
                
            else:
                print(f"      ❌ Failed: {message}")
                results['errors'].append({
                    'alert_id': alert_id,
                    'email': email,
                    'error': message
                })
            
            results['details'].append({
                'alert_id': alert_id,
                'email': email,
                'matches': len(matches),
                'status': 'sent' if success else 'failed'
            })
    
    db.commit()
    db.close()
    
    print(f"\n{'='*50}")
    print(f"✅ Processing complete!")
    print(f"   Alerts processed: {results['processed']}")
    print(f"   Matches found: {results['matches_found']}")
    print(f"   Notifications sent: {results['notifications_sent']}")
    print(f"   Errors: {len(results['errors'])}")
    print(f"{'='*50}\n")
    
    return results

# =============================================================================
# API Endpoints
# =============================================================================

@alert_processor_bp.route('/api/v1/alerts/process', methods=['POST'])
def api_process_alerts():
    """Manually trigger alert processing."""
    data = request.get_json() or {}
    
    # Optional: require admin key
    admin_key = data.get('admin_key', '')
    expected_key = os.environ.get('DCHUB_ADMIN_KEY', '')
    
    if expected_key and admin_key != expected_key:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    frequency_filter = data.get('frequency')  # None = all, or 'immediate', 'daily', 'weekly'
    dry_run = data.get('dry_run', False)
    
    try:
        results = process_all_alerts(frequency_filter=frequency_filter, dry_run=dry_run)
        return jsonify({
            'success': True,
            'results': results
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@alert_processor_bp.route('/api/v1/alerts/status', methods=['GET'])
def api_alert_status():
    """Get alert processing status and stats."""
    db = get_alert_db()
    
    # Get alert counts
    c = db.cursor()
    total = c.execute('SELECT COUNT(*) as cnt FROM simple_alerts').fetchone()['cnt']
    c = db.cursor()
    active = c.execute('SELECT COUNT(*) as cnt FROM simple_alerts WHERE is_active = 1').fetchone()['cnt']
    
    # Get notification stats
    try:
        c = db.cursor()
        sent_today = c.execute('''
            SELECT COUNT(*) as cnt FROM alert_notifications 
            WHERE date(sent_at) = date('now')
        ''').fetchone()['cnt']
        
        c = db.cursor()
        sent_week = c.execute('''
            SELECT COUNT(*) as cnt FROM alert_notifications 
            WHERE sent_at > datetime('now', '-7 days')
        ''').fetchone()['cnt']
    except:
        sent_today = 0
        sent_week = 0
    
    db.close()
    
    return jsonify({
        'success': True,
        'stats': {
            'total_alerts': total,
            'active_alerts': active,
            'notifications_today': sent_today,
            'notifications_this_week': sent_week
        },
        'email_provider': EMAIL_PROVIDER,
        'configured': bool(SENDGRID_API_KEY or MAILGUN_API_KEY or SMTP_USER)
    })

@alert_processor_bp.route('/api/v1/alerts/test-email', methods=['POST'])
def api_test_email():
    """Send a test email to verify configuration."""
    data = request.get_json() or {}
    email = data.get('email', '')
    
    if not email or '@' not in email:
        return jsonify({'success': False, 'error': 'Valid email required'}), 400
    
    html = """
    <div style="font-family: sans-serif; padding: 20px;">
        <h1 style="color: #10b981;">✅ DC Hub Email Test</h1>
        <p>If you're reading this, your email configuration is working!</p>
        <p>Provider: """ + EMAIL_PROVIDER + """</p>
    </div>
    """
    
    success, message = send_email(email, "DC Hub Email Test", html, "DC Hub email test - configuration working!")
    
    return jsonify({
        'success': success,
        'message': message,
        'provider': EMAIL_PROVIDER
    })

# =============================================================================
# Blueprint Registration
# =============================================================================

def register_alert_processor(app):
    """Register alert processor blueprint."""
    app.register_blueprint(alert_processor_bp)
    print("✅ Alert Processor API registered at /api/v1/alerts")

# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='DC Hub Alert Processor')
    parser.add_argument('--frequency', choices=['immediate', 'daily', 'weekly'], 
                       help='Only process alerts with this frequency')
    parser.add_argument('--dry-run', action='store_true',
                       help='Run without sending emails')
    
    args = parser.parse_args()
    
    process_all_alerts(frequency_filter=args.frequency, dry_run=args.dry_run)
