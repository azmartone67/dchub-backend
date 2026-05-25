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
# /api/v1/sitemap/ping — POST tells Google + Bing about updated content
# =============================================================================
# r41-sitemap-ping (2026-05-25): when LinkedIn quad, press publisher, or
# auto-press fires new content, call this endpoint to nudge search
# engines. Search bots usually find new sitemap content within 24-72h
# on their own crawl schedule; ping shortcuts that to minutes-to-hours.
#
# Idempotent + rate-limited (1 ping per minute per engine via in-process
# state) so an over-eager scheduler can't get us flagged as abusive.
# Returns immediately even if the ping fetch itself stalls — we don't
# block the caller on someone else's network.
_PING_STATE = {"last_google_ping": 0.0, "last_bing_ping": 0.0}

@public_bp.route('/api/v1/sitemap/ping', methods=['POST', 'GET'])
def sitemap_ping():
    import time as _ping_time
    import threading as _ping_thr
    import urllib.request as _ping_url
    sitemap = request.args.get('sitemap') or 'https://dchub.cloud/sitemap.xml'
    now = _ping_time.time()
    result = {'sitemap': sitemap, 'pinged': [], 'rate_limited': [], 'failed': []}

    def _fire(name, url, last_key, min_interval):
        if now - _PING_STATE.get(last_key, 0.0) < min_interval:
            result['rate_limited'].append(name)
            return
        _PING_STATE[last_key] = now
        def _bg():
            try:
                req = _ping_url.Request(url,
                    headers={'User-Agent': 'DCHub-SitemapPing/1.0'})
                with _ping_url.urlopen(req, timeout=10) as r:
                    if 200 <= r.status < 300:
                        return
                # non-2xx → quietly record in next pull; don't block
            except Exception as e:
                logger.warning(f"sitemap ping {name} failed: {e}")
        _ping_thr.Thread(target=_bg, daemon=True).start()
        result['pinged'].append(name)

    # Google: 1 ping per 5 min cap (their docs deprecate ping but it still
    # works — and we want to be polite). Bing: 1 per 5 min.
    _fire('google', f'https://www.google.com/ping?sitemap={sitemap}',
          'last_google_ping', 300)
    _fire('bing',   f'https://www.bing.com/ping?sitemap={sitemap}',
          'last_bing_ping',   300)
    return jsonify(result), 200, {'Cache-Control': 'no-store'}


# =============================================================================
# /api/v1/version — Public, no auth
# =============================================================================
_VERSION_CACHE = {"result": None, "ts": 0}

@public_bp.route('/api/v1/version', methods=['GET'])
def get_version():
    # r41-version-speed (2026-05-25): pre-fix this endpoint did 3 COUNT(*)
    # calls on facilities + deals on every request (4-5s cold, ~700ms
    # warm) AND had no Cache-Control so CF couldn't edge-cache it.
    # Hit by every monitor, dashboard, and AI-discovery crawler that
    # checks "what version is dchub on?". Fixes:
    #   (1) replace COUNT(*) with pg_class.reltuples (sub-ms planner
    #       statistic) — facilities is 21k rows, country DISTINCT is
    #       the slow one; keep that as-is since reltuples can't
    #       express DISTINCT.
    #   (2) 60s in-process cache so concurrent requests share work.
    #   (3) Cache-Control header so CF edge-caches 5min, with stale-
    #       while-revalidate so the first request after expiry never
    #       blocks waiting for our DB.
    import time as _t
    if _VERSION_CACHE["result"] and (_t.time() - _VERSION_CACHE["ts"]) < 60:
        return jsonify(_VERSION_CACHE["result"]), 200, {
            "Cache-Control": "public, max-age=300, s-maxage=600, stale-while-revalidate=3600",
        }
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
        # facilities count via planner statistic (instant)
        try:
            cursor.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE relname = 'facilities'")
            row = cursor.fetchone()
            if row and row[0]:
                result['facilities'] = int(row[0])
        except Exception:
            pass
        # markets — DISTINCT can't use reltuples; keep COUNT but with
        # 60s in-process cache so it only runs once per minute.
        try:
            cursor.execute(
                "SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL")
            row = cursor.fetchone()
            if row and row[0]:
                result['markets'] = row[0]
        except Exception:
            pass
        # deals count via planner statistic
        try:
            cursor.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE relname = 'deals'")
            row = cursor.fetchone()
            if row and row[0]:
                result['deals'] = int(row[0])
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
    _VERSION_CACHE["result"] = result
    _VERSION_CACHE["ts"] = _t.time()
    return jsonify(result), 200, {
        "Cache-Control": "public, max-age=300, s-maxage=600, stale-while-revalidate=3600",
    }


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


# Phase UU-3 (2026-05-15): removed @public_bp.route('/api/v1/map') below —
# main.py:1879 has a richer implementation (joins discovered_facilities
# + facilities, generates slugs, returns 19K rows). Tests confirmed the
# live endpoint serves main.py's version. The blueprint version was the
# shadow. Function body kept as `_unused_public_map_view` for reference;
# delete in next cleanup pass.
def _unused_public_map_view():
    conn = None
    try:
        from flask import request as req
        load_all = req.args.get('all', '').lower() in ('true', '1', 'yes')
        limit = min(int(req.args.get('limit', 2000)), 50000)
        offset = int(req.args.get('offset', 0))

        conn = get_read_db()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM facilities WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
        total = cursor.fetchone()[0] or 0

        if load_all:
            cursor.execute("""
                SELECT id, name, city, state, country, latitude, longitude,
                       provider, power_mw, tier, status
                FROM facilities
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (limit, offset))
        else:
            cursor.execute("""
                SELECT id, name, city, state, country, latitude, longitude,
                       provider, power_mw, tier, status
                FROM facilities
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (limit, offset))

        facilities = []
        for row in cursor.fetchall():
            facilities.append({
                'id': row[0], 'name': row[1], 'city': row[2],
                'state': row[3], 'country': row[4], 'latitude': row[5],
                'longitude': row[6], 'provider': row[7], 'power_mw': row[8],
                'tier': row[9], 'status': row[10]
            })
        return jsonify({'success': True, 'data': facilities, 'facilities': facilities, 'total': total, 'count': len(facilities), 'offset': offset, 'has_more': (offset + len(facilities)) < total})
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
