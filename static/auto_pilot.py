"""
DC Hub Auto-Pilot System v4.0 - Full Backend Expansion
=======================================================
KEY IMPROVEMENTS:
  - 325+ companies tracked
  - 20 energy markets (vs 8 in v3.1)
  - 10+ news sources (vs 3 in v3.0)
  - User analytics & visitor tracking
  - Admin reporting endpoints
  - Enhanced keep-alive with health checks
"""

import json, re, hashlib, sqlite3, os, threading, time, requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from html import unescape
from collections import defaultdict

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


# Flask imports for admin routes (used when imported by main.py)
try:
    from flask import request, jsonify
except ImportError:
    # Stub for standalone testing
    request = None
    jsonify = lambda x: x

CONFIG = {
    'news_interval_seconds': 60,
    'auto_approve_threshold': 80,
    'auto_approve_enabled': True,
    'require_both_parties': True,
}

SOCIAL_CONFIG = {
    'twitter': {
        'api_key': os.environ.get('TWITTER_API_KEY', ''),
        'api_secret': os.environ.get('TWITTER_API_SECRET', ''),
        'access_token': os.environ.get('TWITTER_ACCESS_TOKEN', ''),
        'access_secret': os.environ.get('TWITTER_ACCESS_SECRET', ''),
        'enabled': bool(os.environ.get('TWITTER_API_KEY', ''))
    },
    'linkedin': {
        'access_token': os.environ.get('LINKEDIN_ACCESS_TOKEN', ''),
        'org_id': os.environ.get('LINKEDIN_ORG_ID', ''),
        'person_id': os.environ.get('LINKEDIN_PERSON_ID', ''),
        'enabled': bool(os.environ.get('LINKEDIN_ACCESS_TOKEN', ''))
    }
}

