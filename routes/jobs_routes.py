"""
DC Hub - Cron Job Endpoints (/api/jobs/*)
Phase 2 Extract 4: 20 routes + _require_admin_key helper
Extracted from main.py to reduce monolith size

Called by Railway scheduler service. Auth: X-Admin-Key header or
?admin_key= query param, validated against DCHUB_ADMIN_KEY env var.
Each endpoint wraps existing internal logic as a one-shot trigger.

Dependencies injected via init_jobs_routes():
  - _scheduler_registry (mutable dict — updates propagate to main.py)
  - AUTOPILOT_AVAILABLE, EVOLUTION_AVAILABLE (bools)
  - discovery_engine (object or None)
  - IS_RAILWAY (bool)
"""

import os
import logging
import json
import psycopg2
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

APP_VERSION = os.environ.get('APP_VERSION', '2.5.2')

jobs_bp = Blueprint('jobs', __name__)

# Late-binding dependency slots
_scheduler_registry = {}
_AUTOPILOT_AVAILABLE = False
_EVOLUTION_AVAILABLE = False
_discovery_engine = None
_IS_RAILWAY = False


def init_jobs_routes(scheduler_registry, autopilot_available, evolution_available,
                     discovery_engine, is_railway):
    """Inject dependencies from main.py (late-binding pattern)."""
    global _scheduler_registry, _AUTOPILOT_AVAILABLE, _EVOLUTION_AVAILABLE
    global _discovery_engine, _IS_RAILWAY
    _scheduler_registry = scheduler_registry
    _AUTOPILOT_AVAILABLE = autopilot_available
    _EVOLUTION_AVAILABLE = evolution_available
    _discovery_engine = discovery_engine
    _IS_RAILWAY = is_railway
    # Phase QQQ (2026-05-17): ensure cron_last_run table exists.
    _ensure_cron_last_run_table()


# ----------------------------------------------------------------------
# Phase QQQ (2026-05-17): Cron-fired observability
#
# Until now the brain checked that crons were SCHEDULED
# (`check_cron_coverage`) but never that they actually RAN. A cron
# can have a perfect schedule and silently never execute (Railway
# crashed mid-run, env-var gate returned early, scheduler container
# died) and `check_cron_coverage` is none the wiser. That's exactly
# the failure mode behind auto-publish silently skipping for weeks.
#
# Fix: every /api/jobs/* endpoint calls `_record_cron_run(name)` on
# entry. New brain detector `check_cron_freshness` reads this table
# and flags any cron whose last-run timestamp is > 2× expected
# interval (or NULL = never fired since deploy).
# ----------------------------------------------------------------------

