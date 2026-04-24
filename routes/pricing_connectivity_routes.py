"""
DC Hub — Energy Pricing & Connectivity Routes
Serves data from the new discovery tables:
  - eia_electricity_rates
  - eia_natural_gas_prices
  - eia_gas_storage_weekly
  - peeringdb_ix_facilities
  - peeringdb_network_facilities
  - fcc_fiber_availability

Registration in main.py:
  from routes.pricing_connectivity_routes import register_pricing_connectivity_routes
  register_pricing_connectivity_routes(app, get_pg_connection)
"""

import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# PATCH 2026-04-23 (jm): Import require_plan for tier gating the gas-storage
# endpoint (which was leaking Pro-only data to free-tier callers, flagged by
# the UNGATED tier-gating canary at runtime). Defensive import: if
# api_tier_gating fails to load (partial deploy, dev env without the module),
# we fall back to a no-op so the rest of this blueprint still registers.
try:
    from api_tier_gating import require_plan
except Exception as _e:  # pragma: no cover
    logger.warning(
        "pricing_connectivity: require_plan unavailable (%s) — gas-storage will "
        "remain UNGATED until api_tier_gating loads cleanly.", _e
    )
    def require_plan(plan):  # type: ignore[no-redef]
        def _noop(fn):
            return fn
        return _noop

pricing_connectivity_bp = Blueprint('pricing_connectivity', __name__)

_get_conn = None


def register_pricing_connectivity_routes(app, get_pg_connection):
    global _get_conn
    _get_conn = get_pg_connection
    app.register_blueprint(pricing_connectivity_bp)
    logger.info("⚡ Pricing & Connectivity Routes: ✅ Registered")


def _db():
    return _get_conn()


# ═══════════════════════════════════════════════════════════════
# ELECTRICITY PRICING
# ═══════════════════════════════════════════════════════════════