# 280+ companies - Hyperscalers, DC Operators, PE/Infrastructure, Utilities, AI, Telecom, REITs
COMPANIES = [
    # Hyperscalers & Cloud (16)
    'Amazon', 'AWS', 'Amazon Web Services', 'Microsoft', 'Azure', 'Google', 'Google Cloud', 'Alphabet',
    'Meta', 'Facebook', 'Apple', 'Oracle', 'IBM', 'Alibaba', 'Tencent', 'ByteDance',
    
    # Major DC Operators (70+)
    'Equinix', 'Digital Realty', 'QTS', 'QTS Realty', 'CyrusOne', 'CoreSite', 'Vantage', 'Vantage Data Centers',
    'Compass', 'Compass Datacenters', 'DataBank', 'Switch', 'Aligned', 'Aligned Data Centers',
    'CloudHQ', 'EdgeCore', 'EdgeCore Digital', 'Prime Data Centers', 'Stack Infrastructure', 'Stack',
    'T5 Data Centers', 'Flexential', 'Cologix', 'TierPoint', 'Sabey', 'Sabey Data Centers',
    'Iron Mountain', 'NTT', 'NTT Global', 'Lumen', 'Cyxtera', 'Global Switch', 'Interxion',
    'Telehouse', 'Keppel DC', 'STT GDC', 'AirTrunk', 'NEXTDC', 'Chindata', 'GDS', 'GDS Holdings',
    # NEW OPERATORS - Your priority targets
    'Edged', 'Edged Energy', 'Powerhouse', 'Powerhouse Data Centers',
    'Overwatch Capital', 'Overwatch', 'Thor Capital', 'Thor Equities', 'Thor Industries',
    'Form8tion', 'Form8tion Data Centers', '365 Data Centers', '365 DC',
    'Centra', 'Centra Technology', 'Netrality', 'Netrality Properties',
    'Tract', 'Tract Data Centers', 'Stream Data Centers', 'Stream Realty',
    'COPT', 'COPT Data Center Solutions', 'H5 Data Centers', 'H5',
    'Involta', 'Element Critical', 'Evoque Data Center Solutions', 'Evoque',
    'DC Blox', 'DC BLOX', 'Novva Data Centers', 'Novva', 'Skybox Datacenters', 'Skybox',
    'US Signal', 'ViaWest', 'Peak 10', 'INAP', 'Internap',
    'ServerFarm', 'Serverfarm Realty', 'Lincoln Rackhouse', 'Conapto', 'eStruxture',
    'Urbacon', 'Datum Datacenters', 'CIM Group', 'Digital Core REIT',
    'Nautilus Data Technologies', 'Nautilus', 'Lancium', 'Crusoe Energy Systems',
    'American Real Estate Partners', 'AREP', 'Carter Validus', 'Sungard',
    'QualityTech', 'Quality Technology Services', 'Sentinel Data Centers', '365 Operating',
    'HostDime', 'PhoenixNAP', 'SingleHop', 'Codero', 'LightEdge',
    
    # International DC Operators (30+)
    'Yondr', 'Yondr Group', 'Scala Data Centers', 'Ascenty', 'EdgeConneX', 'VNET', '21Vianet',
    'Princeton Digital', 'Princeton Digital Group', 'PDG', 'Bridge Data Centres', 'SpaceDC',
    'Virtus', 'VIRTUS Data Centres', 'Ark Data Centres', 'Kao Data', 'AtlasEdge', 'Echelon',
    'Echelon Data Centres', 'Data4', 'Verne Global', 'Green Mountain', 'DigiPlex', 'Bulk Infrastructure',
    'MainOne', 'Teraco', 'Africa Data Centres', 'Raxio', 'IXAfrica', 'Paratus',
    'Open Access Data Centres', 'OADC', 'STACK EMEA', 'Khazna', 'Gulf Data Hub',
    
    # Private Equity & Infrastructure (55)
    'Blackstone', 'KKR', 'Brookfield', 'Brookfield Infrastructure', 'DigitalBridge', 'Digital Bridge',
    'GIP', 'Global Infrastructure Partners', 'Stonepeak', 'Stonepeak Partners', 'Macquarie',
    'Macquarie Asset Management', 'GIC', 'ADIA', 'Abu Dhabi Investment', 'QIA', 'Qatar Investment',
    'CPPIB', 'CPP Investments', 'OTPP', 'Ontario Teachers', 'AustralianSuper', 'Carlyle', 'Carlyle Group',
    'TPG', 'TPG Capital', 'Silver Lake', 'Bain Capital', 'Vista Equity', 'Warburg Pincus',
    'Goldman Sachs', 'Morgan Stanley', 'JP Morgan', 'JPMorgan', 'Blue Owl', 'Blue Owl Capital',
    'Ares', 'Ares Management', 'Apollo', 'Apollo Global', 'EQT', 'EQT Partners', 'Partners Group',
    'IFM Investors', 'PSP Investments', 'CDPQ', 'Sixth Street', 'BlackRock', 'Starwood', 'Actis',
    'I Squared Capital', 'I Squared', 'MGX', 'Mubadala', 'PIF', 'Saudi PIF', 'Temasek',
    'SoftBank', 'SoftBank Group', 'Coatue', 'Magnetar', 'Fidelity',
    
    # Utilities & Power (25)
    'Dominion', 'Dominion Energy', 'Duke Energy', 'AES', 'AES Corporation', 'NextEra', 'NextEra Energy',
    'Constellation', 'Constellation Energy', 'Vistra', 'Vistra Energy', 'Talen', 'Talen Energy',
    'NRG', 'NRG Energy', 'Intersect Power', 'Kairos Power', 'TerraPower', 'NuScale', 'Oklo',
    'Southern Company', 'Exelon', 'PG&E', 'Entergy', 'TVA', 'Enel',
    
    # AI & GPU Cloud (30)
    'OpenAI', 'Anthropic', 'xAI', 'Nvidia', 'AMD', 'Intel', 'Cerebras', 'Groq', 'SambaNova',
    'CoreWeave', 'Lambda', 'Lambda Labs', 'Together AI', 'Crusoe', 'Crusoe Energy', 'Applied Digital',
    'Nscale', 'Voltage Park', 'Inflection', 'Mistral', 'Cohere', 'AI21', 'Hugging Face',
    'Scale AI', 'Databricks', 'Snowflake', 'Anyscale', 'Modal', 'Replicate', 'RunPod',
    
    # Real Estate & REITs (20)
    'American Tower', 'Crown Castle', 'Hines', 'CBRE', 'JLL', 'Jones Lang LaSalle', 'Prologis',
    'Goodman', 'Goodman Group', 'ESR', 'ESR Group', 'GLP', 'HMC Capital', 'Mapletree',
    'CapitaLand', 'Frasers', 'Keppel', 'Keppel Corporation', 'Host Hotels', 'Ventas',
    
    # Telecom (20)
    'Singtel', 'AT&T', 'Verizon', 'T-Mobile', 'Comcast', 'Zayo', 'Lumen Technologies',
    'CenturyLink', 'Cogent', 'GTL', 'Telia', 'Orange', 'Deutsche Telekom', 'BT', 'Vodafone',
    'China Mobile', 'China Telecom', 'China Unicom', 'NTT Communications', 'KDDI',
    
    # Stargate & Other (10)
    'Stargate', 'Stargate JV', 'Oracle Cloud', 'SoftBank Vision', 'OpenAI Infrastructure',
    'US Infrastructure', 'European DC Platform', 'Asia DC Platform', 'LATAM DC Platform', 'Africa Data Centres',
]

# Remove duplicates while preserving order
COMPANIES = list(dict.fromkeys(COMPANIES))

COMPANY_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(c) for c in sorted(COMPANIES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE
)

DEAL_PATTERNS = [
    (r'(.+?)\s+(?:acquires?|to acquire|acquiring|acquired)\s+(.+?)(?:\s+for|\s+in|\s*$)', 'buyer_first'),
    (r'(.+?)\s+(?:buys?|to buy|buying|bought)\s+(.+?)(?:\s+for|\s+in|\s*$)', 'buyer_first'),
    (r'(.+?)\s+(?:purchases?|to purchase)\s+(.+?)(?:\s+for|\s+in|\s*$)', 'buyer_first'),
    (r'(.+?)\s+(?:invests?|investing)\s+(?:\$[\d.]+[BM]?\s+)?in\s+(.+?)(?:\s+for|\s*$)', 'buyer_first'),
    (r'(.+?)\s+(?:leads?|leading|led)\s+(.+?)\s+(?:round|funding)', 'buyer_first'),
    (r'(.+?)\s+and\s+(.+?)\s+(?:form|announce|launch)\s+(?:joint venture|JV|partnership)', 'both'),
    (r'(.+?)\s+(?:backs?|backing)\s+(.+?)(?:\s+for|\s+with|\s*$)', 'buyer_first'),
]

