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

        try:
            from discovery_monitoring import record_facility_count_snapshot
            record_facility_count_snapshot()
        except Exception as e:
            logger.warning(f"Facility count snapshot: {e}")

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


@jobs_bp.route('/api/jobs/global-intelligence', methods=['POST'])
@require_admin_key
def job_global_intelligence_alias():
    return job_global_intel()


@jobs_bp.route('/api/jobs/ai-ecosystem', methods=['POST'])
@require_admin_key
def job_ai_ecosystem_alias():
    return job_ecosystem()


@jobs_bp.route('/api/jobs/ai-outreach', methods=['POST'])
@require_admin_key
def job_ai_outreach_alias():
    return job_outreach()


@jobs_bp.route('/api/jobs/auto-approve', methods=['POST'])
@require_admin_key
def job_auto_approve():
    start = time.time()
    try:
        try:
            from discovery_auto_approve import run_auto_approval
            result = run_auto_approval()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'discovery_auto_approve module not available'}

        duration = time.time() - start
        _record_run('auto-approve', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'auto-approve',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('auto-approve', False, duration, {'error': str(e)})
        logger.error(f"Job auto-approve failed: {e}")
        return jsonify({'status': 'error', 'job': 'auto-approve', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/keep-alive', methods=['POST'])
@require_admin_key
def job_keep_alive():
    start = time.time()
    _record_run('keep-alive', True, time.time() - start)
    return jsonify({'status': 'ok', 'job': 'keep-alive', 'uptime_seconds': round(time.time() - APP_START_TIME, 1)})


@jobs_bp.route('/api/jobs/autopilot', methods=['POST'])
@require_admin_key
def job_autopilot():
    start = time.time()
    try:
        try:
            from autonomous_brain import run_autopilot_cycle
            result = run_autopilot_cycle()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'autonomous_brain module not available'}

        duration = time.time() - start
        _record_run('autopilot', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'autopilot',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('autopilot', False, duration, {'error': str(e)})
        logger.error(f"Job autopilot failed: {e}")
        return jsonify({'status': 'error', 'job': 'autopilot', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/autonomous-brain', methods=['POST'])
@require_admin_key
def job_autonomous_brain():
    start = time.time()
    try:
        try:
            from autonomous_brain import run_brain_cycle
            result = run_brain_cycle()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'autonomous_brain module not available'}

        duration = time.time() - start
        _record_run('autonomous-brain', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'autonomous-brain',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('autonomous-brain', False, duration, {'error': str(e)})
        logger.error(f"Job autonomous-brain failed: {e}")
        return jsonify({'status': 'error', 'job': 'autonomous-brain', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/alert-emails', methods=['POST'])
@require_admin_key
def job_alert_emails():
    start = time.time()
    try:
        try:
            from intelligence_engine import send_queued_alerts
            result = send_queued_alerts()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'intelligence_engine alert module not available'}

        duration = time.time() - start
        _record_run('alert-emails', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'alert-emails',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('alert-emails', False, duration, {'error': str(e)})
        logger.error(f"Job alert-emails failed: {e}")
        return jsonify({'status': 'error', 'job': 'alert-emails', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/simple-alerts', methods=['POST'])
@require_admin_key
def job_simple_alerts():
    start = time.time()
    try:
        try:
            from intelligence_engine import check_alert_conditions
            result = check_alert_conditions()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'intelligence_engine alert module not available'}

        duration = time.time() - start
        _record_run('simple-alerts', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'simple-alerts',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('simple-alerts', False, duration, {'error': str(e)})
        logger.error(f"Job simple-alerts failed: {e}")
        return jsonify({'status': 'error', 'job': 'simple-alerts', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/market-report', methods=['POST'])
@require_admin_key
def job_market_report():
    start = time.time()
    try:
        try:
            from market_report import generate_market_report
            result = generate_market_report()
        except ImportError:
            from db_utils import get_db
            conn = get_db()
            stats = conn.execute(
                "SELECT COUNT(*) as facilities, COUNT(DISTINCT provider) as providers, "
                "ROUND(SUM(COALESCE(power_mw,0))::numeric) as total_mw FROM facilities"
            ).fetchone()
            conn.close()
            result = {
                'status': 'generated',
                'facilities': stats[0] if stats else 0,
                'providers': stats[1] if stats else 0,
                'total_mw': float(stats[2]) if stats and stats[2] else 0
            }

        duration = time.time() - start
        _record_run('market-report', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'market-report',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('market-report', False, duration, {'error': str(e)})
        logger.error(f"Job market-report failed: {e}")
        return jsonify({'status': 'error', 'job': 'market-report', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/infrastructure-sync', methods=['POST'])
@require_admin_key
def job_infrastructure_sync():
    start = time.time()
    try:
        try:
            from infrastructure_discovery import run_infrastructure_sync
            result = run_infrastructure_sync()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'infrastructure_discovery module not available'}

        duration = time.time() - start
        _record_run('infrastructure-sync', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'infrastructure-sync',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('infrastructure-sync', False, duration, {'error': str(e)})
        logger.error(f"Job infrastructure-sync failed: {e}")
        return jsonify({'status': 'error', 'job': 'infrastructure-sync', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/energy-discovery', methods=['POST'])
@require_admin_key
def job_energy_discovery():
    start = time.time()
    try:
        try:
            from infrastructure_discovery import run_energy_discovery
            result = run_energy_discovery()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'infrastructure_discovery energy module not available'}

        duration = time.time() - start
        _record_run('energy-discovery', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'energy-discovery',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('energy-discovery', False, duration, {'error': str(e)})
        logger.error(f"Job energy-discovery failed: {e}")
        return jsonify({'status': 'error', 'job': 'energy-discovery', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/capacity-headroom', methods=['POST'])
@require_admin_key
def job_capacity_headroom():
    start = time.time()
    try:
        try:
            from capacity_headroom import calculate_headroom
            result = calculate_headroom()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'capacity_headroom module not available'}

        duration = time.time() - start
        _record_run('capacity-headroom', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'capacity-headroom',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('capacity-headroom', False, duration, {'error': str(e)})
        logger.error(f"Job capacity-headroom failed: {e}")
        return jsonify({'status': 'error', 'job': 'capacity-headroom', 'error': str(e)}), 500


@jobs_bp.route('/api/jobs/ambassador', methods=['POST'])
@require_admin_key
def job_ambassador():
    start = time.time()
    try:
        try:
            from ai_outreach_agent import run_ambassador_cycle
            result = run_ambassador_cycle()
        except ImportError:
            result = {'status': 'skipped', 'reason': 'ai_outreach_agent ambassador module not available'}

        duration = time.time() - start
        _record_run('ambassador', True, duration, result if isinstance(result, dict) else {})
        _gc_cleanup()
        return jsonify({
            'status': 'complete',
            'job': 'ambassador',
            'result': result if isinstance(result, dict) else str(result),
            'duration_seconds': round(duration, 2)
        })
    except Exception as e:
        duration = time.time() - start
        _record_run('ambassador', False, duration, {'error': str(e)})
        logger.error(f"Job ambassador failed: {e}")
        return jsonify({'status': 'error', 'job': 'ambassador', 'error': str(e)}), 500


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
            'news-refresh', 'discovery', 'global-intel', 'global-intelligence',
            'ecosystem', 'ai-ecosystem', 'evolution', 'outreach', 'ai-outreach',
            'promotion', 'content-publish', 'ai-wars',
            'auto-approve', 'keep-alive', 'autopilot', 'autonomous-brain',
            'alert-emails', 'simple-alerts', 'market-report',
            'infrastructure-sync', 'energy-discovery', 'capacity-headroom',
            'ambassador'
        ]
    })


def register_jobs_api(app):
    app.register_blueprint(jobs_bp)
    logger.info("✅ Jobs API registered (18 job endpoints + 3 aliases)")
    logger.info("   POST /api/jobs/news-refresh")
    logger.info("   POST /api/jobs/discovery")
    logger.info("   POST /api/jobs/global-intel (alias: global-intelligence)")
    logger.info("   POST /api/jobs/ecosystem (alias: ai-ecosystem)")
    logger.info("   POST /api/jobs/evolution")
    logger.info("   POST /api/jobs/outreach (alias: ai-outreach)")
    logger.info("   POST /api/jobs/promotion")
    logger.info("   POST /api/jobs/content-publish")
    logger.info("   POST /api/jobs/ai-wars")
    logger.info("   POST /api/jobs/auto-approve")
    logger.info("   POST /api/jobs/keep-alive")
    logger.info("   POST /api/jobs/autopilot")
    logger.info("   POST /api/jobs/autonomous-brain")
    logger.info("   POST /api/jobs/alert-emails")
    logger.info("   POST /api/jobs/simple-alerts")
    logger.info("   POST /api/jobs/market-report")
    logger.info("   POST /api/jobs/infrastructure-sync")
    logger.info("   POST /api/jobs/energy-discovery")
    logger.info("   POST /api/jobs/capacity-headroom")
    logger.info("   POST /api/jobs/ambassador")
    logger.info("   GET  /api/jobs/status")
