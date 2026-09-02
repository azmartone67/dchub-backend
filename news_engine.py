"""
DC Hub News Aggregation Engine v3.0
===================================
Syncs articles into PostgreSQL (Neon) via db_utils.
All SQLite references removed.
"""

import feedparser
from util import feed_fetch  # bounded (connect, read) feed I/O — feedparser.parse(url) has NO timeout
import requests
import hashlib
import html as _html
import re
import json
import os
import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote_plus, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from db_utils import get_db
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)

MAIN_DB_PATH = 'pg'
NEWS_DB_PATH = 'pg'

RSS_FEEDS = [
    # PRIORITY 1: DC-SPECIFIC (all articles accepted)
    {"name": "Data Center Dynamics", "url": "https://www.datacenterdynamics.com/en/rss/news/", "category": "Industry", "priority": 1},
    {"name": "Data Center Knowledge", "url": "https://www.datacenterknowledge.com/rss.xml", "category": "Industry", "priority": 1},
    {"name": "Data Center Frontier", "url": "https://www.datacenterfrontier.com/rss", "category": "Industry", "priority": 1},
    {"name": "Datacenter Hawk", "url": "https://www.datacenterhawk.com/blog/rss.xml", "category": "Market", "priority": 1},
    {"name": "TechTarget DC", "url": "https://www.techtarget.com/searchdatacenter/rss/ContentSyndication.xml", "category": "Industry", "priority": 1},
    {"name": "The Register DC", "url": "https://www.theregister.com/data_centre/headlines.atom", "category": "Industry", "priority": 1},
    {"name": "Capacity Media", "url": "https://www.capacitymedia.com/rss", "category": "Industry", "priority": 1},
    {"name": "Datacenter News Asia", "url": "https://datacenternews.asia/rss/feed", "category": "Industry", "priority": 1},
    # PRIORITY 2: ADJACENT (require DC keyword)
    {"name": "SDxCentral", "url": "https://www.sdxcentral.com/feed/", "category": "Industry", "priority": 2},
    {"name": "Cloud Computing News", "url": "https://www.cloudcomputing-news.net/feed/", "category": "Cloud", "priority": 2},
    {"name": "The New Stack", "url": "https://thenewstack.io/feed/", "category": "Cloud", "priority": 2},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": "AI", "priority": 2},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/", "category": "AI", "priority": 2},
    {"name": "Utility Dive", "url": "https://www.utilitydive.com/feeds/news/", "category": "Energy", "priority": 2},
    {"name": "Energy Storage News", "url": "https://www.energy-storage.news/feed/", "category": "Energy", "priority": 2},
    {"name": "CNBC Tech", "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "category": "Financial", "priority": 2},
    {"name": "Bloomberg Tech", "url": "https://feeds.bloomberg.com/technology/news.rss", "category": "Financial", "priority": 2},
    {"name": "Light Reading", "url": "https://www.lightreading.com/rss_simple.asp", "category": "Telecom", "priority": 2},
    {"name": "GlobeSt", "url": "https://www.globest.com/feed/", "category": "Real Estate", "priority": 2},
    {"name": "Bisnow National", "url": "https://www.bisnow.com/feed", "category": "Real Estate", "priority": 2},
    # PRIORITY 3: GENERAL TECH (strict DC keyword filtering)
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "category": "Tech", "priority": 3},
    {"name": "ZDNet", "url": "https://www.zdnet.com/news/rss.xml", "category": "Tech", "priority": 3},
    {"name": "Network World", "url": "https://www.networkworld.com/index.rss", "category": "Tech", "priority": 3},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "category": "Tech", "priority": 3},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "Tech", "priority": 3},
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/", "category": "AI", "priority": 3},
    {"name": "Reuters Business", "url": "https://www.reutersagency.com/feed/?best-sectors=business-finance&post_type=best", "category": "Financial", "priority": 3},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "Financial", "priority": 3},
    {"name": "InfoWorld", "url": "https://www.infoworld.com/index.rss", "category": "Cloud", "priority": 3},
    {"name": "PV Magazine", "url": "https://www.pv-magazine.com/feed/", "category": "Energy", "priority": 3},
    {"name": "Commercial Property Exec", "url": "https://www.commercialsearch.com/news/feed/", "category": "Real Estate", "priority": 3},
    {"name": "Telecoms.com", "url": "https://telecoms.com/feed/", "category": "Telecom", "priority": 3},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "category": "Tech", "priority": 3},
    {"name": "Slashdot", "url": "https://rss.slashdot.org/Slashdot/slashdotMain", "category": "Tech", "priority": 3},
]

DC_KEYWORDS = [
    'data center', 'data centre', 'datacenter', 'datacentre', 'colocation', 'colo ',
    'hyperscale', 'edge computing', 'server farm', 'cloud infrastructure',
    'equinix', 'digital realty', 'cyrusone', 'qts realty', 'coresite', 'vantage data',
    'flexential', 'compass datacenters', 'stack infrastructure', 'prime data centers',
    'aligned data', 'switch data', 'ntt data center', 'kddi data', 'cologix',
    'databank', 'tierpoint', 'sabey', 'h5 data', 'stream data', 'edgeconnex',
    'yondr', 'salute mission critical', 'virtus data', 'global switch',
    'aws region', 'amazon web services', 'microsoft azure', 'google cloud', 'gcp region',
    'meta data center', 'facebook data center', 'oracle cloud infrastructure',
    'alibaba cloud', 'tencent cloud', 'bytedance data',
    'nvidia h100', 'nvidia h200', 'nvidia b100', 'nvidia b200', 'gpu cluster',
    'ai infrastructure', 'ai data center', 'ml infrastructure', 'training cluster',
    'inference cluster', 'ai supercomputer', 'ai factory', 'gpu data center',
    'server rack', 'cooling system', 'ups system', 'power density', 'pue rating',
    'liquid cooling', 'immersion cooling', 'direct-to-chip', 'rear door heat',
    'tier iii', 'tier iv', 'tier 3', 'tier 4', 'uptime institute',
    'megawatt', 'gigawatt', ' mw ', ' gw ', 'power purchase agreement', 'ppa ',
    'renewable energy data', 'nuclear data center', 'small modular reactor',
    'wholesale data center', 'retail colocation', 'interconnection', 'cloud on-ramp',
    'fiber connectivity', 'network neutral', 'carrier hotel',
    'data center acquisition', 'data center investment', 'data center deal',
    'data center construction', 'data center development', 'data center campus',
    'powered shell', 'build-to-suit', 'data center reit', 'digital infrastructure',
    'stargate project', 'project stargate',
]

