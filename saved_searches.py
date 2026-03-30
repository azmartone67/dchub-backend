"""
DC Hub - Saved Searches & Alerts System
========================================
Allow users to save search criteria and get notified when new facilities match.

Features:
- Save facility search criteria (market, provider, power range, etc.)
- Email alerts when new facilities match saved searches
- Dashboard to manage saved searches
- Configurable alert frequency (instant, daily, weekly)

Database Tables:
- saved_searches: User's saved search criteria
- search_alerts: Alert history and tracking
"""

import os
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from flask import Blueprint, request, jsonify
import threading
import time
from db_utils import get_db

# Blueprint
saved_searches_bp = Blueprint('saved_searches', __name__)

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# =============================================================================
# DATABASE SETUP
# =============================================================================

def init_saved_searches_db():
    """Initialize saved searches tables"""
    conn = get_db()
    try:
        c = conn.cursor()

        # Saved searches table
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_searches (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                criteria TEXT NOT NULL,
                alert_frequency TEXT DEFAULT 'daily',
                alert_enabled INTEGER DEFAULT 1,
                last_checked TEXT,
                last_alert_sent TEXT,
                match_count INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # Search alerts history
        c.execute("""
            CREATE TABLE IF NOT EXISTS search_alerts (
                id SERIAL PRIMARY KEY,
                search_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                facilities_matched TEXT,
                facilities_count INTEGER,
                email_sent INTEGER DEFAULT 0,
                created_at TEXT,
                FOREIGN KEY (search_id) REFERENCES saved_searches(id)
            )
        """)

        # Create indexes
        c.execute("CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_alerts_search ON search_alerts(search_id)")

        conn.commit()
    finally:
        conn.close()

# Initialize on import
init_saved_searches_db()

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def generate_search_id(user_id: str, name: str) -> str:
    """Generate unique search ID"""
    return hashlib.md5(f"{user_id}:{name}:{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]

def match_criteria(facility: Dict, criteria: Dict) -> bool:
    """Check if a facility matches search criteria"""
    # Country filter
    if criteria.get('country'):
        if facility.get('country', '').lower() != criteria['country'].lower():
            return False
    
    # State filter
    if criteria.get('state'):
        if facility.get('state', '').lower() != criteria['state'].lower():
            return False
    
    # City filter
    if criteria.get('city'):
        if criteria['city'].lower() not in facility.get('city', '').lower():
            return False
    
    # Provider filter
    if criteria.get('provider'):
        if criteria['provider'].lower() not in facility.get('provider', '').lower():
            return False
    
    # Power range filter
    if criteria.get('min_power_mw'):
        if (facility.get('power_mw') or 0) < criteria['min_power_mw']:
            return False
    
    if criteria.get('max_power_mw'):
        if (facility.get('power_mw') or 0) > criteria['max_power_mw']:
            return False
    
    # Source filter
    if criteria.get('source'):
        if facility.get('source', '').lower() != criteria['source'].lower():
            return False
    
    # Keyword search
    if criteria.get('keyword'):
        keyword = criteria['keyword'].lower()
        searchable = f"{facility.get('name', '')} {facility.get('provider', '')} {facility.get('city', '')}".lower()
        if keyword not in searchable:
            return False
    
    return True

def get_matching_facilities(criteria: Dict, since: str = None) -> List[Dict]:
    """Find facilities matching criteria"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        # Base query
        query = """
            SELECT id, name, provider, city, state, country,
                   latitude, longitude, power_mw, source, first_seen
            FROM facilities
            WHERE 1=1
        """
        params = []

        # Add date filter for new facilities
        if since:
            query += " AND first_seen > %s"
            params.append(since)

        # Add basic filters to query for performance
        if criteria.get('country'):
            query += " AND LOWER(country) = LOWER(%s)"
            params.append(criteria['country'])

        if criteria.get('state'):
            query += " AND LOWER(state) = LOWER(%s)"
            params.append(criteria['state'])

        if criteria.get('source'):
            query += " AND LOWER(source) = LOWER(%s)"
            params.append(criteria['source'])

        query += " ORDER BY first_seen DESC LIMIT 1000"

        c.execute(query, params)
        rows = c.fetchall()
    finally:
        conn.close()
    
    # Apply remaining filters in Python
    facilities = []
    for row in rows:
        facility = dict(row)
        if match_criteria(facility, criteria):
            facilities.append(facility)
    
    return facilities

# =============================================================================
# API ENDPOINTS
# =============================================================================

@saved_searches_bp.route('/api/saved-searches', methods=['GET'])
def list_saved_searches():
    """List user's saved searches"""
    user_id = request.args.get('user_id') or request.headers.get('X-User-Id', 'anonymous')
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        c.execute("""
            SELECT * FROM saved_searches
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, [user_id])

        searches = []
        for row in c.fetchall():
            search = dict(row)
            search['criteria'] = json.loads(search['criteria'])
            searches.append(search)

    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'searches': searches,
        'count': len(searches)
    })

@saved_searches_bp.route('/api/saved-searches', methods=['POST'])
def create_saved_search():
    """Create a new saved search"""
    data = request.get_json() or {}
    
    user_id = data.get('user_id') or request.headers.get('X-User-Id', 'anonymous')
    name = data.get('name', 'Untitled Search')
    criteria = data.get('criteria', {})
    alert_frequency = data.get('alert_frequency', 'daily')
    alert_enabled = data.get('alert_enabled', True)
    
    if not criteria:
        return jsonify({'error': 'Criteria required'}), 400
    
    search_id = generate_search_id(user_id, name)
    now = datetime.utcnow().isoformat()
    
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("""
            INSERT INTO saved_searches
            (id, user_id, name, criteria, alert_frequency, alert_enabled, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, [
            search_id, user_id, name, json.dumps(criteria),
            alert_frequency, 1 if alert_enabled else 0, now, now
        ])

        conn.commit()
    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'search_id': search_id,
        'message': f'Saved search "{name}" created'
    })

@saved_searches_bp.route('/api/saved-searches/<search_id>', methods=['GET'])
def get_saved_search(search_id):
    """Get a specific saved search with current matches"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        c.execute("SELECT * FROM saved_searches WHERE id = %s", [search_id])
        row = c.fetchone()
    finally:
        conn.close()
    
    if not row:
        return jsonify({'error': 'Search not found'}), 404
    
    search = dict(row)
    search['criteria'] = json.loads(search['criteria'])
    
    # Get current matches
    matches = get_matching_facilities(search['criteria'])
    search['current_matches'] = len(matches)
    search['sample_matches'] = matches[:10]  # First 10
    
    return jsonify({
        'success': True,
        'search': search
    })

@saved_searches_bp.route('/api/saved-searches/<search_id>', methods=['PUT'])
def update_saved_search(search_id):
    """Update a saved search"""
    data = request.get_json() or {}
    
    updates = []
    params = []
    
    if 'name' in data:
        updates.append('name = %s')
        params.append(data['name'])
    
    if 'criteria' in data:
        updates.append('criteria = %s')
        params.append(json.dumps(data['criteria']))
    
    if 'alert_frequency' in data:
        updates.append('alert_frequency = %s')
        params.append(data['alert_frequency'])
    
    if 'alert_enabled' in data:
        updates.append('alert_enabled = %s')
        params.append(1 if data['alert_enabled'] else 0)
    
    if not updates:
        return jsonify({'error': 'No updates provided'}), 400
    
    updates.append('updated_at = %s')
    params.append(datetime.utcnow().isoformat())
    params.append(search_id)
    
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE saved_searches SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
    finally:
        conn.close()
    
    return jsonify({'success': True, 'message': 'Search updated'})

@saved_searches_bp.route('/api/saved-searches/<search_id>', methods=['DELETE'])
def delete_saved_search(search_id):
    """Delete a saved search"""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM saved_searches WHERE id = %s", [search_id])
        c.execute("DELETE FROM search_alerts WHERE search_id = %s", [search_id])
        conn.commit()
    finally:
        conn.close()
    
    return jsonify({'success': True, 'message': 'Search deleted'})

@saved_searches_bp.route('/api/saved-searches/<search_id>/run', methods=['POST'])
def run_saved_search(search_id):
    """Run a saved search and return matches"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        c.execute("SELECT criteria FROM saved_searches WHERE id = %s", [search_id])
        row = c.fetchone()
    finally:
        conn.close()
    
    if not row:
        return jsonify({'error': 'Search not found'}), 404
    
    criteria = json.loads(row['criteria'])
    matches = get_matching_facilities(criteria)
    
    return jsonify({
        'success': True,
        'matches': matches,
        'count': len(matches)
    })

@saved_searches_bp.route('/api/saved-searches/check-new', methods=['POST'])
def check_new_matches():
    """Check all saved searches for new matches (called by scheduler)"""
    since = request.json.get('since') if request.json else None
    if not since:
        # Default to last 24 hours
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        c.execute("SELECT * FROM saved_searches WHERE alert_enabled = 1")
        searches = c.fetchall()

        results = []
        for search in searches:
            search_dict = dict(search)
            criteria = json.loads(search_dict['criteria'])

            # Find new matches since last check
            check_since = search_dict.get('last_checked') or since
            new_matches = get_matching_facilities(criteria, check_since)

            if new_matches:
                # Record alert
                alert_id = c.execute("""
                    INSERT INTO search_alerts
                    (search_id, user_id, facilities_matched, facilities_count, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """, [
                    search_dict['id'],
                    search_dict['user_id'],
                    json.dumps([m['id'] for m in new_matches[:50]]),
                    len(new_matches),
                    datetime.utcnow().isoformat()
                ]).lastrowid

                results.append({
                    'search_id': search_dict['id'],
                    'search_name': search_dict['name'],
                    'user_id': search_dict['user_id'],
                    'new_matches': len(new_matches),
                    'alert_id': alert_id
                })

            # Update last checked
            c.execute("""
                UPDATE saved_searches
                SET last_checked = %s, match_count = %s
                WHERE id = %s
            """, [datetime.utcnow().isoformat(), len(new_matches), search_dict['id']])

        conn.commit()
    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'searches_checked': len(searches),
        'alerts_generated': len(results),
        'results': results
    })

