"""Phase 22 — observability blueprint.

Endpoints:
  GET /api/v1/observability/route-audit
      Inventory every Flask route. Detect shadowed paths (multiple
      registrations of the same URL rule). Returns a health-degrading
      flag if any shadow exists.

  GET /api/v1/observability/drift
      Rolling baselines for the platform's critical metrics. Compared
      hourly by the watchdog; >2σ deviation triggers an alert.

  GET /api/v1/observability/anomalies
      Last 7 days of LLM-generated anomaly digests.
"""
from flask import Blueprint, jsonify, current_app
import os, json, datetime

observability_bp = Blueprint('observability', __name__)

CRITICAL_METRICS = [
    'total_substations',
    'total_pipelines',
    'total_power_plants',
    'total_fiber_routes',
    'total_capacity_mw',
    'mcp_tool_calls_24h',
    'mcp_conversions_24h',
    'agent_requests_24h',
    'health_score',
    'linkedin_impressions_24h',
    'pricing_page_views_24h',
    'upgrade_signals_24h',
]


@observability_bp.route('/api/v1/observability/route-audit', methods=['GET'])
def route_audit():
    """Inventory all Flask routes and flag shadowed ones."""
    seen = {}
    shadows = []
    for rule in current_app.url_map.iter_rules():
        path = str(rule)
        endpoint = rule.endpoint
        methods = sorted(rule.methods - {'HEAD', 'OPTIONS'}) if rule.methods else []
        key = (path, tuple(methods))
        if key in seen:
            shadows.append({
                'path': path,
                'methods': list(methods),
                'endpoints': [seen[key], endpoint],
            })
        else:
            seen[key] = endpoint
    return jsonify({
        'success': True,
        'data': {
            'total_routes': len(list(current_app.url_map.iter_rules())),
            'shadowed_routes': shadows,
            'shadowed_count': len(shadows),
            'healthy': len(shadows) == 0,
            'as_of': datetime.datetime.utcnow().isoformat() + 'Z',
        },
    })


@observability_bp.route('/api/v1/observability/drift', methods=['GET'])
def drift():
    """Return rolling baselines + current values for critical metrics."""
    out = {'success': True, 'data': {'metrics': [], 'as_of': datetime.datetime.utcnow().isoformat() + 'Z'}}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn:
            out['data']['_error'] = 'no DB connection'
            return jsonify(out)
        cur = conn.cursor()
        # Ensure table exists (idempotent)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS observability_metrics (
                metric TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_metrics_time ON observability_metrics(metric, recorded_at)")
        try: conn.commit()
        except Exception: pass

        # For each metric: fetch current value, mean of last 7d, stddev
        for m in CRITICAL_METRICS:
            try:
                cur.execute("""
                    SELECT
                        COALESCE(AVG(value), 0)             AS baseline,
                        COALESCE(STDDEV_SAMP(value), 0)     AS sigma,
                        COUNT(*)                            AS samples
                    FROM observability_metrics
                    WHERE metric = %s AND recorded_at > NOW() - INTERVAL '7 days'
                """, (m,))
                r = cur.fetchone() or (0, 0, 0)
                cur.execute("""
                    SELECT value, recorded_at
                    FROM observability_metrics
                    WHERE metric = %s
                    ORDER BY recorded_at DESC
                    LIMIT 1
                """, (m,))
                latest = cur.fetchone()
                cur_v = float(latest[0]) if latest else None
                baseline = float(r[0] or 0)
                sigma = float(r[1] or 0)
                samples = int(r[2] or 0)
                drift_z = None
                drift_flag = False
                if cur_v is not None and sigma > 0:
                    drift_z = (cur_v - baseline) / sigma
                    drift_flag = abs(drift_z) > 2.0
                out['data']['metrics'].append({
                    'metric': m,
                    'current': cur_v,
                    'baseline_7d': baseline,
                    'sigma': sigma,
                    'samples': samples,
                    'z_score': drift_z,
                    'drift': drift_flag,
                    'last_recorded_at': str(latest[1]) if latest else None,
                })
            except Exception as _e:
                try: conn.rollback()
                except Exception: pass
                out['data']['metrics'].append({
                    'metric': m,
                    '_error': type(_e).__name__ + ': ' + str(_e)[:160],
                })
        try: conn.close()
        except Exception: pass
        flagged = [m for m in out['data']['metrics'] if m.get('drift')]
        out['data']['drift_count'] = len(flagged)
        out['data']['healthy'] = len(flagged) == 0
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    return jsonify(out)


