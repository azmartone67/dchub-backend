"""
DC Hub Nexus - Self-Learning Discovery System
==============================================
Automatically discovers new data center sources beyond manually configured ones.
Learns from existing data to find related sites, APIs, and directories.

Features:
- Web crawling to find new data center directories
- API endpoint discovery
- Domain pattern learning
- Source quality scoring
- Auto-disabling of bad sources
"""

import requests
import sqlite3
import re
import hashlib
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from html import unescape
from db_utils import get_db

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

DB_PATH = 'dc_nexus.db'

# Seed domains to crawl for discovery
SEED_DOMAINS = [
    'datacentermap.com',
    'cloudscene.com',
    'baxtel.com',
    'peeringdb.com',
    'datacenterknowledge.com',
    'datacenterdynamics.com',
    'datacenterfrontier.com',
    'capacitymedia.com',
    'uptime.site',
    'colocationamerica.com',
    'datacenterlocations.com',
    'datacenterintelligencegroup.com',
    'bnppartnersrealestate.com',
    'jll.com',
    'cbre.com',
    'cushmanwakefield.com',
    'datacenterrealestate.com',
    'structure.is',
    'datacenterhawk.com',
    'afcom.com',
    'datacenters.com',
    'us.datacenters.com',
    'datacenterjournal.com',
    'insidebigdata.com',
    'enterpriseai.news',
    'hpcwire.com',
]

# Priority industry partners to engage (potential citation sources)
INDUSTRY_PARTNERS = [
    {'domain': 'datacenterhawk.com', 'type': 'directory', 'contact': 'partnerships@datacenterhawk.com'},
    {'domain': 'datacenters.com', 'type': 'directory', 'contact': 'info@datacenters.com'},
    {'domain': 'datacenterfrontier.com', 'type': 'news', 'contact': 'editor@datacenterfrontier.com'},
    {'domain': 'datacenterknowledge.com', 'type': 'news', 'contact': 'tips@datacenterknowledge.com'},
    {'domain': 'datacenterdynamics.com', 'type': 'news', 'contact': 'editorial@datacenterdynamics.com'},
    {'domain': 'cloudscene.com', 'type': 'directory', 'contact': 'hello@cloudscene.com'},
]

# Government and permit monitoring sources
PERMIT_SOURCES = [
    {'url': 'https://planning.loudoun.gov/search', 'region': 'Northern Virginia'},
    {'url': 'https://permits.pima.gov', 'region': 'Phoenix'},
    {'url': 'https://elpasoco.com/building', 'region': 'Colorado Springs'},
]

# Corporate press release feeds
PRESS_RELEASE_SOURCES = [
    'businesswire.com',
    'prnewswire.com',
    'globenewswire.com',
]

# Keywords that indicate a data center directory or listing
DC_KEYWORDS = [
    'data center', 'datacenter', 'colocation', 'colo',
    'facility', 'facilities', 'directory', 'listings',
    'locations', 'map', 'finder', 'search',
    'provider', 'operators', 'carriers'
]

# URL patterns that indicate facility listings
LISTING_PATTERNS = [
    r'/facilities/?',
    r'/locations/?',
    r'/data-centers/?',
    r'/datacenters/?',
    r'/directory/?',
    r'/list/?',
    r'/map/?',
    r'/search/?',
]

