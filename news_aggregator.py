#!/usr/bin/env python3
"""
DC Hub News Aggregator
Fetches data center industry news from RSS feeds, strips HTML,
and stores clean articles in the Neon PostgreSQL `news` table.

Usage:
    python news_aggregator.py              # fetch all feeds
    python news_aggregator.py --source datacenterknowledge   # fetch one source
    python news_aggregator.py --create-table   # create table only
    python news_aggregator.py --clean          # remove articles older than 90 days

Requires: feedparser, psycopg2-binary, python-dateutil
"""

import os
import re
import sys
import json
import hashlib
import logging
import argparse
from datetime import datetime, timedelta, timezone
from html import unescape

try:
    import feedparser
except ImportError:
    print("ERROR: feedparser not installed. Run: pip install feedparser")
    sys.exit(1)

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None  # fallback to manual parsing

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("news_aggregator")

# Data center industry RSS feeds
RSS_FEEDS = {
    "datacenterknowledge": {
        "name": "Data Center Knowledge",
        "url": "https://www.datacenterknowledge.com/rss.xml",
        "category": "industry",
    },
    "datacenterdynamics": {
        "name": "DatacenterDynamics",
        "url": "https://www.datacenterdynamics.com/en/rss/feed/",
        "category": "industry",
    },
    "datacenterworld": {
        "name": "Data Center World",
        "url": "https://www.datacenterworld.com/feed",
        "category": "industry",
    },
    "theregister_dc": {
        "name": "The Register - Data Centre",
        "url": "https://www.theregister.com/headlines.atom",
        "category": "technology",
    },
    "uptime_institute": {
        "name": "Uptime Institute",
        "url": "https://journal.uptimeinstitute.com/feed/",
        "category": "research",
    },
    "capacity_media": {
        "name": "Capacity Media",
        "url": "https://www.capacitymedia.com/rss",
        "category": "connectivity",
    },
    "broadgroup": {
        "name": "BroadGroup",
        "url": "https://www.broadgroup.com/feed",
        "category": "industry",
    },
    "energy_gov": {
        "name": "DOE Energy News",
        "url": "https://www.energy.gov/rss/articles.xml",
        "category": "energy",
    },
    "eia_today": {
        "name": "EIA Today in Energy",
        "url": "https://www.eia.gov/todayinenergy/rss/rss_todayinenergy.xml",
        "category": "energy",
    },
    "fierce_telecom": {
        "name": "Fierce Telecom",
        "url": "https://www.fiercetelecom.com/rss/xml",
        "category": "connectivity",
    },
    "sdxcentral": {
        "name": "SDxCentral",
        "url": "https://www.sdxcentral.com/feed/",
        "category": "technology",
    },
    "cloudcomputing_news": {
        "name": "Cloud Computing News",
        "url": "https://www.cloudcomputing-news.net/feed/",
        "category": "cloud",
    },
}

