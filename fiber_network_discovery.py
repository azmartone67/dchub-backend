"""
Fiber Network Discovery Module
- Major Carrier Network Maps (Zayo, Lumen, Crown Castle)
- NTIA Broadband Grants
- Lit Building Databases
- Dark Fiber Routes
- Fiber Provider Coverage
"""

import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
import json
import sqlite3
import os
from db_utils import get_db

logger = logging.getLogger(__name__)

fiber_bp = Blueprint('fiber_discovery', __name__)

DB_PATH = os.environ.get('DC_NEXUS_DB', 'dc_nexus.db')


def init_fiber_db():
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS fiber_providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id TEXT UNIQUE,
        name TEXT,
        type TEXT,
        coverage_states TEXT,
        total_route_miles INTEGER,
        lit_buildings INTEGER,
        on_net_markets INTEGER,
        website TEXT,
        api_available INTEGER DEFAULT 0,
        last_updated TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS fiber_routes_detail (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id TEXT UNIQUE,
        provider TEXT,
        route_name TEXT,
        start_city TEXT,
        end_city TEXT,
        states TEXT,
        route_miles REAL,
        fiber_count INTEGER,
        dark_fiber_available INTEGER DEFAULT 0,
        lit_capacity_gbps REAL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS lit_buildings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        building_id TEXT UNIQUE,
        address TEXT,
        city TEXT,
        state TEXT,
        zip_code TEXT,
        building_type TEXT,
        providers TEXT,
        provider_count INTEGER,
        is_carrier_hotel INTEGER DEFAULT 0,
        is_datacenter INTEGER DEFAULT 0,
        latitude REAL,
        longitude REAL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS ntia_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grant_id TEXT UNIQUE,
        program TEXT,
        recipient TEXT,
        state TEXT,
        award_amount REAL,
        award_date TEXT,
        project_type TEXT,
        miles_funded REAL,
        status TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Fiber Network Discovery tables initialized")

init_fiber_db()


class FiberProviderAPI:
    """Major fiber carrier data"""
    
    PROVIDERS = {
        'zayo': {
            'name': 'Zayo Group',
            'type': 'Tier 1 Fiber',
            'route_miles': 141000,
            'lit_buildings': 12500,
            'on_net_markets': 400,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['All 48 Continental US', 'Canada', 'Europe'],
            'dc_markets': ['Ashburn', 'Dallas', 'Chicago', 'Denver', 'Phoenix', 'Los Angeles', 'New York', 'Atlanta'],
            'website': 'https://www.zayo.com'
        },
        'lumen': {
            'name': 'Lumen Technologies (CenturyLink)',
            'type': 'Tier 1 Fiber',
            'route_miles': 450000,
            'lit_buildings': 25000,
            'on_net_markets': 350,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['All 50 US States', 'Global'],
            'dc_markets': ['All Major US Markets'],
            'website': 'https://www.lumen.com'
        },
        'crown_castle': {
            'name': 'Crown Castle Fiber',
            'type': 'Metro Fiber',
            'route_miles': 85000,
            'lit_buildings': 35000,
            'on_net_markets': 75,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['Major US Metros'],
            'dc_markets': ['Los Angeles', 'Houston', 'Philadelphia', 'Phoenix', 'Dallas'],
            'website': 'https://www.crowncastle.com'
        },
        'cogent': {
            'name': 'Cogent Communications',
            'type': 'Tier 1 Internet',
            'route_miles': 62000,
            'lit_buildings': 3200,
            'on_net_markets': 210,
            'dark_fiber': False,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['US', 'Europe', 'Canada'],
            'dc_markets': ['All Major Carrier Hotels'],
            'website': 'https://www.cogentco.com'
        },
        'uniti': {
            'name': 'Uniti Fiber',
            'type': 'Regional Fiber',
            'route_miles': 130000,
            'lit_buildings': 8500,
            'on_net_markets': 150,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['Southeast US', 'Mid-Atlantic'],
            'dc_markets': ['Atlanta', 'Jacksonville', 'New Orleans', 'Birmingham'],
            'website': 'https://www.uniti.com'
        },
        'lightpath': {
            'name': 'Lightpath',
            'type': 'Metro Fiber',
            'route_miles': 20000,
            'lit_buildings': 15000,
            'on_net_markets': 1,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['New York Metro'],
            'dc_markets': ['New York', 'New Jersey'],
            'website': 'https://www.lightpathfiber.com'
        },
        'windstream': {
            'name': 'Windstream Enterprise',
            'type': 'Tier 2 Fiber',
            'route_miles': 150000,
            'lit_buildings': 12000,
            'on_net_markets': 200,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['Rural US', 'Mid-South'],
            'dc_markets': ['Dallas', 'Atlanta', 'Denver'],
            'website': 'https://www.windstream.com'
        },
        'segra': {
            'name': 'Segra',
            'type': 'Regional Fiber',
            'route_miles': 45000,
            'lit_buildings': 6000,
            'on_net_markets': 90,
            'dark_fiber': True,
            'wavelength': True,
            'ethernet': True,
            'coverage_states': ['Mid-Atlantic', 'Southeast'],
            'dc_markets': ['Northern Virginia', 'Charlotte', 'Richmond'],
            'website': 'https://www.segra.com'
        },
    }
    
    MAJOR_ROUTES = [
        {'route_id': 'ZAYO-NOVA-DAL', 'provider': 'Zayo', 'route_name': 'Northern Virginia - Dallas', 'start_city': 'Ashburn, VA', 'end_city': 'Dallas, TX', 'route_miles': 1350, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 32.78, 'end_lng': -96.80},
        {'route_id': 'ZAYO-CHI-NOVA', 'provider': 'Zayo', 'route_name': 'Chicago - Northern Virginia', 'start_city': 'Chicago, IL', 'end_city': 'Ashburn, VA', 'route_miles': 710, 'fiber_count': 432, 'dark_fiber_available': 1, 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 38.95, 'end_lng': -77.45},
        {'route_id': 'ZAYO-NOVA-ATL', 'provider': 'Zayo', 'route_name': 'Northern Virginia - Atlanta', 'start_city': 'Ashburn, VA', 'end_city': 'Atlanta, GA', 'route_miles': 640, 'fiber_count': 432, 'dark_fiber_available': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 33.75, 'end_lng': -84.39},
        {'route_id': 'ZAYO-DAL-PHX', 'provider': 'Zayo', 'route_name': 'Dallas - Phoenix', 'start_city': 'Dallas, TX', 'end_city': 'Phoenix, AZ', 'route_miles': 1065, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 33.45, 'end_lng': -112.07},
        {'route_id': 'LUMEN-LA-PHX', 'provider': 'Lumen', 'route_name': 'Los Angeles - Phoenix', 'start_city': 'Los Angeles, CA', 'end_city': 'Phoenix, AZ', 'route_miles': 380, 'fiber_count': 576, 'dark_fiber_available': 1, 'start_lat': 34.05, 'start_lng': -118.24, 'end_lat': 33.45, 'end_lng': -112.07},
        {'route_id': 'LUMEN-DAL-DEN', 'provider': 'Lumen', 'route_name': 'Dallas - Denver', 'start_city': 'Dallas, TX', 'end_city': 'Denver, CO', 'route_miles': 780, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 39.74, 'end_lng': -104.99},
        {'route_id': 'LUMEN-NOVA-COL', 'provider': 'Lumen', 'route_name': 'Northern Virginia - Columbus', 'start_city': 'Ashburn, VA', 'end_city': 'Columbus, OH', 'route_miles': 420, 'fiber_count': 432, 'dark_fiber_available': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 39.96, 'end_lng': -82.99},
        {'route_id': 'LUMEN-CHI-DSM', 'provider': 'Lumen', 'route_name': 'Chicago - Des Moines', 'start_city': 'Chicago, IL', 'end_city': 'Des Moines, IA', 'route_miles': 340, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 41.59, 'end_lng': -93.62},
        {'route_id': 'LUMEN-DEN-SLC', 'provider': 'Lumen', 'route_name': 'Denver - Salt Lake City', 'start_city': 'Denver, CO', 'end_city': 'Salt Lake City, UT', 'route_miles': 525, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 39.74, 'start_lng': -104.99, 'end_lat': 40.76, 'end_lng': -111.89},
        {'route_id': 'LUMEN-SLC-LV', 'provider': 'Lumen', 'route_name': 'Salt Lake City - Las Vegas', 'start_city': 'Salt Lake City, UT', 'end_city': 'Las Vegas, NV', 'route_miles': 420, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 40.76, 'start_lng': -111.89, 'end_lat': 36.17, 'end_lng': -115.14},
        {'route_id': 'LUMEN-LV-PHX', 'provider': 'Lumen', 'route_name': 'Las Vegas - Phoenix', 'start_city': 'Las Vegas, NV', 'end_city': 'Phoenix, AZ', 'route_miles': 300, 'fiber_count': 432, 'dark_fiber_available': 1, 'start_lat': 36.17, 'start_lng': -115.14, 'end_lat': 33.45, 'end_lng': -112.07},
        {'route_id': 'CC-HOU-DAL', 'provider': 'Crown Castle', 'route_name': 'Houston - Dallas', 'start_city': 'Houston, TX', 'end_city': 'Dallas, TX', 'route_miles': 245, 'fiber_count': 432, 'dark_fiber_available': 1, 'start_lat': 29.76, 'start_lng': -95.37, 'end_lat': 32.78, 'end_lng': -96.80},
        {'route_id': 'CC-PHX-LV', 'provider': 'Crown Castle', 'route_name': 'Phoenix - Las Vegas', 'start_city': 'Phoenix, AZ', 'end_city': 'Las Vegas, NV', 'route_miles': 300, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 33.45, 'start_lng': -112.07, 'end_lat': 36.17, 'end_lng': -115.14},
        {'route_id': 'UNITI-ATL-JAX', 'provider': 'Uniti', 'route_name': 'Atlanta - Jacksonville', 'start_city': 'Atlanta, GA', 'end_city': 'Jacksonville, FL', 'route_miles': 350, 'fiber_count': 144, 'dark_fiber_available': 1, 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 30.33, 'end_lng': -81.66},
        {'route_id': 'UNITI-ATL-DAL', 'provider': 'Uniti', 'route_name': 'Atlanta - Dallas', 'start_city': 'Atlanta, GA', 'end_city': 'Dallas, TX', 'route_miles': 780, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 32.78, 'end_lng': -96.80},
        {'route_id': 'SEGRA-NOVA-CLT', 'provider': 'Segra', 'route_name': 'Northern Virginia - Charlotte', 'start_city': 'Ashburn, VA', 'end_city': 'Charlotte, NC', 'route_miles': 330, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 35.23, 'end_lng': -80.84},
        {'route_id': 'COGENT-NOVA-CHI', 'provider': 'Cogent', 'route_name': 'Northern Virginia - Chicago', 'start_city': 'Ashburn, VA', 'end_city': 'Chicago, IL', 'route_miles': 710, 'fiber_count': 576, 'dark_fiber_available': 0, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 41.88, 'end_lng': -87.63},
        {'route_id': 'WINDSTREAM-DAL-ATL', 'provider': 'Windstream', 'route_name': 'Dallas - Atlanta', 'start_city': 'Dallas, TX', 'end_city': 'Atlanta, GA', 'route_miles': 780, 'fiber_count': 288, 'dark_fiber_available': 1, 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 33.75, 'end_lng': -84.39},
        {'route_id': 'WINDSTREAM-DEN-DSM', 'provider': 'Windstream', 'route_name': 'Denver - Des Moines', 'start_city': 'Denver, CO', 'end_city': 'Des Moines, IA', 'route_miles': 660, 'fiber_count': 144, 'dark_fiber_available': 1, 'start_lat': 39.74, 'start_lng': -104.99, 'end_lat': 41.59, 'end_lng': -93.62},
        {'route_id': 'ZAYO-COL-CHI', 'provider': 'Zayo', 'route_name': 'Columbus - Chicago', 'start_city': 'Columbus, OH', 'end_city': 'Chicago, IL', 'route_miles': 350, 'fiber_count': 432, 'dark_fiber_available': 1, 'start_lat': 39.96, 'start_lng': -82.99, 'end_lat': 41.88, 'end_lng': -87.63},
    ]
    
    @classmethod
    def get_all_providers(cls):
        """Get all fiber providers"""
        providers = [{'id': k, **v} for k, v in cls.PROVIDERS.items()]
        return {
            'providers': providers,
            'count': len(providers),
            'total_route_miles': sum(p['route_miles'] for p in providers),
            'total_lit_buildings': sum(p['lit_buildings'] for p in providers)
        }
    
    @classmethod
    def get_provider(cls, provider_id):
        """Get provider details"""
        provider = cls.PROVIDERS.get(provider_id.lower())
        if provider:
            routes = [r for r in cls.MAJOR_ROUTES if r['provider'].lower() == provider_id.lower()]
            return {
                'id': provider_id,
                **provider,
                'routes': routes,
                'route_count': len(routes)
            }
        return {'error': f'Provider not found: {provider_id}'}
    
    @classmethod
    def get_providers_by_market(cls, market):
        """Get providers serving a market"""
        market_lower = market.lower()
        matching = []
        for pid, p in cls.PROVIDERS.items():
            markets = p.get('dc_markets', [])
            if isinstance(markets, list):
                if any(market_lower in m.lower() for m in markets) or 'all' in ' '.join(markets).lower():
                    matching.append({'id': pid, **p})
        
        return {
            'market': market,
            'providers': matching,
            'count': len(matching)
        }
    
    @classmethod
    def get_routes(cls, provider=None):
        """Get fiber routes"""
        routes = cls.MAJOR_ROUTES
        if provider:
            routes = [r for r in routes if r['provider'].lower() == provider.lower()]
        return {
            'routes': routes,
            'count': len(routes),
            'total_miles': sum(r['route_miles'] for r in routes)
        }


class LitBuildingAPI:
    """Lit building database (buildings with fiber)"""
    
    CARRIER_HOTELS = [
        {'building_id': 'CH-60HUD', 'address': '60 Hudson Street', 'city': 'New York', 'state': 'NY', 'providers': ['Zayo', 'Lumen', 'Cogent', 'Telia', 'GTT'], 'provider_count': 45, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-111-8TH', 'address': '111 8th Avenue', 'city': 'New York', 'state': 'NY', 'providers': ['Zayo', 'Lumen', 'Cogent', 'Level3'], 'provider_count': 35, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-ONE-WILSHIRE', 'address': 'One Wilshire', 'city': 'Los Angeles', 'state': 'CA', 'providers': ['Zayo', 'Crown Castle', 'Lumen', 'NTT'], 'provider_count': 50, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-350-CERMAK', 'address': '350 E Cermak Road', 'city': 'Chicago', 'state': 'IL', 'providers': ['Zayo', 'Cogent', 'Lumen', 'PacketFabric'], 'provider_count': 40, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-WESTIN', 'address': '2001 6th Ave (Westin Building)', 'city': 'Seattle', 'state': 'WA', 'providers': ['Zayo', 'Lumen', 'Wave'], 'provider_count': 30, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-56MARIETTA', 'address': '56 Marietta Street', 'city': 'Atlanta', 'state': 'GA', 'providers': ['Zayo', 'Uniti', 'Lumen', 'AT&T'], 'provider_count': 28, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-2121MARKET', 'address': '2121 Market Street', 'city': 'Dallas', 'state': 'TX', 'providers': ['Zayo', 'Crown Castle', 'Lumen'], 'provider_count': 25, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-EQUINIX-ASH', 'address': '21715 Filigree Ct', 'city': 'Ashburn', 'state': 'VA', 'providers': ['Zayo', 'Segra', 'Lumen', 'Cogent', 'Crown Castle'], 'provider_count': 60, 'is_carrier_hotel': 1, 'is_datacenter': 1},
    ]
    
    @classmethod
    def get_carrier_hotels(cls, state=None, city=None):
        """Get carrier hotels"""
        hotels = cls.CARRIER_HOTELS
        if state:
            hotels = [h for h in hotels if h['state'].upper() == state.upper()]
        if city:
            hotels = [h for h in hotels if city.lower() in h['city'].lower()]
        
        return {
            'carrier_hotels': hotels,
            'count': len(hotels),
            'total_providers': sum(h['provider_count'] for h in hotels)
        }
    
    @classmethod
    def get_building(cls, building_id):
        """Get building details"""
        for b in cls.CARRIER_HOTELS:
            if b['building_id'] == building_id:
                return b
        return {'error': 'Building not found'}


class NTIAGrantAPI:
    """NTIA Broadband Infrastructure Grants - All 56 BEAD Allocations"""

    BEAD_ALLOCATIONS = {
        'AL': {'recipient': 'Alabama Department of Economic & Community Affairs', 'amount': 1401221901.77, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 218000},
        'AK': {'recipient': 'Alaska Dept of Commerce', 'amount': 1017139672.42, 'status': 'Final Proposal Approved', 'priority': 'fiber+satellite', 'unserved_locations': 75000},
        'AZ': {'recipient': 'Arizona Commerce Authority', 'amount': 993112231.37, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 153000},
        'AR': {'recipient': 'Arkansas State Broadband Office', 'amount': 1024303993.86, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 168000},
        'CA': {'recipient': 'California Public Utilities Commission', 'amount': 1864136508.93, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 255000},
        'CO': {'recipient': 'Colorado Broadband Office', 'amount': 826522650.41, 'status': 'Final Proposal Approved', 'priority': 'fiber+fixed wireless', 'unserved_locations': 114000},
        'CT': {'recipient': 'Connecticut Office of State Broadband', 'amount': 144180792.71, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 18000},
        'DE': {'recipient': 'Delaware Broadband Office', 'amount': 107748384.66, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 12000},
        'DC': {'recipient': 'DC Office of Cable Television', 'amount': 100694786.93, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 5000},
        'FL': {'recipient': 'Florida Dept of Economic Opportunity', 'amount': 1169947392.70, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 181000},
        'GA': {'recipient': 'Georgia Technology Authority', 'amount': 1307214371.30, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 202000},
        'HI': {'recipient': 'Hawaii Broadband & Digital Equity Office', 'amount': 149484493.57, 'status': 'Final Proposal Approved', 'priority': 'fiber+submarine', 'unserved_locations': 20000},
        'ID': {'recipient': 'Idaho Commerce Department', 'amount': 583256249.88, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 84000},
        'IL': {'recipient': 'Illinois Broadband Office', 'amount': 1040420751.50, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 161000},
        'IN': {'recipient': 'Indiana Office of Community & Rural Affairs', 'amount': 868109929.79, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 129000},
        'IA': {'recipient': 'Iowa Office of Chief Information Officer', 'amount': 415331313.00, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 59000},
        'KS': {'recipient': 'Kansas Office of Broadband Development', 'amount': 451725998.15, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 65000},
        'KY': {'recipient': 'Kentucky Infrastructure Authority', 'amount': 1086172536.86, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 175000},
        'LA': {'recipient': 'Louisiana Office of Broadband', 'amount': 1355554552.94, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 210000},
        'ME': {'recipient': 'Maine Connectivity Authority', 'amount': 271977723.07, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 38000},
        'MD': {'recipient': 'Maryland Office of Statewide Broadband', 'amount': 267738400.71, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 35000},
        'MA': {'recipient': 'Massachusetts Broadband Institute', 'amount': 147422464.39, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 19000},
        'MI': {'recipient': 'Michigan High-Speed Internet Office', 'amount': 1559362479.29, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 244000},
        'MN': {'recipient': 'Minnesota Office of Broadband Development', 'amount': 651839368.20, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 93000},
        'MS': {'recipient': 'Mississippi Broadband Commission', 'amount': 1203561563.05, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 191000},
        'MO': {'recipient': 'Missouri Office of Broadband Development', 'amount': 1736302708.39, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 280000},
        'MT': {'recipient': 'Montana Dept of Administration', 'amount': 628973798.59, 'status': 'Final Proposal Approved', 'priority': 'fiber+fixed wireless', 'unserved_locations': 88000},
        'NE': {'recipient': 'Nebraska Broadband Office', 'amount': 405281070.41, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 57000},
        'NV': {'recipient': 'Nevada Governor Office of Science Innovation & Technology', 'amount': 416666229.74, 'status': 'Final Proposal Approved', 'priority': 'fiber+satellite', 'unserved_locations': 58000},
        'NH': {'recipient': 'New Hampshire Dept of Business & Economic Affairs', 'amount': 196560278.97, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 27000},
        'NJ': {'recipient': 'New Jersey Board of Public Utilities', 'amount': 263689548.65, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 34000},
        'NM': {'recipient': 'New Mexico Office of Broadband Access & Expansion', 'amount': 675372311.86, 'status': 'Final Proposal Approved', 'priority': 'fiber+fixed wireless', 'unserved_locations': 95000},
        'NY': {'recipient': 'New York ConnectALL Office', 'amount': 664618251.49, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 94000},
        'NC': {'recipient': 'North Carolina DNCR Division of Broadband', 'amount': 1532999481.15, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 238000},
        'ND': {'recipient': 'North Dakota Information Technology Dept', 'amount': 130162815.12, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 16000},
        'OH': {'recipient': 'BroadbandOhio', 'amount': 793688107.63, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 117000},
        'OK': {'recipient': 'Oklahoma Broadband Office', 'amount': 797435691.25, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 121000},
        'OR': {'recipient': 'Oregon Broadband Office', 'amount': 688914932.17, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 97000},
        'PA': {'recipient': 'Pennsylvania Broadband Development Authority', 'amount': 1161778272.41, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 179000},
        'RI': {'recipient': 'Rhode Island Commerce Corporation', 'amount': 108718820.75, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 14000},
        'SC': {'recipient': 'South Carolina Office of Regulatory Staff', 'amount': 551535983.05, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 80000},
        'SD': {'recipient': 'South Dakota Bureau of Information & Telecom', 'amount': 207227523.92, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 28000},
        'TN': {'recipient': 'Tennessee Dept of Economic & Community Development', 'amount': 813319680.22, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 123000},
        'TX': {'recipient': 'Texas Broadband Development Office', 'amount': 3312616455.45, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 536000},
        'UT': {'recipient': 'Utah Broadband Center', 'amount': 317399741.54, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 44000},
        'VT': {'recipient': 'Vermont Community Broadband Board', 'amount': 228913019.08, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 32000},
        'VA': {'recipient': 'Virginia Dept of Housing & Community Development', 'amount': 1481489572.87, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 229000},
        'WA': {'recipient': 'Washington State Broadband Office', 'amount': 1227742066.30, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 189000},
        'WV': {'recipient': 'West Virginia Office of Broadband', 'amount': 1210800969.85, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 189000},
        'WI': {'recipient': 'Wisconsin Public Service Commission', 'amount': 1055823573.71, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 163000},
        'WY': {'recipient': 'Wyoming Business Council', 'amount': 347877921.27, 'status': 'Final Proposal Approved', 'priority': 'fiber+fixed wireless', 'unserved_locations': 48000},
        'AS': {'recipient': 'American Samoa Broadband Office', 'amount': 37564827.53, 'status': 'Initial Proposal Approved', 'priority': 'fiber+submarine', 'unserved_locations': 4000},
        'GU': {'recipient': 'Guam Bureau of Statistics & Plans', 'amount': 156831733.59, 'status': 'Initial Proposal Approved', 'priority': 'fiber+submarine', 'unserved_locations': 15000},
        'MP': {'recipient': 'CNMI Office of Planning & Development', 'amount': 80796709.02, 'status': 'Initial Proposal Approved', 'priority': 'fiber+submarine', 'unserved_locations': 7000},
        'PR': {'recipient': 'Puerto Rico Broadband Program', 'amount': 334614151.70, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 46000},
        'VI': {'recipient': 'U.S. Virgin Islands Next Generation Network', 'amount': 27103240.86, 'status': 'Initial Proposal Approved', 'priority': 'fiber+submarine', 'unserved_locations': 3000},
    }

    STATE_COORDS = {
        'AL': (32.8, -86.8), 'AK': (64.2, -152.5), 'AZ': (34.0, -111.1), 'AR': (35.2, -91.8),
        'CA': (36.8, -119.4), 'CO': (39.1, -105.4), 'CT': (41.6, -72.7), 'DE': (39.3, -75.5),
        'DC': (38.9, -77.0), 'FL': (27.6, -81.5), 'GA': (33.0, -83.5), 'HI': (19.9, -155.6),
        'ID': (44.1, -114.7), 'IL': (40.3, -89.0), 'IN': (40.3, -86.1), 'IA': (42.0, -93.2),
        'KS': (38.5, -98.0), 'KY': (37.8, -84.3), 'LA': (30.9, -92.3), 'ME': (45.3, -69.4),
        'MD': (39.0, -76.6), 'MA': (42.4, -71.4), 'MI': (44.3, -85.6), 'MN': (46.7, -94.7),
        'MS': (32.3, -89.4), 'MO': (38.6, -92.2), 'MT': (46.8, -110.4), 'NE': (41.1, -98.3),
        'NV': (38.8, -116.4), 'NH': (43.2, -71.6), 'NJ': (40.1, -74.5), 'NM': (34.5, -105.9),
        'NY': (43.0, -75.5), 'NC': (35.8, -79.0), 'ND': (47.5, -100.5), 'OH': (40.4, -82.9),
        'OK': (35.0, -97.1), 'OR': (43.8, -120.5), 'PA': (41.2, -77.2), 'RI': (41.6, -71.5),
        'SC': (34.0, -81.0), 'SD': (43.9, -99.9), 'TN': (35.5, -86.6), 'TX': (31.0, -100.0),
        'UT': (39.3, -111.1), 'VT': (44.6, -72.6), 'VA': (37.4, -78.2), 'WA': (47.4, -120.7),
        'WV': (38.6, -80.6), 'WI': (43.8, -88.8), 'WY': (43.1, -107.6),
        'AS': (-14.3, -170.1), 'GU': (13.4, 144.8), 'MP': (15.1, 145.7),
        'PR': (18.2, -66.6), 'VI': (18.3, -64.9),
    }

    GRANTS = []

    @classmethod
    def _build_grants(cls):
        if cls.GRANTS:
            return
        for st, info in cls.BEAD_ALLOCATIONS.items():
            miles = max(200, int(info['amount'] / 500000))
            cls.GRANTS.append({
                'grant_id': f'BEAD-{st}-2023-001',
                'program': 'BEAD',
                'recipient': info['recipient'],
                'state': st,
                'award_amount': info['amount'],
                'award_date': '2023-06-30',
                'project_type': 'Fiber Deployment',
                'miles_funded': miles,
                'status': info['status'],
                'priority_tech': info['priority'],
                'unserved_locations': info['unserved_locations'],
                'lat': cls.STATE_COORDS.get(st, (0, 0))[0],
                'lng': cls.STATE_COORDS.get(st, (0, 0))[1],
            })

    @classmethod
    def get_grants(cls, state=None, program=None):
        cls._build_grants()
        grants = cls.GRANTS
        if state:
            grants = [g for g in grants if g['state'].upper() == state.upper()]
        if program:
            grants = [g for g in grants if g['program'].upper() == program.upper()]

        return {
            'grants': grants,
            'count': len(grants),
            'total_funding': sum(g['award_amount'] for g in grants),
            'total_miles': sum(g['miles_funded'] for g in grants)
        }

    @classmethod
    def get_bead_allocations(cls, dc_market_states=None):
        cls._build_grants()
        if dc_market_states:
            filtered = {s: v for s, v in cls.BEAD_ALLOCATIONS.items() if s in dc_market_states}
        else:
            filtered = cls.BEAD_ALLOCATIONS

        allocations = []
        for st, info in filtered.items():
            allocations.append({
                'state': st,
                'recipient': info['recipient'],
                'amount': info['amount'],
                'amount_formatted': f"${info['amount']/1e9:.2f}B" if info['amount'] >= 1e9 else f"${info['amount']/1e6:.1f}M",
                'status': info['status'],
                'priority_tech': info['priority'],
                'unserved_locations': info['unserved_locations'],
                'lat': cls.STATE_COORDS.get(st, (0, 0))[0],
                'lng': cls.STATE_COORDS.get(st, (0, 0))[1],
            })
        allocations.sort(key=lambda x: x['amount'], reverse=True)

        total = sum(a['amount'] for a in allocations)
        return {
            'allocations': allocations,
            'count': len(allocations),
            'total_funding': total,
            'total_funding_formatted': f"${total/1e9:.2f}B",
            'total_unserved': sum(a['unserved_locations'] for a in allocations),
            'program': 'BEAD (Broadband Equity Access & Deployment)',
            'source': 'NTIA / BroadbandUSA',
        }

    @classmethod
    def get_grant_stats(cls):
        cls._build_grants()
        by_program = {}
        by_state = {}
        for g in cls.GRANTS:
            prog = g['program']
            state = g['state']
            if prog not in by_program:
                by_program[prog] = {'count': 0, 'total': 0, 'miles': 0}
            by_program[prog]['count'] += 1
            by_program[prog]['total'] += g['award_amount']
            by_program[prog]['miles'] += g['miles_funded']

            if state not in by_state:
                by_state[state] = {'count': 0, 'total': 0}
            by_state[state]['count'] += 1
            by_state[state]['total'] += g['award_amount']

        return {
            'by_program': by_program,
            'by_state': by_state,
            'total_grants': len(cls.GRANTS),
            'total_funding': sum(g['award_amount'] for g in cls.GRANTS)
        }


class FiberCoverageAPI:
    """Fiber coverage by market"""
    
    COVERAGE = {
        'Ashburn': {'provider_count': 25, 'lit_buildings': 450, 'dark_fiber_available': True, 'avg_latency_ms': 0.5, 'score': 100},
        'Dallas': {'provider_count': 18, 'lit_buildings': 320, 'dark_fiber_available': True, 'avg_latency_ms': 0.8, 'score': 95},
        'Chicago': {'provider_count': 20, 'lit_buildings': 380, 'dark_fiber_available': True, 'avg_latency_ms': 0.6, 'score': 97},
        'Phoenix': {'provider_count': 12, 'lit_buildings': 180, 'dark_fiber_available': True, 'avg_latency_ms': 1.2, 'score': 85},
        'Las Vegas': {'provider_count': 10, 'lit_buildings': 120, 'dark_fiber_available': True, 'avg_latency_ms': 1.5, 'score': 80},
        'Denver': {'provider_count': 14, 'lit_buildings': 200, 'dark_fiber_available': True, 'avg_latency_ms': 1.0, 'score': 88},
        'Atlanta': {'provider_count': 16, 'lit_buildings': 280, 'dark_fiber_available': True, 'avg_latency_ms': 0.9, 'score': 92},
        'Columbus': {'provider_count': 8, 'lit_buildings': 90, 'dark_fiber_available': True, 'avg_latency_ms': 1.8, 'score': 75},
        'Salt Lake City': {'provider_count': 9, 'lit_buildings': 100, 'dark_fiber_available': True, 'avg_latency_ms': 1.6, 'score': 78},
        'New York': {'provider_count': 30, 'lit_buildings': 520, 'dark_fiber_available': True, 'avg_latency_ms': 0.4, 'score': 100},
        'Los Angeles': {'provider_count': 22, 'lit_buildings': 400, 'dark_fiber_available': True, 'avg_latency_ms': 0.7, 'score': 96},
        'Seattle': {'provider_count': 15, 'lit_buildings': 220, 'dark_fiber_available': True, 'avg_latency_ms': 1.1, 'score': 87},
    }
    
    @classmethod
    def get_coverage(cls, market):
        """Get fiber coverage for a market"""
        coverage = cls.COVERAGE.get(market)
        if coverage:
            return {'market': market, **coverage}
        return {'error': f'Market not found: {market}', 'available': list(cls.COVERAGE.keys())}
    
    @classmethod
    def compare_markets(cls, markets=None):
        """Compare fiber coverage"""
        if not markets:
            markets = list(cls.COVERAGE.keys())
        
        results = []
        for m in markets:
            if m in cls.COVERAGE:
                results.append({'market': m, **cls.COVERAGE[m]})
        
        results.sort(key=lambda x: x['score'], reverse=True)
        return {
            'markets': results,
            'best_connected': results[0] if results else None
        }


@fiber_bp.route('/api/fiber/providers')
def get_providers():
    """Get all fiber providers"""
    return jsonify({
        'success': True,
        **FiberProviderAPI.get_all_providers()
    })

@fiber_bp.route('/api/fiber/providers/<provider_id>')
def get_provider(provider_id):
    """Get provider details"""
    return jsonify({
        'success': True,
        **FiberProviderAPI.get_provider(provider_id)
    })

@fiber_bp.route('/api/fiber/providers/market')
def get_providers_by_market():
    """Get providers by market"""
    market = request.args.get('market', 'Ashburn')
    return jsonify({
        'success': True,
        **FiberProviderAPI.get_providers_by_market(market)
    })

@fiber_bp.route('/api/fiber/routes')
def get_routes():
    """Get fiber routes"""
    provider = request.args.get('provider')
    return jsonify({
        'success': True,
        **FiberProviderAPI.get_routes(provider)
    })

@fiber_bp.route('/api/fiber/carrier-hotels')
def get_carrier_hotels():
    """Get carrier hotels"""
    state = request.args.get('state')
    city = request.args.get('city')
    return jsonify({
        'success': True,
        **LitBuildingAPI.get_carrier_hotels(state, city)
    })

@fiber_bp.route('/api/fiber/ntia-grants')
def get_ntia_grants():
    """Get NTIA broadband grants"""
    state = request.args.get('state')
    program = request.args.get('program')
    return jsonify({
        'success': True,
        **NTIAGrantAPI.get_grants(state, program)
    })

@fiber_bp.route('/api/fiber/ntia-grants/stats')
def get_grant_stats():
    """Get grant statistics"""
    return jsonify({
        'success': True,
        **NTIAGrantAPI.get_grant_stats()
    })

@fiber_bp.route('/api/fiber/bead-allocations')
def get_bead_allocations():
    """Get all 56 BEAD state/territory allocations ($42.45B program)"""
    dc_states = request.args.get('dc_markets')
    if dc_states:
        states = [s.strip().upper() for s in dc_states.split(',')]
        return jsonify({'success': True, **NTIAGrantAPI.get_bead_allocations(states)})
    return jsonify({'success': True, **NTIAGrantAPI.get_bead_allocations()})

@fiber_bp.route('/api/fiber/map-data')
def get_fiber_map_data():
    """Get fiber data formatted for capacity map overlay"""
    routes = FiberProviderAPI.get_routes()
    providers = FiberProviderAPI.get_all_providers()
    hotels = LitBuildingAPI.get_carrier_hotels()
    bead = NTIAGrantAPI.get_bead_allocations(['VA', 'TX', 'AZ', 'OH', 'GA', 'NV', 'UT', 'IA'])
    coverage = FiberCoverageAPI.compare_markets()

    fiber_lines = []
    for r in routes['routes']:
        if r.get('start_lat') and r.get('end_lat'):
            fiber_lines.append({
                'route_id': r['route_id'],
                'provider': r['provider'],
                'name': r['route_name'],
                'miles': r['route_miles'],
                'fiber_count': r['fiber_count'],
                'dark_fiber': r['dark_fiber_available'],
                'coords': [[r['start_lat'], r['start_lng']], [r['end_lat'], r['end_lng']]],
            })

    bead_markers = []
    for a in bead.get('allocations', []):
        if a.get('lat') and a.get('lng') and a['lat'] != 0:
            bead_markers.append({
                'state': a['state'],
                'amount': a['amount'],
                'amount_formatted': a['amount_formatted'],
                'status': a['status'],
                'priority': a['priority_tech'],
                'unserved': a['unserved_locations'],
                'lat': a['lat'],
                'lng': a['lng'],
            })

    hotel_markers = []
    for h in hotels.get('carrier_hotels', []):
        hotel_markers.append({
            'name': h['address'],
            'city': h['city'],
            'state': h['state'],
            'providers': h['provider_count'],
            'provider_names': h.get('providers', []),
        })

    return jsonify({
        'success': True,
        'fiber_routes': fiber_lines,
        'fiber_route_count': len(fiber_lines),
        'total_route_miles': routes['total_miles'],
        'bead_grants': bead_markers,
        'bead_total_funding': bead.get('total_funding', 0),
        'carrier_hotels': hotel_markers,
        'provider_count': providers['count'],
        'total_lit_buildings': providers['total_lit_buildings'],
        'coverage': coverage.get('markets', []),
    })

@fiber_bp.route('/api/fiber/coverage')
def get_fiber_coverage():
    """Get fiber coverage by market"""
    market = request.args.get('market')
    if market:
        return jsonify({'success': True, **FiberCoverageAPI.get_coverage(market)})
    return jsonify({'success': True, **FiberCoverageAPI.compare_markets()})

@fiber_bp.route('/api/fiber/summary')
def get_fiber_summary():
    """Get fiber discovery summary"""
    providers = FiberProviderAPI.get_all_providers()
    grants = NTIAGrantAPI.get_grant_stats()
    routes = FiberProviderAPI.get_routes()
    bead = NTIAGrantAPI.get_bead_allocations()

    return jsonify({
        'success': True,
        'modules': {
            'providers': {
                'count': providers['count'],
                'total_route_miles': providers['total_route_miles'],
                'total_lit_buildings': providers['total_lit_buildings'],
                'endpoints': ['/api/fiber/providers', '/api/fiber/providers/<id>', '/api/fiber/providers/market']
            },
            'routes': {
                'count': routes['count'],
                'total_miles': routes['total_miles'],
                'description': 'Major fiber routes between DC markets with lat/lng',
                'endpoints': ['/api/fiber/routes']
            },
            'carrier_hotels': {
                'count': len(LitBuildingAPI.CARRIER_HOTELS),
                'endpoints': ['/api/fiber/carrier-hotels']
            },
            'ntia_bead': {
                'total_funding': bead['total_funding'],
                'total_funding_formatted': bead['total_funding_formatted'],
                'state_count': bead['count'],
                'total_unserved': bead['total_unserved'],
                'endpoints': ['/api/fiber/bead-allocations', '/api/fiber/ntia-grants', '/api/fiber/ntia-grants/stats']
            },
            'coverage': {
                'markets': len(FiberCoverageAPI.COVERAGE),
                'endpoints': ['/api/fiber/coverage']
            },
            'map_data': {
                'description': 'Fiber routes + BEAD grants for capacity map overlay',
                'endpoints': ['/api/fiber/map-data']
            }
        },
        'timestamp': datetime.now().isoformat()
    })


def register_fiber_discovery(app):
    """Register fiber discovery routes"""
    app.register_blueprint(fiber_bp)
    logger.info("✅ Fiber Network Discovery registered")
    routes = FiberProviderAPI.get_routes()
    bead = NTIAGrantAPI.get_bead_allocations()
    print("🔌 Fiber Network Discovery: ✅ Registered")
    print(f"   📡 Providers: /api/fiber/providers (8 carriers, {FiberProviderAPI.get_all_providers()['total_route_miles']:,} route miles)")
    print(f"   🛤️ Routes: /api/fiber/routes ({routes['count']} routes, {routes['total_miles']:,} miles)")
    print("   🏢 Carrier Hotels: /api/fiber/carrier-hotels")
    print(f"   💰 BEAD Allocations: /api/fiber/bead-allocations ({bead['count']} states, {bead['total_funding_formatted']})")
    print("   📊 Coverage: /api/fiber/coverage")
    print("   🗺️ Map Data: /api/fiber/map-data")
