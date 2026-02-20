"""
DC Hub — Public Endpoints (v2.0 - PostgreSQL)
===============================================
All queries use Neon PostgreSQL via db_utils.

Add ONE line to main.py:
    from public_endpoints import register_public_routes; register_public_routes(app)
"""

import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from db_utils import get_db, get_read_db

logger = logging.getLogger(__name__)

APP_VERSION = "v91"
APP_BUILD = 91
RELEASE_NOTES = "Dynamic version banner, public API endpoints, 401 fixes"
FOUNDING_TOTAL = 50
FOUNDING_CLAIMED = 3

public_bp = Blueprint('public_endpoints', __name__)


# =============================================================================
# /api/v1/version — Public, no auth
# =============================================================================
@public_bp.route('/api/v1/version', methods=['GET'])
def get_version():
    result = {
        'version': APP_VERSION,
        'build': APP_BUILD,
        'release_notes': RELEASE_NOTES,
        'facilities': 20000,
        'markets': 35,
        'deals': 673,
        'updated_at': datetime.utcnow().isoformat()
    }
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM facilities")
            row = cursor.fetchone()
            if row and row[0]:
                result['facilities'] = row[0]
        except Exception:
            pass
        try:
            cursor.execute("SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL")
            row = cursor.fetchone()
            if row and row[0]:
                result['markets'] = row[0]
        except Exception:
            pass
        try:
            cursor.execute("SELECT COUNT(*) FROM deals")
            row = cursor.fetchone()
            if row and row[0]:
                result['deals'] = row[0]
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"/api/v1/version DB error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return jsonify(result)


# =============================================================================
# /api/founding-members — Public, no auth
# =============================================================================
@public_bp.route('/api/founding-members', methods=['GET'])
def founding_members_status():
    claimed = FOUNDING_CLAIMED
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM user_plans WHERE plan IN ('founding', 'pro')")
            row = cursor.fetchone()
            if row and row[0]:
                claimed = row[0]
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    remaining = FOUNDING_TOTAL - claimed
    return jsonify({
        'total': FOUNDING_TOTAL,
        'claimed': claimed,
        'remaining': remaining,
        'price': 99,
        'regular_price': 299,
        'program_active': remaining > 0
    })


