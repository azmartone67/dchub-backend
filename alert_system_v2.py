"""
DC HUB - Enhanced Alert System v2
=================================
Modular alert system with expanded alert types and SendGrid integration.

Alert Types:
  - market_watch: New facilities in a geographic market
  - operator_watch: Track specific operator announcements/facilities
  - capacity_threshold: Alert when market MW capacity hits target
  - deal_watch: Track M&A deals by type, operator, or value
  - news_keyword: Alert on news containing specific keywords

Installation:
  1. Add this file to your Replit project
  2. Add SENDGRID_API_KEY to Replit Secrets
  3. Add to main.py: from alert_system_v2 import register_alert_routes
  4. Call: register_alert_routes(app)

Endpoints:
  GET  /api/v2/alerts              - List user's alerts
  POST /api/v2/alerts              - Create new alert
  GET  /api/v2/alerts/:id          - Get single alert
  PUT  /api/v2/alerts/:id          - Update alert
  DELETE /api/v2/alerts/:id        - Delete alert
  POST /api/v2/alerts/:id/test     - Send test notification
  POST /api/v2/alerts/check        - Trigger alert check (admin)
  GET  /api/v2/alerts/types        - List available alert types
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify
from db_utils import get_db

# =============================================================================
# CONFIGURATION
# =============================================================================

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL', 'info@dchub.cloud')
SENDGRID_FROM_NAME = os.environ.get('SENDGRID_FROM_NAME', 'DC Hub Alerts')

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# Alert type definitions
ALERT_TYPES = {
    'market_watch': {
        'name': 'Market Watch',
        'description': 'Get notified when new facilities are added to a market',
        'criteria_schema': {
            'market': {'type': 'string', 'required': True, 'description': 'Market name (e.g., Phoenix, Dallas)'},
            'min_mw': {'type': 'number', 'required': False, 'description': 'Minimum MW capacity to trigger'}
        },
        'icon': '📍'
    },
    'operator_watch': {
        'name': 'Operator Watch',
        'description': 'Track announcements and new facilities from specific operators',
        'criteria_schema': {
            'operator': {'type': 'string', 'required': True, 'description': 'Operator name (e.g., Equinix, Digital Realty)'},
            'include_news': {'type': 'boolean', 'required': False, 'default': True}
        },
        'icon': '🏢'
    },
    'capacity_threshold': {
        'name': 'Capacity Threshold',
        'description': 'Alert when a market reaches a specific MW capacity',
        'criteria_schema': {
            'market': {'type': 'string', 'required': True},
            'threshold_mw': {'type': 'number', 'required': True, 'description': 'MW threshold to trigger alert'},
            'direction': {'type': 'string', 'required': False, 'default': 'above', 'enum': ['above', 'below']}
        },
        'icon': '⚡'
    },
    'deal_watch': {
        'name': 'Deal Watch',
        'description': 'Track M&A deals, acquisitions, and investments',
        'criteria_schema': {
            'deal_types': {'type': 'array', 'required': False, 'description': 'Filter by deal type (acquisition, investment, lease)'},
            'operators': {'type': 'array', 'required': False, 'description': 'Filter by operator names'},
            'min_value_usd': {'type': 'number', 'required': False, 'description': 'Minimum deal value'}
        },
        'icon': '💰'
    },
    'news_keyword': {
        'name': 'News Keyword Alert',
        'description': 'Get notified when news contains specific keywords',
        'criteria_schema': {
            'keywords': {'type': 'array', 'required': True, 'description': 'Keywords to match (OR logic)'},
            'exclude_keywords': {'type': 'array', 'required': False, 'description': 'Keywords to exclude'}
        },
        'icon': '📰'
    },
    'price_change': {
        'name': 'Energy Price Alert',
        'description': 'Alert on significant energy price changes in a region',
        'criteria_schema': {
            'region': {'type': 'string', 'required': True, 'description': 'RTO/ISO region (ERCOT, PJM, CAISO)'},
            'change_percent': {'type': 'number', 'required': True, 'description': 'Percent change threshold'}
        },
        'icon': '💡'
    }
}

# Plan limits
# r32-sweep (2026-05-20): added anonymous/identified/developer/founding
# entries — were missing, so any non-free/pro/ent caller fell through
# to a hard error when .get(tier) returned None. Now every canonical
# tier has an explicit value matching the gating ladder.
ALERT_LIMITS = {
    'anonymous':  0,         # can't create alerts without an account
    'anon':       0,
    'free':       5,
    'identified': 10,        # 2x free taste
    'starter':    18,        # r34: $9/mo Starter — between Identified + Developer
    'developer':  25,        # $49/mo
    'founding':   50,        # Pro-equivalent
    'pro':        50,
    'enterprise': 500,
    'admin':      9999,
}

# =============================================================================
# DATABASE HELPERS
# =============================================================================


def init_alerts_v2_table():
    """Initialize enhanced alerts table"""
    conn = get_db()
    c = conn.cursor()
    
    # Enhanced alerts table with JSON criteria
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts_v2 (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT,
            alert_type TEXT NOT NULL,
            criteria TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            email_notify INTEGER DEFAULT 1,
            push_notify INTEGER DEFAULT 0,
            webhook_url TEXT,
            frequency TEXT DEFAULT 'instant',
            created_at TEXT,
            updated_at TEXT,
            last_triggered TEXT,
            last_checked TEXT,
            trigger_count INTEGER DEFAULT 0,
            UNIQUE(user_id, alert_type, criteria)
        )
    """)
    
    # Alert history for tracking what was sent
    c.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id SERIAL PRIMARY KEY,
            alert_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            triggered_at TEXT NOT NULL,
            trigger_reason TEXT,
            email_sent INTEGER DEFAULT 0,
            email_sent_at TEXT,
            data_snapshot TEXT,
            FOREIGN KEY (alert_id) REFERENCES alerts_v2(id)
        )
    """)
    
    # Index for faster queries
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_v2_user ON alerts_v2(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_v2_type ON alerts_v2(alert_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_v2_enabled ON alerts_v2(enabled)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_alert ON alert_history(alert_id)")
    
    conn.commit()
    conn.close()
    print("✅ Alerts v2 tables initialized")

# =============================================================================
# SENDGRID EMAIL SERVICE
# =============================================================================

def send_alert_email(to_email, subject, html_content, text_content=None):
    """Send email via SendGrid API"""
    if not SENDGRID_API_KEY:
        print(f"⚠️ SendGrid not configured, would send to {to_email}: {subject}")
        return {'success': False, 'error': 'SendGrid not configured'}
    
    try:
        import requests
        
        data = {
            "personalizations": [{
                "to": [{"email": to_email}],
                "subject": subject
            }],
            "from": {
                "email": SENDGRID_FROM_EMAIL,
                "name": SENDGRID_FROM_NAME
            },
            "content": [
                {"type": "text/html", "value": html_content}
            ]
        }
        
        if text_content:
            data["content"].insert(0, {"type": "text/plain", "value": text_content})
        
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json"
            },
            json=data,
            timeout=10
        )
        
        if response.status_code in [200, 202]:
            print(f"✅ Email sent to {to_email}: {subject}")
            return {'success': True}
        else:
            print(f"❌ SendGrid error {response.status_code}: {response.text}")
            return {'success': False, 'error': response.text}
            
    except Exception as e:
        print(f"❌ Email send error: {e}")
        return {'success': False, 'error': str(e)}

def build_alert_email(alert_type, alert_name, trigger_data):
    """Build HTML email content for an alert"""
    icon = ALERT_TYPES.get(alert_type, {}).get('icon', '🔔')
    type_name = ALERT_TYPES.get(alert_type, {}).get('name', alert_type)
    
    # Build data rows
    data_rows = ""
    if isinstance(trigger_data, dict):
        for key, value in trigger_data.items():
            if key not in ['_internal']:
                data_rows += f"""
                <tr>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; color: #666;">{key.replace('_', ' ').title()}</td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; font-weight: 500;">{value}</td>
                </tr>
                """
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 30px; border-radius: 12px 12px 0 0;">
            <h1 style="color: white; margin: 0; font-size: 24px;">
                {icon} DC Hub Alert
            </h1>
            <p style="color: #a0a0a0; margin: 10px 0 0 0; font-size: 14px;">
                {type_name}
            </p>
        </div>
        
        <div style="background: white; padding: 30px; border: 1px solid #e0e0e0; border-top: none;">
            <h2 style="margin: 0 0 20px 0; color: #1a1a2e; font-size: 20px;">
                {alert_name}
            </h2>
            
            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                {data_rows}
            </table>
            
            <div style="margin-top: 30px;">
                <a href="https://dchub.cloud/dashboard.html#alerts" 
                   style="display: inline-block; background: #3b82f6; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 500;">
                    View in Dashboard
                </a>
            </div>
        </div>
        
        <div style="background: #f8f9fa; padding: 20px; border-radius: 0 0 12px 12px; border: 1px solid #e0e0e0; border-top: none;">
            <p style="margin: 0; font-size: 12px; color: #666;">
                You're receiving this because you set up an alert on DC Hub.<br>
                <a href="https://dchub.cloud/dashboard.html#alerts" style="color: #3b82f6;">Manage your alerts</a> | 
                <a href="https://dchub.cloud/unsubscribe" style="color: #3b82f6;">Unsubscribe</a>
            </p>
        </div>
    </body>
    </html>
    """
    
    text = f"""
DC Hub Alert - {type_name}

{alert_name}

{json.dumps(trigger_data, indent=2) if trigger_data else 'Alert triggered'}

View in Dashboard: https://dchub.cloud/dashboard.html#alerts

---
Manage alerts: https://dchub.cloud/dashboard.html#alerts
Unsubscribe: https://dchub.cloud/unsubscribe
    """
    
    return html, text

