"""
DC Hub Alert System
===================
Comprehensive alert management for data center intelligence.

Alert Types:
- operator_watch: Monitor specific operators (e.g., "Alert me when Google announces")
- market_watch: Monitor specific markets (e.g., "Alert me for Phoenix")
- capacity_threshold: Monitor MW thresholds (e.g., "Alert me for >100MW projects")
- keyword_watch: Monitor keywords in news (e.g., "Alert me for 'nuclear' mentions")

Endpoints:
- POST   /api/v1/alerts          - Create new alert
- GET    /api/v1/alerts          - List user's alerts
- GET    /api/v1/alerts/<id>     - Get specific alert
- PUT    /api/v1/alerts/<id>     - Update alert
- DELETE /api/v1/alerts/<id>     - Delete alert
- POST   /api/v1/alerts/test/<id> - Send test notification
- GET    /api/v1/alerts/history  - Get notification history
"""

from flask import Blueprint, request, jsonify, g
from functools import wraps
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import hashlib
import secrets
from db_utils import get_db

alerts_bp = Blueprint('alerts', __name__)

# =============================================================================
# Database Setup
# =============================================================================

def get_alerts_db():
    """Get database connection with row factory."""
    if 'db' not in g:
        g.db = get_db('dchub.db')
    return g.db