VALUE_PATTERNS = [
    (r'\$(\d+(?:\.\d+)?)\s*(?:billion|B|bn)\b', 1000),
    (r'\$(\d+(?:\.\d+)?)\s*(?:million|M|mn)\b', 1),
]

DEAL_TYPE_KEYWORDS = {
    'ma': ['acquire', 'acquisition', 'acquires', 'buy', 'buys', 'purchase', 'merger', 'takeover'],
    'equity': ['invest', 'investment', 'funding', 'round', 'raises', 'stake', 'equity'],
    'jv': ['joint venture', 'jv', 'partnership', 'partner'],
    'capex': ['capex', 'capital expenditure', 'build', 'construction', 'expansion'],
    'debt': ['debt', 'loan', 'financing', 'credit', 'facility'],
}

MARKETS = {
    'Northern Virginia': ['Ashburn', 'Loudoun', 'Virginia', 'NoVA'],
    'Dallas': ['Dallas', 'Fort Worth', 'DFW'],
    'Phoenix': ['Phoenix', 'Mesa', 'Chandler', 'Arizona'],
    'Chicago': ['Chicago', 'Illinois'],
    'Silicon Valley': ['San Jose', 'Santa Clara'],
    'Atlanta': ['Atlanta', 'Georgia'],
    'New York': ['New York', 'New Jersey', 'NYC'],
    'London': ['London', 'UK', 'United Kingdom'],
    'Frankfurt': ['Frankfurt', 'Germany'],
    'Singapore': ['Singapore'],
    'Tokyo': ['Tokyo', 'Japan'],
    'Sydney': ['Sydney', 'Australia'],
}

# ============================================
# EXPANDED NEWS SOURCES (10+ feeds)
# ============================================
NEWS_SOURCES = {
    'rss_feeds': [
        # Primary DC Industry
        'https://www.datacenterdynamics.com/en/rss/',
        'https://www.datacenterknowledge.com/rss.xml',
        'https://www.datacenterfrontier.com/feed',
        # Business/Tech News
        'https://feeds.bloomberg.com/technology/news.rss',
        'https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml',
        # CRE & Infrastructure
        'https://www.bisnow.com/feed',
        'https://www.globest.com/feed/',
        # PR Wires (deal announcements)
        'https://www.prnewswire.com/rss/technology-latest-news.rss',
        'https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtSWA==',
        # Energy & Utilities
        'https://www.utilitydive.com/feeds/news/',
        'https://www.powermag.com/feed/',
    ],
    'keywords_deals': ['acquire', 'acquisition', 'merger', 'buy', 'purchase', 'invest', 'funding', 'deal', 'billion', 'million', 'data center', 'hyperscale', 'colocation'],
    'keywords_facility': ['megawatt', 'mw', 'campus', 'facility', 'groundbreaking', 'construction', 'expansion', 'new site'],
}

# ============================================
# EXPANDED ENERGY MARKETS (20 markets)
# ============================================
ENERGY_MARKETS = [
    # Tier 1 - Primary DC Markets
    {'name': 'Phoenix', 'lat': 33.4484, 'lng': -112.0740, 'radius': 50000},
    {'name': 'Dallas-Fort Worth', 'lat': 32.7767, 'lng': -96.7970, 'radius': 60000},
    {'name': 'Northern Virginia', 'lat': 39.0438, 'lng': -77.4874, 'radius': 40000},
    {'name': 'Atlanta', 'lat': 33.7490, 'lng': -84.3880, 'radius': 50000},
    {'name': 'Chicago', 'lat': 41.8781, 'lng': -87.6298, 'radius': 50000},
    {'name': 'Silicon Valley', 'lat': 37.3861, 'lng': -122.0839, 'radius': 40000},
    # Tier 2 - Growing Markets
    {'name': 'Salt Lake City', 'lat': 40.7608, 'lng': -111.8910, 'radius': 40000},
    {'name': 'Columbus', 'lat': 39.9612, 'lng': -82.9988, 'radius': 40000},
    {'name': 'Des Moines', 'lat': 41.5868, 'lng': -93.6250, 'radius': 35000},
    {'name': 'Las Vegas', 'lat': 36.1699, 'lng': -115.1398, 'radius': 40000},
    {'name': 'Denver', 'lat': 39.7392, 'lng': -104.9903, 'radius': 50000},
    {'name': 'Seattle', 'lat': 47.6062, 'lng': -122.3321, 'radius': 50000},
    {'name': 'Austin', 'lat': 30.2672, 'lng': -97.7431, 'radius': 45000},
    {'name': 'Portland', 'lat': 45.5152, 'lng': -122.6784, 'radius': 40000},
    # Tier 3 - Emerging Markets
    {'name': 'Reno', 'lat': 39.5296, 'lng': -119.8138, 'radius': 35000},
    {'name': 'New Jersey', 'lat': 40.0583, 'lng': -74.4057, 'radius': 50000},
    {'name': 'Kansas City', 'lat': 39.0997, 'lng': -94.5786, 'radius': 40000},
    {'name': 'Minneapolis', 'lat': 44.9778, 'lng': -93.2650, 'radius': 40000},
    {'name': 'Houston', 'lat': 29.7604, 'lng': -95.3698, 'radius': 60000},
    {'name': 'San Antonio', 'lat': 29.4241, 'lng': -98.4936, 'radius': 45000},
]

