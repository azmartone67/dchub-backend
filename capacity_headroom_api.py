"""
DC Hub Capacity Headroom API v1.0
==================================
Calculates spare grid capacity, gas pipeline headroom, and market readiness
scores for all 8 monitored DC markets. Combines:

- EIA installed generation capacity (MW) per ISO/RTO
- EIA real-time grid demand (MW) to calculate spare megawatts
- Gas pipeline capacity vs estimated utilization
- Fiber connectivity density
- Electricity pricing trends

Produces a 0-100 "Market Readiness" score and heatmap-ready data.
Runs automated background refresh every 30 minutes.

All endpoints gated to Pro tier.
"""

import os
import json
import time
import requests
import threading
import logging
from functools import wraps
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
from db_utils import get_db

logger = logging.getLogger(__name__)


def require_plan(min_plan='pro'):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                from api_tier_gating import validate_api_key, user_has_access
                api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
                if not api_key:
                    return jsonify({
                        'success': False,
                        'error': 'authentication_required',
                        'message': f'This endpoint requires a {min_plan.title()} plan or higher.',
                        'signup_url': 'https://dchub.cloud/signup',
                    }), 401
                valid, info = validate_api_key(api_key)
                if not valid:
                    return jsonify({
                        'success': False,
                        'error': 'invalid_api_key',
                        'message': 'Invalid or inactive API key',
                    }), 401
                user_plan = info.get('plan', 'free')
                if not user_has_access(user_plan, min_plan):
                    return jsonify({
                        'success': False,
                        'error': 'plan_upgrade_required',
                        'message': f'This endpoint requires {min_plan.title()} plan. You are on {user_plan.title()}.',
                        'upgrade_url': 'https://dchub.cloud/pricing',
                    }), 403
                return f(*args, **kwargs)
            except ImportError:
                return f(*args, **kwargs)
            except Exception as e:
                logger.error(f"Tier gating error: {e}")
                return f(*args, **kwargs)
        return wrapper
    return decorator

DB_PATH = "dc_nexus.db"
HEADROOM_CACHE = {}
HEADROOM_REFRESH_INTERVAL = 1800  # 30 minutes

MARKET_ISO_MAP = {
    'phoenix':           {'iso': 'CAISO', 'eia_code': 'CISO', 'state': 'AZ', 'name': 'Phoenix, AZ'},
    'dallas':            {'iso': 'ERCOT', 'eia_code': 'ERCO', 'state': 'TX', 'name': 'Dallas, TX'},
    'northern_virginia': {'iso': 'PJM',   'eia_code': 'PJM',  'state': 'VA', 'name': 'Northern Virginia'},
    'atlanta':           {'iso': 'MISO',  'eia_code': 'MISO', 'state': 'GA', 'name': 'Atlanta, GA'},
    'las_vegas':         {'iso': 'CAISO', 'eia_code': 'CISO', 'state': 'NV', 'name': 'Las Vegas, NV'},
    'salt_lake':         {'iso': 'CAISO', 'eia_code': 'CISO', 'state': 'UT', 'name': 'Salt Lake City, UT'},
    'columbus':          {'iso': 'PJM',   'eia_code': 'PJM',  'state': 'OH', 'name': 'Columbus, OH'},
    'des_moines':        {'iso': 'MISO',  'eia_code': 'MISO', 'state': 'IA', 'name': 'Des Moines, IA'},
}

ISO_INSTALLED_CAPACITY_MW = {
    'CAISO': 85000,
    'ERCOT': 140000,
    'PJM':   190000,
    'MISO':  195000,
    'SPP':   92000,
    'NYISO': 42000,
    'ISONE': 33000,
}

GAS_UTILIZATION_ESTIMATES = {
    'TX': 0.72,
    'VA': 0.65,
    'GA': 0.58,
    'AZ': 0.61,
    'NV': 0.55,
    'UT': 0.48,
    'OH': 0.63,
    'IA': 0.52,
}

