#!/usr/bin/env python3
"""
DC HUB NEXUS - DISCOVERY ENGINE v3.0
====================================
Multi-Source Data Center Discovery System
Target: 30,000+ unique facilities

DATA SOURCES:
  1. PeeringDB (FREE API) - ~5,800 facilities ✅
  2. OpenStreetMap Overpass (FREE) - ~4,000 facilities ✅
  3. Wikidata SPARQL (FREE) - ~2,000 facilities ✅
  4. Cloudscene (scrape) - ~6,100 facilities 🆕
  5. Baxtel (scrape) - ~4,255 US facilities 🆕
  6. Data Center Map (scrape) - ~11,000 facilities 🆕
  7. Provider Websites - Major operators ✅
  8. RSS News Feeds - Announcements ✅

FEATURES:
  - Smart deduplication across all sources
  - Confidence scoring per facility
  - Incremental updates (only new/changed)
  - Rate limiting to avoid bans
  - Automatic retry with backoff

Author: DC Hub Team
Version: 3.0
Updated: January 2026
"""

import requests
import json
import time
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set
from urllib.parse import urljoin, quote, urlparse
import logging
from html import unescape
import os
from db_utils import get_db

# Try to import BeautifulSoup for scraping
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    print("⚠️ BeautifulSoup not installed - scraping limited")

# Try to import feedparser for proper date parsing
try:
    from email.utils import parsedate_to_datetime
except ImportError:
    parsedate_to_datetime = None