NEGATIVE_KEYWORDS = [
    'recipe', 'sports score', 'movie review', 'celebrity gossip', 'horoscope',
    'weather forecast', 'lottery results', 'dating app', 'weight loss',
    'fantasy football', 'video game review', 'tv show recap',
    'best laptop', 'best phone', 'best tv', 'best headphone', 'best tablet',
    'iphone review', 'galaxy review', 'pixel review', 'macbook review',
    'smartwatch', 'fitness tracker', 'wireless earbuds', 'bluetooth speaker',
    'streaming service', 'netflix', 'disney+', 'hulu', 'spotify',
    'privacy display', 'screen protector', 'phone case', 'back pain',
    'skincare', 'travel tips', 'vacation', 'holiday deals', 'black friday',
    'video game', 'gaming laptop', 'gaming console', 'playstation', 'xbox',
    'tv gadget', 'bravia tv', 'oled tv', 'samsung galaxy',
]

GOOGLE_NEWS_QUERIES = [
    'data center news today', 'hyperscale data center',
    'colocation data center deal', 'data center construction 2026',
    'data center acquisition 2026', 'cloud infrastructure investment',
    'AI data center GPU', 'data center power grid',
    'liquid cooling data center', 'Equinix data center',
    'Digital Realty expansion', 'Microsoft Azure data center',
    'AWS data center', 'Google data center',
    'Meta AI data center', 'Nvidia AI infrastructure',
]


# ============================================================
# DATABASE SETUP
# ============================================================

def init_news_db(db_path=NEWS_DB_PATH):
    conn = get_db(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS news_articles (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL, category TEXT DEFAULT 'Industry', summary TEXT,
        content TEXT, author TEXT, published_at DATETIME,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        relevance_score REAL DEFAULT 0.5, sentiment TEXT, keywords TEXT,
        image_url TEXT, is_breaking INTEGER DEFAULT 0,
        view_count INTEGER DEFAULT 0, share_count INTEGER DEFAULT 0,
        publisher_url TEXT)''')
    # ★ 2026-09-02. 70% of news_articles.url (9,100 of 13,009) are
    # news.google.com/rss/articles/CBMi... aggregator links. Those are the
    # post-2024 opaque `AU_yq...` tokens: nothing is base64-encoded inside them
    # (verified against live rows) and following one returns 200 with a
    # JavaScript interstitial, NOT a redirect — the destination is obtainable
    # only from Google's private batchexecute endpoint. The article URL
    # therefore CANNOT be resolved at ingest, and this column does not pretend
    # to. It stores what the feed does give, on 100% of entries:
    # <source url="..."/>, the publisher's own domain and name. A row whose
    # `url` is an opaque token can then still say WHO published it.
    #
    # ALTER runs separately because CREATE TABLE IF NOT EXISTS is a no-op
    # against the existing table and would leave the column missing on every
    # already-deployed database.
    try:
        c.execute('ALTER TABLE news_articles '
                  'ADD COLUMN IF NOT EXISTS publisher_url TEXT')
    except Exception:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS feed_health (
        url TEXT PRIMARY KEY, name TEXT, last_success DATETIME,
        last_failure DATETIME, failure_count INTEGER DEFAULT 0,
        success_count INTEGER DEFAULT 0, avg_articles_per_fetch REAL DEFAULT 0,
        is_active INTEGER DEFAULT 1)''')
    for idx_sql in [
        'CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_news_source ON news_articles(source)',
        'CREATE INDEX IF NOT EXISTS idx_news_fetched ON news_articles(fetched_at DESC)',
    ]:
        try:
            c.execute(idx_sql)
            conn.commit()
        except Exception as idx_err:
            try:
                conn.rollback()
                conn.commit()  # Start fresh transaction for next article
            except Exception:
                pass
            logger.warning(f"Index creation skipped (likely exists or timeout): {idx_err}")
    conn.close()
    logger.info("✅ News DB initialized (dchub.db)")