ELECTRICITY_RATE_BENCHMARKS = {
    'AZ': 6.8,
    'TX': 5.9,
    'VA': 7.2,
    'GA': 6.5,
    'NV': 7.1,
    'UT': 5.4,
    'OH': 6.9,
    'IA': 5.7,
}


def init_headroom_db():
    try:
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS capacity_headroom_snapshots (
                    id SERIAL PRIMARY KEY,
                    market TEXT NOT NULL,
                    iso TEXT NOT NULL,
                    installed_capacity_mw REAL,
                    current_demand_mw REAL,
                    spare_capacity_mw REAL,
                    spare_capacity_pct REAL,
                    gas_pipeline_capacity_mdth REAL,
                    gas_utilization_pct REAL,
                    gas_headroom_mdth REAL,
                    fiber_route_count INTEGER,
                    electricity_rate_cents REAL,
                    market_readiness_score REAL,
                    readiness_grade TEXT,
                    grid_signal TEXT,
                    gas_signal TEXT,
                    snapshot_at TEXT NOT NULL,
                    data_source TEXT DEFAULT 'EIA/GridStatus'
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_headroom_market
                ON capacity_headroom_snapshots(market, snapshot_at DESC)
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS headroom_trend_daily (
                    id SERIAL PRIMARY KEY,
                    market TEXT NOT NULL,
                    date TEXT NOT NULL,
                    avg_spare_mw REAL,
                    peak_demand_mw REAL,
                    min_spare_pct REAL,
                    avg_readiness_score REAL,
                    UNIQUE(market, date)
                )
            """)
            conn.commit()
        finally:
            conn.close()
        logger.info("Capacity headroom tables initialized")
    except Exception as e:
        logger.error(f"Headroom DB init error: {e}")


def fetch_eia_demand(eia_code):
    api_key = os.environ.get('EIA_API_KEY', '')
    if not api_key:
        return None

    try:
        url = (
            f"https://api.eia.gov/v2/electricity/rto/region-data/data/"
            f"?api_key={api_key}&frequency=hourly&data[0]=value"
            f"&facets[respondent][]={eia_code}&facets[type][]=D"
            f"&sort[0][column]=period&sort[0][direction]=desc&length=1"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        records = data.get('response', {}).get('data', [])
        if records:
            return {
                'demand_mw': round(float(records[0].get('value', 0))),
                'timestamp': records[0].get('period')
            }
    except Exception as e:
        logger.warning(f"EIA demand fetch for {eia_code}: {e}")
    return None


def fetch_eia_retail_rate(state):
    api_key = os.environ.get('EIA_API_KEY', '')
    if not api_key:
        return ELECTRICITY_RATE_BENCHMARKS.get(state)

    try:
        url = (
            f"https://api.eia.gov/v2/electricity/retail-sales/data/"
            f"?api_key={api_key}&frequency=monthly&data[0]=price"
            f"&facets[stateid][]={state}&facets[sectorid][]=IND"
            f"&sort[0][column]=period&sort[0][direction]=desc&length=1"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        records = data.get('response', {}).get('data', [])
        if records and records[0].get('price'):
            return round(float(records[0]['price']), 2)
    except Exception as e:
        logger.warning(f"EIA retail rate for {state}: {e}")

    return ELECTRICITY_RATE_BENCHMARKS.get(state)


def get_market_power_data(market_key):
    try:
        conn = get_db()
        try:
            # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
            c = conn.cursor()

            c.execute("""
                SELECT SUM(capacity_mw) as total_mw, COUNT(*) as count
                FROM discovered_power_plants WHERE market = %s
            """, (market_key,))
            row = c.fetchone()
            local_capacity_mw = round(row['total_mw'] or 0, 1) if row else 0
            plant_count = row['count'] or 0 if row else 0

            c.execute("""
                SELECT fuel_type, SUM(capacity_mw) as cap
                FROM discovered_power_plants WHERE market = %s
                GROUP BY fuel_type ORDER BY cap DESC
            """, (market_key,))
            fuel_mix = {r['fuel_type']: round(r['cap'] or 0, 1) for r in c.fetchall()}

        finally:
            conn.close()
        return {
            'local_capacity_mw': local_capacity_mw,
            'plant_count': plant_count,
            'fuel_mix': fuel_mix
        }
    except Exception as e:
        logger.warning(f"Power data for {market_key}: {e}")
        return {'local_capacity_mw': 0, 'plant_count': 0, 'fuel_mix': {}}


def get_market_gas_data(market_key):
    try:
        conn = get_db()
        try:
            # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
            c = conn.cursor()

            c.execute("""
                SELECT COUNT(*) as count, SUM(capacity_mdth) as total
                FROM discovered_pipelines WHERE market = %s
            """, (market_key,))
            row = c.fetchone()
        finally:
            conn.close()

        return {
            'pipeline_count': row['count'] or 0 if row else 0,
            'total_capacity_mdth': round(row['total'] or 0, 1) if row else 0
        }
    except Exception as e:
        logger.warning(f"Gas data for {market_key}: {e}")
        return {'pipeline_count': 0, 'total_capacity_mdth': 0}


def get_fiber_count(market_key):
    try:
        from fiber_network_discovery import get_fiber_routes
        routes = get_fiber_routes()
        count = 0
        for route in routes:
            markets_served = route.get('markets', [])
            if market_key in markets_served or market_key.replace('_', ' ') in str(route.get('name', '')).lower():
                count += 1
        return max(count, 1)
    except:
        return 2


def calculate_readiness_score(spare_pct, gas_util_pct, fiber_count, rate_cents):
    grid_score = min(100, max(0, spare_pct * 2.5))

    gas_headroom_pct = max(0, 100 - gas_util_pct)
    gas_score = min(100, gas_headroom_pct * 2.0)

    fiber_score = min(100, fiber_count * 20)

    best_rate = 4.5
    worst_rate = 12.0
    if rate_cents is None:
        rate_cents = 7.0
    rate_norm = max(0, min(1, (worst_rate - rate_cents) / (worst_rate - best_rate)))
    cost_score = rate_norm * 100

    readiness = (
        grid_score * 0.35 +
        gas_score * 0.25 +
        fiber_score * 0.20 +
        cost_score * 0.20
    )

    return round(min(100, max(0, readiness)), 1)


def score_to_grade(score):
    if score >= 85:
        return 'A'
    elif score >= 70:
        return 'B'
    elif score >= 55:
        return 'C'
    elif score >= 40:
        return 'D'
    else:
        return 'F'


def get_signal(value, thresholds):
    if value >= thresholds['green']:
        return 'green'
    elif value >= thresholds['yellow']:
        return 'yellow'
    else:
        return 'red'


def compute_market_headroom(market_key):
    market = MARKET_ISO_MAP[market_key]
    iso = market['iso']
    eia_code = market['eia_code']
    state = market['state']

    installed_capacity = ISO_INSTALLED_CAPACITY_MW.get(iso, 100000)

    eia_demand = fetch_eia_demand(eia_code)
    if eia_demand:
        current_demand = eia_demand['demand_mw']
        demand_timestamp = eia_demand['timestamp']
        data_source = 'EIA Live'
    else:
        demand_ratios = {
            'CAISO': 0.55, 'ERCOT': 0.60, 'PJM': 0.58,
            'MISO': 0.52, 'SPP': 0.48, 'NYISO': 0.62, 'ISONE': 0.58
        }
        ratio = demand_ratios.get(iso, 0.55)
        current_demand = round(installed_capacity * ratio)
        demand_timestamp = datetime.utcnow().isoformat()
        data_source = 'Estimated'

    spare_mw = installed_capacity - current_demand
    spare_pct = round((spare_mw / installed_capacity) * 100, 1) if installed_capacity > 0 else 0

    gas_data = get_market_gas_data(market_key)
    gas_util_pct = GAS_UTILIZATION_ESTIMATES.get(state, 0.55) * 100
    gas_headroom_mdth = round(gas_data['total_capacity_mdth'] * (1 - GAS_UTILIZATION_ESTIMATES.get(state, 0.55)), 1)

    fiber_count = get_fiber_count(market_key)

    rate_cents = fetch_eia_retail_rate(state)

    power_data = get_market_power_data(market_key)

    readiness = calculate_readiness_score(spare_pct, gas_util_pct, fiber_count, rate_cents)
    grade = score_to_grade(readiness)

    grid_signal = get_signal(spare_pct, {'green': 30, 'yellow': 15})
    gas_signal = get_signal(100 - gas_util_pct, {'green': 30, 'yellow': 15})

    result = {
        'market': market_key,
        'name': market['name'],
        'iso': iso,
        'state': state,
        'grid': {
            'installed_capacity_mw': installed_capacity,
            'current_demand_mw': current_demand,
            'spare_capacity_mw': spare_mw,
            'spare_capacity_pct': spare_pct,
            'signal': grid_signal,
            'demand_timestamp': demand_timestamp,
            'data_source': data_source
        },
        'gas': {
            'pipeline_count': gas_data['pipeline_count'],
            'total_capacity_mdth': gas_data['total_capacity_mdth'],
            'utilization_pct': round(gas_util_pct, 1),
            'headroom_mdth': gas_headroom_mdth,
            'signal': gas_signal
        },
        'power': {
            'local_plants': power_data['plant_count'],
            'local_capacity_mw': power_data['local_capacity_mw'],
            'fuel_mix': power_data['fuel_mix']
        },
        'fiber': {
            'route_count': fiber_count
        },
        'cost': {
            'electricity_rate_cents_kwh': rate_cents,
            'rate_vs_national_avg': round((rate_cents or 7) - 7.5, 2)
        },
        'readiness': {
            'score': readiness,
            'grade': grade,
            'label': f"{'Excellent' if grade == 'A' else 'Good' if grade == 'B' else 'Moderate' if grade == 'C' else 'Limited' if grade == 'D' else 'Constrained'} Capacity"
        },
        'computed_at': datetime.utcnow().isoformat()
    }

    return result


def save_snapshot(result):
    import time as _time
    for attempt in range(5):
        try:
            conn = get_db()
            try:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO capacity_headroom_snapshots
                    (market, iso, installed_capacity_mw, current_demand_mw, spare_capacity_mw,
                     spare_capacity_pct, gas_pipeline_capacity_mdth, gas_utilization_pct,
                     gas_headroom_mdth, fiber_route_count, electricity_rate_cents,
                     market_readiness_score, readiness_grade, grid_signal, gas_signal,
                     snapshot_at, data_source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    result['market'], result['iso'],
                    result['grid']['installed_capacity_mw'],
                    result['grid']['current_demand_mw'],
                    result['grid']['spare_capacity_mw'],
                    result['grid']['spare_capacity_pct'],
                    result['gas']['total_capacity_mdth'],
                    result['gas']['utilization_pct'],
                    result['gas']['headroom_mdth'],
                    result['fiber']['route_count'],
                    result['cost']['electricity_rate_cents_kwh'],
                    result['readiness']['score'],
                    result['readiness']['grade'],
                    result['grid']['signal'],
                    result['gas']['signal'],
                    result['computed_at'],
                    result['grid']['data_source']
                ))

                today = datetime.utcnow().strftime('%Y-%m-%d')
                c.execute("""
                    INSERT INTO headroom_trend_daily (market, date, avg_spare_mw, peak_demand_mw, min_spare_pct, avg_readiness_score)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(market, date) DO UPDATE SET
                        avg_spare_mw = (avg_spare_mw + excluded.avg_spare_mw) / 2,
                        peak_demand_mw = MAX(peak_demand_mw, excluded.peak_demand_mw),
                        min_spare_pct = MIN(min_spare_pct, excluded.min_spare_pct),
                        avg_readiness_score = (avg_readiness_score + excluded.avg_readiness_score) / 2
                """, (
                    result['market'], today,
                    result['grid']['spare_capacity_mw'],
                    result['grid']['current_demand_mw'],
                    result['grid']['spare_capacity_pct'],
                    result['readiness']['score']
                ))

                conn.commit()
            finally:
                conn.close()
            return
        except Exception as e:
            if 'locked' in str(e) and attempt < 4:
                _time.sleep(5.0 * (attempt + 1))
                continue
            logger.warning(f"Snapshot save error: {e}")
            return
        except Exception as e:
            logger.warning(f"Snapshot save error: {e}")
            return


def refresh_all_headroom():
    logger.info("Refreshing capacity headroom for all markets...")
    results = {}
    for market_key in MARKET_ISO_MAP:
        try:
            result = compute_market_headroom(market_key)
            results[market_key] = result
            save_snapshot(result)
        except Exception as e:
            logger.error(f"Headroom compute error for {market_key}: {e}")

    HEADROOM_CACHE['all_markets'] = results
    HEADROOM_CACHE['last_refresh'] = datetime.utcnow().isoformat()
    HEADROOM_CACHE['refresh_count'] = HEADROOM_CACHE.get('refresh_count', 0) + 1

    scores = [r['readiness']['score'] for r in results.values() if r]
    HEADROOM_CACHE['summary'] = {
        'markets_computed': len(results),
        'avg_readiness': round(sum(scores) / len(scores), 1) if scores else 0,
        'best_market': max(results.items(), key=lambda x: x[1]['readiness']['score'])[0] if results else None,
        'tightest_market': min(results.items(), key=lambda x: x[1]['readiness']['score'])[0] if results else None,
    }

    logger.info(f"Headroom refresh complete: {len(results)} markets, avg readiness {HEADROOM_CACHE['summary']['avg_readiness']}")
    return results


def start_headroom_scheduler(delay_seconds=120):
    def _run():
        time.sleep(delay_seconds)
        while True:
            try:
                refresh_all_headroom()
            except Exception as e:
                logger.error(f"Headroom scheduler error: {e}")
            time.sleep(HEADROOM_REFRESH_INTERVAL)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info(f"Capacity headroom scheduler started (delay={delay_seconds}s, interval={HEADROOM_REFRESH_INTERVAL}s)")


def create_headroom_blueprint():
    bp = Blueprint('capacity_headroom', __name__)

    @bp.route('/api/v1/capacity/headroom', methods=['GET'])
    @require_plan('pro')
    def capacity_headroom_all():
        """All markets spare capacity and readiness scores - heatmap ready"""
        cached = HEADROOM_CACHE.get('all_markets')
        if not cached:
            cached = refresh_all_headroom()

        markets_list = []
        for key, data in cached.items():
            markets_list.append(data)

        markets_list.sort(key=lambda x: x['readiness']['score'], reverse=True)

        total_spare_gw = sum(m['grid']['spare_capacity_mw'] for m in markets_list) / 1000

        return jsonify({
            'success': True,
            'endpoint': '/api/v1/capacity/headroom',
            'description': 'Spare grid capacity and market readiness for DC site selection',
            'summary': {
                'markets_analyzed': len(markets_list),
                'total_spare_capacity_gw': round(total_spare_gw, 1),
                'avg_readiness_score': round(sum(m['readiness']['score'] for m in markets_list) / len(markets_list), 1) if markets_list else 0,
                'best_market': markets_list[0]['name'] if markets_list else None,
                'tightest_market': markets_list[-1]['name'] if markets_list else None,
            },
            'markets': markets_list,
            'last_refresh': HEADROOM_CACHE.get('last_refresh'),
            'refresh_interval_minutes': HEADROOM_REFRESH_INTERVAL // 60,
            'data_sources': ['EIA RTO Demand', 'EIA Retail Rates', 'EIA Power Plants', 'FERC Pipelines', 'NTIA BEAD']
        })

    @bp.route('/api/v1/capacity/headroom/<market>', methods=['GET'])
    @require_plan('pro')
    def capacity_headroom_market(market):
        """Single market detailed headroom analysis"""
        market = market.lower().replace('-', '_').replace(' ', '_')

        if market not in MARKET_ISO_MAP:
            return jsonify({
                'success': False,
                'error': f'Unknown market: {market}',
                'available_markets': list(MARKET_ISO_MAP.keys())
            }), 404

        cached = HEADROOM_CACHE.get('all_markets', {})
        if market in cached:
            result = cached[market]
        else:
            result = compute_market_headroom(market)

        return jsonify({
            'success': True,
            'market': result,
            'data_sources': ['EIA RTO Demand', 'EIA Retail Rates', 'EIA Power Plants', 'FERC Pipelines']
        })

    @bp.route('/api/v1/capacity/heatmap', methods=['GET'])
    @require_plan('pro')
    def capacity_heatmap():
        """Simplified heatmap data with lat/lng and scores for map rendering"""
        market_coords = {
            'phoenix':           {'lat': 33.45, 'lng': -112.07},
            'dallas':            {'lat': 32.78, 'lng': -96.80},
            'northern_virginia': {'lat': 38.95, 'lng': -77.45},
            'atlanta':           {'lat': 33.75, 'lng': -84.39},
            'las_vegas':         {'lat': 36.17, 'lng': -115.14},
            'salt_lake':         {'lat': 40.76, 'lng': -111.89},
            'columbus':          {'lat': 39.96, 'lng': -82.99},
            'des_moines':        {'lat': 41.59, 'lng': -93.62},
        }

        cached = HEADROOM_CACHE.get('all_markets')
        if not cached:
            cached = refresh_all_headroom()

        heatmap_points = []
        for key, data in cached.items():
            coords = market_coords.get(key, {'lat': 0, 'lng': 0})
            score = data['readiness']['score']
            grade = data['readiness']['grade']

            if grade == 'A':
                color = '#10b981'
            elif grade == 'B':
                color = '#3b82f6'
            elif grade == 'C':
                color = '#f59e0b'
            elif grade == 'D':
                color = '#ef4444'
            else:
                color = '#991b1b'

            heatmap_points.append({
                'market': key,
                'name': data['name'],
                'lat': coords['lat'],
                'lng': coords['lng'],
                'score': score,
                'grade': grade,
                'label': data['readiness']['label'],
                'color': color,
                'spare_gw': round(data['grid']['spare_capacity_mw'] / 1000, 1),
                'spare_pct': data['grid']['spare_capacity_pct'],
                'grid_signal': data['grid']['signal'],
                'gas_signal': data['gas']['signal'],
                'rate_cents': data['cost']['electricity_rate_cents_kwh'],
                'radius': max(25, min(50, score / 2)),
            })

        heatmap_points.sort(key=lambda x: x['score'], reverse=True)

        return jsonify({
            'success': True,
            'endpoint': '/api/v1/capacity/heatmap',
            'description': 'Heatmap-ready capacity data with coordinates and color coding',
            'points': heatmap_points,
            'legend': {
                'A': {'color': '#10b981', 'label': 'Excellent Capacity (85-100)'},
                'B': {'color': '#3b82f6', 'label': 'Good Capacity (70-84)'},
                'C': {'color': '#f59e0b', 'label': 'Moderate Capacity (55-69)'},
                'D': {'color': '#ef4444', 'label': 'Limited Capacity (40-54)'},
                'F': {'color': '#991b1b', 'label': 'Constrained (<40)'}
            },
            'last_refresh': HEADROOM_CACHE.get('last_refresh')
        })

    @bp.route('/api/v1/capacity/trends', methods=['GET'])
    @require_plan('pro')
    def capacity_trends():
        """Historical headroom trends per market"""
        market = request.args.get('market')
        days = int(request.args.get('days', 30))

        try:
            conn = get_db()
            try:
                # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
                c = conn.cursor()

                if market:
                    c.execute("""
                        SELECT * FROM headroom_trend_daily
                        WHERE market = %s AND date >= date('now', %s)
                        ORDER BY date ASC
                    """, (market, f'-{days} days'))
                else:
                    c.execute("""
                        SELECT * FROM headroom_trend_daily
                        WHERE date >= date('now', %s)
                        ORDER BY market, date ASC
                    """, (f'-{days} days',))

                rows = [dict(r) for r in c.fetchall()]
            finally:
                conn.close()

            by_market = {}
            for row in rows:
                mk = row['market']
                if mk not in by_market:
                    by_market[mk] = []
                by_market[mk].append(row)

            return jsonify({
                'success': True,
                'period_days': days,
                'market_filter': market,
                'trends': by_market,
                'total_data_points': len(rows)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @bp.route('/api/v1/capacity/compare', methods=['GET'])
    @require_plan('pro')
    def capacity_compare():
        """Compare 2-5 markets side by side"""
        markets_param = request.args.get('markets', '')
        if not markets_param:
            return jsonify({
                'success': False,
                'error': 'Provide markets parameter (comma-separated)',
                'example': '/api/v1/capacity/compare%smarkets=dallas,phoenix,northern_virginia'
            }), 400

        market_keys = [m.strip().lower().replace('-', '_').replace(' ', '_') for m in markets_param.split(',')]
        market_keys = [m for m in market_keys if m in MARKET_ISO_MAP]

        if len(market_keys) < 2:
            return jsonify({'success': False, 'error': 'Need at least 2 valid markets to compare'}), 400
        if len(market_keys) > 5:
            market_keys = market_keys[:5]

        cached = HEADROOM_CACHE.get('all_markets', {})
        comparison = []
        for key in market_keys:
            if key in cached:
                data = cached[key]
            else:
                data = compute_market_headroom(key)
            comparison.append(data)

        comparison.sort(key=lambda x: x['readiness']['score'], reverse=True)

        rankings = {
            'best_grid': max(comparison, key=lambda x: x['grid']['spare_capacity_pct'])['name'],
            'best_gas': min(comparison, key=lambda x: x['gas']['utilization_pct'])['name'],
            'cheapest_power': min(comparison, key=lambda x: x['cost']['electricity_rate_cents_kwh'] or 99)['name'],
            'overall_best': comparison[0]['name'],
        }

        return jsonify({
            'success': True,
            'comparison': comparison,
            'rankings': rankings,
            'markets_compared': len(comparison)
        })

    @bp.route('/api/v1/capacity/status', methods=['GET'])
    def capacity_status():
        """Headroom engine status and refresh info"""
        return jsonify({
            'success': True,
            'engine': 'Capacity Headroom API v1.0',
            'status': 'active',
            'last_refresh': HEADROOM_CACHE.get('last_refresh'),
            'refresh_count': HEADROOM_CACHE.get('refresh_count', 0),
            'refresh_interval_minutes': HEADROOM_REFRESH_INTERVAL // 60,
            'markets_tracked': len(MARKET_ISO_MAP),
            'summary': HEADROOM_CACHE.get('summary', {}),
            'data_sources': [
                'EIA v2 API (RTO Demand, Retail Rates, Power Plants)',
                'FERC/DOT Gas Pipelines',
                'NTIA BEAD Fiber Grants',
                'GridStatus Library (when available)',
            ],
            'endpoints': {
                '/api/v1/capacity/headroom': 'All markets spare capacity + readiness (Pro)',
                '/api/v1/capacity/headroom/<market>': 'Single market detail (Pro)',
                '/api/v1/capacity/heatmap': 'Map-ready heatmap data with colors (Pro)',
                '/api/v1/capacity/trends': 'Historical headroom trends (Pro)',
                '/api/v1/capacity/compare': 'Side-by-side market comparison (Pro)',
                '/api/v1/capacity/status': 'Engine status (Public)',
            }
        })

    return bp
