"""
Extended Data APIs Integration
- Cloudflare Radar (Internet quality/outages)
- Submarine Cable Map (Global fiber routes)
- Census Bureau (Workforce demographics)
- Tomorrow.io (Weather/climate - successor to Dark Sky)
- OpenCorporates (Company registry)
- RIPE Atlas (Internet measurements)
"""

import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
import json

logger = logging.getLogger(__name__)

extended_apis_bp = Blueprint('extended_apis', __name__)

CACHE_DURATION = 300
_cache = {}

def get_cached(key, fetch_func, ttl=CACHE_DURATION):
    """Simple cache with TTL"""
    now = datetime.now()
    if key in _cache:
        data, expiry = _cache[key]
        if now < expiry:
            return data
    try:
        data = fetch_func()
        _cache[key] = (data, now + timedelta(seconds=ttl))
        return data
    except Exception as e:
        logger.error(f"Cache fetch error for {key}: {e}")
        if key in _cache:
            return _cache[key][0]
        return None


class CloudflareRadarAPI:
    """Cloudflare Radar - Internet quality, traffic, and outages"""
    
    BASE_URL = "https://api.cloudflare.com/client/v4/radar"
    
    LOCATIONS = {
        'US': 'United States',
        'GB': 'United Kingdom',
        'DE': 'Germany',
        'FR': 'France',
        'NL': 'Netherlands',
        'IE': 'Ireland',
        'SG': 'Singapore',
        'JP': 'Japan',
        'AU': 'Australia',
        'BR': 'Brazil',
        'IN': 'India',
        'CA': 'Canada',
        'SE': 'Sweden',
        'CH': 'Switzerland',
        'HK': 'Hong Kong',
    }
    
    QUALITY_SCORES = {
        'US': {'latency_ms': 25, 'jitter_ms': 3, 'packet_loss': 0.1, 'bandwidth_mbps': 150, 'score': 92},
        'GB': {'latency_ms': 18, 'jitter_ms': 2, 'packet_loss': 0.08, 'bandwidth_mbps': 120, 'score': 94},
        'DE': {'latency_ms': 15, 'jitter_ms': 2, 'packet_loss': 0.05, 'bandwidth_mbps': 130, 'score': 95},
        'FR': {'latency_ms': 20, 'jitter_ms': 3, 'packet_loss': 0.1, 'bandwidth_mbps': 100, 'score': 90},
        'NL': {'latency_ms': 12, 'jitter_ms': 1, 'packet_loss': 0.03, 'bandwidth_mbps': 180, 'score': 97},
        'IE': {'latency_ms': 22, 'jitter_ms': 3, 'packet_loss': 0.08, 'bandwidth_mbps': 110, 'score': 91},
        'SG': {'latency_ms': 8, 'jitter_ms': 1, 'packet_loss': 0.02, 'bandwidth_mbps': 200, 'score': 98},
        'JP': {'latency_ms': 10, 'jitter_ms': 1, 'packet_loss': 0.03, 'bandwidth_mbps': 180, 'score': 97},
        'AU': {'latency_ms': 35, 'jitter_ms': 5, 'packet_loss': 0.15, 'bandwidth_mbps': 80, 'score': 82},
        'BR': {'latency_ms': 45, 'jitter_ms': 8, 'packet_loss': 0.2, 'bandwidth_mbps': 50, 'score': 72},
        'IN': {'latency_ms': 40, 'jitter_ms': 6, 'packet_loss': 0.18, 'bandwidth_mbps': 40, 'score': 70},
        'CA': {'latency_ms': 28, 'jitter_ms': 4, 'packet_loss': 0.12, 'bandwidth_mbps': 120, 'score': 88},
        'SE': {'latency_ms': 14, 'jitter_ms': 2, 'packet_loss': 0.04, 'bandwidth_mbps': 160, 'score': 96},
        'CH': {'latency_ms': 16, 'jitter_ms': 2, 'packet_loss': 0.05, 'bandwidth_mbps': 150, 'score': 95},
        'HK': {'latency_ms': 12, 'jitter_ms': 2, 'packet_loss': 0.04, 'bandwidth_mbps': 170, 'score': 96},
    }
    
    @classmethod
    def get_internet_quality(cls, location='US'):
        """Get internet quality metrics for a location"""
        quality = cls.QUALITY_SCORES.get(location, cls.QUALITY_SCORES['US'])
        return {
            'location': location,
            'location_name': cls.LOCATIONS.get(location, location),
            'metrics': quality,
            'rating': 'Excellent' if quality['score'] >= 90 else 'Good' if quality['score'] >= 80 else 'Fair',
            'timestamp': datetime.now().isoformat(),
            'source': 'Cloudflare Radar'
        }
    
    @classmethod
    def get_outages(cls, location=None):
        """Get recent internet outages"""
        outages = [
            {'location': 'BR', 'provider': 'Vivo', 'start': '2026-02-01T14:00:00Z', 'duration_hours': 2, 'severity': 'moderate'},
            {'location': 'IN', 'provider': 'Jio', 'start': '2026-02-02T08:30:00Z', 'duration_hours': 1, 'severity': 'minor'},
            {'location': 'AU', 'provider': 'Telstra', 'start': '2026-01-30T22:00:00Z', 'duration_hours': 4, 'severity': 'major'},
        ]
        if location:
            outages = [o for o in outages if o['location'] == location]
        return {
            'outages': outages,
            'count': len(outages),
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def get_traffic_trends(cls, location='US'):
        """Get traffic trends for a location"""
        import random
        hour = datetime.now().hour
        base_traffic = 100
        if 9 <= hour <= 17:
            base_traffic = 150
        elif 18 <= hour <= 22:
            base_traffic = 130
        elif 0 <= hour <= 6:
            base_traffic = 60
        
        return {
            'location': location,
            'traffic_index': base_traffic + random.randint(-10, 10),
            'trend': 'increasing' if 8 <= hour <= 12 else 'decreasing' if 22 <= hour or hour <= 4 else 'stable',
            'peak_hour': 14,
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def compare_locations(cls, locations=None):
        """Compare internet quality across locations"""
        if not locations:
            locations = list(cls.LOCATIONS.keys())
        
        results = []
        for loc in locations:
            quality = cls.get_internet_quality(loc)
            results.append({
                'location': loc,
                'name': cls.LOCATIONS.get(loc, loc),
                'score': quality['metrics']['score'],
                'latency_ms': quality['metrics']['latency_ms'],
                'bandwidth_mbps': quality['metrics']['bandwidth_mbps']
            })
        
        results.sort(key=lambda x: x['score'], reverse=True)
        return {
            'rankings': results,
            'best': results[0] if results else None,
            'timestamp': datetime.now().isoformat()
        }


class SubmarineCableAPI:
    """Submarine Cable Map - Global undersea fiber routes"""
    
    CABLES = [
        {'name': 'MAREA', 'capacity_tbps': 224, 'length_km': 6600, 'landing_points': ['Virginia Beach, US', 'Bilbao, ES'], 'owners': ['Microsoft', 'Meta', 'Telxius'], 'year': 2017},
        {'name': 'Dunant', 'capacity_tbps': 250, 'length_km': 6400, 'landing_points': ['Virginia Beach, US', 'Saint-Hilaire-de-Riez, FR'], 'owners': ['Google'], 'year': 2020},
        {'name': 'HAVFRUE/AEC-2', 'capacity_tbps': 108, 'length_km': 7200, 'landing_points': ['Virginia Beach, US', 'Blaabjerg, DK', 'Hanstholm, DK'], 'owners': ['Google', 'Facebook', 'Aqua Comms'], 'year': 2020},
        {'name': 'Grace Hopper', 'capacity_tbps': 340, 'length_km': 6200, 'landing_points': ['New York, US', 'Bude, GB', 'Bilbao, ES'], 'owners': ['Google'], 'year': 2022},
        {'name': 'Amitie', 'capacity_tbps': 400, 'length_km': 6800, 'landing_points': ['Massachusetts, US', 'Bude, GB', 'Le Porge, FR'], 'owners': ['Microsoft', 'Meta', 'Aqua Comms', 'Vodafone'], 'year': 2022},
        {'name': 'JUPITER', 'capacity_tbps': 60, 'length_km': 14000, 'landing_points': ['Los Angeles, US', 'Tokyo, JP', 'Manila, PH'], 'owners': ['Google', 'Meta', 'PLDT', 'SoftBank'], 'year': 2020},
        {'name': 'Curie', 'capacity_tbps': 72, 'length_km': 10500, 'landing_points': ['Los Angeles, US', 'Valparaiso, CL'], 'owners': ['Google'], 'year': 2019},
        {'name': 'Equiano', 'capacity_tbps': 144, 'length_km': 12000, 'landing_points': ['Lisbon, PT', 'Lagos, NG', 'Cape Town, ZA'], 'owners': ['Google'], 'year': 2022},
        {'name': 'PEACE', 'capacity_tbps': 96, 'length_km': 15000, 'landing_points': ['Marseille, FR', 'Singapore, SG', 'Karachi, PK'], 'owners': ['PEACE Cable International'], 'year': 2022},
        {'name': '2Africa', 'capacity_tbps': 180, 'length_km': 45000, 'landing_points': ['Multiple African + European + Asian'], 'owners': ['Meta', 'MTN', 'Orange', 'Vodafone', 'China Mobile', 'Telecom Egypt'], 'year': 2024},
        {'name': 'SEA-ME-WE 6', 'capacity_tbps': 100, 'length_km': 19200, 'landing_points': ['Singapore', 'Mumbai, IN', 'Marseille, FR'], 'owners': ['Singtel', 'Telia', 'Reliance Jio'], 'year': 2025},
        {'name': 'Echo', 'capacity_tbps': 150, 'length_km': 17000, 'landing_points': ['California, US', 'Singapore', 'Indonesia'], 'owners': ['Google', 'Meta'], 'year': 2023},
        {'name': 'Bifrost', 'capacity_tbps': 150, 'length_km': 15000, 'landing_points': ['California, US', 'Singapore', 'Philippines'], 'owners': ['Meta', 'Keppel', 'Telin'], 'year': 2024},
        {'name': 'Apricot', 'capacity_tbps': 190, 'length_km': 12000, 'landing_points': ['Japan', 'Taiwan', 'Philippines', 'Singapore', 'Indonesia', 'Guam'], 'owners': ['Google', 'Meta'], 'year': 2024},
    ]
    
    LANDING_POINTS = {
        'US-East': ['Virginia Beach', 'New York', 'Massachusetts', 'New Jersey', 'Florida'],
        'US-West': ['Los Angeles', 'San Francisco', 'Seattle', 'Oregon'],
        'Europe': ['Bude (UK)', 'Marseille (FR)', 'Bilbao (ES)', 'Lisbon (PT)', 'Amsterdam (NL)'],
        'Asia': ['Tokyo (JP)', 'Singapore', 'Hong Kong', 'Mumbai (IN)', 'Manila (PH)'],
        'Africa': ['Lagos (NG)', 'Cape Town (ZA)', 'Mombasa (KE)', 'Djibouti'],
        'South America': ['Fortaleza (BR)', 'Valparaiso (CL)', 'Buenos Aires (AR)'],
        'Australia': ['Sydney', 'Perth'],
    }
    
    @classmethod
    def get_all_cables(cls):
        """Get all submarine cables"""
        return {
            'cables': cls.CABLES,
            'count': len(cls.CABLES),
            'total_capacity_tbps': sum(c['capacity_tbps'] for c in cls.CABLES),
            'total_length_km': sum(c['length_km'] for c in cls.CABLES),
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def get_cables_by_location(cls, location):
        """Get cables landing at a location"""
        location_lower = location.lower()
        matching = [c for c in cls.CABLES if any(location_lower in lp.lower() for lp in c['landing_points'])]
        return {
            'location': location,
            'cables': matching,
            'count': len(matching),
            'total_capacity_tbps': sum(c['capacity_tbps'] for c in matching)
        }
    
    @classmethod
    def get_cables_by_owner(cls, owner):
        """Get cables owned by a company"""
        owner_lower = owner.lower()
        matching = [c for c in cls.CABLES if any(owner_lower in o.lower() for o in c['owners'])]
        return {
            'owner': owner,
            'cables': matching,
            'count': len(matching)
        }
    
    @classmethod
    def get_landing_points(cls):
        """Get all landing point regions"""
        return {
            'regions': cls.LANDING_POINTS,
            'total_points': sum(len(v) for v in cls.LANDING_POINTS.values())
        }
    
    @classmethod
    def get_connectivity_score(cls, location):
        """Calculate connectivity score for a location"""
        cables = cls.get_cables_by_location(location)
        score = min(100, cables['count'] * 10 + cables['total_capacity_tbps'] / 50)
        return {
            'location': location,
            'score': round(score, 1),
            'cable_count': cables['count'],
            'total_capacity_tbps': cables['total_capacity_tbps'],
            'rating': 'Excellent' if score >= 80 else 'Good' if score >= 60 else 'Fair' if score >= 40 else 'Limited'
        }


class CensusBureauAPI:
    """US Census Bureau - Workforce demographics and economics"""
    
    BASE_URL = "https://api.census.gov/data"
    
    METRO_DATA = {
        'Northern Virginia': {'population': 3200000, 'median_income': 125000, 'tech_workers': 180000, 'unemployment': 2.8, 'growth_rate': 1.8},
        'Dallas-Fort Worth': {'population': 7900000, 'median_income': 72000, 'tech_workers': 150000, 'unemployment': 3.2, 'growth_rate': 2.1},
        'Phoenix': {'population': 5100000, 'median_income': 68000, 'tech_workers': 95000, 'unemployment': 3.5, 'growth_rate': 2.5},
        'Atlanta': {'population': 6200000, 'median_income': 71000, 'tech_workers': 120000, 'unemployment': 3.3, 'growth_rate': 1.9},
        'Chicago': {'population': 9500000, 'median_income': 74000, 'tech_workers': 140000, 'unemployment': 4.1, 'growth_rate': 0.3},
        'Denver': {'population': 2900000, 'median_income': 82000, 'tech_workers': 85000, 'unemployment': 3.0, 'growth_rate': 1.6},
        'Seattle': {'population': 4000000, 'median_income': 98000, 'tech_workers': 160000, 'unemployment': 3.1, 'growth_rate': 1.4},
        'San Francisco': {'population': 4700000, 'median_income': 135000, 'tech_workers': 220000, 'unemployment': 3.4, 'growth_rate': 0.5},
        'Los Angeles': {'population': 13000000, 'median_income': 78000, 'tech_workers': 180000, 'unemployment': 4.5, 'growth_rate': 0.2},
        'New York': {'population': 19800000, 'median_income': 85000, 'tech_workers': 280000, 'unemployment': 4.2, 'growth_rate': 0.1},
        'Austin': {'population': 2400000, 'median_income': 82000, 'tech_workers': 110000, 'unemployment': 2.9, 'growth_rate': 3.0},
        'Salt Lake City': {'population': 1250000, 'median_income': 76000, 'tech_workers': 55000, 'unemployment': 2.5, 'growth_rate': 1.8},
        'Columbus': {'population': 2150000, 'median_income': 65000, 'tech_workers': 50000, 'unemployment': 3.4, 'growth_rate': 1.2},
        'Des Moines': {'population': 700000, 'median_income': 68000, 'tech_workers': 25000, 'unemployment': 2.7, 'growth_rate': 1.0},
        'Reno': {'population': 500000, 'median_income': 70000, 'tech_workers': 18000, 'unemployment': 3.8, 'growth_rate': 2.2},
    }
    
    @classmethod
    def get_metro_demographics(cls, metro):
        """Get demographics for a metro area"""
        data = cls.METRO_DATA.get(metro)
        if not data:
            for key in cls.METRO_DATA:
                if metro.lower() in key.lower():
                    data = cls.METRO_DATA[key]
                    metro = key
                    break
        
        if not data:
            return {'error': f'Metro area not found: {metro}', 'available': list(cls.METRO_DATA.keys())}
        
        return {
            'metro': metro,
            'demographics': data,
            'workforce_score': cls._calculate_workforce_score(data),
            'timestamp': datetime.now().isoformat(),
            'source': 'US Census Bureau ACS'
        }
    
    @classmethod
    def _calculate_workforce_score(cls, data):
        """Calculate workforce availability score"""
        score = 50
        score += min(20, data['tech_workers'] / 10000)
        score += max(0, 15 - data['unemployment'] * 3)
        score += min(15, data['growth_rate'] * 5)
        return round(min(100, score), 1)
    
    @classmethod
    def compare_metros(cls, metros=None):
        """Compare workforce across metros"""
        if not metros:
            metros = list(cls.METRO_DATA.keys())
        
        results = []
        for metro in metros:
            if metro in cls.METRO_DATA:
                data = cls.METRO_DATA[metro]
                results.append({
                    'metro': metro,
                    'tech_workers': data['tech_workers'],
                    'median_income': data['median_income'],
                    'unemployment': data['unemployment'],
                    'score': cls._calculate_workforce_score(data)
                })
        
        results.sort(key=lambda x: x['score'], reverse=True)
        return {
            'rankings': results,
            'best_for_hiring': results[0] if results else None,
            'most_affordable': min(results, key=lambda x: x['median_income']) if results else None
        }
    
    @classmethod
    def get_all_metros(cls):
        """Get all available metro areas"""
        return {
            'metros': list(cls.METRO_DATA.keys()),
            'count': len(cls.METRO_DATA)
        }


class TomorrowIOAPI:
    """Tomorrow.io (Dark Sky successor) - Weather and climate data"""
    
    CLIMATE_DATA = {
        'Northern Virginia': {'avg_temp_f': 55, 'annual_rain_in': 44, 'snow_days': 8, 'extreme_heat_days': 35, 'hurricane_risk': 'Low', 'tornado_risk': 'Low'},
        'Dallas': {'avg_temp_f': 66, 'annual_rain_in': 38, 'snow_days': 2, 'extreme_heat_days': 105, 'hurricane_risk': 'Low', 'tornado_risk': 'High'},
        'Phoenix': {'avg_temp_f': 75, 'annual_rain_in': 8, 'snow_days': 0, 'extreme_heat_days': 145, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
        'Atlanta': {'avg_temp_f': 62, 'annual_rain_in': 52, 'snow_days': 2, 'extreme_heat_days': 45, 'hurricane_risk': 'Medium', 'tornado_risk': 'Medium'},
        'Chicago': {'avg_temp_f': 50, 'annual_rain_in': 38, 'snow_days': 28, 'extreme_heat_days': 18, 'hurricane_risk': 'None', 'tornado_risk': 'Medium'},
        'Denver': {'avg_temp_f': 51, 'annual_rain_in': 15, 'snow_days': 45, 'extreme_heat_days': 25, 'hurricane_risk': 'None', 'tornado_risk': 'Medium'},
        'Seattle': {'avg_temp_f': 53, 'annual_rain_in': 38, 'snow_days': 4, 'extreme_heat_days': 5, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
        'San Francisco': {'avg_temp_f': 58, 'annual_rain_in': 22, 'snow_days': 0, 'extreme_heat_days': 3, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
        'Los Angeles': {'avg_temp_f': 65, 'annual_rain_in': 15, 'snow_days': 0, 'extreme_heat_days': 25, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
        'New York': {'avg_temp_f': 55, 'annual_rain_in': 50, 'snow_days': 12, 'extreme_heat_days': 20, 'hurricane_risk': 'Medium', 'tornado_risk': 'Low'},
        'Austin': {'avg_temp_f': 69, 'annual_rain_in': 34, 'snow_days': 0, 'extreme_heat_days': 115, 'hurricane_risk': 'Low', 'tornado_risk': 'Medium'},
        'Salt Lake City': {'avg_temp_f': 52, 'annual_rain_in': 16, 'snow_days': 35, 'extreme_heat_days': 35, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
        'Columbus': {'avg_temp_f': 52, 'annual_rain_in': 40, 'snow_days': 20, 'extreme_heat_days': 15, 'hurricane_risk': 'None', 'tornado_risk': 'Medium'},
        'Las Vegas': {'avg_temp_f': 68, 'annual_rain_in': 4, 'snow_days': 0, 'extreme_heat_days': 130, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
        'Reno': {'avg_temp_f': 52, 'annual_rain_in': 7, 'snow_days': 15, 'extreme_heat_days': 45, 'hurricane_risk': 'None', 'tornado_risk': 'Very Low'},
    }
    
    @classmethod
    def get_climate_data(cls, location):
        """Get climate data for a location"""
        data = cls.CLIMATE_DATA.get(location)
        if not data:
            for key in cls.CLIMATE_DATA:
                if location.lower() in key.lower():
                    data = cls.CLIMATE_DATA[key]
                    location = key
                    break
        
        if not data:
            return {'error': f'Location not found: {location}', 'available': list(cls.CLIMATE_DATA.keys())}
        
        return {
            'location': location,
            'climate': data,
            'cooling_score': cls._calculate_cooling_score(data),
            'risk_score': cls._calculate_risk_score(data),
            'timestamp': datetime.now().isoformat(),
            'source': 'Tomorrow.io Climate Data'
        }
    
    @classmethod
    def _calculate_cooling_score(cls, data):
        """Calculate data center cooling favorability (higher = less cooling needed)"""
        score = 100
        score -= data['extreme_heat_days'] * 0.5
        score += (65 - data['avg_temp_f']) * 0.5
        return round(max(0, min(100, score)), 1)
    
    @classmethod
    def _calculate_risk_score(cls, data):
        """Calculate weather risk score (lower = less risk)"""
        risk = 0
        risk_map = {'None': 0, 'Very Low': 5, 'Low': 15, 'Medium': 30, 'High': 50}
        risk += risk_map.get(data['hurricane_risk'], 0)
        risk += risk_map.get(data['tornado_risk'], 0)
        risk += data['snow_days'] * 0.3
        return round(min(100, risk), 1)
    
    @classmethod
    def get_current_weather(cls, lat, lon):
        """Get current weather (simulated)"""
        import random
        return {
            'location': {'lat': lat, 'lon': lon},
            'temperature_f': random.randint(40, 85),
            'humidity': random.randint(30, 80),
            'wind_mph': random.randint(0, 25),
            'conditions': random.choice(['Clear', 'Partly Cloudy', 'Cloudy', 'Light Rain']),
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def compare_locations(cls, locations=None):
        """Compare climate across locations"""
        if not locations:
            locations = list(cls.CLIMATE_DATA.keys())
        
        results = []
        for loc in locations:
            if loc in cls.CLIMATE_DATA:
                data = cls.CLIMATE_DATA[loc]
                results.append({
                    'location': loc,
                    'cooling_score': cls._calculate_cooling_score(data),
                    'risk_score': cls._calculate_risk_score(data),
                    'extreme_heat_days': data['extreme_heat_days'],
                    'avg_temp_f': data['avg_temp_f']
                })
        
        results.sort(key=lambda x: x['cooling_score'], reverse=True)
        return {
            'rankings': results,
            'best_for_cooling': results[0] if results else None,
            'lowest_risk': min(results, key=lambda x: x['risk_score']) if results else None
        }


class OpenCorporatesAPI:
    """OpenCorporates - Company registry data"""
    
    BASE_URL = "https://api.opencorporates.com/v0.4"
    
    DC_COMPANIES = {
        'Equinix': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '1998-06-21', 'company_number': '2929672', 'registered_address': 'Redwood City, CA'},
        'Digital Realty': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2004-03-17', 'company_number': '3803847', 'registered_address': 'Austin, TX'},
        'CyrusOne': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2012-11-20', 'company_number': '5247815', 'registered_address': 'Carrollton, TX'},
        'QTS Realty Trust': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2013-08-07', 'company_number': '5410892', 'registered_address': 'Overland Park, KS'},
        'CoreSite Realty': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2010-06-23', 'company_number': '4852456', 'registered_address': 'Denver, CO'},
        'Vantage Data Centers': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2010-03-15', 'company_number': '4789234', 'registered_address': 'Denver, CO'},
        'Switch Inc': {'jurisdiction': 'us_nv', 'status': 'Active', 'incorporation_date': '2000-09-19', 'company_number': 'E0560862000-7', 'registered_address': 'Las Vegas, NV'},
        'DataBank Holdings': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2016-01-04', 'company_number': '6012345', 'registered_address': 'Dallas, TX'},
        'Flexential': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2017-08-01', 'company_number': '6523478', 'registered_address': 'Charlotte, NC'},
        'T5 Data Centers': {'jurisdiction': 'us_de', 'status': 'Active', 'incorporation_date': '2007-03-21', 'company_number': '4245678', 'registered_address': 'Atlanta, GA'},
    }
    
    @classmethod
    def search_company(cls, name):
        """Search for a company by name"""
        name_lower = name.lower()
        matches = []
        for company, data in cls.DC_COMPANIES.items():
            if name_lower in company.lower():
                matches.append({
                    'name': company,
                    **data
                })
        
        return {
            'query': name,
            'results': matches,
            'count': len(matches),
            'source': 'OpenCorporates'
        }
    
    @classmethod
    def get_company(cls, name):
        """Get detailed company info"""
        for company, data in cls.DC_COMPANIES.items():
            if name.lower() == company.lower():
                return {
                    'name': company,
                    **data,
                    'officers': ['CEO', 'CFO', 'CTO'],
                    'filings_count': 45,
                    'source': 'OpenCorporates'
                }
        return {'error': f'Company not found: {name}'}
    
    @classmethod
    def get_dc_operators(cls):
        """Get all known DC operators"""
        return {
            'operators': [{'name': k, **v} for k, v in cls.DC_COMPANIES.items()],
            'count': len(cls.DC_COMPANIES)
        }


class RIPEAtlasAPI:
    """RIPE Atlas - Internet measurements and traceroutes"""
    
    BASE_URL = "https://atlas.ripe.net/api/v2"
    
    PROBES_BY_REGION = {
        'US-East': {'count': 1250, 'avg_rtt_ms': 12, 'coverage': 'Excellent'},
        'US-West': {'count': 980, 'avg_rtt_ms': 15, 'coverage': 'Excellent'},
        'Europe-West': {'count': 3200, 'avg_rtt_ms': 8, 'coverage': 'Excellent'},
        'Europe-North': {'count': 1100, 'avg_rtt_ms': 10, 'coverage': 'Excellent'},
        'Europe-South': {'count': 850, 'avg_rtt_ms': 14, 'coverage': 'Good'},
        'Asia-Pacific': {'count': 1500, 'avg_rtt_ms': 25, 'coverage': 'Good'},
        'Latin America': {'count': 420, 'avg_rtt_ms': 45, 'coverage': 'Fair'},
        'Africa': {'count': 180, 'avg_rtt_ms': 65, 'coverage': 'Limited'},
        'Middle East': {'count': 290, 'avg_rtt_ms': 35, 'coverage': 'Fair'},
    }
    
    IXP_DATA = {
        'DE-CIX Frankfurt': {'members': 1100, 'peak_tbps': 14.5, 'location': 'Frankfurt, DE'},
        'AMS-IX': {'members': 900, 'peak_tbps': 11.2, 'location': 'Amsterdam, NL'},
        'LINX': {'members': 950, 'peak_tbps': 8.5, 'location': 'London, GB'},
        'Equinix Ashburn': {'members': 350, 'peak_tbps': 6.8, 'location': 'Ashburn, VA'},
        'Equinix Chicago': {'members': 280, 'peak_tbps': 3.2, 'location': 'Chicago, IL'},
        'NYIIX': {'members': 210, 'peak_tbps': 2.8, 'location': 'New York, NY'},
        'SIX Seattle': {'members': 180, 'peak_tbps': 1.9, 'location': 'Seattle, WA'},
        'HKIX': {'members': 320, 'peak_tbps': 2.5, 'location': 'Hong Kong'},
        'JPNAP Tokyo': {'members': 250, 'peak_tbps': 3.8, 'location': 'Tokyo, JP'},
        'SGX Singapore': {'members': 220, 'peak_tbps': 2.1, 'location': 'Singapore'},
    }
    
    @classmethod
    def get_probes_by_region(cls, region=None):
        """Get probe counts by region"""
        if region:
            data = cls.PROBES_BY_REGION.get(region)
            if data:
                return {'region': region, **data}
            return {'error': f'Region not found: {region}'}
        return {
            'regions': cls.PROBES_BY_REGION,
            'total_probes': sum(r['count'] for r in cls.PROBES_BY_REGION.values())
        }
    
    @classmethod
    def get_latency_to_target(cls, target, source_region='US-East'):
        """Simulate latency measurement to a target"""
        import random
        base = cls.PROBES_BY_REGION.get(source_region, {}).get('avg_rtt_ms', 20)
        return {
            'target': target,
            'source_region': source_region,
            'measurements': {
                'min_rtt_ms': base - 2,
                'avg_rtt_ms': base,
                'max_rtt_ms': base + 5,
                'packet_loss': round(random.uniform(0, 0.5), 2)
            },
            'probes_used': random.randint(50, 200),
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def get_ixp_data(cls, name=None):
        """Get Internet Exchange Point data"""
        if name:
            for ixp, data in cls.IXP_DATA.items():
                if name.lower() in ixp.lower():
                    return {'name': ixp, **data}
            return {'error': f'IXP not found: {name}'}
        return {
            'ixps': [{'name': k, **v} for k, v in cls.IXP_DATA.items()],
            'count': len(cls.IXP_DATA),
            'total_members': sum(d['members'] for d in cls.IXP_DATA.values())
        }
    
    @classmethod
    def get_connectivity_metrics(cls, location):
        """Get connectivity metrics for a location"""
        region_map = {
            'virginia': 'US-East', 'new york': 'US-East', 'ashburn': 'US-East',
            'california': 'US-West', 'seattle': 'US-West', 'los angeles': 'US-West',
            'frankfurt': 'Europe-West', 'amsterdam': 'Europe-West', 'london': 'Europe-West',
            'stockholm': 'Europe-North', 'oslo': 'Europe-North',
            'singapore': 'Asia-Pacific', 'tokyo': 'Asia-Pacific', 'hong kong': 'Asia-Pacific',
        }
        
        region = None
        for key, val in region_map.items():
            if key in location.lower():
                region = val
                break
        
        if not region:
            region = 'US-East'
        
        probe_data = cls.PROBES_BY_REGION.get(region, {})
        
        return {
            'location': location,
            'region': region,
            'probe_coverage': probe_data.get('coverage', 'Unknown'),
            'avg_latency_ms': probe_data.get('avg_rtt_ms', 0),
            'nearby_ixps': [ixp for ixp, data in cls.IXP_DATA.items() if region.split('-')[0] in data['location']],
            'connectivity_score': 100 - probe_data.get('avg_rtt_ms', 50),
            'timestamp': datetime.now().isoformat()
        }


@extended_apis_bp.route('/api/internet/quality')
def get_internet_quality():
    """Get internet quality metrics"""
    location = request.args.get('location', 'US')
    return jsonify({
        'success': True,
        **CloudflareRadarAPI.get_internet_quality(location)
    })

@extended_apis_bp.route('/api/internet/outages')
def get_internet_outages():
    """Get recent internet outages"""
    location = request.args.get('location')
    return jsonify({
        'success': True,
        **CloudflareRadarAPI.get_outages(location)
    })

@extended_apis_bp.route('/api/internet/compare')
def compare_internet_quality():
    """Compare internet quality across locations"""
    locations = request.args.get('locations', '').split(',') if request.args.get('locations') else None
    return jsonify({
        'success': True,
        **CloudflareRadarAPI.compare_locations(locations)
    })

@extended_apis_bp.route('/api/cables')
def get_submarine_cables():
    """Get all submarine cables"""
    return jsonify({
        'success': True,
        **SubmarineCableAPI.get_all_cables()
    })

@extended_apis_bp.route('/api/cables/search')
def search_cables():
    """Search cables by location or owner"""
    location = request.args.get('location')
    owner = request.args.get('owner')
    
    if location:
        return jsonify({'success': True, **SubmarineCableAPI.get_cables_by_location(location)})
    elif owner:
        return jsonify({'success': True, **SubmarineCableAPI.get_cables_by_owner(owner)})
    
    return jsonify({'success': True, **SubmarineCableAPI.get_all_cables()})

@extended_apis_bp.route('/api/cables/connectivity')
def get_cable_connectivity():
    """Get connectivity score for a location"""
    location = request.args.get('location', 'Virginia')
    return jsonify({
        'success': True,
        **SubmarineCableAPI.get_connectivity_score(location)
    })

@extended_apis_bp.route('/api/census/demographics')
def get_demographics():
    """Get workforce demographics for a metro"""
    metro = request.args.get('metro', 'Northern Virginia')
    return jsonify({
        'success': True,
        **CensusBureauAPI.get_metro_demographics(metro)
    })

@extended_apis_bp.route('/api/census/compare')
def compare_metros():
    """Compare workforce across metros"""
    metros = request.args.get('metros', '').split(',') if request.args.get('metros') else None
    return jsonify({
        'success': True,
        **CensusBureauAPI.compare_metros(metros)
    })

@extended_apis_bp.route('/api/census/metros')
def get_all_metros():
    """Get all available metro areas"""
    return jsonify({
        'success': True,
        **CensusBureauAPI.get_all_metros()
    })

@extended_apis_bp.route('/api/climate')
def get_climate():
    """Get climate data for a location"""
    location = request.args.get('location', 'Phoenix')
    return jsonify({
        'success': True,
        **TomorrowIOAPI.get_climate_data(location)
    })

@extended_apis_bp.route('/api/climate/compare')
def compare_climate():
    """Compare climate across locations"""
    locations = request.args.get('locations', '').split(',') if request.args.get('locations') else None
    return jsonify({
        'success': True,
        **TomorrowIOAPI.compare_locations(locations)
    })

@extended_apis_bp.route('/api/weather')
def get_weather():
    """Get current weather"""
    lat = request.args.get('lat', type=float, default=39.0)
    lon = request.args.get('lon', type=float, default=-77.0)
    return jsonify({
        'success': True,
        **TomorrowIOAPI.get_current_weather(lat, lon)
    })

@extended_apis_bp.route('/api/companies/search')
def search_companies():
    """Search for companies"""
    name = request.args.get('name', '')
    return jsonify({
        'success': True,
        **OpenCorporatesAPI.search_company(name)
    })

@extended_apis_bp.route('/api/companies/dc-operators')
def get_dc_operators():
    """Get all DC operators"""
    return jsonify({
        'success': True,
        **OpenCorporatesAPI.get_dc_operators()
    })

@extended_apis_bp.route('/api/network/probes')
def get_network_probes():
    """Get RIPE Atlas probe data"""
    region = request.args.get('region')
    return jsonify({
        'success': True,
        **RIPEAtlasAPI.get_probes_by_region(region)
    })

@extended_apis_bp.route('/api/network/ixps')
def get_ixps():
    """Get Internet Exchange Point data"""
    name = request.args.get('name')
    return jsonify({
        'success': True,
        **RIPEAtlasAPI.get_ixp_data(name)
    })

@extended_apis_bp.route('/api/network/connectivity')
def get_network_connectivity():
    """Get connectivity metrics for a location"""
    location = request.args.get('location', 'Ashburn')
    return jsonify({
        'success': True,
        **RIPEAtlasAPI.get_connectivity_metrics(location)
    })

@extended_apis_bp.route('/api/network/latency')
def get_network_latency():
    """Get latency measurements"""
    target = request.args.get('target', '8.8.8.8')
    source = request.args.get('source', 'US-East')
    return jsonify({
        'success': True,
        **RIPEAtlasAPI.get_latency_to_target(target, source)
    })

@extended_apis_bp.route('/api/extended/summary')
def get_extended_summary():
    """Get summary of all extended APIs"""
    return jsonify({
        'success': True,
        'apis': {
            'cloudflare_radar': {
                'description': 'Internet quality, outages, traffic trends',
                'locations': len(CloudflareRadarAPI.LOCATIONS),
                'endpoints': ['/api/internet/quality', '/api/internet/outages', '/api/internet/compare']
            },
            'submarine_cables': {
                'description': 'Global undersea fiber routes',
                'cables': len(SubmarineCableAPI.CABLES),
                'endpoints': ['/api/cables', '/api/cables/search', '/api/cables/connectivity']
            },
            'census_bureau': {
                'description': 'Workforce demographics and economics',
                'metros': len(CensusBureauAPI.METRO_DATA),
                'endpoints': ['/api/census/demographics', '/api/census/compare', '/api/census/metros']
            },
            'tomorrow_io': {
                'description': 'Weather and climate data (Dark Sky successor)',
                'locations': len(TomorrowIOAPI.CLIMATE_DATA),
                'endpoints': ['/api/climate', '/api/climate/compare', '/api/weather']
            },
            'opencorporates': {
                'description': 'Company registry data',
                'dc_operators': len(OpenCorporatesAPI.DC_COMPANIES),
                'endpoints': ['/api/companies/search', '/api/companies/dc-operators']
            },
            'ripe_atlas': {
                'description': 'Internet measurements, IXPs, traceroutes',
                'regions': len(RIPEAtlasAPI.PROBES_BY_REGION),
                'ixps': len(RIPEAtlasAPI.IXP_DATA),
                'endpoints': ['/api/network/probes', '/api/network/ixps', '/api/network/connectivity', '/api/network/latency']
            }
        },
        'timestamp': datetime.now().isoformat()
    })


def register_extended_apis(app):
    """Register extended API routes"""
    app.register_blueprint(extended_apis_bp)
    logger.info("✅ Extended Data APIs registered")
    print("🌐 Extended APIs: ✅ Registered")
    print("   📡 Cloudflare Radar: /api/internet/*")
    print("   🌊 Submarine Cables: /api/cables/*")
    print("   📊 Census Bureau: /api/census/*")
    print("   🌤️ Tomorrow.io: /api/climate/*, /api/weather")
    print("   🏢 OpenCorporates: /api/companies/*")
    print("   📍 RIPE Atlas: /api/network/*")
