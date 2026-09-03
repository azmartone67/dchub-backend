"""
DC Hub - SEO & Site Promotion Engine
===========================================
Automatically promotes dchub.cloud across search engines and industry platforms.

Features:
- Sitemap generation and submission to Google, Bing, DuckDuckGo
- IndexNow API for instant indexing on Bing/Yandex
- Structured data (JSON-LD) generation for rich search results
- Press release content generation
- Industry site backlink tracking
- Social media cross-posting
"""

import requests
import hashlib
import time
import threading
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import urljoin, quote
import xml.etree.ElementTree as ET
from db_utils import get_db
from routes._swallowed_writes import note_swallowed_write
from ai_surface_canon import canon_text
_CANON_FAC = canon_text("{canon_facilities}")

DB_PATH = 'dc_nexus.db'
SITE_URL = os.environ.get('SITE_URL', 'https://dchub.cloud')

INDEXNOW_KEY = os.environ.get('INDEXNOW_KEY', '')

# 2026-09-02 (QA sweep F10): Google retired the /ping?sitemap= endpoint in
# 2023 (404 since) and Bing deprecated its /ping in favour of IndexNow. Both
# engines are now reached through the channels that actually work — the
# Search Console API (routes/google_search_console.py) and IndexNow
# (routes/indexnow.py) — so neither carries a `ping_url` any more.
# submit_sitemap_to_engines skips an engine without one; it had been logging
# a 404 "submission" per cycle for each.
SEARCH_ENGINES = {
    'google': {
        'search_console': 'https://search.google.com/search-console',
        'enabled': True
    },
    'bing': {
        'indexnow_url': 'https://www.bing.com/indexnow',
        'enabled': True
    },
    'yandex': {
        'ping_url': 'https://webmaster.yandex.com/ping?sitemap=',
        'indexnow_url': 'https://yandex.com/indexnow',
        'enabled': True
    }
}

def compose_location_slug(city, state=None, country=None):
    """`<city>-<state-or-country>` slug for /locations/, or '' when there is
    no city. Before 2026-09-02 an empty-string city (the legacy `facilities`
    table has ~100 of them; `IS NOT NULL` let them through) produced
    "-al", "-ao", ... — 372-URL sitemaps carrying 100+ live 404s."""
    city = (city or "").strip()
    if not city:
        return ""
    region = (state or "").strip() or (country or "").strip()
    parts = [city] + ([region] if region else [])
    return "-".join(parts).lower().replace(" ", "-")


INDUSTRY_SITES = [
    {'name': 'Data Center Dynamics', 'url': 'https://www.datacenterdynamics.com', 'contact': 'press@datacenterdynamics.com'},
    {'name': 'Data Center Knowledge', 'url': 'https://www.datacenterknowledge.com', 'contact': 'tips@datacenterknowledge.com'},
    {'name': 'Data Center Frontier', 'url': 'https://www.datacenterfrontier.com', 'contact': 'editor@datacenterfrontier.com'},
    {'name': 'Capacity Media', 'url': 'https://www.capacitymedia.com', 'contact': 'news@capacitymedia.com'},
    {'name': 'Uptime Institute', 'url': 'https://uptimeinstitute.com', 'contact': 'press@uptimeinstitute.com'},
    {'name': 'BizNow', 'url': 'https://www.bisnow.com', 'contact': 'tips@bisnow.com'},
    {'name': 'Commercial Observer', 'url': 'https://commercialobserver.com', 'contact': 'tips@commercialobserver.com'},
]