def ensure_announcements_table(db_path=MAIN_DB_PATH):
    """Ensure announcements table exists in dc_nexus.db."""
    conn = None
    try:
        conn = get_db(db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS announcements (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT,
            source_url TEXT, source TEXT, published_date TEXT,
            discovered_at TEXT, category TEXT DEFAULT 'Industry', url TEXT)''')
        try:
            c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'announcements' AND column_name = 'category'")
            if not c.fetchone():
                c.execute("ALTER TABLE announcements ADD COLUMN category TEXT DEFAULT 'Industry News'")
                logger.info("Added missing 'category' column to announcements table")
        except Exception:
            pass
        c.execute('CREATE INDEX IF NOT EXISTS idx_ann_pubdate ON announcements(published_date DESC)')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Could not ensure announcements table: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# ARTICLE PROCESSING
# ============================================================

def generate_article_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:16]

def is_relevant_article(title, summary=''):
    text = f"{title} {summary}".lower()
    for neg in NEGATIVE_KEYWORDS:
        if neg.lower() in text:
            return False
    for kw in DC_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

def calculate_relevance_score(title, summary='', source=''):
    text = f"{title} {summary}".lower()
    for neg in NEGATIVE_KEYWORDS:
        if neg.lower() in text:
            return 0.0
    matches = high = 0
    for kw in DC_KEYWORDS:
        if kw.lower() in text:
            matches += 1
            if kw in ['data center','data centre','hyperscale','colocation','megawatt',
                       'equinix','digital realty','edgeconnex','liquid cooling',
                       'ai infrastructure','ai data center','gpu cluster']:
                high += 1
    base = min(matches * 0.08, 0.4)
    bonus = min(high * 0.12, 0.35)
    src_bonus = 0
    sl = source.lower()
    for f in RSS_FEEDS:
        if f['name'].lower() in sl or sl in f['name'].lower():
            src_bonus = 0.25 if f.get('priority',3)==1 else (0.15 if f.get('priority',3)==2 else 0)
            break
    return min(base + bonus + src_bonus, 1.0)

def parse_date(date_str):
    if not date_str: return None
    if isinstance(date_str, datetime): return date_str
    if hasattr(date_str, 'tm_year'):
        try: return datetime(*date_str[:6])
        except: pass
    if not isinstance(date_str, str): return None
    for fmt in ['%a, %d %b %Y %H:%M:%S %z','%a, %d %b %Y %H:%M:%S %Z',
                '%Y-%m-%dT%H:%M:%S%z','%Y-%m-%dT%H:%M:%SZ','%Y-%m-%dT%H:%M:%S.%f%z',
                '%Y-%m-%d %H:%M:%S','%Y-%m-%d','%d %b %Y %H:%M:%S']:
        try: return datetime.strptime(date_str, fmt)
        except: continue
    try:
        import email.utils
        return email.utils.parsedate_to_datetime(date_str)
    except: pass
    return None

def extract_keywords(title, summary=''):
    text = f"{title} {summary}".lower()
    return [kw for kw in DC_KEYWORDS if kw.lower() in text][:10]

def categorize_article(title, summary='', source_category='Industry'):
    text = f"{title} {summary}".lower()
    if any(k in text for k in ['acquisition','merger','deal','buys','acquires','purchase','ipo','funding round','raises']): return 'M&A'
    if any(k in text for k in ['construction','development','build','expansion','campus','new facility','groundbreaking','breaks ground','new data center']): return 'Expansion'
    if any(k in text for k in ['ai ','artificial intelligence','gpu','nvidia','machine learning','llm','training cluster','inference','ai factory']): return 'AI'
    if any(k in text for k in ['power outage','grid','renewable','solar','wind','nuclear','ppa','energy deal','electricity','megawatt','gigawatt','smr']): return 'Power'
    if any(k in text for k in ['cooling','liquid','immersion','thermal','heat']): return 'Cooling'
    if any(k in text for k in ['sustainability','carbon','green','esg','net zero','emissions']): return 'Sustainability'
    if any(k in text for k in ['cloud','aws region','azure region','gcp region','saas','iaas']): return 'Cloud'
    if any(k in text for k in ['fiber','network','connectivity','interconnect','5g','telecom']): return 'Network'
    return source_category


# ============================================================
# RSS FEED FETCHING
# ============================================================

def fetch_single_feed(feed_info, db_path=NEWS_DB_PATH):
    url = feed_info['url']
    name = feed_info['name']
    category = feed_info.get('category', 'Industry')
    priority = feed_info.get('priority', 3)
    articles = []
    try:
        feed = feed_fetch.parse_feed(url,
            agent='Mozilla/5.0 (compatible; DCHub/3.0; +https://dchub.cloud)',
            request_headers={'Accept': 'application/rss+xml, application/xml, text/xml, */*'})
        if feed.bozo and not feed.entries:
            raise Exception(f"Feed error: {getattr(feed, 'bozo_exception', 'Unknown')}")

        for entry in feed.entries[:50]:
            try:
                title = entry.get('title', '').strip()
                link = entry.get('link', '').strip()
                if not title or not link: continue

                summary = entry.get('summary', entry.get('description', '')).strip()
                summary = re.sub(r'<[^>]+>', '', summary)
                summary = _html.unescape(summary)  # decode &nbsp; &amp; &#39; etc.
                summary = re.sub(r'\s+', ' ', summary).strip()[:500]

                # STRICT FILTERING
                if priority == 1:
                    text_check = f"{title} {summary}".lower()
                    if any(neg.lower() in text_check for neg in NEGATIVE_KEYWORDS):
                        continue
                else:
                    if not is_relevant_article(title, summary):
                        continue

                pub_date = None
                for df in ['published_parsed','updated_parsed','created_parsed']:
                    if hasattr(entry, df) and getattr(entry, df):
                        pub_date = parse_date(getattr(entry, df))
                        if pub_date: break
                if not pub_date:
                    for df in ['published','updated','created']:
                        if hasattr(entry, df) and getattr(entry, df):
                            pub_date = parse_date(getattr(entry, df))
                            if pub_date: break

                image_url = None
                if hasattr(entry, 'media_content') and entry.media_content:
                    image_url = entry.media_content[0].get('url')
                elif hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
                    image_url = entry.media_thumbnail[0].get('url')
                elif hasattr(entry, 'enclosures') and entry.enclosures:
                    for enc in entry.enclosures:
                        if 'image' in enc.get('type', ''):
                            image_url = enc.get('href') or enc.get('url')
                            break

                articles.append({
                    'id': generate_article_id(link), 'title': title, 'url': link,
                    'source': name, 'category': categorize_article(title, summary, category),
                    'summary': summary, 'author': entry.get('author', ''),
                    'published_at': pub_date,
                    'relevance_score': calculate_relevance_score(title, summary, name),
                    'keywords': json.dumps(extract_keywords(title, summary)),
                    'image_url': image_url,
                })
            except Exception: continue

        update_feed_health(url, name, success=True, article_count=len(articles), db_path=db_path)
        return (name, len(articles), articles)
    except Exception:
        update_feed_health(url, name, success=False, db_path=db_path)
        return (name, 0, [])


def fetch_all_rss_feeds(db_path=NEWS_DB_PATH):
    logger.info(f"📰 Fetching from {len(RSS_FEEDS)} RSS feeds...")
    all_articles = []
    successful = 0
    failed = []
    # POOL FIX: Reduced from 15 to 5 workers. 15 threads each calling
    # update_feed_health → get_db() → pool checkout was the #1 cause of
    # pool exhaustion during news sync. 5 workers still fetches all 34
    # feeds in ~20s but keeps pool pressure to max 5 concurrent checkouts.
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_single_feed, f, db_path): f for f in RSS_FEEDS}
        for future in as_completed(futures, timeout=90):
            try:
                name, count, articles = future.result()
                if count > 0:
                    successful += 1
                    all_articles.extend(articles)
                    logger.info(f"   ✅ {name}: {count} articles")
                else:
                    failed.append(name)
            except Exception:
                failed.append(futures[future]['name'])
    if failed:
        logger.info(f"   ⚠️ Failed/empty: {', '.join(failed[:8])}{'...' if len(failed)>8 else ''}")
    all_articles = _drop_implausibly_future(all_articles, 'rss')
    logger.info(f"📊 RSS Total: {len(all_articles)} relevant articles from {successful}/{len(RSS_FEEDS)} feeds")
    return all_articles


def update_feed_health(url, name, success, article_count=0, db_path=NEWS_DB_PATH):
    """Update feed health tracking. Uses get_db which routes to pool.
    POOL FIX: Added proper finally-close to prevent leaked connections."""
    conn = None
    try:
        conn = get_db(db_path)
        c = conn.cursor()
        now = datetime.utcnow().isoformat()
        if success:
            c.execute('''INSERT INTO feed_health (url,name,last_success,success_count,avg_articles_per_fetch)
                VALUES (%s,%s,%s,1,%s) ON CONFLICT(url) DO UPDATE SET last_success=EXCLUDED.last_success,
                success_count=feed_health.success_count+1, failure_count=0,
                avg_articles_per_fetch=(feed_health.avg_articles_per_fetch*0.7+EXCLUDED.avg_articles_per_fetch*0.3), is_active=1''',
                (url, name, now, article_count))
        else:
            c.execute('''INSERT INTO feed_health (url,name,last_failure,failure_count)
                VALUES (%s,%s,%s,1) ON CONFLICT(url) DO UPDATE SET last_failure=EXCLUDED.last_failure,
                failure_count=feed_health.failure_count+1,
                is_active=CASE WHEN feed_health.failure_count>=10 THEN 0 ELSE feed_health.is_active END''',
                (url, name, now))
        conn.commit()
    except Exception:
        note_swallowed_write("feed_health", where="news_engine.update_feed_health")
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# GOOGLE NEWS
# ============================================================

def fetch_google_news(query, max_results=20):
    articles = []
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        feed = feed_fetch.parse_feed(url, agent='Mozilla/5.0 (compatible; DCHub/3.0; +https://dchub.cloud)')
        for entry in feed.entries[:max_results]:
            title = entry.get('title','').strip()
            link = entry.get('link','').strip()
            if not title or not link: continue
            # ★ The feed's <source> element is authoritative and present on
            # 100% of entries (measured 2026-09-02). The ' - ' split below
            # GUESSES the publisher out of the headline and silently leaves
            # 'Google News' whenever the headline has no dash or a long tail —
            # crediting the aggregator instead of whoever wrote the story.
            # Prefer the element; keep the split only to TRIM the redundant
            # suffix off the title.
            _src = entry.get('source') or {}
            _src_name = (_src.get('title') or '').strip()
            _src_href = (_src.get('href') or '').strip()
            source = 'Google News'
            if ' - ' in title:
                parts = title.rsplit(' - ', 1)
                if len(parts)==2 and len(parts[1])<50:
                    title, source = parts[0], parts[1]
            if _src_name:
                source = _src_name
            summary = re.sub(r'<[^>]+>','', entry.get('summary','').strip())
            summary = _html.unescape(summary)  # decode &nbsp; &amp; &#39; etc.
            summary = re.sub(r'\s+',' ', summary).strip()[:500]
            title = _html.unescape(title)
            if not is_relevant_article(title, summary): continue
            pub_date = None
            if hasattr(entry,'published_parsed') and entry.published_parsed:
                pub_date = parse_date(entry.published_parsed)
            elif hasattr(entry,'published'):
                pub_date = parse_date(entry.published)
            articles.append({
                'id': generate_article_id(link), 'title': title, 'url': link,
                'source': source, 'category': categorize_article(title, summary),
                'publisher_url': _src_href or None,
                'summary': summary, 'published_at': pub_date,
                'relevance_score': calculate_relevance_score(title, summary, source),
                'keywords': json.dumps(extract_keywords(title, summary)),
            })
    except Exception as e:
        logger.warning(f"   ⚠️ Google News ({query[:20]}...): {str(e)[:30]}")
    return articles

def fetch_all_google_news():
    logger.info(f"🔍 Searching Google News ({len(GOOGLE_NEWS_QUERIES)} queries)...")
    all_articles = []
    for i, query in enumerate(GOOGLE_NEWS_QUERIES):
        articles = fetch_google_news(query, max_results=15)
        all_articles.extend(articles)
        if articles: logger.info(f"   📰 '{query[:25]}...': {len(articles)} articles")
        if i < len(GOOGLE_NEWS_QUERIES)-1: time.sleep(0.3)
    seen = set()
    unique = [a for a in all_articles if a['url'] not in seen and not seen.add(a['url'])]
    unique = _drop_implausibly_future(unique, 'google_news')
    logger.info(f"📊 Google News total: {len(unique)} unique articles")
    return unique


# ============================================================
# ARTICLE STORAGE
# ============================================================

def _clamp_future_published_at(pub, now=None):
    """Clamp a future-dated published_at to now; leave past/unparseable as-is.

    Phase r33-J+sweep (2026-05-21) put this clamp INLINE in save_articles (the
    SQLite path). ★2026-08-22, two things it never did:
      1. `_sync_articles_to_pg` — the write-through that fills the news_articles
         table every agent read actually serves — had NO clamp at all, so a Data
         Center Knowledge /events/ landing page carrying its EVENT date
         (2026-09-21T11:00:00) landed in Postgres untouched and sat at
         articles[0] on the REST alias for weeks (#2216 only filtered the MCP
         read path).
      2. The inline version compared a tz-NAIVE parsed date against an AWARE
         now (`datetime.now(tzinfo or timezone.utc)`), which raises TypeError on
         exactly the naive ISO strings RSS yields — caught by `except: pass`, so
         it clamped nothing it was built to clamp. A guard that cannot fail its
         own input is not a guard.
    ONE helper, BOTH writers, naive and aware both handled. Unparseable values
    are returned unchanged — never invent a date.
    """
    if isinstance(pub, datetime):
        pub = pub.isoformat()
    if not pub or not isinstance(pub, str):
        return pub
    try:
        dt = datetime.fromisoformat(pub.strip().replace('Z', '+00:00'))
    except Exception:
        return pub
    ref = now
    if dt.tzinfo is None:
        if ref is None:
            ref = datetime.utcnow()
        elif ref.tzinfo is not None:
            ref = ref.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        if ref is None:
            ref = datetime.now(timezone.utc)
        elif ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
    if dt > ref:
        return ref.isoformat()
    return pub


# ★2026-08-22 (owner call): a published_at more than FUTURE_REJECT_HOURS ahead
# of now is not a publication time — it is the EVENT date scraped off a
# conference landing page (the DCK /events/ row carried 2026-09-21 and sat at
# the top of /api/news for weeks, then poisoned MAX(published_date) behind
# /api/health/data-freshness -> MCP get_backup_status). Such an item is not
# news: REJECT it at the fetchers (the choke point every writer shares) rather
# than clamp it to "now" and publish a conference as today's headline. The 0h
# clamp above stays for the 0-24h band (timezone slop), so nothing a reader or
# the 6h freshness check sees is ever in the future.
FUTURE_REJECT_HOURS = 24


def _is_implausibly_future(pub, now=None, hours=FUTURE_REJECT_HOURS):
    """True when published_at parses AND lies more than `hours` ahead of now.

    Naive and aware inputs both handled (same contract as the clamp). Empty /
    unparseable -> False: never reject on a guess.
    """
    if isinstance(pub, datetime):
        pub = pub.isoformat()
    if not pub or not isinstance(pub, str):
        return False
    try:
        dt = datetime.fromisoformat(pub.strip().replace('Z', '+00:00'))
    except Exception:
        return False
    ref = now
    if dt.tzinfo is None:
        if ref is None:
            ref = datetime.utcnow()
        elif ref.tzinfo is not None:
            ref = ref.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        if ref is None:
            ref = datetime.now(timezone.utc)
        elif ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
    return dt > ref + timedelta(hours=hours)


def _drop_implausibly_future(articles, source_label):
    """Filter out rows _is_implausibly_future flags; log what was dropped."""
    kept, dropped = [], []
    for a in articles:
        if _is_implausibly_future(a.get('published_at')):
            dropped.append(a)
            continue
        kept.append(a)
    if dropped:
        sample = "; ".join(f"{d.get('published_at')} {d.get('url', '')[:80]}" for d in dropped[:3])
        logger.warning(
            f"🚫 {source_label}: dropped {len(dropped)} item(s) dated >{FUTURE_REJECT_HOURS}h "
            f"in the future (event pages, not news) — {sample}")
    return kept


def save_articles(articles, db_path=NEWS_DB_PATH):
    if not articles: return 0
    conn = get_db(db_path)
    c = conn.cursor()
    saved = 0
    now_ts = datetime.utcnow().isoformat()
    for a in articles:
        try:
            pub = _clamp_future_published_at(a.get('published_at'))
            c.execute("SAVEPOINT sp_article")
            # ★ publisher_url is written through a FALLBACK rather than
            # assumed. init_db's ALTER runs before writes in-process, but a
            # read replica or a DB restored from an older dump can still lack
            # the column, and an UndefinedColumn there would stop news ingest
            # dead to save one field. Degrade, never block — the SAVEPOINT
            # this block already opens is what makes the retry safe.
            # Both statements are written out in full rather than composed
            # from a shared column list: regression_lint's insert-no-on-conflict
            # rule reads the literal, and a concatenated INSERT hides its
            # ON CONFLICT from the one check that guarantees it is there.
            _vals = (a['id'], a['title'], a['url'], a['source'],
                     a.get('category','Industry'), a.get('summary',''),
                     a.get('author',''), pub, a.get('relevance_score',0.5),
                     a.get('keywords','[]'), a.get('image_url'), now_ts)
            try:
                c.execute('''INSERT INTO news_articles
                    (id,title,url,source,category,summary,author,published_at,
                     relevance_score,keywords,image_url,fetched_at,publisher_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING''',
                    _vals + (a.get('publisher_url'),))
            except Exception:
                c.execute("ROLLBACK TO SAVEPOINT sp_article")
                c.execute('''INSERT INTO news_articles
                    (id,title,url,source,category,summary,author,published_at,
                     relevance_score,keywords,image_url,fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING''', _vals)
            if c.rowcount > 0: saved += 1
        except Exception:
            try:
                conn.rollback()
                conn.commit()  # Start fresh transaction for next article
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    conn.close()

    _sync_articles_to_pg(articles)
    return saved


def _sync_articles_to_pg(articles):
    """Sync articles to PostgreSQL — write-through so PG stays current.
    
    POOL FIX v2: Uses a dedicated direct psycopg2 connection (NOT from the pool)
    and batch-inserts with execute_values for speed. The old approach grabbed a
    pool connection via get_bg_db() and did row-by-row SAVEPOINTs across 300+
    articles — holding a pool slot for 30-60s and causing pool exhaustion.
    
    Failures here are non-fatal; SQLite remains the fallback."""
    if not articles:
        return
    import psycopg2
    from psycopg2.extras import execute_values
    
    db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not db_url:
        logger.warning("⚠️ News PG sync skipped: no DATABASE_URL")
        return
    
    pg_conn = None
    try:
        # Dedicated connection — bypasses the shared pool entirely
        pg_conn = psycopg2.connect(db_url, connect_timeout=15)
        cur = pg_conn.cursor()
        
        now_ts = datetime.utcnow().isoformat()
        # ★ Second news_articles writer. Same degrade-never-block contract as
        # the single-row one: publisher_url is attempted, and a chunk that
        # fails because the column is absent is retried WITHOUT it rather than
        # dropped. Before this, a chunk_err only logged a warning and lost 100
        # articles silently.
        insert_sql = '''INSERT INTO news_articles
            (id,title,url,source,category,summary,author,published_at,
             relevance_score,keywords,image_url,fetched_at,publisher_url)
            VALUES %s
            ON CONFLICT (id) DO NOTHING'''
        insert_sql_legacy = '''INSERT INTO news_articles
            (id,title,url,source,category,summary,author,published_at,
             relevance_score,keywords,image_url,fetched_at)
            VALUES %s
            ON CONFLICT (id) DO NOTHING'''

        # Build values tuples
        values = []
        for a in articles:
            pub = _clamp_future_published_at(a.get('published_at'))
            values.append((
                a['id'], a['title'], a['url'], a['source'],
                a.get('category', 'Industry'), a.get('summary', ''),
                a.get('author', ''), pub, a.get('relevance_score', 0.5),
                a.get('keywords', '[]'), a.get('image_url'), now_ts,
                a.get('publisher_url')
            ))
        
        # Batch insert in chunks of 100
        synced = 0
        for i in range(0, len(values), 100):
            chunk = values[i:i+100]
            try:
                execute_values(cur, insert_sql, chunk,
                    template='(' + ','.join(['%s'] * 13) + ')')
                synced += cur.rowcount
            except Exception as chunk_err:
                try:
                    pg_conn.rollback()
                except Exception:
                    pass
                # Retry without publisher_url before giving up on 100 rows.
                try:
                    execute_values(cur, insert_sql_legacy,
                                   [v[:12] for v in chunk],
                                   template='(' + ','.join(['%s'] * 12) + ')')
                    synced += cur.rowcount
                    logger.warning("⚠️ News PG batch: publisher_url column "
                                   "absent, inserted without it")
                except Exception:
                    logger.warning(
                        f"⚠️ News PG batch insert chunk failed: {chunk_err}")
                    try:
                        pg_conn.rollback()
                    except Exception:
                        pass
        
        pg_conn.commit()
        
        # Cleanup old articles
        try:
            cur.execute("DELETE FROM news_articles WHERE fetched_at::timestamptz < NOW() - INTERVAL '90 days'")
            pg_conn.commit()
        except Exception as e:
            logger.warning(f"⚠️ News PG cleanup failed (non-fatal): {e}")
            try:
                pg_conn.rollback()
            except Exception:
                pass
        
        cur.close()
        if synced > 0:
            logger.info(f"📰 PG sync: {synced} new articles inserted ({len(articles)} total, batch mode)")
    except Exception as e:
        logger.warning(f"⚠️ News PG sync failed (falling back to SQLite only): {e}")
    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception:
                pass


def sync_to_announcements(articles, db_path=MAIN_DB_PATH):
    """Sync into announcements table in PostgreSQL.
    
    POOL FIX v2: Uses dedicated direct connection (not pool) and batch insert
    via execute_values. Old approach grabbed pool connection via get_bg_db()
    and did row-by-row SAVEPOINTs across 300+ articles."""
    if not articles: return 0
    import psycopg2
    from psycopg2.extras import execute_values
    
    db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not db_url:
        logger.warning("⚠️ Announcements sync skipped: no DATABASE_URL")
        return 0
    
    pg_conn = None
    try:
        ensure_announcements_table(db_path)
        
        # Dedicated connection — bypasses shared pool
        pg_conn = psycopg2.connect(db_url, connect_timeout=15)
        cur = pg_conn.cursor()
        saved = 0
        now = datetime.utcnow().isoformat()
        
        insert_sql = '''INSERT INTO announcements
            (id,title,summary,source_url,source,published_date,discovered_at,category,url)
            VALUES %s
            ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title, summary=EXCLUDED.summary'''
        
        values = []
        for a in articles:
            # ★ Third writer. announcements is what /api/health/data-freshness
            # (MCP get_backup_status) reads; the 2026-09-21 EVENT row reached it
            # raw because only the two news_articles writers clamped (#3035).
            pub = _clamp_future_published_at(a.get('published_at'))
            values.append((
                a['id'], a['title'], a.get('summary', ''), a['url'],
                a['source'], pub or now, now,
                a.get('category', 'Industry'), a['url']
            ))
        
        # Batch insert in chunks of 100
        for i in range(0, len(values), 100):
            chunk = values[i:i+100]
            try:
                execute_values(cur, insert_sql, chunk,
                    template='(%s,%s,%s,%s,%s,%s,%s,%s,%s)')
                saved += cur.rowcount
            except Exception as chunk_err:
                logger.warning(f"⚠️ Announcements batch chunk failed: {chunk_err}")
                try:
                    pg_conn.rollback()
                except Exception:
                    pass
        
        pg_conn.commit()
        
        # Cleanup old announcements
        try:
            cur.execute("DELETE FROM announcements WHERE published_date::timestamptz < NOW() - INTERVAL '90 days'")
            pg_conn.commit()
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass
        
        cur.close()
        logger.info(f"📋 Synced {saved} articles to announcements (PostgreSQL, batch mode)")
        return saved
    except Exception as e:
        logger.error(f"❌ Failed to sync to announcements: {e}")
        return 0
    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception:
                pass


# ============================================================
# API FUNCTIONS
# ============================================================

def format_time_ago(dt):
    now = datetime.utcnow()
    if dt.tzinfo: dt = dt.replace(tzinfo=None)
    diff = now - dt
    if diff.days > 30: return f"{diff.days // 30}mo ago"
    elif diff.days > 7: return f"{diff.days // 7}w ago"
    elif diff.days > 0: return f"{diff.days}d ago"
    elif diff.seconds > 3600: return f"{diff.seconds // 3600}h ago"
    elif diff.seconds > 60: return f"{diff.seconds // 60}m ago"
    else: return "just now"

def get_latest_news(limit=100, category=None, source=None, hours=168, db_path=NEWS_DB_PATH):
    conn = get_db(db_path)
    c = conn.cursor()
    # The window is parameterised, so it cannot use the fixed datetime()->
    # INTERVAL swaps in db_utils.SQLITE_TO_PG_FUNC (those match literal strings
    # only), and `fetched_at` is TEXT and absent from that module's auto-cast
    # column list — hence the explicit ::timestamptz. The bind already carries
    # its own sign ('-168 hours'), so this is NOW() + (…), not NOW() - (…).
    query = "SELECT * FROM news_articles WHERE fetched_at::timestamptz > NOW() + (%s)::interval"
    params = [f'-{hours} hours']
    if category and category.lower() != 'all':
        query += ' AND category = %s'; params.append(category)
    if source and source.lower() != 'all':
        query += ' AND source LIKE %s'; params.append(f'%{source}%')
    query += ' ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT %s'
    params.append(limit)
    c.execute(query, params)
    articles = []
    for row in c.fetchall():
        pub = row['published_at'] or row['fetched_at']
        ta = '1d ago'
        if pub:
            try: ta = format_time_ago(datetime.fromisoformat(pub.replace('Z','+00:00').replace('+00:00','')))
            except: pass
        articles.append({
            'id': row['id'], 'title': row['title'], 'url': row['url'],
            'link': row['url'], 'source': row['source'], 'category': row['category'],
            'summary': row['summary'], 'published': row['published_at'],
            'published_at': row['published_at'], 'timeAgo': ta,
            'relevance': row['relevance_score'], 'image': row['image_url'],
        })
    # These two rollups keep their own fixed 168h window rather than following
    # `hours`; that predates this fix (deals_routes calls with hours=48) and is
    # left as written — this change repairs syntax, not the window.
    c.execute("SELECT source, COUNT(*) as cnt FROM news_articles WHERE fetched_at::timestamptz > NOW() - INTERVAL '168 hours' GROUP BY source ORDER BY cnt DESC LIMIT 50")
    sources = {r['source']: r['cnt'] for r in c.fetchall()}
    c.execute("SELECT category, COUNT(*) as cnt FROM news_articles WHERE fetched_at::timestamptz > NOW() - INTERVAL '168 hours' GROUP BY category ORDER BY cnt DESC")
    categories = {r['category']: r['cnt'] for r in c.fetchall()}
    conn.close()
    return {'success': True, 'articles': articles, 'total': len(articles),
            'sources': sources, 'categories': categories,
            'lastUpdate': datetime.utcnow().isoformat()}

def get_feed_health(db_path=NEWS_DB_PATH):
    conn = get_db(db_path)
    c = conn.cursor()
    c.execute('SELECT * FROM feed_health ORDER BY is_active DESC, success_count DESC')
    feeds = [dict(r) for r in c.fetchall()]
    conn.close()
    return feeds


# ============================================================
# BACKGROUND SCHEDULER
# ============================================================

class NewsScheduler:
    def __init__(self, interval_seconds=300, news_db=NEWS_DB_PATH, main_db=MAIN_DB_PATH):
        self.interval = interval_seconds
        self.news_db = news_db
        self.main_db = main_db
        self._timer = None
        self._running = False
        self._lock = threading.Lock()
        self._sync_in_progress = False
        self.last_sync = None
        self.last_result = None
        self.sync_count = 0
        self.error_count = 0

    def start(self, skip_initial_sync=False):
        with self._lock:
            if self._running: return
            self._running = True
        logger.info(f"📰 News Scheduler started (every {self.interval}s)")
        if not skip_initial_sync:
            threading.Thread(target=self._sync_now, daemon=True).start()
        self._schedule_next()

    def stop(self):
        with self._lock:
            self._running = False
            if self._timer: self._timer.cancel(); self._timer = None

    def _schedule_next(self):
        with self._lock:
            if not self._running: return
            self._timer = threading.Timer(self.interval, self._sync_now)
            self._timer.daemon = True
            self._timer.start()

    def _sync_now(self):
        if self._sync_in_progress:
            logger.info("📰 Sync skipped (previous cycle still running)")
            self._schedule_next()
            return
        self._sync_in_progress = True
        try:
            result = sync_all_news(self.news_db, self.main_db)
            self.last_sync = datetime.utcnow().isoformat()
            self.last_result = result
            self.sync_count += 1
            logger.info(f"📰 Sync #{self.sync_count}: {result.get('new_saved',0)} new, {result.get('announcements_synced',0)} to announcements")
        except Exception as e:
            self.error_count += 1
            self.last_sync = datetime.utcnow().isoformat()
            self.last_result = {'success': False, 'error': str(e)}
            logger.error(f"📰 Sync error #{self.error_count}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._sync_in_progress = False
            try:
                self._schedule_next()
            except Exception as e:
                logger.error(f"📰 CRITICAL: Failed to schedule next sync: {e}")

    def status(self):
        return {'running': self._running, 'interval': self.interval,
                'last_sync': self.last_sync, 'sync_count': self.sync_count,
                'error_count': self.error_count, 'last_result': self.last_result}

_scheduler = None

def start_news_scheduler(interval_seconds=300, news_db=NEWS_DB_PATH, main_db=MAIN_DB_PATH, skip_initial_sync=True):
    global _scheduler
    if _scheduler and _scheduler._running: return _scheduler
    init_news_db(news_db)
    ensure_announcements_table(main_db)
    _scheduler = NewsScheduler(interval_seconds=interval_seconds, news_db=news_db, main_db=main_db)
    _scheduler.start(skip_initial_sync=skip_initial_sync)
    return _scheduler

def get_news_scheduler():
    return _scheduler


# ============================================================
# MAIN SYNC
# ============================================================

def sync_all_news(news_db=NEWS_DB_PATH, main_db=MAIN_DB_PATH):
    start_time = time.time()
    all_articles = []
    errors = []

    try:
        init_news_db(news_db)
    except Exception as e:
        errors.append(f"init_news_db: {e}")
        logger.error(f"NEWS SYNC: init_news_db failed: {e}")

    try:
        rss = fetch_all_rss_feeds(news_db)
        all_articles.extend(rss)
    except Exception as e:
        errors.append(f"rss_feeds: {e}")
        logger.error(f"NEWS SYNC: RSS fetch failed, continuing: {e}")

    try:
        google = fetch_all_google_news()
        all_articles.extend(google)
    except Exception as e:
        errors.append(f"google_news: {e}")
        logger.error(f"NEWS SYNC: Google News failed, continuing: {e}")

    logger.info(f"🔄 Deduplicating {len(all_articles)} articles...")
    seen = set()
    unique = []
    for a in all_articles:
        norm = re.sub(r'[^a-z0-9]', '', a['title'].lower())[:50]
        if norm not in seen:
            seen.add(norm)
            unique.append(a)
    logger.info(f"   📊 {len(unique)} unique after dedup")

    saved = 0
    ann_synced = 0
    try:
        saved = save_articles(unique, news_db)
    except Exception as e:
        errors.append(f"save_articles: {e}")
        logger.error(f"NEWS SYNC: save_articles failed, continuing: {e}")

    try:
        ann_synced = sync_to_announcements(unique, main_db)
    except Exception as e:
        errors.append(f"sync_announcements: {e}")
        logger.error(f"NEWS SYNC: sync_to_announcements failed, continuing: {e}")

    total = 0
    sources = 0
    deleted = 0
    conn = None
    try:
        conn = get_db(news_db)
        c = conn.cursor()
        c.execute("DELETE FROM news_articles WHERE fetched_at::timestamptz < NOW() - INTERVAL '90 days'")
        deleted = c.rowcount
        c.execute("SELECT COUNT(*) FROM news_articles")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT source) FROM news_articles")
        sources = c.fetchone()[0]
        conn.commit()
    except Exception as e:
        errors.append(f"cleanup: {e}")
        logger.error(f"NEWS SYNC: cleanup failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    elapsed = time.time() - start_time
    logger.info(f"✅ Sync done in {elapsed:.1f}s — {saved} new, {ann_synced} to announcements, {total} total")
    if errors:
        logger.warning(f"NEWS SYNC: completed with {len(errors)} error(s): {'; '.join(errors)}")

    return {
        'success': len(errors) == 0, 'total_fetched': len(all_articles),
        'unique_after_dedup': len(unique), 'new_saved': saved,
        'announcements_synced': ann_synced, 'total_in_db': total,
        'unique_sources': sources, 'deleted': deleted,
        'elapsed_seconds': round(elapsed, 1),
        'timestamp': datetime.utcnow().isoformat(),
        'errors': errors if errors else None,
    }


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'sync'

    if cmd == 'sync':
        result = sync_all_news()
        print(json.dumps(result, indent=2))
    elif cmd == 'clean':
        print("🧹 Cleaning irrelevant articles...")
        for db, table in [(NEWS_DB_PATH, 'news_articles'), (MAIN_DB_PATH, 'announcements')]:
            try:
                conn = get_db(db)
                c = conn.cursor()
                c.execute(f"SELECT id, title, summary, source FROM {table}")
                to_del = []
                for row in c.fetchall():
                    aid, t, s, src = row
                    is_p1 = any(f['name']==src and f.get('priority',3)==1 for f in RSS_FEEDS)
                    if not is_p1 and not is_relevant_article(t, s or ''):
                        to_del.append(aid)
                if to_del:
                    c.executemany(f"DELETE FROM {table} WHERE id=%s", [(x,) for x in to_del])
                    conn.commit()
                    print(f"   Deleted {len(to_del)} from {db}.{table}")
                else:
                    print(f"   {db}.{table}: all clean")
                conn.close()
            except Exception as e:
                print(f"   ⚠️ {db}.{table}: {e}")
        print("✅ Done. Run 'python news_engine.py sync' to re-fetch.")
    elif cmd == 'scheduler':
        print("Starting scheduler (Ctrl+C to stop)...")
        sched = start_news_scheduler(300)
        try:
            while True:
                time.sleep(60)
                s = sched.status()
                print(f"  Syncs: {s['sync_count']}, Errors: {s['error_count']}, Last: {s['last_sync']}")
        except KeyboardInterrupt:
            sched.stop()
    elif cmd == 'latest':
        init_news_db()
        r = get_latest_news(limit=30)
        for a in r['articles']:
            print(f"[{a['category']:<12}] [{a['source'][:20]:<20}] {a['title'][:60]}  ({a['timeAgo']})")
    elif cmd == 'health':
        init_news_db()
        for f in get_feed_health():
            st = '✅' if f['is_active'] else '❌'
            print(f"{st} {f['name'][:30]:<30} ok:{f['success_count']} fail:{f['failure_count']}")
    else:
        print("Usage: python news_engine.py [sync|clean|scheduler|latest|health]")

# === phase 92: source-registry heartbeat (auto-fires on clean module exit) ===
# Non-invasive: never crashes the script if the registry is unreachable.
# Source ID: backend-news-engine
_phase92_heartbeat_registered = True
try:
    import atexit as _phase92_atexit
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    def _phase92_emit():
        try:
            _phase92_heartbeat("backend-news-engine", status="success",
                              metadata={"trigger": "atexit"})
        except Exception:
            pass
    _phase92_atexit.register(_phase92_emit)
except Exception:
    pass  # heartbeat module unavailable; extractor continues normally
