"""Phase 40/42 — complete observability blueprint. phase42_unstacked

Consolidates everything from Phases 22, 24, 25, 27, 38, 39 into one file:
  GET  /api/v1/observability/route-audit          — Flask url_map shadow detection
  GET  /api/v1/observability/drift                — rolling baselines + z-scores
  GET  /api/v1/observability/anomalies            — last 7 days of digest entries
  POST /api/v1/observability/snapshot             — record current metric values
  GET  /api/v1/observability/diag-routes          — full url_map dump
  POST /api/v1/observability/conversion-track     — record upgrade-URL click
  GET  /api/v1/observability/conversion-funnel    — signals/clicks/paid rollup
  GET  /api/v1/conversion/track                   — legacy alias (CF-blocked)
  GET  /api/v1/conversion/funnel                  — legacy alias (CF-blocked)
"""
from flask import Blueprint, jsonify, current_app, request
import datetime

observability_bp = Blueprint('observability', __name__)


CRITICAL_METRICS = [
    'total_substations', 'total_pipelines', 'total_power_plants',
    'total_fiber_routes', 'total_capacity_mw',
    'mcp_tool_calls_24h', 'mcp_conversions_24h', 'agent_requests_24h',
    'health_score', 'linkedin_impressions_24h',
    'pricing_page_views_24h', 'upgrade_signals_24h',
]


# ──────────────────────────────────────────────────────────────────────────
#  Phase 22 endpoints
# ──────────────────────────────────────────────────────────────────────────

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
        if not conn:
            return jsonify(out)
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
    """Persist current metric values to observability_metrics."""
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

        samples = {}
        for label, sql in [
            ('total_substations',    "SELECT COUNT(*) FROM substations"),
            ('total_pipelines',      "SELECT COUNT(*) FROM pipelines"),
            ('total_power_plants',   "SELECT COUNT(*) FROM power_plants"),
            ('total_fiber_routes',   "SELECT COUNT(*) FROM fiber_routes"),
            ('total_capacity_mw',    "SELECT COALESCE(SUM(capacity_mw),0) FROM power_plants"),
            ('mcp_tool_calls_24h',   "SELECT COUNT(*) FROM mcp_tool_calls WHERE called_at > NOW() - INTERVAL '24 hours'"),
            ('mcp_conversions_24h',  "SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '24 hours'"),
        ]:
            try:
                cur.execute(sql)
                samples[label] = int((cur.fetchone() or (0,))[0])
            except Exception:
                try: conn.rollback()
                except Exception: pass

        for k, v in samples.items():
            try:
                cur.execute(
                    "INSERT INTO observability_metrics (metric, value) VALUES (%s, %s)",
                    (k, float(v))
                )
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


# ──────────────────────────────────────────────────────────────────────────
#  Phase 27 — diag-routes diagnostic endpoint
# ──────────────────────────────────────────────────────────────────────────

@observability_bp.route('/api/v1/observability/diag-routes', methods=['GET'])
def diag_routes():
    """List every Flask route currently registered."""
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
    conv = [r for r in rules if 'conversion' in r['path']]
    return jsonify({
        'success': True,
        'data': {
            'total_routes': len(rules),
            'observability_routes': obs,
            'grid_routes_first_30': grid[:30],
            'conversion_routes': conv,
            'sample_first_60': rules[:60],
        }
    })


# ──────────────────────────────────────────────────────────────────────────
#  Phase 38 + 39 — conversion tracking
# ──────────────────────────────────────────────────────────────────────────

def _track_click_inner():
    """Record an attributed upgrade-URL click in mcp_conversion_clicks."""
    args = request.args if request.method == 'GET' else (request.get_json(silent=True) or request.form)
    tool = (args.get('tool') or 'unknown')[:64]
    calls = args.get('calls', '0')
    tier = (args.get('tier') or 'free')[:32]
    try: calls_int = int(calls)
    except (ValueError, TypeError): calls_int = 0

    out = {
        'success': True, 'tracked': True,
        'tool': tool, 'calls': calls_int, 'tier': tier,
        'tracked_at': datetime.datetime.utcnow().isoformat() + 'Z',
    }
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mcp_conversion_clicks (
                        id SERIAL PRIMARY KEY,
                        clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        tool_name TEXT,
                        prior_calls INTEGER,
                        tier_at_click TEXT,
                        user_agent TEXT,
                        referer TEXT
                    )
                """)
                conn.commit()
                cur.execute("""
                    INSERT INTO mcp_conversion_clicks
                        (tool_name, prior_calls, tier_at_click, user_agent, referer)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    tool, calls_int, tier,
                    (request.headers.get('User-Agent') or '')[:300],
                    (request.headers.get('Referer') or '')[:300],
                ))
                conn.commit()
            except Exception as _e:
                try: conn.rollback()
                except Exception: pass
                out['_db_error'] = type(_e).__name__
            try: conn.close()
            except Exception: pass
    except Exception as _e:
        out['_error'] = type(_e).__name__
    return jsonify(out)


@observability_bp.route('/api/v1/observability/conversion-track', methods=['POST', 'GET'])
def conversion_track():
    """Phase 39 — record an attributed upgrade-URL click.

    Canonical path: /api/v1/observability/conversion-track (CF-allowlisted)
    Legacy path:    /api/v1/conversion/track (CF-blocked, kept for fallback)
    """
    return _track_click_inner()


def _funnel_inner():
    """Return signals + clicks + paid rollup over last N days."""
    days = max(1, min(int(request.args.get('days', 30)), 90))
    out = {'success': True, 'days': days, 'data': {
        'signals': 0, 'clicks': 0, 'paid': 0,
        'click_through_rate': 0.0, 'conversion_rate': 0.0,
    }}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()
            for label, sql in [
                ('signals', f"SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '{days} days'"),
                ('clicks',  f"SELECT COUNT(*) FROM mcp_conversion_clicks WHERE clicked_at > NOW() - INTERVAL '{days} days'"),
                ('paid',    f"SELECT COUNT(*) FROM mcp_conversions WHERE stage = 'paid' AND created_at > NOW() - INTERVAL '{days} days'"),
            ]:
                try:
                    cur.execute(sql)
                    out['data'][label] = int((cur.fetchone() or (0,))[0])
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    out['data'][label] = -1
            try: conn.close()
            except Exception: pass

            sig = out['data']['signals'] or 0
            clk = out['data']['clicks'] or 0
            pad = out['data']['paid'] or 0
            if sig > 0:
                out['data']['click_through_rate'] = round(clk / sig * 100, 2)
            if clk > 0:
                out['data']['conversion_rate'] = round(pad / clk * 100, 2)
    except Exception as _e:
        out['_error'] = type(_e).__name__
    return jsonify(out)


@observability_bp.route('/api/v1/observability/conversion-funnel', methods=['GET'])
def conversion_funnel():
    """Phase 39 — funnel rollup endpoint.

    Canonical: /api/v1/observability/conversion-funnel (CF-allowlisted)
    Legacy:    /api/v1/conversion/funnel (CF-blocked)
    """
    return _funnel_inner()
