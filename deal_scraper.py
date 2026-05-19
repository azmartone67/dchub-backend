"""
deal_scraper.py — Automated Data Center M&A Deal Discovery, 2.0
============================================================
Scrapes DC industry news sources for M&A transactions, parses
deal details (buyer, seller, value, MW, market), and inserts
into the Neon PostgreSQL `deals` table.

Architecture:
  - Runs as standalone script (cron or manual)
  - Fetches RSS feeds from DC-specific sources
  - Extracts deal signals from headlines + summaries
  - Parses structured deal data using regex patterns
  - Deduplicates against existing deals in Neon
  - Inserts clean records with ON CONFLICT DO NOTHING

Usage:
  python3 deal_scraper.py              # Run full scrape
  python3 deal_scraper.py --dry-run    # Preview without inserting
  python3 deal_scraper.py --stats      # Show deal table stats

Environment:
  DATABASE_URL or NEON_DATABASE_URL must be set

Schedule (recommended):
  Run 2x/day via cron: 08:00 UTC and 18:00 UTC
"""

import os
import re
import json
import hashlib
import logging
import sys
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

import feedparser
import requests

# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [DEALS] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Database URL
DATABASE_URL = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL', '')

# RSS feeds most likely to contain M&A news
DEAL_FEEDS = [
    # DC-specific (highest signal)
    {"name": "DCD", "url": "https://www.datacenterdynamics.com/en/rss/news/", "priority": 1},
    {"name": "DC Knowledge", "url": "https://www.datacenterknowledge.com/rss.xml", "priority": 1},
    {"name": "DC Frontier", "url": "https://www.datacenterfrontier.com/rss", "priority": 1},
    {"name": "The Register DC", "url": "https://www.theregister.com/data_centre/headlines.atom", "priority": 1},
    {"name": "Capacity Media", "url": "https://www.capacitymedia.com/rss", "priority": 2},
    # Business/finance (need keyword filtering)
    {"name": "Reuters Business", "url": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best", "priority": 3},
    {"name": "Bloomberg Tech", "url": "https://feeds.bloomberg.com/technology/news.rss", "priority": 3},
]

# Google News search queries for deals
GOOGLE_NEWS_QUERIES = [
    "data center acquisition 2026",
    "data center M&A deal",
    "colocation acquisition",
    "hyperscale data center investment",
    "data center joint venture",
    "data center power agreement GW MW",
]

# ============================================================
# DEAL SIGNAL DETECTION
# ============================================================

# Keywords that indicate a deal/transaction
DEAL_KEYWORDS = [
    'acquir', 'acquisition', 'acquire', 'acquired',
    'merger', 'merge', 'merging',
    'purchase', 'purchased', 'buy', 'bought',
    'invest', 'investment', 'investing',
    'joint venture', 'jv', 'partnership',
    'deal', 'transaction',
    'stake', 'equity', 'funding', 'raises',
    'ipo', 'goes public', 'public offering',
    'billion deal', 'million deal',
    '$', 'bn', 'gw', 'mw',
    'power agreement', 'ppa', 'power purchase',
    'lease', 'leasing', 'land acquisition',
    'breaks ground', 'new campus', 'new build',
    'capex', 'capital expenditure',
]

# Keywords that must also be present (DC relevance)
DC_KEYWORDS = [
    'data center', 'data centre', 'datacenter', 'datacentre',
    'colocation', 'colo', 'hyperscale', 'hyperscaler',
    'cloud infrastructure', 'digital infrastructure',
    'server farm', 'computing facility',
    'equinix', 'digital realty', 'cyrusone', 'coreweave',
    'qts', 'vantage', 'aligned', 'stack', 'compass',
    'edgeconnex', 'flexential', 'databank', 'tierpoint',
    'switch', 'cloudflare', 'aws', 'azure', 'gcp',
    'microsoft', 'google', 'amazon', 'meta', 'nvidia',
    'blackstone', 'brookfield', 'kkr', 'gip', 'macquarie',
]

# Negative keywords — skip these
NEGATIVE_KEYWORDS = [
    'laptop', 'phone', 'tablet', 'gaming', 'console',
    'wearable', 'smartwatch', 'headphone', 'earbuds',
    'tv ', 'television', 'streaming service',
    'social media', 'dating app', 'food delivery',
]

# ============================================================
# VALUE PARSING
# ============================================================

def parse_value_millions(text: str) -> Optional[float]:
    """Extract deal value in millions from text."""
    if not text:
        return None
    
    text = text.lower().replace(',', '')
    
    # $X billion / $Xbn / $X.Xb
    patterns_billion = [
        r'\$\s*([\d.]+)\s*(%s:billion|bn|b\b)',
        r'([\d.]+)\s*(%s:billion|bn)\s*(%s:dollar|usd|\$)',
        r'worth\s*\$%s\s*([\d.]+)\s*(%s:billion|bn|b\b)',
        r'valued%s\s*(%s:at)%s\s*\$%s\s*([\d.]+)\s*(%s:billion|bn|b\b)',
    ]
    for pattern in patterns_billion:
        match = re.search(pattern, text)
        if match:
            val = float(match.group(1))
            if 0.01 <= val <= 500:  # Sanity: $10M to $500B
                return val * 1000  # Convert to millions
    
    # $X million / $Xm
    patterns_million = [
        r'\$\s*([\d.]+)\s*(%s:million|mn|m\b)',
        r'([\d.]+)\s*(%s:million|mn)\s*(%s:dollar|usd|\$)',
    ]
    for pattern in patterns_million:
        match = re.search(pattern, text)
        if match:
            val = float(match.group(1))
            if 1 <= val <= 999:  # Sanity: $1M to $999M
                return val
    
    # $X,XXX (raw number likely in millions if > 100)
    raw_match = re.search(r'\$([\d,]+)', text.replace(',', ''))
    if raw_match:
        val = float(raw_match.group(1))
        if 100 <= val <= 50000:
            return val  # Assume millions
    
    return None


def parse_mw(text: str) -> Optional[float]:
    """Extract MW/GW capacity from text."""
    if not text:
        return None
    
    text = text.lower().replace(',', '')
    
    # GW patterns
    gw_match = re.search(r'([\d.]+)\s*gw', text)
    if gw_match:
        val = float(gw_match.group(1))
        if 0.01 <= val <= 100:
            return val * 1000  # Convert to MW
    
    # MW patterns
    mw_match = re.search(r'([\d.]+)\s*mw', text)
    if mw_match:
        val = float(mw_match.group(1))
        if 1 <= val <= 50000:
            return val
    
    return None


def parse_deal_type(text: str) -> str:
    """Classify deal type from text."""
    text = text.lower()
    
    if any(w in text for w in ['acquir', 'acquisition', 'purchase', 'bought', 'buy']):
        return 'M&A'
    if any(w in text for w in ['merger', 'merge']):
        return 'M&A'
    if any(w in text for w in ['joint venture', ' jv ', 'partnership']):
        return 'JV'
    if any(w in text for w in ['equity', 'funding', 'raise', 'investment round', 'series']):
        return 'Equity'
    if any(w in text for w in ['debt', 'loan', 'financing', 'credit facility']):
        return 'Debt'
    if any(w in text for w in ['ipo', 'goes public', 'public offering', 'listed']):
        return 'IPO'
    if any(w in text for w in ['power agreement', 'ppa', 'power purchase', 'energy deal']):
        return 'Power Agreement'
    if any(w in text for w in ['land', 'campus', 'breaks ground', 'new build', 'construction']):
        return 'New Build'
    if any(w in text for w in ['lease', 'leasing']):
        return 'Lease'
    if any(w in text for w in ['capex', 'capital expenditure', 'spending']):
        return 'CapEx'
    
    return 'M&A'  # Default


def extract_companies(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Try to extract buyer and seller from headline/summary."""
    text_lower = text.lower()
    
    # Known company patterns
    KNOWN_COMPANIES = [
        'Equinix', 'Digital Realty', 'CyrusOne', 'QTS', 'CoreWeave',
        'Vantage', 'Aligned', 'Stack', 'Compass', 'Switch',
        'EdgeConneX', 'Flexential', 'DataBank', 'TierPoint',
        'BlackRock', 'Blackstone', 'Brookfield', 'KKR', 'GIP',
        'Macquarie', 'Stonepeak', 'DigitalBridge', 'TPG', 'Ares',
        'Microsoft', 'Google', 'Amazon', 'Meta', 'Nvidia', 'AMD',
        'Apple', 'Oracle', 'OpenAI', 'Anthropic', 'xAI',
        'SoftBank', 'Constellation Energy', 'Duke Energy',
        'NRG Energy', 'Calpine', 'AirTrunk', 'atNorth',
        'CPP Investments', 'GIC', 'Mubadala', 'ADIA',
        'Hut 8', 'Riot Platforms', 'CleanSpark', 'Bitdeer',
        'Hyundai', 'Samsung', 'SK Telecom', 'NTT',
        'Mistral AI', 'EcoDataCenter', 'Serverfarm',
    ]
    
    buyer, seller = None, None
    
    # Pattern: "X acquires Y" / "X to acquire Y" / "X buys Y"
    acq_patterns = [
        r'([\w\s&/.]+%s)\s+(%s:acquires%s|buys%s|purchases%s|to acquire|to buy|to purchase)\s+([\w\s&/.]+%s)(%s:\s+(%s:for|in|from)|\s*$)',
        r'([\w\s&/.]+?)\s+(?:and|,)\s+([\w\s&/.]+?)\s+(?:acquire|buy|purchase)\s+([\w\s&/.]+)',
    ]
    
    for pattern in acq_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            buyer = match.group(1).strip()
            seller = match.group(2).strip() if match.lastindex >= 2 else None
            break
    
    # If no pattern match, find known companies in text
    if not buyer:
        found = []
        for company in KNOWN_COMPANIES:
            if company.lower() in text_lower:
                found.append(company)
        if len(found) >= 2:
            buyer = found[0]
            seller = found[1]
        elif len(found) == 1:
            buyer = found[0]
    
    # Clean up
    if buyer:
        buyer = buyer.strip(' .,;:')[:100]
    if seller:
        seller = seller.strip(' .,;:')[:100]
    
    return buyer, seller


def detect_region(text: str) -> str:
    """Detect deal region from text."""
    text = text.lower()
    
    na_signals = ['virginia', 'texas', 'phoenix', 'dallas', 'chicago', 'ohio',
                  'north carolina', 'indiana', 'nevada', 'california', 'oregon',
                  'us ', 'u.s.', 'united states', 'american', 'canada']
    emea_signals = ['uk', 'london', 'frankfurt', 'ireland', 'iceland', 'nordic',
                    'sweden', 'finland', 'european', 'europe', 'germany', 'france',
                    'israel', 'africa', 'middle east', 'uae', 'saudi']
    apac_signals = ['singapore', 'japan', 'tokyo', 'australia', 'sydney', 'india',
                    'south korea', 'indonesia', 'hong kong', 'malaysia', 'china',
                    'asia', 'apac']
    latam_signals = ['brazil', 'mexico', 'latin america', 'chile', 'colombia']
    
    if any(s in text for s in apac_signals):
        return 'APAC'
    if any(s in text for s in emea_signals):
        return 'EMEA'
    if any(s in text for s in latam_signals):
        return 'LATAM'
    if any(s in text for s in na_signals):
        return 'North America'
    
    return 'Global'


def detect_market(text: str) -> str:
    """Detect specific market from text."""
    text = text.lower()
    
    markets = {
        'Northern Virginia': ['northern virginia', 'nova', 'loudoun', 'ashburn', 'prince william'],
        'Dallas-Fort Worth': ['dallas', 'fort worth', 'dfw', 'texas'],
        'Phoenix': ['phoenix', 'mesa', 'chandler', 'arizona'],
        'Chicago': ['chicago', 'illinois'],
        'Silicon Valley': ['silicon valley', 'santa clara', 'san jose'],
        'North Carolina': ['north carolina', 'charlotte', 'raleigh', 'durham'],
        'Indiana': ['indiana', 'indianapolis', 'lebanon'],
        'Nevada': ['nevada', 'las vegas', 'reno'],
        'Ohio': ['ohio', 'columbus', 'new albany'],
        'Oregon': ['oregon', 'hillsboro', 'portland'],
        'London': ['london'],
        'Frankfurt': ['frankfurt'],
        'Singapore': ['singapore'],
        'Tokyo': ['tokyo', 'japan'],
        'Sydney': ['sydney', 'australia'],
        'Iceland': ['iceland', 'reykjavik'],
        'Sweden': ['sweden', 'stockholm'],
        'South Korea': ['south korea', 'korea'],
        'Israel': ['israel', 'ashdod', 'tel aviv'],
    }
    
    for market, signals in markets.items():
        if any(s in text for s in signals):
            return market
    
    return 'Global'


# ============================================================
# FEED FETCHING
# ============================================================

def fetch_rss_articles(feed: Dict, max_age_days: int = 7) -> List[Dict]:
    """Fetch articles from an RSS feed."""
    articles = []
    try:
        parsed = feedparser.parse(feed['url'])
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        
        for entry in parsed.entries[:30]:  # Max 30 per feed
            # Parse date
            pub_date = None
            for date_field in ['published_parsed', 'updated_parsed', 'created_parsed']:
                dt = getattr(entry, date_field, None)
                if dt:
                    try:
                        pub_date = datetime(*dt[:6])
                        break
                    except:
                        pass
            
            if pub_date and pub_date < cutoff:
                continue
            
            title = getattr(entry, 'title', '')
            summary = getattr(entry, 'summary', getattr(entry, 'description', ''))
            link = getattr(entry, 'link', '')
            
            # Strip HTML from summary
            summary = re.sub(r'<[^>]+>', '', summary)[:500]
            
            articles.append({
                'title': title,
                'summary': summary,
                'url': link,
                'source': feed['name'],
                'priority': feed['priority'],
                'published': pub_date.isoformat() if pub_date else datetime.utcnow().isoformat(),
            })
    except Exception as e:
        logger.warning(f"Failed to fetch {feed['name']}: {e}")
    
    return articles


def fetch_google_news_deals(max_age_days: int = 7) -> List[Dict]:
    """Fetch deal-related articles from Google News RSS."""
    articles = []
    
    for query in GOOGLE_NEWS_QUERIES:
        try:
            url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en"
            parsed = feedparser.parse(url)
            
            for entry in parsed.entries[:10]:
                title = getattr(entry, 'title', '')
                link = getattr(entry, 'link', '')
                source = getattr(entry, 'source', {})
                source_name = source.get('title', 'Google News') if isinstance(source, dict) else str(source)
                
                pub_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:6])
                    except:
                        pass
                
                articles.append({
                    'title': title,
                    'summary': '',
                    'url': link,
                    'source': source_name,
                    'priority': 3,
                    'published': pub_date.isoformat() if pub_date else datetime.utcnow().isoformat(),
                })
        except Exception as e:
            logger.warning(f"Google News query failed for '{query}': {e}")
    
    return articles


# ============================================================
# DEAL EXTRACTION PIPELINE
# ============================================================

def is_deal_article(title: str, summary: str, priority: int) -> bool:
    """Check if article contains deal signals."""
    combined = (title + ' ' + summary).lower()
    
    # Skip negative keywords
    if any(neg in combined for neg in NEGATIVE_KEYWORDS):
        return False
    
    # Must have at least one deal keyword
    has_deal_signal = any(kw in combined for kw in DEAL_KEYWORDS)
    if not has_deal_signal:
        return False
    
    # Priority 1 (DC-specific sources) — deal keyword is enough
    if priority == 1:
        return True
    
    # Priority 2-3 — also need DC relevance
    has_dc_signal = any(kw in combined for kw in DC_KEYWORDS)
    return has_dc_signal


def article_to_deal(article: Dict) -> Optional[Dict]:
    """Extract structured deal data from an article."""
    title = article.get('title', '')
    summary = article.get('summary', '')
    combined = title + ' ' + summary
    
    buyer, seller = extract_companies(combined)
    
    # Must have at least a buyer
    if not buyer:
        return None
    
    # Skip if buyer looks like a parsed headline fragment
    if len(buyer) < 2 or buyer.lower() in ['it', 'a', 'the', 'this', 'that', 'deal', 'billion']:
        return None
    if any(w in buyer.lower() for w in ['deal', ' bn ', 'bn deal', 'expand power', 'rootmetrics']):
        return None
    if len(buyer) > 60:
        return None
    
    value = parse_value_millions(combined)
    if value and value > 50000:
        return None
    # Cap value at $50B (50000M) to prevent CapEx/Stargate inflation
    if value and value > 50000:
        return None
    mw = parse_mw(combined)
    deal_type = parse_deal_type(combined)
    region = detect_region(combined)
    market = detect_market(combined)
    
    # Skip CapEx announcements (they inflate stats)
    if deal_type == 'CapEx':
        return None
    
    # Generate deterministic ID from key fields
    id_seed = f"{buyer}:{seller}:{value}:{article.get('published', '')[:10]}"
    deal_id = f"AUTO-{datetime.utcnow().strftime('%Y%m%d')}-{hashlib.md5(id_seed.encode()).hexdigest()[:6]}"
    
    return {
        'id': deal_id,
        'buyer': buyer,
        'seller': seller,
        'value': value,
        'mw': mw,
        'market': market,
        'date': article.get('published', '')[:10],
        'year': int(article.get('published', '2026')[:4]) if article.get('published') else 2026,
        'type': deal_type,
        'region': region,
        'status': 'announced',
        'notes': title[:200],
    }


# ============================================================
# NEON DATABASE
# ============================================================

def get_neon_connection():
    """Get a psycopg2 connection to Neon."""
    try:
        import psycopg2
    except ImportError:
        logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        return None
    
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        return None
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Neon connection failed: {e}")
        return None


def insert_deals(deals: List[Dict], dry_run: bool = False) -> int:
    """Insert deals into Neon, skip duplicates."""
    if not deals:
        return 0
    
    if dry_run:
        for d in deals:
            logger.info(f"  [DRY RUN] {d['buyer']} → {d['seller']} | ${d['value']}M | {d['type']} | {d['market']}")
        return len(deals)
    
    conn = get_neon_connection()
    if not conn:
        return 0
    
    inserted = 0
    try:
        cur = conn.cursor()
        for d in deals:
            try:
                cur.execute("""
                    INSERT INTO deals (id, buyer, seller, value, mw, market, date, year, type, region, status, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    d['id'], d['buyer'], d['seller'], d['value'], d['mw'],
                    d['market'], d['date'], d['year'], d['type'], d['region'],
                    d['status'], d['notes']
                ))
                if cur.rowcount > 0:
                    inserted += 1
                    logger.info(f"  ✅ NEW: {d['buyer']} → {d['seller']} | ${d.get('value', '?')}M | {d['type']}")
            except Exception as e:
                logger.warning(f"  ❌ Insert failed for {d['id']}: {e}")
                conn.rollback()
                continue
        conn.commit()
    except Exception as e:
        logger.error(f"Database error: {e}")
        conn.rollback()
    finally:
        conn.close()
    
    return inserted


def get_deal_stats() -> Dict:
    """Get current deal table stats."""
    conn = get_neon_connection()
    if not conn:
        return {}
    
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN value IS NOT NULL THEN 1 END) as with_value,
                ROUND(SUM(COALESCE(value, 0))::numeric, 0) as total_value_m,
                MAX(value) as max_value_m,
                MAX(date) as newest,
                MIN(date) as oldest
            FROM deals
        """)
        row = cur.fetchone()
        return {
            'total_deals': row[0],
            'deals_with_value': row[1],
            'total_value_millions': float(row[2] or 0),
            'max_deal_millions': float(row[3] or 0),
            'newest_deal': row[4],
            'oldest_deal': row[5],
        }
    except Exception as e:
        logger.error(f"Stats query failed: {e}")
        return {}
    finally:
        conn.close()


# ============================================================
# MAIN SCRAPE PIPELINE
# ============================================================

def run_scrape(dry_run: bool = False) -> Dict:
    """Full deal scrape pipeline."""
    logger.info("=" * 60)
    logger.info("🔍 DC Hub Deal Scraper — Starting")
    logger.info("=" * 60)
    
    # Step 1: Fetch articles from all sources
    all_articles = []
    
    logger.info(f"\n📡 Fetching from {len(DEAL_FEEDS)} RSS feeds...")
    for feed in DEAL_FEEDS:
        articles = fetch_rss_articles(feed, max_age_days=7)
        all_articles.extend(articles)
        if articles:
            logger.info(f"  {feed['name']}: {len(articles)} articles")
    
    logger.info(f"\n🔎 Fetching from Google News ({len(GOOGLE_NEWS_QUERIES)} queries)...")
    gn_articles = fetch_google_news_deals(max_age_days=7)
    all_articles.extend(gn_articles)
    logger.info(f"  Google News: {len(gn_articles)} articles")
    
    logger.info(f"\n📊 Total articles fetched: {len(all_articles)}")
    
    # Step 2: Filter for deal signals
    deal_articles = []
    for article in all_articles:
        if is_deal_article(article['title'], article['summary'], article['priority']):
            deal_articles.append(article)
    
    logger.info(f"📈 Articles with deal signals: {len(deal_articles)}")
    
    # Step 3: Extract structured deal data
    deals = []
    seen_keys = set()
    
    for article in deal_articles:
        deal = article_to_deal(article)
        if deal:
            # Dedup by buyer+seller+date
            dedup_key = f"{deal['buyer']}:{deal.get('seller', '')}:{deal['date']}"
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                deals.append(deal)
    
    logger.info(f"💰 Deals extracted: {len(deals)} (after dedup)")
    
    # Step 4: Insert into database
    if deals:
        inserted = insert_deals(deals, dry_run=dry_run)
        logger.info(f"\n✅ Inserted {inserted} new deals into Neon")
    else:
        inserted = 0
        logger.info("\n⚠️  No new deals found this cycle")
    
    # Step 5: Summary
    stats = get_deal_stats() if not dry_run else {}
    
    result = {
        'articles_fetched': len(all_articles),
        'deal_signals': len(deal_articles),
        'deals_extracted': len(deals),
        'deals_inserted': inserted,
        'dry_run': dry_run,
        'timestamp': datetime.utcnow().isoformat(),
        'db_stats': stats,
    }
    
    logger.info(f"\n{'=' * 60}")
    logger.info(f"📊 Scrape complete: {inserted} new deals added")
    if stats:
        logger.info(f"   Total deals in DB: {stats.get('total_deals', '?')}")
        logger.info(f"   Newest deal: {stats.get('newest_deal', '?')}")
    logger.info(f"{'=' * 60}\n")
    
    return result


# ============================================================
# FLASK INTEGRATION (for Railway /api/deals/refresh endpoint)
# ============================================================

def register_deal_scraper_routes(app):
    """Register deal scraper endpoints on a Flask app."""
    from flask import jsonify
    
# AUTO-REPAIR: duplicate route '/api/deals/refresh' also in main.py:14256 — review and remove one
    @app.route('/api/deals/refresh', methods=['POST'])
    def refresh_deals():
        """Trigger a manual deal scrape."""
        result = run_scrape(dry_run=False)
        return jsonify(result)
    
    @app.route('/api/deals/refresh/dry-run', methods=['GET'])
    def dry_run_deals():
        """Preview what deals would be found."""
        result = run_scrape(dry_run=True)
        return jsonify(result)
    
    @app.route('/api/deals/stats', methods=['GET'])
    def deal_stats():
        """Get deal table health stats."""
        stats = get_deal_stats()
        return jsonify(stats)
    
    logger.info("💰 Deal scraper routes registered: /api/deals/refresh, /api/deals/stats")


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    args = sys.argv[1:]
    
    if '--stats' in args:
        stats = get_deal_stats()
        print(json.dumps(stats, indent=2, default=str))
    elif '--dry-run' in args:
        run_scrape(dry_run=True)
    else:
        run_scrape(dry_run=False)
