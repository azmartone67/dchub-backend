"""
DC Hub Simple Alert System
==========================
No authentication required - uses email as identifier.

Endpoints:
- POST   /api/v1/simple-alerts          - Create new alert
- GET    /api/v1/simple-alerts          - List alerts by email
- DELETE /api/v1/simple-alerts/<id>     - Delete alert
- POST   /api/v1/simple-alerts/test/<id> - Send test notification

Usage:
  GET /api/v1/simple-alerts%semail=user@example.com
  POST /api/v1/simple-alerts with JSON body
"""

from flask import Blueprint, request, jsonify
import json
import os
import hashlib
from datetime import datetime
from db_utils import get_db
from util.deals import DEALS_OK

simple_alerts_bp = Blueprint('simple_alerts', __name__)

# =============================================================================
# Database Setup
# =============================================================================

def get_alerts_db():
    """Get database connection for alerts."""
    return get_db('dchub.db')

def init_simple_alerts_db():
    """Initialize simple alerts table."""
    db = get_db('dchub.db')
    cursor = db.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS simple_alerts (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            name TEXT NOT NULL,
            config TEXT NOT NULL,
            frequency TEXT DEFAULT 'immediate',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered TIMESTAMP,
            trigger_count INTEGER DEFAULT 0
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_simple_alerts_email ON simple_alerts(email)')
    
    db.commit()
    db.close()
    print("✅ Simple Alerts database initialized")

# Initialize on import
init_simple_alerts_db()

# =============================================================================
# Validation
# =============================================================================

def validate_email(email):
    """Basic email validation."""
    if not email or '@' not in email or '.' not in email:
        return False
    return True

def validate_alert_config(alert_type, config):
    """Validate alert configuration."""
    if alert_type == 'operator_watch':
        if not config.get('operators') or not isinstance(config['operators'], list):
            return False, 'operators must be a non-empty list'
    elif alert_type == 'market_watch':
        if not config.get('markets') or not isinstance(config['markets'], list):
            return False, 'markets must be a non-empty list'
    elif alert_type == 'capacity_threshold':
        if 'min_mw' not in config and 'max_mw' not in config:
            return False, 'min_mw or max_mw required'
    elif alert_type == 'keyword_watch':
        if not config.get('keywords') or not isinstance(config['keywords'], list):
            return False, 'keywords must be a non-empty list'
    else:
        return False, f'Invalid alert_type: {alert_type}'
    
    return True, None

# =============================================================================
# API Endpoints
# =============================================================================

@simple_alerts_bp.route('/api/v1/simple-alerts', methods=['GET'])
def list_alerts():
    """List alerts for an email address."""
    email = request.args.get('email', '').lower().strip()
    
    if not validate_email(email):
        return jsonify({
            'success': False,
            'error': 'Valid email parameter required'
        }), 400
    
    db = get_alerts_db()
    c = db.cursor()
    c.execute(
        'SELECT * FROM simple_alerts WHERE email = %s ORDER BY created_at DESC',
        (email,)
    )
    alerts = c.fetchall()
    db.close()
    
    result = []
    for alert in alerts:
        a = dict(alert)
        a['config'] = json.loads(a['config'])
        result.append(a)
    
    return jsonify({
        'success': True,
        'alerts': result,
        'count': len(result),
        'email': email
    })

# AUTO-REPAIR: duplicate route '/api/v1/simple-alerts' also in simple_alerts.py:97 — review and remove one
@simple_alerts_bp.route('/api/v1/simple-alerts', methods=['POST'])
def create_alert():
    """Create a new alert."""
    data = request.get_json()
    
    if not data:
        return jsonify({'success': False, 'error': 'JSON body required'}), 400
    
    email = data.get('email', '').lower().strip()
    alert_type = data.get('alert_type', '')
    name = data.get('name', '')
    config = data.get('config', {})
    frequency = data.get('frequency', 'immediate')
    
    # Validate email
    if not validate_email(email):
        return jsonify({'success': False, 'error': 'Valid email required'}), 400
    
    # Validate name
    if not name or len(name) < 2:
        return jsonify({'success': False, 'error': 'Alert name required (min 2 chars)'}), 400
    
    # Validate alert type and config
    valid, error = validate_alert_config(alert_type, config)
    if not valid:
        return jsonify({'success': False, 'error': error}), 400
    
    # Validate frequency
    if frequency not in ('immediate', 'daily', 'weekly'):
        return jsonify({'success': False, 'error': 'frequency must be: immediate, daily, or weekly'}), 400
    
    # Check for duplicate
    db = get_alerts_db()
    c = db.cursor()
    c.execute(
        'SELECT id FROM simple_alerts WHERE email = %s AND name = %s',
        (email, name)
    )
    existing = c.fetchone()
    
    if existing:
        db.close()
        return jsonify({'success': False, 'error': 'Alert with this name already exists'}), 409
    
    # Check limit (max 10 alerts per email)
    c = db.cursor()
    c.execute(
        'SELECT COUNT(*) as cnt FROM simple_alerts WHERE email = %s',
        (email,)
    )
    count = c.fetchone()['cnt']
    
    if count >= 10:
        db.close()
        return jsonify({'success': False, 'error': 'Maximum 10 alerts per email'}), 400
    
    # Create alert
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO simple_alerts (email, alert_type, name, config, frequency)
        VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
    ''', (email, alert_type, name, json.dumps(config), frequency))
    
    alert_id = cursor.lastrowid
    db.commit()
    db.close()
    
    return jsonify({
        'success': True,
        'message': f'Alert "{name}" created',
        'alert': {
            'id': alert_id,
            'email': email,
            'alert_type': alert_type,
            'name': name,
            'config': config,
            'frequency': frequency
        }
    }), 201

@simple_alerts_bp.route('/api/v1/simple-alerts/<int:alert_id>', methods=['GET'])
def get_alert(alert_id):
    """Get a specific alert."""
    email = request.args.get('email', '').lower().strip()
    
    if not validate_email(email):
        return jsonify({'success': False, 'error': 'Valid email parameter required'}), 400
    
    db = get_alerts_db()
    c = db.cursor()
    c.execute(
        'SELECT * FROM simple_alerts WHERE id = %s AND email = %s',
        (alert_id, email)
    )
    alert = c.fetchone()
    db.close()
    
    if not alert:
        return jsonify({'success': False, 'error': 'Alert not found'}), 404
    
    result = dict(alert)
    result['config'] = json.loads(result['config'])
    
    return jsonify({
        'success': True,
        'alert': result
    })
# AUTO-REPAIR: duplicate route '/api/v1/simple-alerts/<int:alert_id>' also in simple_alerts.py:210 — review and remove one

@simple_alerts_bp.route('/api/v1/simple-alerts/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    """Delete an alert."""
    email = request.args.get('email', '').lower().strip()
    
    if not validate_email(email):
        return jsonify({'success': False, 'error': 'Valid email parameter required'}), 400
    
    db = get_alerts_db()
    
    # Check exists
    c = db.cursor()
    c.execute(
        'SELECT id, name FROM simple_alerts WHERE id = %s AND email = %s',
        (alert_id, email)
    )
    alert = c.fetchone()
    
    if not alert:
        db.close()
        return jsonify({'success': False, 'error': 'Alert not found'}), 404
    
    # Delete
    c = db.cursor()
    c.execute('DELETE FROM simple_alerts WHERE id = %s', (alert_id,))
    db.commit()
    db.close()
    
    return jsonify({
        'success': True,
        'message': f'Alert "{alert["name"]}" deleted'
# AUTO-REPAIR: duplicate route '/api/v1/simple-alerts/<int:alert_id>' also in simple_alerts.py:210 — review and remove one
    })

@simple_alerts_bp.route('/api/v1/simple-alerts/<int:alert_id>', methods=['PUT'])
def update_alert(alert_id):
    """Update an alert."""
    data = request.get_json()
    
    if not data:
        return jsonify({'success': False, 'error': 'JSON body required'}), 400
    
    email = data.get('email', '').lower().strip()
    
    if not validate_email(email):
        return jsonify({'success': False, 'error': 'Valid email required in body'}), 400
    
    db = get_alerts_db()
    
    # Check exists
    c = db.cursor()
    c.execute(
        'SELECT * FROM simple_alerts WHERE id = %s AND email = %s',
        (alert_id, email)
    )
    alert = c.fetchone()
    
    if not alert:
        db.close()
        return jsonify({'success': False, 'error': 'Alert not found'}), 404
    
    # Build update
    updates = []
    params = []
    
    if 'name' in data:
        updates.append('name = %s')
        params.append(data['name'])
    
    if 'config' in data:
        alert_type = data.get('alert_type', alert['alert_type'])
        valid, error = validate_alert_config(alert_type, data['config'])
        if not valid:
            db.close()
            return jsonify({'success': False, 'error': error}), 400
        updates.append('config = %s')
        params.append(json.dumps(data['config']))
    
    if 'frequency' in data:
        if data['frequency'] not in ('immediate', 'daily', 'weekly'):
            db.close()
            return jsonify({'success': False, 'error': 'Invalid frequency'}), 400
        updates.append('frequency = %s')
        params.append(data['frequency'])
    
    if 'is_active' in data:
        updates.append('is_active = %s')
        params.append(1 if data['is_active'] else 0)
    
    if not updates:
        db.close()
        return jsonify({'success': False, 'error': 'No valid fields to update'}), 400
    
    params.extend([alert_id, email])
    
    c = db.cursor()
    c.execute(
        f'UPDATE simple_alerts SET {", ".join(updates)} WHERE id = %s AND email = %s',
        params
    )
    db.commit()
    
    # Fetch updated
    c = db.cursor()
    c.execute('SELECT * FROM simple_alerts WHERE id = %s', (alert_id,))
    updated = c.fetchone()
    db.close()
    
    result = dict(updated)
    result['config'] = json.loads(result['config'])
    
    return jsonify({
        'success': True,
        'message': 'Alert updated',
        'alert': result
    })

@simple_alerts_bp.route('/api/v1/simple-alerts/test/<int:alert_id>', methods=['POST'])
def test_alert(alert_id):
    """Send a test notification (simulated)."""
    data = request.get_json() or {}
    email = data.get('email', '').lower().strip()
    
    if not validate_email(email):
        return jsonify({'success': False, 'error': 'Valid email required in body'}), 400
    
    db = get_alerts_db()
    c = db.cursor()
    c.execute(
        'SELECT * FROM simple_alerts WHERE id = %s AND email = %s',
        (alert_id, email)
    )
    alert = c.fetchone()
    db.close()
    
    if not alert:
        return jsonify({'success': False, 'error': 'Alert not found'}), 404
    
    # In production, this would send an actual email
    # For now, just simulate success
    return jsonify({
        'success': True,
        'message': f'Test notification would be sent to {email}',
        'alert_name': alert['name'],
        'note': 'Email sending not configured - this is a simulation'
    })

@simple_alerts_bp.route('/api/v1/simple-alerts/types', methods=['GET'])
def list_alert_types():
    """List available alert types and their configuration."""
    return jsonify({
        'success': True,
        'alert_types': [
            {
                'type': 'operator_watch',
                'name': 'Operator Watch',
                'description': 'Get notified when specific operators are mentioned',
                'icon': '🏢',
                'config_example': {'operators': ['Google', 'Microsoft', 'AWS']}
            },
            {
                'type': 'market_watch',
                'name': 'Market Watch',
                'description': 'Get notified about activity in specific markets',
                'icon': '📍',
                'config_example': {'markets': ['Phoenix', 'Dallas', 'Northern Virginia']}
            },
            {
                'type': 'capacity_threshold',
                'name': 'Capacity Threshold',
                'description': 'Get notified when projects exceed MW thresholds',
                'icon': '⚡',
                'config_example': {'min_mw': 100, 'max_mw': 500}
            },
            {
                'type': 'keyword_watch',
                'name': 'Keyword Watch',
                'description': 'Get notified when specific keywords appear',
                'icon': '🔍',
                'config_example': {'keywords': ['nuclear', 'renewable', 'expansion']}
            }
        ],
        'frequencies': ['immediate', 'daily', 'weekly']
    })

# =============================================================================
# Background Processing (called by /api/jobs/simple-alerts)
# =============================================================================

def process_alerts():
    """Check active alerts against recent news/deals and queue notifications."""
    db = get_alerts_db()
    try:
        # execute() must not be chained: db_utils.PGCursorWrapper.execute returns
        # None for a SELECT (only DDL returns self), so `.execute(...).fetchall()`
        # raised AttributeError on the FIRST query — before either time window
        # below was ever reached.
        c = db.cursor()
        c.execute(
            'SELECT id, email, alert_type, name, config, frequency FROM simple_alerts WHERE is_active = 1'
        )
        alerts = c.fetchall()

        if not alerts:
            return {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}

        # `news_articles` has no `published_date` column — it is `published_at`,
        # and it is NULL on some rows, so fall back to fetched_at the way
        # news_engine does. Aliased back to the name the matcher below reads.
        c = db.cursor()
        c.execute(
            "SELECT id, title, source, COALESCE(published_at, fetched_at) AS published_date "
            "FROM news_articles "
            "WHERE COALESCE(published_at, fetched_at)::timestamptz >= NOW() - INTERVAL '1 day' "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 200"
        )
        recent_news = c.fetchall()

        # `deals` has no title/operator/mw_it either; the live columns are
        # buyer/seller/mw. Aliased so the matching below is untouched, and
        # quarantined rows are excluded via the canonical DEALS_OK guard.
        c = db.cursor()
        c.execute(
            "SELECT id, "
            "       NULLIF(TRIM(CONCAT_WS(' / ', NULLIF(buyer,''), NULLIF(seller,''))), '') AS title, "
            "       COALESCE(NULLIF(buyer,''), NULLIF(seller,'')) AS operator, "
            "       market, mw AS mw_it "
            f"FROM deals WHERE {DEALS_OK} "
            "  AND created_at::timestamptz >= NOW() - INTERVAL '1 day' "
            "ORDER BY created_at DESC LIMIT 100"
        )
        recent_deals = c.fetchall()

        matched = 0
        for alert in alerts:
            alert_dict = dict(alert)
            try:
                config = json.loads(alert_dict['config']) if isinstance(alert_dict['config'], str) else alert_dict['config']
            except (json.JSONDecodeError, TypeError):
                config = {}

            alert_type = alert_dict['alert_type']
            hits = []

            if alert_type == 'keyword_watch':
                keywords = [k.lower() for k in config.get('keywords', [])]
                for article in recent_news:
                    title = (dict(article).get('title') or '').lower()
                    if any(kw in title for kw in keywords):
                        hits.append({'type': 'news', 'title': dict(article).get('title'), 'id': dict(article).get('id')})

            elif alert_type == 'operator_watch':
                operators = [o.lower() for o in config.get('operators', [])]
                for deal in recent_deals:
                    d = dict(deal)
                    op = (d.get('operator') or '').lower()
                    title = (d.get('title') or '').lower()
                    if any(o in op or o in title for o in operators):
                        hits.append({'type': 'deal', 'title': d.get('title'), 'id': d.get('id')})
                for article in recent_news:
                    title = (dict(article).get('title') or '').lower()
                    if any(o in title for o in operators):
                        hits.append({'type': 'news', 'title': dict(article).get('title'), 'id': dict(article).get('id')})

            elif alert_type == 'market_watch':
                markets = [m.lower() for m in config.get('markets', [])]
                for deal in recent_deals:
                    d = dict(deal)
                    mkt = (d.get('market') or '').lower()
                    title = (d.get('title') or '').lower()
                    if any(m in mkt or m in title for m in markets):
                        hits.append({'type': 'deal', 'title': d.get('title'), 'id': d.get('id')})

            elif alert_type == 'capacity_threshold':
                min_mw = config.get('min_mw', 0)
                max_mw = config.get('max_mw', 999999)
                for deal in recent_deals:
                    d = dict(deal)
                    mw = d.get('mw_it') or 0
                    try:
                        mw = float(mw)
                    except (ValueError, TypeError):
                        mw = 0
                    if min_mw <= mw <= max_mw:
                        hits.append({'type': 'deal', 'title': d.get('title'), 'id': d.get('id'), 'mw': mw})

            if hits:
                matched += 1
                c = db.cursor()
                c.execute(
                    'UPDATE simple_alerts SET last_triggered = CURRENT_TIMESTAMP, trigger_count = trigger_count + 1 WHERE id = %s',
                    (alert_dict['id'],)
                )

        db.commit()
        return {'status': 'ok', 'processed': len(alerts), 'matched': matched}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}
    finally:
        db.close()

# =============================================================================
# Register Blueprint
# =============================================================================

def register_simple_alerts(app):
    """Register simple alerts blueprint."""
    app.register_blueprint(simple_alerts_bp)
    print("✅ Simple Alerts API registered at /api/v1/simple-alerts")
