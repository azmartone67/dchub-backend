"""
auto_sync.py — Drop-in bridge between main.py and news_engine v3.
=================================================================
main.py imports these from auto_sync:
  - register_admin_apis(app)
  - NewsSyncer(interval_seconds=300)
  - fetch_rss_news()
  - save_news_to_db(articles)
  - sync_news()

This module delegates everything to news_engine.py which handles
fetching, filtering, and writing to BOTH dchub.db AND dc_nexus.db.
"""

import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
# Import from news_engine (the real engine)
# ============================================================

from news_engine import (
    sync_all_news,
    fetch_all_rss_feeds,
    save_articles,
    sync_to_announcements,
    init_news_db,
    ensure_announcements_table,
    start_news_scheduler,
    get_news_scheduler,
    get_latest_news,
    get_feed_health,
    NEWS_DB_PATH,
    MAIN_DB_PATH,
)


# ============================================================
# NewsSyncer — wrapper around news_engine.NewsScheduler
# main.py does: news_syncer = NewsSyncer(interval_seconds=300)
# ============================================================

class NewsSyncer:
    """Drop-in replacement that starts news_engine's scheduler."""

    def __init__(self, interval_seconds=300):
        self.interval = interval_seconds
        self._scheduler = start_news_scheduler(
            interval_seconds=interval_seconds,
            news_db=NEWS_DB_PATH,
            main_db=MAIN_DB_PATH,
        )
        logger.info(f"📰 NewsSyncer started via news_engine v3 (every {interval_seconds}s)")

    def sync(self):
        """Run a manual sync cycle — returns list of articles."""
        try:
            articles = fetch_all_rss_feeds(NEWS_DB_PATH)
            if articles:
                sync_to_announcements(articles, MAIN_DB_PATH)
            return articles or []
        except Exception as e:
            logger.error(f"NewsSyncer.sync error: {e}")
            return []

    def status(self):
        if self._scheduler:
            return self._scheduler.status()
        return {'running': False}


# ============================================================
# fetch_rss_news() — used by /api/news/live
# Returns list of dicts with keys main.py expects:
#   id, title, summary, source_url, source, category, published_date
# ============================================================

def fetch_rss_news():
    """Fetch fresh RSS articles, return in format main.py expects."""
    articles = fetch_all_rss_feeds(NEWS_DB_PATH)

    # Also sync to announcements while we're at it
    if articles:
        sync_to_announcements(articles, MAIN_DB_PATH)

    # Convert to the format /api/news/live expects
    result = []
    for a in articles:
        pub = a.get('published_at')
        if isinstance(pub, datetime):
            pub = pub.isoformat()
        result.append({
            'id': a.get('id'),
            'title': a.get('title'),
            'summary': a.get('summary', ''),
            'source_url': a.get('url', '#'),
            'source': a.get('source', 'Unknown'),
            'category': a.get('category', 'Industry'),
            'published_date': pub,
            'published': pub,
        })
    return result


# ============================================================
# save_news_to_db(articles) — used by /api/news/live
# ============================================================

def save_news_to_db(articles):
    """Save articles to both databases. Returns count saved."""
    if not articles:
        return 0

    # Convert from main.py format back to news_engine format if needed
    engine_articles = []
    for a in articles:
        engine_articles.append({
            'id': a.get('id'),
            'title': a.get('title', ''),
            'url': a.get('source_url') or a.get('url', '#'),
            'source': a.get('source', 'Unknown'),
            'category': a.get('category', 'Industry'),
            'summary': a.get('summary', ''),
            'author': a.get('author', ''),
            'published_at': a.get('published_date') or a.get('published'),
            'relevance_score': a.get('relevance_score', 0.5),
            'keywords': a.get('keywords', '[]'),
            'image_url': a.get('image_url'),
        })

    saved = save_articles(engine_articles, NEWS_DB_PATH)
    sync_to_announcements(engine_articles, MAIN_DB_PATH)
    return saved


# ============================================================
# sync_news() — used by /api/news/sync POST endpoint
# ============================================================

def sync_news():
    """Full sync — returns count of new articles saved."""
    result = sync_all_news(NEWS_DB_PATH, MAIN_DB_PATH)
    return result.get('new_saved', 0)


# ============================================================
# register_admin_apis(app) — registers admin/status endpoints
# ============================================================

def register_admin_apis(app):
    """Register admin API endpoints on the Flask app."""

    @app.route('/api/admin/news-status', methods=['GET'])
    def admin_news_status():
        from flask import jsonify
        scheduler = get_news_scheduler()
        return jsonify({
            'success': True,
            'engine': 'news_engine_v3',
            'scheduler': scheduler.status() if scheduler else {'running': False},
            'feeds': len(get_feed_health(NEWS_DB_PATH)),
        })

    @app.route('/api/admin/news-sync', methods=['POST'])
    def admin_news_sync():
        from flask import jsonify
        result = sync_all_news(NEWS_DB_PATH, MAIN_DB_PATH)
        return jsonify(result)

    @app.route('/api/admin/feed-health', methods=['GET'])
    def admin_feed_health():
        from flask import jsonify
        return jsonify({
            'success': True,
            'feeds': get_feed_health(NEWS_DB_PATH),
        })

    logger.info("✅ Auto-Sync admin APIs registered (powered by news_engine v3)")