def init_alerts_db():
    """Initialize alerts tables."""
    db = get_db('dchub.db')
    cursor = db.cursor()
    
    # Alerts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            alert_type TEXT NOT NULL CHECK(alert_type IN ('operator_watch', 'market_watch', 'capacity_threshold', 'keyword_watch')),
            name TEXT NOT NULL,
            config TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            frequency TEXT DEFAULT 'immediate' CHECK(frequency IN ('immediate', 'daily', 'weekly')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered TIMESTAMP,
            trigger_count INTEGER DEFAULT 0
        )
    ''')
    
    # Alert history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_history (
            id SERIAL PRIMARY KEY,
            alert_id INTEGER NOT NULL,
            triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            trigger_reason TEXT NOT NULL,
            notification_sent INTEGER DEFAULT 0,
            notification_error TEXT,
            matched_data TEXT,
            FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
        )
    ''')
    
    # Email verification tokens
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_verifications (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            verified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    ''')
    
    # Create indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(is_active)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_history_alert ON alert_history(alert_id)')
    
    db.commit()
    db.close()
    print("✅ Alerts database initialized")

# Initialize on import
init_alerts_db()

# =============================================================================
# Email Configuration
# =============================================================================

SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'info@dchub.cloud')
FROM_NAME = os.environ.get('FROM_NAME', 'DC Hub Alerts')

def send_email(to_email: str, subject: str, html_body: str, text_body: str = None) -> dict:
    """Send email notification."""
    if not SMTP_USER or not SMTP_PASS:
        return {'success': False, 'error': 'Email not configured. Set SMTP_USER and SMTP_PASS.'}
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg['To'] = to_email
        
        # Plain text fallback
        if text_body:
            msg.attach(MIMEText(text_body, 'plain'))
        
        # HTML body
        msg.attach(MIMEText(html_body, 'html'))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def generate_alert_email(alert: dict, trigger_data: dict) -> tuple:
    """Generate HTML and text email for alert notification."""
    alert_type_labels = {
        'operator_watch': '🏢 Operator Alert',
        'market_watch': '📍 Market Alert',
        'capacity_threshold': '⚡ Capacity Alert',
        'keyword_watch': '🔍 Keyword Alert'
    }
    
    type_label = alert_type_labels.get(alert['alert_type'], '🔔 Alert')
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
            .content {{ background: #f8f9fa; padding: 20px; border: 1px solid #e9ecef; }}
            .footer {{ background: #1a1a2e; color: #888; padding: 15px; text-align: center; font-size: 12px; border-radius: 0 0 8px 8px; }}
            .highlight {{ background: #e3f2fd; padding: 15px; border-left: 4px solid #2196f3; margin: 15px 0; }}
            .btn {{ display: inline-block; background: #2196f3; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px; }}
            .meta {{ color: #666; font-size: 14px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin:0;">{type_label}</h1>
                <p style="margin:5px 0 0 0; opacity: 0.9;">{alert['name']}</p>
            </div>
            <div class="content">
                <div class="highlight">
                    <strong>Trigger Reason:</strong><br>
                    {trigger_data.get('reason', 'Alert condition met')}
                </div>
                
                {f"<p><strong>Matched Content:</strong><br>{trigger_data.get('matched_content', '')}</p>" if trigger_data.get('matched_content') else ''}
                
                {f"<p><strong>Source:</strong> <a href='{trigger_data.get('source_url', '#')}'>{trigger_data.get('source_title', 'View Source')}</a></p>" if trigger_data.get('source_url') else ''}
                
                <p class="meta">
                    Alert created: {alert.get('created_at', 'N/A')}<br>
                    Times triggered: {alert.get('trigger_count', 0) + 1}
                </p>
                
                <p>
                    <a href="https://dchub.cloud/alerts" class="btn">Manage Alerts</a>
                </p>
            </div>
            <div class="footer">
                <p>DC Hub - Data Center Intelligence Platform</p>
                <p><a href="https://dchub.cloud/alerts/unsubscribe%sid={alert['id']}" style="color: #888;">Unsubscribe from this alert</a></p>
            </div>
        </div>
    </body>
    </html>
    '''
    
    text = f'''
{type_label}: {alert['name']}

Trigger Reason: {trigger_data.get('reason', 'Alert condition met')}

{f"Matched Content: {trigger_data.get('matched_content', '')}" if trigger_data.get('matched_content') else ''}

{f"Source: {trigger_data.get('source_url', '')}" if trigger_data.get('source_url') else ''}

---
Manage your alerts: https://dchub.cloud/alerts
Unsubscribe: https://dchub.cloud/alerts/unsubscribe%sid={alert['id']}
    '''
    
    return html, text

# =============================================================================
# Authentication Helper
# =============================================================================

def get_user_from_request():
    """Extract user info from request (Google OAuth or API key)."""
    # Check for API key
    api_key = request.headers.get('X-API-Key')
    if api_key:
        # Validate API key and return user_id
        db = get_alerts_db()
        c = db.cursor()
        user = c.execute('SELECT user_id, email FROM api_keys WHERE key = %s AND is_active = 1', (api_key,)).fetchone()
        if user:
            return {'user_id': user['user_id'], 'email': user['email']}
    
    # Check for session (Google OAuth)
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header[7:]
        # In production, validate with Google OAuth
        # For now, decode from token or session
        db = get_alerts_db()
        c = db.cursor()
        session = c.execute('SELECT user_id, email FROM sessions WHERE token = %s AND expires_at > %s', 
                            (token, datetime.utcnow())).fetchone()
        if session:
            return {'user_id': session['user_id'], 'email': session['email']}
    
    # Demo mode - use email from request body or query
    email = request.json.get('email') if request.is_json else request.args.get('email')
    if email:
        user_id = hashlib.sha256(email.encode()).hexdigest()[:16]
        return {'user_id': user_id, 'email': email}
    
    return None

def require_auth(f):
    """Decorator to require authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_user_from_request()
        if not user:
            return jsonify({'error': 'Authentication required', 'code': 'AUTH_REQUIRED'}), 401
        g.user = user
        return f(*args, **kwargs)
    return decorated

# =============================================================================
# Alert Validation
# =============================================================================

def validate_alert_config(alert_type: str, config: dict) -> tuple:
    """Validate alert configuration based on type."""
    errors = []
    
    if alert_type == 'operator_watch':
        if not config.get('operators'):
            errors.append('operators: At least one operator required')
        elif not isinstance(config['operators'], list):
            errors.append('operators: Must be a list')
    
    elif alert_type == 'market_watch':
        if not config.get('markets'):
            errors.append('markets: At least one market required')
        elif not isinstance(config['markets'], list):
            errors.append('markets: Must be a list')
    
    elif alert_type == 'capacity_threshold':
        if 'min_mw' not in config and 'max_mw' not in config:
            errors.append('capacity_threshold: At least min_mw or max_mw required')
        if config.get('min_mw') and not isinstance(config['min_mw'], (int, float)):
            errors.append('min_mw: Must be a number')
        if config.get('max_mw') and not isinstance(config['max_mw'], (int, float)):
            errors.append('max_mw: Must be a number')
    
    elif alert_type == 'keyword_watch':
        if not config.get('keywords'):
            errors.append('keywords: At least one keyword required')
        elif not isinstance(config['keywords'], list):
            errors.append('keywords: Must be a list')
    
    else:
        errors.append(f'Invalid alert_type: {alert_type}')
    
    return len(errors) == 0, errors

# =============================================================================
# API Endpoints
# =============================================================================

@alerts_bp.route('/api/v1/alerts', methods=['POST'])
@require_auth
def create_alert():
    """Create a new alert."""
    data = request.get_json()
    
    # Validate required fields
    required = ['alert_type', 'name', 'config']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400
    
    # Validate alert type and config
    valid, errors = validate_alert_config(data['alert_type'], data['config'])
    if not valid:
        return jsonify({'error': 'Invalid configuration', 'details': errors}), 400
    
    # Validate frequency
    frequency = data.get('frequency', 'immediate')
    if frequency not in ('immediate', 'daily', 'weekly'):
        return jsonify({'error': 'Invalid frequency. Must be: immediate, daily, or weekly'}), 400
    
    db = get_alerts_db()
    cursor = db.cursor()
    
    cursor.execute('''
        INSERT INTO alerts (user_id, user_email, alert_type, name, config, frequency, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (
        g.user['user_id'],
        g.user['email'],
        data['alert_type'],
        data['name'],
        json.dumps(data['config']),
        frequency,
        1 if data.get('is_active', True) else 0
    ))
    
    db.commit()
    alert_id = cursor.lastrowid
    
    # Fetch the created alert
    alert = cursor.execute('SELECT * FROM alerts WHERE id = %s', (alert_id,)).fetchone()
    
    return jsonify({
        'success': True,
        'alert': dict(alert),
        'message': f'Alert "{data["name"]}" created successfully'
    }), 201

@alerts_bp.route('/api/v1/alerts', methods=['GET'])
@require_auth
def list_alerts():
    """List user's alerts."""
    db = get_alerts_db()
    
    # Filter options
    alert_type = request.args.get('type')
    is_active = request.args.get('active')
    
    query = 'SELECT * FROM alerts WHERE user_id = %s'
    params = [g.user['user_id']]
    
    if alert_type:
        query += ' AND alert_type = %s'
        params.append(alert_type)
    
    if is_active is not None:
        query += ' AND is_active = %s'
        params.append(1 if is_active.lower() == 'true' else 0)
    
    query += ' ORDER BY created_at DESC'
    
    c = db.cursor()
    alerts = c.execute(query, params).fetchall()
    
    return jsonify({
        'alerts': [dict(a) for a in alerts],
        'count': len(alerts)
    })

@alerts_bp.route('/api/v1/alerts/<int:alert_id>', methods=['GET'])
@require_auth
def get_alert(alert_id):
    """Get a specific alert."""
    db = get_alerts_db()
    c = db.cursor()
    alert = c.execute(
        'SELECT * FROM alerts WHERE id = %s AND user_id = %s',
        (alert_id, g.user['user_id'])
    ).fetchone()
    
    if not alert:
        return jsonify({'error': 'Alert not found'}), 404
    
    # Get recent history
    c = db.cursor()
    history = c.execute(
        'SELECT * FROM alert_history WHERE alert_id = %s ORDER BY triggered_at DESC LIMIT 10',
        (alert_id,)
    ).fetchall()
    
    result = dict(alert)
    result['config'] = json.loads(result['config'])
    result['history'] = [dict(h) for h in history]
    
    return jsonify(result)

@alerts_bp.route('/api/v1/alerts/<int:alert_id>', methods=['PUT'])
@require_auth
def update_alert(alert_id):
    """Update an alert."""
    db = get_alerts_db()
    
    # Check ownership
    c = db.cursor()
    alert = c.execute(
        'SELECT * FROM alerts WHERE id = %s AND user_id = %s',
        (alert_id, g.user['user_id'])
    ).fetchone()
    
    if not alert:
        return jsonify({'error': 'Alert not found'}), 404
    
    data = request.get_json()
    updates = []
    params = []
    
    # Validate and apply updates
    if 'name' in data:
        updates.append('name = %s')
        params.append(data['name'])
    
    if 'config' in data:
        alert_type = data.get('alert_type', alert['alert_type'])
        valid, errors = validate_alert_config(alert_type, data['config'])
        if not valid:
            return jsonify({'error': 'Invalid configuration', 'details': errors}), 400
        updates.append('config = %s')
        params.append(json.dumps(data['config']))
    
    if 'frequency' in data:
        if data['frequency'] not in ('immediate', 'daily', 'weekly'):
            return jsonify({'error': 'Invalid frequency'}), 400
        updates.append('frequency = %s')
        params.append(data['frequency'])
    
    if 'is_active' in data:
        updates.append('is_active = %s')
        params.append(1 if data['is_active'] else 0)
    
    if not updates:
        return jsonify({'error': 'No valid updates provided'}), 400
    
    updates.append('updated_at = CURRENT_TIMESTAMP')
    params.extend([alert_id, g.user['user_id']])
    
    c = db.cursor()
    c.execute(
        f'UPDATE alerts SET {", ".join(updates)} WHERE id = %s AND user_id = %s',
        params
    )
    db.commit()
    
    # Return updated alert
    c = db.cursor()
    updated = c.execute('SELECT * FROM alerts WHERE id = %s', (alert_id,)).fetchone()
    
    return jsonify({
        'success': True,
        'alert': dict(updated),
        'message': 'Alert updated successfully'
    })

@alerts_bp.route('/api/v1/alerts/<int:alert_id>', methods=['DELETE'])
@require_auth
def delete_alert(alert_id):
    """Delete an alert."""
    db = get_alerts_db()
    
    c = db.cursor()
    result = c.execute(
        'DELETE FROM alerts WHERE id = %s AND user_id = %s',
        (alert_id, g.user['user_id'])
    )
    db.commit()
    
    if result.rowcount == 0:
        return jsonify({'error': 'Alert not found'}), 404
    
    return jsonify({
        'success': True,
        'message': 'Alert deleted successfully'
    })

@alerts_bp.route('/api/v1/alerts/test/<int:alert_id>', methods=['POST'])
@require_auth
def test_alert(alert_id):
    """Send a test notification for an alert."""
    db = get_alerts_db()
    
    c = db.cursor()
    alert = c.execute(
        'SELECT * FROM alerts WHERE id = %s AND user_id = %s',
        (alert_id, g.user['user_id'])
    ).fetchone()
    
    if not alert:
        return jsonify({'error': 'Alert not found'}), 404
    
    alert_dict = dict(alert)
    alert_dict['config'] = json.loads(alert_dict['config'])
    
    test_trigger = {
        'reason': 'This is a test notification',
        'matched_content': 'Test content to verify your alert is working correctly.',
        'source_url': 'https://dchub.cloud',
        'source_title': 'DC Hub Test'
    }
    
    html, text = generate_alert_email(alert_dict, test_trigger)
    result = send_email(
        alert['user_email'],
        f'[TEST] DC Hub Alert: {alert["name"]}',
        html,
        text
    )
    
    return jsonify({
        'success': result['success'],
        'message': 'Test notification sent' if result['success'] else result.get('error', 'Failed to send'),
        'email': alert['user_email']
    })

@alerts_bp.route('/api/v1/alerts/history', methods=['GET'])
@require_auth
def get_alert_history():
    """Get notification history for user's alerts."""
    db = get_alerts_db()
    
    # Get all user's alert IDs
    c = db.cursor()
    alert_ids = c.execute(
        'SELECT id FROM alerts WHERE user_id = %s',
        (g.user['user_id'],)
    ).fetchall()
    
    if not alert_ids:
        return jsonify({'history': [], 'count': 0})
    
    ids = [a['id'] for a in alert_ids]
    placeholders = ','.join('%s' * len(ids))
    
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    
    c = db.cursor()
    history = c.execute(f'''
        SELECT h.*, a.name as alert_name, a.alert_type
        FROM alert_history h
        JOIN alerts a ON h.alert_id = a.id
        WHERE h.alert_id IN ({placeholders})
        ORDER BY h.triggered_at DESC
        LIMIT %s OFFSET %s
    ''', ids + [limit, offset]).fetchall()
    
    return jsonify({
        'history': [dict(h) for h in history],
        'count': len(history),
        'limit': limit,
        'offset': offset
    })

# =============================================================================
# Alert Processing (called by scheduler/cron)
# =============================================================================

def check_alerts_against_news(news_items: list):
    """Check all active alerts against new news items."""
    db = get_db('dchub.db')
    
    c = db.cursor()
    alerts = c.execute('SELECT * FROM alerts WHERE is_active = 1').fetchall()
    
    for alert in alerts:
        config = json.loads(alert['config'])
        
        for news in news_items:
            triggered = False
            trigger_reason = ''
            
            if alert['alert_type'] == 'operator_watch':
                for operator in config.get('operators', []):
                    if operator.lower() in news.get('title', '').lower() or \
                       operator.lower() in news.get('content', '').lower():
                        triggered = True
                        trigger_reason = f'Operator "{operator}" mentioned'
                        break
            
            elif alert['alert_type'] == 'market_watch':
                for market in config.get('markets', []):
                    if market.lower() in news.get('title', '').lower() or \
                       market.lower() in news.get('content', '').lower():
                        triggered = True
                        trigger_reason = f'Market "{market}" mentioned'
                        break
            
            elif alert['alert_type'] == 'keyword_watch':
                for keyword in config.get('keywords', []):
                    if keyword.lower() in news.get('title', '').lower() or \
                       keyword.lower() in news.get('content', '').lower():
                        triggered = True
                        trigger_reason = f'Keyword "{keyword}" found'
                        break
            
            elif alert['alert_type'] == 'capacity_threshold':
                # Extract MW from news
                mw_match = re.search(r'(\d+(%s:\.\d+)%s)\s*MW', news.get('title', '') + ' ' + news.get('content', ''), re.I)
                if mw_match:
                    mw = float(mw_match.group(1))
                    min_mw = config.get('min_mw', 0)
                    max_mw = config.get('max_mw', float('inf'))
                    if min_mw <= mw <= max_mw:
                        triggered = True
                        trigger_reason = f'{mw}MW capacity detected (threshold: {min_mw}-{max_mw}MW)'
            
            if triggered:
                # Record trigger
                c = db.cursor()
                c.execute('''
                    INSERT INTO alert_history (alert_id, trigger_reason, matched_data)
                    VALUES (%s, %s, %s)
                ''', (alert['id'], trigger_reason, json.dumps(news)))
                
                # Update alert stats
                c = db.cursor()
                c.execute('''
                    UPDATE alerts SET last_triggered = CURRENT_TIMESTAMP, trigger_count = trigger_count + 1
                    WHERE id = %s
                ''', (alert['id'],))
                
                # Send notification if immediate
                if alert['frequency'] == 'immediate':
                    alert_dict = dict(alert)
                    alert_dict['config'] = config
                    
                    trigger_data = {
                        'reason': trigger_reason,
                        'matched_content': news.get('title', ''),
                        'source_url': news.get('url', ''),
                        'source_title': news.get('source', 'DC Hub News')
                    }
                    
                    html, text = generate_alert_email(alert_dict, trigger_data)
                    result = send_email(
                        alert['user_email'],
                        f'DC Hub Alert: {alert["name"]}',
                        html,
                        text
                    )
                    
                    # Update history with notification status
                    c = db.cursor()
                    c.execute('''
                        UPDATE alert_history 
                        SET notification_sent = %s, notification_error = %s
                        WHERE alert_id = %s AND triggered_at = (
                            SELECT MAX(triggered_at) FROM alert_history WHERE alert_id = %s
                        )
                    ''', (1 if result['success'] else 0, result.get('error'), alert['id'], alert['id']))
    
    db.commit()
    db.close()

def send_digest_emails(frequency: str):
    """Send daily/weekly digest emails."""
    db = get_db('dchub.db')
    
    # Get alerts with pending notifications
    if frequency == 'daily':
        since = datetime.utcnow() - timedelta(days=1)
    else:  # weekly
        since = datetime.utcnow() - timedelta(weeks=1)
    
    # Group by user
    c = db.cursor()
    users = c.execute('''
        SELECT DISTINCT a.user_id, a.user_email
        FROM alerts a
        JOIN alert_history h ON a.id = h.alert_id
        WHERE a.frequency = %s AND a.is_active = 1 AND h.triggered_at > %s AND h.notification_sent = 0
    ''', (frequency, since)).fetchall()
    
    for user in users:
        # Get all pending notifications for this user
        c = db.cursor()
        notifications = c.execute('''
            SELECT h.*, a.name as alert_name, a.alert_type
            FROM alert_history h
            JOIN alerts a ON h.alert_id = a.id
            WHERE a.user_id = %s AND a.frequency = %s AND h.triggered_at > %s AND h.notification_sent = 0
            ORDER BY h.triggered_at DESC
        ''', (user['user_id'], frequency, since)).fetchall()
        
        if notifications:
            # Generate digest email
            html = generate_digest_email(notifications, frequency)
            result = send_email(
                user['user_email'],
                f'DC Hub {frequency.title()} Digest: {len(notifications)} Alerts',
                html
            )
            
            if result['success']:
                # Mark as sent
                ids = [n['id'] for n in notifications]
                placeholders = ','.join('%s' * len(ids))
                c = db.cursor()
                c.execute(f'UPDATE alert_history SET notification_sent = 1 WHERE id IN ({placeholders})', ids)
    
    db.commit()
    db.close()

def generate_digest_email(notifications: list, frequency: str) -> str:
    """Generate HTML digest email."""
    items_html = ''
    for n in notifications:
        matched = json.loads(n['matched_data']) if n['matched_data'] else {}
        items_html += f'''
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #eee;">
                <strong>{n['alert_name']}</strong><br>
                <span style="color: #666; font-size: 13px;">{n['trigger_reason']}</span>
                {f"<br><a href='{matched.get('url', '#')}'>{matched.get('title', 'View')}</a>" if matched.get('url') else ''}
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #eee; color: #888; font-size: 12px;">
                {n['triggered_at']}
            </td>
        </tr>
        '''
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        </style>
    </head>
    <body>
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #1a1a2e;">DC Hub {frequency.title()} Digest</h1>
            <p>You have {len(notifications)} alert(s) triggered:</p>
            <table style="width: 100%; border-collapse: collapse;">
                {items_html}
            </table>
            <p style="margin-top: 20px;">
                <a href="https://dchub.cloud/alerts" style="background: #2196f3; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px;">Manage Alerts</a>
            </p>
        </div>
    </body>
    </html>
    '''

# =============================================================================
# Register Blueprint
# =============================================================================

def register_alerts_api(app):
    """Register alerts blueprint with Flask app."""
    app.register_blueprint(alerts_bp)
    
    @app.teardown_appcontext
    def close_db(error):
        db = g.pop('db', None)
        if db is not None:
            db.close()
    
    print("✅ Alerts API registered")