class SelfLearningDiscovery:
    """Self-learning system that discovers new data sources"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; DCHubBot/1.0; +https://dchub.cloud)'
        })
        self.discovered_domains: Set[str] = set()
        self.running = False
        self._init_db()
        
    def _init_db(self):
        """Initialize database tables for self-learning"""
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discovered_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                source_type TEXT DEFAULT 'directory',
                discovery_method TEXT,
                facilities_found INTEGER DEFAULT 0,
                quality_score REAL DEFAULT 0.5,
                last_crawl TEXT,
                crawl_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                enabled BOOLEAN DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discovery_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT UNIQUE NOT NULL,
                pattern_type TEXT,
                success_count INTEGER DEFAULT 0,
                total_tries INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS learned_keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL,
                frequency INTEGER DEFAULT 1,
                relevance_score REAL DEFAULT 0.5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        
    def crawl_for_new_sources(self, max_depth: int = 2) -> Dict:
        """Crawl seed domains to find new data center sources"""
        if not BS4_AVAILABLE:
            return {'error': 'BeautifulSoup not available'}
        
        results = {
            'crawled': 0,
            'new_sources': 0,
            'sources': []
        }
        
        visited = set()
        to_visit = [(domain, 0) for domain in SEED_DOMAINS]
        
        while to_visit and results['crawled'] < 50:  # Limit crawls per run
            domain, depth = to_visit.pop(0)
            
            if domain in visited or depth > max_depth:
                continue
            visited.add(domain)
            
            try:
                url = f'https://{domain}' if not domain.startswith('http') else domain
                response = self.session.get(url, timeout=10)
                results['crawled'] += 1
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Find all links
                    for link in soup.find_all('a', href=True):
                        href = link.get('href', '')
                        text = link.get_text().lower()
                        
                        # Check if link points to a data center resource
                        if self._is_dc_related(href, text):
                            full_url = urljoin(url, href)
                            parsed = urlparse(full_url)
                            new_domain = parsed.netloc
                            
                            if new_domain and new_domain not in self.discovered_domains:
                                if self._add_discovered_source(new_domain, full_url, 'crawl'):
                                    results['new_sources'] += 1
                                    results['sources'].append({
                                        'domain': new_domain,
                                        'url': full_url,
                                        'found_on': domain
                                    })
                                    
                                    # Add to crawl queue
                                    if depth + 1 <= max_depth:
                                        to_visit.append((new_domain, depth + 1))
                                        
                time.sleep(0.5)  # Rate limit
                
            except Exception as e:
                print(f"⚠️ Error crawling {domain}: {e}")
                
        return results
    
    def _is_dc_related(self, href: str, text: str) -> bool:
        """Check if a URL/text is related to data centers"""
        combined = f"{href.lower()} {text}"
        
        # Check keywords
        for keyword in DC_KEYWORDS:
            if keyword in combined:
                return True
        
        # Check URL patterns
        for pattern in LISTING_PATTERNS:
            if re.search(pattern, href, re.IGNORECASE):
                return True
                
        return False
    
    def _add_discovered_source(self, domain: str, url: str, method: str) -> bool:
        """Add a newly discovered source to the database"""
        if domain in self.discovered_domains:
            return False
            
        # Skip known bad domains
        bad_patterns = ['facebook', 'twitter', 'linkedin', 'youtube', 'google']
        if any(p in domain.lower() for p in bad_patterns):
            return False
            
        import time as _time
        for attempt in range(5):
            try:
                conn = get_db(self.db_path)
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT OR IGNORE INTO discovered_sources (domain, url, discovery_method, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (domain, url, method, datetime.now().isoformat()))
                
                if cursor.rowcount > 0:
                    self.discovered_domains.add(domain)
                    conn.commit()
                    conn.close()
                    return True
                    
                conn.close()
                return False
                
            except sqlite3.OperationalError as e:
                if 'locked' in str(e) and attempt < 4:
                    _time.sleep(1.0 * (attempt + 1))
                    continue
                print(f"❌ Error adding source: {e}")
                return False
            except Exception as e:
                print(f"❌ Error adding source: {e}")
                return False
    
    def discover_from_search(self) -> Dict:
        """Use search patterns to find new sources"""
        results = {
            'queries': 0,
            'new_sources': 0,
            'sources': []
        }
        
        search_queries = [
            'data center directory site list',
            'colocation facilities map',
            'data center providers by location',
            'enterprise data centers list',
            'cloud infrastructure facilities',
            'hyperscale data centers directory',
        ]
        
        # Note: This requires a search API key to be fully functional
        # For now, we use pattern-based discovery from known sources
        
        return results
    
    def extract_facilities_from_source(self, domain: str) -> Dict:
        """Try to extract facility data from a discovered source"""
        if not BS4_AVAILABLE:
            return {'error': 'BeautifulSoup not available', 'facilities': []}
        
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT url FROM discovered_sources WHERE domain = ?', (domain,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return {'error': 'Source not found', 'facilities': []}
        
        url = row[0]
        facilities = []
        
        try:
            response = self.session.get(url, timeout=15)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Look for facility patterns
                # Pattern 1: Structured data (JSON-LD)
                for script in soup.find_all('script', type='application/ld+json'):
                    try:
                        import json
                        data = json.loads(script.string)
                        if isinstance(data, dict):
                            if data.get('@type') in ['Place', 'LocalBusiness', 'Organization']:
                                facilities.append(self._extract_from_jsonld(data))
                    except:
                        pass
                
                # Pattern 2: Look for location/address blocks
                for elem in soup.find_all(['div', 'article', 'li'], class_=re.compile(r'facility|location|datacenter|site', re.I)):
                    facility = self._extract_from_element(elem)
                    if facility.get('name'):
                        facilities.append(facility)
                
                # Update source stats
                self._update_source_stats(domain, len(facilities))
                
        except Exception as e:
            self._record_source_error(domain)
            return {'error': str(e), 'facilities': []}
        
        return {
            'domain': domain,
            'facilities': facilities,
            'count': len(facilities)
        }
    
    def _extract_from_jsonld(self, data: dict) -> dict:
        """Extract facility info from JSON-LD"""
        address = data.get('address', {})
        geo = data.get('geo', {})
        
        return {
            'name': data.get('name', ''),
            'city': address.get('addressLocality', ''),
            'state': address.get('addressRegion', ''),
            'country': address.get('addressCountry', ''),
            'latitude': geo.get('latitude'),
            'longitude': geo.get('longitude'),
            'source': 'json-ld'
        }
    
    def _extract_from_element(self, elem) -> dict:
        """Extract facility info from HTML element"""
        text = elem.get_text(separator=' ', strip=True)
        
        # Try to find name (usually first heading or bold text)
        name = ''
        name_elem = elem.find(['h1', 'h2', 'h3', 'h4', 'strong', 'b'])
        if name_elem:
            name = name_elem.get_text(strip=True)
        
        # Try to find location patterns
        city = ''
        country = ''
        
        # Common patterns like "City, State, Country" or "City, Country"
        location_pattern = r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*([A-Z]{2}|\w+)'
        match = re.search(location_pattern, text)
        if match:
            city = match.group(1)
            
        return {
            'name': name[:200] if name else '',
            'city': city,
            'raw_text': text[:500],
            'source': 'html'
        }
    
    def _update_source_stats(self, domain: str, facilities_found: int):
        """Update statistics for a source"""
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            # Update crawl stats
            cursor.execute('''
                UPDATE discovered_sources 
                SET facilities_found = facilities_found + ?,
                    crawl_count = crawl_count + 1,
                    last_crawl = ?,
                    quality_score = CASE 
                        WHEN ? > 0 THEN MIN(CAST(1.0 AS numeric), quality_score + CAST(0.1 AS numeric))
                        ELSE MAX(CAST(0.0 AS numeric), quality_score - CAST(0.05 AS numeric))
                    END
                WHERE domain = ?
            ''', (facilities_found, datetime.now().isoformat(), facilities_found, domain))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ Error updating stats: {e}")
    
    def _record_source_error(self, domain: str):
        """Record an error for a source"""
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE discovered_sources 
                SET error_count = error_count + 1,
                    quality_score = MAX(0.0, quality_score - 0.1),
                    enabled = CASE WHEN error_count >= 3 THEN 0 ELSE enabled END
                WHERE domain = ?
            ''', (domain,))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ Error recording error: {e}")
    
    def learn_from_facilities(self) -> Dict:
        """Learn patterns from existing facility data"""
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            # Get operator names to learn patterns
            cursor.execute('''
                SELECT DISTINCT operator FROM facilities 
                WHERE operator IS NOT NULL AND operator != ''
                LIMIT 500
            ''')
            
            operators = [row[0] for row in cursor.fetchall()]
            
            # Extract common patterns
            patterns = {}
            for op in operators:
                # Domain pattern: company.com
                words = re.findall(r'\b\w+\b', op.lower())
                for word in words:
                    if len(word) > 3:
                        patterns[word] = patterns.get(word, 0) + 1
            
            # Save learned keywords
            for keyword, freq in sorted(patterns.items(), key=lambda x: -x[1])[:50]:
                cursor.execute('''
                    INSERT OR REPLACE INTO learned_keywords (keyword, frequency, relevance_score)
                    VALUES (?, ?, ?)
                ''', (keyword, freq, min(1.0, freq / 100)))
            
            conn.commit()
            conn.close()
            
            return {
                'operators_analyzed': len(operators),
                'patterns_learned': len(patterns),
                'top_keywords': sorted(patterns.items(), key=lambda x: -x[1])[:10]
            }
            
        except Exception as e:
            return {'error': str(e)}
    
    def get_stats(self) -> Dict:
        """Get discovery statistics"""
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM discovered_sources')
            total = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM discovered_sources WHERE enabled = 1')
            active = cursor.fetchone()[0]
            
            cursor.execute('SELECT SUM(facilities_found) FROM discovered_sources')
            facilities = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM learned_keywords')
            keywords = cursor.fetchone()[0]
            
            cursor.execute('''
                SELECT domain, facilities_found, quality_score 
                FROM discovered_sources 
                WHERE enabled = 1 
                ORDER BY facilities_found DESC 
                LIMIT 10
            ''')
            top_sources = [
                {'domain': row[0], 'facilities': row[1], 'score': row[2]}
                for row in cursor.fetchall()
            ]
            
            conn.close()
            
            return {
                'total_sources': total,
                'active_sources': active,
                'facilities_discovered': facilities,
                'keywords_learned': keywords,
                'top_sources': top_sources
            }
            
        except Exception as e:
            return {'error': str(e)}
    
    def run_discovery_cycle(self) -> Dict:
        """Run a complete discovery cycle"""
        print("\n🧠 Starting self-learning discovery cycle...")
        
        results = {
            'timestamp': datetime.now().isoformat(),
            'crawl': {},
            'learning': {},
            'extraction': {}
        }
        
        # Phase 1: Crawl for new sources
        print("  Phase 1: Crawling for new sources...")
        results['crawl'] = self.crawl_for_new_sources(max_depth=1)
        
        # Phase 2: Learn from existing data
        print("  Phase 2: Learning patterns from facilities...")
        results['learning'] = self.learn_from_facilities()
        
        # Phase 3: Extract from discovered sources
        print("  Phase 3: Extracting from discovered sources...")
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT domain FROM discovered_sources 
            WHERE enabled = 1 AND quality_score > 0.3
            ORDER BY quality_score DESC
            LIMIT 5
        ''')
        domains = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        total_facilities = 0
        for domain in domains:
            extraction = self.extract_facilities_from_source(domain)
            total_facilities += extraction.get('count', 0)
        
        results['extraction'] = {
            'sources_processed': len(domains),
            'facilities_found': total_facilities
        }
        
        # Phase 4: Monitor permit sources
        print("  Phase 4: Checking permit sources...")
        results['permits'] = self.monitor_permit_sources()
        
        # Phase 5: Scan press release feeds
        print("  Phase 5: Scanning press releases...")
        results['press_releases'] = self.scan_press_releases()
        
        print(f"✅ Discovery cycle complete: {results['crawl'].get('new_sources', 0)} new sources, {total_facilities} facilities")
        
        return results
    
    def monitor_permit_sources(self) -> Dict:
        """Check government permit databases for data center construction permits"""
        results = {'sources_checked': 0, 'permits_found': 0, 'items': []}
        
        dc_permit_keywords = [
            'data center', 'datacenter', 'server farm', 'colocation',
            'cloud computing', 'hyperscale', 'computing facility'
        ]
        
        for source in PERMIT_SOURCES:
            try:
                url = source.get('url', '')
                region = source.get('region', 'Unknown')
                
                response = self.session.get(url, timeout=15)
                results['sources_checked'] += 1
                
                if response.status_code == 200 and BS4_AVAILABLE:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    text = soup.get_text().lower()
                    
                    for keyword in dc_permit_keywords:
                        if keyword in text:
                            results['permits_found'] += 1
                            results['items'].append({
                                'region': region,
                                'keyword': keyword,
                                'source': url
                            })
                            
                            self._log_permit_discovery(region, keyword, url)
                            break
                            
                time.sleep(1)
                
            except Exception as e:
                print(f"    ⚠️ Error checking permit source {source.get('url', '')}: {e}")
                
        return results
    
    def scan_press_releases(self) -> Dict:
        """Scan press release feeds for data center announcements"""
        results = {'sources_checked': 0, 'releases_found': 0, 'items': []}
        
        press_keywords = [
            'data center', 'hyperscale', 'colocation', 'cloud campus',
            'megawatt', 'MW', 'server farm'
        ]
        
        for domain in PRESS_RELEASE_SOURCES:
            try:
                search_url = f"https://www.{domain}/search?query=data+center"
                
                response = self.session.get(search_url, timeout=15)
                results['sources_checked'] += 1
                
                if response.status_code == 200 and BS4_AVAILABLE:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    for article in soup.find_all(['article', 'div'], class_=re.compile(r'result|item|release', re.I))[:10]:
                        title = article.get_text()[:200]
                        for keyword in press_keywords:
                            if keyword.lower() in title.lower():
                                results['releases_found'] += 1
                                results['items'].append({
                                    'source': domain,
                                    'keyword': keyword,
                                    'title': title[:100]
                                })
                                break
                                
                time.sleep(1)
                
            except Exception as e:
                print(f"    ⚠️ Error scanning {domain}: {e}")
                
        return results
    
    def _log_permit_discovery(self, region: str, keyword: str, url: str):
        """Log permit discovery to database"""
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR IGNORE INTO discovered_sources 
                (domain, url, source_type, discovery_method, created_at)
                VALUES (?, ?, 'permit', 'permit_monitor', CURRENT_TIMESTAMP)
            ''', (f"permit:{region}", url))
            
            conn.commit()
            conn.close()
        except:
            pass


# Thread-safe singleton
_discovery_instance = None
_discovery_lock = threading.Lock()

def get_discovery_instance() -> SelfLearningDiscovery:
    global _discovery_instance
    with _discovery_lock:
        if _discovery_instance is None:
            _discovery_instance = SelfLearningDiscovery()
        return _discovery_instance


def run_self_learning_discovery() -> Dict:
    """Convenience function to run discovery"""
    discovery = get_discovery_instance()
    return discovery.run_discovery_cycle()


def get_discovery_stats() -> Dict:
    """Get discovery statistics"""
    discovery = get_discovery_instance()
    return discovery.get_stats()