# =============================================================================
# ALERT CHECKING LOGIC
# =============================================================================

def check_market_watch_alert(criteria, last_checked):
    """Check if new facilities added to market"""
    market = criteria.get('market', '')
    min_mw = criteria.get('min_mw', 0)
    
    if not market:
        return None
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        # Check for new facilities since last check
        query = """
            SELECT COUNT(*) as count, SUM(COALESCE(capacity_mw, 0)) as total_mw
            FROM facilities
            WHERE (market LIKE %s OR city LIKE %s OR state LIKE %s)
            AND created_at > %s
        """
        market_pattern = f'%{market}%'
        check_time = last_checked or (datetime.utcnow() - timedelta(days=1)).isoformat()
        
        c.execute(query, (market_pattern, market_pattern, market_pattern, check_time))
        row = c.fetchone()
        
        if row and row['count'] > 0:
            if min_mw and (row['total_mw'] or 0) < min_mw:
                return None
            
            return {
                'new_facilities': row['count'],
                'total_mw_added': row['total_mw'] or 0,
                'market': market,
                'since': check_time
            }
    except Exception as e:
        print(f"Market watch check error: {e}")
    finally:
        conn.close()
    
    return None

def check_operator_watch_alert(criteria, last_checked):
    """Check for new operator facilities or news"""
    operator = criteria.get('operator', '')
    include_news = criteria.get('include_news', True)
    
    if not operator:
        return None
    
    conn = get_db()
    c = conn.cursor()
    results = {}
    
    try:
        check_time = last_checked or (datetime.utcnow() - timedelta(days=1)).isoformat()
        
        # Check facilities
        c.execute("""
            SELECT COUNT(*) as count
            FROM facilities
            WHERE provider LIKE %s
            AND created_at > %s
        """, (f'%{operator}%', check_time))
        fac_row = c.fetchone()
        
        if fac_row and fac_row['count'] > 0:
            results['new_facilities'] = fac_row['count']
        
        # Check news if enabled
        if include_news:
            c.execute("""
                SELECT COUNT(*) as count
                FROM news
                WHERE (title LIKE %s OR content LIKE %s)
                AND published_at > %s
            """, (f'%{operator}%', f'%{operator}%', check_time))
            news_row = c.fetchone()
            
            if news_row and news_row['count'] > 0:
                results['news_mentions'] = news_row['count']
        
        if results:
            results['operator'] = operator
            results['since'] = check_time
            return results
            
    except Exception as e:
        print(f"Operator watch check error: {e}")
    finally:
        conn.close()
    
    return None