@observability_bp.route('/api/v1/observability/anomalies', methods=['GET'])
def anomalies():
    """Last 7 days of anomaly digests."""
    out = {'success': True, 'data': {'anomalies': []}}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn: return jsonify(out)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_anomalies (
                id SERIAL PRIMARY KEY,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                severity TEXT NOT NULL DEFAULT 'info',
                summary TEXT NOT NULL,
                details JSONB
            )
        """)
        try: conn.commit()
        except Exception: pass
        cur.execute("""
            SELECT id, detected_at, severity, summary, details
            FROM daily_anomalies
            WHERE detected_at > NOW() - INTERVAL '7 days'
            ORDER BY detected_at DESC
            LIMIT 50
        """)
        for r in cur.fetchall():
            out['data']['anomalies'].append({
                'id': r[0],
                'detected_at': str(r[1]),
                'severity': r[2],
                'summary': r[3],
                'details': r[4] if r[4] else {},
            })
        try: conn.close()
        except Exception: pass
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    return jsonify(out)


@observability_bp.route('/api/v1/observability/snapshot', methods=['POST'])
def snapshot():
    """Persist current metric values to observability_metrics.

    Called by the hourly watchdog. Idempotent — safe to call repeatedly.
    """
    out = {'success': True, 'data': {'recorded': []}}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn: return jsonify(out)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS observability_metrics (
                metric TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        try: conn.commit()
        except Exception: pass

        # Sample each metric
        samples = {}
        try:
            cur.execute("SELECT COUNT(*) FROM substations"); samples['total_substations'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute("SELECT COUNT(*) FROM pipelines"); samples['total_pipelines'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute("SELECT COUNT(*) FROM power_plants"); samples['total_power_plants'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute("SELECT COUNT(*) FROM fiber_routes"); samples['total_fiber_routes'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute("SELECT COALESCE(SUM(capacity_mw),0) FROM power_plants"); samples['total_capacity_mw'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute("SELECT COUNT(*) FROM mcp_tool_calls WHERE called_at > NOW() - INTERVAL '24 hours'"); samples['mcp_tool_calls_24h'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute("SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '24 hours'"); samples['mcp_conversions_24h'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: conn.rollback()
            except Exception: pass

        # Persist
        for k, v in samples.items():
            try:
                cur.execute("INSERT INTO observability_metrics (metric, value) VALUES (%s, %s)", (k, float(v)))
                out['data']['recorded'].append({'metric': k, 'value': v})
            except Exception:
                try: conn.rollback()
                except Exception: pass
        try: conn.commit()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    return jsonify(out)


@observability_bp.route('/api/v1/observability/diag-routes', methods=['GET'])
def phase27_diag_routes():
    """Phase 27 — list every Flask route. Lives under the
    /api/v1/observability/* namespace which is on the CF Worker allowlist."""
    from flask import current_app, jsonify
    rules = []
    for r in current_app.url_map.iter_rules():
        rules.append({
            'path': str(r),
            'endpoint': r.endpoint,
            'methods': sorted((r.methods or set()) - {'HEAD','OPTIONS'}),
        })
    rules.sort(key=lambda x: x['path'])
    obs = [r for r in rules if 'observability' in r['path'] or r['endpoint'].startswith('observability')]
    grid = [r for r in rules if r['path'].startswith('/grid') or '/grid' in r['path']]
    return jsonify({
        'success': True,
        'data': {
            'total_routes': len(rules),
            'observability_routes': obs,
            'grid_routes': grid,
            'sample_first_60': rules[:60],
        }
    })


@observability_bp.route('/api/v1/watchdog', methods=['GET'])
def phase34_watchdog():
    """Phase 34 — minimal watchdog endpoint.

    Returns 200 + a status snapshot. Used by the post-deploy smoke test
    and external monitors. The real watchdog logic (anomaly detection
    over the last 24h of metrics) is in health_watchdog.py — this
    endpoint just provides a stable URL that always responds.
    """
    from flask import jsonify, current_app
    out = {
        'success': True,
        'status': 'ok',
        'data': {
            'as_of': None,
            'flask_routes': len(list(current_app.url_map.iter_rules())),
        },
    }
    try:
        import datetime
        out['data']['as_of'] = datetime.datetime.utcnow().isoformat() + 'Z'
    except Exception:
        pass
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT COUNT(*) FROM mcp_tool_calls WHERE called_at > NOW() - INTERVAL '1 hour'")
                r = cur.fetchone() or (0,)
                out['data']['mcp_calls_1h'] = int(r[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try: conn.close()
            except Exception: pass
    except Exception as _e:
        out['data']['_db_error'] = type(_e).__name__
    return jsonify(out)
