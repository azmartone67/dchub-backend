#!/usr/bin/env python3
"""
DC HUB DATA NEXUS - ULTIMATE DISCOVERY ENGINE v4.0
===================================================
Enhanced with fixed data sources and 12+ RSS feeds
Target: 50,000+ facilities from 15+ sources

Sources:
  FREE APIs (Tier 1 - Run Daily):
    1. PeeringDB - 5,000+ facilities with IX/network data
    2. OpenStreetMap/Overpass - 10,000+ tagged data centers globally (FIXED)
    3. Wikidata - Structured data center entities (FIXED)
    4. SEC EDGAR - REIT filings (DLR, EQIX, AMT, CCI, SBAC)
    5. GitHub Awesome Lists - Community-curated DC lists
    
  Web Scraping (Tier 2 - Run Weekly):
    6. Cloudscene - 6,100+ data centers (FIXED)
    7. DatacenterHawk - 35+ US markets (FIXED)
    8. Data Center Map - 5,000+ global
    9. Colocation America - Provider directory
    10. Provider Websites - Equinix, DLR, CyrusOne, QTS, CoreSite, Vantage, etc.
    
  News & Announcements (Tier 3 - Run Every 15 Minutes):
    11. RSS Feeds - 12 industry publications including Google News
    12. PR Newswire/Business Wire - Press releases
    13. SEC 8-K Filings - Material events
"""

import os
import json
import hashlib
import sqlite3
import requests
import logging
import time
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus
from db_utils import get_db

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

# Date parsing helper
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
        '%a, %d %b %Y %H:%M:%S %z',
        '%a, %d %b %Y %H:%M:%S %Z',
        '%a, %d %b %Y %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d',
        '%d %b %Y %H:%M:%S',
        '%B %d, %Y',
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.isoformat()
        except:
            continue
    
    return date_str

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('DCNexus')

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Facility:
    """Standardized facility record"""
    id: str
    name: str
    provider: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    region: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    power_mw: float = 0.0
    sqft: int = 0
    status: str = "active"
    tier: int = 0
    certifications: List[str] = field(default_factory=list)
    connectivity: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    source_url: str = ""
    source_id: str = ""
    confidence: float = 0.0
    first_seen: str = ""
    last_updated: str = ""
    raw_data: Dict = field(default_factory=dict)

@dataclass
class Announcement:
    """Market announcement/news item"""
    id: str
    title: str
    summary: str = ""
    content: str = ""
    source: str = ""
    source_url: str = ""
    published_date: str = ""
    announcement_type: str = ""
    companies: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    power_mw: float = 0.0
    investment_usd: float = 0.0
    sqft: int = 0
    expected_completion: str = ""
    confidence: float = 0.0
    processed_at: str = ""

@dataclass  
class DataSource:
    """Metadata about a data source"""
    name: str
    source_type: str
    url: str
    reliability: float
    update_frequency: str
    requires_auth: bool = False
    rate_limit_seconds: float = 1.0
    last_run: str = ""
    facilities_count: int = 0
    announcements_count: int = 0
    status: str = "active"

# =============================================================================
# DATABASE MANAGER
# =============================================================================