def check_capacity_threshold_alert(criteria, last_checked):
    """Check if market capacity crossed threshold"""
    market = criteria.get('market', '')
    threshold_mw = criteria.get('threshold_mw', 0)
    direction = criteria.get('direction', 'above')
    
    if not market or not threshold_mw:
        return None
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        c.execute("""
            SELECT SUM(COALESCE(capacity_mw, 0)) as total_mw
            FROM facilities
            WHERE market LIKE %s OR city LIKE %s
        """, (f'%{market}%', f'%{market}%'))
        row = c.fetchone()
        
        if row and row['total_mw']:
            total_mw = row['total_mw']
            
            if direction == 'above' and total_mw >= threshold_mw:
                return {
                    'market': market,
                    'current_mw': round(total_mw, 1),
                    'threshold_mw': threshold_mw,
                    'status': f'Capacity is now {round(total_mw, 1)} MW (above {threshold_mw} MW threshold)'
                }
            elif direction == 'below' and total_mw <= threshold_mw:
                return {
                    'market': market,
                    'current_mw': round(total_mw, 1),
                    'threshold_mw': threshold_mw,
                    'status': f'Capacity dropped to {round(total_mw, 1)} MW (below {threshold_mw} MW threshold)'
                }
    except Exception as e:
        print(f"Capacity threshold check error: {e}")
    finally:
        conn.close()
    
    return None

