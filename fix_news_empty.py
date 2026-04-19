#!/usr/bin/env python3
"""
DC Hub Fix: News returning empty data
=====================================
Run this in Railway shell: python3 fix_news_empty.py

Root cause: The Cloudflare Worker's Neon direct route queries news_articles,
but the news sync scheduler may be writing to SQLite instead of Neon PostgreSQL.

This script:
1. Checks if news_articles has data in Neon
2. If empty, runs a one-time RSS sync directly to Neon
3. Verifies the fix
"""

import os
import sys
import json
import time
import hashlib
from datetime import datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing psycopg2...")
    os.system("pip install psycopg2-binary --break-system-packages -q")
    import psycopg2
    import psycopg2.extras

try:
    import feedparser
except ImportError:
    print("Installing feedparser...")
    os.system("pip install feedparser --break-system-packages -q")
    import feedparser

# --- Config ---
DATABASE_URL = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL')
if not DATABASE_URL:
    print("ERROR: No DATABASE_URL or NEON_DATABASE_URL found in environment")
    sys.exit(1)

# Top DC industry RSS feeds
RSS_FEEDS = [
    {"url": "https://www.datacenterdynamics.com/en/rss/", "source": "DatacenterDynamics", "category": "Industry"},
    {"url": "https://www.datacenterknowledge.com/rss.xml", "source": "Data Center Knowledge", "category": "Industry"},
    {"url": "https://datacenterfrontier.com/feed/", "source": "Data Center Frontier", "category": "Industry"},
    {"url": "https://www.datacenters.com/news/rss", "source": "Datacenters.com", "category": "Industry"},
    {"url": "https://www.capacitymedia.com/rss", "source": "Capacity Media", "category": "Industry"},
    {"url": "https://www.lightreading.com/rss.xml", "source": "Light Reading", "category": "Telecom"},
    {"url": "https://www.fiercetelecom.com/rss/xml", "source": "Fierce Telecom", "category": "Telecom"},
    {"url": "https://www.sdxcentral.com/feed/", "source": "SDxCentral", "category": "Technology"},
    {"url": "https://www.theregister.com/data_centre/headlines.atom", "source": "The Register", "category": "Technology"},
    {"url": "https://www.crn.com/rss/all.xml", "source": "CRN", "category": "Technology"},
    {"url": "https://techcrunch.com/tag/data-center/feed/", "source": "TechCrunch", "category": "Technology"},
    {"url": "https://www.reuters.com/technology/rss", "source": "Reuters Tech", "category": "Business"},
    {"url": "https://bisnow.com/rss/national/data-center", "source": "Bisnow", "category": "Real Estate"},
]

def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=15)

def check_news_count():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM news_articles")
        count = cur.fetchone()[0]
        cur.execute("SELECT MAX(published_at) FROM news_articles WHERE published_at IS NOT NULL")
        latest = cur.fetchone()[0]
        return count, latest
    finally:
        conn.close()

def ensure_table():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news_articles (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                source TEXT,
                url TEXT UNIQUE,
                category TEXT DEFAULT 'Industry',
                image_url TEXT,
                published_at TIMESTAMP,
                is_breaking BOOLEAN DEFAULT FALSE,
                relevance_score FLOAT DEFAULT 0.5,
                created_at TIMESTAMP DEFAULT NOW(),
                content_hash TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_news_source ON news_articles(source)")
        conn.commit()
        print("✅ news_articles table ready")
    finally:
        conn.close()

def sync_rss_to_neon():
    """Fetch RSS feeds and insert articles directly into Neon."""
    conn = get_conn()
    cur = conn.cursor()
    total_new = 0
    total_skipped = 0
    errors = []

    for feed_info in RSS_FEEDS:
        source = feed_info["source"]
        category = feed_info["category"]
        try:
            print(f"  📡 Fetching {source}...", end=" ", flush=True)
            feed = feedparser.parse(feed_info["url"])
            
            if not feed.entries:
                print(f"0 entries")
                continue
            
            new_count = 0
            for entry in feed.entries[:30]:  # Max 30 per source
                title = entry.get("title", "").strip()
                if not title:
                    continue
                
                url = entry.get("link", "").strip()
                if not url:
                    continue
                
                summary = entry.get("summary", entry.get("description", ""))
                if summary:
                    # Strip HTML tags
                    import re
                    summary = re.sub(r'<[^>]+>', '', summary).strip()[:500]
                
                # Parse published date
                published_at = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except:
                        pass
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    try:
                        published_at = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                    except:
                        pass
                
                # Image
                image_url = None
                if hasattr(entry, 'media_content') and entry.media_content:
                    image_url = entry.media_content[0].get('url')
                elif hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
                    image_url = entry.media_thumbnail[0].get('url')
                
                content_hash = hashlib.md5(f"{title}{url}".encode()).hexdigest()
                
                try:
                    cur.execute("""
                        INSERT INTO news_articles (title, summary, source, url, category, image_url, published_at, content_hash, relevance_score)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (url) DO NOTHING
                    """, (title, summary, source, url, category, image_url, published_at, content_hash, 0.5))
                    if cur.rowcount > 0:
                        new_count += 1
                except Exception as e:
                    pass  # Skip duplicates silently
            
            conn.commit()
            total_new += new_count
            print(f"{new_count} new / {len(feed.entries)} total")
            
        except Exception as e:
            errors.append(f"{source}: {str(e)[:80]}")
            print(f"ERROR: {str(e)[:60]}")
    
    conn.close()
    return total_new, total_skipped, errors

def main():
    print("=" * 60)
    print("DC Hub News Fix — Neon Direct Sync")
    print("=" * 60)
    print()
    
    # Step 1: Check current state
    print("Step 1: Checking Neon news_articles...")
    try:
        count, latest = check_news_count()
        print(f"  Current articles: {count}")
        print(f"  Latest article:   {latest or 'None'}")
    except Exception as e:
        print(f"  Table may not exist: {e}")
        count = 0
    
    # Step 2: Ensure table exists
    print("\nStep 2: Ensuring table schema...")
    ensure_table()
    
    # Step 3: Sync RSS feeds
    print(f"\nStep 3: Syncing {len(RSS_FEEDS)} RSS feeds to Neon...")
    new_articles, skipped, errors = sync_rss_to_neon()
    
    # Step 4: Verify
    print(f"\nStep 4: Verifying...")
    final_count, final_latest = check_news_count()
    
    print()
    print("=" * 60)
    print(f"RESULTS:")
    print(f"  New articles added: {new_articles}")
    print(f"  Total in Neon:      {final_count}")
    print(f"  Latest article:     {final_latest}")
    if errors:
        print(f"  Feed errors:        {len(errors)}")
        for e in errors[:5]:
            print(f"    - {e}")
    print("=" * 60)
    
    if final_count > 0:
        print("\n✅ News should now appear on dchub.cloud/news")
        print("   The Worker's Neon direct route will serve these articles.")
        print("\n   IMPORTANT: Make sure the Railway scheduler keeps running")
        print("   the news_sync job to keep articles fresh (every 5 min).")
        print("\n   To verify the scheduler writes to Neon (not SQLite):")
        print("   grep -n 'news_articles' main.py | grep -i 'insert\\|sqlite\\|dchub.db'")
    else:
        print("\n❌ No articles were added. Check RSS feed connectivity.")

if __name__ == "__main__":
    main()