class SEOPromotionEngine:
    """Engine for promoting the site across search engines and industry platforms"""
    
    def __init__(self, db_path: str = DB_PATH, site_url: str = SITE_URL):
        self.db_path = db_path
        self.site_url = site_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'DCHub-SEO-Bot/1.0 (+https://dchub.cloud)'
        })
        self._init_db()
        
    def _get_db(self):
        """Get database connection with WAL mode and timeout"""
        conn = get_db(self.db_path)
        return conn

    def _init_db(self):
        """Initialize SEO tracking tables.

        r-seoddl (2026-06-23): these CREATE TABLEs ran through db_utils get_db(),
        which SILENTLY SKIPS DDL (SKIP_DDL=1) — so the tables were NEVER created
        and get_seo_stats 500'd `relation "seo_submissions" does not exist`. Run
        the DDL on a RAW autocommit psycopg2 conn that bypasses the skip (same
        pattern as routes/ai_reach_rollup._ensure_tables). Also Postgres-cleaned:
        TIMESTAMPTZ DEFAULT NOW() (was `TEXT DEFAULT CURRENT_TIMESTAMP`, which
        Postgres can't auto-cast) and `verified BOOLEAN DEFAULT FALSE` (was
        `DEFAULT 0`, invalid for a Postgres boolean). Fail-soft."""
        import os
        import psycopg2 as _pg
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            return
        try:
            conn = _pg.connect(dsn, sslmode="require", connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS seo_submissions (
                    id SERIAL PRIMARY KEY,
                    engine TEXT NOT NULL,
                    url_submitted TEXT NOT NULL,
                    submission_type TEXT,
                    status TEXT DEFAULT 'pending',
                    response_code INTEGER,
                    submitted_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS backlinks (
                    id SERIAL PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    source_domain TEXT,
                    target_url TEXT,
                    anchor_text TEXT,
                    discovered_at TIMESTAMPTZ DEFAULT NOW(),
                    verified BOOLEAN DEFAULT FALSE,
                    UNIQUE(source_url, target_url)
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS press_releases (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT,
                    status TEXT DEFAULT 'draft',
                    distributed_to TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    published_at TIMESTAMPTZ
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS seo_stats (
                    id SERIAL PRIMARY KEY,
                    date TEXT UNIQUE,
                    pages_indexed INTEGER DEFAULT 0,
                    sitemap_submissions INTEGER DEFAULT 0,
                    indexnow_pings INTEGER DEFAULT 0,
                    backlinks_found INTEGER DEFAULT 0
                )
            ''')
            conn.close()
        except Exception as _e:
            print(f"[seo_engine] _init_db skipped: {_e}")
    
    def generate_sitemap(self) -> str:
        """Generate XML sitemap from facilities and pages"""
        root = ET.Element('urlset')
        root.set('xmlns', 'http://www.sitemaps.org/schemas/sitemap/0.9')
        
        static_pages = [
            {'loc': '/', 'priority': '1.0', 'changefreq': 'daily'},
            {'loc': '/news', 'priority': '0.9', 'changefreq': 'hourly'},
            {'loc': '/map', 'priority': '0.8', 'changefreq': 'daily'},
            {'loc': '/transactions', 'priority': '0.8', 'changefreq': 'daily'},
            {'loc': '/land-power', 'priority': '0.7', 'changefreq': 'weekly'},
            {'loc': '/api-docs', 'priority': '0.6', 'changefreq': 'weekly'},
        ]
        
        for page in static_pages:
            url_elem = ET.SubElement(root, 'url')
            ET.SubElement(url_elem, 'loc').text = urljoin(self.site_url, page['loc'])
            ET.SubElement(url_elem, 'lastmod').text = datetime.now().strftime('%Y-%m-%d')
            ET.SubElement(url_elem, 'changefreq').text = page['changefreq']
            ET.SubElement(url_elem, 'priority').text = page['priority']
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT DISTINCT city, state, country FROM facilities
                WHERE city IS NOT NULL AND TRIM(city) <> ''
                LIMIT 500
            ''')
            
            for city, state, country in cursor.fetchall():
                location_slug = compose_location_slug(city, state, country)
                if not location_slug:
                    continue   # empty city used to emit "/locations/-al" (404)
                url_elem = ET.SubElement(root, 'url')
                ET.SubElement(url_elem, 'loc').text = urljoin(self.site_url, f'/locations/{location_slug}')
                ET.SubElement(url_elem, 'changefreq').text = 'weekly'
                ET.SubElement(url_elem, 'priority').text = '0.6'
            
            conn.close()
        except Exception as e:
            print(f"⚠️ Error adding locations to sitemap: {e}")
        
        sitemap_xml = ET.tostring(root, encoding='unicode')
        sitemap_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + sitemap_xml
        
        try:
            with open('static/sitemap.xml', 'w') as f:
                f.write(sitemap_xml)
            print(f"✅ Sitemap generated with {len(root)} URLs")
        except Exception as e:
            print(f"⚠️ Could not save sitemap file: {e}")
        
        return sitemap_xml
    
    def submit_sitemap_to_engines(self) -> Dict:
        """Submit sitemap to all search engines"""
        results = {}
        sitemap_url = quote(urljoin(self.site_url, '/sitemap.xml'), safe=':/')
        
        for engine, config in SEARCH_ENGINES.items():
            if not config.get('enabled') or not config.get('ping_url'):
                continue

            try:
                ping_url = config['ping_url'] + sitemap_url
                response = self.session.get(ping_url, timeout=10)
                
                results[engine] = {
                    'status': 'success' if response.status_code == 200 else 'failed',
                    'code': response.status_code,
                    'url': ping_url
                }
                
                self._log_submission(engine, sitemap_url, 'sitemap', response.status_code)
                print(f"   {'✅' if response.status_code == 200 else '❌'} {engine.title()}: {response.status_code}")
                
            except Exception as e:
                results[engine] = {'status': 'error', 'error': str(e)}
                print(f"   ❌ {engine.title()}: {e}")
        
        return results
    
    def ping_indexnow(self, urls: List[str] = None) -> Dict:
        """Use IndexNow API for instant indexing on Bing/Yandex.

        r-indexnow-consolidate (2026-07-03): delegates to the canonical
        routes.indexnow.submit_to_indexnow — this class's own loop was gated
        on the INDEXNOW_KEY env var (unset in prod → no-op). Keeps the
        per-URL _log_submission bookkeeping and the results-dict shape."""
        if not urls:
            urls = [
                urljoin(self.site_url, '/'),
                urljoin(self.site_url, '/news'),
                urljoin(self.site_url, '/map'),
            ]

        results = {}
        try:
            from routes.indexnow import submit_to_indexnow
            res = submit_to_indexnow(urls)
            engine_name = res.get('endpoint') or 'indexnow'
            results[engine_name] = {
                'status': 'success' if res.get('ok') else 'failed',
                'code': res.get('status'),
                'urls_submitted': res.get('submitted', 0),
            }
            for url in urls:
                self._log_submission(engine_name, url, 'indexnow', res.get('status'))
        except Exception as e:
            results['indexnow'] = {'status': 'error', 'error': str(e)}

        return results
    
    def generate_structured_data(self) -> Dict:
        """Generate JSON-LD structured data for rich search results"""
        organization = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "DC Hub",
            "url": self.site_url,
            "logo": urljoin(self.site_url, "/static/logo.png"),
            "description": canon_text("Comprehensive data center intelligence platform tracking {canon_facilities} distinct facilities worldwide"),
            "sameAs": [
                "https://twitter.com/dchubcloud",
                "https://www.linkedin.com/company/dchub"
            ],
            "contactPoint": {
                "@type": "ContactPoint",
                "email": "info@dchub.cloud",
                "contactType": "customer service"
            }
        }
        
        website = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "DC Hub",
            "url": self.site_url,
            "potentialAction": {
                "@type": "SearchAction",
                "target": urljoin(self.site_url, "/search%sq={search_term_string}"),
                "query-input": "required name=search_term_string"
            }
        }
        
        dataset = {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": "Global Data Center Directory",
            "description": "Comprehensive database of data center facilities worldwide",
            "url": urljoin(self.site_url, "/api/v1/facilities"),
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "creator": organization,
            "dateModified": datetime.now().isoformat()
        }
        
        return {
            'organization': organization,
            'website': website,
            'dataset': dataset
        }
    
    def generate_press_release(self, topic: str = 'platform_update') -> Dict:
        """Generate press release content for distribution"""
        templates = {
            'platform_update': {
                'title': f"DC Hub Expands Global Data Center Coverage to {_CANON_FAC} Facilities",
                'content': f"""FOR IMMEDIATE RELEASE

DC Hub, the leading data center intelligence platform, today announced significant expansion of its global facility database, now tracking over 10,000 data centers across 170+ countries.

The platform provides real-time market intelligence for hyperscale infrastructure, including:
- Comprehensive facility data from PeeringDB, OpenStreetMap, and industry sources
- AI-powered transaction detection and M&A tracking
- Capacity pipeline monitoring for new developments
- Energy infrastructure analysis for site selection

"Our self-learning AI continuously discovers and verifies new facilities, ensuring the most comprehensive coverage in the industry," said the DC Hub team.

Key Features:
- 10,000+ verified data center facilities
- 60+ automated news sources
- Real-time transaction intelligence
- Energy market analysis across 20 regions

For more information, visit {self.site_url}

Media Contact:
press@dchub.cloud

###
"""
            },
            'new_feature': {
                'title': "DC Hub Launches AI-Powered Deep Learning Engine",
                'content': f"""FOR IMMEDIATE RELEASE

DC Hub introduces breakthrough AI technology for data center market intelligence.

The new Deep Learning Engine automatically:
- Detects M&A transactions from news sources
- Tracks capacity pipeline and construction projects
- Identifies market trends and emerging operators
- Discovers new data sources continuously

Visit {self.site_url} to explore the platform.

###
"""
            }
        }
        
        template = templates.get(topic, templates['platform_update'])
        
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO press_releases (title, content, status)
                VALUES (%s, %s, 'draft')
            ''', (template['title'], template['content']))
            pr_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            return {
                'id': pr_id,
                'title': template['title'],
                'content': template['content'],
                'status': 'draft'
            }
        except Exception as e:
            return {'error': str(e)}
    
    def discover_backlinks(self) -> Dict:
        """Discover sites linking to dchub.cloud"""
        results = {'checked': 0, 'found': 0, 'new': 0}
        
        search_queries = [
            f'site:*.com "dchub.cloud"',
            f'"DC Hub" data center',
            f'link:dchub.cloud',
        ]
        
        return results
    
    def _log_submission(self, engine: str, url: str, sub_type: str, status_code: int):
        """Log a search engine submission"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO seo_submissions (engine, url_submitted, submission_type, response_code, status)
                VALUES (%s, %s, %s, %s, %s)
            ''', (engine, url, sub_type, status_code, 'success' if status_code in [200, 202] else 'failed'))
            conn.commit()
            conn.close()
        except:
            note_swallowed_write("seo_submissions", where="seo_promotion_engine._log_submission")
            pass
    
    def run_promotion_cycle(self) -> Dict:
        """Run a complete SEO promotion cycle"""
        start = time.time()
        
        results = {
            'timestamp': datetime.now().isoformat(),
            'sitemap': {},
            'search_engines': {},
            'indexnow': {},
            'structured_data': {},
            'duration': 0
        }
        
        print("\n📢 SEO PROMOTION CYCLE STARTING...")
        
        print("   📄 Generating sitemap...")
        self.generate_sitemap()
        results['sitemap'] = {'generated': True}
        
        print("   🔍 Submitting to search engines...")
        results['search_engines'] = self.submit_sitemap_to_engines()
        
        if INDEXNOW_KEY:
            print("   ⚡ Pinging IndexNow...")
            results['indexnow'] = self.ping_indexnow()
        
        print("   📊 Generating structured data...")
        results['structured_data'] = self.generate_structured_data()
        
        results['duration'] = time.time() - start
        
        print(f"✅ SEO PROMOTION COMPLETE in {results['duration']:.1f}s")
        
        return results
    
    def get_stats(self) -> Dict:
        """Get SEO statistics"""
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            
            # r-seopg (2026-06-23): single-quote string literals — in Postgres,
            # "success" is an IDENTIFIER (→ column does not exist), not a string.
            # And a Postgres boolean compares to TRUE, not 1. (SQLite tolerated both.)
            cursor.execute("SELECT COUNT(*) FROM seo_submissions WHERE status = 'success'")
            total_submissions = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM backlinks WHERE verified = TRUE')
            verified_backlinks = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM press_releases')
            total_prs = cursor.fetchone()[0]

            cursor.execute('''
                SELECT engine, COUNT(*) FROM seo_submissions
                WHERE status = 'success'
                GROUP BY engine
            ''')
            by_engine = {row[0]: row[1] for row in cursor.fetchall()}
            
            conn.close()
            
            return {
                'total_submissions': total_submissions,
                'verified_backlinks': verified_backlinks,
                'press_releases': total_prs,
                'by_engine': by_engine,
                'industry_sites_tracked': len(INDUSTRY_SITES)
            }
        except Exception as e:
            return {'error': str(e)}


_engine_instance = None
_engine_lock = threading.Lock()

def get_seo_engine() -> SEOPromotionEngine:
    """Get singleton instance"""
    global _engine_instance
    with _engine_lock:
        if _engine_instance is None:
            _engine_instance = SEOPromotionEngine()
        return _engine_instance


def run_seo_promotion() -> Dict:
    """Run SEO promotion cycle"""
    engine = get_seo_engine()
    return engine.run_promotion_cycle()


def get_seo_stats() -> Dict:
    """Get SEO statistics"""
    engine = get_seo_engine()
    return engine.get_stats()


def generate_press_release(topic: str = 'platform_update') -> Dict:
    """Generate a press release"""
    engine = get_seo_engine()
    return engine.generate_press_release(topic)
