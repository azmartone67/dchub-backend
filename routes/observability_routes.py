"""Phase 45 — observability blueprint with click tracking on /snapshot.

After Phase 44 emergency restore brought back the Phase 22 version of this
file (which had POST-only /snapshot and no event branches), Phase 45
re-overlays the Phase 43 click tracking + funnel logic.

  GET  /api/v1/observability/route-audit              — Flask url_map shadow detection
  GET  /api/v1/observability/route-audit?event=funnel — funnel rollup (NEW BRANCH)
  GET  /api/v1/observability/drift                    — rolling baselines
  GET  /api/v1/observability/anomalies                — last 7 days digest
  POST /api/v1/observability/snapshot                 — record metric values
  POST /api/v1/observability/snapshot?event=click     — record upgrade-URL click
  GET  /api/v1/observability/snapshot?event=click     — same, GET-friendly for img-pixel calls
  GET  /api/v1/observability/diag-routes              — full url_map dump
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


def _record_click():
    """Phase 55 — record an attributed upgrade-URL click via NEON direct.
    Matches Phase 54 funnel reader's connection so both endpoints hit
    the same database."""
    import os
    args = request.args if request.method == 'GET' else (request.get_json(silent=True) or request.form or request.args)
    tool = (args.get('tool') or 'unknown')[:64]
    calls = args.get('calls', '0')
    tier = (args.get('tier') or 'free')[:32]
    try: calls_int = int(calls)
    except (ValueError, TypeError): calls_int = 0

    out = {
        'success': True, 'event': 'click', 'tracked': False,  # set true only on real DB success
        'tool': tool, 'calls': calls_int, 'tier': tier,
        'tracked_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'phase55_neon_click': True,
    }

    NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not NEON_URL:
        out['_error'] = 'NEON_DATABASE_URL not set'
        return jsonify(out)

    try:
        try:
            import psycopg
            _conn = psycopg.connect(NEON_URL, autocommit=True)
        except ImportError:
            import psycopg2 as psycopg
            _conn = psycopg.connect(NEON_URL)
            _conn.autocommit = True
    except Exception as _e:
        out['_error'] = f'connect failed: {type(_e).__name__}'
        return jsonify(out)

    try:
        cur = _conn.cursor()
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
        cur.execute("""
            INSERT INTO mcp_conversion_clicks
                (tool_name, prior_calls, tier_at_click, user_agent, referer)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            tool, calls_int, tier,
            (request.headers.get('User-Agent') or '')[:300],
            (request.headers.get('Referer') or '')[:300],
        ))
        out['tracked'] = True  # only flip on real success
        try:
            cur.execute("SELECT COUNT(*) FROM mcp_conversion_clicks")
            out['total_clicks_recorded'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            pass
        try: _conn.close()
        except Exception: pass
    except Exception as _e:
        out['_db_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
        try: _conn.close()
        except Exception: pass

    return jsonify(out)



def _funnel_rollup():
    """Phase 54 funnel rollup — uses NEON_DATABASE_URL directly to match the
    working /api/v1/mcp/funnel widget. Reads from mcp_upgrade_signals +
    mcp_conversions + mcp_conversion_clicks."""
    import os
    days = max(1, min(int(request.args.get('days', 30)), 90))
    out = {'success': True, 'event': 'funnel', 'days': days, 'data': {
        'signals': 0, 'clicks': 0, 'paid': 0,
        'click_through_rate': 0.0, 'conversion_rate': 0.0,
        'phase54_neon_direct': True,
    }}

    NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not NEON_URL:
        out['_error'] = 'NEON_DATABASE_URL not set'
        return jsonify(out)

    try:
        try:
            import psycopg
            _conn = psycopg.connect(NEON_URL, autocommit=True)
        except ImportError:
            import psycopg2 as psycopg
            _conn = psycopg.connect(NEON_URL)
            _conn.autocommit = True
    except Exception as _e:
        out['_error'] = f'connect failed: {type(_e).__name__}'
        return jsonify(out)

    try:
        cur = _conn.cursor()

        # Signals — try mcp_upgrade_signals first (the actual table the
        # dashboard widget uses).
        for sql in [
            f"SELECT COUNT(*) FROM mcp_upgrade_signals WHERE created_at > NOW() - INTERVAL '{days} days'",
            f"SELECT COUNT(*) FROM mcp_signals WHERE created_at > NOW() - INTERVAL '{days} days'",
        ]:
            try:
                cur.execute(sql)
                n = int((cur.fetchone() or (0,))[0])
                if n > 0:
                    out['data']['signals'] = n
                    out['data']['signals_source'] = sql.split('FROM ')[1].split(' ')[0]
                    break
            except Exception:
                try: _conn.rollback()
                except Exception: pass

        # Clicks — try mcp_conversion_clicks
        try:
            cur.execute(f"SELECT COUNT(*) FROM mcp_conversion_clicks WHERE clicked_at > NOW() - INTERVAL '{days} days'")
            out['data']['clicks'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: _conn.rollback()
            except Exception: pass
            out['data']['clicks'] = -1

        # Paid — mcp_conversions stage='paid'
        try:
            cur.execute(f"SELECT COUNT(*) FROM mcp_conversions WHERE stage = 'paid' AND created_at > NOW() - INTERVAL '{days} days'")
            out['data']['paid'] = int((cur.fetchone() or (0,))[0])
        except Exception:
            try: _conn.rollback()
            except Exception: pass
            try:
                # Fallback: just count any mcp_conversions
                cur.execute(f"SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '{days} days'")
                out['data']['paid'] = int((cur.fetchone() or (0,))[0])
            except Exception:
                try: _conn.rollback()
                except Exception: pass
                out['data']['paid'] = -1

        try: _conn.close()
        except Exception: pass

        sig = out['data']['signals'] or 0
        clk = max(0, out['data']['clicks']) or 0
        pad = max(0, out['data']['paid']) or 0
        if sig > 0:
            out['data']['click_through_rate'] = round(clk / sig * 100, 2)
        if clk > 0:
            out['data']['conversion_rate'] = round(pad / clk * 100, 2)
    except Exception as _e:
        out['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]

    return jsonify(out)

@observability_bp.route('/api/v1/observability/route-audit', methods=['GET'])
def route_audit():
    """Inventory routes. Branches:
       ?event=funnel&days=N  → funnel rollup (signals + clicks + paid)
    """
    event = (request.args.get('event') or '').lower()
    if event == 'funnel':
        return _funnel_rollup()

    seen = {}
    shadows = []
    for rule in current_app.url_map.iter_rules():
        path = str(rule)
        endpoint = rule.endpoint
        methods = sorted(rule.methods - {'HEAD', 'OPTIONS'}) if rule.methods else []
        key = (path, tuple(methods))
        if key in seen:
            shadows.append({'path': path, 'methods': list(methods),
                          'endpoints': [seen[key], endpoint]})
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


@observability_bp.route('/api/v1/observability/snapshot', methods=['POST', 'GET'])
def snapshot():
    """Snapshot metrics. Branches:
       ?event=click&tool=X&calls=N&tier=T → record upgrade-URL click
    """
    event = (request.args.get('event') or '').lower()
    if event == 'click':
        return _record_click()

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


@observability_bp.route('/api/v1/observability/drift', methods=['GET'])
def drift():
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
        try: conn.commit()
        except Exception: pass

        for m in CRITICAL_METRICS:
            try:
                cur.execute("""
                    SELECT COALESCE(AVG(value), 0), COALESCE(STDDEV_SAMP(value), 0), COUNT(*)
                    FROM observability_metrics
                    WHERE metric = %s AND recorded_at > NOW() - INTERVAL '7 days'
                """, (m,))
                r = cur.fetchone() or (0, 0, 0)
                cur.execute("""
                    SELECT value, recorded_at FROM observability_metrics
                    WHERE metric = %s ORDER BY recorded_at DESC LIMIT 1
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
                    'metric': m, 'current': cur_v, 'baseline_7d': baseline,
                    'sigma': sigma, 'samples': samples, 'z_score': drift_z, 'drift': drift_flag,
                })
            except Exception:
                try: conn.rollback()
                except Exception: pass
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
            ORDER BY detected_at DESC LIMIT 50
        """)
        for r in cur.fetchall():
            out['data']['anomalies'].append({
                'id': r[0], 'detected_at': str(r[1]),
                'severity': r[2], 'summary': r[3],
                'details': r[4] if r[4] else {},
            })
        try: conn.close()
        except Exception: pass
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    return jsonify(out)


@observability_bp.route('/api/v1/observability/diag-routes', methods=['GET'])
def diag_routes():
    rules = []
    for r in current_app.url_map.iter_rules():
        rules.append({
            'path': str(r), 'endpoint': r.endpoint,
            'methods': sorted((r.methods or set()) - {'HEAD','OPTIONS'}),
        })
    rules.sort(key=lambda x: x['path'])
    obs = [r for r in rules if 'observability' in r['path']]
    return jsonify({
        'success': True,
        'data': {
            'total_routes': len(rules),
            'observability_routes': obs,
            'sample_first_60': rules[:60],
        }
    })


# ----------------------------------------------------------------------------
# Phase 60 / 60d -- phase60d_defensive
# Top users by upgrade-signal count, with full exception surfacing.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/observability/top-users', methods=['GET'])
def phase60_top_users():
    """Top users by upgrade-signal count, defensive version."""
    import os, csv, io, traceback
    from flask import request, jsonify, Response

    debug_steps = []

    def _step(msg):
        debug_steps.append(msg)

    try:
        _step("entered handler")

        # Optional gate
        admin_token = os.environ.get('TOP_USERS_TOKEN')
        if admin_token:
            provided = request.headers.get('X-Admin-Token') or request.args.get('token')
            if provided != admin_token:
                return jsonify({'error': 'unauthorized'}), 401

        try:
            limit = int(request.args.get('limit', '50'))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))
        fmt = (request.args.get('format') or 'json').lower()
        debug = request.args.get('debug') == '1'
        _step(f"params parsed: limit={limit} fmt={fmt}")

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'NEON_DATABASE_URL/DATABASE_URL not set', 'phase': '60d'}), 500
        _step(f"neon url present: {bool(neon)}")

        # Try psycopg first, then psycopg2 (Railway typically has psycopg2)
        conn = None
        connector = None
        last_err = None
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                conn = mod.connect(neon)
                connector = modname
                break
            except Exception as e:
                last_err = f"{modname}: {type(e).__name__}: {e}"
                continue
        if not conn:
            return jsonify({
                'error': 'no postgres driver could connect',
                'last_error': last_err,
                'phase': '60d',
            }), 500
        _step(f"connected via {connector}")

        try:
            cur = conn.cursor()

            # Look for the signals table -- might be named differently in prod
            cur.execute(
                "SELECT table_schema, table_name "
                "FROM information_schema.tables "
                "WHERE table_name ILIKE %s OR table_name ILIKE %s "
                "ORDER BY table_schema, table_name",
                ('%upgrade_signal%', '%mcp_signal%')
            )
            tables = cur.fetchall()
            _step(f"signals-like tables found: {[(r[0], r[1]) for r in tables]}")

            if not tables:
                return jsonify({
                    'error': 'no signals table found',
                    'phase': '60d',
                    'debug_steps': debug_steps,
                    'searched_for': ['%upgrade_signal%', '%mcp_signal%'],
                }), 500

            # Pick the first one
            schema, table = tables[0][0], tables[0][1]
            qualified = f'"{schema}"."{table}"' if schema and schema != 'public' else f'"{table}"'
            _step(f"using table: {qualified}")

            # Discover columns
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = %s AND table_schema = %s "
                "ORDER BY ordinal_position",
                (table, schema)
            )
            col_rows = cur.fetchall()
            cols = [r[0] for r in col_rows]
            _step(f"columns: {cols}")

            user_candidates = ['user_id', 'user_identifier', 'email', 'user_email',
                               'api_key', 'client_id', 'session_id', 'ip', 'ip_address',
                               'remote_addr', 'caller_id', 'user']
            user_col = next((c for c in user_candidates if c in cols), None)
            tool_col = next((c for c in ['tool_name', 'tool', 'mcp_tool', 'function_name'] if c in cols), None)
            time_col = next((c for c in ['created_at', 'timestamp', 'ts', 'inserted_at', 'occurred_at'] if c in cols), None)
            _step(f"chose: user={user_col} tool={tool_col} time={time_col}")

            if not user_col or not tool_col or not time_col:
                return jsonify({
                    'error': 'could not identify required columns',
                    'columns_found': col_rows,
                    'user_col': user_col,
                    'tool_col': tool_col,
                    'time_col': time_col,
                    'phase': '60d',
                    'debug_steps': debug_steps,
                }), 500

            # Aggregate
            sql = (
                "SELECT "
                "  {u} AS user_id, "
                "  COUNT(*) AS signal_count, "
                "  COUNT(DISTINCT {t}) AS distinct_tools, "
                "  STRING_AGG(DISTINCT {t}::text, ',' ORDER BY {t}::text) AS tools_csv, "
                "  MIN({ts}) AS first_seen, "
                "  MAX({ts}) AS last_seen "
                "FROM {tbl} "
                "WHERE {u} IS NOT NULL "
                "GROUP BY {u} "
                "ORDER BY signal_count DESC "
                "LIMIT %s"
            ).format(u=user_col, t=tool_col, ts=time_col, tbl=qualified)

            cur.execute(sql, (limit,))
            rows = cur.fetchall()
            _step(f"top-N rows: {len(rows)}")

            result = []
            for r in rows:
                tools_csv = r[3] or ''
                tools_list = [s.strip() for s in tools_csv.split(',') if s.strip()] if tools_csv else []
                result.append({
                    'user_id': str(r[0]) if r[0] is not None else None,
                    'signal_count': int(r[1] or 0),
                    'distinct_tools': int(r[2] or 0),
                    'tools_tried': tools_list,
                    'first_seen': r[4].isoformat() if r[4] else None,
                    'last_seen': r[5].isoformat() if r[5] else None,
                })

            cur.execute(
                ("SELECT COUNT(*), COUNT(DISTINCT {u}) "
                 "FROM {tbl} WHERE {u} IS NOT NULL").format(u=user_col, tbl=qualified)
            )
            agg = cur.fetchone()
            total_signals = int(agg[0] or 0)
            distinct_users = int(agg[1] or 0)
            _step("aggregates done")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if fmt == 'csv':
            sio = io.StringIO()
            writer = csv.writer(sio)
            writer.writerow(['user_id', 'signal_count', 'distinct_tools',
                             'tools_tried', 'first_seen', 'last_seen'])
            for u in result:
                writer.writerow([
                    u['user_id'], u['signal_count'], u['distinct_tools'],
                    '|'.join(u['tools_tried']),
                    u['first_seen'], u['last_seen']
                ])
            return Response(sio.getvalue(), mimetype='text/csv', headers={
                'Content-Disposition': 'attachment; filename="dchub-top-users.csv"'
            })

        payload = {
            'phase': '60d',
            'connector': connector,
            'table': qualified,
            'user_column': user_col,
            'tool_column': tool_col,
            'time_column': time_col,
            'distinct_users': distinct_users,
            'total_signals': total_signals,
            'count': len(result),
            'limit': limit,
            'top_users': result,
        }
        if debug:
            payload['debug_steps'] = debug_steps
        return jsonify(payload)

    except Exception as e:
        return jsonify({
            'error': 'unhandled exception',
            'type': type(e).__name__,
            'message': str(e),
            'traceback': traceback.format_exc(),
            'debug_steps': debug_steps,
            'phase': '60d',
        }), 500