@saved_searches_bp.route('/api/saved-searches/alerts', methods=['GET'])
def get_alerts_history():
    """Get alert history for a user"""
    user_id = request.args.get('user_id') or request.headers.get('X-User-Id', 'anonymous')
    limit = min(int(request.args.get('limit', 50)), 100)
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()

        c.execute("""
            SELECT a.*, s.name as search_name
            FROM search_alerts a
            JOIN saved_searches s ON a.search_id = s.id
            WHERE a.user_id = %s
            ORDER BY a.created_at DESC
            LIMIT %s
        """, [user_id, limit])

        alerts = [dict(row) for row in c.fetchall()]
    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'alerts': alerts
    })

# =============================================================================
# ALERT EMAIL INTEGRATION
# =============================================================================

def send_alert_email(user_email: str, search_name: str, matches: List[Dict]):
    """Send alert email for new matches"""
    try:
        from email_service import send_email
        
        # Build email content
        subject = f"🔔 DC Hub Alert: {len(matches)} new facilities match '{search_name}'"
        
        facilities_html = ""
        for m in matches[:10]:
            facilities_html += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #eee;">{m.get('name', 'Unknown')}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{m.get('city', '')}, {m.get('state', '')}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{m.get('provider', '')}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{m.get('power_mw', 0)} MW</td>
            </tr>
            """
        
        html_content = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
            <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:24px;border-radius:12px 12px 0 0;">
                <h1 style="color:white;margin:0;font-size:24px;">🔔 New Facilities Found!</h1>
            </div>
            <div style="background:#f9fafb;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">
                <p style="color:#374151;font-size:16px;">
                    Your saved search <strong>"{search_name}"</strong> has {len(matches)} new matching facilities.
                </p>
                
                <table style="width:100%;border-collapse:collapse;margin:20px 0;">
                    <thead>
                        <tr style="background:#e5e7eb;">
                            <th style="padding:10px;text-align:left;">Facility</th>
                            <th style="padding:10px;text-align:left;">Location</th>
                            <th style="padding:10px;text-align:left;">Provider</th>
                            <th style="padding:10px;text-align:left;">Power</th>
                        </tr>
                    </thead>
                    <tbody>
                        {facilities_html}
                    </tbody>
                </table>
                
                {"<p style='color:#6b7280;font-size:14px;'>+ " + str(len(matches) - 10) + " more facilities</p>" if len(matches) > 10 else ""}
                
                <a href="https://dchub.cloud/dashboard.html" style="display:inline-block;background:#6366f1;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:16px;">
                    View All Matches →
                </a>
                
                <p style="color:#9ca3af;font-size:12px;margin-top:24px;">
                    You're receiving this because you have alerts enabled for this saved search.
                    <a href="https://dchub.cloud/settings" style="color:#6366f1;">Manage alerts</a>
                </p>
            </div>
        </div>
        """
        
        send_email(user_email, subject, html_content)
        return True
        
    except Exception as e:
        print(f"Failed to send alert email: {e}")
        return False

# =============================================================================
# REGISTER BLUEPRINT
# =============================================================================

def register_saved_searches(app):
    """Register saved searches blueprint"""
    app.register_blueprint(saved_searches_bp)
    print("💾 Saved Searches API registered")

__all__ = ['saved_searches_bp', 'register_saved_searches', 'check_new_matches']