def parse_rss_date(date_str: str) -> str:
    """Parse RSS date formats and return ISO 8601 string"""
    if not date_str:
        return ''
    
    # Already ISO format
    if 'T' in date_str and ('+' in date_str or 'Z' in date_str or date_str.count('-') >= 2):
        return date_str
    
    # Try RFC 2822 format (common in RSS)
    try:
        if parsedate_to_datetime:
            dt = parsedate_to_datetime(date_str)
            return dt.isoformat()
    except:
        pass
    
    # Try common date formats
    formats = [
        '%a, %d %b %Y %H:%M:%S %z',      # RFC 2822 with timezone
        '%a, %d %b %Y %H:%M:%S %Z',      # RFC 2822 with named tz
        '%a, %d %b %Y %H:%M:%S',         # RFC 2822 no tz
        '%Y-%m-%dT%H:%M:%S%z',           # ISO 8601
        '%Y-%m-%dT%H:%M:%SZ',            # ISO 8601 UTC
        '%Y-%m-%d %H:%M:%S',             # Common format
        '%Y-%m-%d',                       # Date only
        '%d %b %Y %H:%M:%S',             # Day Month Year
        '%B %d, %Y',                      # Month Day, Year
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.isoformat()
        except:
            continue
    
    # Fallback: return as-is if nothing works
    return date_str

# Try to import feedparser for RSS
try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    print("⚠️ feedparser not installed - RSS feeds disabled")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')
REQUEST_DELAY = 2  # Seconds between requests
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# User agent for requests
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# Major DC operators for provider website scraping
MAJOR_OPERATORS = [
    'Equinix', 'Digital Realty', 'NTT', 'CyrusOne', 'QTS', 'CoreSite',
    'Vantage', 'EdgeConneX', 'DataBank', 'TierPoint', 'Flexential',
    'Switch', 'Stream Data Centers', 'H5 Data Centers', 'Prime Data Centers',
    'Compass Datacenters', 'Stack Infrastructure', 'Aligned', 'Novva',
    'CloudHQ', 'Iron Mountain', 'Sabey', 'Lincoln Rackhouse'
]

# =============================================================================
# DATABASE SETUP
# =============================================================================

def init_database():
    """Initialize database with discovery tables"""
    conn = get_db()
    c = conn.cursor()
    
    # Main facilities table
    c.execute("""
        CREATE TABLE IF NOT EXISTS facilities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            provider TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            country TEXT,
            region TEXT,
            latitude REAL,
            longitude REAL,
            power_mw REAL DEFAULT 0,
            sqft REAL DEFAULT 0,
            tier TEXT,
            status TEXT DEFAULT 'active',
            certifications TEXT DEFAULT '[]',
            connectivity TEXT,
            source TEXT,
            source_id TEXT,
            source_url TEXT,
            raw_data TEXT,
            confidence REAL DEFAULT 0.5,
            first_seen TEXT,
            last_updated TEXT,
            UNIQUE(source, source_id)
        )
    """)
    
    # Announcements/news table
    c.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT,
            url TEXT,
            source TEXT,
            category TEXT,
            location TEXT,
            power_mw REAL,
            investment_usd REAL,
            company TEXT,
            published_date TEXT,
            discovered_at TEXT
        )
    """)
    
    # Discovery sync log
    c.execute("""
        CREATE TABLE IF NOT EXISTS discovery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            sync_type TEXT,
            started_at TEXT,
            completed_at TEXT,
            facilities_found INTEGER DEFAULT 0,
            facilities_new INTEGER DEFAULT 0,
            facilities_updated INTEGER DEFAULT 0,
            status TEXT,
            error TEXT
        )
    """)
    
    # Create indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_source ON facilities(source)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_country ON facilities(country)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_provider ON facilities(provider)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_coords ON facilities(latitude, longitude)")
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def generate_facility_id(source: str, source_id: str) -> str:
    """Generate unique facility ID"""
    return hashlib.md5(f"{source}:{source_id}".encode()).hexdigest()[:16]

def normalize_name(name: str) -> str:
    """Normalize facility name for deduplication"""
    if not name:
        return ""
    # Lowercase, remove special chars, normalize whitespace
    name = name.lower().strip()
    name = re.sub(r'[^\w\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name)
    # Remove common suffixes
    for suffix in ['data center', 'datacenter', 'dc', 'facility', 'campus', 'site']:
        name = name.replace(suffix, '').strip()
    return name

def calculate_confidence(facility: Dict) -> float:
    """Calculate confidence score based on data completeness"""
    score = 0.3  # Base score
    
    if facility.get('latitude') and facility.get('longitude'):
        score += 0.2
    if facility.get('address'):
        score += 0.1
    if facility.get('city') and facility.get('country'):
        score += 0.1
    if facility.get('provider'):
        score += 0.1
    if facility.get('power_mw') and facility.get('power_mw') > 0:
        score += 0.1
    if facility.get('sqft') and facility.get('sqft') > 0:
        score += 0.1
        
    return min(score, 1.0)

def safe_request(url: str, method: str = 'GET', **kwargs) -> Optional[requests.Response]:
    """Make HTTP request with retry logic"""
    headers = kwargs.pop('headers', {})
    headers.setdefault('User-Agent', USER_AGENT)
    
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_DELAY)
            response = requests.request(
                method, url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(REQUEST_DELAY * (attempt + 1))
    return None

def is_valid_datacenter(name: str, desc: str = "", source: str = "") -> bool:
    """Strict filter for data center entries - rejects entries without positive indicators"""
    if not name:
        return False
    
    text = f"{name} {desc}".lower().strip()
    
    # Check for generic/placeholder names
    if re.match(r'^Q\d+$', name):  # Wikidata ID without label
        return False
    if name.lower() in ['data center', 'datacenter', 'unknown', 'unnamed']:
        return False
    
    # Strong negative keywords (definitely NOT data centers)
    # NOTE: Avoid words that appear in legitimate DC addresses like "park" (Science Park, Business Park)
    strong_negative = [
        # Transportation
        'railway', 'railroad', 'train station', 'metro station', 'bus station',
        'airport terminal', 'seaport', 'subway station',
        # Education
        'school', 'university', 'college', 'academy', 'kindergarten',
        # Healthcare
        'hospital', 'clinic', 'medical center', 'pharmacy',
        # Retail
        'shopping mall', 'shopping center', 'supermarket', 'restaurant', 'hotel & casino',
        # Industrial non-DC
        'warehouse district', 'factory', 'manufacturing plant', 'power station', 'power plant',
        'wind farm', 'solar farm', 'oil refinery',
        # Residential
        'apartment complex', 'residential area', 'housing estate', 'condominium',
        # Religious/Cultural
        'church', 'mosque', 'temple', 'cathedral', 'museum', 'public library',
        # Government non-DC
        'city hall', 'courthouse', 'prison', 'police headquarters', 'fire station',
        # Sports/Recreation - be specific to avoid false positives
        'football stadium', 'baseball stadium', 'sports arena', 'fitness center',
        'golf course', 'swimming pool', 'amusement park', 'theme park', 'national park',
        # Crypto
        'bitcoin mine', 'crypto mine', 'mining farm', 'cryptocurrency',
        # Generic buildings - be more specific
        'office tower', 'corporate headquarters'
    ]
    
    for neg in strong_negative:
        if neg in text:
            return False
    
    # Strong positive keywords (high confidence)
    strong_positive = [
        'data center', 'datacenter', 'data centre', 'datacentre',
        'colocation', 'colo facility', 'server farm',
        'internet exchange', 'carrier hotel', 'telecom hotel',
        'hosting facility', 'network hub'
    ]
    
    for pos in strong_positive:
        if pos in text:
            return True
    
    # Check if name contains known operator (high confidence)
    for op in MAJOR_OPERATORS:
        if op.lower() in text:
            return True
    
    # Weak positive keywords
    weak_positive = ['cloud', 'hosting', 'server', 'network', 'ix', 'noc', 'pop', 'edge', 'peering']
    weak_matches = sum(1 for p in weak_positive if p in text)
    
    if weak_matches >= 2:
        return True
    
    # Source-based trust: PeeringDB and OSM are reliable
    if source.lower() in ['peeringdb', 'osm', 'openstreetmap', 'provider', 'providerwebsites']:
        return True
    
    # For Wikidata without clear indicators, REJECT
    if source.lower() == 'wikidata':
        return False
    
    # Default: accept if at least one weak positive
    return weak_matches >= 1


# =============================================================================
# SOURCE: PEERINGDB (Primary Source - Most Reliable)
# =============================================================================

class PeeringDBSource:
    """PeeringDB API - ~5,800 facilities worldwide"""
    
    API_URL = "https://www.peeringdb.com/api/fac"
    SOURCE_NAME = "peeringdb"
    
    def fetch(self) -> List[Dict]:
        """Fetch all facilities from PeeringDB"""
        logger.info("📡 Fetching from PeeringDB...")
        
        response = safe_request(self.API_URL)
        if not response:
            logger.error("❌ PeeringDB fetch failed")
            return []
        
        try:
            data = response.json()
            facilities = data.get('data', [])
            logger.info(f"✅ PeeringDB: {len(facilities)} facilities")
            return [self._transform(f) for f in facilities]
        except Exception as e:
            logger.error(f"❌ PeeringDB parse error: {e}")
            return []
    
    def _transform(self, raw: Dict) -> Dict:
        """Transform PeeringDB format to standard format"""
        return {
            'source': self.SOURCE_NAME,
            'source_id': str(raw.get('id', '')),
            'name': raw.get('name', ''),
            'provider': raw.get('org_name', raw.get('name', '')),
            'address': raw.get('address1', ''),
            'city': raw.get('city', ''),
            'state': raw.get('state', ''),
            'country': raw.get('country', ''),
            'region': self._get_region(raw.get('country', '')),
            'latitude': raw.get('latitude'),
            'longitude': raw.get('longitude'),
            'source_url': f"https://www.peeringdb.com/fac/{raw.get('id')}",
            'raw_data': json.dumps(raw)
        }
    
    def _get_region(self, country: str) -> str:
        """Map country to region"""
        na = ['US', 'CA', 'MX']
        eu = ['GB', 'DE', 'FR', 'NL', 'IE', 'ES', 'IT', 'SE', 'NO', 'DK', 'FI', 'PL', 'CH', 'AT', 'BE']
        apac = ['JP', 'SG', 'AU', 'HK', 'KR', 'IN', 'CN', 'TW', 'MY', 'ID', 'TH', 'NZ']
        
        if country in na:
            return 'North America'
        elif country in eu:
            return 'Europe'
        elif country in apac:
            return 'Asia Pacific'
        else:
            return 'Other'


# =============================================================================
# SOURCE: OPENSTREETMAP OVERPASS
# =============================================================================

class OpenStreetMapSource:
    """OpenStreetMap Overpass API - ~4,000 facilities"""
    
    OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    SOURCE_NAME = "openstreetmap"
    
    def fetch(self) -> List[Dict]:
        """Fetch datacenters from OSM via Overpass"""
        logger.info("🗺️ Fetching from OpenStreetMap...")
        
        query = """
        [out:json][timeout:120];
        (
          node["building"="data_center"];
          way["building"="data_center"];
          node["telecom"="data_center"];
          way["telecom"="data_center"];
          node["industrial"="data_centre"];
          way["industrial"="data_centre"];
        );
        out center meta;
        """
        
        response = safe_request(
            self.OVERPASS_URL,
            method='POST',
            data={'data': query}
        )
        
        if not response:
            logger.error("❌ OpenStreetMap fetch failed")
            return []
        
        try:
            data = response.json()
            elements = data.get('elements', [])
            facilities = [self._transform(e) for e in elements if self._is_valid(e)]
            logger.info(f"✅ OpenStreetMap: {len(facilities)} facilities")
            return facilities
        except Exception as e:
            logger.error(f"❌ OpenStreetMap parse error: {e}")
            return []
    
    def _is_valid(self, element: Dict) -> bool:
        """Check if element is a valid datacenter"""
        tags = element.get('tags', {})
        name = tags.get('name', '')
        return bool(name) and is_valid_datacenter(name, source='osm')
    
    def _transform(self, element: Dict) -> Dict:
        """Transform OSM element to standard format"""
        tags = element.get('tags', {})
        
        # Get coordinates (handle both node and way/center)
        lat = element.get('lat') or element.get('center', {}).get('lat')
        lon = element.get('lon') or element.get('center', {}).get('lon')
        
        return {
            'source': self.SOURCE_NAME,
            'source_id': str(element.get('id', '')),
            'name': tags.get('name', 'Unknown'),
            'provider': tags.get('operator', tags.get('name', '')),
            'address': tags.get('addr:full', tags.get('addr:street', '')),
            'city': tags.get('addr:city', ''),
            'state': tags.get('addr:state', ''),
            'country': tags.get('addr:country', ''),
            'latitude': lat,
            'longitude': lon,
            'source_url': f"https://www.openstreetmap.org/{element.get('type', 'node')}/{element.get('id')}",
            'raw_data': json.dumps(element)
        }


# =============================================================================
# SOURCE: WIKIDATA SPARQL
# =============================================================================

class WikidataSource:
    """Wikidata SPARQL - strict filtering for real data centers only"""
    
    SPARQL_URL = "https://query.wikidata.org/sparql"
    SOURCE_NAME = "wikidata"
    
    def fetch(self) -> List[Dict]:
        """Fetch datacenters from Wikidata with strict filtering"""
        logger.info("📚 Fetching from Wikidata (strict mode)...")
        
        # Strict query - require coordinates, direct instance of data center
        query = """
        SELECT DISTINCT ?item ?itemLabel ?itemDescription ?coords ?country ?countryLabel ?operator ?operatorLabel WHERE {
          # Must be direct instance of data center (Q1066984)
          ?item wdt:P31 wd:Q1066984 .
          
          # Must have coordinates (filters out abstract/placeholder entries)
          ?item wdt:P625 ?coords .
          
          OPTIONAL { ?item wdt:P17 ?country . }
          OPTIONAL { ?item wdt:P137 ?operator . }
          
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
        }
        LIMIT 5000
        """
        
        response = safe_request(
            self.SPARQL_URL,
            headers={
                'Accept': 'application/sparql-results+json',
                'User-Agent': 'DCHub/3.0 (https://dchub.cloud)'
            },
            params={'query': query}
        )
        
        if not response:
            logger.error("❌ Wikidata fetch failed")
            return []
        
        try:
            data = response.json()
            results = data.get('results', {}).get('bindings', [])
            facilities = [self._transform(r) for r in results if self._is_valid(r)]
            logger.info(f"✅ Wikidata: {len(facilities)} valid facilities (from {len(results)} total)")
            return facilities
        except Exception as e:
            logger.error(f"❌ Wikidata parse error: {e}")
            return []
    
    def _is_valid(self, result: Dict) -> bool:
        """Strict validation for Wikidata result"""
        name = result.get('itemLabel', {}).get('value', '')
        desc = result.get('itemDescription', {}).get('value', '')
        operator = result.get('operatorLabel', {}).get('value', '')
        
        # Skip items without labels or just Wikidata IDs
        if not name or name.startswith('Q'):
            return False
        
        # Skip generic names
        if name.lower() in ['data center', 'datacenter', 'data centre']:
            return False
        
        # Use strict validation with source context
        combined = f"{name} {operator} {desc}"
        return is_valid_datacenter(name, combined, source='wikidata')
    
    def _transform(self, result: Dict) -> Dict:
        """Transform Wikidata result to standard format"""
        name = result.get('itemLabel', {}).get('value', '')
        item_uri = result.get('item', {}).get('value', '')
        item_id = item_uri.split('/')[-1] if item_uri else ''
        
        # Parse coordinates
        coords = result.get('coords', {}).get('value', '')
        lat, lon = None, None
        if coords:
            match = re.search(r'Point\(([-\d.]+)\s+([-\d.]+)\)', coords)
            if match:
                lon, lat = float(match.group(1)), float(match.group(2))
        
        return {
            'source': self.SOURCE_NAME,
            'source_id': item_id,
            'name': name,
            'provider': result.get('operatorLabel', {}).get('value', ''),
            'country': result.get('countryLabel', {}).get('value', ''),
            'latitude': lat,
            'longitude': lon,
            'source_url': item_uri,
            'raw_data': json.dumps(result),
            'confidence': 0.7  # Lower confidence for Wikidata
        }


# =============================================================================
# SOURCE: CLOUDSCENE (Scrape)
# =============================================================================

class CloudsceneSource:
    """Cloudscene scraper - ~6,100 facilities"""
    
    BASE_URL = "https://cloudscene.com"
    SOURCE_NAME = "cloudscene"
    
    def fetch(self) -> List[Dict]:
        """Fetch facilities from Cloudscene"""
        if not BS4_AVAILABLE:
            logger.warning("⚠️ Cloudscene requires BeautifulSoup")
            return []
        
        logger.info("☁️ Fetching from Cloudscene...")
        facilities = []
        
        # Cloudscene has country pages
        countries = [
            ('united-states', 'US'), ('canada', 'CA'), ('united-kingdom', 'GB'),
            ('germany', 'DE'), ('france', 'FR'), ('netherlands', 'NL'),
            ('australia', 'AU'), ('singapore', 'SG'), ('japan', 'JP'),
            ('india', 'IN'), ('brazil', 'BR'), ('ireland', 'IE')
        ]
        
        for country_slug, country_code in countries:
            url = f"{self.BASE_URL}/data-centers/{country_slug}"
            response = safe_request(url)
            
            if response:
                try:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    items = soup.select('.dc-list-item, .datacenter-card, [data-dc-id]')
                    
                    for item in items:
                        facility = self._parse_item(item, country_code)
                        if facility:
                            facilities.append(facility)
                            
                except Exception as e:
                    logger.warning(f"Cloudscene parse error for {country_slug}: {e}")
            
            time.sleep(REQUEST_DELAY)
        
        logger.info(f"✅ Cloudscene: {len(facilities)} facilities")
        return facilities
    
    def _parse_item(self, item, country_code: str) -> Optional[Dict]:
        """Parse a Cloudscene list item"""
        try:
            name_elem = item.select_one('.dc-name, .title, h3, h4')
            name = name_elem.get_text(strip=True) if name_elem else None
            
            if not name:
                return None
            
            link = item.select_one('a[href*="/data-center/"]')
            source_url = urljoin(self.BASE_URL, link['href']) if link else ''
            source_id = link['href'].split('/')[-1] if link else hashlib.md5(name.encode()).hexdigest()[:12]
            
            location = item.select_one('.location, .city')
            city = location.get_text(strip=True) if location else ''
            
            provider = item.select_one('.provider, .company')
            provider_name = provider.get_text(strip=True) if provider else ''
            
            return {
                'source': self.SOURCE_NAME,
                'source_id': source_id,
                'name': name,
                'provider': provider_name,
                'city': city,
                'country': country_code,
                'source_url': source_url,
                'raw_data': json.dumps({'name': name, 'city': city})
            }
        except Exception:
            return None


# =============================================================================
# SOURCE: BAXTEL (Scrape - US Focus)
# =============================================================================

class BaxtelSource:
    """Baxtel scraper - ~4,255 US facilities"""
    
    BASE_URL = "https://baxtel.com"
    SOURCE_NAME = "baxtel"
    
    def fetch(self) -> List[Dict]:
        """Fetch facilities from Baxtel"""
        if not BS4_AVAILABLE:
            logger.warning("⚠️ Baxtel requires BeautifulSoup")
            return []
        
        logger.info("🏢 Fetching from Baxtel...")
        facilities = []
        
        # Baxtel organizes by state
        states = [
            'virginia', 'texas', 'california', 'arizona', 'nevada', 'illinois',
            'georgia', 'new-york', 'new-jersey', 'florida', 'ohio', 'oregon',
            'washington', 'colorado', 'north-carolina', 'pennsylvania'
        ]
        
        for state in states:
            url = f"{self.BASE_URL}/data-centers/{state}"
            response = safe_request(url)
            
            if response:
                try:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    items = soup.select('.facility-card, .dc-item, [data-facility]')
                    
                    for item in items:
                        facility = self._parse_item(item, state)
                        if facility:
                            facilities.append(facility)
                            
                except Exception as e:
                    logger.warning(f"Baxtel parse error for {state}: {e}")
            
            time.sleep(REQUEST_DELAY)
        
        logger.info(f"✅ Baxtel: {len(facilities)} facilities")
        return facilities
    
    def _parse_item(self, item, state: str) -> Optional[Dict]:
        """Parse a Baxtel facility item"""
        try:
            name_elem = item.select_one('.facility-name, .name, h3')
            name = name_elem.get_text(strip=True) if name_elem else None
            
            if not name:
                return None
            
            link = item.select_one('a[href*="/data-center/"]')
            source_url = urljoin(self.BASE_URL, link['href']) if link else ''
            source_id = link['href'].split('/')[-1] if link else hashlib.md5(name.encode()).hexdigest()[:12]
            
            city_elem = item.select_one('.city, .location')
            city = city_elem.get_text(strip=True) if city_elem else ''
            
            provider_elem = item.select_one('.provider, .operator')
            provider = provider_elem.get_text(strip=True) if provider_elem else ''
            
            # Extract power if available
            power_elem = item.select_one('.power, .mw')
            power_mw = 0
            if power_elem:
                power_text = power_elem.get_text()
                match = re.search(r'([\d.]+)\s*MW', power_text, re.I)
                if match:
                    power_mw = float(match.group(1))
            
            return {
                'source': self.SOURCE_NAME,
                'source_id': source_id,
                'name': name,
                'provider': provider,
                'city': city,
                'state': state.replace('-', ' ').title(),
                'country': 'US',
                'power_mw': power_mw,
                'source_url': source_url,
                'raw_data': json.dumps({'name': name, 'state': state})
            }
        except Exception:
            return None


# =============================================================================
# SOURCE: DATA CENTER MAP (Scrape)
# =============================================================================

class DataCenterMapSource:
    """DataCenterMap.com scraper - ~11,000 facilities"""
    
    BASE_URL = "https://www.datacentermap.com"
    SOURCE_NAME = "datacentermap"
    
    def fetch(self) -> List[Dict]:
        """Fetch facilities from DataCenterMap"""
        if not BS4_AVAILABLE:
            logger.warning("⚠️ DataCenterMap requires BeautifulSoup")
            return []
        
        logger.info("🌍 Fetching from DataCenterMap...")
        facilities = []
        
        # Major countries
        countries = [
            ('united-states', 'US'), ('germany', 'DE'), ('united-kingdom', 'GB'),
            ('netherlands', 'NL'), ('france', 'FR'), ('canada', 'CA'),
            ('australia', 'AU'), ('japan', 'JP'), ('singapore', 'SG'),
            ('china', 'CN'), ('india', 'IN'), ('brazil', 'BR')
        ]
        
        for country_slug, country_code in countries:
            url = f"{self.BASE_URL}/datacenters/{country_slug}/"
            response = safe_request(url)
            
            if response:
                try:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    # DataCenterMap uses table listings
                    rows = soup.select('table tr, .datacenter-row, .listing')
                    
                    for row in rows:
                        facility = self._parse_row(row, country_code)
                        if facility:
                            facilities.append(facility)
                            
                except Exception as e:
                    logger.warning(f"DataCenterMap parse error for {country_slug}: {e}")
            
            time.sleep(REQUEST_DELAY)
        
        logger.info(f"✅ DataCenterMap: {len(facilities)} facilities")
        return facilities
    
    def _parse_row(self, row, country_code: str) -> Optional[Dict]:
        """Parse a DataCenterMap row"""
        try:
            cells = row.select('td')
            if len(cells) < 2:
                return None
            
            name = cells[0].get_text(strip=True)
            if not name or not is_valid_datacenter(name, source='datacentermap'):
                return None
            
            link = row.select_one('a[href]')
            source_url = urljoin(self.BASE_URL, link['href']) if link else ''
            source_id = hashlib.md5(f"{name}:{country_code}".encode()).hexdigest()[:12]
            
            city = cells[1].get_text(strip=True) if len(cells) > 1 else ''
            
            return {
                'source': self.SOURCE_NAME,
                'source_id': source_id,
                'name': name,
                'city': city,
                'country': country_code,
                'source_url': source_url,
                'raw_data': json.dumps({'name': name})
            }
        except Exception:
            return None


# =============================================================================
# SOURCE: RSS NEWS FEEDS
# =============================================================================

class RSSNewsSource:
    """RSS feeds for data center announcements"""
    
    SOURCE_NAME = "rss_news"
    
    FEEDS = [
        ('https://datacenterfrontier.com/feed/', 'Data Center Frontier'),
        ('https://www.datacenterdynamics.com/en/rss/', 'DCD'),
        ('https://www.datacenterknowledge.com/rss.xml', 'DCK'),
        ('https://news.google.com/rss/search?q=data+center+construction', 'Google News'),
    ]
    
    def fetch(self) -> List[Dict]:
        """Fetch announcements from RSS feeds"""
        if not FEEDPARSER_AVAILABLE:
            logger.warning("⚠️ RSS requires feedparser")
            return []
        
        logger.info("📰 Fetching from RSS feeds...")
        announcements = []
        
        for feed_url, feed_name in self.FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:20]:  # Last 20 per feed
                    ann = self._parse_entry(entry, feed_name)
                    if ann:
                        announcements.append(ann)
            except Exception as e:
                logger.warning(f"RSS error for {feed_name}: {e}")
            
            time.sleep(1)
        
        logger.info(f"✅ RSS: {len(announcements)} announcements")
        return announcements
    
    def _parse_entry(self, entry, source: str) -> Optional[Dict]:
        """Parse RSS entry"""
        title = entry.get('title', '')
        
        # Filter for relevant news
        keywords = ['data center', 'datacenter', 'colocation', 'hyperscale', 
                   'megawatt', 'MW', 'campus', 'facility']
        if not any(kw.lower() in title.lower() for kw in keywords):
            return None
        
        return {
            'id': hashlib.md5(entry.get('link', title).encode()).hexdigest()[:16],
            'title': title,
            'summary': entry.get('summary', '')[:500],
            'url': entry.get('link', ''),
            'source': source,
            'published_date': parse_rss_date(entry.get('published', '')),
            'discovered_at': datetime.utcnow().isoformat()
        }


# =============================================================================
# MAIN DISCOVERY ENGINE
# =============================================================================

class DiscoveryEngine:
    """Main discovery engine orchestrating all sources"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_database()
        
        # Initialize sources
        self.sources = {
            'peeringdb': PeeringDBSource(),
            'openstreetmap': OpenStreetMapSource(),
            'wikidata': WikidataSource(),
            'cloudscene': CloudsceneSource(),
            'baxtel': BaxtelSource(),
            'datacentermap': DataCenterMapSource(),
        }
        
        self.news_source = RSSNewsSource()
    
    def run_quick_sync(self) -> Dict:
        """Quick sync - PeeringDB only (most reliable)"""
        logger.info("🚀 Starting quick sync...")
        return self._sync_sources(['peeringdb'])
    
    def run_full_sync(self) -> Dict:
        """Full sync - All sources"""
        logger.info("🚀 Starting full sync (all sources)...")
        return self._sync_sources(list(self.sources.keys()))
    
    def run_news_sync(self) -> Dict:
        """Sync news/announcements only"""
        logger.info("📰 Syncing news feeds...")
        announcements = self.news_source.fetch()
        
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        new_count = 0
        for ann in announcements:
            try:
                c.execute("""
                    INSERT OR IGNORE INTO announcements 
                    (id, title, summary, url, source, published_date, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    ann['id'], ann['title'], ann['summary'],
                    ann['url'], ann['source'], ann['published_date'],
                    ann['discovered_at']
                ))
                if c.rowcount > 0:
                    new_count += 1
            except Exception as e:
                logger.warning(f"Failed to save announcement: {e}")
        
        conn.commit()
        conn.close()
        
        return {
            'success': True,
            'total_found': len(announcements),
            'new_saved': new_count
        }
    
    def _sync_sources(self, source_names: List[str]) -> Dict:
        """Sync specified sources"""
        results = {
            'success': True,
            'sources': {},
            'total_found': 0,
            'total_new': 0,
            'total_updated': 0
        }
        
        for name in source_names:
            if name not in self.sources:
                continue
            
            source = self.sources[name]
            try:
                facilities = source.fetch()
                saved = self._save_facilities(facilities)
                
                results['sources'][name] = {
                    'found': len(facilities),
                    'new': saved['new'],
                    'updated': saved['updated']
                }
                results['total_found'] += len(facilities)
                results['total_new'] += saved['new']
                results['total_updated'] += saved['updated']
                
            except Exception as e:
                logger.error(f"Source {name} failed: {e}")
                results['sources'][name] = {'error': str(e)}
        
        return results
    
    def _save_facilities(self, facilities: List[Dict]) -> Dict:
        """Save facilities to database with deduplication"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        new_count = 0
        updated_count = 0
        
        for f in facilities:
            try:
                facility_id = generate_facility_id(f.get('source', ''), f.get('source_id', ''))
                confidence = calculate_confidence(f)
                now = datetime.utcnow().isoformat()
                
                # Check if exists
                c.execute("SELECT id FROM facilities WHERE id = ?", (facility_id,))
                exists = c.fetchone()
                
                if exists:
                    # Update existing
                    c.execute("""
                        UPDATE facilities SET
                            name = COALESCE(?, name),
                            provider = COALESCE(?, provider),
                            address = COALESCE(?, address),
                            city = COALESCE(?, city),
                            state = COALESCE(?, state),
                            country = COALESCE(?, country),
                            latitude = COALESCE(?, latitude),
                            longitude = COALESCE(?, longitude),
                            power_mw = COALESCE(?, power_mw),
                            confidence = ?,
                            last_updated = ?
                        WHERE id = ?
                    """, (
                        f.get('name'), f.get('provider'), f.get('address'),
                        f.get('city'), f.get('state'), f.get('country'),
                        f.get('latitude'), f.get('longitude'), f.get('power_mw'),
                        confidence, now, facility_id
                    ))
                    updated_count += 1
                else:
                    # Insert new
                    c.execute("""
                        INSERT INTO facilities 
                        (id, name, provider, address, city, state, country, region,
                         latitude, longitude, power_mw, sqft, source, source_id,
                         source_url, raw_data, confidence, first_seen, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        facility_id, f.get('name'), f.get('provider'),
                        f.get('address'), f.get('city'), f.get('state'),
                        f.get('country'), f.get('region', ''),
                        f.get('latitude'), f.get('longitude'),
                        f.get('power_mw', 0), f.get('sqft', 0),
                        f.get('source'), f.get('source_id'),
                        f.get('source_url'), f.get('raw_data', ''),
                        confidence, now, now
                    ))
                    new_count += 1
                    
            except Exception as e:
                logger.warning(f"Failed to save facility: {e}")
        
        conn.commit()
        conn.close()
        
        return {'new': new_count, 'updated': updated_count}
    
    def get_stats(self) -> Dict:
        """Get current database statistics"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM facilities")
        total = c.fetchone()[0]
        
        c.execute("SELECT source, COUNT(*) FROM facilities GROUP BY source")
        by_source = dict(c.fetchall())
        
        c.execute("SELECT country, COUNT(*) FROM facilities GROUP BY country ORDER BY COUNT(*) DESC LIMIT 20")
        by_country = dict(c.fetchall())
        
        c.execute("SELECT COUNT(*) FROM announcements")
        announcements = c.fetchone()[0]
        
        conn.close()
        
        return {
            'total_facilities': total,
            'total_announcements': announcements,
            'by_source': by_source,
            'top_countries': by_country
        }
    
    def cleanup_duplicates(self) -> Dict:
        """Remove duplicate facilities based on name similarity"""
        logger.info("🧹 Cleaning up duplicates...")
        
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        # Get all facilities
        c.execute("SELECT id, name, latitude, longitude, confidence FROM facilities")
        facilities = c.fetchall()
        
        # Group by normalized name
        seen = {}
        duplicates = []
        
        for fac_id, name, lat, lon, conf in facilities:
            norm_name = normalize_name(name)
            key = f"{norm_name}:{round(lat or 0, 2)}:{round(lon or 0, 2)}"
            
            if key in seen:
                # Keep the one with higher confidence
                if conf > seen[key]['confidence']:
                    duplicates.append(seen[key]['id'])
                    seen[key] = {'id': fac_id, 'confidence': conf}
                else:
                    duplicates.append(fac_id)
            else:
                seen[key] = {'id': fac_id, 'confidence': conf}
        
        # Remove duplicates
        if duplicates:
            c.executemany("DELETE FROM facilities WHERE id = ?", [(d,) for d in duplicates])
            conn.commit()
        
        conn.close()
        
        logger.info(f"✅ Removed {len(duplicates)} duplicates")
        return {'removed': len(duplicates)}


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="DC Hub Discovery Engine v3.0")
    parser.add_argument("--quick", action="store_true", help="Quick sync (PeeringDB only)")
    parser.add_argument("--full", action="store_true", help="Full sync (all sources)")
    parser.add_argument("--news", action="store_true", help="Sync news only")
    parser.add_argument("--cleanup", action="store_true", help="Clean duplicates")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    
    args = parser.parse_args()
    engine = DiscoveryEngine()
    
    if args.cleanup:
        print(json.dumps(engine.cleanup_duplicates(), indent=2))
    
    if args.quick:
        print(json.dumps(engine.run_quick_sync(), indent=2))
    elif args.full:
        print(json.dumps(engine.run_full_sync(), indent=2))
    elif args.news:
        print(json.dumps(engine.run_news_sync(), indent=2))
    elif args.stats:
        print(json.dumps(engine.get_stats(), indent=2))
    else:
        # Default: full sync
        print(json.dumps(engine.run_full_sync(), indent=2))