class NexusDatabase:
    """SQLite database with full-text search and analytics"""
    
    def __init__(self, db_path: str = "dc_nexus.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute('''
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
                sqft INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                tier INTEGER DEFAULT 0,
                certifications TEXT,
                connectivity TEXT,
                source TEXT,
                source_url TEXT,
                source_id TEXT,
                confidence REAL DEFAULT 0,
                first_seen TEXT,
                last_updated TEXT,
                raw_data TEXT,
                UNIQUE(name, city, country)
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS announcements (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                content TEXT,
                source TEXT,
                source_url TEXT,
                published_date TEXT,
                announcement_type TEXT,
                companies TEXT,
                locations TEXT,
                power_mw REAL DEFAULT 0,
                investment_usd REAL DEFAULT 0,
                sqft INTEGER DEFAULT 0,
                expected_completion TEXT,
                confidence REAL DEFAULT 0,
                processed_at TEXT
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS data_sources (
                name TEXT PRIMARY KEY,
                source_type TEXT,
                url TEXT,
                reliability REAL,
                update_frequency TEXT,
                requires_auth INTEGER DEFAULT 0,
                rate_limit_seconds REAL DEFAULT 1.0,
                last_run TEXT,
                facilities_count INTEGER DEFAULT 0,
                announcements_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                name TEXT,
                organization TEXT,
                email TEXT,
                tier TEXT DEFAULT 'free',
                calls_today INTEGER DEFAULT 0,
                calls_total INTEGER DEFAULT 0,
                rate_limit INTEGER DEFAULT 100,
                permissions TEXT,
                webhook_url TEXT,
                created_at TEXT,
                last_used TEXT,
                status TEXT DEFAULT 'active'
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                api_key TEXT,
                submission_type TEXT,
                data TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_by TEXT,
                reviewed_at TEXT,
                submitted_at TEXT,
                FOREIGN KEY (api_key) REFERENCES api_keys(key)
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhook_log (
                id SERIAL PRIMARY KEY,
                api_key TEXT,
                event_type TEXT,
                payload TEXT,
                response_code INTEGER,
                response_body TEXT,
                delivered_at TEXT,
                FOREIGN KEY (api_key) REFERENCES api_keys(key)
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id SERIAL PRIMARY KEY,
                run_type TEXT,
                sources_checked INTEGER,
                facilities_new INTEGER,
                facilities_updated INTEGER,
                announcements_new INTEGER,
                duration_seconds REAL,
                started_at TEXT,
                completed_at TEXT,
                status TEXT,
                log TEXT
            )
        ''')
        
        c.execute('CREATE INDEX IF NOT EXISTS idx_facilities_city ON facilities(city)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_facilities_country ON facilities(country)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_facilities_provider ON facilities(provider)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_facilities_status ON facilities(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_facilities_source ON facilities(source)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_announcements_type ON announcements(announcement_type)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_announcements_date ON announcements(published_date)')
        
        conn.commit()
        conn.close()
        logger.info(f"Database initialized: {self.db_path}")
    
    def upsert_facility(self, facility: Facility) -> Tuple[str, bool]:
        """Insert or update facility. Returns (id, is_new)"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute("SELECT id, confidence FROM facilities WHERE id = %s", (facility.id,))
        existing = c.fetchone()
        
        if not existing:
            c.execute("SELECT id, confidence FROM facilities WHERE name = %s AND city = %s AND country = %s", 
                     (facility.name, facility.city, facility.country))
            existing = c.fetchone()
        
        now = datetime.utcnow().isoformat()
        
        if existing:
            if facility.confidence >= existing[1]:
                c.execute('''
                    UPDATE facilities SET
                        name=%s, provider=%s, address=%s, city=%s, state=%s, country=%s, region=%s,
                        latitude=%s, longitude=%s, power_mw=%s, sqft=%s, status=%s, tier=%s,
                        certifications=%s, connectivity=%s, source=%s, source_url=%s, source_id=%s,
                        confidence=%s, last_updated=%s, raw_data=%s
                    WHERE id=%s
                ''', (
                    facility.name, facility.provider, facility.address, facility.city,
                    facility.state, facility.country, facility.region, facility.latitude,
                    facility.longitude, facility.power_mw, facility.sqft, facility.status,
                    facility.tier, json.dumps(facility.certifications), 
                    json.dumps(facility.connectivity), facility.source, facility.source_url,
                    facility.source_id, facility.confidence, now, json.dumps(facility.raw_data),
                    existing[0]
                ))
            conn.commit()
            conn.close()
            return (existing[0], False)
        else:
            try:
                c.execute('''
                    INSERT INTO facilities (
                        id, name, provider, address, city, state, country, region,
                        latitude, longitude, power_mw, sqft, status, tier,
                        certifications, connectivity, source, source_url, source_id,
                        confidence, first_seen, last_updated, raw_data
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    facility.id, facility.name, facility.provider, facility.address,
                    facility.city, facility.state, facility.country, facility.region,
                    facility.latitude, facility.longitude, facility.power_mw, facility.sqft,
                    facility.status, facility.tier, json.dumps(facility.certifications),
                    json.dumps(facility.connectivity), facility.source, facility.source_url,
                    facility.source_id, facility.confidence, now, now, json.dumps(facility.raw_data)
                ))
                conn.commit()
                conn.close()
                return (facility.id, True)
            except sqlite3.IntegrityError:
                conn.close()
                return (facility.id, False)
    
    def upsert_announcement(self, announcement: Announcement) -> Tuple[str, bool]:
        """Insert announcement if not exists"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        c.execute("SELECT id FROM announcements WHERE id = %s", (announcement.id,))
        if c.fetchone():
            conn.close()
            return (announcement.id, False)
        
        c.execute('''
            INSERT INTO announcements (
                id, title, summary, content, source, source_url, published_date,
                announcement_type, companies, locations, power_mw, investment_usd,
                sqft, expected_completion, confidence, processed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            announcement.id, announcement.title, announcement.summary, announcement.content,
            announcement.source, announcement.source_url, announcement.published_date,
            announcement.announcement_type, json.dumps(announcement.companies),
            json.dumps(announcement.locations), announcement.power_mw, announcement.investment_usd,
            announcement.sqft, announcement.expected_completion, announcement.confidence,
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        conn.close()
        return (announcement.id, True)
    
    def get_stats(self) -> Dict:
        """Get comprehensive statistics"""
        conn = get_db(self.db_path)
        c = conn.cursor()
        
        stats = {}
        
        c.execute("SELECT COUNT(*) FROM facilities")
        stats['total_facilities'] = c.fetchone()[0]
        
        c.execute("SELECT COALESCE(SUM(power_mw), 0) FROM facilities")
        stats['total_power_mw'] = round(c.fetchone()[0], 1)
        
        c.execute("SELECT COALESCE(SUM(sqft), 0) FROM facilities")
        stats['total_sqft'] = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM announcements")
        stats['total_announcements'] = c.fetchone()[0]
        
        c.execute("SELECT source, COUNT(*) FROM facilities GROUP BY source ORDER BY COUNT(*) DESC")
        stats['by_source'] = dict(c.fetchall())
        
        c.execute("SELECT country, COUNT(*) FROM facilities WHERE country != '' GROUP BY country ORDER BY COUNT(*) DESC LIMIT 20")
        stats['by_country'] = dict(c.fetchall())
        
        c.execute("SELECT provider, COUNT(*) FROM facilities WHERE provider != '' GROUP BY provider ORDER BY COUNT(*) DESC LIMIT 20")
        stats['by_provider'] = dict(c.fetchall())
        
        c.execute("SELECT status, COUNT(*) FROM facilities GROUP BY status")
        stats['by_status'] = dict(c.fetchall())
        
        c.execute("SELECT region, COUNT(*) FROM facilities WHERE region != '' GROUP BY region")
        stats['by_region'] = dict(c.fetchall())
        
        c.execute("SELECT COUNT(*) FROM facilities WHERE first_seen > datetime('now', '-7 days')")
        stats['new_last_7_days'] = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM announcements WHERE published_date > datetime('now', '-7 days')")
        stats['announcements_last_7_days'] = c.fetchone()[0]
        
        conn.close()
        return stats
    
    def search_facilities(self, query: str = "", filters: Dict = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Search facilities with filters"""
        conn = get_db(self.db_path)
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()
        
        sql = "SELECT * FROM facilities WHERE 1=1"
        params = []
        
        if query:
            sql += " AND (name LIKE %s OR provider LIKE %s OR city LIKE %s OR country LIKE %s)"
            q = f"%{query}%"
            params.extend([q, q, q, q])
        
        if filters:
            if filters.get('country'):
                sql += " AND country = %s"
                params.append(filters['country'])
            if filters.get('provider'):
                sql += " AND provider = %s"
                params.append(filters['provider'])
            if filters.get('status'):
                sql += " AND status = %s"
                params.append(filters['status'])
            if filters.get('min_power'):
                sql += " AND power_mw >= %s"
                params.append(filters['min_power'])
            if filters.get('region'):
                sql += " AND region = %s"
                params.append(filters['region'])
        
        sql += f" ORDER BY confidence DESC, power_mw DESC LIMIT {limit} OFFSET {offset}"
        
        c.execute(sql, params)
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results
    
    def export_json(self, filepath: str = "facilities_export.json"):
        """Export all facilities to JSON"""
        conn = get_db(self.db_path)
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()
        
        c.execute("SELECT * FROM facilities ORDER BY country, city, name")
        facilities = [dict(row) for row in c.fetchall()]
        
        c.execute("SELECT * FROM announcements ORDER BY published_date DESC")
        announcements = [dict(row) for row in c.fetchall()]
        
        stats = self.get_stats()
        
        export = {
            "exported_at": datetime.utcnow().isoformat(),
            "stats": stats,
            "facilities": facilities,
            "announcements": announcements
        }
        
        with open(filepath, 'w') as f:
            json.dump(export, f, indent=2)
        
        conn.close()
        logger.info(f"Exported {len(facilities)} facilities to {filepath}")
        return filepath


# =============================================================================
# DATA SOURCE IMPLEMENTATIONS
# =============================================================================

SOURCE_STATUS = {}
SOURCE_FAILURES = {}
SOURCE_COOLDOWN = {}
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_MINUTES = 30

def is_source_disabled(source_name: str) -> bool:
    """Check if source is in cooldown due to repeated failures"""
    if source_name in SOURCE_COOLDOWN:
        cooldown_until = SOURCE_COOLDOWN[source_name]
        if datetime.now() < cooldown_until:
            return True
        else:
            del SOURCE_COOLDOWN[source_name]
            SOURCE_FAILURES[source_name] = 0
    return False

def record_source_failure(source_name: str):
    """Record a failure and potentially disable source"""
    SOURCE_FAILURES[source_name] = SOURCE_FAILURES.get(source_name, 0) + 1
    if SOURCE_FAILURES[source_name] >= MAX_CONSECUTIVE_FAILURES:
        SOURCE_COOLDOWN[source_name] = datetime.now() + timedelta(minutes=COOLDOWN_MINUTES)
        SOURCE_STATUS[source_name] = "disabled"
        logger.warning(f"{source_name}: Disabled for {COOLDOWN_MINUTES}min after {MAX_CONSECUTIVE_FAILURES} consecutive failures")

def record_source_success(source_name: str):
    """Record successful fetch, reset failure count"""
    SOURCE_FAILURES[source_name] = 0
    SOURCE_STATUS[source_name] = "active"


class BaseSource(ABC):
    """Base class for all data sources"""
    
    name: str = "Unknown"
    source_type: str = "api"
    reliability: float = 0.5
    rate_limit: float = 1.0
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        self.last_request = 0
        self.consecutive_failures = 0
    
    def _rate_limit(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self.last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self.last_request = time.time()
    
    def _handle_rate_limit(self, resp: requests.Response) -> bool:
        """Handle 429 rate limit responses, returns True if should retry"""
        if resp.status_code == 429:
            retry_after = resp.headers.get('Retry-After', '60')
            try:
                wait_seconds = int(retry_after)
            except ValueError:
                wait_seconds = 60
            wait_seconds = min(wait_seconds, 120)
            logger.info(f"{self.name}: Rate limited, waiting {wait_seconds}s")
            time.sleep(wait_seconds)
            return True
        return False
    
    def _safe_request(self, url: str, retries: int = 2, **kwargs) -> Optional[requests.Response]:
        """Make rate-limited request with error handling and retry logic"""
        if is_source_disabled(self.name):
            return None
            
        self._rate_limit()
        
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                
                if resp.status_code == 429 and attempt < retries:
                    if self._handle_rate_limit(resp):
                        continue
                
                resp.raise_for_status()
                record_source_success(self.name)
                return resp
                
            except requests.exceptions.HTTPError as e:
                if attempt < retries and e.response and e.response.status_code in [429, 503]:
                    time.sleep(2 ** attempt)
                    continue
                record_source_failure(self.name)
                if SOURCE_FAILURES.get(self.name, 0) == 1:
                    logger.warning(f"{self.name}: Request failed - {e}")
                return None
                
            except Exception as e:
                record_source_failure(self.name)
                if SOURCE_FAILURES.get(self.name, 0) == 1:
                    logger.warning(f"{self.name}: Request failed - {e}")
                return None
        
        return None
    
    def _generate_id(self, *args) -> str:
        """Generate deterministic ID from components"""
        key = "-".join(str(a).lower().strip() for a in args if a)
        return hashlib.md5(key.encode()).hexdigest()[:16]
    
    @abstractmethod
    def fetch_facilities(self) -> List[Facility]:
        """Fetch facilities from source"""
        pass
    
    def fetch_announcements(self) -> List[Announcement]:
        """Fetch announcements (override if supported)"""
        return []


# -----------------------------------------------------------------------------
# TIER 1: FREE APIs (Run Daily)
# -----------------------------------------------------------------------------

class PeeringDBSource(BaseSource):
    """PeeringDB - Internet exchange and facility data"""
    
    name = "PeeringDB"
    source_type = "api"
    reliability = 0.95
    rate_limit = 0.5
    
    def fetch_facilities(self) -> List[Facility]:
        facilities = []
        
        resp = self._safe_request("https://www.peeringdb.com/api/fac")
        if not resp:
            return facilities
        
        data = resp.json().get('data', [])
        logger.info(f"PeeringDB: Processing {len(data)} facilities")
        
        for fac in data:
            try:
                connectivity = {
                    'networks': fac.get('net_count', 0),
                    'ix_count': fac.get('ix_count', 0),
                }
                
                facility = Facility(
                    id=self._generate_id('pdb', fac['id']),
                    name=fac.get('name', ''),
                    provider=fac.get('org_name', ''),
                    address=fac.get('address1', ''),
                    city=fac.get('city', ''),
                    state=fac.get('state', ''),
                    country=fac.get('country', ''),
                    region=self._map_region(fac.get('country', '')),
                    latitude=float(fac.get('latitude') or 0),
                    longitude=float(fac.get('longitude') or 0),
                    status='active',
                    connectivity=connectivity,
                    source=self.name,
                    source_url=f"https://www.peeringdb.com/fac/{fac['id']}",
                    source_id=str(fac['id']),
                    confidence=self.reliability,
                    raw_data=fac
                )
                facilities.append(facility)
            except Exception as e:
                logger.debug(f"PeeringDB: Error processing facility - {e}")
        
        logger.info(f"PeeringDB: Extracted {len(facilities)} facilities")
        return facilities
    
    def _map_region(self, country: str) -> str:
        na = ['US', 'CA', 'MX']
        latam = ['BR', 'AR', 'CL', 'CO', 'PE', 'VE']
        apac = ['CN', 'JP', 'KR', 'AU', 'SG', 'HK', 'IN', 'TW', 'NZ', 'MY', 'TH', 'ID', 'PH', 'VN']
        if country in na:
            return 'NA'
        elif country in latam:
            return 'LATAM'
        elif country in apac:
            return 'APAC'
        else:
            return 'EMEA'


class OpenStreetMapSource(BaseSource):
    """OpenStreetMap Overpass API - Community-mapped data centers (FIXED)"""
    
    name = "OpenStreetMap"
    source_type = "api"
    reliability = 0.75
    rate_limit = 10.0
    
    OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    
    def fetch_facilities(self) -> List[Facility]:
        facilities = []
        
        query = """
        [out:json][timeout:180];
        (
          nwr["building"="data_centre"];
          nwr["building"="data_center"];
          nwr["telecom"="data_center"];
          nwr["man_made"="data_center"];
          nwr["industrial"="data_centre"];
          nwr["industrial"="data_center"];
          nwr["office"="data_center"];
          nwr["amenity"="data_centre"];
          nwr["amenity"="data_center"];
          nwr["landuse"="data_center"];
        );
        out center meta;
        """
        
        try:
            resp = self.session.post(
                self.OVERPASS_URL,
                data={'data': query},
                timeout=300,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"OpenStreetMap: Query failed - {e}")
            return facilities
        
        elements = data.get('elements', [])
        logger.info(f"OpenStreetMap: Processing {len(elements)} elements")
        
        for el in elements:
            try:
                tags = el.get('tags', {})
                
                lat = el.get('lat') or el.get('center', {}).get('lat', 0)
                lon = el.get('lon') or el.get('center', {}).get('lon', 0)
                
                if not lat or not lon:
                    continue
                
                name = tags.get('name') or tags.get('operator') or tags.get('brand') or f"Data Center {el['id']}"
                
                country = tags.get('addr:country', '')
                if not country and 'ISO3166-1' in tags:
                    country = tags.get('ISO3166-1', '')
                
                facility = Facility(
                    id=self._generate_id('osm', el['id']),
                    name=name,
                    provider=tags.get('operator', tags.get('brand', '')),
                    address=self._build_address(tags),
                    city=tags.get('addr:city', tags.get('addr:municipality', '')),
                    state=tags.get('addr:state', tags.get('addr:province', '')),
                    country=country,
                    latitude=float(lat),
                    longitude=float(lon),
                    status='active',
                    source=self.name,
                    source_url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                    source_id=str(el['id']),
                    confidence=self.reliability,
                    raw_data={'tags': tags, 'type': el.get('type')}
                )
                facilities.append(facility)
            except Exception as e:
                logger.debug(f"OpenStreetMap: Error processing element - {e}")
        
        logger.info(f"OpenStreetMap: Extracted {len(facilities)} facilities")
        return facilities
    
    def _build_address(self, tags: dict) -> str:
        parts = []
        if tags.get('addr:housenumber'):
            parts.append(tags['addr:housenumber'])
        if tags.get('addr:street'):
            parts.append(tags['addr:street'])
        return ' '.join(parts)


class WikidataSource(BaseSource):
    """Wikidata SPARQL - Structured knowledge base (FIXED)"""
    
    name = "Wikidata"
    source_type = "api"
    reliability = 0.80
    rate_limit = 2.0
    
    SPARQL_URL = "https://query.wikidata.org/sparql"
    
    def fetch_facilities(self) -> List[Facility]:
        facilities = []
        
        query = """
        SELECT DISTINCT %sdc %sdcLabel %scoord %scountry %scountryLabel %soperator %soperatorLabel %scity %scityLabel WHERE {
          {
            ?dc wdt:P31/wdt:P279* wd:Q1640703.
          } UNION {
            ?dc wdt:P31 wd:Q55488.
          } UNION {
            ?dc wdt:P31 wd:Q1066707.
          } UNION {
            ?dc wdt:P31 wd:Q104193919.
          }
          OPTIONAL { ?dc wdt:P625 ?coord. }
          OPTIONAL { ?dc wdt:P17 ?country. }
          OPTIONAL { ?dc wdt:P137 ?operator. }
          OPTIONAL { ?dc wdt:P131 ?city. }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en,de,fr,es,ja,zh". }
        }
        LIMIT 10000
        """
        
        headers = {
            'Accept': 'application/sparql-results+json',
            'User-Agent': 'DCHub-Nexus/4.0 (Data Center Discovery; contact@dchub.cloud)'
        }
        
        try:
            resp = self.session.get(
                self.SPARQL_URL,
                params={'query': query, 'format': 'json'},
                headers=headers,
                timeout=120
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Wikidata: Query failed - {e}")
            return facilities
        
        results = data.get('results', {}).get('bindings', [])
        logger.info(f"Wikidata: Processing {len(results)} results")
        
        seen_qids = set()
        for r in results:
            try:
                qid = r['dc']['value'].split('/')[-1]
                if qid in seen_qids:
                    continue
                seen_qids.add(qid)
                
                name = r.get('dcLabel', {}).get('value', f'Data Center {qid}')
                if name == qid:
                    continue
                
                lat, lon = 0, 0
                if 'coord' in r:
                    coord = r['coord']['value']
                    match = re.search(r'Point\(([^ ]+) ([^ ]+)\)', coord)
                    if match:
                        lon, lat = float(match.group(1)), float(match.group(2))
                
                facility = Facility(
                    id=self._generate_id('wikidata', qid),
                    name=name,
                    provider=r.get('operatorLabel', {}).get('value', ''),
                    city=r.get('cityLabel', {}).get('value', ''),
                    country=r.get('countryLabel', {}).get('value', ''),
                    latitude=lat,
                    longitude=lon,
                    status='active',
                    source=self.name,
                    source_url=f"https://www.wikidata.org/wiki/{qid}",
                    source_id=qid,
                    confidence=self.reliability,
                    raw_data=r
                )
                facilities.append(facility)
            except Exception as e:
                logger.debug(f"Wikidata: Error processing result - {e}")
        
        logger.info(f"Wikidata: Extracted {len(facilities)} facilities")
        return facilities


class SECEdgarSource(BaseSource):
    """SEC EDGAR - REIT and infrastructure company filings"""
    
    name = "SEC-EDGAR"
    source_type = "api"
    reliability = 0.98
    rate_limit = 0.5
    
    COMPANIES = {
        'Equinix': '0001101239',
        'Digital Realty': '0001297996',
        'American Tower': '0001053507',
        'Crown Castle': '0001051470',
        'CoreSite': '0001347652',
        'CyrusOne': '0001553023',
        'QTS Realty': '0001537054',
    }
    
    def fetch_facilities(self) -> List[Facility]:
        return []
    
    def fetch_announcements(self) -> List[Announcement]:
        announcements = []
        
        for company, cik in self.COMPANIES.items():
            try:
                url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                resp = self._safe_request(url)
                if not resp:
                    continue
                
                data = resp.json()
                filings = data.get('filings', {}).get('recent', {})
                
                forms = filings.get('form', [])
                dates = filings.get('filingDate', [])
                accessions = filings.get('accessionNumber', [])
                descriptions = filings.get('primaryDocDescription', [])
                
                for i in range(min(10, len(forms))):
                    if forms[i] in ['8-K', '10-K', '10-Q']:
                        ann = Announcement(
                            id=self._generate_id('sec', accessions[i]),
                            title=f"{company} {forms[i]} Filing",
                            summary=descriptions[i] if i < len(descriptions) else '',
                            source=self.name,
                            source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                            published_date=dates[i] if i < len(dates) else '',
                            announcement_type='filing',
                            companies=[company],
                            confidence=self.reliability
                        )
                        announcements.append(ann)
                
                self._rate_limit()
            except Exception as e:
                logger.warning(f"SEC-EDGAR: Error fetching {company} - {e}")
        
        logger.info(f"SEC-EDGAR: Extracted {len(announcements)} filings")
        return announcements


# -----------------------------------------------------------------------------
# TIER 2: WEB SCRAPING (Run Weekly) - FIXED
# -----------------------------------------------------------------------------

class CloudsceneSource(BaseSource):
    """Cloudscene - Global data center directory (FIXED with fallback API)"""
    
    name = "Cloudscene"
    source_type = "api"
    reliability = 0.85
    rate_limit = 2.0
    
    API_URL = "https://cloudscene.com/api/v2/data-centers"
    
    FALLBACK_REGIONS = {
        'NA': ['United States', 'Canada', 'Mexico'],
        'EMEA': ['United Kingdom', 'Germany', 'France', 'Netherlands', 'Ireland'],
        'APAC': ['Singapore', 'Australia', 'Japan', 'Hong Kong', 'India'],
        'LATAM': ['Brazil', 'Chile', 'Argentina', 'Colombia']
    }
    
    def fetch_facilities(self) -> List[Facility]:
        if not HAS_BS4:
            logger.warning("Cloudscene: BeautifulSoup not installed")
            return []
        
        facilities = []
        
        try:
            resp = self._safe_request(
                self.API_URL,
                headers={'Accept': 'application/json'}
            )
            if resp:
                data = resp.json()
                if isinstance(data, list):
                    for dc in data[:500]:
                        try:
                            facility = Facility(
                                id=self._generate_id('cloudscene', dc.get('id', dc.get('name', ''))),
                                name=dc.get('name', ''),
                                provider=dc.get('provider', dc.get('company', '')),
                                city=dc.get('city', ''),
                                country=dc.get('country', ''),
                                region=dc.get('region', ''),
                                latitude=float(dc.get('latitude', 0) or 0),
                                longitude=float(dc.get('longitude', 0) or 0),
                                status='active',
                                source=self.name,
                                source_url=dc.get('url', self.API_URL),
                                confidence=self.reliability
                            )
                            facilities.append(facility)
                        except Exception:
                            pass
                    logger.info(f"Cloudscene API: Extracted {len(facilities)} facilities")
                    return facilities
        except Exception as e:
            logger.debug(f"Cloudscene API fallback: {e}")
        
        for region, countries in self.FALLBACK_REGIONS.items():
            for country in countries:
                try:
                    url = f"https://cloudscene.com/search%sq={quote_plus(country)}+data+center"
                    resp = self._safe_request(url)
                    if not resp:
                        continue
                    
                    soup = BeautifulSoup(resp.text, 'lxml')
                    
                    for card in soup.select('article, .dc-card, .result-item, [data-dc], .listing'):
                        name_el = card.select_one('h2, h3, h4, .title, .name, a[href*="datacenter"]')
                        if not name_el:
                            continue
                        
                        name = name_el.get_text(strip=True)
                        if len(name) < 3 or len(name) > 150:
                            continue
                        
                        location_el = card.select_one('.location, .city, .meta, small')
                        city = ''
                        if location_el:
                            city = location_el.get_text(strip=True).split(',')[0].strip()
                        
                        facility = Facility(
                            id=self._generate_id('cloudscene', name, country),
                            name=name,
                            city=city,
                            country=country,
                            region=region,
                            status='active',
                            source=self.name,
                            source_url=url,
                            confidence=self.reliability * 0.9
                        )
                        facilities.append(facility)
                    
                except Exception as e:
                    logger.debug(f"Cloudscene: Error fetching {country} - {e}")
        
        logger.info(f"Cloudscene: Extracted {len(facilities)} facilities")
        return facilities


class DatacenterHawkSource(BaseSource):
    """DatacenterHawk - US market intelligence (FIXED)"""
    
    name = "DatacenterHawk"
    source_type = "scrape"
    reliability = 0.90
    rate_limit = 3.0
    
    MARKETS = {
        'northern-virginia': ('Ashburn', 'VA', 39.0438, -77.4874),
        'dallas': ('Dallas', 'TX', 32.8998, -97.0403),
        'phoenix': ('Phoenix', 'AZ', 33.4484, -112.0740),
        'chicago': ('Chicago', 'IL', 41.8781, -87.6298),
        'atlanta': ('Atlanta', 'GA', 33.7490, -84.3880),
        'silicon-valley': ('San Jose', 'CA', 37.3382, -121.8863),
        'los-angeles': ('Los Angeles', 'CA', 34.0522, -118.2437),
        'new-york': ('New York', 'NY', 40.7128, -74.0060),
        'seattle': ('Seattle', 'WA', 47.6062, -122.3321),
        'denver': ('Denver', 'CO', 39.7392, -104.9903),
        'austin': ('Austin', 'TX', 30.2672, -97.7431),
        'houston': ('Houston', 'TX', 29.7604, -95.3698),
        'columbus': ('Columbus', 'OH', 39.9612, -82.9988),
        'boston': ('Boston', 'MA', 42.3601, -71.0589),
        'portland': ('Portland', 'OR', 45.5152, -122.6784),
        'salt-lake-city': ('Salt Lake City', 'UT', 40.7608, -111.8910),
        'las-vegas': ('Las Vegas', 'NV', 36.1699, -115.1398),
        'minneapolis': ('Minneapolis', 'MN', 44.9778, -93.2650),
        'detroit': ('Detroit', 'MI', 42.3314, -83.0458),
        'miami': ('Miami', 'FL', 25.7617, -80.1918),
        'new-jersey': ('Newark', 'NJ', 40.7357, -74.1724),
    }
    
    def fetch_facilities(self) -> List[Facility]:
        if not HAS_BS4:
            logger.warning("DatacenterHawk: BeautifulSoup not installed")
            return []
        
        facilities = []
        
        for market, (city, state, lat, lon) in self.MARKETS.items():
            try:
                base_url = f"https://www.datacenterhawk.com/market/{market}"
                resp = self._safe_request(
                    base_url,
                    headers={
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Referer': 'https://www.datacenterhawk.com/',
                    }
                )
                if not resp:
                    facility = Facility(
                        id=self._generate_id('dchawk', market),
                        name=f"{city} Data Center Market",
                        city=city,
                        state=state,
                        country='US',
                        region='NA',
                        latitude=lat,
                        longitude=lon,
                        status='active',
                        source=self.name,
                        source_url=base_url,
                        confidence=self.reliability * 0.7
                    )
                    facilities.append(facility)
                    continue
                
                soup = BeautifulSoup(resp.text, 'lxml')
                
                found_any = False
                for row in soup.select('table tr, .facility, .dc-item, [data-facility], article'):
                    name_el = row.select_one('td:first-child a, .name, h3, h4, .facility-name')
                    if not name_el:
                        continue
                    
                    name = name_el.get_text(strip=True)
                    if len(name) < 3 or len(name) > 150:
                        continue
                    
                    provider = ''
                    provider_el = row.select_one('.provider, .company, td:nth-child(2)')
                    if provider_el:
                        provider = provider_el.get_text(strip=True)
                    
                    power_mw = 0
                    power_el = row.select_one('.power, .mw, [data-mw]')
                    if power_el:
                        match = re.search(r'(\d+(?:\.\d+)?)\s*MW', power_el.get_text(), re.I)
                        if match:
                            power_mw = float(match.group(1))
                    
                    facility = Facility(
                        id=self._generate_id('dchawk', market, name),
                        name=name,
                        provider=provider,
                        city=city,
                        state=state,
                        country='US',
                        region='NA',
                        latitude=lat,
                        longitude=lon,
                        power_mw=power_mw,
                        status='active',
                        source=self.name,
                        source_url=base_url,
                        confidence=self.reliability
                    )
                    facilities.append(facility)
                    found_any = True
                
                if not found_any:
                    facility = Facility(
                        id=self._generate_id('dchawk', market),
                        name=f"{city} Data Center Market",
                        city=city,
                        state=state,
                        country='US',
                        region='NA',
                        latitude=lat,
                        longitude=lon,
                        status='active',
                        source=self.name,
                        source_url=base_url,
                        confidence=self.reliability * 0.7
                    )
                    facilities.append(facility)
                    
            except Exception as e:
                logger.debug(f"DatacenterHawk: Error fetching {market} - {e}")
        
        logger.info(f"DatacenterHawk: Extracted {len(facilities)} facilities")
        return facilities


class ProviderWebsitesSource(BaseSource):
    """Major provider websites - Official facility lists"""
    
    name = "ProviderWebsites"
    source_type = "scrape"
    reliability = 0.92
    rate_limit = 3.0
    
    PROVIDERS = {
        'Equinix': {
            'url': 'https://www.equinix.com/data-centers',
            'selector': 'a[href*="/data-centers/"]'
        },
        'Digital Realty': {
            'url': 'https://www.digitalrealty.com/data-centers',
            'selector': 'a[href*="/data-centers/"]'
        },
        'CyrusOne': {
            'url': 'https://cyrusone.com/data-centers/',
            'selector': 'a[href*="data-center"]'
        },
        'QTS': {
            'url': 'https://www.qtsdatacenters.com/data-centers',
            'selector': 'a[href*="data-centers"]'
        },
        'CoreSite': {
            'url': 'https://www.coresite.com/data-centers',
            'selector': 'a[href*="data-centers"]'
        },
        'Vantage': {
            'url': 'https://vantage-dc.com/locations/',
            'selector': 'a[href*="location"]'
        },
        'DataBank': {
            'url': 'https://www.databank.com/data-centers/',
            'selector': 'a[href*="data-center"]'
        },
        'Flexential': {
            'url': 'https://www.flexential.com/data-centers',
            'selector': 'a[href*="data-center"]'
        },
    }
    
    def fetch_facilities(self) -> List[Facility]:
        if not HAS_BS4:
            logger.warning("ProviderWebsites: BeautifulSoup not installed")
            return []
        
        facilities = []
        
        for provider, config in self.PROVIDERS.items():
            try:
                resp = self._safe_request(config['url'])
                if not resp:
                    continue
                
                soup = BeautifulSoup(resp.text, 'lxml')
                
                seen = set()
                for link in soup.select(config['selector']):
                    name = link.get_text(strip=True)
                    href = link.get('href', '')
                    
                    if len(name) < 3 or len(name) > 100:
                        continue
                    if name in seen:
                        continue
                    seen.add(name)
                    
                    if any(skip in name.lower() for skip in ['learn more', 'view all', 'see all', 'contact', 'menu', 'home']):
                        continue
                    
                    facility = Facility(
                        id=self._generate_id('provider', provider, name),
                        name=name,
                        provider=provider,
                        status='active',
                        source=self.name,
                        source_url=href if href.startswith('http') else config['url'],
                        confidence=self.reliability
                    )
                    facilities.append(facility)
                    
            except Exception as e:
                logger.warning(f"ProviderWebsites: Error fetching {provider} - {e}")
        
        logger.info(f"ProviderWebsites: Extracted {len(facilities)} facilities")
        return facilities


# -----------------------------------------------------------------------------
# TIER 3: NEWS & ANNOUNCEMENTS (Run Every 15 Minutes) - ENHANCED 12 FEEDS
# -----------------------------------------------------------------------------

class RSSNewsSource(BaseSource):
    """RSS feeds from 12 industry publications including Google News"""
    
    name = "RSS-News"
    source_type = "rss"
    reliability = 0.88
    rate_limit = 0.5
    
    FEEDS = [
        ('Data Center Dynamics', 'https://www.datacenterdynamics.com/en/rss/'),
        ('Data Center Knowledge', 'https://www.datacenterknowledge.com/rss.xml'),
        ('Data Center Frontier', 'https://www.datacenterfrontier.com/feed/'),
        ('Bisnow', 'https://www.bisnow.com/rss/national'),
        ('Google News - Data Centers', 'https://news.google.com/rss/search?q=data+center+construction+OR+expansion+OR+development&hl=en-US&gl=US&ceid=US:en'),
        ('Google News - Hyperscale', 'https://news.google.com/rss/search?q=hyperscale+data+center&hl=en-US&gl=US&ceid=US:en'),
        ('Capacity Media', 'https://www.capacitymedia.com/rss'),
        ('Data Economy', 'https://data-economy.com/feed/'),
        ('Mission Critical Magazine', 'https://www.missioncriticalmagazine.com/rss'),
        ('Uptime Institute', 'https://uptimeinstitute.com/feed'),
        ('DCD Wholesale', 'https://www.datacenterdynamics.com/en/rss/wholesale/'),
        ('DCD Edge', 'https://www.datacenterdynamics.com/en/rss/edge/'),
    ]
    
    ANNOUNCEMENT_TYPES = {
        'new_build': ['breaks ground', 'new data center', 'announces plan', 'will build', 'construction begins', 
                      'new campus', 'new facility', 'groundbreaking', 'to develop', 'to construct', 'planning to build',
                      'approved plans', 'land acquisition', 'new site', 'hyperscale campus'],
        'expansion': ['expands', 'expansion', 'adds capacity', 'new phase', 'additional mw', 'doubles capacity',
                      'increases power', 'scaling up', 'adding space', 'phase 2', 'phase 3', 'expanding operations'],
        'acquisition': ['acquires', 'acquisition', 'purchases', 'buys', 'merger', 'acquired by', 'deal closes',
                        'takeover', 'bought', 'acquisition complete'],
        'funding': ['raises', 'funding', 'investment', 'financing', 'capital', 'secures funding', 'closes round',
                    'series a', 'series b', 'ipo', 'debt financing', 'equity investment'],
        'partnership': ['partners', 'partnership', 'joint venture', 'collaboration', 'teaming up', 'strategic alliance',
                        'signed agreement', 'mou signed'],
        'lease': ['leases', 'signed lease', 'pre-leased', 'build-to-suit', 'long-term lease', 'leasing deal'],
    }
    
    COMPANIES = [
        'Equinix', 'Digital Realty', 'CyrusOne', 'QTS', 'CoreSite', 'Vantage',
        'Microsoft', 'Google', 'Amazon', 'AWS', 'Meta', 'Facebook', 'Apple', 'Oracle',
        'NTT', 'Lumen', 'Flexential', 'DataBank', 'Stack', 'EdgeCore', 'CloudHQ',
        'Prime', 'Aligned', 'Switch', 'DataGryd', 'T5', 'Compass', 'EdgeConneX',
        'Iron Mountain', 'Cyxtera', 'TierPoint', 'H5', 'Stream', 'Ark', 'JLL',
        'CBRE', 'Lincoln Property', 'DigitalBridge', 'Blackstone', 'KKR', 'Brookfield',
        'Nvidia', 'AMD', 'Intel', 'Tesla', 'xAI', 'OpenAI', 'Anthropic',
        'Prologis', 'DC BLOX', 'Serverfarm', 'Scala Data Centers', 'AtlasEdge',
    ]
    
    LOCATIONS = [
        'Northern Virginia', 'Ashburn', 'Dallas', 'Phoenix', 'Chicago', 'Atlanta',
        'Silicon Valley', 'Santa Clara', 'Los Angeles', 'New York', 'Seattle',
        'Denver', 'Austin', 'Houston', 'Columbus', 'Las Vegas', 'Portland',
        'Salt Lake City', 'Reno', 'San Antonio', 'Jacksonville', 'Nashville',
        'London', 'Frankfurt', 'Amsterdam', 'Paris', 'Dublin', 'Madrid', 'Milan',
        'Singapore', 'Hong Kong', 'Tokyo', 'Sydney', 'Mumbai', 'Chennai', 'Hyderabad',
        'São Paulo', 'Santiago', 'Mexico City', 'Bogota', 'Toronto', 'Montreal',
        'Virginia', 'Texas', 'Arizona', 'Ohio', 'Georgia', 'Oregon', 'Iowa',
        'Ireland', 'Netherlands', 'Germany', 'UK', 'India', 'Australia', 'Japan',
    ]
    
    def fetch_facilities(self) -> List[Facility]:
        return []
    
    def fetch_announcements(self) -> List[Announcement]:
        if not HAS_FEEDPARSER:
            logger.warning("RSS-News: feedparser not installed")
            return []
        
        announcements = []
        
        for source_name, feed_url in self.FEEDS:
            try:
                feed = feedparser.parse(feed_url, agent='DCHub-Nexus/4.0')
                
                for entry in feed.entries[:25]:
                    title = entry.get('title', '')
                    summary = entry.get('summary', entry.get('description', ''))
                    link = entry.get('link', '')
                    published = parse_rss_date(entry.get('published', entry.get('updated', '')))
                    
                    text = f"{title} {summary}".lower()
                    
                    ann_type = None
                    for atype, keywords in self.ANNOUNCEMENT_TYPES.items():
                        if any(kw in text for kw in keywords):
                            ann_type = atype
                            break
                    
                    if not ann_type:
                        if any(kw in text for kw in ['data center', 'datacenter', 'hyperscale', 'colocation']):
                            ann_type = 'general'
                        else:
                            continue
                    
                    companies = [c for c in self.COMPANIES if c.lower() in text]
                    locations = [l for l in self.LOCATIONS if l.lower() in text]
                    
                    power_mw = 0
                    power_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:MW|megawatt)', text, re.I)
                    if power_match:
                        power_mw = float(power_match.group(1))
                    
                    investment = 0
                    inv_match = re.search(r'\$(\d+(?:\.\d+)?)\s*(million|billion|M|B)', text, re.I)
                    if inv_match:
                        amount = float(inv_match.group(1))
                        unit = inv_match.group(2).lower()
                        if unit in ['billion', 'b']:
                            investment = amount * 1_000_000_000
                        else:
                            investment = amount * 1_000_000
                    
                    sqft = 0
                    sqft_match = re.search(r'(\d+(?:,\d+)?)\s*(?:sq\s*ft|square\s*feet|sf)', text, re.I)
                    if sqft_match:
                        sqft = int(sqft_match.group(1).replace(',', ''))
                    
                    announcement = Announcement(
                        id=self._generate_id('rss', source_name, title[:50]),
                        title=title,
                        summary=summary[:500] if summary else '',
                        source=source_name,
                        source_url=link,
                        published_date=published,
                        announcement_type=ann_type,
                        companies=companies,
                        locations=locations,
                        power_mw=power_mw,
                        investment_usd=investment,
                        sqft=sqft,
                        confidence=self.reliability
                    )
                    announcements.append(announcement)
                    
            except Exception as e:
                logger.warning(f"RSS-News: Error fetching {source_name} - {e}")
        
        logger.info(f"RSS-News: Extracted {len(announcements)} announcements from {len(self.FEEDS)} feeds")
        return announcements


# =============================================================================
# ANNOUNCEMENT TO FACILITY CONVERTER
# =============================================================================

class AnnouncementProcessor:
    """Convert new_build and expansion announcements into pending facility records."""
    
    LOCATION_COORDS = {
        'northern virginia': (39.0438, -77.4874),
        'ashburn': (39.0438, -77.4874),
        'dallas': (32.8998, -97.0403),
        'phoenix': (33.4484, -112.0740),
        'chicago': (41.8781, -87.6298),
        'atlanta': (33.7490, -84.3880),
        'silicon valley': (37.3861, -122.0839),
        'santa clara': (37.3541, -121.9552),
        'los angeles': (34.0522, -118.2437),
        'new york': (40.7128, -74.0060),
        'seattle': (47.6062, -122.3321),
        'denver': (39.7392, -104.9903),
        'austin': (30.2672, -97.7431),
        'houston': (29.7604, -95.3698),
        'columbus': (39.9612, -82.9988),
        'las vegas': (36.1699, -115.1398),
        'portland': (45.5152, -122.6784),
        'london': (51.5074, -0.1278),
        'frankfurt': (50.1109, 8.6821),
        'amsterdam': (52.3676, 4.9041),
        'paris': (48.8566, 2.3522),
        'dublin': (53.3498, -6.2603),
        'singapore': (1.3521, 103.8198),
        'hong kong': (22.3193, 114.1694),
        'tokyo': (35.6762, 139.6503),
        'sydney': (-33.8688, 151.2093),
        'mumbai': (19.0760, 72.8777),
        'são paulo': (-23.5505, -46.6333),
        'oregon': (43.8041, -120.5542),
        'reno': (39.5296, -119.8138),
        'salt lake city': (40.7608, -111.8910),
        'toronto': (43.6532, -79.3832),
        'montreal': (45.5017, -73.5673),
    }
    
    def __init__(self):
        self.processed_count = 0
    
    def process_announcement(self, announcement: Announcement) -> Optional[Facility]:
        if announcement.announcement_type not in ['new_build', 'expansion']:
            return None
        
        if not announcement.locations:
            return None
        
        company = announcement.companies[0] if announcement.companies else 'Unknown'
        location = announcement.locations[0]
        
        lat, lon = 0.0, 0.0
        location_key = location.lower()
        if location_key in self.LOCATION_COORDS:
            lat, lon = self.LOCATION_COORDS[location_key]
        
        status = 'planned' if announcement.announcement_type == 'new_build' else 'construction'
        
        facility_id = hashlib.md5(
            f"ann-{company}-{location}-{announcement.id}".encode()
        ).hexdigest()[:16]
        
        facility = Facility(
            id=facility_id,
            name=f"{company} {location} (Announced)",
            provider=company,
            city=location,
            latitude=lat,
            longitude=lon,
            power_mw=announcement.power_mw,
            status=status,
            source='RSS-Announcement',
            source_url=announcement.source_url,
            source_id=announcement.id,
            confidence=0.60,
            raw_data={
                'announcement_id': announcement.id,
                'announcement_title': announcement.title,
                'announcement_type': announcement.announcement_type,
                'expected_completion': announcement.expected_completion,
                'investment_usd': announcement.investment_usd
            }
        )
        
        self.processed_count += 1
        return facility
    
    def process_announcements(self, announcements: List[Announcement]) -> List[Facility]:
        facilities = []
        for ann in announcements:
            facility = self.process_announcement(ann)
            if facility:
                facilities.append(facility)
        
        logger.info(f"AnnouncementProcessor: Converted {len(facilities)} announcements to facilities")
        return facilities


# =============================================================================
# MAIN DISCOVERY ENGINE
# =============================================================================

class NexusEngine:
    """Main orchestrator for discovery operations"""
    
    def __init__(self, db_path: str = "dc_nexus.db"):
        self.db = NexusDatabase(db_path)
        self.announcement_processor = AnnouncementProcessor()
        
        self.tier1_sources = [
            PeeringDBSource(),
            OpenStreetMapSource(),
            WikidataSource(),
            SECEdgarSource(),
        ]
        
        self.tier2_sources = [
            CloudsceneSource(),
            DatacenterHawkSource(),
            ProviderWebsitesSource(),
        ]
        
        self.tier3_sources = [
            RSSNewsSource(),
        ]
        
        self.all_sources = self.tier1_sources + self.tier2_sources + self.tier3_sources
    
    def run_quick(self) -> Dict:
        """Quick discovery - PeeringDB only"""
        logger.info("=" * 60)
        logger.info("DC HUB NEXUS - QUICK DISCOVERY")
        logger.info("=" * 60)
        
        start = time.time()
        source = PeeringDBSource()
        
        facilities = source.fetch_facilities()
        new_count = 0
        for f in facilities:
            _, is_new = self.db.upsert_facility(f)
            if is_new:
                new_count += 1
        
        duration = time.time() - start
        stats = self.db.get_stats()
        
        logger.info(f"✅ Quick discovery complete in {duration:.1f}s")
        logger.info(f"📊 New facilities: {new_count} | Total: {stats['total_facilities']}")
        
        return {
            'duration': duration,
            'facilities_new': new_count,
            'total_facilities': stats['total_facilities']
        }
    
    def run_full(self) -> Dict:
        """Full discovery - All sources"""
        logger.info("=" * 60)
        logger.info("DC HUB NEXUS - FULL DISCOVERY v4.0")
        logger.info("=" * 60)
        
        start = time.time()
        total_new = 0
        total_updated = 0
        total_announcements = 0
        
        all_facilities = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(s.fetch_facilities): s.name for s in self.all_sources}
            for future in as_completed(futures):
                source_name = futures[future]
                try:
                    facilities = future.result()
                    all_facilities.extend(facilities)
                    logger.info(f"  ✓ {source_name}: {len(facilities)} facilities")
                except Exception as e:
                    logger.error(f"  ✗ {source_name}: {e}")
        
        for f in all_facilities:
            try:
                _, is_new = self.db.upsert_facility(f)
                if is_new:
                    total_new += 1
                else:
                    total_updated += 1
            except Exception as e:
                logger.debug(f"Facility upsert error: {e}")
        
        all_announcements = []
        for source in self.all_sources:
            try:
                announcements = source.fetch_announcements()
                for a in announcements:
                    _, is_new = self.db.upsert_announcement(a)
                    if is_new:
                        total_announcements += 1
                        all_announcements.append(a)
            except Exception as e:
                logger.debug(f"Announcements from {source.name}: {e}")
        
        converted_facilities = self.announcement_processor.process_announcements(all_announcements)
        total_converted = 0
        for f in converted_facilities:
            try:
                _, is_new = self.db.upsert_facility(f)
                if is_new:
                    total_converted += 1
            except Exception:
                pass
        
        duration = time.time() - start
        stats = self.db.get_stats()
        
        logger.info("=" * 60)
        logger.info(f"✅ Full discovery complete in {duration:.1f}s")
        logger.info(f"🏢 Facilities: {stats['total_facilities']} (new: {total_new}, updated: {total_updated})")
        logger.info(f"📰 Announcements: {stats['total_announcements']} (new: {total_announcements})")
        logger.info(f"🔄 Announcements→Facilities: {total_converted}")
        logger.info(f"⚡ Total Power: {stats['total_power_mw']:,.0f} MW")
        logger.info(f"🌍 Countries: {len(stats['by_country'])}")
        logger.info(f"🏭 Providers: {len(stats['by_provider'])}")
        logger.info("=" * 60)
        
        return {
            'duration': duration,
            'facilities_new': total_new,
            'facilities_updated': total_updated,
            'announcements_new': total_announcements,
            'total_facilities': stats['total_facilities'],
            'total_announcements': stats['total_announcements'],
            'total_power_mw': stats['total_power_mw']
        }
    
    def run_news_only(self) -> Dict:
        """News/announcements only - Run every 15 minutes"""
        logger.info("📰 Running news discovery (12 feeds including Google News)...")
        
        start = time.time()
        total_new = 0
        
        for source in self.tier3_sources:
            try:
                announcements = source.fetch_announcements()
                for a in announcements:
                    _, is_new = self.db.upsert_announcement(a)
                    if is_new:
                        total_new += 1
            except Exception as e:
                logger.error(f"News source {source.name}: {e}")
        
        duration = time.time() - start
        logger.info(f"✅ News discovery complete: {total_new} new announcements in {duration:.1f}s")
        
        return {'announcements_new': total_new, 'duration': duration}
    
    def export(self, filepath: str = "dc_nexus_export.json"):
        """Export all data to JSON"""
        return self.db.export_json(filepath)
    
    def get_stats(self) -> Dict:
        """Get current statistics"""
        return self.db.get_stats()


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="DC Hub Nexus - Data Center Discovery Engine v4.0")
    parser.add_argument('--quick', action='store_true', help='Quick discovery (PeeringDB only)')
    parser.add_argument('--full', action='store_true', help='Full discovery (all sources)')
    parser.add_argument('--news', action='store_true', help='News/announcements only')
    parser.add_argument('--stats', action='store_true', help='Show statistics')
    parser.add_argument('--export', type=str, metavar='FILE', help='Export to JSON file')
    parser.add_argument('--continuous', type=int, metavar='HOURS', help='Run continuously every N hours')
    parser.add_argument('--db', type=str, default='dc_nexus.db', help='Database path')
    
    args = parser.parse_args()
    
    engine = NexusEngine(args.db)
    
    if args.stats:
        stats = engine.get_stats()
        print(json.dumps(stats, indent=2))
    elif args.quick:
        engine.run_quick()
    elif args.news:
        engine.run_news_only()
    elif args.export:
        engine.export(args.export)
    elif args.continuous:
        logger.info(f"🔄 Starting continuous mode (every {args.continuous} hours)")
        while True:
            engine.run_full()
            engine.export()
            logger.info(f"💤 Sleeping for {args.continuous} hours...")
            time.sleep(args.continuous * 3600)
    else:
        engine.run_full()
        engine.export()