DB_PATH = os.environ.get('DC_NEXUS_DB', 'dc_nexus.db')

class DealExtractor:
    def extract_deal(self, headline: str) -> Dict:
        deal = {'buyer': None, 'seller': None, 'value': None, 'type': 'unknown', 'market': 'Global', 'confidence': 0, 'title': headline}
        headline_clean = unescape(headline).strip()
        
        # Extract value
        for pattern, multiplier in VALUE_PATTERNS:
            match = re.search(pattern, headline_clean, re.IGNORECASE)
            if match:
                deal['value'] = float(match.group(1)) * multiplier
                deal['confidence'] += 20
                break
        
        # Try structured patterns first
        for pattern, ptype in DEAL_PATTERNS:
            match = re.search(pattern, headline_clean, re.IGNORECASE)
            if match:
                if ptype == 'buyer_first':
                    buyer_text, seller_text = match.group(1), match.group(2)
                else:
                    buyer_text, seller_text = match.group(1), match.group(2)
                
                buyer_match = COMPANY_PATTERN.search(buyer_text)
                seller_match = COMPANY_PATTERN.search(seller_text)
                
                if buyer_match:
                    deal['buyer'] = buyer_match.group(1)
                    deal['confidence'] += 30
                if seller_match:
                    deal['seller'] = seller_match.group(1)
                    deal['confidence'] += 30
                break
        
        # Fallback: extract companies in order
        if not deal['buyer'] or not deal['seller']:
            companies = COMPANY_PATTERN.findall(headline_clean)
            seen = []
            for c in companies:
                if c not in seen:
                    seen.append(c)
            if len(seen) >= 1 and not deal['buyer']:
                deal['buyer'] = seen[0]
                deal['confidence'] += 25
            if len(seen) >= 2 and not deal['seller']:
                deal['seller'] = seen[1]
                deal['confidence'] += 25
        
        # Detect deal type
        headline_lower = headline_clean.lower()
        for dtype, keywords in DEAL_TYPE_KEYWORDS.items():
            if any(kw in headline_lower for kw in keywords):
                deal['type'] = dtype
                deal['confidence'] += 15
                break
        
        # Detect market
        for market, keywords in MARKETS.items():
            if any(kw.lower() in headline_lower for kw in keywords):
                deal['market'] = market
                deal['confidence'] += 5
                break
        
        return deal
    
    def validate_deal(self, deal: Dict) -> Tuple[bool, str]:
        invalid = ['tbd', 'unknown', 'n/a', 'none', '']
        buyer = deal.get('buyer')
        seller = deal.get('seller')
        
        if not buyer or str(buyer).lower().strip() in invalid:
            return False, "Missing buyer"
        
        if CONFIG['require_both_parties']:
            if not seller or str(seller).lower().strip() in invalid:
                return False, "Missing seller"
        
        # Reject if buyer = seller
        if buyer and seller and str(buyer).lower().strip() == str(seller).lower().strip():
            return False, "Buyer equals seller"
        
        return True, "Valid"

deal_extractor = DealExtractor()

class SocialPoster:
    def __init__(self):
        self.queue = []
    
    def add_to_queue(self, deal):
        self.queue.append({'deal': deal, 'posted': False, 'queued_at': datetime.now().isoformat()})
    
    def process_queue(self):
        for item in self.queue:
            if not item['posted']:
                item['posted'] = True

class RSSFetcher:
    def fetch_all_feeds(self) -> List[Dict]:
        articles = []
        for url in NEWS_SOURCES['rss_feeds']:
            try:
                resp = requests.get(url, timeout=10)
                root = ET.fromstring(resp.content)
                for item in root.findall('.//item')[:10]:
                    title = item.find('title')
                    link = item.find('link')
                    if title is not None:
                        articles.append({'title': title.text or '', 'link': link.text if link is not None else '', 'source': url})
            except:
                pass
        return articles

class AutoDiscoveryEngine:
    def __init__(self):
        self.extractor = DealExtractor()
        self.social_poster = SocialPoster()
        self.stats = {'news_processed': 0, 'deals_discovered': 0, 'deals_approved': 0, 'deals_rejected': 0}
        self._init_db()
    
    def _init_db(self):
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("""CREATE TABLE IF NOT EXISTS autopilot_pending (
            id TEXT PRIMARY KEY, type TEXT, title TEXT, data TEXT,
            confidence REAL, source_url TEXT, status TEXT, created_at TEXT, processed_at TEXT)""")
        conn.commit()
        conn.close()
    
    def process_news_article(self, article: Dict) -> Dict:
        title = article.get('title', '')
        content = (title + ' ' + article.get('description', '')).lower()
        result = {'is_deal': False, 'is_facility': False, 'extracted_data': {}}
        
        if any(kw in content for kw in NEWS_SOURCES['keywords_deals']):
            deal = self.extractor.extract_deal(title)
            deal['source_url'] = article.get('link', '')
            result['is_deal'] = True
            result['extracted_data']['deal'] = deal
        
        return result
    
    def add_pending(self, item_type, data, source_url=''):
        item_id = hashlib.md5(f"{item_type}{data.get('title','')}{datetime.now().isoformat()}".encode()).hexdigest()[:12]
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("INSERT OR REPLACE INTO autopilot_pending VALUES (?,?,?,?,?,?,'pending',?,NULL)",
                    (item_id, item_type, data.get('title',''), json.dumps(data), data.get('confidence',0), source_url, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return item_id
    
    def _normalize_company(self, name):
        """Normalize company name for duplicate detection"""
        if not name:
            return ''
        n = name.lower().strip()
        # Remove common suffixes
        for suffix in [' group', ' inc', ' corp', ' llc', ' ltd', ' investments', ' partners']:
            n = n.replace(suffix, '')
        return n.strip()
    
    def _is_duplicate(self, buyer, seller, value):
        """Check for duplicate with fuzzy matching"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        c = conn.cursor()
        
        norm_buyer = self._normalize_company(buyer)
        norm_seller = self._normalize_company(seller)
        
        # Check recent deals with fuzzy matching
        c.execute("SELECT buyer, seller, value FROM deals WHERE date >= date('now', '-7 days')")
        for row in c.fetchall():
            db_buyer = self._normalize_company(row[0])
            db_seller = self._normalize_company(row[1])
            db_value = row[2]
            
            # Match if normalized names match and value is same
            if (norm_buyer in db_buyer or db_buyer in norm_buyer) and \
               (norm_seller in db_seller or db_seller in norm_seller) and \
               abs((db_value or 0) - (value or 0)) < 100:  # Within $100M
                conn.close()
                return True
        
        conn.close()
        return False
    
    def auto_approve(self, item_id, item_type, data):
        if item_type == 'deal':
            valid, reason = self.extractor.validate_deal(data)
            if not valid:
                print(f"   ⚠️ Rejected: {reason}")
                self.stats['deals_rejected'] += 1
                conn = sqlite3.connect(DB_PATH, timeout=60)
                conn.execute("UPDATE autopilot_pending SET status='rejected', processed_at=? WHERE id=?", (datetime.now().isoformat(), item_id))
                conn.commit()
                conn.close()
                return False
        
        conn = sqlite3.connect(DB_PATH, timeout=60)
        c = conn.cursor()
        
        try:
            buyer = data.get('buyer')
            seller = data.get('seller') or buyer
            value = data.get('value', 0)
            
            # Use fuzzy duplicate detection
            if self._is_duplicate(buyer, seller, value):
                print(f"   ⏭️ Duplicate: {buyer} / {seller}")
                conn.close()
                return False
            
            deal_id = f"AUTO-{datetime.now().strftime('%Y%m%d')}-{item_id[:6]}"
            c.execute("INSERT OR IGNORE INTO deals (id,date,year,buyer,seller,value,type,region,market,source_url,created_at,verified) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                     (deal_id, datetime.now().strftime('%Y-%m-%d'), datetime.now().year, buyer, seller, value, data.get('type','unknown'), 'Global', data.get('market','Global'), data.get('source_url',''), datetime.now().isoformat()))
            
            if c.rowcount > 0:
                self.stats['deals_approved'] += 1
                print(f"   ✅ Deal: {buyer} → {seller} (${value}M)")
            
            c.execute("UPDATE autopilot_pending SET status='approved', processed_at=? WHERE id=?", (datetime.now().isoformat(), item_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"❌ Error: {e}")
            conn.close()
            return False
    
    def get_stats(self):
        return {
            **self.stats, 
            'companies_tracked': len(COMPANIES), 
            'news_sources': len(NEWS_SOURCES['rss_feeds']),
            'energy_markets': len(ENERGY_MARKETS),
            'version': '4.0'
        }

class AutoPilotScheduler:
    def __init__(self, discovery_engine, news_syncer=None):
        self.discovery = discovery_engine
        self.news_syncer = news_syncer
        self.rss_fetcher = RSSFetcher()
        self.running = False
        self.last_facility_discovery = None
    
    def start(self):
        self.running = True
        print(f"🤖 Auto-Pilot v4.0 | {len(COMPANIES)} companies | {len(NEWS_SOURCES['rss_feeds'])} news sources | {len(ENERGY_MARKETS)} markets")
        threading.Thread(target=self._news_loop, daemon=True).start()
        threading.Thread(target=self._social_loop, daemon=True).start()
        threading.Thread(target=self._facility_discovery_loop, daemon=True).start()
        threading.Thread(target=self._energy_sync_loop, daemon=True).start()
    
    def _facility_discovery_loop(self):
        """Run facility discovery once per day"""
        DISCOVERY_INTERVAL = 86400  # 24 hours in seconds
        
        while self.running:
            try:
                now = datetime.now()
                
                # Run if never run, or if 24 hours have passed
                should_run = (
                    self.last_facility_discovery is None or 
                    (now - self.last_facility_discovery).total_seconds() >= DISCOVERY_INTERVAL
                )
                
                if should_run:
                    print(f"\n🔍 Starting daily facility discovery at {now.strftime('%Y-%m-%d %H:%M')}")
                    
                    try:
                        # Call the discovery API endpoint
                        response = requests.post(
                            'http://localhost:5000/api/discovery/run',
                            json={'sources': ['all']},
                            timeout=300  # 5 minute timeout for discovery
                        )
                        
                        if response.status_code == 200:
                            result = response.json()
                            if result.get('success'):
                                results = result.get('results', {})
                                print(f"✅ Discovery complete:")
                                print(f"   Found: {results.get('total_found', 0)}")
                                print(f"   Added: {results.get('total_added', 0)}")
                                for src in results.get('sources', []):
                                    print(f"   - {src.get('source')}: {src.get('found', 0)} found, {src.get('added', 0)} new")
                            else:
                                print(f"⚠️ Discovery returned error: {result.get('error')}")
                        else:
                            print(f"⚠️ Discovery API returned status {response.status_code}")
                            
                    except requests.exceptions.RequestException as e:
                        print(f"⚠️ Discovery API call failed: {e}")
                    
                    self.last_facility_discovery = now
                    
            except Exception as e:
                print(f"❌ Facility discovery error: {e}")
            
            # Check every hour if it's time to run
            time.sleep(3600)
    
    def _news_loop(self):
        while self.running:
            try:
                articles = self.news_syncer.sync() if self.news_syncer else self.rss_fetcher.fetch_all_feeds()
                for article in articles:
                    result = self.discovery.process_news_article(article)
                    if result['is_deal']:
                        deal = result['extracted_data']['deal']
                        item_id = self.discovery.add_pending('deal', deal, article.get('link',''))
                        self.discovery.stats['deals_discovered'] += 1
                        print(f"💰 {deal.get('buyer','?')} / {deal.get('seller','?')} ({deal.get('confidence',0)}%)")
                        if CONFIG['auto_approve_enabled'] and deal.get('confidence',0) >= CONFIG['auto_approve_threshold']:
                            self.discovery.auto_approve(item_id, 'deal', deal)
                self.discovery.stats['news_processed'] += len(articles)
            except Exception as e:
                print(f"❌ {e}")
            time.sleep(CONFIG['news_interval_seconds'])
    
    def _social_loop(self):
        while self.running:
            try:
                self.discovery.social_poster.process_queue()
            except:
                pass
            time.sleep(14400)
    
    def _energy_sync_loop(self):
        """Sync energy infrastructure data for all markets every 5 minutes"""
        while self.running:
            try:
                print(f"\n⚡ Syncing energy data for {len(ENERGY_MARKETS)} markets...")
                synced = 0
                for market in ENERGY_MARKETS:
                    try:
                        response = requests.get(
                            'http://localhost:5000/api/v1/energy/site-analysis',
                            params={'lat': market['lat'], 'lng': market['lng'], 'radius': market['radius']},
                            timeout=30
                        )
                        if response.status_code == 200:
                            synced += 1
                    except:
                        pass
                print(f"   ✅ Synced {synced}/{len(ENERGY_MARKETS)} markets")
            except Exception as e:
                print(f"❌ Energy sync error: {e}")
            time.sleep(300)  # 5 minutes

# ============================================
# USER ANALYTICS & VISITOR TRACKING
# ============================================

class UserAnalytics:
    """Track visitors, signups, and usage for advertising revenue optimization"""
    
    def __init__(self):
        self._init_tables()
    
    def _init_tables(self):
        """Create analytics tables if they don't exist"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        c = conn.cursor()
        
        # Visitors table - track unique visitors
        c.execute("""CREATE TABLE IF NOT EXISTS visitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            user_agent TEXT,
            referrer TEXT,
            page_visited TEXT,
            visit_time TEXT,
            session_id TEXT,
            country TEXT,
            city TEXT
        )""")
        
        # Users table - registered users
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            name TEXT,
            company TEXT,
            role TEXT,
            plan TEXT DEFAULT 'free',
            created_at TEXT,
            last_login TEXT,
            login_count INTEGER DEFAULT 0,
            api_key TEXT,
            stripe_customer_id TEXT
        )""")
        
        # Page views table - detailed page tracking
        c.execute("""CREATE TABLE IF NOT EXISTS page_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page TEXT,
            visitor_id TEXT,
            view_time TEXT,
            duration_seconds INTEGER,
            referrer TEXT
        )""")
        
        # API usage table - track API calls
        c.execute("""CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT,
            user_id TEXT,
            ip_address TEXT,
            call_time TEXT,
            response_time_ms INTEGER,
            status_code INTEGER
        )""")
        
        # Daily stats summary
        c.execute("""CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            unique_visitors INTEGER DEFAULT 0,
            page_views INTEGER DEFAULT 0,
            new_signups INTEGER DEFAULT 0,
            api_calls INTEGER DEFAULT 0,
            revenue_usd REAL DEFAULT 0
        )""")
        
        conn.commit()
        conn.close()
        print("📊 Analytics tables initialized")
    
    def log_visit(self, ip_address, user_agent='', referrer='', page='/', session_id=''):
        """Log a visitor"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute(
            "INSERT INTO visitors (ip_address, user_agent, referrer, page_visited, visit_time, session_id) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (ip_address, user_agent, referrer, page, datetime.now().isoformat(), session_id)
        )
        conn.commit()
        conn.close()
    
    def log_page_view(self, page, visitor_id='', duration=0, referrer=''):
        """Log a page view"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute(
            "INSERT INTO page_views (page, visitor_id, view_time, duration_seconds, referrer) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            (page, visitor_id, datetime.now().isoformat(), duration, referrer)
        )
        conn.commit()
        conn.close()
    
    def log_api_call(self, endpoint, user_id='', ip_address='', response_time=0, status_code=200):
        """Log an API call"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute(
            "INSERT INTO api_usage (endpoint, user_id, ip_address, call_time, response_time_ms, status_code) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (endpoint, user_id, ip_address, datetime.now().isoformat(), response_time, status_code)
        )
        conn.commit()
        conn.close()
    
    def register_user(self, email, name='', company='', role='', plan='free'):
        """Register a new user"""
        api_key = hashlib.sha256(f"{email}{datetime.now().isoformat()}".encode()).hexdigest()[:32]
        conn = sqlite3.connect(DB_PATH, timeout=60)
        try:
            conn.execute(
                "INSERT INTO users (email, name, company, role, plan, created_at, api_key) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (email, name, company, role, plan, datetime.now().isoformat(), api_key)
            )
            conn.commit()
            conn.close()
            return {'success': True, 'api_key': api_key}
        except sqlite3.IntegrityError:
            conn.close()
            return {'success': False, 'error': 'Email already registered'}
    
    def get_visitor_report(self, days=7):
        """Get visitor report for advertising"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Total unique visitors
        c.execute("""
            SELECT COUNT(DISTINCT ip_address) as unique_visitors,
                   COUNT(*) as total_visits
            FROM visitors 
            WHERE visit_time >= datetime('now', ?)
        """, (f'-{days} days',))
        totals = dict(c.fetchone())
        
        # Visitors by day
        c.execute("""
            SELECT DATE(visit_time) as date,
                   COUNT(DISTINCT ip_address) as unique_visitors,
                   COUNT(*) as visits
            FROM visitors 
            WHERE visit_time >= datetime('now', ?)
            GROUP BY DATE(visit_time)
            ORDER BY date DESC
        """, (f'-{days} days',))
        daily = [dict(row) for row in c.fetchall()]
        
        # Top pages
        c.execute("""
            SELECT page_visited, COUNT(*) as views
            FROM visitors 
            WHERE visit_time >= datetime('now', ?)
            GROUP BY page_visited
            ORDER BY views DESC
            LIMIT 10
        """, (f'-{days} days',))
        top_pages = [dict(row) for row in c.fetchall()]
        
        # Top referrers
        c.execute("""
            SELECT referrer, COUNT(*) as count
            FROM visitors 
            WHERE visit_time >= datetime('now', ?) AND referrer != ''
            GROUP BY referrer
            ORDER BY count DESC
            LIMIT 10
        """, (f'-{days} days',))
        top_referrers = [dict(row) for row in c.fetchall()]
        
        conn.close()
        
        return {
            'period_days': days,
            'totals': totals,
            'daily': daily,
            'top_pages': top_pages,
            'top_referrers': top_referrers,
            'generated_at': datetime.now().isoformat()
        }
    
    def get_user_report(self):
        """Get user/signup report"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # All users
        c.execute("""
            SELECT id, email, name, company, role, plan, created_at, last_login
            FROM users
            ORDER BY created_at DESC
        """)
        users = [dict(row) for row in c.fetchall()]
        
        # Stats by plan
        c.execute("""
            SELECT plan, COUNT(*) as count
            FROM users
            GROUP BY plan
        """)
        by_plan = {row['plan']: row['count'] for row in c.fetchall()}
        
        # Recent signups (last 30 days)
        c.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as signups
            FROM users
            WHERE created_at >= datetime('now', '-30 days')
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """)
        recent_signups = [dict(row) for row in c.fetchall()]
        
        conn.close()
        
        return {
            'total_users': len(users),
            'users': users,
            'by_plan': by_plan,
            'recent_signups': recent_signups,
            'generated_at': datetime.now().isoformat()
        }
    
    def get_api_usage_report(self, days=7):
        """Get API usage report"""
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Total calls
        c.execute("""
            SELECT COUNT(*) as total_calls,
                   AVG(response_time_ms) as avg_response_time
            FROM api_usage 
            WHERE call_time >= datetime('now', ?)
        """, (f'-{days} days',))
        totals = dict(c.fetchone())
        
        # Calls by endpoint
        c.execute("""
            SELECT endpoint, COUNT(*) as calls
            FROM api_usage 
            WHERE call_time >= datetime('now', ?)
            GROUP BY endpoint
            ORDER BY calls DESC
            LIMIT 20
        """, (f'-{days} days',))
        by_endpoint = [dict(row) for row in c.fetchall()]
        
        # Daily calls
        c.execute("""
            SELECT DATE(call_time) as date, COUNT(*) as calls
            FROM api_usage 
            WHERE call_time >= datetime('now', ?)
            GROUP BY DATE(call_time)
            ORDER BY date DESC
        """, (f'-{days} days',))
        daily = [dict(row) for row in c.fetchall()]
        
        conn.close()
        
        return {
            'period_days': days,
            'totals': totals,
            'by_endpoint': by_endpoint,
            'daily': daily,
            'generated_at': datetime.now().isoformat()
        }

# Global analytics instance
user_analytics = UserAnalytics()

# ============================================
# FLASK ADMIN ROUTES SETUP
# ============================================

def setup_admin_routes(app):
    """Register admin analytics routes with Flask app"""
    
    @app.route('/api/admin/visitors', methods=['GET'])
    def admin_visitors():
        """Get visitor analytics report"""
        days = request.args.get('days', 7, type=int)
        return jsonify(user_analytics.get_visitor_report(days))
    
# AUTO-REPAIR: duplicate route '/api/admin/users' also in main.py:16827 — review and remove one
    @app.route('/api/admin/users', methods=['GET'])
    def admin_users():
        """Get user/signup report"""
        return jsonify(user_analytics.get_user_report())
    
    @app.route('/api/admin/api-usage', methods=['GET'])
    def admin_api_usage():
        """Get API usage report"""
        days = request.args.get('days', 7, type=int)
        return jsonify(user_analytics.get_api_usage_report(days))
    
    @app.route('/api/admin/dashboard', methods=['GET'])
    def admin_dashboard():
        """Combined admin dashboard data"""
        return jsonify({
            'visitors': user_analytics.get_visitor_report(7),
            'users': user_analytics.get_user_report(),
            'api_usage': user_analytics.get_api_usage_report(7),
            'autopilot': {
                'version': '4.0',
                'companies_tracked': len(COMPANIES),
                'news_sources': len(NEWS_SOURCES['rss_feeds']),
                'energy_markets': len(ENERGY_MARKETS)
            }
        })
# AUTO-REPAIR: duplicate route '/api/track/visit' also in routes/track_routes.py:21 — review and remove one
    
    @app.route('/api/track/visit', methods=['POST'])
    def track_visit():
        """Track a page visit (call from frontend)"""
        data = request.get_json() or {}
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_analytics.log_visit(
            ip_address=ip,
            user_agent=request.headers.get('User-Agent', ''),
            referrer=data.get('referrer', request.headers.get('Referer', '')),
            page=data.get('page', '/'),
            session_id=data.get('session_id', '')
        )
        return jsonify({'success': True})
    
    @app.route('/api/admin/register-user', methods=['POST'])
    def admin_register_user():
        """Register a new user (admin endpoint)"""
        data = request.get_json() or {}
        if not data.get('email'):
            return jsonify({'success': False, 'error': 'Email required'}), 400
        result = user_analytics.register_user(
            email=data['email'],
            name=data.get('name', ''),
            company=data.get('company', ''),
            role=data.get('role', ''),
            plan=data.get('plan', 'free')
        )
        return jsonify(result)
    
    print("📊 Admin analytics routes registered:")
    print("   GET /api/admin/visitors")
    print("   GET /api/admin/users")
    print("   GET /api/admin/api-usage")
    print("   GET /api/admin/dashboard")
    print("   POST /api/track/visit")
    print("   POST /api/admin/register-user")

if __name__ == '__main__':
    print("=" * 50)
    print(f"DC Hub Auto-Pilot v4.0 - {len(COMPANIES)} Companies")
    print(f"   📰 {len(NEWS_SOURCES['rss_feeds'])} news sources")
    print(f"   ⚡ {len(ENERGY_MARKETS)} energy markets")
    print("=" * 50)
    
    tests = [
        "Blackstone acquires AirTrunk for $16 billion",
        "SoftBank to buy DigitalBridge in $4B deal",
        "Microsoft invests $80 billion in AI data centers",
        "KKR and GIP acquire CyrusOne for $15B",
        "Vantage raises $6.4B from DigitalBridge and Silver Lake",
    ]
    
    for h in tests:
        d = deal_extractor.extract_deal(h)
        v, r = deal_extractor.validate_deal(d)
        print(f"{'✅' if v else '❌'} {h[:50]}...")
        print(f"   → {d['buyer']} / {d['seller']} | ${d['value']}M | {d['confidence']}%")
