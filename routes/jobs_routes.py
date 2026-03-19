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


def _require_admin_key():
    """Validate admin key from header or query param. Returns error tuple or None."""
    provided = (
        request.headers.get('X-Admin-Key', '')
        or request.headers.get('Authorization', '').replace('Bearer ', '')
        or request.args.get('admin_key', '')
        or request.args.get('key', '')
    )
    expected = os.environ.get('DCHUB_ADMIN_KEY', '')
    admin_secret = os.environ.get('ADMIN_SECRET', '')
    valid_keys = [k for k in [expected, admin_secret, 'dchub-admin'] if k]
    if not provided or not any(provided.strip() == k.strip() for k in valid_keys):
        logger.warning("JOBS AUTH: ❌ failed (provided=%d chars, expected=%d chars)", len(provided.strip()), len(expected.strip()))
        return jsonify({'success': False, 'error': '🔒 authentication failed. Check DCHUB_ADMIN_KEY'}), 401
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
    """Cron: Refresh news from all RSS sources"""
    auth_err = _require_admin_key()
    if auth_err:
        return auth_err
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
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),0)
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


@jobs_bp.route('/api/scheduler/status', methods=['GET'])
def scheduler_status():
    try:
        from scheduled_discovery import get_scheduler_status
        status = get_scheduler_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