@pricing_connectivity_bp.route('/api/v1/energy/electricity-rates', methods=['GET'])
def get_electricity_rates():
    """
    Get EIA electricity retail rates.
    Params:
      state  - 2-letter state code (required or returns all latest)
      sector - COM, IND, RES, ALL (default: IND)
      months - Number of months of history (default: 12)
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        state = request.args.get('state', '').upper()
        sector = request.args.get('sector', 'IND').upper()
        months = min(int(request.args.get('months', 12)), 60)

        if state:
            cur.execute("""
                SELECT state, sector, price_cents_kwh, period
                FROM eia_electricity_rates
                WHERE state = %s AND sector = %s
                ORDER BY period DESC
                LIMIT %s
            """, (state, sector, months))
        else:
            # Latest rate per state for the given sector
            cur.execute("""
                SELECT DISTINCT ON (state) state, sector, price_cents_kwh, period
                FROM eia_electricity_rates
                WHERE sector = %s
                ORDER BY state, period DESC
            """, (sector,))

        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                'state': r[0],
                'sector': r[1],
                'price_cents_kwh': float(r[2]) if r[2] else None,
                'price_dollars_mwh': round(float(r[2]) * 10, 2) if r[2] else None,
                'period': r[3]
            })

        return jsonify({
            'success': True,
            'count': len(results),
            'source': 'EIA',
            'data': results
        })
    except Exception as e:
        logger.error(f"Electricity rates error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@pricing_connectivity_bp.route('/api/v1/energy/gas-prices', methods=['GET'])
def get_gas_prices():
    """
    Get EIA natural gas prices.
    Params:
      state  - 2-letter state code
      sector - citygate, industrial, commercial, electric_power, residential
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        state = request.args.get('state', '').upper()
        sector = request.args.get('sector', 'citygate')

        if state:
            cur.execute("""
                SELECT state, sector, price_dollars_mcf, period
                FROM eia_natural_gas_prices
                WHERE state = %s AND sector = %s
                ORDER BY period DESC
                LIMIT 12
            """, (state, sector))
        else:
            cur.execute("""
                SELECT DISTINCT ON (state) state, sector, price_dollars_mcf, period
                FROM eia_natural_gas_prices
                WHERE sector = %s
                ORDER BY state, period DESC
            """, (sector,))

        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                'state': r[0],
                'sector': r[1],
                'price_dollars_mcf': float(r[2]) if r[2] else None,
                'period': r[3]
            })

        return jsonify({
            'success': True,
            'count': len(results),
            'source': 'EIA',
            'data': results
        })
    except Exception as e:
        logger.error(f"Gas prices error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@pricing_connectivity_bp.route('/api/v1/energy/gas-storage', methods=['GET'])
@require_plan('pro')  # PATCH 2026-04-23 (jm): gate Pro-only — was leaking to free tier
def get_gas_storage():
    """
    Get weekly natural gas storage data.
    Params:
      region - e.g. "Lower 48", "East", "Midwest"
      weeks  - Number of weeks (default: 12)
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        region = request.args.get('region', '')
        weeks = min(int(request.args.get('weeks', 12)), 52)

        if region:
            cur.execute("""
                SELECT region, working_gas_bcf, net_change_bcf, period
                FROM eia_gas_storage_weekly
                WHERE region = %s
                ORDER BY period DESC
                LIMIT %s
            """, (region, weeks))
        else:
            # Latest per region
            cur.execute("""
                SELECT DISTINCT ON (region) region, working_gas_bcf, net_change_bcf, period
                FROM eia_gas_storage_weekly
                ORDER BY region, period DESC
            """)

        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                'region': r[0],
                'working_gas_bcf': float(r[1]) if r[1] else None,
                'net_change_bcf': float(r[2]) if r[2] else None,
                'period': r[3]
            })

        return jsonify({
            'success': True,
            'count': len(results),
            'source': 'EIA',
            'data': results
        })
    except Exception as e:
        logger.error(f"Gas storage error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


# ═══════════════════════════════════════════════════════════════
# CONNECTIVITY — PeeringDB & FCC
# ═══════════════════════════════════════════════════════════════

@pricing_connectivity_bp.route('/api/v1/connectivity/ix', methods=['GET'])
def get_internet_exchanges():
    """
    Get Internet Exchange points from PeeringDB.
    Params:
      country - 2-letter country code (default: all)
      city    - City name filter
      lat, lng, radius - Spatial search (km)
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        country = request.args.get('country', '').upper()
        city = request.args.get('city', '')
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', 100, type=float)
        limit = min(int(request.args.get('limit', 50)), 500)

        if lat and lng:
            # Spatial search using Haversine approximation
            # ~111 km per degree lat, ~85 km per degree lng at 35°N
            dlat = radius / 111.0
            dlng = radius / 85.0
            cur.execute("""
                SELECT ix_id, name, city, country, latitude, longitude, 
                       participants, speed_gbps, website
                FROM peeringdb_ix_facilities
                WHERE latitude BETWEEN %s AND %s
                  AND longitude BETWEEN %s AND %s
                ORDER BY name
                LIMIT %s
            """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng, limit))
        elif country:
            cur.execute("""
                SELECT ix_id, name, city, country, latitude, longitude,
                       participants, speed_gbps, website
                FROM peeringdb_ix_facilities
                WHERE country = %s
                ORDER BY participants DESC NULLS LAST
                LIMIT %s
            """, (country, limit))
        elif city:
            cur.execute("""
                SELECT ix_id, name, city, country, latitude, longitude,
                       participants, speed_gbps, website
                FROM peeringdb_ix_facilities
                WHERE LOWER(city) LIKE %s
                ORDER BY participants DESC NULLS LAST
                LIMIT %s
            """, (f'%{city.lower()}%', limit))
        else:
            cur.execute("""
                SELECT ix_id, name, city, country, latitude, longitude,
                       participants, speed_gbps, website
                FROM peeringdb_ix_facilities
                ORDER BY participants DESC NULLS LAST
                LIMIT %s
            """, (limit,))

        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                'ix_id': r[0],
                'name': r[1],
                'city': r[2],
                'country': r[3],
                'latitude': float(r[4]) if r[4] else None,
                'longitude': float(r[5]) if r[5] else None,
                'participants': r[6],
                'speed_gbps': float(r[7]) if r[7] else None,
                'website': r[8]
            })

        return jsonify({
            'success': True,
            'count': len(results),
            'source': 'PeeringDB',
            'data': results
        })
    except Exception as e:
        logger.error(f"IX error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@pricing_connectivity_bp.route('/api/v1/connectivity/networks', methods=['GET'])
def get_network_facilities():
    """
    Get network presence at facilities from PeeringDB.
    Params:
      facility_name - Filter by facility name
      network_name  - Filter by network/carrier name
      city          - Filter by city
      country       - Filter by country
      lat, lng, radius - Spatial search (km)
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        facility_name = request.args.get('facility_name', '')
        network_name = request.args.get('network_name', '')
        city = request.args.get('city', '')
        country = request.args.get('country', '').upper()
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', 50, type=float)
        limit = min(int(request.args.get('limit', 100)), 1000)

        conditions = []
        params = []

        if lat and lng:
            dlat = radius / 111.0
            dlng = radius / 85.0
            conditions.append("latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s")
            params.extend([lat - dlat, lat + dlat, lng - dlng, lng + dlng])

        if facility_name:
            conditions.append("LOWER(facility_name) LIKE %s")
            params.append(f'%{facility_name.lower()}%')

        if network_name:
            conditions.append("LOWER(network_name) LIKE %s")
            params.append(f'%{network_name.lower()}%')

        if city:
            conditions.append("LOWER(city) LIKE %s")
            params.append(f'%{city.lower()}%')

        if country:
            conditions.append("country = %s")
            params.append(country)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        cur.execute(f"""
            SELECT facility_id, network_id, network_name, facility_name,
                   city, country, latitude, longitude
            FROM peeringdb_network_facilities
            WHERE {where}
            ORDER BY facility_name, network_name
            LIMIT %s
        """, params)

        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                'facility_id': r[0],
                'network_id': r[1],
                'network_name': r[2],
                'facility_name': r[3],
                'city': r[4],
                'country': r[5],
                'latitude': float(r[6]) if r[6] else None,
                'longitude': float(r[7]) if r[7] else None
            })

        return jsonify({
            'success': True,
            'count': len(results),
            'source': 'PeeringDB',
            'data': results
        })
    except Exception as e:
        logger.error(f"Network facilities error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@pricing_connectivity_bp.route('/api/v1/connectivity/fiber-coverage', methods=['GET'])
def get_fiber_coverage():
    """
    Get FCC fiber availability by county.
    Params:
      state - 2-letter state code
      county_fips - Specific county FIPS
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        state = request.args.get('state', '').upper()
        county_fips = request.args.get('county_fips', '')

        if county_fips:
            cur.execute("""
                SELECT state, county_fips, county_name, provider_count,
                       residential_coverage_pct, max_download_mbps, source
                FROM fcc_fiber_availability
                WHERE county_fips = %s
            """, (county_fips,))
        elif state:
            cur.execute("""
                SELECT state, county_fips, county_name, provider_count,
                       residential_coverage_pct, max_download_mbps, source
                FROM fcc_fiber_availability
                WHERE state = %s
                ORDER BY residential_coverage_pct DESC NULLS LAST
            """, (state,))
        else:
            cur.execute("""
                SELECT state, county_fips, county_name, provider_count,
                       residential_coverage_pct, max_download_mbps, source
                FROM fcc_fiber_availability
                ORDER BY state, residential_coverage_pct DESC NULLS LAST
            """)

        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                'state': r[0],
                'county_fips': r[1],
                'county_name': r[2],
                'provider_count': r[3],
                'fiber_coverage_pct': float(r[4]) if r[4] else None,
                'max_download_mbps': float(r[5]) if r[5] else None,
                'source': r[6]
            })

        return jsonify({
            'success': True,
            'count': len(results),
            'source': 'FCC_BDC',
            'data': results
        })
    except Exception as e:
        logger.error(f"Fiber coverage error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


# ═══════════════════════════════════════════════════════════════
# COMBINED SITE ANALYSIS ENRICHMENT
# ═══════════════════════════════════════════════════════════════

@pricing_connectivity_bp.route('/api/v1/energy/site-pricing', methods=['GET'])
def get_site_pricing():
    """
    Get all energy pricing for a site analysis.
    Params:
      state - 2-letter state code (required)
    Returns electricity + gas prices + gas storage for that state's region.
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        state = request.args.get('state', '').upper()
        if not state:
            return jsonify({'success': False, 'error': 'state parameter required'}), 400

        # Electricity rates — all sectors, latest period
        cur.execute("""
            SELECT DISTINCT ON (sector) sector, price_cents_kwh, period
            FROM eia_electricity_rates
            WHERE state = %s
            ORDER BY sector, period DESC
        """, (state,))
        elec = {}
        for r in cur.fetchall():
            elec[r[0].lower()] = {
                'price_cents_kwh': float(r[1]) if r[1] else None,
                'price_dollars_mwh': round(float(r[1]) * 10, 2) if r[1] else None,
                'period': r[2]
            }

        # Gas prices — all sectors, latest
        cur.execute("""
            SELECT DISTINCT ON (sector) sector, price_dollars_mcf, period
            FROM eia_natural_gas_prices
            WHERE state = %s
            ORDER BY sector, period DESC
        """, (state,))
        gas = {}
        for r in cur.fetchall():
            gas[r[0]] = {
                'price_dollars_mcf': float(r[1]) if r[1] else None,
                'period': r[2]
            }

        # Gas storage — latest for Lower 48
        cur.execute("""
            SELECT region, working_gas_bcf, net_change_bcf, period
            FROM eia_gas_storage_weekly
            WHERE region = 'Lower 48'
            ORDER BY period DESC
            LIMIT 1
        """)
        storage_row = cur.fetchone()
        storage = None
        if storage_row:
            storage = {
                'region': storage_row[0],
                'working_gas_bcf': float(storage_row[1]) if storage_row[1] else None,
                'net_change_bcf': float(storage_row[2]) if storage_row[2] else None,
                'period': storage_row[3]
            }

        return jsonify({
            'success': True,
            'state': state,
            'electricity': elec,
            'natural_gas': gas,
            'gas_storage': storage,
            'source': 'EIA'
        })
    except Exception as e:
        logger.error(f"Site pricing error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@pricing_connectivity_bp.route('/api/v1/connectivity/site-connectivity', methods=['GET'])
def get_site_connectivity():
    """
    Get connectivity summary for a location.
    Params:
      lat, lng - Location (required)
      radius   - Search radius in km (default: 50)
      state    - For fiber coverage lookup
    """
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()

        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        state = request.args.get('state', '').upper()
        radius = request.args.get('radius', 50, type=float)

        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat and lng required'}), 400

        dlat = radius / 111.0
        dlng = radius / 85.0

        # Nearby IXes
        cur.execute("""
            SELECT ix_id, name, city, country, latitude, longitude, participants
            FROM peeringdb_ix_facilities
            WHERE latitude BETWEEN %s AND %s
              AND longitude BETWEEN %s AND %s
            ORDER BY participants DESC NULLS LAST
            LIMIT 10
        """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
        ixes = [{'ix_id': r[0], 'name': r[1], 'city': r[2], 'country': r[3],
                 'latitude': float(r[4]) if r[4] else None,
                 'longitude': float(r[5]) if r[5] else None,
                 'participants': r[6]} for r in cur.fetchall()]

        # Nearby network facilities — count unique networks
        cur.execute("""
            SELECT COUNT(DISTINCT network_id), COUNT(DISTINCT facility_id)
            FROM peeringdb_network_facilities
            WHERE latitude BETWEEN %s AND %s
              AND longitude BETWEEN %s AND %s
        """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
        net_row = cur.fetchone()
        network_count = net_row[0] if net_row else 0
        facility_count = net_row[1] if net_row else 0

        # Top networks at nearby facilities
        cur.execute("""
            SELECT DISTINCT network_name, facility_name
            FROM peeringdb_network_facilities
            WHERE latitude BETWEEN %s AND %s
              AND longitude BETWEEN %s AND %s
            ORDER BY network_name
            LIMIT 20
        """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
        top_networks = [{'network': r[0], 'facility': r[1]} for r in cur.fetchall()]

        # Fiber coverage for state
        fiber = None
        if state:
            cur.execute("""
                SELECT county_name, provider_count, residential_coverage_pct
                FROM fcc_fiber_availability
                WHERE state = %s
                ORDER BY residential_coverage_pct DESC NULLS LAST
                LIMIT 5
            """, (state,))
            fiber = [{'county': r[0], 'providers': r[1],
                      'coverage_pct': float(r[2]) if r[2] else None}
                     for r in cur.fetchall()]

        return jsonify({
            'success': True,
            'location': {'lat': lat, 'lng': lng, 'radius_km': radius},
            'internet_exchanges': ixes,
            'networks': {
                'unique_networks': network_count,
                'unique_facilities': facility_count,
                'top_networks': top_networks
            },
            'fiber_coverage': fiber,
            'source': 'PeeringDB + FCC'
        })
    except Exception as e:
        logger.error(f"Site connectivity error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass
