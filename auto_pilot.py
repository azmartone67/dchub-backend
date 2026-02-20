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
from db_utils import get_db

# Flask imports for admin routes (used when imported by main.py)
try:
    from flask import request, jsonify
except ImportError:
    request = None

# Intelligence Engine for daily emails, LinkedIn, alerts
try:
    from intelligence_engine import run_daily_intelligence, post_to_linkedin, generate_linkedin_post, check_for_new_deals, send_deal_alerts, get_last_linkedin_post_time, save_linkedin_post_time
    INTELLIGENCE_ENGINE_AVAILABLE = True
except ImportError:
    INTELLIGENCE_ENGINE_AVAILABLE = False
    run_daily_intelligence = None
    send_deal_alerts = None
    jsonify = lambda x: x

try:
    from evolution_engine import get_evolution_engine, run_evolution_cycle, get_learning_status
    EVOLUTION_ENGINE_AVAILABLE = True
except ImportError:
    EVOLUTION_ENGINE_AVAILABLE = False
    get_evolution_engine = None
    run_evolution_cycle = None
    get_learning_status = None

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
    
    # Private Equity & Infrastructure (65+)
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
    'Permira', 'CVC', 'CVC Capital', 'BlackRock GIP', 'Ardian', 'Ontario Municipal', 'OMERS',
    
    # Utilities & Power (25)
    'Dominion', 'Dominion Energy', 'Duke Energy', 'AES', 'AES Corporation', 'NextEra', 'NextEra Energy',
    'Constellation', 'Constellation Energy', 'Vistra', 'Vistra Energy', 'Talen', 'Talen Energy',
    'NRG', 'NRG Energy', 'Intersect Power', 'Kairos Power', 'TerraPower', 'NuScale', 'Oklo',
    'Southern Company', 'Exelon', 'PG&E', 'Entergy', 'TVA', 'Enel',
    
    # AI & GPU Cloud (40+)
    'OpenAI', 'Anthropic', 'xAI', 'Nvidia', 'AMD', 'Intel', 'Cerebras', 'Groq', 'SambaNova',
    'CoreWeave', 'Lambda', 'Lambda Labs', 'Together AI', 'Crusoe', 'Crusoe Energy', 'Applied Digital',
    'Nscale', 'Voltage Park', 'Inflection', 'Mistral', 'Cohere', 'AI21', 'Hugging Face',
    'Scale AI', 'Databricks', 'Snowflake', 'Anyscale', 'Modal', 'Replicate', 'RunPod',
    'Cloverleaf', 'Cloverleaf Infrastructure', 'Core Scientific', 'Hut 8', 'Hut 8 Mining',
    'Bitdeer', 'Marathon Digital', 'Riot Platforms', 'CleanSpark', 'TeraWulf', 'Cipher Mining',
    
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
# EXPANDED NEWS SOURCES (36 feeds)
# ============================================
NEWS_SOURCES = {
    'rss_feeds': [
        # ============================================
        # PRIMARY DATA CENTER INDUSTRY (5)
        # ============================================
        'https://www.datacenterdynamics.com/en/rss/',
        'https://www.datacenterknowledge.com/rss.xml',
        'https://www.datacenterfrontier.com/feed',
        'https://www.theregister.com/headlines.atom',
        'https://www.capacitymedia.com/rss/rss.xml',
        
        # ============================================
        # TECH & BUSINESS NEWS (8)
        # ============================================
        'https://feeds.bloomberg.com/technology/news.rss',
        'https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml',
        'https://techcrunch.com/feed/',
        'https://www.reuters.com/technology/rss',
        'https://www.wsj.com/xml/rss/3_7455.xml',  # WSJ Tech
        'https://feeds.arstechnica.com/arstechnica/technology-lab',
        'https://www.wired.com/feed/rss',
        'https://www.theverge.com/rss/index.xml',
        
        # ============================================
        # M&A / DEALS / PRIVATE EQUITY (6)
        # ============================================
        'https://www.prnewswire.com/rss/technology-latest-news.rss',
        'https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtSWA==',
        'https://www.privateequitywire.co.uk/rss.xml',
        'https://pitchbook.com/rss/news',
        'https://www.infrastructureinvestor.com/feed/',
        'https://www.pehub.com/feed/',
        
        # ============================================
        # COMMERCIAL REAL ESTATE (5)
        # ============================================
        'https://www.bisnow.com/feed',
        'https://www.globest.com/feed/',
        'https://commercialobserver.com/feed/',
        'https://www.connectcre.com/feed/',
        'https://www.rejournals.com/feed/',
        
        # ============================================
        # ENERGY & UTILITIES (8)
        # ============================================
        'https://www.utilitydive.com/feeds/news/',
        'https://www.powermag.com/feed/',
        'https://www.power-grid.com/feed/',
        'https://www.energycentral.com/rss.xml',
        'https://www.renewableenergyworld.com/feed/',
        'https://cleantechnica.com/feed/',
        'https://www.greentechmedia.com/feed/',
        'https://www.solarpowerworldonline.com/feed/',
        
        # ============================================
        # AI / HYPERSCALE / CLOUD (4)
        # ============================================
        'https://www.sdxcentral.com/feed/',
        'https://www.lightreading.com/rss_simple.asp',
        'https://venturebeat.com/feed/',
        'https://www.hpcwire.com/feed/',
    ],
    'keywords_deals': [
        'acquire', 'acquisition', 'merger', 'buy', 'purchase', 'invest', 
        'funding', 'deal', 'billion', 'million', 'data center', 'hyperscale', 
        'colocation', 'joint venture', 'stake', 'buyout', 'private equity',
        'infrastructure fund', 'recapitalization', 'divestiture', 'IPO'
    ],
    'keywords_facility': [
        'megawatt', 'mw', 'campus', 'facility', 'groundbreaking', 'construction', 
        'expansion', 'new site', 'breaking ground', 'ribbon cutting', 'opening',
        'under construction', 'development', 'hyperscale campus', 'data center park'
    ],
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

# ============================================
# DYNAMIC RSS FEED DISCOVERY SYSTEM
# ============================================
RSS_DISCOVERY_SOURCES = [
    # Directories and aggregators to search for new feeds
    {'type': 'feedly_category', 'url': 'https://feedly.com/i/category/data%20center', 'category': 'data center'},
    {'type': 'google_search', 'query': 'data center news RSS feed', 'category': 'data center'},
    {'type': 'google_search', 'query': 'energy infrastructure news RSS', 'category': 'energy'},
    {'type': 'google_search', 'query': 'commercial real estate news RSS feed', 'category': 'real estate'},
    {'type': 'google_search', 'query': 'private equity M&A news RSS', 'category': 'deals'},
]

# Known sites to check for RSS feeds (will extract /feed, /rss, etc.)
RSS_DISCOVERY_SITES = [
    # Data Center Industry (Core)
    'https://datacenternews.us',
    'https://www.datacenters.com',
    'https://insidehpc.com',
    'https://www.nextplatform.com',
    'https://blocksandfiles.com',
    'https://www.datacenterjournal.com',
    'https://www.datacenternews.asia',
    'https://www.datacentreplus.com',
    'https://www.dataeconomy.com',
    'https://www.bcs.org',
    
    # Energy & Utilities
    'https://www.greenbiz.com',
    'https://www.eenews.net',
    'https://www.canarymedia.com',
    'https://www.utilitydive.com',
    'https://www.power-grid.com',
    'https://www.tdworld.com',
    'https://www.powermag.com',
    'https://www.renewableenergyworld.com',
    'https://cleantechnica.com',
    'https://www.energystorage.com',
    'https://www.solarpowerworldonline.com',
    'https://www.windpowermonthly.com',
    
    # Real Estate / Infrastructure / Construction
    'https://product.costar.com',
    'https://www.cbre.com',
    'https://www.jll.com',
    'https://www.cushmanwakefield.com',
    'https://www.bisnow.com',
    'https://www.globest.com',
    'https://www.constructiondive.com',
    'https://www.enr.com',
    'https://www.naiop.org',
    'https://www.reit.com',
    
    # Tech / AI / Cloud
    'https://www.technologyreview.com',
    'https://spectrum.ieee.org',
    'https://syncedreview.com',
    'https://www.infoworld.com',
    'https://www.computerworld.com',
    'https://www.networkworld.com',
    'https://www.ciodive.com',
    'https://www.sdxcentral.com',
    'https://www.lightreading.com',
    'https://www.fiercetelecom.com',
    'https://www.telecomtv.com',
    
    # Finance / M&A / Investment
    'https://www.privateequitywire.co.uk',
    'https://www.infrastructureinvestor.com',
    'https://www.perenews.com',
    'https://www.realassets.com',
    'https://www.pitchbook.com',
    'https://www.preqin.com',
    
    # Regional / International
    'https://www.capacitymedia.com',
    'https://www.developingtelecoms.com',
    'https://www.commsupdate.com',
    'https://www.totaltele.com',
    'https://www.africanbusinessreview.co.za',
    'https://www.asianinvestor.net',
    
    # Sustainability / ESG
    'https://www.environmentalleader.com',
    'https://www.triplepundit.com',
    'https://www.sustainablebrands.com',
    'https://www.greentechmedia.com',
]

def init_rss_feeds_table():
    """Initialize table for dynamic RSS feed management"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rss_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            name TEXT,
            category TEXT DEFAULT 'general',
            status TEXT DEFAULT 'active',
            error_count INTEGER DEFAULT 0,
            last_success TIMESTAMP,
            last_error TIMESTAMP,
            last_error_msg TEXT,
            article_count INTEGER DEFAULT 0,
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source TEXT DEFAULT 'manual'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rss_discovery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            feeds_checked INTEGER DEFAULT 0,
            feeds_added INTEGER DEFAULT 0,
            feeds_removed INTEGER DEFAULT 0,
            duration_seconds REAL
        )
    ''')
    conn.commit()
    conn.close()
    return True

def get_all_rss_feeds():
    """Get all active RSS feeds (static + dynamic)"""
    feeds = list(NEWS_SOURCES['rss_feeds'])  # Start with static feeds
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM rss_feeds WHERE status = 'active' AND error_count < 3")
        dynamic_feeds = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        # Add dynamic feeds (avoid duplicates)
        for feed in dynamic_feeds:
            if feed not in feeds:
                feeds.append(feed)
    except Exception as e:
        pass  # If table doesn't exist yet, just use static feeds
    
    return feeds

def validate_rss_feed(url: str, timeout: int = 10) -> dict:
    """Validate an RSS feed URL and return info"""
    result = {'valid': False, 'url': url, 'name': None, 'article_count': 0, 'error': None}
    
    try:
        resp = requests.get(url, timeout=timeout, headers={'User-Agent': 'DC-Hub-RSS-Validator/1.0'})
        if resp.status_code != 200:
            result['error'] = f'HTTP {resp.status_code}'
            return result
        
        content = resp.text[:50000]  # Limit content size
        
        # Try XML parsing
        try:
            root = ET.fromstring(resp.content)
            
            # Check for RSS format
            if root.tag == 'rss' or root.find('.//channel') is not None:
                channel = root.find('.//channel')
                if channel is not None:
                    title = channel.find('title')
                    result['name'] = title.text if title is not None else url
                    items = root.findall('.//item')
                    result['article_count'] = len(items)
                    result['valid'] = len(items) > 0
            
            # Check for Atom format
            elif 'atom' in root.tag.lower() or root.find('.//{http://www.w3.org/2005/Atom}entry') is not None:
                title = root.find('.//{http://www.w3.org/2005/Atom}title')
                result['name'] = title.text if title is not None else url
                entries = root.findall('.//{http://www.w3.org/2005/Atom}entry')
                result['article_count'] = len(entries)
                result['valid'] = len(entries) > 0
                
        except ET.ParseError as e:
            result['error'] = f'XML parse error: {str(e)[:50]}'
            
    except requests.Timeout:
        result['error'] = 'Timeout'
    except requests.RequestException as e:
        result['error'] = str(e)[:100]
    
    return result

def discover_rss_from_site(site_url: str) -> list:
    """Try to discover RSS feeds from a website"""
    found_feeds = []
    common_paths = ['/feed', '/rss', '/rss.xml', '/feed.xml', '/atom.xml', '/feeds/posts/default', '/blog/feed', '/news/feed']
    
    for path in common_paths:
        try:
            test_url = site_url.rstrip('/') + path
            result = validate_rss_feed(test_url, timeout=60)
            if result['valid']:
                found_feeds.append({
                    'url': test_url,
                    'name': result['name'],
                    'article_count': result['article_count']
                })
                break  # Found one valid feed, stop trying
        except:
            continue
    
    return found_feeds

def add_discovered_feed(url: str, name: str = None, category: str = 'discovered', source: str = 'auto'):
    """Add a newly discovered feed to the database"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO rss_feeds (url, name, category, source, status)
            VALUES (?, ?, ?, ?, 'active')
        ''', (url, name or url, category, source))
        inserted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        return False

def update_feed_health(url: str, success: bool, error_msg: str = None):
    """Update feed health status after fetch attempt"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if success:
            cursor.execute('''
                UPDATE rss_feeds 
                SET last_success = CURRENT_TIMESTAMP, error_count = 0, status = 'active'
                WHERE url = ?
            ''', (url,))
        else:
            cursor.execute('''
                UPDATE rss_feeds 
                SET last_error = CURRENT_TIMESTAMP, 
                    last_error_msg = ?,
                    error_count = error_count + 1,
                    status = CASE WHEN error_count >= 2 THEN 'disabled' ELSE status END
                WHERE url = ?
            ''', (error_msg, url))
        
        conn.commit()
        conn.close()
    except:
        pass

def run_feed_discovery():
    """Run periodic RSS feed discovery"""
    start = time.time()
    feeds_checked = 0
    feeds_added = 0
    
    print(f"🔍 RSS Discovery: Checking {len(RSS_DISCOVERY_SITES)} sites...")
    
    for site in RSS_DISCOVERY_SITES:
        feeds_checked += 1
        discovered = discover_rss_from_site(site)
        
        for feed in discovered:
            # Check if relevant to our topics
            url_lower = feed['url'].lower()
            name_lower = (feed['name'] or '').lower()
            
            # Relevance check
            relevant_keywords = ['data center', 'datacenter', 'energy', 'power', 'infrastructure', 
                               'real estate', 'commercial', 'technology', 'cloud', 'ai', 
                               'colocation', 'hyperscale', 'enterprise', 'digital']
            
            is_relevant = any(kw in url_lower or kw in name_lower for kw in relevant_keywords)
            
            if is_relevant or feed['article_count'] > 5:  # Accept if has articles
                if add_discovered_feed(feed['url'], feed['name'], 'discovered', 'auto'):
                    feeds_added += 1
                    print(f"   ✅ Added: {feed['name'] or feed['url'][:50]}")
    
    # Log discovery run
    duration = time.time() - start
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO rss_discovery_log (feeds_checked, feeds_added, duration_seconds)
            VALUES (?, ?, ?)
        ''', (feeds_checked, feeds_added, duration))
        conn.commit()
        conn.close()
    except:
        pass
    
    print(f"🔍 RSS Discovery complete: {feeds_added} new feeds from {feeds_checked} sites ({duration:.1f}s)")
    return {'checked': feeds_checked, 'added': feeds_added, 'duration': duration}

def get_feed_stats():
    """Get statistics about RSS feeds"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        stats = {
            'static_feeds': len(NEWS_SOURCES['rss_feeds']),
            'dynamic_feeds': 0,
            'active_feeds': 0,
            'disabled_feeds': 0,
            'total_articles': 0
        }
        
        cursor.execute("SELECT COUNT(*) FROM rss_feeds WHERE status = 'active'")
        stats['active_feeds'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM rss_feeds WHERE status = 'disabled'")
        stats['disabled_feeds'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM rss_feeds")
        stats['dynamic_feeds'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(article_count) FROM rss_feeds")
        total = cursor.fetchone()[0]
        stats['total_articles'] = total or 0
        
        conn.close()
        return stats
    except:
        return {'static_feeds': len(NEWS_SOURCES['rss_feeds']), 'dynamic_feeds': 0}

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

class CapacityExtractor:
    """Extract MW capacity announcements from news headlines"""
    
    MW_PATTERNS = [
        r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:mega)?watts?\b',
        r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*MW\b',
        r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*GW\b',
        r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*megawatt',
    ]
    
    STATUS_KEYWORDS = {
        'under_construction': ['construction', 'building', 'break ground', 'groundbreaking', 'underway', 'begins construction'],
        'planned': ['announced', 'plans', 'planned', 'proposed', 'will build', 'to develop', 'investment'],
        'operational': ['opens', 'launched', 'operational', 'completed', 'ribbon cutting', 'now open']
    }
    
    def extract_capacity(self, headline: str, source_url: str = '') -> Optional[Dict]:
        """Extract capacity info from headline"""
        headline_clean = unescape(headline).strip()
        headline_lower = headline_clean.lower()
        
        # Must have capacity keywords
        if not any(kw in headline_lower for kw in ['mw', 'megawatt', 'gigawatt', 'gw', 'capacity', 'power']):
            return None
        
        # Extract MW value
        capacity_mw = None
        for pattern in self.MW_PATTERNS:
            match = re.search(pattern, headline_clean, re.IGNORECASE)
            if match:
                val = float(match.group(1).replace(',', ''))
                if 'GW' in pattern or 'gw' in headline_lower:
                    val *= 1000
                if 10 <= val <= 5000:  # Realistic DC range
                    capacity_mw = val
                    break
        
        if not capacity_mw:
            return None
        
        # Extract operator
        operator = None
        for company in COMPANIES:
            if company.lower() in headline_lower:
                operator = company
                break
        
        if not operator:
            return None
        
        # Detect status
        status = 'planned'
        for s, keywords in self.STATUS_KEYWORDS.items():
            if any(kw in headline_lower for kw in keywords):
                status = s
                break
        
        # Extract location
        location = 'Unknown'
        for market in ENERGY_MARKETS:
            if market['name'].lower() in headline_lower:
                location = market['name']
                break
        
        return {
            'operator': operator,
            'capacity_mw': capacity_mw,
            'status': status,
            'location': location,
            'source': source_url,
            'headline': headline_clean
        }
    
    def save_capacity(self, cap: Dict) -> bool:
        """Save capacity to database (deduped by operator+location+capacity)"""
        import time as _time
        for attempt in range(5):
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("""
                    SELECT id FROM capacity_tracking 
                    WHERE operator = ? AND location = ? AND ABS(capacity_mw - ?) < 1
                """, (cap['operator'], cap['location'], cap['capacity_mw']))
                if c.fetchone():
                    conn.close()
                    return False
                c.execute("""
                    INSERT INTO capacity_tracking 
                    (operator, location, capacity_mw, status, source, discovered_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                """, (cap['operator'], cap['location'], cap['capacity_mw'], cap['status'], cap.get('source', '')))
                conn.commit()
                inserted = c.rowcount > 0
                conn.close()
                return inserted
            except sqlite3.OperationalError as e:
                if 'locked' in str(e) and attempt < 4:
                    _time.sleep(3.0 * (attempt + 1))
                    continue
                print(f"Error saving capacity: {e}")
                return False
            except Exception as e:
                print(f"Error saving capacity: {e}")
                return False

capacity_extractor = CapacityExtractor()

DC_INDUSTRY_KEYWORDS = [
    'data center', 'data centre', 'datacenter', 'datacentre', 'colocation', 'colo ',
    'hyperscale', 'cloud infrastructure', 'edge computing', 'digital infrastructure',
    'digital realty', 'equinix', 'cyrusone', 'qts', 'coresite', 'switch', 'vantage',
    'stack infrastructure', 'flexential', 'compass datacenters', 'yondr', 'prime data centers',
    'ntt data', 'chindata', 'gds holdings', 'airtrunk', 'megaport', 'ix australia',
    'server farm', 'hosting facility', 'cloud campus', 'ai infrastructure',
    'fiber route', 'interconnection', 'internet exchange', 'peering',
]

NON_DC_BUYER_BLACKLIST = [
    'the', 'a', 'an', 'this', 'that', 'new', 'top', 'best', 'first', 'last',
    'report', 'analysis', 'study', 'survey', 'review', 'update', 'news',
    'market', 'industry', 'sector', 'global', 'world', 'international',
]

def _is_dc_relevant(title):
    title_lower = title.lower()
    return any(kw in title_lower for kw in DC_INDUSTRY_KEYWORDS) or \
           any(kw in title_lower for kw in NEWS_SOURCES['keywords_deals'])

def _is_valid_company_name(name):
    if not name or len(name) < 2:
        return False
    if name.lower().strip() in NON_DC_BUYER_BLACKLIST:
        return False
    if len(name.split()) > 6:
        return False
    return True

def auto_discover_from_news():
    """Main auto-discovery function - extracts deals and capacity from news"""
    stats = {'deals_found': 0, 'capacity_found': 0, 'deals_saved': 0, 'capacity_saved': 0, 'skipped_irrelevant': 0}
    
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT title, url FROM announcements WHERE published_date > datetime('now', '-7 days') LIMIT 500")
        articles = c.fetchall()
        
        for title, url in articles:
            if not title:
                continue
            
            if not _is_dc_relevant(title):
                stats['skipped_irrelevant'] += 1
                continue
            
            deal = deal_extractor.extract_deal(title)
            buyer = deal.get('buyer')
            seller = deal.get('seller')
            value = deal.get('value')
            confidence = deal.get('confidence', 0)
            deal_type = deal.get('type', 'unknown')
            
            if not _is_valid_company_name(buyer):
                continue
            
            should_save = False
            if buyer and seller and buyer != seller and _is_valid_company_name(seller) and confidence >= 60:
                should_save = True
            elif buyer and value and value >= 10 and confidence >= 65:
                should_save = True
            
            if deal_type == 'unknown':
                should_save = False
            
            if should_save:
                stats['deals_found'] += 1
                deal_hash = hashlib.md5(f"{buyer}{seller or ''}{value or 0}".encode()).hexdigest()[:16]
                try:
                    c.execute("""
                        INSERT OR IGNORE INTO deals 
                        (id, date, year, buyer, seller, value, type, market, source_url, created_at, status)
                        VALUES (?, date('now'), strftime('%Y', 'now'), ?, ?, ?, ?, ?, ?, datetime('now'), 'discovered')
                    """, (deal_hash, buyer, seller or buyer, 
                          value or 0, deal_type, deal.get('market', 'Global'), url))
                    if c.rowcount > 0:
                        stats['deals_saved'] += 1
                        print(f"   ✅ Deal: {buyer} → {seller or buyer} (${value}M)" if value else f"   ✅ Deal: {buyer} / {seller or buyer}")
                except:
                    pass
            
            cap = capacity_extractor.extract_capacity(title, url)
            if cap:
                stats['capacity_found'] += 1
                if capacity_extractor.save_capacity(cap):
                    stats['capacity_saved'] += 1
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"Auto-discovery error: {e}")
    
    return stats

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
        conn = get_db()
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
        conn = get_db()
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
        conn = get_db()
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
                conn = get_db()
                conn.execute("UPDATE autopilot_pending SET status='rejected', processed_at=? WHERE id=?", (datetime.now().isoformat(), item_id))
                conn.commit()
                conn.close()
                return False
            
            if not _is_valid_company_name(data.get('buyer')):
                print(f"   ⚠️ Rejected: Invalid buyer name '{data.get('buyer')}'")
                self.stats['deals_rejected'] += 1
                return False
            
            if data.get('type', 'unknown') == 'unknown':
                print(f"   ⚠️ Rejected: Unknown deal type")
                self.stats['deals_rejected'] += 1
                return False
            
            title = data.get('title', '')
            if title and not _is_dc_relevant(title):
                print(f"   ⚠️ Rejected: Not DC-industry relevant")
                self.stats['deals_rejected'] += 1
                return False
        
        conn = get_db()
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
        # Initialize dynamic feeds table
        init_rss_feeds_table()
        
        # Count total feeds (static + dynamic)
        feed_stats = get_feed_stats()
        total_feeds = feed_stats['static_feeds'] + feed_stats.get('active_feeds', 0)
        
        print(f"🤖 Auto-Pilot v4.0 | {len(COMPANIES)} companies | {total_feeds} news sources | {len(ENERGY_MARKETS)} markets")
        print(f"   📰 Static feeds: {feed_stats['static_feeds']} | Dynamic feeds: {feed_stats.get('active_feeds', 0)}")
        print(f"   🔄 Facility discovery: every 5 min | RSS discovery: every 30 min")
        print(f"   🧠 Deep learning: every 15 min | SEO promotion: every 6 hours")
        print(f"   📣 Intelligence: LinkedIn daily | Deal alerts hourly")
        print(f"   🧬 Evolution Engine: every 30 min (continuous self-improvement)")
        print(f"   🎯 AI Orchestrator: every 20 min (proactive intelligence)")
        print(f"   🔍 API Discovery: every 1 hour (auto-discovers new data sources)")
        threading.Thread(target=self._news_loop, daemon=True).start()
        threading.Thread(target=self._social_loop, daemon=True).start()
        threading.Thread(target=self._facility_discovery_loop, daemon=True).start()
        threading.Thread(target=self._energy_sync_loop, daemon=True).start()
        threading.Thread(target=self._rss_discovery_loop, daemon=True).start()
        threading.Thread(target=self._self_learning_loop, daemon=True).start()
        threading.Thread(target=self._deep_learning_loop, daemon=True).start()
        threading.Thread(target=self._seo_promotion_loop, daemon=True).start()
        threading.Thread(target=self._intelligence_loop, daemon=True).start()
        threading.Thread(target=self._evolution_loop, daemon=True).start()
        threading.Thread(target=self._orchestrator_loop, daemon=True).start()
        threading.Thread(target=self._api_discovery_loop, daemon=True).start()
        threading.Thread(target=self._auto_discovery_loop, daemon=True).start()
    
    def _auto_discovery_loop(self):
        """Auto-discover deals and capacity from news every 5 minutes"""
        INTERVAL = 300  # 5 minutes
        last_run = None
        
        while self.running:
            try:
                now = datetime.now()
                if last_run is None or (now - last_run).total_seconds() >= INTERVAL:
                    print(f"\n🤖 Auto-discovery: scanning news for deals & capacity...")
                    stats = auto_discover_from_news()
                    if stats['deals_saved'] > 0 or stats['capacity_saved'] > 0:
                        print(f"   ✅ Found: {stats['deals_found']} deals, {stats['capacity_found']} capacity announcements")
                        print(f"   💾 Saved: {stats['deals_saved']} new deals, {stats['capacity_saved']} new capacity")
                    else:
                        print(f"   ℹ️ No new discoveries (scanned {stats.get('deals_found', 0)} potential)")
                    
                    try:
                        from discovery_pipeline import run_pipeline
                        pipeline_stats = run_pipeline(limit=50)
                        if pipeline_stats.get('facilities_extracted', 0) > 0:
                            print(f"   🔬 Pipeline: {pipeline_stats['articles_classified']} classified, {pipeline_stats['facilities_extracted']} extracted")
                            print(f"   ✅ Auto-verified: {pipeline_stats['auto_verified']} | Pending: {pipeline_stats['pending_review']} | Discarded: {pipeline_stats['discarded']}")
                    except Exception as e:
                        print(f"   ⚠️ Discovery pipeline skipped: {e}")
                    last_run = now
            except Exception as e:
                print(f"❌ Auto-discovery error: {e}")
            time.sleep(60)  # Check every minute
    
    def _facility_discovery_loop(self):
        """Run facility discovery every 5 minutes for continuous growth"""
        DISCOVERY_INTERVAL = 300  # 5 minutes in seconds
        
        while self.running:
            try:
                now = datetime.now()
                
                # Run if never run, or if 5 minutes have passed
                should_run = (
                    self.last_facility_discovery is None or 
                    (now - self.last_facility_discovery).total_seconds() >= DISCOVERY_INTERVAL
                )
                
                if should_run:
                    print(f"\n🔍 Starting facility discovery at {now.strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    try:
                        # Call the discovery API endpoint
                        response = requests.post(
                            'http://localhost:5000/api/discovery/run',
                            json={'sources': ['all']},
                            timeout=240  # 4 minute timeout for discovery
                        )
                        
                        if response.status_code == 200:
                            result = response.json()
                            if result.get('success'):
                                results = result.get('results', {})
                                added = results.get('total_added', 0)
                                if added > 0:
                                    print(f"✅ Discovery complete: +{added} new facilities")
                                    for src in results.get('sources', []):
                                        if src.get('added', 0) > 0:
                                            print(f"   - {src.get('source')}: +{src.get('added', 0)}")
                                else:
                                    print(f"✅ Discovery complete: No new facilities found")
                            else:
                                print(f"⚠️ Discovery returned error: {result.get('error')}")
                        else:
                            print(f"⚠️ Discovery API returned status {response.status_code}")
                            
                    except requests.exceptions.RequestException as e:
                        print(f"⚠️ Discovery API call failed: {e}")
                    
                    self.last_facility_discovery = now
                    
            except Exception as e:
                print(f"❌ Facility discovery error: {e}")
            
            # Check every 5 minutes
            time.sleep(DISCOVERY_INTERVAL)
    
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
            except sqlite3.OperationalError as e:
                if 'locked' in str(e):
                    time.sleep(5)
                    continue
                print(f"❌ {e}")
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
                            headers={'X-Internal-Key': 'dchub-internal-sync-2026'},
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

    def _rss_discovery_loop(self):
        """Discover new RSS feeds every 30 minutes"""
        RSS_DISCOVERY_INTERVAL = 1800  # 30 minutes (was 1 hour)
        
        # Wait 5 minutes before first discovery run (let server stabilize)
        time.sleep(300)
        
        while self.running:
            try:
                print(f"\n🔍 RSS Feed Discovery starting ({len(RSS_DISCOVERY_SITES)} sites)...")
                result = run_feed_discovery()
                
                # Run self-learning to find new sites from discovered feeds
                try:
                    new_sites = self._discover_new_rss_sites()
                    if new_sites > 0:
                        print(f"   🧠 Self-learned {new_sites} new potential RSS sites")
                except Exception as e:
                    pass
                
                # Update stats
                feed_stats = get_feed_stats()
                print(f"   📊 Feed Stats: {feed_stats['static_feeds']} static + {feed_stats.get('active_feeds', 0)} dynamic = {feed_stats['static_feeds'] + feed_stats.get('active_feeds', 0)} total")
                
            except Exception as e:
                print(f"❌ RSS Discovery error: {e}")
            
            time.sleep(RSS_DISCOVERY_INTERVAL)
    
    def _discover_new_rss_sites(self):
        """Self-learn new RSS sites by crawling existing feeds for links"""
        new_sites_found = 0
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Get active feeds to crawl for links
            cursor.execute("SELECT url FROM rss_feeds WHERE status = 'active' LIMIT 10")
            feeds = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            for feed_url in feeds:
                try:
                    import feedparser
                    parsed = feedparser.parse(feed_url)
                    
                    # Look for links in feed entries
                    for entry in parsed.entries[:5]:
                        link = entry.get('link', '')
                        if link:
                            from urllib.parse import urlparse
                            domain = urlparse(link).netloc
                            if domain and domain not in [urlparse(s).netloc for s in RSS_DISCOVERY_SITES]:
                                # Check if this domain has RSS
                                test_urls = [
                                    f"https://{domain}/feed",
                                    f"https://{domain}/rss",
                                    f"https://{domain}/feed.xml"
                                ]
                                for test_url in test_urls:
                                    try:
                                        import requests
                                        resp = requests.head(test_url, timeout=3, allow_redirects=True)
                                        if resp.status_code == 200:
                                            if add_discovered_feed(test_url, domain, 'discovered', 'self-learned'):
                                                new_sites_found += 1
                                            break
                                    except:
                                        pass
                except:
                    pass
                    
        except Exception as e:
            pass
            
        return new_sites_found
    
    def _self_learning_loop(self):
        """Run self-learning discovery every 30 minutes"""
        LEARNING_INTERVAL = 1800  # 30 minutes
        
        # Wait 10 minutes before first run
        time.sleep(600)
        
        while self.running:
            try:
                from self_learning_discovery import run_self_learning_discovery, get_discovery_stats
                
                print(f"\n🧠 Self-Learning Discovery starting...")
                result = run_self_learning_discovery()
                
                stats = get_discovery_stats()
                print(f"   📊 Sources: {stats.get('active_sources', 0)} active, {stats.get('facilities_discovered', 0)} facilities")
                
            except Exception as e:
                print(f"❌ Self-learning discovery error: {e}")
            
            time.sleep(LEARNING_INTERVAL)
    
    def _deep_learning_loop(self):
        """Run deep learning engine every 15 minutes"""
        DEEP_LEARNING_INTERVAL = 900  # 15 minutes
        
        # Wait 8 minutes before first run
        time.sleep(480)
        
        while self.running:
            try:
                from deep_learning_engine import run_deep_learning_cycle, get_deep_learning_stats
                
                result = run_deep_learning_cycle()
                
                stats = get_deep_learning_stats()
                print(f"   🧠 Deep Learning: {stats.get('total_entities_learned', 0)} entities, {stats.get('total_transactions_detected', 0)} transactions")
                
            except Exception as e:
                print(f"❌ Deep learning error: {e}")
            
            time.sleep(DEEP_LEARNING_INTERVAL)
    
    def _api_discovery_loop(self):
        """Run API auto-discovery every hour to find new data sources"""
        API_DISCOVERY_INTERVAL = 3600  # 1 hour
        
        # Wait 15 minutes before first run
        time.sleep(900)
        
        while self.running:
            try:
                from api_auto_discovery import APIAutoDiscovery
                
                discovery = APIAutoDiscovery()
                
                # Seed known APIs
                seed_result = discovery.seed_known_apis()
                
                # Discover from Data.gov
                datagov_result = discovery.discover_from_data_gov()
                
                # Discover from HIFLD
                hifld_result = discovery.discover_from_hifld()
                
                # Test discovered APIs
                test_result = discovery.test_all_apis()
                
                # Learn from working APIs
                learn_result = discovery.learn_from_apis()
                
                status = discovery.get_discovery_status()
                print(f"   🔍 API Discovery: {status.get('total_apis_discovered', 0)} APIs, {status.get('working_apis', 0)} working, {status.get('items_learned', 0)} items learned")
                
            except Exception as e:
                print(f"❌ API discovery error: {e}")
            
            time.sleep(API_DISCOVERY_INTERVAL)
    
    def _seo_promotion_loop(self):
        """Run SEO promotion every 6 hours"""
        SEO_INTERVAL = 21600  # 6 hours
        
        # Wait 15 minutes before first run
        time.sleep(900)
        
        while self.running:
            try:
                from seo_promotion_engine import run_seo_promotion, get_seo_stats
                
                result = run_seo_promotion()
                
                stats = get_seo_stats()
                print(f"   📢 SEO: {stats.get('total_submissions', 0)} submissions, {stats.get('verified_backlinks', 0)} backlinks")
                
            except Exception as e:
                print(f"❌ SEO promotion error: {e}")
            
            time.sleep(SEO_INTERVAL)
    
    def _intelligence_loop(self):
        """Run intelligence engine - daily email, LinkedIn posts, deal alerts"""
        LINKEDIN_INTERVAL = 86400  # Post to LinkedIn once per day
        ALERT_CHECK_INTERVAL = 3600  # Check for deals every hour
        
        # Load last LinkedIn post time from database (persist across restarts)
        last_linkedin_post = get_last_linkedin_post_time() if INTELLIGENCE_ENGINE_AVAILABLE else None
        if last_linkedin_post:
            print(f"📣 Intelligence: Last LinkedIn post was at {last_linkedin_post}")
        
        # Wait 30 minutes before first run
        time.sleep(1800)
        
        while self.running:
            try:
                if not INTELLIGENCE_ENGINE_AVAILABLE:
                    time.sleep(ALERT_CHECK_INTERVAL)
                    continue
                
                now = datetime.now()
                
                # Check for deal alerts every hour and send to webhooks
                try:
                    alerts = check_for_new_deals()
                    if alerts:
                        print(f"🔔 Intelligence: {len(alerts)} new deal/capacity alerts detected")
                        # Send alerts to subscribed webhooks
                        result = send_deal_alerts(alerts)
                        if result.get('sent', 0) > 0:
                            print(f"📨 Intelligence: Sent alerts to {result['sent']} webhooks")
                except Exception as e:
                    print(f"⚠️ Alert check error: {e}")
                
                # Post to LinkedIn once per day (at 6:30 PM)
                should_post_linkedin = (
                    last_linkedin_post is None or 
                    (now - last_linkedin_post).total_seconds() >= LINKEDIN_INTERVAL
                ) and now.hour == 18 and now.minute >= 30
                
                if should_post_linkedin:
                    try:
                        content = generate_linkedin_post()
                        result = post_to_linkedin(content)
                        if result.get('success'):
                            print(f"📣 LinkedIn: Posted daily update successfully")
                            last_linkedin_post = now
                        else:
                            print(f"⚠️ LinkedIn: {result.get('error', 'Unknown error')}")
                    except Exception as e:
                        print(f"⚠️ LinkedIn post error: {e}")
                
            except Exception as e:
                print(f"❌ Intelligence loop error: {e}")
            
            time.sleep(ALERT_CHECK_INTERVAL)

    def _evolution_loop(self):
        """Run Evolution Engine every 30 minutes for continuous self-improvement"""
        EVOLUTION_INTERVAL = 1800  # 30 minutes
        
        # Wait 10 minutes before first run (let other systems stabilize)
        time.sleep(600)
        
        while self.running:
            try:
                if not EVOLUTION_ENGINE_AVAILABLE:
                    print("⚠️ Evolution Engine not available")
                    time.sleep(EVOLUTION_INTERVAL)
                    continue
                
                print(f"\n🧬 Evolution Engine: Starting evolution cycle...")
                
                try:
                    result = run_evolution_cycle()
                    
                    if result:
                        improvements = result.get('total_improvements', 0)
                        duration = result.get('duration_seconds', 0)
                        
                        print(f"🧬 Evolution complete: {improvements} improvements in {duration:.1f}s")
                        
                        # Log phase summaries
                        phases = result.get('phases', {})
                        if phases.get('learn', {}).get('patterns_learned', 0) > 0:
                            print(f"   📚 Learned {phases['learn']['patterns_learned']} new patterns")
                        if phases.get('improve', {}).get('fixes', 0) > 0:
                            print(f"   🔧 Applied {phases['improve']['fixes']} quality fixes")
                        if phases.get('analyze', {}).get('ai_used'):
                            print(f"   🤖 AI analysis completed")
                    else:
                        print("⚠️ Evolution cycle returned no result")
                        
                except Exception as e:
                    print(f"⚠️ Evolution cycle error: {e}")
                
            except Exception as e:
                print(f"❌ Evolution loop error: {e}")
            
            time.sleep(EVOLUTION_INTERVAL)

    def _orchestrator_loop(self):
        """Run AI Orchestrator every 20 minutes for proactive intelligence"""
        ORCHESTRATOR_INTERVAL = 1200  # 20 minutes
        
        # Wait 5 minutes before first run
        time.sleep(300)
        
        while self.running:
            try:
                try:
                    from ai_orchestrator import get_orchestrator
                    orch = get_orchestrator()
                    
                    print(f"\n🎯 AI Orchestrator: Starting proactive intelligence cycle...")
                    
                    result = orch.run_orchestration_cycle()
                    
                    if result.get('success'):
                        pulse = result.get('market_pulse', {})
                        sentiment = pulse.get('sentiment', 'unknown').upper()
                        opps = result.get('opportunities_count', 0)
                        preds = result.get('predictions_count', 0)
                        
                        print(f"🎯 Orchestration complete: {sentiment} market, {opps} opportunities, {preds} predictions")
                    else:
                        print("⚠️ Orchestration cycle returned no result")
                        
                except ImportError:
                    print("⚠️ AI Orchestrator not available")
                except Exception as e:
                    print(f"⚠️ Orchestrator cycle error: {e}")
                
            except Exception as e:
                print(f"❌ Orchestrator loop error: {e}")
            
            time.sleep(ORCHESTRATOR_INTERVAL)

# ============================================
# USER ANALYTICS & VISITOR TRACKING
# ============================================

class UserAnalytics:
    """Track visitors, signups, and usage for advertising revenue optimization"""
    
    def __init__(self):
        self._init_tables()
    
    def _init_tables(self):
        """Create analytics tables if they don't exist"""
        conn = get_db()
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
        conn = get_db()
        conn.execute(
            "INSERT INTO visitors (ip_address, user_agent, referrer, page_visited, visit_time, session_id) VALUES (?,?,?,?,?,?)",
            (ip_address, user_agent, referrer, page, datetime.now().isoformat(), session_id)
        )
        conn.commit()
        conn.close()
    
    def log_page_view(self, page, visitor_id='', duration=0, referrer=''):
        """Log a page view"""
        conn = get_db()
        conn.execute(
            "INSERT INTO page_views (page, visitor_id, view_time, duration_seconds, referrer) VALUES (?,?,?,?,?)",
            (page, visitor_id, datetime.now().isoformat(), duration, referrer)
        )
        conn.commit()
        conn.close()
    
    def log_api_call(self, endpoint, user_id='', ip_address='', response_time=0, status_code=200):
        """Log an API call"""
        conn = get_db()
        conn.execute(
            "INSERT INTO api_usage (endpoint, user_id, ip_address, timestamp, response_time_ms, status_code) VALUES (?,?,?,?,?,?)",
            (endpoint, user_id, ip_address, datetime.now().isoformat(), response_time, status_code)
        )
        conn.commit()
        conn.close()
    
    def register_user(self, email, name='', company='', role='', plan='free'):
        """Register a new user"""
        api_key = hashlib.sha256(f"{email}{datetime.now().isoformat()}".encode()).hexdigest()[:32]
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (email, name, company, role, plan, created_at, api_key) VALUES (?,?,?,?,?,?,?)",
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
        conn = get_db()
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
        conn = get_db()
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
        conn = get_db()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        try:
            c.execute("PRAGMA table_info(api_usage)")
            cols = [col[1] for col in c.fetchall()]
            time_col = 'call_time' if 'call_time' in cols else 'timestamp' if 'timestamp' in cols else None
            date_col = 'date' if 'date' in cols else None
            
            if not time_col and not date_col:
                conn.close()
                return {'period_days': days, 'totals': {'total_calls': 0, 'avg_response_time': 0}, 'by_endpoint': [], 'daily': [], 'generated_at': datetime.now().isoformat()}
            
            filter_col = time_col or date_col
            
            c.execute(f"""
                SELECT COUNT(*) as total_calls,
                       AVG(response_time_ms) as avg_response_time
                FROM api_usage 
                WHERE {filter_col} >= datetime('now', ?)
            """, (f'-{days} days',))
            totals = dict(c.fetchone())
            
            c.execute(f"""
                SELECT endpoint, COUNT(*) as calls
                FROM api_usage 
                WHERE {filter_col} >= datetime('now', ?)
                GROUP BY endpoint
                ORDER BY calls DESC
                LIMIT 20
            """, (f'-{days} days',))
            by_endpoint = [dict(row) for row in c.fetchall()]
            
            date_expr = f"DATE({time_col})" if time_col else date_col
            c.execute(f"""
                SELECT {date_expr} as date, COUNT(*) as calls
                FROM api_usage 
                WHERE {filter_col} >= datetime('now', ?)
                GROUP BY {date_expr}
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
        except Exception as e:
            conn.close()
            return {'period_days': days, 'totals': {'total_calls': 0, 'avg_response_time': 0}, 'by_endpoint': [], 'daily': [], 'error': str(e), 'generated_at': datetime.now().isoformat()}

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
    
    # ============================================
    # RSS FEED MANAGEMENT ENDPOINTS
    # ============================================
    
    @app.route('/api/admin/rss-feeds', methods=['GET'])
    def admin_rss_feeds():
        """Get all RSS feeds (static + dynamic)"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, url, name, category, status, error_count, 
                       last_success, last_error, article_count, discovered_at, source
                FROM rss_feeds ORDER BY discovered_at DESC
            ''')
            rows = cursor.fetchall()
            conn.close()
            
            dynamic_feeds = []
            for row in rows:
                dynamic_feeds.append({
                    'id': row[0], 'url': row[1], 'name': row[2], 'category': row[3],
                    'status': row[4], 'error_count': row[5], 'last_success': row[6],
                    'last_error': row[7], 'article_count': row[8], 'discovered_at': row[9],
                    'source': row[10]
                })
            
            return jsonify({
                'static_feeds': NEWS_SOURCES['rss_feeds'],
                'dynamic_feeds': dynamic_feeds,
                'stats': get_feed_stats()
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/admin/rss-feeds', methods=['POST'])
    def admin_add_rss_feed():
        """Manually add a new RSS feed"""
        data = request.get_json() or {}
        url = data.get('url')
        
        if not url:
            return jsonify({'success': False, 'error': 'URL required'}), 400
        
        # Validate the feed first
        validation = validate_rss_feed(url)
        if not validation['valid']:
            return jsonify({
                'success': False, 
                'error': f"Invalid feed: {validation.get('error', 'Unknown error')}"
            }), 400
        
        # Add to database
        added = add_discovered_feed(
            url=url,
            name=data.get('name') or validation.get('name'),
            category=data.get('category', 'manual'),
            source='manual'
        )
        
        return jsonify({
            'success': added,
            'feed': validation,
            'message': 'Feed added successfully' if added else 'Feed already exists'
        })
    
    @app.route('/api/admin/rss-feeds/<int:feed_id>', methods=['DELETE'])
    def admin_delete_rss_feed(feed_id):
        """Remove a dynamic RSS feed"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM rss_feeds WHERE id = ?", (feed_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return jsonify({'success': deleted})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/admin/rss-feeds/<int:feed_id>/toggle', methods=['POST'])
    def admin_toggle_rss_feed(feed_id):
        """Enable/disable a dynamic RSS feed"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE rss_feeds 
                SET status = CASE WHEN status = 'active' THEN 'disabled' ELSE 'active' END,
                    error_count = 0
                WHERE id = ?
            ''', (feed_id,))
            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return jsonify({'success': updated})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/admin/rss-discovery/run', methods=['POST'])
    def admin_run_rss_discovery():
        """Manually trigger RSS feed discovery"""
        result = run_feed_discovery()
        return jsonify({
            'success': True,
            'result': result,
            'stats': get_feed_stats()
        })
    
    @app.route('/api/admin/rss-discovery/log', methods=['GET'])
    def admin_rss_discovery_log():
        """Get RSS discovery history"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, feeds_checked, feeds_added, feeds_removed, duration_seconds
                FROM rss_discovery_log ORDER BY timestamp DESC LIMIT 50
            ''')
            rows = cursor.fetchall()
            conn.close()
            
            return jsonify({
                'log': [{
                    'timestamp': row[0], 'checked': row[1], 'added': row[2],
                    'removed': row[3], 'duration': row[4]
                } for row in rows]
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    print("📊 Admin analytics routes registered:")
    print("   GET /api/admin/visitors")
    print("   GET /api/admin/users")
    print("   GET /api/admin/api-usage")
    print("   GET /api/admin/dashboard")
    print("   POST /api/track/visit")
    print("   POST /api/admin/register-user")
    print("📰 RSS Feed management routes registered:")
    print("   GET /api/admin/rss-feeds")
    print("   POST /api/admin/rss-feeds")
    print("   DELETE /api/admin/rss-feeds/<id>")
    print("   POST /api/admin/rss-feeds/<id>/toggle")
    print("   POST /api/admin/rss-discovery/run")
    print("   GET /api/admin/rss-discovery/log")

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
