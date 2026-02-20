"""
DC Hub Jobs API - External Scheduler Endpoints
================================================
One-shot job endpoints triggered by external cron/scheduler.
All endpoints require DCHUB_ADMIN_KEY authentication.
Each job does its work, cleans up memory, and returns JSON status.
"""

import gc
import os
import time
import logging
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

jobs_bp = Blueprint('jobs', __name__)

ADMIN_API_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')

APP_START_TIME = time.time()

_last_runs = {}


def require_admin_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('Authorization', '').replace('Bearer ', '') or \
              request.headers.get('X-API-Key', '')
        if not ADMIN_API_KEY or key != ADMIN_API_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def _record_run(job_name, success, duration, details=None):
    _last_runs[job_name] = {
        'last_run': datetime.now(timezone.utc).isoformat(),
        'success': success,
        'duration_seconds': round(duration, 2),
        'details': details or {}
    }


def _gc_cleanup():
    gc.collect(2)
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass


@jobs_bp.route('/api/jobs/news-refresh', methods=['POST'])
@require_admin_key
def job_news_refresh():
    start = time.time()
    try:
        from auto_sync import sync_news
        saved = sync_news()
        count = saved if isinstance(saved, int) else 0
        duration = time.time() - start
        _record_run('news-refresh', True, duration, {'articles_updated': count})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'news-refresh',
            'articles_updated': count,
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('news-refresh', False, duration, {'error': str(e)})
        logger.error(f"Job news-refresh failed: {e}")
        return jsonify({'status': 'error', 'job': 'news-refresh', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/discovery', methods=['POST'])
@require_admin_key
def job_discovery():
    start = time.time()
    try:
        from main import run_peeringdb_discovery, run_osm_discovery, run_datacentermap_discovery, init_discovery_tables
        try:
            init_discovery_tables()
        except Exception:
            pass

        total_added = 0
        total_found = 0
        errors = []
        sources = {}

        for source_name, run_func in [
            ('peeringdb', run_peeringdb_discovery),
            ('openstreetmap', run_osm_discovery),
            ('datacentermap', run_datacentermap_discovery)
        ]:
            try:
                result = run_func()
                found = result.get('found', 0)
                added = result.get('added', 0)
                total_found += found
                total_added += added
                sources[source_name] = {'found': found, 'added': added}
            except Exception as e:
                errors.append(f"{source_name}: {str(e)}")
                sources[source_name] = {'error': str(e)}

        duration = time.time() - start
        _record_run('discovery', len(errors) == 0, duration, {
            'facilities_found': total_found,
            'facilities_added': total_added,
            'sources': sources,
            'errors': errors
        })
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'discovery',
            'facilities_found': total_found,
            'facilities_added': total_added,
            'sources': sources,
            'errors': errors,
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('discovery', False, duration, {'error': str(e)})
        logger.error(f"Job discovery failed: {e}")
        return jsonify({'status': 'error', 'job': 'discovery', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/global-intel', methods=['POST'])
@require_admin_key
def job_global_intel():
    start = time.time()
    try:
        from main import run_peeringdb_discovery, run_osm_discovery, run_datacentermap_discovery, init_discovery_tables
        try:
            init_discovery_tables()
        except Exception:
            pass

        total_added = 0
        total_found = 0

        for source_name, run_func in [
            ('openstreetmap', run_osm_discovery),
            ('datacentermap', run_datacentermap_discovery)
        ]:
            try:
                result = run_func()
                total_found += result.get('found', 0)
                total_added += result.get('added', 0)
            except Exception:
                pass

        duration = time.time() - start
        _record_run('global-intel', True, duration, {
            'facilities_found': total_found,
            'facilities_added': total_added
        })
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'global-intel',
            'facilities_found': total_found,
            'facilities_added': total_added,
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('global-intel', False, duration, {'error': str(e)})
        logger.error(f"Job global-intel failed: {e}")
        return jsonify({'status': 'error', 'job': 'global-intel', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/ecosystem', methods=['POST'])
@require_admin_key
def job_ecosystem():
    start = time.time()
    try:
        from db_utils import get_db
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM ecosystem_companies").fetchone()[0] or 0
        conn.close()

        duration = time.time() - start
        _record_run('ecosystem', True, duration, {'companies_count': count})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'ecosystem',
            'companies_count': count,
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('ecosystem', False, duration, {'error': str(e)})
        logger.error(f"Job ecosystem failed: {e}")
        return jsonify({'status': 'error', 'job': 'ecosystem', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/evolution', methods=['POST'])
@require_admin_key
def job_evolution():
    start = time.time()
    try:
        from db_utils import get_db
        conn = get_db()
        suggestions = []
        try:
            rows = conn.execute(
                "SELECT topic, suggestion FROM evolution_suggestions ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            suggestions = [{'topic': r[0], 'suggestion': r[1]} for r in rows]
        except Exception:
            pass
        conn.close()

        duration = time.time() - start
        _record_run('evolution', True, duration, {'suggestions_count': len(suggestions)})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'evolution',
            'suggestions_count': len(suggestions),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('evolution', False, duration, {'error': str(e)})
        logger.error(f"Job evolution failed: {e}")
        return jsonify({'status': 'error', 'job': 'evolution', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/outreach', methods=['POST'])
@require_admin_key
def job_outreach():
    start = time.time()
    try:
        try:
            from ai_outreach_agent import run_outreach_cycle
            if run_outreach_cycle:
                result = run_outreach_cycle()
            else:
                result = {'status': 'skipped', 'reason': 'outreach module not available'}
        except ImportError:
            result = {'status': 'skipped', 'reason': 'outreach module not installed'}

        duration = time.time() - start
        _record_run('outreach', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'outreach',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('outreach', False, duration, {'error': str(e)})
        logger.error(f"Job outreach failed: {e}")
        return jsonify({'status': 'error', 'job': 'outreach', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/promotion', methods=['POST'])
@require_admin_key
def job_promotion():
    start = time.time()
    try:
        try:
            from enhanced_promotion import run_promotion_cycle
            result = run_promotion_cycle()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'promotion module not available'}

        duration = time.time() - start
        _record_run('promotion', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'promotion',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('promotion', False, duration, {'error': str(e)})
        logger.error(f"Job promotion failed: {e}")
        return jsonify({'status': 'error', 'job': 'promotion', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/content-publish', methods=['POST'])
@require_admin_key
def job_content_publish():
    start = time.time()
    try:
        from db_utils import get_db
        conn = get_db()
        pending = 0
        published = 0
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM content_queue WHERE status = 'pending'"
            ).fetchone()[0] or 0
            published = conn.execute(
                "SELECT COUNT(*) FROM content_queue WHERE status = 'published'"
            ).fetchone()[0] or 0
        except Exception:
            pass
        conn.close()

        duration = time.time() - start
        _record_run('content-publish', True, duration, {
            'pending': pending, 'published': published
        })
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'content-publish',
            'pending': pending,
            'published': published,
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('content-publish', False, duration, {'error': str(e)})
        logger.error(f"Job content-publish failed: {e}")
        return jsonify({'status': 'error', 'job': 'content-publish', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/ai-wars', methods=['POST'])
@require_admin_key
def job_ai_wars():
    start = time.time()
    try:
        try:
            from ai_wars_automation import _weekly_battle_runner
            _weekly_battle_runner()
            result = {'battles_run': True}
        except ImportError:
            result = {'status': 'skipped', 'reason': 'ai_wars_automation not available'}

        duration = time.time() - start
        _record_run('ai-wars', True, duration, result)
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'ai-wars',
            'result': result,
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('ai-wars', False, duration, {'error': str(e)})
        logger.error(f"Job ai-wars failed: {e}")
        return jsonify({'status': 'error', 'job': 'ai-wars', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/status', methods=['GET'])
@require_admin_key
def job_status():
    try:
        import psutil
        rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        rss_mb = 0

    try:
        from db_utils import get_pool_health
        pool = get_pool_health()
    except Exception:
        pool = {}

    return jsonify({
        'status': 'ok',
        'memory_mb': round(rss_mb, 1),
        'db_pool': pool,
        'uptime_seconds': round(time.time() - APP_START_TIME, 1),
        'last_runs': _last_runs,
        'available_jobs': [
            'news-refresh', 'discovery', 'global-intel', 'ecosystem',
            'evolution', 'outreach', 'promotion', 'content-publish', 'ai-wars'
        ]
    })


def register_jobs_api(app):
    app.register_blueprint(jobs_bp)
    logger.info("✅ Jobs API registered (external scheduler endpoints)")
    logger.info("   POST /api/jobs/news-refresh")
    logger.info("   POST /api/jobs/discovery")
    logger.info("   POST /api/jobs/global-intel")
    logger.info("   POST /api/jobs/ecosystem")
    logger.info("   POST /api/jobs/evolution")
    logger.info("   POST /api/jobs/outreach")
    logger.info("   POST /api/jobs/promotion")
    logger.info("   POST /api/jobs/content-publish")
    logger.info("   POST /api/jobs/ai-wars")
    logger.info("   GET  /api/jobs/status")