def check_deal_watch_alert(criteria, last_checked):
    """Check for new deals matching criteria"""
    deal_types = criteria.get('deal_types', [])
    operators = criteria.get('operators', [])
    min_value = criteria.get('min_value_usd', 0)
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        check_time = last_checked or (datetime.utcnow() - timedelta(days=1)).isoformat()
        
        # Build query dynamically
        query = "SELECT * FROM deals WHERE created_at > %s"
        params = [check_time]
        
        if deal_types:
            placeholders = ','.join(['%s' for _ in deal_types])
            query += f" AND deal_type IN ({placeholders})"
            params.extend(deal_types)
        
        if min_value:
            query += " AND value_usd >= %s"
            params.append(min_value)
        
        c.execute(query, params)
        deals = c.fetchall()
        
        # Filter by operators if specified
        if operators and deals:
            filtered = []
            for deal in deals:
                deal_dict = dict(deal)
                for op in operators:
                    if op.lower() in (deal_dict.get('buyer', '') or '').lower() or \
                       op.lower() in (deal_dict.get('seller', '') or '').lower() or \
                       op.lower() in (deal_dict.get('parties', '') or '').lower():
                        filtered.append(deal_dict)
                        break
            deals = filtered
        
        if deals:
            return {
                'new_deals': len(deals),
                'total_value': sum(d.get('value_usd', 0) or 0 for d in deals if isinstance(d, dict)),
                'deal_types': list(set(d.get('deal_type', 'Unknown') for d in deals if isinstance(d, dict))),
                'since': check_time
            }
            
    except Exception as e:
        print(f"Deal watch check error: {e}")
    finally:
        conn.close()
    
    return None

def check_news_keyword_alert(criteria, last_checked):
    """Check for news matching keywords"""
    keywords = criteria.get('keywords', [])
    exclude = criteria.get('exclude_keywords', [])
    
    if not keywords:
        return None
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        check_time = last_checked or (datetime.utcnow() - timedelta(days=1)).isoformat()
        
        # Build OR query for keywords
        keyword_conditions = ' OR '.join(['title LIKE %s OR content LIKE %s' for _ in keywords])
        params = []
        for kw in keywords:
            params.extend([f'%{kw}%', f'%{kw}%'])
        
        query = f"""
            SELECT id, title, source, published_at
            FROM news
            WHERE ({keyword_conditions})
            AND published_at > %s
            ORDER BY published_at DESC
            LIMIT 10
        """
        params.append(check_time)
        
        c.execute(query, params)
        articles = c.fetchall()
        
        # Filter out excluded keywords
        if exclude and articles:
            filtered = []
            for article in articles:
                article_dict = dict(article)
                title = (article_dict.get('title', '') or '').lower()
                skip = False
                for ex in exclude:
                    if ex.lower() in title:
                        skip = True
                        break
                if not skip:
                    filtered.append(article_dict)
            articles = filtered
        
        if articles:
            return {
                'matching_articles': len(articles),
                'keywords_matched': keywords,
                'latest_title': articles[0]['title'] if articles else None,
                'since': check_time
            }
            
    except Exception as e:
        print(f"News keyword check error: {e}")
    finally:
        conn.close()
    
    return None