def _ensure_cron_last_run_table():
    """Create cron_last_run if missing. Safe to call repeatedly."""
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        logger.warning("cron_last_run: DATABASE_URL missing, skipping table create")
        return
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS cron_last_run (
                        job_name           TEXT PRIMARY KEY,
                        last_started_at    TIMESTAMP WITH TIME ZONE NOT NULL,
                        last_completed_at  TIMESTAMP WITH TIME ZONE,
                        last_status        TEXT,
                        last_duration_ms   INTEGER,
                        last_error         TEXT,
                        expected_interval_s INTEGER,
                        run_count          BIGINT DEFAULT 0
                    )
                """)
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"cron_last_run: table create failed: {type(e).__name__}: {e}")


def _record_cron_run(job_name, expected_interval_s=None):
    """Stamp cron_last_run with start time + bump run_count.

    Call at the very TOP of each /api/jobs/* endpoint. Failures are
    swallowed — observability MUST NEVER break the underlying job.
    """
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        return
    try:
        conn = psycopg2.connect(db_url, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cron_last_run
                        (job_name, last_started_at, expected_interval_s, run_count)
                    VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s, 1)
                    ON CONFLICT (job_name) DO UPDATE SET
                        last_started_at = EXCLUDED.last_started_at,
                        expected_interval_s = COALESCE(
                            EXCLUDED.expected_interval_s,
                            cron_last_run.expected_interval_s
                        ),
                        run_count = cron_last_run.run_count + 1
                """, (job_name, expected_interval_s))
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"_record_cron_run({job_name}): {type(e).__name__}: {e}")


def _record_cron_complete(job_name, status='ok', duration_ms=None, error=None):
    """Optional completion stamp. The detector only needs start time
    to spot silently-dead crons; this enables richer telemetry."""
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        return
    try:
        conn = psycopg2.connect(db_url, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE cron_last_run
                       SET last_completed_at = NOW(),
                           last_status = %s,
                           last_duration_ms = %s,
                           last_error = %s
                     WHERE job_name = %s
                """, (status, duration_ms, (error or '')[:500], job_name))
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"_record_cron_complete({job_name}): {type(e).__name__}: {e}")


def _require_admin_key():
    """Validate admin key from header or query param. Returns error tuple or None."""
    # Keep-alive is a low-security liveness ping — allow anonymous.
    # Railway's internal health-check (source IP 100.64.x.x CGN range) hits
    # this with no auth headers; the 'JOBS AUTH failed provided=0 chars'
    # warnings were all this caller. No sensitive data returned, no side
    # effects triggered, so short-circuiting the admin check is safe.
    if request.path.endswith('/api/jobs/keep-alive'):
        return None
    provided = (
        request.headers.get('X-Admin-Key', '')
        or request.headers.get('Authorization', '').replace('Bearer ', '')
        or request.args.get('admin_key', '')
        or request.args.get('key', '')
    )
    expected = os.environ.get('DCHUB_ADMIN_KEY', '')
    admin_secret = os.environ.get('ADMIN_SECRET', '')
    valid_keys = [k for k in [expected, admin_secret] if k]
    if not provided or not any(provided.strip() == k.strip() for k in valid_keys):
        logger.warning(
            "JOBS AUTH: ❌ failed (provided=%d chars, expected=%d chars) "
            "method=%s path=%s ip=%s ua=%s",
            len(provided.strip()), len(expected.strip()),
            request.method, request.path,
            request.remote_addr or "?",
            (request.user_agent.string if request.user_agent else "?")[:80],
        )
        return jsonify({'success': False, 'error': '🔒 authentication failed. Check DCHUB_ADMIN_KEY'}), 401

    # Phase QQQ (2026-05-17): record the cron run for every authenticated
    # /api/jobs/* hit. Derives job name from URL — one helper, zero
    # endpoint edits, all 20+ jobs get freshness tracking instantly.
    # Safe: _record_cron_run swallows all errors.
    try:
        path = request.path or ""
        # "/api/jobs/news-refresh" -> "news-refresh"
        if path.startswith('/api/jobs/'):
            job_name = path[len('/api/jobs/'):].split('/', 1)[0].strip()
            if job_name and job_name not in ('status', 'keep-alive'):
                _record_cron_run(job_name)
    except Exception:
        pass

    return None


def _get_pg():
    """Get a direct psycopg2 connection to Neon."""
    return psycopg2.connect(os.environ.get('DATABASE_URL', ''))


def _reg_update(key):
    """Update scheduler registry for a job key."""
    if key in _scheduler_registry:
        _scheduler_registry[key]['last_run'] = datetime.utcnow().isoformat()
        _scheduler_registry[key]['total_runs'] = _scheduler_registry[key].get('total_runs', 0) + 1


# =============================================================================
# EXTERNAL CRON JOB ENDPOINTS -- /api/jobs/*
# =============================================================================


@jobs_bp.route('/api/jobs/news-refresh', methods=['POST'])
def job_news_refresh():
    """Cron: Refresh news from all RSS sources.
    
    Pool isolation: news sync is the heaviest scheduled job (~90-150s, 34 feeds +
    16 Google News queries). To prevent pool exhaustion that cascades into health
    check failures and watchdog restarts, we set a dedicated DB connection via
    env var override so news_engine uses its own connection instead of the shared
    pool. The connection is cleaned up in the finally block.
    """
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err

    # --- Pool isolation: create a dedicated connection for the news sync ---
    _dedicated_conn = None
    try:
        db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        if db_url:
            _dedicated_conn = psycopg2.connect(db_url, connect_timeout=15)
            _dedicated_conn.autocommit = True
            # Store on module-level so news_engine can optionally pick it up
            os.environ['_NEWS_SYNC_ACTIVE'] = '1'
    except Exception as e:
        logger.warning("JOB news-refresh: dedicated conn failed (%s), proceeding with pool", e)

    try:
        from auto_sync import sync_news
        saved = sync_news()
        if 'news_sync' in _scheduler_registry:
            _scheduler_registry['news_sync']['last_run'] = datetime.utcnow().isoformat()
            _scheduler_registry['news_sync']['last_success'] = datetime.utcnow().isoformat()
            _scheduler_registry['news_sync']['items_last_cycle'] = saved if isinstance(saved, int) else 0
            _scheduler_registry['news_sync']['total_runs'] += 1
        logger.info("JOB news-refresh: ✅ %s new articles", saved)
        return jsonify({'success': True, 'job': 'news-refresh', 'new_articles': saved, 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB news-refresh: ❌ %s", e)
        return jsonify({'success': False, 'job': 'news-refresh', 'error': str(e)}), 500
    finally:
        os.environ.pop('_NEWS_SYNC_ACTIVE', None)
        if _dedicated_conn:
            try:
                _dedicated_conn.close()
            except Exception:
                pass


@jobs_bp.route('/api/jobs/discovery', methods=['POST'])
def job_discovery():
    """Cron: Run facility discovery (PeeringDB, OSM, datacentermap)"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        import concurrent.futures
        total_added = 0
        total_found = 0
        errors = []
        try:
            from routes.discovery_routes import (run_peeringdb_discovery,
                                                  run_osm_discovery, run_datacentermap_discovery)
            sources = [('peeringdb', run_peeringdb_discovery),
                       ('openstreetmap', run_osm_discovery),
                       ('datacentermap', run_datacentermap_discovery)]
        except ImportError:
            return jsonify({'success': True, 'job': 'discovery', 'found': 0, 'added': 0,
                            'note': 'discovery_routes not available', 'ts': datetime.utcnow().isoformat()})
        for source_name, run_func in sources:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(run_func)
                    result = future.result(timeout=120)
                total_found += result.get('found', 0)
                total_added += result.get('added', 0)
            except concurrent.futures.TimeoutError:
                errors.append(f"{source_name}: timed out after 120s")
                logger.warning("JOB discovery: %s timed out after 120s", source_name)
            except Exception as e:
                errors.append(f"{source_name}: {str(e)[:100]}")

        _reg_update('facility_discovery')
        if 'facility_discovery' in _scheduler_registry:
            _scheduler_registry['facility_discovery']['last_success'] = datetime.utcnow().isoformat()
            _scheduler_registry['facility_discovery']['items_last_cycle'] = total_added
        logger.info("JOB discovery: ✅ found=%d added=%d errors=%d", total_found, total_added, len(errors))
        return jsonify({'success': True, 'job': 'discovery', 'found': total_found, 'added': total_added, 'errors': errors or None, 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB discovery: ❌ %s", e)
        return jsonify({'success': False, 'job': 'discovery', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/auto-approve', methods=['POST'])
def job_auto_approve():
    """Cron: Auto-approve staged discoveries into facilities"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from discovery_auto_approve import run_auto_approval
        result = run_auto_approval(max_records=100)
        _reg_update('auto_approval')
        logger.info("JOB auto-approve: ✅ %s", result)
        return jsonify({'success': True, 'job': 'auto-approve', 'result': result, 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB auto-approve: ❌ %s", e)
        return jsonify({'success': False, 'job': 'auto-approve', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/evolution', methods=['POST'])
def job_evolution():
    """Cron: Run Evolution Engine cycle"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from evolution_engine import run_evolution_cycle
        result = run_evolution_cycle()
        _reg_update('evolution')
        logger.info("JOB evolution: ✅")
        return jsonify({'success': True, 'job': 'evolution', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'evolution', 'skipped': 'evolution_engine not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB evolution: ❌ %s", e)
        return jsonify({'success': False, 'job': 'evolution', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/ai-ecosystem', methods=['POST'])
def job_ai_ecosystem():
    """Cron: AI Ecosystem Agent enrichment cycle"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from ai_ecosystem_agent import agent as ecosystem_agent
        result = ecosystem_agent.run_cycle()
        _reg_update('ai_ecosystem')
        logger.info("JOB ai-ecosystem: ✅")
        return jsonify({'success': True, 'job': 'ai-ecosystem', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'ai-ecosystem', 'skipped': 'ai_ecosystem_agent not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB ai-ecosystem: ❌ %s", e)
        return jsonify({'success': False, 'job': 'ai-ecosystem', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/ai-outreach', methods=['POST'])
def job_ai_outreach():
    """Cron: AI Outreach Agent -- ping directories & platforms"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from ai_outreach_agent import run_outreach_cycle
        result = run_outreach_cycle()
        _reg_update('ai_outreach')
        logger.info("JOB ai-outreach: ✅")
        return jsonify({'success': True, 'job': 'ai-outreach', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'ai-outreach', 'skipped': 'ai_outreach_agent not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB ai-outreach: ❌ %s", e)
        return jsonify({'success': False, 'job': 'ai-outreach', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/global-intelligence', methods=['POST'])
def job_global_intelligence():
    """Cron: Global Intelligence Agent -- market analysis & enrichment"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from global_intelligence_agent import GlobalIntelligenceAgent
        agent = GlobalIntelligenceAgent()
        result = {}; result["international"] = agent.discover_international_facilities(); result["pipeline"] = agent.track_capacity_pipeline()
        _reg_update('global_intelligence')
        logger.info("JOB global-intelligence: ✅")
        return jsonify({'success': True, 'job': 'global-intelligence', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'global-intelligence', 'skipped': 'global_intelligence_agent not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB global-intelligence: ❌ %s", e)
        return jsonify({'success': False, 'job': 'global-intelligence', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/content-publish', methods=['POST'])
def job_content_publish():
    """Cron: Content publishing -- social posts, SEO updates"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    results = {}
    try:
        from seo_promotion_engine import run_seo_promotion
        results['seo'] = run_seo_promotion()
    except Exception as e:
        results['seo'] = {'error': str(e)[:200]}
    try:
        if _AUTOPILOT_AVAILABLE and _discovery_engine and hasattr(_discovery_engine, 'social_poster'):
            results['social'] = {'status': 'available', 'note': 'Use /api/autopilot/social/test to trigger'}
        else:
            results['social'] = {'status': 'not_available'}
    except Exception as e:
        results['social'] = {'error': str(e)[:200]}
    _reg_update('promotion_engine')
    logger.info("JOB content-publish: ✅ %s", results)
    return jsonify({'success': True, 'job': 'content-publish', 'results': results, 'ts': datetime.utcnow().isoformat()})


@jobs_bp.route('/api/jobs/keep-alive', methods=['POST', 'GET'])
def job_keep_alive():
    """Cron: Keep-alive ping -- prevents idle timeout, pings Neon DB"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        conn = _get_pg()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.commit()
        cur.close()
        conn.close()
        _reg_update('keep_alive')
        return jsonify({'success': True, 'job': 'keep-alive', 'status': 'healthy', 'version': APP_VERSION, 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        return jsonify({'job': 'keep-alive', 'success': False, 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/status', methods=['GET'])
def job_status():
    """List all available cron job endpoints and their last run status"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    jobs = {
        'news-refresh': {'endpoint': '/api/jobs/news-refresh', 'method': 'POST', 'registry': _scheduler_registry.get('news_sync', {})},
        'discovery': {'endpoint': '/api/jobs/discovery', 'method': 'POST', 'registry': _scheduler_registry.get('facility_discovery', {})},
        'auto-approve': {'endpoint': '/api/jobs/auto-approve', 'method': 'POST', 'registry': _scheduler_registry.get('auto_approval', {})},
        'evolution': {'endpoint': '/api/jobs/evolution', 'method': 'POST', 'registry': _scheduler_registry.get('evolution', {})},
        'ai-ecosystem': {'endpoint': '/api/jobs/ai-ecosystem', 'method': 'POST', 'registry': _scheduler_registry.get('ai_ecosystem', {})},
        'ai-outreach': {'endpoint': '/api/jobs/ai-outreach', 'method': 'POST', 'registry': _scheduler_registry.get('ai_outreach', {})},
        'global-intelligence': {'endpoint': '/api/jobs/global-intelligence', 'method': 'POST', 'registry': _scheduler_registry.get('global_intelligence', {})},
        'content-publish': {'endpoint': '/api/jobs/content-publish', 'method': 'POST', 'registry': _scheduler_registry.get('promotion_engine', {})},
        'keep-alive': {'endpoint': '/api/jobs/keep-alive', 'method': 'POST/GET', 'registry': _scheduler_registry.get('keep_alive', {})},
        'autopilot': {'endpoint': '/api/jobs/autopilot', 'method': 'POST', 'registry': _scheduler_registry.get('autopilot', {})},
        'autonomous-brain': {'endpoint': '/api/jobs/autonomous-brain', 'method': 'POST', 'registry': _scheduler_registry.get('autonomous_brain', {})},
        'alert-emails': {'endpoint': '/api/jobs/alert-emails', 'method': 'POST', 'registry': _scheduler_registry.get('alert_email_checker', {})},
        'simple-alerts': {'endpoint': '/api/jobs/simple-alerts', 'method': 'POST', 'registry': _scheduler_registry.get('simple_alerts_processor', {})},
        'market-report': {'endpoint': '/api/jobs/market-report', 'method': 'POST', 'registry': _scheduler_registry.get('daily_market_report', {})},
        'infrastructure-sync': {'endpoint': '/api/jobs/infrastructure-sync', 'method': 'POST', 'registry': _scheduler_registry.get('infrastructure_sync', {})},
        'energy-discovery': {'endpoint': '/api/jobs/energy-discovery', 'method': 'POST', 'registry': _scheduler_registry.get('energy_discovery', {})},
        'capacity-headroom': {'endpoint': '/api/jobs/capacity-headroom', 'method': 'POST', 'registry': _scheduler_registry.get('capacity_headroom', {})},
        'ambassador': {'endpoint': '/api/jobs/ambassador', 'method': 'POST', 'registry': _scheduler_registry.get('ambassador', {})},
        'backup': {'endpoint': '/api/jobs/backup', 'method': 'POST', 'registry': _scheduler_registry.get('db_backup', {})},
        'mcp-rate-cleanup': {'endpoint': '/api/jobs/mcp-rate-cleanup', 'method': 'POST', 'registry': _scheduler_registry.get('mcp_rate_cleanup', {})},
    }
    return jsonify({'success': True, 'jobs': jobs, 'total': len(jobs), 'ts': datetime.utcnow().isoformat()})


# =============================================================================
# ADDITIONAL CRON JOB ENDPOINTS -- /api/jobs/* (Phase 2)
# =============================================================================


@jobs_bp.route('/api/jobs/autopilot', methods=['POST'])
def job_autopilot():
    """Cron: Auto-Pilot -- deal discovery from RSS feeds, saves to Neon"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        results = {}
        import re, hashlib
        from datetime import datetime as dt, timezone

        try:
            import feedparser
        except ImportError:
            return jsonify({'success': False, 'job': 'autopilot', 'error': 'feedparser not installed'}), 503

        FEEDS = [
            "https://www.datacenterdynamics.com/rss/",
            "https://www.datacenterknowledge.com/rss.xml",
            "https://www.prnewswire.com/rss/news-releases-list.rss",
            "https://www.businesswire.com/rss/home/?rss=G7",
            "https://feeds.reuters.com/reuters/businessNews",
        ]
        DEAL_KW = ['acqui','merger','data center','datacenter','colocation','hyperscale',
                   'billion','million','invest','joint venture','equity','debt','lease']
        VALUE_RE = re.compile(r'\$\s*([\d,.]+)\s*(billion|million|B|M)', re.IGNORECASE)
        BUYER_RE = re.compile(r'^([A-Z][\w\s/&,]+?)\s+(?:acquires?|buys?|invests?|announces?|closes?|completes?|partners?)', re.MULTILINE)
        JUNK = {'undisclosed','unknown','tbd','n/a','the','a ','an '}

        def val_m(t):
            m = VALUE_RE.search(t)
            if not m: return None
            n = float(m.group(1).replace(',',''))
            return round(n*1000 if m.group(2).lower() in ('billion','b') else n, 1)

        def buyer(t):
            m = BUYER_RE.search(t)
            if m:
                b = m.group(1).strip().rstrip(',')
                if 4 <= len(b) <= 80 and not any(j in b.lower() for j in JUNK):
                    return b
            return None

        def is_relevant(t):
            tl = t.lower()
            return sum(1 for k in DEAL_KW if k in tl) >= 2

        def deal_type(t):
            tl = t.lower()
            for k,v in [('acqui','acquisition'),('merger','acquisition'),('joint venture','jv'),
                        (' jv ','jv'),('debt','debt'),('loan','debt'),('financ','debt'),
                        ('equity','equity'),('invest','equity'),('lease','lease'),('capex','capex')]:
                if k in tl: return v
            return 'investment'

        db_url = os.environ.get('DATABASE_URL','')
        if not db_url:
            return jsonify({'success': False, 'error': 'No DATABASE_URL'}), 503

        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        saved = skipped = 0

        for feed_url in FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:25]:
                    title = entry.get('title','')
                    summary = entry.get('summary','') or ''
                    if not is_relevant(f"{title} {summary}"):
                        skipped += 1
                        continue
                    b = buyer(title)
                    if not b:
                        skipped += 1
                        continue
                    v = val_m(f"{title} {summary}")
                    dtype = deal_type(f"{title} {summary}")
                    pub = entry.get('published_parsed')
                    if pub:
                        ddate = dt(*pub[:3]).strftime('%Y-%m-%d')
                        dyear = pub[0]
                    else:
                        ddate = dt.now(timezone.utc).strftime('%Y-%m-%d')
                        dyear = dt.now(timezone.utc).year
                    did = hashlib.md5(f"{b}{title[:50]}".encode()).hexdigest()[:16]
                    try:
                        cur.execute("""
                            INSERT INTO deals (id,date,year,buyer,seller,value,type,region,market,source_url,created_at,verified)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING,0)
                            ON CONFLICT (id) DO NOTHING
                        """, (did, ddate, dyear, b[:100], 'Undisclosed', v, dtype, None, None, entry.get('link',feed_url)[:500]))
                        if cur.rowcount: saved += 1
                    except Exception as ie:
                        conn.rollback()
                        logger.warning(f"Deal insert: {ie}")
            except Exception as fe:
                logger.warning(f"Feed error {feed_url}: {fe}")

        conn.commit()
        cur.close()
        conn.close()

        conn2 = psycopg2.connect(db_url)
        cur2 = conn2.cursor()
        cur2.execute("SELECT COUNT(*) FROM deals")
        total = cur2.fetchone()[0]
        cur2.close(); conn2.close()

        results = {'saved': saved, 'skipped': skipped, 'total_neon': total, 'status': 'ok'}
        _reg_update('autopilot')
        logger.info("JOB autopilot: ✅ saved=%d total=%d", saved, total)
        return jsonify({'success': True, 'job': 'autopilot', 'results': results, 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB autopilot: ❌ %s", e)
        return jsonify({'success': False, 'job': 'autopilot', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/autonomous-brain', methods=['POST'])
def job_autonomous_brain():
    """Cron: Autonomous Brain -- self-learning & pattern detection"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from autonomous_brain import init_autonomous_brain
        result = init_autonomous_brain()
        _reg_update('autonomous_brain')
        logger.info("JOB autonomous-brain: ✅")
        return jsonify({'success': True, 'job': 'autonomous-brain', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'autonomous-brain', 'skipped': 'autonomous_brain not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB autonomous-brain: ❌ %s", e)
        return jsonify({'success': False, 'job': 'autonomous-brain', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/alert-emails', methods=['POST'])
def job_alert_emails():
    """Cron: Alert email notification checker"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        try:
            from alert_emails import check_and_send_alert_emails
        except ImportError:
            from main import check_and_send_alert_emails
        result = check_and_send_alert_emails()
        _reg_update('alert_email_checker')
        logger.info("JOB alert-emails: ✅ %s", result)
        return jsonify({'success': True, 'job': 'alert-emails', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB alert-emails: ❌ %s", e)
        return jsonify({'success': False, 'job': 'alert-emails', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/simple-alerts', methods=['POST'])
def job_simple_alerts():
    """Cron: Simple alerts processing"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from simple_alerts import process_alerts
        result = process_alerts()
        _reg_update('simple_alerts_processor')
        logger.info("JOB simple-alerts: ✅")
        return jsonify({'success': True, 'job': 'simple-alerts', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'simple-alerts', 'skipped': 'simple_alerts not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB simple-alerts: ❌ %s", e)
        return jsonify({'success': False, 'job': 'simple-alerts', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/market-report', methods=['POST'])
def job_market_report():
    """Cron: Daily market intelligence report generation"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        conn = _get_pg()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM facilities")
        fac_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM deals")
        deal_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM news_articles")
        news_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT provider) FROM facilities")
        provider_count = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw IS NOT NULL")
        total_mw = cur.fetchone()[0]
        cur.execute("SELECT title, source, published_at FROM news_articles ORDER BY published_at DESC LIMIT 10")
        recent_news = [{'title': r[0], 'source': r[1], 'date': str(r[2])[:10] if r[2] else None} for r in cur.fetchall()]
        cur.close()
        conn.close()

        report = {
            'generated_at': datetime.utcnow().isoformat(),
            'summary': {
                'total_facilities': fac_count,
                'total_providers': provider_count,
                'total_power_mw': float(total_mw),
                'total_deals': deal_count,
                'total_news': news_count,
            },
            'recent_news': recent_news,
        }
        report_dir = 'market_reports'
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, f"market_report_{datetime.utcnow().strftime('%Y-%m-%d')}.json")
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        _reg_update('daily_market_report')
        logger.info("JOB market-report: ✅ saved to %s", report_path)
        return jsonify({'success': True, 'job': 'market-report', 'result': 'generated', 'report': report['summary'], 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB market-report: ❌ %s", e)
        return jsonify({'success': False, 'job': 'market-report', 'error': str(e)}), 500


# AUTO-REPAIR: duplicate route '/api/jobs/infrastructure-sync' also in energy_auto_discovery.py:559 — review and remove one
@jobs_bp.route('/api/jobs/infrastructure-sync', methods=['POST'])
def job_infrastructure_sync():
    """Cron: Infrastructure sync -- fiber, properties, permits, substations"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    results = {}
    try:
        from fiber_network_discovery import run_fiber_discovery
        results['fiber'] = run_fiber_discovery()
    except ImportError:
        results['fiber'] = {'status': 'not_available'}
    except Exception as e:
        results['fiber'] = {'error': str(e)[:200]}
    try:
        from construction_permit_tracker import run_permit_scan
        results['permits'] = run_permit_scan()
    except ImportError:
        results['permits'] = {'status': 'not_available'}
    except Exception as e:
        results['permits'] = {'error': str(e)[:200]}
    _reg_update('infrastructure_sync')
    logger.info("JOB infrastructure-sync: ✅ %s", {k: 'ok' if 'error' not in v else 'err' for k, v in results.items()})
    return jsonify({'success': True, 'job': 'infrastructure-sync', 'results': results, 'ts': datetime.utcnow().isoformat()})


# ──────────────────────────────────────────────────────────────────
# Phase r33-D (2026-05-21) — per-table infrastructure refresh.
#
# Closes the gap where transmission_lines / gas_pipelines / substations
# get stale (autopilot's check_data_freshness_sla_breach detector has
# no refresh endpoint to call, so it escalates instead of recovering).
# Each endpoint kicks the loader in a daemon thread so the HTTP call
# returns immediately — the loaders take 60-240s for full HIFLD pulls.
# Pairs with brain_autopilot.REFRESH_MAP entries below.
# ──────────────────────────────────────────────────────────────────
def _spawn_loader(name: str, run_fn) -> dict:
    """Fire-and-forget the loader on a daemon thread. Returns a token
    so the operator (or autopilot) can poll status if needed."""
    import threading, time as _t, traceback as _tb
    state = {"name": name, "started_at": datetime.utcnow().isoformat() + "Z",
             "status": "running", "rows": None, "error": None,
             "elapsed_s": None}
    _INFRA_REFRESH_STATE[name] = state
    def _runner():
        t0 = _t.time()
        try:
            res = run_fn()
            state["status"] = "ok"
            state["rows"] = res if isinstance(res, (int, str)) else "completed"
        except Exception as e:
            state["status"] = "error"
            state["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            logger.error("infra-refresh %s failed: %s\n%s",
                         name, e, _tb.format_exc()[:1000])
        finally:
            state["elapsed_s"] = round(_t.time() - t0, 1)
            state["ended_at"] = datetime.utcnow().isoformat() + "Z"
    threading.Thread(target=_runner, daemon=True,
                     name=f"infra-refresh-{name}").start()
    return state


_INFRA_REFRESH_STATE: dict[str, dict] = {}


@jobs_bp.route('/api/jobs/transmission-refresh', methods=['POST'])
def job_transmission_refresh():
    """Refresh transmission_lines from HIFLD. Daemon-threaded —
    returns immediately. Poll /api/jobs/infra-refresh-status."""
    auth_err = _require_admin_key()
    if auth_err: return auth_err
    def _run():
        # load_hifld_transmission.py is script-style (executes at
        # import). Run via runpy so we don't pollute sys.modules.
        import runpy
        runpy.run_path('/app/load_hifld_transmission.py',
                       run_name='__main__')
        return "transmission_lines refresh started"
    state = _spawn_loader('transmission_lines', _run)
    _reg_update('transmission_refresh')
    return jsonify({'success': True, 'job': 'transmission-refresh',
                    'state': state}), 202


@jobs_bp.route('/api/jobs/gas-refresh', methods=['POST'])
def job_gas_refresh():
    """Refresh gas_pipelines + gas_compressors + gas_processings. Uses
    the existing energy_auto_discovery_pg sync-all path but filters
    to the gas-related loaders only. Daemon-threaded."""
    auth_err = _require_admin_key()
    if auth_err: return auth_err
    def _run():
        rows = 0
        for mod, fn in (('pipeline_loader',        'load_pipelines'),
                        ('gas_compressor_loader',  'load_gas_compressors'),
                        ('gas_processing_loader',  'load_gas_processings')):
            try:
                m = __import__(mod, fromlist=[fn])
                f = getattr(m, fn, None)
                if f:
                    r = f()
                    if isinstance(r, int): rows += r
            except ImportError:
                continue
            except Exception as e:
                logger.warning("gas-refresh %s.%s skipped: %s", mod, fn, e)
        return rows or 'completed'
    state = _spawn_loader('gas_pipelines', _run)
    _reg_update('gas_refresh')
    return jsonify({'success': True, 'job': 'gas-refresh',
                    'state': state}), 202


@jobs_bp.route('/api/jobs/substations-refresh', methods=['POST'])
def job_substations_refresh():
    """Refresh substations from HIFLD via load_substations.load(). Daemon-threaded."""
    auth_err = _require_admin_key()
    if auth_err: return auth_err
    def _run():
        # load_substations.py exposes load() that connects via DATABASE_URL
        from load_substations import load as _load_substations
        r = _load_substations()
        return r if isinstance(r, (int, str)) else 'completed'
    state = _spawn_loader('substations', _run)
    _reg_update('substations_refresh')
    return jsonify({'success': True, 'job': 'substations-refresh',
                    'state': state}), 202


@jobs_bp.route('/api/jobs/infra-refresh-status', methods=['GET'])
def job_infra_refresh_status():
    """Poll status of any currently-running or just-completed
    transmission/gas/substations refresh. Public read."""
    return jsonify({'success': True,
                    'state': _INFRA_REFRESH_STATE}), 200


@jobs_bp.route('/api/jobs/energy-discovery', methods=['POST'])
def job_energy_discovery():
    """Cron: Energy infrastructure auto-discovery"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from energy_auto_discovery_pg import run_full_sync as run_energy_discovery
        import psycopg2
        db_url = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL')
        conn = psycopg2.connect(db_url)
        try:
            result = run_energy_discovery(conn)
        finally:
            conn.close()
        _reg_update('energy_discovery')
        logger.info("JOB energy-discovery: ✅")
        return jsonify({'success': True, 'job': 'energy-discovery', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'energy-discovery', 'skipped': 'energy_auto_discovery not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB energy-discovery: ❌ %s", e)
        return jsonify({'success': False, 'job': 'energy-discovery', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/capacity-headroom', methods=['POST'])
def job_capacity_headroom():
    """Cron: Capacity headroom scoring refresh"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from capacity_headroom_api import refresh_all_headroom
        result = refresh_all_headroom()
        _reg_update('capacity_headroom')
        logger.info("JOB capacity-headroom: ✅")
        return jsonify({'success': True, 'job': 'capacity-headroom', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'capacity-headroom', 'skipped': 'capacity_headroom_api not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB capacity-headroom: ❌ %s", e)
        return jsonify({'success': False, 'job': 'capacity-headroom', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/ambassador', methods=['POST'])
def job_ambassador():
    """Cron: Agentic ambassador outreach system"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from agentic_ambassador import run_ambassador_cycle
        result = run_ambassador_cycle()
        _reg_update('ambassador')
        logger.info("JOB ambassador: ✅")
        return jsonify({'success': True, 'job': 'ambassador', 'result': str(result)[:500] if result else 'ok', 'ts': datetime.utcnow().isoformat()})
    except ImportError:
        return jsonify({'success': True, 'job': 'ambassador', 'skipped': 'agentic_ambassador not available', 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB ambassador: ❌ %s", e)
        return jsonify({'success': False, 'job': 'ambassador', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/backup', methods=['POST'])
def job_backup():
    """Cron: Neon DB backup -- lightweight table-count + row-count snapshot.
    Scheduler hits /api/jobs/backup (not /api/jobs/db-backup).
    This is the simple daily check; the full export is on /api/jobs/db-backup.
    """
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        conn = _get_pg()
        cur = conn.cursor()

        # Snapshot key table counts
        tables = [
            'facilities', 'deals', 'news_articles', 'discovered_facilities',
            'fiber_routes', 'gas_pipelines', 'power_plants_eia',
            'transmission_lines_eia', 'api_keys', 'users',
            'mcp_rate_limits', 'daily_record_usage',
        ]
        snapshot = {}
        total_rows = 0
        for table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                snapshot[table] = count
                total_rows += count
            except Exception:
                conn.rollback()
                snapshot[table] = -1

        # DB size
        try:
            cur.execute("SELECT pg_database_size(current_database())")
            db_size_bytes = cur.fetchone()[0]
            db_size_mb = round(db_size_bytes / (1024 * 1024), 1)
        except Exception:
            conn.rollback()
            db_size_mb = -1

        # Active connections
        try:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
            active_conns = cur.fetchone()[0]
        except Exception:
            conn.rollback()
            active_conns = -1

        cur.close()
        conn.close()

        _reg_update('db_backup')
        logger.info("JOB backup: ✅ %d tables, %d total rows, %.1f MB", len(snapshot), total_rows, db_size_mb)
        return jsonify({
            'success': True,
            'job': 'backup',
            'size_mb': db_size_mb,
            'total_rows': total_rows,
            'tables': snapshot,
            'active_connections': active_conns,
            'ts': datetime.utcnow().isoformat(),
        })
    except Exception as e:
        logger.error("JOB backup: ❌ %s", e)
        return jsonify({'success': False, 'job': 'backup', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/mcp-rate-cleanup', methods=['POST'])
def job_mcp_rate_cleanup():
    """Cron: Clean up expired MCP rate limit entries from Neon.
    Deletes rows from mcp_rate_limits where the window has expired (>24h old).
    Also cleans stale daily_record_usage rows older than 7 days.
    """
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        conn = _get_pg()
        cur = conn.cursor()
        cleaned = {}

        # Clean expired MCP rate limits (older than 24 hours)
        try:
            cur.execute("""
                DELETE FROM mcp_rate_limits
                WHERE window_start < NOW() - INTERVAL '24 hours'
            """)
            cleaned['mcp_rate_limits'] = cur.rowcount
        except Exception as e:
            conn.rollback()
            cleaned['mcp_rate_limits'] = f'error: {str(e)[:100]}'

        # Clean stale daily record usage (older than 7 days)
        try:
            cur.execute("""
                DELETE FROM daily_record_usage
                WHERE usage_date < CURRENT_DATE - INTERVAL '7 days'
            """)
            cleaned['daily_record_usage'] = cur.rowcount
        except Exception as e:
            conn.rollback()
            cleaned['daily_record_usage'] = f'error: {str(e)[:100]}'

        conn.commit()
        cur.close()
        conn.close()

        total_cleaned = sum(v for v in cleaned.values() if isinstance(v, int))
        _reg_update('mcp_rate_cleanup')
        logger.info("JOB mcp-rate-cleanup: ✅ cleaned %d rows", total_cleaned)
        return jsonify({
            'success': True,
            'job': 'mcp-rate-cleanup',
            'cleaned': cleaned,
            'total_cleaned': total_cleaned,
            'ts': datetime.utcnow().isoformat(),
        })
    except Exception as e:
        logger.error("JOB mcp-rate-cleanup: ❌ %s", e)
        return jsonify({'success': False, 'job': 'mcp-rate-cleanup', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/db-backup', methods=['POST'])
def job_db_backup():
    """Cron: Neon database backup to local + optional R2"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from db_backup import run_backup, list_backups
        result = run_backup(include_secondary=True)
        _reg_update('db_backup')
        logger.info("JOB db-backup: ✅ %d tables, %d rows, %.1f MB",
                     result.get('tables_exported', 0), result.get('total_rows', 0),
                     result.get('compressed_size_mb', 0))
        return jsonify({'success': True, 'job': 'db-backup', 'result': result, 'ts': datetime.utcnow().isoformat()})
    except Exception as e:
        logger.error("JOB db-backup: ❌ %s", e)
        return jsonify({'success': False, 'job': 'db-backup', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/db-backup/list', methods=['GET'])
def job_db_backup_list():
    """List available database backups"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from db_backup import list_backups
        backups = list_backups()
        return jsonify({'success': True, 'backups': backups, 'count': len(backups)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/db-backup/verify', methods=['GET'])
def job_db_backup_verify():
    """Verify the most recent backup is valid"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
    try:
        from db_backup import list_backups, verify_backup
        from pathlib import Path
        backups = list_backups()
        if not backups:
            return jsonify({'success': False, 'error': 'No backups found'}), 404
        backup_dir = Path(os.path.dirname(os.path.abspath(__file__))).parent / "backups"
        latest = backup_dir / backups[0]['filename']
        result = verify_backup(str(latest))
        return jsonify({'success': True, 'verification': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@jobs_bp.route('/api/scheduler/status', methods=['GET'])
def scheduler_status():
    try:
        from scheduled_discovery import get_scheduler_status
        status = get_scheduler_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Phase A.5 cron + alerts (added 2026-04-21) ──
@jobs_bp.route("/api/jobs/health-probe", methods=["POST", "GET"])
def job_health_probe():
    """Cron: run probe + autoheal + alert on NEW failures only."""
    import httpx as _hx, os as _os
    port = _os.environ.get("PORT", "8080")
    base = _os.environ.get("INTERNAL_BASE_URL", f"http://127.0.0.1:{port}")
    prev_fails = set()
    try:
        import psycopg2
        conn = psycopg2.connect(_os.environ.get("DATABASE_URL", ""))
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ON (check_name) check_name, status FROM site_health_findings WHERE check_name NOT LIKE 'autoheal:%' ORDER BY check_name, checked_at DESC")
        for r in cur.fetchall():
            if r[1] == "fail": prev_fails.add(r[0])
        conn.close()
    except Exception as e:
        logger.warning(f"[cron-health] prev-fails: {e}")
    try:
        probe = _hx.get(base + "/api/_health/probe", timeout=60.0).json()
    except Exception as e:
        return jsonify({"ok": False, "stage": "probe", "error": str(e)[:200]}), 500
    current_fails = set(c["check"] for c in probe.get("checks", []) if c["status"] == "fail")
    new_fails = current_fails - prev_fails
    resolved = prev_fails - current_fails
    heal = None
    if current_fails:
        try:
            heal = _hx.get(base + "/api/_health/autoheal", timeout=120.0).json()
        except Exception as e:
            heal = {"error": str(e)[:200]}
    alerted = False
    webhook = _os.environ.get("HEALTH_ALERT_WEBHOOK", "")
    if new_fails and webhook:
        try:
            details = [c for c in probe.get("checks", []) if c["check"] in new_fails]
            msg = {"text": f"DC Hub watchdog: {len(new_fails)} new failure(s)",
                   "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                       "text": "*DC Hub — new failures*\n" + "\n".join(f"• `{c['check']}` — {str(c.get('actual', ''))[:150]}" for c in details)}}]}
            _hx.post(webhook, json=msg, timeout=10.0)
            alerted = True
        except Exception as e:
            logger.warning(f"[cron-health] webhook: {e}")
    _reg_update("health_probe")
    return jsonify({"ok": True, "summary": probe.get("summary"),
                    "new_fails": sorted(new_fails), "resolved": sorted(resolved),
                    "autoheal_summary": (heal or {}).get("summary"),
                    "alerted": alerted, "webhook_configured": bool(webhook)})