# Keywords to filter articles that are relevant to DC Hub
DC_KEYWORDS = [
    "data center", "data centre", "datacenter", "datacentre",
    "colocation", "colo ", "hyperscale", "edge computing",
    "cloud infrastructure", "server farm",
    "power grid", "renewable energy", "solar", "wind power",
    "fiber optic", "interconnection", "peering",
    "ai infrastructure", "gpu cluster", "hpc",
    "cooling", "pue", "uptime", "tier iii", "tier iv",
    "equinix", "digital realty", "cyrusone", "qts",
    "coresite", "flexential", "vantage", "compass",
    "stack infrastructure", "switch", "cloudflare",
    "aws", "azure", "google cloud", "gcp",
    "electricity", "megawatt", "gigawatt", "power purchase",
    "capacity", "latency", "bandwidth",
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS news (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    url             TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_key      TEXT,
    category        TEXT DEFAULT 'industry',
    published_date  TIMESTAMPTZ,
    guid            TEXT UNIQUE,
    image_url       TEXT,
    author          TEXT,
    tags            TEXT[],
    relevance_score REAL DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_published ON news (published_date DESC);
CREATE INDEX IF NOT EXISTS idx_news_source ON news (source);
CREATE INDEX IF NOT EXISTS idx_news_category ON news (category);
CREATE INDEX IF NOT EXISTS idx_news_guid ON news (guid);
"""

UPSERT_SQL = """
INSERT INTO news (title, description, url, source, source_key, category,
                  published_date, guid, image_url, author, tags, relevance_score)
VALUES %s
ON CONFLICT (guid) DO UPDATE SET
    title           = EXCLUDED.title,
    description     = EXCLUDED.description,
    url             = EXCLUDED.url,
    image_url       = EXCLUDED.image_url,
    relevance_score = EXCLUDED.relevance_score,
    updated_at      = NOW()
"""

CLEAN_OLD_SQL = """
DELETE FROM news WHERE published_date < NOW() - INTERVAL '%s days'
"""


def get_connection():
    """Get a PostgreSQL connection using DATABASE_URL."""
    if not DATABASE_URL:
        log.error("DATABASE_URL environment variable is not set")
        sys.exit(1)
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        conn.autocommit = False
        return conn
    except Exception as e:
        log.error(f"Database connection failed: {e}")
        sys.exit(1)


def create_table():
    """Create the news table if it doesn't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        log.info("News table created/verified successfully")
    except Exception as e:
        conn.rollback()
        log.error(f"Table creation failed: {e}")
        raise
    finally:
        conn.close()


def clean_old_articles(days=90):
    """Remove articles older than N days."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(CLEAN_OLD_SQL, (days,))
            deleted = cur.rowcount
        conn.commit()
        log.info(f"Cleaned {deleted} articles older than {days} days")
        return deleted
    except Exception as e:
        conn.rollback()
        log.error(f"Cleanup failed: {e}")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTML / Text Cleaning
# ---------------------------------------------------------------------------

# Compiled regex patterns for performance
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')
_ENTITY_RE = re.compile(r'&[#\w]+;')


def strip_html(text):
    """Remove HTML tags and decode entities from a string."""
    if not text:
        return ""
    # Remove script/style blocks entirely
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Remove CDATA
    text = re.sub(r'<!\[CDATA\[.*?\]\]>', '', text, flags=re.DOTALL)
    # Strip tags
    text = _TAG_RE.sub(' ', text)
    # Decode HTML entities
    text = unescape(text)
    # Normalize whitespace
    text = _WS_RE.sub(' ', text).strip()
    return text


def clean_description(text, max_length=500):
    """Clean and truncate a description to max_length chars."""
    text = strip_html(text)
    if len(text) > max_length:
        # Try to break at a sentence boundary
        truncated = text[:max_length]
        last_period = truncated.rfind('.')
        if last_period > max_length * 0.6:
            text = truncated[:last_period + 1]
        else:
            text = truncated.rstrip() + "..."
    return text


# ---------------------------------------------------------------------------
# Feed Parsing
# ---------------------------------------------------------------------------

def parse_date(date_str):
    """Parse a date string into a timezone-aware datetime."""
    if not date_str:
        return None
    # Try dateutil first (handles most formats)
    if dateutil_parser:
        try:
            dt = dateutil_parser.parse(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, OverflowError):
            pass
    # Fallback: try common RSS date formats
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def make_guid(entry, source_key):
    """Generate a unique GUID for an entry."""
    # Prefer the entry's own id/guid
    raw_id = getattr(entry, 'id', '') or getattr(entry, 'guid', '') or ''
    if raw_id:
        return hashlib.md5(f"{source_key}:{raw_id}".encode()).hexdigest()
    # Fallback to URL + title hash
    url = getattr(entry, 'link', '') or ''
    title = getattr(entry, 'title', '') or ''
    return hashlib.md5(f"{source_key}:{url}:{title}".encode()).hexdigest()


def compute_relevance(title, description):
    """Score 0-1 based on how many DC keywords appear in the text."""
    text = f"{title} {description}".lower()
    hits = sum(1 for kw in DC_KEYWORDS if kw.lower() in text)
    # Normalize: max score at 5+ keyword hits
    return min(hits / 5.0, 1.0)


def get_image_url(entry):
    """Extract an image URL from a feed entry."""
    # Check media:thumbnail
    media = getattr(entry, 'media_thumbnail', None)
    if media and isinstance(media, list) and len(media) > 0:
        return media[0].get('url', '')
    # Check media:content
    media_content = getattr(entry, 'media_content', None)
    if media_content and isinstance(media_content, list):
        for mc in media_content:
            if mc.get('medium') == 'image' or mc.get('type', '').startswith('image'):
                return mc.get('url', '')
    # Check enclosures
    enclosures = getattr(entry, 'enclosures', [])
    for enc in enclosures:
        if enc.get('type', '').startswith('image'):
            return enc.get('href', '') or enc.get('url', '')
    # Check for image in content (first img src)
    content = ''
    if hasattr(entry, 'content') and entry.content:
        content = entry.content[0].get('value', '')
    elif hasattr(entry, 'summary'):
        content = entry.summary or ''
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
    if img_match:
        return img_match.group(1)
    return None


def parse_feed(source_key, feed_config):
    """Parse a single RSS feed and return a list of article tuples."""
    url = feed_config["url"]
    source_name = feed_config["name"]
    category = feed_config.get("category", "industry")
    articles = []

    log.info(f"Fetching: {source_name} ({url})")

    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning(f"Failed to fetch {source_name}: {e}")
        return articles

    if feed.bozo and not feed.entries:
        log.warning(f"Feed error for {source_name}: {getattr(feed, 'bozo_exception', 'unknown')}")
        return articles

    for entry in feed.entries:
        try:
            title = strip_html(getattr(entry, 'title', '') or '')
            if not title:
                continue

            # Get description from summary or content
            raw_desc = ''
            if hasattr(entry, 'summary') and entry.summary:
                raw_desc = entry.summary
            elif hasattr(entry, 'content') and entry.content:
                raw_desc = entry.content[0].get('value', '')
            elif hasattr(entry, 'description') and entry.description:
                raw_desc = entry.description
            description = clean_description(raw_desc)

            url_link = getattr(entry, 'link', '') or ''
            if not url_link:
                continue

            # Parse published date
            date_str = (
                getattr(entry, 'published', '') or
                getattr(entry, 'updated', '') or
                getattr(entry, 'created', '') or ''
            )
            published_date = parse_date(date_str)

            # r33-Q+news-future-clamp (2026-05-22): clamp future-dated
            # published_date to NOW(). The `news` table (read by the
            # freshness radar at routes/_freshness.py) showed a row
            # "955h ahead of now" because some RSS feeds emit
            # future-scheduled or year-off-by-one dates. The existing
            # clamp in news_engine.py only protected news_articles, NOT
            # this `news` table — different ingester, different table.
            # Clamp here so the freshness radar stops false-alarming.
            if published_date:
                _now = datetime.now(published_date.tzinfo or timezone.utc)
                if published_date > _now:
                    published_date = _now

            # Skip articles older than 90 days
            if published_date:
                cutoff = datetime.now(timezone.utc) - timedelta(days=90)
                if published_date < cutoff:
                    continue

            guid = make_guid(entry, source_key)
            image_url = get_image_url(entry)
            author = strip_html(getattr(entry, 'author', '') or '')

            # Tags from feed categories
            tags = []
            if hasattr(entry, 'tags') and entry.tags:
                tags = [strip_html(t.get('term', '')) for t in entry.tags if t.get('term')]
            tags = tags[:10]  # limit to 10 tags

            relevance = compute_relevance(title, description)

            articles.append((
                title,
                description,
                url_link,
                source_name,
                source_key,
                category,
                published_date,
                guid,
                image_url,
                author,
                tags,
                relevance,
            ))

        except Exception as e:
            log.warning(f"Error parsing entry in {source_name}: {e}")
            continue

    log.info(f"  Parsed {len(articles)} articles from {source_name}")
    return articles


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_aggregator(source_filter=None, min_relevance=0.0):
    """Main aggregation loop: fetch feeds, parse, store."""
    create_table()

    all_articles = []

    for key, config in RSS_FEEDS.items():
        if source_filter and key != source_filter:
            continue
        articles = parse_feed(key, config)
        # Filter by relevance if threshold set
        if min_relevance > 0:
            articles = [a for a in articles if a[11] >= min_relevance]
        all_articles.extend(articles)

    if not all_articles:
        log.info("No new articles to store")
        return {"success": True, "articles_found": 0, "articles_stored": 0}

    # Store in database
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur, UPSERT_SQL, all_articles,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                page_size=100,
            )
            stored = cur.rowcount
        conn.commit()
        log.info(f"Stored/updated {stored} articles (from {len(all_articles)} parsed)")
        return {
            "success": True,
            "articles_found": len(all_articles),
            "articles_stored": stored,
            "sources_processed": len(RSS_FEEDS) if not source_filter else 1,
        }
    except Exception as e:
        conn.rollback()
        log.error(f"Database insert failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="DC Hub News Aggregator")
    parser.add_argument("--source", help="Fetch only this source key", default=None)
    parser.add_argument("--create-table", action="store_true", help="Create table only")
    parser.add_argument("--clean", action="store_true", help="Remove old articles")
    parser.add_argument("--clean-days", type=int, default=90, help="Days to keep (default 90)")
    parser.add_argument("--min-relevance", type=float, default=0.0,
                        help="Minimum relevance score 0-1 (default 0 = all)")
    parser.add_argument("--list-sources", action="store_true", help="List available sources")
    args = parser.parse_args()

    if args.list_sources:
        print("\nAvailable RSS Sources:")
        print("-" * 60)
        for key, config in sorted(RSS_FEEDS.items()):
            print(f"  {key:25s} {config['name']:30s} [{config['category']}]")
        print()
        return

    if args.create_table:
        create_table()
        print(json.dumps({"success": True, "action": "create_table"}))
        return

    if args.clean:
        deleted = clean_old_articles(args.clean_days)
        print(json.dumps({"success": True, "action": "clean", "deleted": deleted}))
        return

    result = run_aggregator(
        source_filter=args.source,
        min_relevance=args.min_relevance,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
