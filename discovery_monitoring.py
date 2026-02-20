"""
Discovery Monitoring & Outreach Tracking API
=============================================
6 read-only admin endpoints for monitoring the autodiscovery pipeline,
scheduler health, and AI platform connections.

All endpoints under /api/admin/discovery/ require DCHUB_ADMIN_KEY auth.
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

discovery_monitor_bp = Blueprint('discovery_monitor', __name__)

ADMIN_API_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')
_APP_START_TIME = time.time()

_discovery_running = False
_discovery_lock = threading.Lock()


def _require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('Authorization', '').replace('Bearer ', '') or \
              request.headers.get('X-API-Key', '') or \
              request.headers.get('X-Internal-Key', '')
        if not ADMIN_API_KEY or key != ADMIN_API_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def _format_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@discovery_monitor_bp.route('/api/admin/discovery/status', methods=['GET'])
@_require_admin
def discovery_scheduler_status():
    try:
        from jobs_api import _last_runs
    except ImportError:
        _last_runs = {}

    job_mapping = {
        'facility_discovery': {'job_key': 'discovery', 'interval': 21600},
        'news_sync': {'job_key': 'news-refresh', 'interval': 900},
        'global_intel': {'job_key': 'global-intel', 'interval': 21600},
        'ecosystem': {'job_key': 'ecosystem', 'interval': 86400},
        'evolution': {'job_key': 'evolution', 'interval': 86400},
        'outreach': {'job_key': 'outreach', 'interval': 86400},
        'promotion': {'job_key': 'promotion', 'interval': 86400},
        'content_publish': {'job_key': 'content-publish', 'interval': 86400},
        'ai_wars': {'job_key': 'ai-wars', 'interval': 86400},
    }

    schedulers = {}
    for name, cfg in job_mapping.items():
        run_info = _last_runs.get(cfg['job_key'], {})
        last_run = run_info.get('last_run')
        schedulers[name] = {
            'mode': 'external_cron',
            'last_run': last_run,
            'interval_seconds': cfg['interval'],
            'last_success': run_info.get('success'),
            'last_duration_seconds': run_info.get('duration_seconds'),
            'status': 'idle' if not run_info else ('ok' if run_info.get('success') else 'last_run_failed'),
        }

    bg_threads = {}
    for t in threading.enumerate():
        if t.daemon and t.name != 'MainThread':
            bg_threads[t.name] = {'alive': t.is_alive()}

    uptime_s = time.time() - _APP_START_TIME
    return jsonify({
        'schedulers': schedulers,
        'background_threads': bg_threads,
        'discovery_running': _discovery_running,
        'uptime': _format_uptime(uptime_s),
        'uptime_seconds': round(uptime_s),
        'server_time': datetime.now(timezone.utc).isoformat()
    })


@discovery_monitor_bp.route('/api/admin/discovery/queue', methods=['GET'])
@_require_admin
def discovery_queue():
    from db_utils import get_read_db
    db = None
    try:
        db = get_read_db()

        total_row = db.execute('SELECT COUNT(*) FROM discovered_facilities').fetchone()
        total = total_row[0] if total_row else 0

        status_rows = db.execute(
            "SELECT status, COUNT(*) as cnt FROM discovered_facilities GROUP BY status"
        ).fetchall()
        by_status = {}
        for row in status_rows:
            s = row[0] or 'unknown'
            by_status[s] = row[1]

        source_rows = db.execute("""
            SELECT source, status, COUNT(*) as cnt
            FROM discovered_facilities
            GROUP BY source, status
        """).fetchall()
        by_source = {}
        for row in source_rows:
            src = row[0] or 'unknown'
            status = row[1] or 'unknown'
            cnt = row[2]
            if src not in by_source:
                by_source[src] = {'total': 0}
            by_source[src]['total'] += cnt
            by_source[src][status] = cnt

        main_row = db.execute('SELECT COUNT(*) FROM facilities').fetchone()
        main_count = main_row[0] if main_row else 0

        return jsonify({
            'total': total,
            'by_status': by_status,
            'by_source': by_source,
            'main_facilities_count': main_count
        })
    except Exception as e:
        logger.error(f"Discovery queue error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if db:
            try: db.close()
            except: pass


@discovery_monitor_bp.route('/api/admin/discovery/recent', methods=['GET'])
@_require_admin
def discovery_recent():
    from db_utils import get_read_db
    limit = min(request.args.get('limit', 50, type=int), 200)
    db = None
    try:
        db = get_read_db()

        total_row = db.execute('SELECT COUNT(*) FROM discovered_facilities').fetchone()
        total = total_row[0] if total_row else 0

        rows = db.execute("""
            SELECT id, name, provider, city, country, source,
                   confidence_score, status, discovered_at, is_duplicate
            FROM discovered_facilities
            ORDER BY discovered_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        discoveries = []
        for r in rows:
            discoveries.append({
                'id': r[0],
                'name': r[1],
                'operator': r[2],
                'city': r[3],
                'country': r[4],
                'source': r[5],
                'confidence': r[6],
                'status': r[7],
                'discovered_at': r[8],
                'is_duplicate': bool(r[9]) if r[9] is not None else False
            })

        return jsonify({
            'discoveries': discoveries,
            'total_count': total,
            'returned': len(discoveries)
        })
    except Exception as e:
        logger.error(f"Discovery recent error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if db:
            try: db.close()
            except: pass


@discovery_monitor_bp.route('/api/admin/discovery/metrics', methods=['GET'])
@_require_admin
def discovery_metrics():
    from db_utils import get_read_db
    db = None
    try:
        db = get_read_db()

        now = datetime.now(timezone.utc)
        periods = {
            'last_24h': (now - timedelta(hours=24)).isoformat(),
            'last_7d': (now - timedelta(days=7)).isoformat(),
            'last_30d': (now - timedelta(days=30)).isoformat(),
        }

        approval_rates = {}
        for period_name, since in periods.items():
            rows = db.execute(
                "SELECT status, COUNT(*) as cnt FROM discovered_facilities WHERE discovered_at > ? GROUP BY status",
                (since,)
            ).fetchall()
            counts = {}
            total = 0
            for r in rows:
                s = r[0] or 'unknown'
                counts[s] = r[1]
                total += r[1]
            approved = counts.get('approved', 0)
            rate = round(approved / total, 2) if total > 0 else 0
            counts['rate'] = rate
            counts['total'] = total
            approval_rates[period_name] = counts

        growth = []
        try:
            growth_rows = db.execute("""
                SELECT recorded_date, facility_count
                FROM facility_count_history
                ORDER BY recorded_date DESC
                LIMIT 30
            """).fetchall()
            growth = [{'date': r[0], 'main_count': r[1]} for r in reversed(growth_rows)]
        except Exception:
            pass

        current_count = db.execute('SELECT COUNT(*) FROM facilities').fetchone()[0]
        today = now.strftime('%Y-%m-%d')
        if not growth or growth[-1]['date'] != today:
            growth.append({'date': today, 'main_count': current_count})

        return jsonify({
            'approval_rates': approval_rates,
            'growth': growth
        })
    except Exception as e:
        logger.error(f"Discovery metrics error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if db:
            try: db.close()
            except: pass


def init_facility_count_history():
    from db_utils import get_db
    db = None
    try:
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS facility_count_history (
                id SERIAL PRIMARY KEY,
                recorded_date TEXT NOT NULL UNIQUE,
                facility_count INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
        logger.info("✅ facility_count_history table initialized")
    except Exception as e:
        logger.warning(f"facility_count_history init: {e}")
    finally:
        if db:
            try: db.close()
            except: pass


def record_facility_count_snapshot():
    from db_utils import get_db
    db = None
    try:
        db = get_db()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        existing = db.execute(
            "SELECT 1 FROM facility_count_history WHERE recorded_date = ?", (today,)
        ).fetchone()

        if not existing:
            count = db.execute('SELECT COUNT(*) FROM facilities').fetchone()[0]
            db.execute(
                "INSERT INTO facility_count_history (recorded_date, facility_count) VALUES (?, ?)",
                (today, count)
            )
            db.commit()
            logger.info(f"Recorded facility count history: {today} = {count}")
            return {'recorded': True, 'date': today, 'count': count}
        return {'recorded': False, 'date': today, 'reason': 'already_recorded'}
    except Exception as e:
        logger.warning(f"facility_count_history update: {e}")
        try:
            if db: db.rollback()
        except: pass
        return {'recorded': False, 'error': str(e)}
    finally:
        if db:
            try: db.close()
            except: pass


@discovery_monitor_bp.route('/api/admin/discovery/trigger', methods=['POST'])
@_require_admin
def discovery_trigger():
    global _discovery_running

    with _discovery_lock:
        if _discovery_running:
            return jsonify({
                'status': 'already_running',
                'message': 'A discovery run is already in progress'
            })
        _discovery_running = True

    job_id = f"disc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    def _run_discovery():
        global _discovery_running
        try:
            from api_auto_discovery import _discovery_instance
            if _discovery_instance:
                _discovery_instance.run_discovery_cycle()
            else:
                logger.warning("Discovery trigger: no discovery instance available")
        except Exception as e:
            logger.error(f"Discovery trigger error: {e}")
        finally:
            _discovery_running = False

    threading.Thread(target=_run_discovery, daemon=True, name=f"DiscoveryTrigger-{job_id}").start()

    return jsonify({
        'status': 'triggered',
        'job_id': job_id,
        'message': 'Discovery run started in background',
        'estimated_duration': '5-15 minutes'
    })


@discovery_monitor_bp.route('/api/admin/discovery/ai-tracking', methods=['GET'])
@_require_admin
def discovery_ai_tracking():
    platforms_data = []

    try:
        from ai_tracking import AI_PLATFORMS, get_cumulative_totals, get_platform_chart_data
        cumulative = get_cumulative_totals()
        chart_data = get_platform_chart_data(7)

        platform_map = {}
        for entry in cumulative:
            p = entry.get('platform', '')
            if p in AI_PLATFORMS:
                platform_map[p] = {
                    'name': AI_PLATFORMS[p]['name'],
                    'hits_total': entry.get('total_requests', 0),
                    'last_seen': entry.get('last_seen'),
                    'hits_7d': chart_data.get(p, {}).get('requests_7d', 0),
                    'status': 'active' if entry.get('last_seen') else 'inactive'
                }

        for key, info in AI_PLATFORMS.items():
            if key not in platform_map:
                platform_map[key] = {
                    'name': info['name'],
                    'hits_total': 0,
                    'last_seen': None,
                    'hits_7d': 0,
                    'status': 'inactive'
                }

        platforms_data = sorted(platform_map.values(), key=lambda x: x['hits_total'], reverse=True)
    except Exception as e:
        logger.warning(f"AI tracking import error: {e}")

    mcp_info = {'total': 0, 'last_seen': None}
    try:
        from db_utils import get_read_db
        db = get_read_db()
        try:
            mcp_row = db.execute("SELECT COUNT(*) FROM mcp_tool_calls").fetchone()
            mcp_info['total'] = mcp_row[0] if mcp_row else 0
            last_row = db.execute("SELECT MAX(created_at) FROM mcp_tool_calls").fetchone()
            mcp_info['last_seen'] = last_row[0] if last_row and last_row[0] else None
        except Exception:
            pass
        finally:
            try: db.close()
            except: pass
    except Exception:
        pass

    return jsonify({
        'platforms': platforms_data,
        'mcp_connections': mcp_info,
        'total_platforms': len(platforms_data)
    })
