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
# Phase 60 / 60e -- phase60e_outreach_workbench
# Top users by upgrade-signal count, schema-aware, with outreach metadata.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/observability/top-users', methods=['GET'])
def phase60_top_users():
    """Top users by upgrade-signal count. Outreach-campaign workbench.

    Query params:
      limit               int, default 50, max 500
      format              json (default) or csv
      include_converted   1 to include already-converted users
      include_contacted   1 to include users where outreach_sent=true
      tier                filter to specific tier_required (Pro, Enterprise)
      mcp_client          filter to specific AI client
      token               required if TOP_USERS_TOKEN env is set
    """
    import os, csv, io, traceback
    from flask import request, jsonify, Response

    debug_steps = []
    def _step(msg): debug_steps.append(msg)

    try:
        _step("entered handler")

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
        include_converted = request.args.get('include_converted') == '1'
        include_contacted = request.args.get('include_contacted') == '1'
        tier_filter = request.args.get('tier')
        client_filter = request.args.get('mcp_client')

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'no DB url configured', 'phase': '60e'}), 500

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
            return jsonify({'error': 'no postgres driver', 'last_error': last_err, 'phase': '60e'}), 500
        _step(f"connected via {connector}")

        try:
            cur = conn.cursor()

            # Build the WHERE clause for filters
            where_clauses = ["user_email IS NOT NULL"]
            sql_args = []
            if not include_converted:
                where_clauses.append("(converted IS NULL OR converted = false)")
            if not include_contacted:
                where_clauses.append("(outreach_sent IS NULL OR outreach_sent = false)")
            if tier_filter:
                where_clauses.append("tier_required = %s")
                sql_args.append(tier_filter)
            if client_filter:
                where_clauses.append("mcp_client = %s")
                sql_args.append(client_filter)
            where_sql = " AND ".join(where_clauses)

            # Per-user leaderboard
            sql = (
                "SELECT "
                "  user_email, "
                "  COUNT(*) AS signal_count, "
                "  COUNT(DISTINCT tool_requested) AS distinct_tools, "
                "  STRING_AGG(DISTINCT tool_requested::text, ',' ORDER BY tool_requested::text) AS tools_csv, "
                "  STRING_AGG(DISTINCT COALESCE(mcp_client, 'unknown')::text, ',') AS clients_csv, "
                "  STRING_AGG(DISTINCT COALESCE(tier_required, '')::text, ',') AS tiers_csv, "
                "  BOOL_OR(COALESCE(converted, false)) AS has_converted, "
                "  MAX(converted_at) AS converted_at, "
                "  BOOL_OR(COALESCE(outreach_sent, false)) AS outreach_done, "
                "  MAX(outreach_sent_at) AS outreach_sent_at, "
                "  MIN(created_at) AS first_seen, "
                "  MAX(created_at) AS last_seen "
                "FROM mcp_upgrade_signals "
                "WHERE " + where_sql + " "
                "GROUP BY user_email "
                "ORDER BY signal_count DESC "
                "LIMIT %s"
            )
            cur.execute(sql, tuple(sql_args) + (limit,))
            rows = cur.fetchall()
            _step(f"top-N rows: {len(rows)}")

            top_users = []
            for r in rows:
                tools_csv = r[3] or ''
                clients_csv = r[4] or ''
                tiers_csv = r[5] or ''
                top_users.append({
                    'email': r[0],
                    'signal_count': int(r[1] or 0),
                    'distinct_tools': int(r[2] or 0),
                    'tools_tried': [s.strip() for s in tools_csv.split(',') if s.strip()],
                    'mcp_clients': [s.strip() for s in clients_csv.split(',') if s.strip()],
                    'tiers_required': [s.strip() for s in tiers_csv.split(',') if s.strip()],
                    'converted': bool(r[6]),
                    'converted_at': r[7].isoformat() if r[7] else None,
                    'outreach_sent': bool(r[8]),
                    'outreach_sent_at': r[9].isoformat() if r[9] else None,
                    'first_seen': r[10].isoformat() if r[10] else None,
                    'last_seen': r[11].isoformat() if r[11] else None,
                })

            # Top-level totals (across the WHOLE signals table, no filter)
            cur.execute("SELECT COUNT(*) FROM mcp_upgrade_signals")
            total_signals = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(DISTINCT user_email) FROM mcp_upgrade_signals WHERE user_email IS NOT NULL")
            total_distinct_users = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT COUNT(DISTINCT user_email) FROM mcp_upgrade_signals "
                "WHERE user_email IS NOT NULL AND COALESCE(converted, false) = true"
            )
            converted_users = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT COUNT(DISTINCT user_email) FROM mcp_upgrade_signals "
                "WHERE user_email IS NOT NULL AND COALESCE(outreach_sent, false) = true"
            )
            contacted_users = int(cur.fetchone()[0] or 0)

            # Breakdown by mcp_client
            cur.execute(
                "SELECT COALESCE(mcp_client, 'unknown') AS c, COUNT(*) AS n, "
                "COUNT(DISTINCT user_email) AS users "
                "FROM mcp_upgrade_signals "
                "GROUP BY c "
                "ORDER BY n DESC "
                "LIMIT 20"
            )
            by_client = [{'mcp_client': r[0], 'signals': int(r[1]), 'users': int(r[2])} for r in cur.fetchall()]

            # Breakdown by tier_required
            cur.execute(
                "SELECT COALESCE(tier_required, 'unknown') AS t, COUNT(*) AS n, "
                "COUNT(DISTINCT user_email) AS users "
                "FROM mcp_upgrade_signals "
                "GROUP BY t "
                "ORDER BY n DESC"
            )
            by_tier = [{'tier_required': r[0], 'signals': int(r[1]), 'users': int(r[2])} for r in cur.fetchall()]

            # Breakdown by tool_requested
            cur.execute(
                "SELECT COALESCE(tool_requested, 'unknown') AS tr, COUNT(*) AS n, "
                "COUNT(DISTINCT user_email) AS users "
                "FROM mcp_upgrade_signals "
                "GROUP BY tr "
                "ORDER BY n DESC "
                "LIMIT 20"
            )
            by_tool = [{'tool_requested': r[0], 'signals': int(r[1]), 'users': int(r[2])} for r in cur.fetchall()]
            _step("aggregates done")
        finally:
            try: conn.close()
            except Exception: pass

        if fmt == 'csv':
            sio = io.StringIO()
            writer = csv.writer(sio)
            writer.writerow([
                'email', 'signal_count', 'distinct_tools', 'tools_tried',
                'mcp_clients', 'tiers_required', 'converted', 'converted_at',
                'outreach_sent', 'outreach_sent_at', 'first_seen', 'last_seen'
            ])
            for u in top_users:
                writer.writerow([
                    u['email'], u['signal_count'], u['distinct_tools'],
                    '|'.join(u['tools_tried']),
                    '|'.join(u['mcp_clients']),
                    '|'.join(u['tiers_required']),
                    u['converted'], u['converted_at'],
                    u['outreach_sent'], u['outreach_sent_at'],
                    u['first_seen'], u['last_seen']
                ])
            return Response(sio.getvalue(), mimetype='text/csv', headers={
                'Content-Disposition': 'attachment; filename="dchub-top-users.csv"'
            })

        payload = {
            'phase': '60e',
            'connector': connector,
            'count': len(top_users),
            'limit': limit,
            'filters': {
                'include_converted': include_converted,
                'include_contacted': include_contacted,
                'tier': tier_filter,
                'mcp_client': client_filter,
            },
            'totals': {
                'total_signals': total_signals,
                'distinct_users_with_email': total_distinct_users,
                'converted_users': converted_users,
                'contacted_users': contacted_users,
                'conversion_rate_pct': round(100.0 * converted_users / total_distinct_users, 2) if total_distinct_users else 0,
            },
            'by_mcp_client': by_client,
            'by_tier_required': by_tier,
            'by_tool_requested': by_tool,
            'top_users': top_users,
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
            'phase': '60e',
        }), 500
