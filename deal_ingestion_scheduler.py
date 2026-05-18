"""
Deal Ingestion Scheduler — Background thread for Railway
=========================================================
Scrapes Google News RSS for data center M&A deals every 6 hours
and inserts them into the Neon PostgreSQL ai_deals table.

Starts automatically when Flask boots via:
    from deal_ingestion_scheduler import start_deal_scheduler
    start_deal_scheduler(get_db)
"""

import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime, date

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
INTERVAL_HOURS = int(os.getenv('DEAL_INGEST_INTERVAL_HOURS', '6'))
INTERVAL_SECONDS = INTERVAL_HOURS * 3600

RSS_FEEDS = [
    'https://news.google.com/rss/search?q=data+center+acquisition&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=data+center+merger&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=AI+infrastructure+deal&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=hyperscale+data+center+investment&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=data+center+land+acquisition&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=colocation+acquisition&hl=en-US&gl=US&ceid=US%3Aen',
]

DEAL_TYPE_PATTERNS = {
    'acquisition':   r'\b(acquire[ds]?|acquisition|acquiring|bought|purchase[ds]?)\b',
    'merger':        r'\b(merge[drs]?|merger|combining)\b',
    'joint_venture': r'\b(joint.%sventure|jv|partnership|teaming)\b',
    'investment':    r'\b(invest(?:s|ed|ing|ment)?|fund(?:s|ed|ing)?|stake|raise[ds]?)\b',
    'land_acquisition': r'\b(land|site|campus|acre|parcel|property)\b',
    'divestiture':   r'\b(divest(?:s|ed|ing|iture)?|spin.?off|sell(?:s|ing)?|sold)\b',
}

MONEY_RE = re.compile(r'\$\s*(\d+(?:\.\d+)?)\s*(billion|million|thousand|[KMB])\b', re.I)


# ── Helpers ───────────────────────────────────────────────────────────
def _money_to_usd(text):
    """Extract first monetary value from text, return as USD float or None."""
    m = MONEY_RE.search(text)
    if not m:
        return None, None
    val, unit = float(m.group(1)), m.group(2).upper()
    if unit in ('B', 'BILLION'):
        usd = val * 1_000_000_000
        display = f"${val}B"
    elif unit in ('M', 'MILLION'):
        usd = val * 1_000_000
        display = f"${val}M"
    elif unit in ('K', 'THOUSAND'):
        usd = val * 1_000
        display = f"${val}K"
    else:
        usd = val
        display = f"${val}"
    return usd, display


def _deal_type(text):
    low = text.lower()
    for dtype, pat in DEAL_TYPE_PATTERNS.items():
        if re.search(pat, low):
            return dtype
    return 'unknown'


def _extract_companies(text):
    """Heuristic: sequences of capitalised words (≥2 chars each)."""
    stop = {'The', 'And', 'For', 'With', 'From', 'Data', 'Center', 'New', 'Report',
            'Global', 'North', 'South', 'East', 'West', 'United', 'States', 'Google',
            'News', 'Reuters', 'Bloomberg', 'According', 'Monday', 'Tuesday',
            'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'January',
            'February', 'March', 'April', 'May', 'June', 'July', 'August',
            'September', 'October', 'November', 'December'}
    matches = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', text)
    seen, out = set(), []
    for m in matches:
        if m not in seen and m not in stop and len(m) > 3:
            seen.add(m)
            out.append(m)
    return out[:8]


def _deal_hash(buyer, seller, deal_date, value_str):
    raw = f"{(buyer or '').strip().lower()}|{(seller or '').strip().lower()}|{deal_date}|{(value_str or '').strip().lower()}"
    return hashlib.md5(raw.encode()).hexdigest()


def _parse_date(published_str):
    """Best-effort date parse from RSS published field."""
    if not published_str:
        return date.today()
    for fmt in ('%a, %d %b %Y %H:%M:%S %Z', '%a, %d %b %Y %H:%M:%S %z',
                '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d'):
        try:
            return datetime.strptime(published_str.strip(), fmt).date()
        except ValueError:
            continue
    return date.today()