# Alert checker dispatcher
ALERT_CHECKERS = {
    'market_watch': check_market_watch_alert,
    'operator_watch': check_operator_watch_alert,
    'capacity_threshold': check_capacity_threshold_alert,
    'deal_watch': check_deal_watch_alert,
    'news_keyword': check_news_keyword_alert,
}

def check_single_alert(alert):
    """Check a single alert and return trigger data if triggered"""
    alert_type = alert['alert_type']
    criteria = json.loads(alert['criteria']) if isinstance(alert['criteria'], str) else alert['criteria']
    last_checked = alert.get('last_checked')
    
    checker = ALERT_CHECKERS.get(alert_type)
    if not checker:
        return None
    
    return checker(criteria, last_checked)

def process_all_alerts():
    """Process all enabled alerts and send notifications"""
    conn = get_db()
    c = conn.cursor()
    
    results = {
        'alerts_checked': 0,
        'alerts_triggered': 0,
        'emails_sent': 0,
        'errors': []
    }
    
    try:
        # Get all enabled alerts with user email
        c.execute("""
            SELECT a.*, u.email as user_email
            FROM alerts_v2 a
            LEFT JOIN users u ON a.user_id = u.id
            WHERE a.enabled = 1
        """)
        alerts = c.fetchall()
        
        now = datetime.utcnow().isoformat()
        
        for alert in alerts:
            alert_dict = dict(alert)
            results['alerts_checked'] += 1
            
            try:
                trigger_data = check_single_alert(alert_dict)
                
                # Update last_checked regardless
                c.execute("UPDATE alerts_v2 SET last_checked = %s WHERE id = %s", 
                         (now, alert_dict['id']))
                
                if trigger_data:
                    results['alerts_triggered'] += 1
                    
                    # Update alert stats
                    c.execute("""
                        UPDATE alerts_v2 
                        SET last_triggered = %s, trigger_count = trigger_count + 1
                        WHERE id = %s
                    """, (now, alert_dict['id']))
                    
                    # Record in history
                    c.execute("""
                        INSERT INTO alert_history (alert_id, user_id, triggered_at, trigger_reason, data_snapshot)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        alert_dict['id'],
                        alert_dict['user_id'],
                        now,
                        alert_dict['alert_type'],
                        json.dumps(trigger_data)
                    ))
                    
                    # Send email if enabled
                    if alert_dict.get('email_notify') and alert_dict.get('user_email'):
                        alert_name = alert_dict.get('name') or ALERT_TYPES.get(alert_dict['alert_type'], {}).get('name', 'Alert')
                        html, text = build_alert_email(
                            alert_dict['alert_type'],
                            alert_name,
                            trigger_data
                        )
                        
                        subject = f"🔔 DC Hub Alert: {alert_name}"
                        email_result = send_alert_email(
                            alert_dict['user_email'],
                            subject,
                            html,
                            text
                        )
                        
                        if email_result.get('success'):
                            results['emails_sent'] += 1
                            c.execute("""
                                UPDATE alert_history 
                                SET email_sent = 1, email_sent_at = %s
                                WHERE alert_id = %s AND triggered_at = %s
                            """, (now, alert_dict['id'], now))
                            
            except Exception as e:
                results['errors'].append(f"Alert {alert_dict['id']}: {str(e)}")
                print(f"Error processing alert {alert_dict['id']}: {e}")
        
        conn.commit()
        
    except Exception as e:
        results['errors'].append(f"General error: {str(e)}")
        print(f"Alert processing error: {e}")
    finally:
        conn.close()
    
    return results

# =============================================================================
# FLASK ROUTES
# =============================================================================

alerts_v2_bp = Blueprint('alerts_v2', __name__)

def require_auth(f):
    """Auth decorator - imports from main app or handles locally"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Try to use main app's auth
        try:
            from main import decode_jwt
        except ImportError:
            import jwt as pyjwt
            JWT_SECRET = os.environ.get('JWT_SECRET', 'your-secret-key')
            def decode_jwt(token):
                try:
                    return pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
                except:
                    return None
        
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authorization required'}), 401
        
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        
        if not payload:
            return jsonify({'error': 'Invalid or expired token'}), 401
        
        request.user = payload
        return f(*args, **kwargs)
    
    return decorated

@alerts_v2_bp.route('/api/v2/alerts/types', methods=['GET'])
def list_alert_types():
    """List available alert types and their schemas"""
    return jsonify({
        'success': True,
        'types': ALERT_TYPES
    })

@alerts_v2_bp.route('/api/v2/alerts', methods=['GET'])
@require_auth
def list_alerts():
    """List user's alerts"""
    user_id = request.user['user_id']
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT * FROM alerts_v2
        WHERE user_id = %s
        ORDER BY created_at DESC
    """, (user_id,))
    
    alerts = []
    for row in c.fetchall():
        alert = dict(row)
        alert['criteria'] = json.loads(alert['criteria']) if alert['criteria'] else {}
        alert['enabled'] = bool(alert['enabled'])
        alert['email_notify'] = bool(alert['email_notify'])
        alert['push_notify'] = bool(alert['push_notify'])
        alert['type_info'] = ALERT_TYPES.get(alert['alert_type'], {})
        alerts.append(alert)
    
    conn.close()
    
    return jsonify({
        'success': True,
        'alerts': alerts,
        'count': len(alerts)
    })

# AUTO-REPAIR: duplicate route '/api/v2/alerts' also in alert_system_v2.py:736 — review and remove one
@alerts_v2_bp.route('/api/v2/alerts', methods=['POST'])
@require_auth
def create_alert():
    """Create a new alert"""
    user_id = request.user['user_id']
    data = request.get_json()
    
    alert_type = data.get('alert_type')
    criteria = data.get('criteria', {})
    name = data.get('name', '')
    
    # Validate alert type
    if alert_type not in ALERT_TYPES:
        return jsonify({
            'error': f'Invalid alert type. Valid types: {list(ALERT_TYPES.keys())}'
        }), 400
    
    # Validate required criteria
    schema = ALERT_TYPES[alert_type]['criteria_schema']
    for field, field_schema in schema.items():
        if field_schema.get('required') and field not in criteria:
            return jsonify({
                'error': f'Missing required field: {field}'
            }), 400
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        # Check user's plan and alert limit
        c.execute("SELECT plan FROM users WHERE id = %s", (user_id,))
        user_row = c.fetchone()
        plan = user_row['plan'] if user_row else 'free'
        
        max_alerts = ALERT_LIMITS.get(plan, ALERT_LIMITS['free'])
        
        c.execute("SELECT COUNT(*) as count FROM alerts_v2 WHERE user_id = %s", (user_id,))
        current_count = c.fetchone()['count']
        
        if current_count >= max_alerts:
            conn.close()
            return jsonify({
                'error': f'Alert limit reached ({max_alerts}). Upgrade for more alerts.',
                'code': 'LIMIT_REACHED',
                'current': current_count,
                'limit': max_alerts
            }), 403
        
        # Check for duplicate
        criteria_json = json.dumps(criteria, sort_keys=True)
        c.execute("""
            SELECT id FROM alerts_v2 
            WHERE user_id = %s AND alert_type = %s AND criteria = %s
        """, (user_id, alert_type, criteria_json))
        
        if c.fetchone():
            conn.close()
            return jsonify({'error': 'You already have this exact alert configured'}), 409
        
        # Insert alert
        now = datetime.utcnow().isoformat()
        c.execute("""
            INSERT INTO alerts_v2 (user_id, name, alert_type, criteria, email_notify, frequency, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (
            user_id,
            name or ALERT_TYPES[alert_type]['name'],
            alert_type,
            criteria_json,
            1,  # email_notify default true
            data.get('frequency', 'instant'),
            now,
            now
        ))
        
        alert_id = c.lastrowid
        conn.commit()
        
        return jsonify({
            'success': True,
            'alert': {
                'id': alert_id,
                'name': name or ALERT_TYPES[alert_type]['name'],
                'alert_type': alert_type,
                'criteria': criteria,
                'enabled': True,
                'email_notify': True,
                'created_at': now,
                'type_info': ALERT_TYPES[alert_type]
            }
        }), 201
        
    except sqlite3.IntegrityError as e:
        return jsonify({'error': 'Alert already exists'}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@alerts_v2_bp.route('/api/v2/alerts/<int:alert_id>', methods=['GET'])
@require_auth
def get_alert(alert_id):
    """Get single alert details"""
    user_id = request.user['user_id']
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM alerts_v2 WHERE id = %s AND user_id = %s", (alert_id, user_id))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Alert not found'}), 404
    
    alert = dict(row)
    alert['criteria'] = json.loads(alert['criteria']) if alert['criteria'] else {}
    alert['enabled'] = bool(alert['enabled'])
    alert['type_info'] = ALERT_TYPES.get(alert['alert_type'], {})
    
    # Get recent history
    c.execute("""
        SELECT * FROM alert_history
        WHERE alert_id = %s
        ORDER BY triggered_at DESC
        LIMIT 10
    """, (alert_id,))
    
    history = []
    for h in c.fetchall():
        hist = dict(h)
        hist['data_snapshot'] = json.loads(hist['data_snapshot']) if hist['data_snapshot'] else {}
        history.append(hist)
    
    alert['history'] = history
    
    conn.close()
    
    return jsonify({
        'success': True,
        'alert': alert
    })
# AUTO-REPAIR: duplicate route '/api/v2/alerts/<int:alert_id>' also in alert_system_v2.py:868 — review and remove one

@alerts_v2_bp.route('/api/v2/alerts/<int:alert_id>', methods=['PUT'])
@require_auth
def update_alert(alert_id):
    """Update an alert"""
    user_id = request.user['user_id']
    data = request.get_json()
    
    conn = get_db()
    c = conn.cursor()
    
    # Verify ownership
    c.execute("SELECT id FROM alerts_v2 WHERE id = %s AND user_id = %s", (alert_id, user_id))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Alert not found'}), 404
    
    # Build update
    updates = []
    params = []
    
    if 'name' in data:
        updates.append('name = %s')
        params.append(data['name'])
    
    if 'criteria' in data:
        updates.append('criteria = %s')
        params.append(json.dumps(data['criteria'], sort_keys=True))
    
    if 'enabled' in data:
        updates.append('enabled = %s')
        params.append(1 if data['enabled'] else 0)
    
    if 'email_notify' in data:
        updates.append('email_notify = %s')
        params.append(1 if data['email_notify'] else 0)
    
    if 'frequency' in data:
        updates.append('frequency = %s')
        params.append(data['frequency'])
    
    if updates:
        updates.append('updated_at = %s')
        params.append(datetime.utcnow().isoformat())
        params.append(alert_id)
        
        c.execute(f"UPDATE alerts_v2 SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
    
    conn.close()
    
# AUTO-REPAIR: duplicate route '/api/v2/alerts/<int:alert_id>' also in alert_system_v2.py:868 — review and remove one
    return jsonify({'success': True, 'updated': alert_id})

@alerts_v2_bp.route('/api/v2/alerts/<int:alert_id>', methods=['DELETE'])
@require_auth
def delete_alert(alert_id):
    """Delete an alert"""
    user_id = request.user['user_id']
    
    conn = get_db()
    c = conn.cursor()
    
    # Verify ownership
    c.execute("SELECT id FROM alerts_v2 WHERE id = %s AND user_id = %s", (alert_id, user_id))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Alert not found'}), 404
    
    # Delete history first (foreign key)
    c.execute("DELETE FROM alert_history WHERE alert_id = %s", (alert_id,))
    c.execute("DELETE FROM alerts_v2 WHERE id = %s", (alert_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'deleted': alert_id})

@alerts_v2_bp.route('/api/v2/alerts/<int:alert_id>/test', methods=['POST'])
@require_auth
def test_alert(alert_id):
    """Send a test notification for an alert"""
    user_id = request.user['user_id']
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT a.*, u.email as user_email
        FROM alerts_v2 a
        LEFT JOIN users u ON a.user_id = u.id
        WHERE a.id = %s AND a.user_id = %s
    """, (alert_id, user_id))
    
    row = c.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Alert not found'}), 404
    
    alert = dict(row)
    
    if not alert.get('user_email'):
        return jsonify({'error': 'No email address on file'}), 400
    
    # Build test email
    alert_name = alert.get('name') or 'Test Alert'
    html, text = build_alert_email(
        alert['alert_type'],
        alert_name,
        {'test': True, 'message': 'This is a test notification', 'timestamp': datetime.utcnow().isoformat()}
    )
    
    result = send_alert_email(
        alert['user_email'],
        f"🧪 Test: {alert_name}",
        html,
        text
    )
    
    return jsonify({
        'success': result.get('success', False),
        'message': 'Test email sent' if result.get('success') else result.get('error', 'Failed to send'),
        'to': alert['user_email']
    })

@alerts_v2_bp.route('/api/v2/alerts/check', methods=['POST'])
def trigger_alert_check():
    """Manually trigger alert processing (admin/cron endpoint)"""
    # Optional: Add API key check for security
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    valid_keys = os.environ.get('DCHUB_API_KEYS', '').split(',')
    
    # Allow if no keys configured (dev mode) or valid key provided
    if valid_keys and valid_keys[0] and api_key not in valid_keys:
        return jsonify({'error': 'API key required'}), 401
    
    results = process_all_alerts()
    
    return jsonify({
        'success': True,
        'timestamp': datetime.utcnow().isoformat(),
        **results
    })

# =============================================================================
# BACKGROUND SCHEDULER
# =============================================================================

class AlertScheduler:
    """Background scheduler for periodic alert checks"""
    
    def __init__(self, interval_seconds=3600):
        self.interval = interval_seconds
        self.running = False
        self.thread = None
    
    def start(self):
        """Start the scheduler"""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print(f"📧 Alert Scheduler started (every {self.interval}s)")
    
    def stop(self):
        """Stop the scheduler"""
        self.running = False
    
    def _run(self):
        """Main scheduler loop"""
        import time
        
        while self.running:
            time.sleep(self.interval)
            
            if not self.running:
                break
            
            try:
                results = process_all_alerts()
                print(f"📧 Alert check: {results['alerts_checked']} checked, "
                      f"{results['alerts_triggered']} triggered, "
                      f"{results['emails_sent']} emails sent")
            except Exception as e:
                print(f"📧 Alert check error: {e}")

# Global scheduler instance
alert_scheduler = None

# =============================================================================
# REGISTRATION FUNCTION
# =============================================================================

def register_alert_routes(app, start_scheduler=True, scheduler_interval=3600):
    """
    Register alert routes with Flask app
    
    Args:
        app: Flask application
        start_scheduler: Whether to start background alert checker
        scheduler_interval: Seconds between alert checks (default: 1 hour)
    """
    global alert_scheduler
    
    # Initialize tables
    init_alerts_v2_table()
    
    # Register blueprint
    app.register_blueprint(alerts_v2_bp)
    
    # Start scheduler if requested
    if start_scheduler:
        alert_scheduler = AlertScheduler(interval_seconds=scheduler_interval)
        alert_scheduler.start()
    
    print("✅ Alert System v2 registered")
    print("   📍 GET/POST /api/v2/alerts")
    print("   📍 GET/PUT/DELETE /api/v2/alerts/:id")
    print("   📍 POST /api/v2/alerts/:id/test")
    print("   📍 POST /api/v2/alerts/check")
    print("   📍 GET /api/v2/alerts/types")
    
    return alert_scheduler

# =============================================================================
# STANDALONE TESTING
# =============================================================================

if __name__ == '__main__':
    # Test mode - run standalone
    from flask import Flask
    
    app = Flask(__name__)
    import secrets as _sec
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or _sec.token_hex(32)
    
    register_alert_routes(app, start_scheduler=False)
    
    print("\n🧪 Running in test mode...")
    print("   Try: GET http://localhost:5001/api/v2/alerts/types")
    
    app.run(host='0.0.0.0', port=5001, debug=True)
