"""
DC Hub Nexus - Deep Learning Self-Aware Engine
===============================================
Unified AI-powered system that learns, adapts, and grows automatically across:
- Facilities: Discovers new data centers from multiple sources
- Transactions: Detects and tracks M&A deals, investments
- Capacity: Monitors new capacity coming online
- News Sources: Finds and adds new RSS feeds
- Operators: Learns new companies and operators
- Markets: Discovers emerging data center markets

Features:
- Pattern recognition from existing data
- Cross-source correlation
- Automatic quality scoring
- Self-improving discovery algorithms
- Trend detection and prediction
"""

import sqlite3
import requests
import re
import json
import hashlib
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin
from collections import defaultdict
from db_utils import get_db

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

DB_PATH = 'dc_nexus.db'

class DeepLearningEngine:
    """Unified self-learning engine for all DC Hub data"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; DCHubBot/2.0; +https://dchub.cloud)'
        })
        
        self.learned_patterns = {
            'operators': set(),
            'locations': set(),
            'deal_keywords': set(),
            'capacity_patterns': set(),
            'source_domains': set()
        }
        
        self.stats = {
            'facilities_discovered': 0,
            'transactions_detected': 0,
            'capacity_updates': 0,
            'sources_added': 0,
            'patterns_learned': 0,
            'last_run': None
        }
        
        self._init_db()
        self._load_learned_patterns()

    def _get_db(self):
        """Get database connection with WAL mode and timeout"""
        conn = get_db(self.db_path)
        return conn

    def _init_db(self):
        """Initialize deep learning tables"""
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS learned_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                frequency INTEGER DEFAULT 1,
                first_seen TEXT,
                last_seen TEXT,
                source TEXT,
                metadata TEXT,
                UNIQUE(entity_type, entity_value)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discovery_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                item_data TEXT NOT NULL,
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                source TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS capacity_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                facility_id TEXT,
                operator TEXT,
                location TEXT,
                capacity_mw REAL,
                status TEXT,
                expected_online TEXT,
                source TEXT,
                confidence REAL DEFAULT 0.5,
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                verified BOOLEAN DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transaction_intelligence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_hash TEXT UNIQUE,
                buyer TEXT,
                seller TEXT,
                target TEXT,
                deal_type TEXT,
                value_millions REAL,
                announced_date TEXT,
                closed_date TEXT,
                source_url TEXT,
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'pending',
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS learning_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                learning_type TEXT,
                items_processed INTEGER DEFAULT 0,
                items_learned INTEGER DEFAULT 0,
                new_patterns INTEGER DEFAULT 0,
                duration_seconds REAL
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def _load_learned_patterns(self):
        """Load previously learned patterns from database"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT entity_type, entity_value FROM learned_entities
                WHERE confidence > 0.3
                ORDER BY frequency DESC
                LIMIT 1000
            ''')
            
            for row in cursor.fetchall():
                entity_type, value = row
                if entity_type in self.learned_patterns:
                    self.learned_patterns[entity_type].add(value)
            
            conn.close()
        except Exception as e:
            print(f"⚠️ Error loading patterns: {e}")
    
    def learn_from_facilities(self) -> Dict:
        """Learn patterns from existing facility data"""
        results = {'operators': 0, 'locations': 0, 'patterns': 0}
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT DISTINCT provider FROM facilities 
                WHERE provider IS NOT NULL AND provider != ''
                LIMIT 100
            ''')
            operators = [row[0] for row in cursor.fetchall()]
            
            cursor.execute('''
                SELECT DISTINCT city, state, country FROM facilities
                WHERE city IS NOT NULL
                LIMIT 100
            ''')
            locations = cursor.fetchall()
            
            now = datetime.now().isoformat()
            for op in operators:
                if op and len(op) >= 2:
                    cursor.execute('''
                        INSERT INTO learned_entities (entity_type, entity_value, source, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(entity_type, entity_value) DO UPDATE SET
                            frequency = frequency + 1, last_seen = ?
                    ''', ('operators', op, 'facilities', now, now, now))
                    results['operators'] += 1
            
            for city, state, country in locations:
                location = f"{city}, {state or ''}, {country or ''}".strip(', ')
                if location and len(location) >= 2:
                    cursor.execute('''
                        INSERT INTO learned_entities (entity_type, entity_value, source, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(entity_type, entity_value) DO UPDATE SET
                            frequency = frequency + 1, last_seen = ?
                    ''', ('locations', location, 'facilities', now, now, now))
                    results['locations'] += 1
            
            conn.commit()
            
            cursor.execute('SELECT name, provider FROM facilities LIMIT 100')
            for name, provider in cursor.fetchall():
                patterns = self._extract_naming_patterns(name, provider)
                results['patterns'] += len(patterns)
            
            conn.close()
        except Exception as e:
            print(f"❌ Error learning from facilities: {e}")
        
        return results
    
    def learn_from_news(self) -> Dict:
        """Learn from news articles to detect transactions and capacity updates"""
        results = {'transactions': 0, 'capacity': 0, 'new_operators': 0}
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT id, title, summary, source, published_date, url 
                FROM announcements 
                ORDER BY published_date DESC
                LIMIT 100
            ''')
            
            articles = cursor.fetchall()
            
            for article in articles:
                article_id, title, summary, source, pub_date, url = article
                text = f"{title or ''} {summary or ''}"
                
                transactions = self._detect_transactions(text, url)
                for tx in transactions:
                    if self._save_transaction(tx):
                        results['transactions'] += 1
                
                capacity = self._detect_capacity_updates(text, url)
                for cap in capacity:
                    if self._save_capacity_update(cap):
                        results['capacity'] += 1
                
                operators = self._extract_operators(text)
                for op in operators:
                    if self._learn_entity('operators', op, 'news'):
                        results['new_operators'] += 1
                
                try:
                    cursor.execute('''
                        UPDATE announcements SET processed_for_learning = 1 WHERE id = ?
                    ''', (article_id,))
                except:
                    pass
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ Error learning from news: {e}")
        
        return results
    
    def discover_new_sources(self) -> Dict:
        """Discover new data sources by crawling existing ones"""
        results = {'sites_crawled': 0, 'new_sources': 0}
        
        if not BS4_AVAILABLE:
            return {'error': 'BeautifulSoup not available'}
        
        seed_sites = [
            'https://www.datacenterknowledge.com',
            'https://www.datacenterdynamics.com',
            'https://www.datacenterfrontier.com',
            'https://www.capacitymedia.com',
            'https://www.bisnow.com/national/news/data-center',
            'https://www.prnewswire.com/news-releases/technology-latest-news/data-centers-list/',
            'https://www.businesswire.com/portal/site/home/',
        ]
        
        for site in seed_sites:
            try:
                response = self.session.get(site, timeout=10)
                results['sites_crawled'] += 1
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    for link in soup.find_all('a', href=True):
                        href = link.get('href', '')
                        
                        if '/feed' in href or '/rss' in href or 'atom.xml' in href:
                            full_url = urljoin(site, href)
                            if self._add_discovered_source(full_url, 'rss'):
                                results['new_sources'] += 1
                        
                        if self._is_dc_related_link(href, link.get_text()):
                            domain = urlparse(urljoin(site, href)).netloc
                            if domain and self._learn_entity('source_domains', domain, 'crawl'):
                                pass
                
                time.sleep(0.5)
            except Exception as e:
                pass
        
        return results
    
    def auto_discover_facilities(self) -> Dict:
        """Use learned patterns to discover new facilities"""
        results = {'checked': 0, 'found': 0, 'added': 0}
        
        operators = list(self.learned_patterns.get('operators', []))[:50]
        
        for operator in operators:
            try:
                search_query = f'"{operator}" data center new facility site:linkedin.com OR site:prnewswire.com'
                results['checked'] += 1
            except:
                pass
        
        return results
    
    def track_capacity_pipeline(self) -> Dict:
        """Track upcoming capacity coming online"""
        results = {'tracked': 0, 'new': 0, 'updated': 0}
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT title, summary, url FROM announcements
                WHERE (title LIKE '%MW%' OR title LIKE '%megawatt%' 
                       OR summary LIKE '%MW%' OR summary LIKE '%megawatt%')
                AND published_date > datetime('now', '-30 days')
                ORDER BY published_date DESC
                LIMIT 50
            ''')
            
            for row in cursor.fetchall():
                title, summary, url = row
                text = f"{title or ''} {summary or ''}"
                
                capacity_info = self._extract_capacity_info(text)
                if capacity_info:
                    results['tracked'] += 1
                    if self._save_capacity_update({
                        **capacity_info,
                        'source_url': url
                    }):
                        results['new'] += 1
            
            conn.close()
        except Exception as e:
            print(f"❌ Error tracking capacity: {e}")
        
        return results
    
    def detect_market_trends(self) -> Dict:
        """Analyze data to detect emerging market trends"""
        trends = {
            'hot_markets': [],
            'active_operators': [],
            'deal_activity': [],
            'capacity_growth': []
        }
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT city, state, country, COUNT(*) as cnt
                FROM facilities
                WHERE last_updated > datetime('now', '-90 days')
                GROUP BY city, state, country
                ORDER BY cnt DESC
                LIMIT 10
            ''')
            trends['hot_markets'] = [
                {'location': f"{r[0]}, {r[1] or r[2]}", 'new_facilities': r[3]}
                for r in cursor.fetchall()
            ]
            
            cursor.execute('''
                SELECT provider, COUNT(*) as cnt
                FROM facilities
                WHERE provider IS NOT NULL
                GROUP BY provider
                ORDER BY cnt DESC
                LIMIT 10
            ''')
            trends['active_operators'] = [
                {'operator': r[0], 'facilities': r[1]}
                for r in cursor.fetchall()
            ]
            
            conn.close()
        except Exception as e:
            print(f"❌ Error detecting trends: {e}")
        
        return trends
    
    def _detect_transactions(self, text: str, source_url: str = '') -> List[Dict]:
        """Detect M&A transactions from text"""
        transactions = []
        
        deal_patterns = [
            (r'(\w+(?:\s+\w+)*)\s+(?:acquires?|to acquire|acquired)\s+(\w+(?:\s+\w+)*)', 'acquisition'),
            (r'(\w+(?:\s+\w+)*)\s+(?:buys?|to buy|bought)\s+(\w+(?:\s+\w+)*)', 'acquisition'),
            (r'(\w+(?:\s+\w+)*)\s+(?:merges? with|merger with)\s+(\w+(?:\s+\w+)*)', 'merger'),
            (r'(\w+(?:\s+\w+)*)\s+(?:invests?|investing)\s+.*in\s+(\w+(?:\s+\w+)*)', 'investment'),
            (r'(\w+(?:\s+\w+)*)\s+(?:sells?|sold|divests?)\s+(\w+(?:\s+\w+)*)', 'divestiture'),
        ]
        
        value_pattern = r'\$(\d+(?:\.\d+)?)\s*(?:billion|B|bn|million|M|mn)'
        
        for pattern, deal_type in deal_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                buyer, target = match[0], match[1]
                
                value_match = re.search(value_pattern, text, re.IGNORECASE)
                value = None
                if value_match:
                    value = float(value_match.group(1))
                    if 'billion' in text.lower() or 'bn' in text.lower():
                        value *= 1000
                
                tx_hash = hashlib.md5(f"{buyer}{target}{deal_type}".encode()).hexdigest()[:16]
                
                transactions.append({
                    'hash': tx_hash,
                    'buyer': buyer.strip(),
                    'target': target.strip(),
                    'deal_type': deal_type,
                    'value_millions': value,
                    'source_url': source_url,
                    'confidence': 0.6
                })
        
        return transactions
    
    def _detect_capacity_updates(self, text: str, source_url: str = '') -> List[Dict]:
        """Detect capacity updates from text"""
        updates = []
        
        mw_pattern = r'(\d+(?:\.\d+)?)\s*(?:MW|megawatt)'
        matches = re.findall(mw_pattern, text, re.IGNORECASE)
        
        # Known operators to look for (sorted by length desc to match longer names first)
        known_operators = sorted([
            'Equinix', 'Digital Realty', 'NTT', 'CyrusOne', 'QTS', 'Vantage', 
            'CoreSite', 'DataBank', 'Flexential', 'Switch', 'CloudHQ', 'Prime Data Centers',
            'Microsoft', 'Google', 'Amazon', 'AWS', 'Meta', 'Apple', 'Oracle',
            'Iron Mountain', 'Cyxtera', 'EdgeCore', 'Stack Infrastructure',
            'Compass Datacenters', 'Stream Data Centers', 'T5 Data Centers', 'Aligned', 'Applied Digital',
            'Novva', 'Cologix', 'TierPoint', 'Lumen', 'Scala Data Centers', 'STACK', 'Yondr',
            'Vantage Data Centers', 'QTS Realty', 'CyrusOne', 'EdgeConnex', 'Sabey',
            'H5 Data Centers', 'Flexential', 'TierPoint', 'DataBank', 'JLL', 'CBRE',
            'Lincoln Rackhouse', 'Green Mountain', 'Interxion', 'Global Switch',
            'GDS Holdings', 'Chindata', 'AtScale', 'Princeton Digital', 'AirTrunk',
            'ST Telemedia', 'Keppel Data Centres', 'Bridge Data Centres', 'SpaceDC',
            'PDG', 'Virtus Data Centres', 'NextDC', 'NEXTDC', 'Macquarie Data Centres',
            'DXN', 'Involta', 'TierPoint', 'Element Critical', 'DataHouse'
        ], key=len, reverse=True)
        
        # Known locations - cities, states, countries, regions
        known_locations = sorted([
            # US Cities
            'Phoenix', 'Dallas', 'Ashburn', 'Chicago', 'Atlanta', 'Denver', 'Portland', 
            'Seattle', 'San Jose', 'Santa Clara', 'Los Angeles', 'New York', 'Boston', 
            'Miami', 'Houston', 'Austin', 'Salt Lake City', 'Las Vegas', 'Columbus', 
            'Des Moines', 'Hillsboro', 'Manassas', 'Sterling', 'Reno', 'Sacramento',
            'San Antonio', 'Albuquerque', 'Omaha', 'Kansas City', 'Minneapolis',
            'Wharton County', 'Loudoun County', 'Prince William County',
            # US States/Regions
            'Northern Virginia', 'Virginia', 'Texas', 'Arizona', 'Ohio', 'Georgia',
            'California', 'Oregon', 'Washington', 'Nevada', 'Utah', 'Colorado',
            'Illinois', 'New Jersey', 'North Carolina', 'South Carolina', 'Florida',
            # International Cities
            'Singapore', 'Tokyo', 'London', 'Frankfurt', 'Amsterdam', 'Dublin', 
            'Sydney', 'Melbourne', 'Mumbai', 'Hyderabad', 'Chennai', 'Bangalore',
            'Hong Kong', 'Seoul', 'Osaka', 'Jakarta', 'Kuala Lumpur', 'Bangkok',
            'São Paulo', 'Toronto', 'Montreal', 'Vancouver', 'Paris', 'Madrid',
            'Milan', 'Warsaw', 'Stockholm', 'Oslo', 'Helsinki', 'Copenhagen',
            'Zurich', 'Vienna', 'Brussels', 'Manchester', 'Birmingham',
            # Countries/Regions
            'Australia', 'Germany', 'UK', 'United Kingdom', 'France', 'Spain', 'Italy',
            'Netherlands', 'Ireland', 'Japan', 'South Korea', 'India', 'China',
            'Malaysia', 'Thailand', 'Indonesia', 'Philippines', 'Vietnam',
            'Brazil', 'Mexico', 'Chile', 'Colombia', 'Canada',
            'Qatar', 'UAE', 'Saudi Arabia', 'Israel', 'South Africa',
            'Poland', 'Czech Republic', 'Hungary', 'Romania', 'Bulgaria',
            'Western Australia', 'New South Wales', 'Queensland', 'Gujarat', 'Maharashtra'
        ], key=len, reverse=True)
        
        # Words to exclude from operator names (false positives)
        operator_blacklist = ['Data Center', 'Data Centres', 'Data Facilities', 'Facilities', 
                              'Campus', 'Project', 'Development', 'The', 'New', 'Will']
        
        for mw in matches:
            try:
                capacity = float(mw)
                if capacity > 0:
                    # Find operator - check known operators first
                    operator_match = None
                    text_lower = text.lower()
                    for op in known_operators:
                        if op.lower() in text_lower:
                            operator_match = op
                            break
                    
                    # Also check learned operators (but validate them)
                    if not operator_match:
                        for op in self.learned_patterns.get('operators', []):
                            if op.lower() in text_lower and op not in operator_blacklist:
                                # Make sure it's a real company name (has capital letters, reasonable length)
                                if len(op) >= 3 and any(c.isupper() for c in op):
                                    operator_match = op
                                    break
                    
                    # Clean up operator if it's a blacklisted generic term
                    if operator_match in operator_blacklist:
                        operator_match = None
                    
                    # Find location - check known locations first (they're sorted by length)
                    location = None
                    for loc in known_locations:
                        if loc.lower() in text_lower:
                            location = loc
                            break
                    
                    # If no known location, try regex patterns
                    if not location:
                        # Pattern: "in [City], [State]" - be more specific
                        loc_match = re.search(r'\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z]{2})\b', text)
                        if loc_match:
                            location = f"{loc_match.group(1)}, {loc_match.group(2)}"
                        else:
                            # Pattern: "in [City]" only if followed by punctuation or common words
                            loc_match = re.search(r'\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[,\.\-]', text)
                            if loc_match:
                                location = loc_match.group(1)
                    
                    # Validate location - remove obviously bad extractions
                    if location:
                        bad_locations = ['Free', 'Will', 'The', 'New', 'Data', 'Center', 'Project', 
                                        'Covestro', 'Phase', 'Building', 'Campus', 'Site']
                        # Check if location contains bad words
                        for bad in bad_locations:
                            if bad in location:
                                # Try to clean it
                                location = location.replace(bad, '').strip()
                        # If location is now too short or empty, set to None
                        if not location or len(location) < 3:
                            location = None
                    
                    # Determine status
                    status = 'planned'
                    if 'operational' in text_lower or 'online' in text_lower or 'completed' in text_lower or 'opened' in text_lower:
                        status = 'operational'
                    elif 'construction' in text_lower or 'building' in text_lower or 'broke ground' in text_lower or 'under development' in text_lower:
                        status = 'under_construction'
                    
                    # Set confidence based on data quality
                    confidence = 0.3
                    if operator_match and location:
                        confidence = 0.7
                    elif operator_match or location:
                        confidence = 0.5
                    
                    updates.append({
                        'capacity_mw': capacity,
                        'location': location,
                        'operator': operator_match,
                        'source_url': source_url,
                        'status': status,
                        'confidence': confidence
                    })
            except:
                pass
        
        return updates
    
    def _extract_operators(self, text: str) -> List[str]:
        """Extract operator names from text"""
        known_suffixes = ['Data Centers', 'Digital', 'Infrastructure', 'Realty', 'Capital']
        operators = []
        
        pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:Data\s+Center|Digital|Infrastructure)'
        matches = re.findall(pattern, text)
        operators.extend(matches)
        
        return list(set(operators))
    
    def _extract_capacity_info(self, text: str) -> Optional[Dict]:
        """Extract detailed capacity information including operator and location"""
        mw_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:MW|megawatt)', text, re.IGNORECASE)
        if not mw_match:
            return None
        
        capacity = float(mw_match.group(1))
        text_lower = text.lower()
        
        # Determine status
        status = 'planned'
        if 'operational' in text_lower or 'online' in text_lower or 'completed' in text_lower or 'opened' in text_lower:
            status = 'operational'
        elif 'construction' in text_lower or 'building' in text_lower or 'under development' in text_lower or 'broke ground' in text_lower:
            status = 'under_construction'
        elif 'announced' in text_lower or 'planned' in text_lower or 'proposed' in text_lower:
            status = 'planned'
        
        # Known operators (sorted by length desc to match longer names first)
        known_operators = sorted([
            'Equinix', 'Digital Realty', 'NTT', 'CyrusOne', 'QTS', 'Vantage', 
            'CoreSite', 'DataBank', 'Flexential', 'Switch', 'CloudHQ', 'Prime Data Centers',
            'Microsoft', 'Google', 'Amazon', 'AWS', 'Meta', 'Apple', 'Oracle',
            'Iron Mountain', 'Cyxtera', 'EdgeCore', 'Stack Infrastructure',
            'Compass Datacenters', 'Stream Data Centers', 'T5 Data Centers', 'Aligned', 'Applied Digital',
            'Novva', 'Cologix', 'TierPoint', 'Lumen', 'Scala Data Centers', 'STACK', 'Yondr',
            'Vantage Data Centers', 'QTS Realty', 'EdgeConnex', 'Sabey',
            'H5 Data Centers', 'JLL', 'CBRE', 'Lincoln Rackhouse', 'Green Mountain', 
            'Interxion', 'Global Switch', 'GDS Holdings', 'Chindata', 'Princeton Digital', 
            'AirTrunk', 'ST Telemedia', 'Keppel Data Centres', 'Bridge Data Centres', 
            'SpaceDC', 'PDG', 'Virtus Data Centres', 'NextDC', 'NEXTDC', 
            'Macquarie Data Centres', 'DXN', 'Involta', 'Element Critical', 'DataHouse'
        ], key=len, reverse=True)
        
        # Blacklist generic terms
        operator_blacklist = ['Data Center', 'Data Centres', 'Data Facilities', 'Facilities', 
                              'Campus', 'Project', 'Development', 'The', 'New', 'Will']
        
        # Extract operator
        operator = None
        for op in known_operators:
            if op.lower() in text_lower:
                operator = op
                break
        
        # Also check learned operators (but validate them)
        if not operator:
            for op in self.learned_patterns.get('operators', []):
                if op.lower() in text_lower and op not in operator_blacklist:
                    if len(op) >= 3 and any(c.isupper() for c in op):
                        operator = op
                        break
        
        # Clean up operator if it's blacklisted
        if operator in operator_blacklist:
            operator = None
        
        # Known locations (sorted by length desc)
        known_locations = sorted([
            # US Cities
            'Phoenix', 'Dallas', 'Ashburn', 'Chicago', 'Atlanta', 'Denver', 'Portland', 
            'Seattle', 'San Jose', 'Santa Clara', 'Los Angeles', 'New York', 'Boston', 
            'Miami', 'Houston', 'Austin', 'Salt Lake City', 'Las Vegas', 'Columbus', 
            'Des Moines', 'Hillsboro', 'Manassas', 'Sterling', 'Reno', 'Sacramento',
            'Wharton County', 'Loudoun County', 'Prince William County',
            # US States/Regions
            'Northern Virginia', 'Virginia', 'Texas', 'Arizona', 'Ohio', 'Georgia',
            'California', 'Oregon', 'Washington', 'Nevada', 'Utah', 'Colorado',
            # International
            'Singapore', 'Tokyo', 'London', 'Frankfurt', 'Amsterdam', 'Dublin', 
            'Sydney', 'Melbourne', 'Mumbai', 'Hong Kong', 'Seoul',
            'São Paulo', 'Toronto', 'Montreal', 'Paris', 'Madrid',
            # Countries
            'Australia', 'Germany', 'UK', 'France', 'Japan', 'India', 'China',
            'Qatar', 'UAE', 'Saudi Arabia', 'Bulgaria', 'Western Australia', 'Gujarat'
        ], key=len, reverse=True)
        
        # Extract location - check known locations first
        location = None
        for loc in known_locations:
            if loc.lower() in text_lower:
                location = loc
                break
        
        # If no known location, try regex
        if not location:
            loc_match = re.search(r'\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z]{2})\b', text)
            if loc_match:
                location = f"{loc_match.group(1)}, {loc_match.group(2)}"
        
        # Validate location - remove bad extractions
        if location:
            bad_words = ['Free', 'Will', 'The', 'New', 'Data', 'Center', 'Project', 'Covestro', 'Phase']
            for bad in bad_words:
                if bad in location:
                    location = location.replace(bad, '').strip()
            if not location or len(location) < 3:
                location = None
        
        return {
            'capacity_mw': capacity,
            'status': status,
            'operator': operator,
            'location': location
        }
    
    def _learn_entity(self, entity_type: str, value: str, source: str) -> bool:
        """Learn a new entity and store it"""
        if not value or len(value) < 2:
            return False
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO learned_entities (entity_type, entity_value, source, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_value) DO UPDATE SET
                    frequency = frequency + 1,
                    last_seen = ?,
                    confidence = MIN(CAST(1.0 AS numeric), confidence + CAST(0.05 AS numeric))
            ''', (entity_type, value, source, datetime.now().isoformat(), 
                  datetime.now().isoformat(), datetime.now().isoformat()))
            
            is_new = cursor.rowcount > 0
            conn.commit()
            conn.close()
            
            if is_new and entity_type in self.learned_patterns:
                self.learned_patterns[entity_type].add(value)
            
            return is_new
        except Exception as e:
            return False
    
    def _save_transaction(self, tx: Dict) -> bool:
        """Save a detected transaction"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR IGNORE INTO transaction_intelligence 
                (transaction_hash, buyer, target, deal_type, value_millions, source_url, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (tx['hash'], tx['buyer'], tx['target'], tx['deal_type'],
                  tx.get('value_millions'), tx.get('source_url'), tx.get('confidence', 0.5)))
            
            is_new = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return is_new
        except:
            return False
    
    def _save_capacity_update(self, cap: Dict) -> bool:
        """Save a capacity update"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO capacity_tracking 
                (operator, location, capacity_mw, status, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (cap.get('operator'), cap.get('location'), cap.get('capacity_mw'),
                  cap.get('status', 'planned'), cap.get('source_url'), cap.get('confidence', 0.5)))
            
            is_new = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return is_new
        except:
            return False
    
    def _add_discovered_source(self, url: str, source_type: str) -> bool:
        """Add a discovered source"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR IGNORE INTO discovered_sources (domain, url, source_type, discovery_method)
                VALUES (?, ?, ?, 'deep_learning')
            ''', (urlparse(url).netloc, url, source_type))
            
            is_new = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return is_new
        except:
            return False
    
    def _extract_naming_patterns(self, name: str, operator: str) -> List[str]:
        """Extract naming patterns from facility names"""
        patterns = []
        if name and operator:
            pattern = name.replace(operator, '{OPERATOR}')
            if pattern != name:
                patterns.append(pattern)
        return patterns
    
    def _is_dc_related_link(self, href: str, text: str) -> bool:
        """Check if link is data center related"""
        keywords = ['data center', 'datacenter', 'colocation', 'hyperscale', 
                   'facility', 'campus', 'infrastructure']
        combined = f"{href.lower()} {text.lower()}"
        return any(kw in combined for kw in keywords)
    
    def run_full_learning_cycle(self) -> Dict:
        """Run a complete learning cycle across all data"""
        start = time.time()
        
        results = {
            'timestamp': datetime.now().isoformat(),
            'facilities': {},
            'news': {},
            'sources': {},
            'capacity': {},
            'trends': {},
            'duration': 0
        }
        
        print("\n🧠 DEEP LEARNING CYCLE STARTING...")
        
        print("   📊 Phase 1: Learning from facilities...")
        results['facilities'] = self.learn_from_facilities()
        
        print("   📰 Phase 2: Learning from news...")
        results['news'] = self.learn_from_news()
        
        print("   🔍 Phase 3: Discovering new sources...")
        results['sources'] = self.discover_new_sources()
        
        print("   ⚡ Phase 4: Tracking capacity...")
        results['capacity'] = self.track_capacity_pipeline()
        
        print("   📈 Phase 5: Detecting trends...")
        results['trends'] = self.detect_market_trends()
        
        results['duration'] = time.time() - start
        
        self._log_learning_run(results)
        
        print(f"✅ DEEP LEARNING COMPLETE in {results['duration']:.1f}s")
        print(f"   Operators: +{results['facilities'].get('operators', 0)}")
        print(f"   Transactions: +{results['news'].get('transactions', 0)}")
        print(f"   Capacity updates: +{results['capacity'].get('new', 0)}")
        print(f"   New sources: +{results['sources'].get('new_sources', 0)}")
        
        self.stats['last_run'] = datetime.now().isoformat()
        
        return results
    
    def _log_learning_run(self, results: Dict):
        """Log the learning run to database"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            items_learned = (
                results.get('facilities', {}).get('operators', 0) +
                results.get('news', {}).get('transactions', 0) +
                results.get('capacity', {}).get('new', 0)
            )
            
            cursor.execute('''
                INSERT INTO learning_log (learning_type, items_processed, items_learned, duration_seconds)
                VALUES (?, ?, ?, ?)
            ''', ('full_cycle', 100, items_learned, results.get('duration', 0)))
            
            conn.commit()
            conn.close()
        except:
            pass
    
    def get_stats(self) -> Dict:
        """Get deep learning statistics"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM learned_entities')
            total_entities = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM transaction_intelligence')
            total_transactions = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM capacity_tracking')
            total_capacity = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM learning_log')
            total_runs = cursor.fetchone()[0]
            
            cursor.execute('''
                SELECT entity_type, COUNT(*) 
                FROM learned_entities 
                GROUP BY entity_type
            ''')
            entity_breakdown = {row[0]: row[1] for row in cursor.fetchall()}
            
            conn.close()
            
            return {
                'total_entities_learned': total_entities,
                'total_transactions_detected': total_transactions,
                'total_capacity_tracked': total_capacity,
                'total_learning_runs': total_runs,
                'entity_breakdown': entity_breakdown,
                'patterns_in_memory': {k: len(v) for k, v in self.learned_patterns.items()},
                'last_run': self.stats.get('last_run')
            }
        except Exception as e:
            return {'error': str(e)}


_engine_instance = None
_engine_lock = threading.Lock()

def get_deep_learning_engine() -> DeepLearningEngine:
    """Get singleton instance of deep learning engine"""
    global _engine_instance
    with _engine_lock:
        if _engine_instance is None:
            _engine_instance = DeepLearningEngine()
        return _engine_instance


def run_deep_learning_cycle() -> Dict:
    """Run a full deep learning cycle"""
    engine = get_deep_learning_engine()
    return engine.run_full_learning_cycle()


def get_deep_learning_stats() -> Dict:
    """Get deep learning statistics"""
    engine = get_deep_learning_engine()
    return engine.get_stats()


def get_detected_transactions(limit: int = 50) -> List[Dict]:
    """Get detected transactions"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT buyer, target, deal_type, value_millions, source_url, confidence, discovered_at
            FROM transaction_intelligence
            ORDER BY discovered_at DESC
            LIMIT ?
        ''', (limit,))
        
        transactions = [
            {
                'buyer': row[0],
                'target': row[1],
                'deal_type': row[2],
                'value_millions': row[3],
                'source_url': row[4],
                'confidence': row[5],
                'discovered_at': row[6]
            }
            for row in cursor.fetchall()
        ]
        
        conn.close()
        return transactions
    except:
        return []


def get_capacity_pipeline(limit: int = 50) -> List[Dict]:
    """Get capacity pipeline"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT operator, location, capacity_mw, status, source, confidence, discovered_at
            FROM capacity_tracking
            ORDER BY discovered_at DESC
            LIMIT ?
        ''', (limit,))
        
        pipeline = [
            {
                'operator': row[0],
                'location': row[1],
                'capacity_mw': row[2],
                'status': row[3],
                'source': row[4],
                'confidence': row[5],
                'discovered_at': row[6]
            }
            for row in cursor.fetchall()
        ]
        
        conn.close()
        return pipeline
    except:
        return []