# ── Core ingestion ────────────────────────────────────────────────────
def run_ingestion(get_db):
    """Single ingestion run: fetch RSS → extract deals → upsert into Neon."""
    try:
        import feedparser
    except ImportError:
        logger.error("feedparser not installed — pip install feedparser")
        return

    logger.info("🔄 Deal ingestion starting...")
    articles = []

    # 1. Fetch RSS
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in getattr(feed, 'entries', []):
                articles.append({
                    'title': entry.get('title', ''),
                    'summary': entry.get('summary', ''),
                    'link': entry.get('link', ''),
                    'published': entry.get('published', ''),
                    'source': entry.get('source', {}).get('title', 'Google News'),
                })
        except Exception as e:
            logger.warning(f"RSS fetch error ({url}): {e}")

    logger.info(f"  Fetched {len(articles)} articles from {len(RSS_FEEDS)} feeds")

    # 2. Extract deals
    deals = []
    for art in articles:
        text = f"{art['title']} {art['summary']}"
        dtype = _deal_type(text)
        if dtype == 'unknown':
            continue
        companies = _extract_companies(art['title'])
        buyer = companies[0] if companies else None
        seller = companies[1] if len(companies) > 1 else None
        if not buyer:
            continue
        usd, display = _money_to_usd(text)
        deal_date = _parse_date(art['published'])
        deals.append({
            'buyer': buyer,
            'seller': seller or 'Undisclosed',
            'deal_type': dtype,
            'deal_value_usd': usd,
            'deal_value_str': display,
            'deal_date': str(deal_date),
            'source_url': art['link'],
            'source_name': art['source'],
            'description': f"{dtype}: {buyer} → {seller or 'Undisclosed'}",
            'deal_hash': _deal_hash(buyer, seller, str(deal_date), display),
        })

    logger.info(f"  Extracted {len(deals)} deals")
    if not deals:
        logger.info("  No new deals to insert")
        return

    # 3. Upsert into Neon
    inserted, updated, errors = 0, 0, 0
    try:
        conn = get_db()
        cur = conn.cursor()
        for d in deals:
            try:
                cur.execute("""
                    INSERT INTO ai_deals (
                        deal_hash, buyer, seller, deal_type,
                        deal_value_usd, deal_value_str, deal_date,
                        source_url, source_name, description,
                        ai_detected, confidence, status
                    ) VALUES (
                        %(deal_hash) ON CONFLICT DO NOTHINGs, %(buyer)s, %(seller)s, %(deal_type)s,
                        %(deal_value_usd)s, %(deal_value_str)s, %(deal_date)s,
                        %(source_url)s, %(source_name)s, %(description)s,
                        true, 70, 'active'
                    )
                    ON CONFLICT (deal_hash) DO UPDATE SET
                        updated_at = NOW(),
                        confidence = GREATEST(ai_deals.confidence, EXCLUDED.confidence)
                """, d)
                if cur.rowcount > 0:
                    inserted += 1
            except Exception as e:
                errors += 1
                logger.warning(f"  Insert error: {e}")
                conn.rollback()
                cur = conn.cursor()
                continue
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"  DB error: {e}")
        errors = len(deals)

    logger.info(f"  ✅ Ingestion complete: {inserted} inserted/updated, {errors} errors")


# ── Scheduler thread ──────────────────────────────────────────────────
_scheduler_running = False


def _scheduler_loop(get_db):
    """Runs in a daemon thread: ingest → sleep → repeat."""
    global _scheduler_running
    logger.info(f"📅 Deal ingestion scheduler started (every {INTERVAL_HOURS}h)")

    # Initial delay: let Flask finish booting (30 seconds)
    time.sleep(30)

    while _scheduler_running:
        try:
            run_ingestion(get_db)
        except Exception as e:
            logger.error(f"Ingestion error (will retry next cycle): {e}")
        # Sleep in 60-second chunks so we can stop cleanly
        for _ in range(INTERVAL_SECONDS // 60):
            if not _scheduler_running:
                break
            time.sleep(60)


def start_deal_scheduler(get_db):
    """Call from main.py after Flask app is configured.
    Starts a background daemon thread — won't block Flask."""
    global _scheduler_running
    if _scheduler_running:
        logger.info("Deal scheduler already running")
        return
    _scheduler_running = True
    t = threading.Thread(target=_scheduler_loop, args=(get_db,), daemon=True)
    t.name = "DealIngestionScheduler"
    t.start()
    logger.info("✅ Deal ingestion scheduler thread launched")


def stop_deal_scheduler():
    """Graceful stop (optional)."""
    global _scheduler_running
    _scheduler_running = False
    logger.info("Deal ingestion scheduler stopped")