# =============================================================================
# /api/v1/map/public — Public facility markers, no auth
# =============================================================================
@public_bp.route('/api/v1/map/public', methods=['GET'])
def public_map_data():
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, provider, city, state, country, region,
                   latitude, longitude, power_mw, status, tier
            FROM facilities
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            LIMIT 5000
        """)
        facilities = []
        for row in cursor.fetchall():
            facilities.append({
                'id': row[0], 'name': row[1], 'provider': row[2],
                'city': row[3], 'state': row[4], 'country': row[5],
                'region': row[6], 'lat': row[7], 'lng': row[8],
                'power_mw': row[9], 'status': row[10], 'tier': row[11]
            })
        return jsonify({'success': True, 'count': len(facilities), 'facilities': facilities})
    except Exception as e:
        logger.error(f"/api/v1/map/public error: {e}")
        return jsonify({'success': False, 'error': str(e), 'facilities': []}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# /api/v1/map — Public facility data for map view, no auth
# =============================================================================
@public_bp.route('/api/v1/map', methods=['GET'])
def public_map_view():
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, city, state, country, latitude, longitude,
                   provider, power_mw, tier, status
            FROM facilities
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            LIMIT 500
        """)
        facilities = []
        for row in cursor.fetchall():
            facilities.append({
                'id': row[0], 'name': row[1], 'city': row[2],
                'state': row[3], 'country': row[4], 'latitude': row[5],
                'longitude': row[6], 'provider': row[7], 'power_mw': row[8],
                'tier': row[9], 'status': row[10]
            })
        cursor.execute("SELECT COUNT(*) FROM facilities")
        total = cursor.fetchone()[0] or 0
        return jsonify({'success': True, 'facilities': facilities, 'total': total})
    except Exception as e:
        logger.error(f"/api/v1/map error: {e}")
        return jsonify({'success': False, 'error': str(e), 'facilities': [], 'total': 0}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# /api/transactions/public — Public M&A deals, no auth
# =============================================================================
@public_bp.route('/api/transactions/public', methods=['GET'])
def public_transactions():
    limit = request.args.get('limit', 15, type=int)
    limit = min(limit, 25)
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, buyer, seller, value, mw, type, region, market, date, notes
            FROM deals
            WHERE buyer IS NOT NULL AND buyer != '' AND buyer != 'TBD'
              AND value > 0 AND value < 1000000
              AND LENGTH(buyer) > 3
            ORDER BY date DESC NULLS LAST, value DESC
            LIMIT %s
        """, (limit,))
        transactions = []
        for row in cursor.fetchall():
            rid, buyer, seller, value, mw, dtype, region, market, date, notes = row
            value = value or 0
            if value >= 1000:
                value_display = f"${value/1000:.1f}B"
            elif value > 0:
                value_display = f"${value:.0f}M"
            else:
                value_display = "Undisclosed"
            transactions.append({
                'id': rid,
                'title': f"{buyer} {'acquires' if dtype in ('Acquisition','ma') else 'invests in'} {seller or 'N/A'}",
                'buyer': buyer, 'seller': seller,
                'value_usd_millions': value, 'value_display': value_display,
                'power_mw': mw or 0, 'type': dtype,
                'region': region, 'market': market,
                'date': date, 'notes': notes,
                'source': 'DC Hub Intelligence',
            })
        return jsonify({'success': True, 'data': transactions, 'total': len(transactions)})
    except Exception as e:
        logger.error(f"/api/transactions/public error: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': [], 'total': 0}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# /api/v1/market-report — Public market summary, no auth
# =============================================================================
@public_bp.route('/api/v1/market-report', methods=['GET'])
def market_report():
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM facilities")
        total_facilities = cursor.fetchone()[0] or 0
        total_transactions = 0
        try:
            cursor.execute("SELECT COUNT(*) FROM deals")
            total_transactions = cursor.fetchone()[0] or 0
        except Exception:
            pass
        markets = 0
        try:
            cursor.execute("SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL")
            markets = cursor.fetchone()[0] or 0
        except Exception:
            pass
        return jsonify({
            'success': True,
            'report': {
                'total_facilities': total_facilities,
                'total_transactions': total_transactions,
                'markets': markets,
                'last_updated': datetime.utcnow().isoformat()
            }
        })
    except Exception as e:
        logger.error(f"/api/v1/market-report error: {e}")
        return jsonify({'success': False, 'error': str(e), 'report': {}}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# /api/v1/market-growth — Market growth data with projections
# =============================================================================
@public_bp.route('/api/v1/market-growth', methods=['GET'])
def market_growth():
    current_facilities = 0
    current_power = 0
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM facilities")
        current_facilities = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw > 0")
        current_power = cursor.fetchone()[0] or 0
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    growth_data = {
        'success': True,
        'years': [2020, 2021, 2022, 2023, 2024, 2025, 2026],
        'facilities': [4200, 5100, 6300, 7800, 9200, current_facilities or 10436, int((current_facilities or 10436) * 1.18)],
        'power_gw': [35.2, 42.1, 52.8, 68.5, 89.3, 118.0, 145.0],
        'investment_billions': [45, 58, 72, 95, 142, 220, 310],
        'market_size_billions': [59.3, 68.0, 79.2, 93.8, 114.5, 142.0, 178.0],
        'projection_note': '2026 values are projections based on current market trends',
        'sources': ['CBRE', 'JLL', 'Cushman & Wakefield', 'DC Hub Intelligence'],
        'generated_at': datetime.utcnow().isoformat()
    }

    return jsonify(growth_data)


# =============================================================================
# REGISTRATION
# =============================================================================
def register_public_routes(app):
    """Register all public endpoints on the Flask app."""
    app.register_blueprint(public_bp)
    logger.info("✅ Public Endpoints registered: /api/v1/version, /api/founding-members, /api/v1/map/public, /api/transactions/public")
    print("✅ Public Endpoints registered: /api/v1/version, /api/founding-members, /api/v1/map/public, /api/transactions/public")
