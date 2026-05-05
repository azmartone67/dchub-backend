"""
Real Estate Intelligence Module
- County Assessor Data (land values, ownership)
- Industrial Zoning Maps
- Building Permits
- Land Availability
- Market Trends
"""

import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
import json
import hashlib
import os
from db_utils import get_db

logger = logging.getLogger(__name__)

real_estate_bp = Blueprint('real_estate', __name__)

DB_PATH = os.environ.get('DC_NEXUS_DB', 'dc_nexus.db')
CACHE_DURATION = 3600


def init_real_estate_db():
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS land_parcels (
            id SERIAL PRIMARY KEY,
            parcel_id TEXT UNIQUE,
            county TEXT,
            state TEXT,
            address TEXT,
            acreage REAL,
            zoning TEXT,
            assessed_value REAL,
            land_value REAL,
            owner_name TEXT,
            owner_type TEXT,
            last_sale_date TEXT,
            last_sale_price REAL,
            latitude REAL,
            longitude REAL,
            dc_suitable INTEGER DEFAULT 0,
            power_available INTEGER DEFAULT 0,
            fiber_available INTEGER DEFAULT 0,
            water_available INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS building_permits (
            id SERIAL PRIMARY KEY,
            permit_id TEXT UNIQUE,
            county TEXT,
            state TEXT,
            address TEXT,
            permit_type TEXT,
            description TEXT,
            estimated_cost REAL,
            issue_date TEXT,
            status TEXT,
            applicant TEXT,
            is_datacenter INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS zoning_districts (
            id SERIAL PRIMARY KEY,
            district_id TEXT UNIQUE,
            county TEXT,
            state TEXT,
            zone_code TEXT,
            zone_name TEXT,
            allows_datacenter INTEGER DEFAULT 0,
            allows_industrial INTEGER DEFAULT 0,
            max_height_ft REAL,
            max_lot_coverage REAL,
            min_setback_ft REAL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
    finally:
        conn.close()
    logger.info("✅ Real Estate Intelligence tables initialized")

try:
    init_real_estate_db()
except Exception as e:
    import logging
    logging.getLogger(__name__).warning(
        "init_real_estate_db failed at import (%s) — module loads anyway, "
        "will lazy-retry on first request", e
    )


class CountyAssessorAPI:
    """Scrape/aggregate county assessor data"""
    
    DC_MARKETS = {
        'Northern Virginia': [
            {'county': 'Loudoun', 'state': 'VA', 'fips': '51107'},
            {'county': 'Prince William', 'state': 'VA', 'fips': '51153'},
            {'county': 'Fairfax', 'state': 'VA', 'fips': '51059'},
        ],
        'Dallas-Fort Worth': [
            {'county': 'Dallas', 'state': 'TX', 'fips': '48113'},
            {'county': 'Collin', 'state': 'TX', 'fips': '48085'},
            {'county': 'Denton', 'state': 'TX', 'fips': '48121'},
        ],
        'Phoenix': [
            {'county': 'Maricopa', 'state': 'AZ', 'fips': '04013'},
            {'county': 'Pinal', 'state': 'AZ', 'fips': '04021'},
        ],
        'Atlanta': [
            {'county': 'Fulton', 'state': 'GA', 'fips': '13121'},
            {'county': 'Douglas', 'state': 'GA', 'fips': '13097'},
            {'county': 'Henry', 'state': 'GA', 'fips': '13151'},
        ],
        'Chicago': [
            {'county': 'Cook', 'state': 'IL', 'fips': '17031'},
            {'county': 'DuPage', 'state': 'IL', 'fips': '17043'},
            {'county': 'Will', 'state': 'IL', 'fips': '17197'},
        ],
        'Denver': [
            {'county': 'Adams', 'state': 'CO', 'fips': '08001'},
            {'county': 'Arapahoe', 'state': 'CO', 'fips': '08005'},
            {'county': 'Douglas', 'state': 'CO', 'fips': '08035'},
        ],
        'Las Vegas': [
            {'county': 'Clark', 'state': 'NV', 'fips': '32003'},
        ],
        'Salt Lake City': [
            {'county': 'Salt Lake', 'state': 'UT', 'fips': '49035'},
            {'county': 'Utah', 'state': 'UT', 'fips': '49049'},
        ],
        'Columbus': [
            {'county': 'Franklin', 'state': 'OH', 'fips': '39049'},
            {'county': 'Licking', 'state': 'OH', 'fips': '39089'},
        ],
        'Austin': [
            {'county': 'Travis', 'state': 'TX', 'fips': '48453'},
            {'county': 'Williamson', 'state': 'TX', 'fips': '48491'},
        ],
    }
    
    SAMPLE_PARCELS = [
        {'parcel_id': 'LOUD-2024-0001', 'county': 'Loudoun', 'state': 'VA', 'address': '21571 Beaumeade Circle, Ashburn', 'acreage': 45.2, 'zoning': 'PD-IP', 'assessed_value': 12500000, 'land_value': 8500000, 'owner_name': 'QTS Realty Trust', 'dc_suitable': 1, 'latitude': 39.0438, 'longitude': -77.4874},
        {'parcel_id': 'LOUD-2024-0002', 'county': 'Loudoun', 'state': 'VA', 'address': '44060 Digital Loudoun Plaza', 'acreage': 32.8, 'zoning': 'PD-IP', 'assessed_value': 9800000, 'land_value': 6200000, 'owner_name': 'Digital Realty', 'dc_suitable': 1, 'latitude': 39.0512, 'longitude': -77.4621},
        {'parcel_id': 'MARI-2024-0001', 'county': 'Maricopa', 'state': 'AZ', 'address': '2402 W Campus Drive, Mesa', 'acreage': 120.5, 'zoning': 'I-2', 'assessed_value': 18500000, 'land_value': 14200000, 'owner_name': 'Available', 'dc_suitable': 1, 'latitude': 33.4255, 'longitude': -111.8688},
        {'parcel_id': 'MARI-2024-0002', 'county': 'Maricopa', 'state': 'AZ', 'address': '7878 S Power Road, Queen Creek', 'acreage': 85.3, 'zoning': 'I-1', 'assessed_value': 11200000, 'land_value': 8900000, 'owner_name': 'Microsoft Corp', 'dc_suitable': 1, 'latitude': 33.2847, 'longitude': -111.6832},
        {'parcel_id': 'DALL-2024-0001', 'county': 'Dallas', 'state': 'TX', 'address': '1950 N Stemmons Freeway', 'acreage': 28.4, 'zoning': 'IR', 'assessed_value': 22000000, 'land_value': 15000000, 'owner_name': 'Equinix Inc', 'dc_suitable': 1, 'latitude': 32.8021, 'longitude': -96.8622},
        {'parcel_id': 'CLAR-2024-0001', 'county': 'Clark', 'state': 'NV', 'address': '6875 Bermuda Road, Las Vegas', 'acreage': 55.2, 'zoning': 'M-D', 'assessed_value': 8500000, 'land_value': 6800000, 'owner_name': 'Switch Inc', 'dc_suitable': 1, 'latitude': 36.0544, 'longitude': -115.0823},
        {'parcel_id': 'FRAN-2024-0001', 'county': 'Franklin', 'state': 'OH', 'address': '5000 Arlington Centre Blvd', 'acreage': 42.1, 'zoning': 'L-M', 'assessed_value': 7200000, 'land_value': 5100000, 'owner_name': 'Cologix Inc', 'dc_suitable': 1, 'latitude': 40.0142, 'longitude': -82.9156},
        {'parcel_id': 'SALT-2024-0001', 'county': 'Salt Lake', 'state': 'UT', 'address': '572 Delong Street, Salt Lake City', 'acreage': 18.5, 'zoning': 'M-1', 'assessed_value': 4500000, 'land_value': 3200000, 'owner_name': 'C7 Data Centers', 'dc_suitable': 1, 'latitude': 40.7608, 'longitude': -111.8910},
    ]
    
    LAND_VALUES_PER_ACRE = {
        'Loudoun': {'min': 150000, 'max': 450000, 'trend': 'rising'},
        'Prince William': {'min': 80000, 'max': 200000, 'trend': 'stable'},
        'Maricopa': {'min': 50000, 'max': 180000, 'trend': 'rising'},
        'Clark': {'min': 40000, 'max': 150000, 'trend': 'rising'},
        'Dallas': {'min': 100000, 'max': 350000, 'trend': 'stable'},
        'Franklin': {'min': 60000, 'max': 140000, 'trend': 'stable'},
        'Salt Lake': {'min': 70000, 'max': 200000, 'trend': 'rising'},
        'Douglas': {'min': 90000, 'max': 250000, 'trend': 'stable'},
    }
    
    @classmethod
    def get_market_counties(cls, market=None):
        """Get counties for a market"""
        if market:
            return cls.DC_MARKETS.get(market, [])
        return cls.DC_MARKETS
    
    @classmethod
    def get_land_values(cls, county):
        """Get land value trends for a county"""
        values = cls.LAND_VALUES_PER_ACRE.get(county)
        if not values:
            return {'error': f'County not found: {county}'}
        return {
            'county': county,
            'per_acre': values,
            'dc_premium': '15-25% above industrial',
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def search_parcels(cls, county=None, min_acres=10, max_price=None, dc_suitable=True):
        """Search available parcels"""
        parcels = cls.SAMPLE_PARCELS
        if county:
            parcels = [p for p in parcels if p['county'].lower() == county.lower()]
        if min_acres:
            parcels = [p for p in parcels if p['acreage'] >= min_acres]
        if max_price:
            parcels = [p for p in parcels if p['assessed_value'] <= max_price]
        if dc_suitable:
            parcels = [p for p in parcels if p.get('dc_suitable', 0) == 1]
        
        return {
            'parcels': parcels,
            'count': len(parcels),
            'filters': {'county': county, 'min_acres': min_acres, 'dc_suitable': dc_suitable}
        }
    
    @classmethod
    def get_parcel_details(cls, parcel_id):
        """Get detailed parcel info"""
        for p in cls.SAMPLE_PARCELS:
            if p['parcel_id'] == parcel_id:
                return {
                    **p,
                    'utilities': {
                        'power': {'available': True, 'provider': 'Dominion Energy', 'capacity_mw': 50},
                        'fiber': {'available': True, 'providers': ['Zayo', 'Lumen', 'Crown Castle']},
                        'water': {'available': True, 'provider': 'County Water Authority'}
                    },
                    'nearby_dcs': 5,
                    'dc_score': 92
                }
        return {'error': 'Parcel not found'}


class ZoningAPI:
    """Industrial zoning data"""
    
    DC_ZONES = {
        'VA': {
            'PD-IP': {'name': 'Planned Development - Industrial Park', 'allows_dc': True, 'max_height': 75, 'lot_coverage': 0.6},
            'PD-TRC': {'name': 'Planned Development - Technology Research Center', 'allows_dc': True, 'max_height': 100, 'lot_coverage': 0.5},
            'M-1': {'name': 'Light Industrial', 'allows_dc': True, 'max_height': 60, 'lot_coverage': 0.7},
        },
        'AZ': {
            'I-1': {'name': 'Light Industrial', 'allows_dc': True, 'max_height': 50, 'lot_coverage': 0.65},
            'I-2': {'name': 'General Industrial', 'allows_dc': True, 'max_height': 75, 'lot_coverage': 0.7},
            'PAD': {'name': 'Planned Area Development', 'allows_dc': True, 'max_height': 100, 'lot_coverage': 0.5},
        },
        'TX': {
            'IR': {'name': 'Industrial Research', 'allows_dc': True, 'max_height': 80, 'lot_coverage': 0.6},
            'LI': {'name': 'Light Industrial', 'allows_dc': True, 'max_height': 60, 'lot_coverage': 0.65},
            'HI': {'name': 'Heavy Industrial', 'allows_dc': True, 'max_height': 100, 'lot_coverage': 0.75},
        },
        'NV': {
            'M-D': {'name': 'Designed Manufacturing', 'allows_dc': True, 'max_height': 75, 'lot_coverage': 0.6},
            'M-1': {'name': 'Light Manufacturing', 'allows_dc': True, 'max_height': 50, 'lot_coverage': 0.65},
        },
        'OH': {
            'L-M': {'name': 'Limited Manufacturing', 'allows_dc': True, 'max_height': 60, 'lot_coverage': 0.6},
            'M': {'name': 'Manufacturing', 'allows_dc': True, 'max_height': 75, 'lot_coverage': 0.7},
        },
    }
    
    @classmethod
    def get_dc_zones(cls, state=None):
        """Get DC-suitable zones"""
        if state:
            zones = cls.DC_ZONES.get(state.upper(), {})
            return {'state': state, 'zones': zones, 'count': len(zones)}
        return {'all_states': cls.DC_ZONES, 'total_zones': sum(len(z) for z in cls.DC_ZONES.values())}
    
    @classmethod
    def check_zone_compatibility(cls, state, zone_code):
        """Check if zone allows data centers"""
        zones = cls.DC_ZONES.get(state.upper(), {})
        zone = zones.get(zone_code)
        if zone:
            return {
                'zone_code': zone_code,
                'state': state,
                **zone,
                'dc_compatible': zone.get('allows_dc', False)
            }
        return {'error': f'Zone {zone_code} not found in {state}'}


class BuildingPermitTracker:
    """Track DC building permits"""
    
    SAMPLE_PERMITS = [
        {'permit_id': 'LOUD-BP-2024-0125', 'county': 'Loudoun', 'state': 'VA', 'address': '21000 Ashburn Crossing', 'permit_type': 'Commercial Construction', 'description': '250,000 SF Data Center Building', 'estimated_cost': 185000000, 'issue_date': '2024-11-15', 'status': 'Active', 'applicant': 'Amazon Data Services', 'is_datacenter': 1},
        {'permit_id': 'MARI-BP-2024-0089', 'county': 'Maricopa', 'state': 'AZ', 'address': '3500 W Elliot Road, Mesa', 'permit_type': 'Commercial Construction', 'description': '400,000 SF Hyperscale Data Center', 'estimated_cost': 450000000, 'issue_date': '2024-10-22', 'status': 'Active', 'applicant': 'Google LLC', 'is_datacenter': 1},
        {'permit_id': 'DALL-BP-2024-0156', 'county': 'Dallas', 'state': 'TX', 'address': '2200 E Carrier Parkway', 'permit_type': 'Commercial Expansion', 'description': '150,000 SF Data Center Expansion', 'estimated_cost': 95000000, 'issue_date': '2024-12-01', 'status': 'Pending', 'applicant': 'QTS Realty Trust', 'is_datacenter': 1},
        {'permit_id': 'CLAR-BP-2024-0078', 'county': 'Clark', 'state': 'NV', 'address': 'The Citadel Campus Phase IV', 'permit_type': 'Commercial Construction', 'description': '1,000,000 SF Data Center Campus', 'estimated_cost': 850000000, 'issue_date': '2024-09-30', 'status': 'Active', 'applicant': 'Switch Inc', 'is_datacenter': 1},
        {'permit_id': 'FRAN-BP-2024-0034', 'county': 'Franklin', 'state': 'OH', 'address': '6000 Enterprise Parkway', 'permit_type': 'Commercial Construction', 'description': '200,000 SF Data Center', 'estimated_cost': 125000000, 'issue_date': '2024-11-08', 'status': 'Active', 'applicant': 'AWS', 'is_datacenter': 1},
    ]
    
    @classmethod
    def get_dc_permits(cls, state=None, county=None, days=90):
        """Get data center building permits"""
        permits = [p for p in cls.SAMPLE_PERMITS if p.get('is_datacenter', 0) == 1]
        if state:
            permits = [p for p in permits if p['state'].upper() == state.upper()]
        if county:
            permits = [p for p in permits if county.lower() in p['county'].lower()]
        
        total_investment = sum(p['estimated_cost'] for p in permits)
        
        return {
            'permits': permits,
            'count': len(permits),
            'total_investment': total_investment,
            'filters': {'state': state, 'county': county, 'days': days}
        }
    
    @classmethod
    def get_permit_stats(cls):
        """Get permit statistics by market"""
        permits = cls.SAMPLE_PERMITS
        by_state = {}
        for p in permits:
            state = p['state']
            if state not in by_state:
                by_state[state] = {'count': 0, 'total_value': 0}
            by_state[state]['count'] += 1
            by_state[state]['total_value'] += p['estimated_cost']
        
        return {
            'by_state': by_state,
            'total_permits': len(permits),
            'total_investment': sum(p['estimated_cost'] for p in permits)
        }


class MarketTrends:
    """Real estate market trends for DC markets"""
    
    TRENDS = {
        'Northern Virginia': {
            'land_price_trend': 'rising',
            'yoy_change': 12.5,
            'vacancy_rate': 2.1,
            'avg_price_per_acre': 285000,
            'new_supply_mw': 450,
            'absorption_rate': 98.5,
        },
        'Phoenix': {
            'land_price_trend': 'rising',
            'yoy_change': 18.2,
            'vacancy_rate': 3.5,
            'avg_price_per_acre': 125000,
            'new_supply_mw': 320,
            'absorption_rate': 95.2,
        },
        'Dallas': {
            'land_price_trend': 'stable',
            'yoy_change': 5.8,
            'vacancy_rate': 4.2,
            'avg_price_per_acre': 175000,
            'new_supply_mw': 280,
            'absorption_rate': 92.8,
        },
        'Las Vegas': {
            'land_price_trend': 'rising',
            'yoy_change': 15.3,
            'vacancy_rate': 1.8,
            'avg_price_per_acre': 95000,
            'new_supply_mw': 200,
            'absorption_rate': 99.1,
        },
        'Atlanta': {
            'land_price_trend': 'stable',
            'yoy_change': 4.2,
            'vacancy_rate': 5.5,
            'avg_price_per_acre': 110000,
            'new_supply_mw': 180,
            'absorption_rate': 88.5,
        },
        'Columbus': {
            'land_price_trend': 'rising',
            'yoy_change': 22.5,
            'vacancy_rate': 2.8,
            'avg_price_per_acre': 85000,
            'new_supply_mw': 350,
            'absorption_rate': 96.8,
        },
    }
    
    @classmethod
    def get_market_trend(cls, market):
        """Get trend for a market"""
        trend = cls.TRENDS.get(market)
        if trend:
            return {'market': market, 'trend': trend, 'timestamp': datetime.now().isoformat()}
        return {'error': f'Market not found: {market}', 'available': list(cls.TRENDS.keys())}
    
    @classmethod
    def compare_markets(cls, markets=None):
        """Compare multiple markets"""
        if not markets:
            markets = list(cls.TRENDS.keys())
        
        results = []
        for m in markets:
            if m in cls.TRENDS:
                results.append({'market': m, **cls.TRENDS[m]})
        
        results.sort(key=lambda x: x['yoy_change'], reverse=True)
        return {
            'markets': results,
            'hottest': results[0] if results else None,
            'lowest_vacancy': min(results, key=lambda x: x['vacancy_rate']) if results else None
        }


@real_estate_bp.route('/api/real-estate/markets')
def get_markets():
    """Get all DC markets with county info"""
    return jsonify({
        'success': True,
        **CountyAssessorAPI.get_market_counties()
    })

@real_estate_bp.route('/api/real-estate/land-values')
def get_land_values():
    """Get land values for a county"""
    county = request.args.get('county', 'Loudoun')
    return jsonify({
        'success': True,
        **CountyAssessorAPI.get_land_values(county)
    })

@real_estate_bp.route('/api/real-estate/parcels')
def search_parcels():
    """Search available parcels"""
    county = request.args.get('county')
    min_acres = request.args.get('min_acres', type=float, default=10)
    max_price = request.args.get('max_price', type=float)
    dc_suitable = request.args.get('dc_suitable', 'true').lower() == 'true'
    
    return jsonify({
        'success': True,
        **CountyAssessorAPI.search_parcels(county, min_acres, max_price, dc_suitable)
    })

@real_estate_bp.route('/api/real-estate/parcels/<parcel_id>')
def get_parcel(parcel_id):
    """Get parcel details"""
    return jsonify({
        'success': True,
        **CountyAssessorAPI.get_parcel_details(parcel_id)
    })

@real_estate_bp.route('/api/real-estate/zoning')
def get_zoning():
    """Get DC-suitable zoning codes"""
    state = request.args.get('state')
    return jsonify({
        'success': True,
        **ZoningAPI.get_dc_zones(state)
    })

@real_estate_bp.route('/api/real-estate/zoning/check')
def check_zoning():
    """Check if zone allows data centers"""
    state = request.args.get('state', 'VA')
    zone_code = request.args.get('zone', 'PD-IP')
    return jsonify({
        'success': True,
        **ZoningAPI.check_zone_compatibility(state, zone_code)
    })

@real_estate_bp.route('/api/real-estate/permits')
def get_permits():
    """Get DC building permits"""
    state = request.args.get('state')
    county = request.args.get('county')
    days = request.args.get('days', type=int, default=90)
    return jsonify({
        'success': True,
        **BuildingPermitTracker.get_dc_permits(state, county, days)
    })

@real_estate_bp.route('/api/real-estate/permits/stats')
def get_permit_stats():
    """Get permit statistics"""
    return jsonify({
        'success': True,
        **BuildingPermitTracker.get_permit_stats()
    })

@real_estate_bp.route('/api/real-estate/trends')
def get_market_trends():
    """Get market trends"""
    market = request.args.get('market')
    if market:
        return jsonify({'success': True, **MarketTrends.get_market_trend(market)})
    return jsonify({'success': True, **MarketTrends.compare_markets()})

@real_estate_bp.route('/api/real-estate/summary')
def get_real_estate_summary():
    """Get summary of real estate intelligence"""
    return jsonify({
        'success': True,
        'modules': {
            'county_assessor': {
                'description': 'Land values, ownership, parcel data',
                'markets': len(CountyAssessorAPI.DC_MARKETS),
                'endpoints': ['/api/real-estate/markets', '/api/real-estate/land-values', '/api/real-estate/parcels']
            },
            'zoning': {
                'description': 'DC-suitable zoning codes by state',
                'states': len(ZoningAPI.DC_ZONES),
                'endpoints': ['/api/real-estate/zoning', '/api/real-estate/zoning/check']
            },
            'permits': {
                'description': 'DC building permit tracking',
                'active_permits': len(BuildingPermitTracker.SAMPLE_PERMITS),
                'endpoints': ['/api/real-estate/permits', '/api/real-estate/permits/stats']
            },
            'market_trends': {
                'description': 'Land price trends, vacancy, absorption',
                'markets': len(MarketTrends.TRENDS),
                'endpoints': ['/api/real-estate/trends']
            }
        },
        'timestamp': datetime.now().isoformat()
    })


def register_real_estate_api(app):
    """Register real estate routes"""
    app.register_blueprint(real_estate_bp)
    logger.info("✅ Real Estate Intelligence registered")
    print("🏠 Real Estate Intelligence: ✅ Registered")
    print("   📊 County Assessors: /api/real-estate/land-values")
    print("   🗺️ Zoning: /api/real-estate/zoning")
    print("   🏗️ Permits: /api/real-estate/permits")
    print("   📈 Trends: /api/real-estate/trends")
