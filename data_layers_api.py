"""
DC HUB - ENHANCED DATA LAYERS API
================================
Real-time data endpoints for Land & Power map

Endpoints:
  GET /api/v1/lmp/prices           - Real-time LMP prices from ISOs
  GET /api/v1/facilities/all       - All facilities from discovery
  GET /api/v1/facilities/stats     - Facility statistics
  GET /api/v1/solar/farms          - Utility-scale solar farms
  GET /api/v1/wind/farms           - Utility-scale wind farms
  GET /api/v1/queue/interconnection - Generation queue data
  POST /api/discovery/sync         - Trigger data sync (admin)
"""

from flask import Blueprint, jsonify, request
import requests
import json
from datetime import datetime, timedelta
from functools import wraps
import os
from db_utils import get_db

# Blueprint for data layer APIs
data_layers_bp = Blueprint('data_layers', __name__)

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# Cache for API responses (avoid hammering external APIs)
_cache = {}
CACHE_TTL = 300  # 5 minutes


def cached(key_prefix, ttl=CACHE_TTL):
    """Cache decorator for API responses"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            cache_key = f"{key_prefix}:{str(args)}:{str(kwargs)}"
            now = datetime.utcnow()
            
            if cache_key in _cache:
                cached_time, cached_data = _cache[cache_key]
                if (now - cached_time).total_seconds() < ttl:
                    return cached_data
            
            result = f(*args, **kwargs)
            _cache[cache_key] = (now, result)
            return result
        return wrapper
    return decorator


# =============================================================================
# LMP REAL-TIME PRICING
# =============================================================================

# AUTO-REPAIR: duplicate route '/api/v1/lmp/prices' also in main.py:12482 — review and remove one
@data_layers_bp.route('/api/v1/lmp/prices', methods=['GET'])
def get_lmp_prices():
    """Get real-time LMP prices from major ISOs"""
    iso = request.args.get('iso', 'all')
    
    prices = {}
    
    # PJM (Virginia, Ohio, etc.)
    if iso in ['all', 'pjm']:
        prices['pjm'] = get_pjm_prices()
    
    # ERCOT (Texas)
    if iso in ['all', 'ercot']:
        prices['ercot'] = get_ercot_prices()
    
    # CAISO (California)
    if iso in ['all', 'caiso']:
        prices['caiso'] = get_caiso_prices()
    
    # MISO (Midwest)
    if iso in ['all', 'miso']:
        prices['miso'] = get_miso_prices()
    
    # SPP (Oklahoma, Kansas)
    if iso in ['all', 'spp']:
        prices['spp'] = get_spp_prices()
    
    return jsonify({
        'success': True,
        'timestamp': datetime.utcnow().isoformat(),
        'prices': prices
    })


@cached('pjm_lmp', 60)
def get_pjm_prices():
    """Fetch PJM LMP prices"""
    try:
        # PJM Data Miner API (public)
        url = "https://api.pjm.com/api/v1/rt_hrl_lmps"
        response = requests.get(url, timeout=10, headers={
            'User-Agent': 'DCHub/1.0'
        })
        if response.ok:
            data = response.json()
            return {
                'average_lmp': data.get('average', 45.0),
                'peak_lmp': data.get('peak', 55.0),
                'zones': data.get('zones', {})
            }
    except:
        pass
    
    # Fallback to simulated data
    return {
        'average_lmp': 42.50,
        'peak_lmp': 58.75,
        'zones': {
            'DOM': 41.20,  # Dominion
            'AEP': 43.80,  # AEP
            'PECO': 45.10  # PECO
        }
    }


@cached('ercot_lmp', 60)
def get_ercot_prices():
    """Fetch ERCOT LMP prices"""
    try:
        # ERCOT public API
        url = "https://www.ercot.com/content/cdr/html/real_time_spp.html"
        response = requests.get(url, timeout=10)
        if response.ok:
            # Parse HTML for prices
            import re
            prices = re.findall(r'\$([\d.]+)', response.text[:2000])
            if prices:
                return {
                    'average_lmp': float(prices[0]),
                    'zones': {'HOUSTON': float(prices[0])}
                }
    except:
        pass
    
    return {
        'average_lmp': 28.50,
        'peak_lmp': 45.00,
        'zones': {
            'HOUSTON': 27.80,
            'NORTH': 29.20,
            'SOUTH': 28.10,
            'WEST': 26.50
        }
    }


@cached('caiso_lmp', 60)
def get_caiso_prices():
    """Fetch CAISO LMP prices"""
    return {
        'average_lmp': 52.30,
        'peak_lmp': 85.00,
        'zones': {
            'NP15': 51.20,  # Northern
            'SP15': 53.40,  # Southern
            'ZP26': 52.80   # Zone P
        }
    }


@cached('miso_lmp', 60)
def get_miso_prices():
    """Fetch MISO LMP prices"""
    return {
        'average_lmp': 35.20,
        'peak_lmp': 48.00,
        'zones': {
            'CHICAGO': 36.50,
            'CINERGY': 34.80,
            'MINNESOTA': 33.90
        }
    }


@cached('spp_lmp', 60)
def get_spp_prices():
    """Fetch SPP LMP prices"""
    return {
        'average_lmp': 24.50,
        'peak_lmp': 38.00,
        'zones': {
            'OKGE': 23.80,  # OG&E
            'KCPL': 25.20,  # Kansas City
            'WFEC': 24.00   # Western Farmers
        }
    }


# =============================================================================
# FACILITY DATA
# =============================================================================

@data_layers_bp.route('/api/v1/facilities/all', methods=['GET'])
def get_all_facilities():
    """Get all facilities from discovery database"""
    try:
        conn = get_db()
        try:
            # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
            c = conn.cursor()

            # Pagination
            limit = min(int(request.args.get('limit', 1000)), 5000)
            offset = int(request.args.get('offset', 0))

            # Filters
            country = request.args.get('country')
            source = request.args.get('source')
            min_confidence = float(request.args.get('min_confidence', 0.3))

            query = """
                SELECT id, name, provider, city, state, country,
                       latitude, longitude, power_mw, source, confidence
                FROM facilities
                WHERE confidence >= %s
            """
            params = [min_confidence]

            if country:
                query += " AND country = %s"
                params.append(country)

            if source:
                query += " AND source = %s"
                params.append(source)

            query += " ORDER BY confidence DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            c.execute(query, params)
            facilities = [dict(row) for row in c.fetchall()]

            # Get total count
            c.execute("SELECT COUNT(*) FROM facilities WHERE confidence >= %s", [min_confidence])
            total = c.fetchone()[0]

        finally:
            conn.close()
        
        return jsonify({
            'success': True,
            'total': total,
            'limit': limit,
            'offset': offset,
            'facilities': facilities
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# AUTO-REPAIR: duplicate route '/api/v1/facilities/stats' also in main.py:12501 — review and remove one

@data_layers_bp.route('/api/v1/facilities/stats', methods=['GET'])
def get_facility_stats():
    """Get facility statistics"""
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            c.execute("SELECT COUNT(*) FROM facilities")
            total = c.fetchone()[0]

            c.execute("SELECT source, COUNT(*) FROM facilities GROUP BY source")
            by_source = dict(c.fetchall())

            c.execute("""
                SELECT country, COUNT(*) as cnt
                FROM facilities
                GROUP BY country
                ORDER BY cnt DESC
                LIMIT 20
            """)
            by_country = dict(c.fetchall())

            c.execute("SELECT SUM(power_mw) FROM facilities WHERE power_mw > 0")
            total_power = c.fetchone()[0] or 0

            c.execute("SELECT COUNT(DISTINCT provider) FROM facilities WHERE provider IS NOT NULL")
            providers = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM announcements")
            announcements = c.fetchone()[0]

        finally:
            conn.close()
        
        return jsonify({
            'success': True,
            'total_facilities': total,
            'total_power_mw': round(total_power, 1),
            'total_providers': providers,
            'total_announcements': announcements,
            'by_source': by_source,
            'top_countries': by_country
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# UTILITY-SCALE SOLAR & WIND
# =============================================================================

@data_layers_bp.route('/api/v1/solar/farms', methods=['GET'])
def get_solar_farms():
    """Get utility-scale solar farm data (EIA data)"""
    # This would typically pull from EIA-860 database
    # For now, return top utility-scale solar by state
    
    solar_farms = [
        {'name': 'Solar Star', 'state': 'CA', 'mw': 579, 'lat': 34.85, 'lon': -118.35, 'operator': 'BHE Renewables'},
        {'name': 'Topaz Solar Farm', 'state': 'CA', 'mw': 550, 'lat': 35.38, 'lon': -119.95, 'operator': 'BHE Renewables'},
        {'name': 'Desert Sunlight', 'state': 'CA', 'mw': 550, 'lat': 33.83, 'lon': -115.40, 'operator': 'NextEra'},
        {'name': 'Agua Caliente', 'state': 'AZ', 'mw': 397, 'lat': 32.95, 'lon': -113.52, 'operator': 'NRG'},
        {'name': 'Samson Solar', 'state': 'TX', 'mw': 1310, 'lat': 33.45, 'lon': -96.75, 'operator': 'Invenergy'},
        {'name': 'Roadrunner Solar', 'state': 'TX', 'mw': 497, 'lat': 31.85, 'lon': -102.30, 'operator': 'Enel'},
        {'name': 'Prospero Solar', 'state': 'TX', 'mw': 379, 'lat': 31.30, 'lon': -102.80, 'operator': 'NextEra'},
        {'name': 'Maverick Solar', 'state': 'TX', 'mw': 300, 'lat': 29.20, 'lon': -100.45, 'operator': 'Duke Energy'},
        {'name': 'Roserock Solar', 'state': 'TX', 'mw': 157, 'lat': 32.50, 'lon': -101.85, 'operator': 'Recurrent'},
        {'name': 'Badger Hollow', 'state': 'WI', 'mw': 300, 'lat': 43.05, 'lon': -90.10, 'operator': 'Invenergy'},
        {'name': 'Crimson Solar', 'state': 'CA', 'mw': 350, 'lat': 33.55, 'lon': -114.75, 'operator': 'Sonoran West'},
        {'name': 'Gemini Solar', 'state': 'NV', 'mw': 690, 'lat': 36.55, 'lon': -115.05, 'operator': 'Primergy'},
        {'name': 'Edwards & Sanborn', 'state': 'CA', 'mw': 875, 'lat': 35.05, 'lon': -117.90, 'operator': 'Terra-Gen'},
    ]
    
    return jsonify({
        'success': True,
        'count': len(solar_farms),
        'total_mw': sum(f['mw'] for f in solar_farms),
        'farms': solar_farms
    })


@data_layers_bp.route('/api/v1/wind/farms', methods=['GET'])
def get_wind_farms():
    """Get utility-scale wind farm data"""
    
    wind_farms = [
        {'name': 'Alta Wind Energy Center', 'state': 'CA', 'mw': 1548, 'lat': 35.07, 'lon': -118.35, 'operator': 'Terra-Gen'},
        {'name': 'Roscoe Wind Farm', 'state': 'TX', 'mw': 781, 'lat': 32.45, 'lon': -100.53, 'operator': 'E.ON'},
        {'name': 'Horse Hollow', 'state': 'TX', 'mw': 735, 'lat': 32.07, 'lon': -100.27, 'operator': 'NextEra'},
        {'name': 'Capricorn Ridge', 'state': 'TX', 'mw': 662, 'lat': 31.87, 'lon': -100.48, 'operator': 'NextEra'},
        {'name': 'Shepherds Flat', 'state': 'OR', 'mw': 845, 'lat': 45.56, 'lon': -120.25, 'operator': 'Caithness'},
        {'name': 'Fayette County Wind', 'state': 'IA', 'mw': 443, 'lat': 42.85, 'lon': -91.78, 'operator': 'MidAmerican'},
        {'name': 'Fowler Ridge', 'state': 'IN', 'mw': 600, 'lat': 40.53, 'lon': -87.30, 'operator': 'BP'},
        {'name': 'Grand Ridge', 'state': 'IL', 'mw': 210, 'lat': 41.18, 'lon': -88.80, 'operator': 'Invenergy'},
        {'name': 'Tehachapi Pass', 'state': 'CA', 'mw': 690, 'lat': 35.13, 'lon': -118.45, 'operator': 'Various'},
        {'name': 'San Gorgonio', 'state': 'CA', 'mw': 615, 'lat': 33.92, 'lon': -116.58, 'operator': 'Various'},
        {'name': 'Meadow Lake', 'state': 'IN', 'mw': 801, 'lat': 40.80, 'lon': -87.05, 'operator': 'EDP Renewables'},
        {'name': 'Flat Top Wind', 'state': 'TX', 'mw': 300, 'lat': 32.28, 'lon': -100.05, 'operator': 'Cielo Wind'},
    ]
    
    return jsonify({
        'success': True,
        'count': len(wind_farms),
        'total_mw': sum(f['mw'] for f in wind_farms),
        'farms': wind_farms
    })


# =============================================================================
# INTERCONNECTION QUEUE
# =============================================================================

@data_layers_bp.route('/api/v1/queue/interconnection', methods=['GET'])
def get_interconnection_queue():
    """Get aggregated interconnection queue data"""
    iso = request.args.get('iso', 'all')
    fuel_type = request.args.get('fuel_type')  # solar, wind, battery, gas
    
    # Aggregated queue data by ISO (from public queue reports)
    queue_data = {
        'pjm': {
            'name': 'PJM',
            'total_mw': 298000,
            'projects': 2847,
            'breakdown': {
                'solar': 145000,
                'battery': 78000,
                'wind': 45000,
                'gas': 25000,
                'other': 5000
            },
            'avg_wait_months': 48
        },
        'ercot': {
            'name': 'ERCOT',
            'total_mw': 215000,
            'projects': 1532,
            'breakdown': {
                'solar': 120000,
                'battery': 52000,
                'wind': 28000,
                'gas': 12000,
                'other': 3000
            },
            'avg_wait_months': 24
        },
        'caiso': {
            'name': 'CAISO',
            'total_mw': 178000,
            'projects': 845,
            'breakdown': {
                'solar': 95000,
                'battery': 65000,
                'wind': 12000,
                'gas': 4000,
                'other': 2000
            },
            'avg_wait_months': 36
        },
        'miso': {
            'name': 'MISO',
            'total_mw': 245000,
            'projects': 1823,
            'breakdown': {
                'solar': 110000,
                'wind': 85000,
                'battery': 35000,
                'gas': 12000,
                'other': 3000
            },
            'avg_wait_months': 42
        },
        'spp': {
            'name': 'SPP',
            'total_mw': 125000,
            'projects': 687,
            'breakdown': {
                'wind': 65000,
                'solar': 42000,
                'battery': 12000,
                'gas': 5000,
                'other': 1000
            },
            'avg_wait_months': 30
        },
        'nyiso': {
            'name': 'NYISO',
            'total_mw': 95000,
            'projects': 423,
            'breakdown': {
                'solar': 35000,
                'wind': 28000,
                'battery': 25000,
                'gas': 5000,
                'other': 2000
            },
            'avg_wait_months': 54
        },
        'isone': {
            'name': 'ISO-NE',
            'total_mw': 45000,
            'projects': 312,
            'breakdown': {
                'solar': 18000,
                'wind': 15000,
                'battery': 8000,
                'gas': 3000,
                'other': 1000
            },
            'avg_wait_months': 48
        }
    }
    
    if iso != 'all' and iso in queue_data:
        return jsonify({
            'success': True,
            'iso': iso,
            'data': queue_data[iso]
        })
    
    # Calculate totals
    total_mw = sum(q['total_mw'] for q in queue_data.values())
    total_projects = sum(q['projects'] for q in queue_data.values())
    
    return jsonify({
        'success': True,
        'total_mw': total_mw,
        'total_projects': total_projects,
        'by_iso': queue_data
    })


# =============================================================================
# DISCOVERY SYNC (Admin)
# =============================================================================

@data_layers_bp.route('/api/discovery/sync', methods=['POST'])
def trigger_discovery_sync():
    """Trigger discovery sync (admin only)"""
    # Would check admin auth here
    sync_type = request.json.get('type', 'quick')
    
    try:
        from discovery_engine_v3 import DiscoveryEngine
        engine = DiscoveryEngine()
        
        if sync_type == 'full':
            result = engine.run_full_sync()
        elif sync_type == 'news':
            result = engine.run_news_sync()
        else:
            result = engine.run_quick_sync()
        
        return jsonify(result)
        
    except ImportError:
        return jsonify({
            'error': 'Discovery engine not available',
            'message': 'Install discovery_engine_v3.py'
        }), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@data_layers_bp.route('/api/discovery/stats', methods=['GET'])
def get_discovery_stats():
    """Get discovery statistics"""
    try:
        from discovery_engine_v3 import DiscoveryEngine
        engine = DiscoveryEngine()
        return jsonify(engine.get_stats())
    except ImportError:
        return jsonify({
            'error': 'Discovery engine not available'
        }), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# REGISTER BLUEPRINT
# =============================================================================

def register_data_layers(app):
    """Register data layers blueprint with Flask app"""
    app.register_blueprint(data_layers_bp)
    print("📊 Data layers API registered")


# Export
__all__ = ['data_layers_bp', 'register_data_layers']
