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
# Phase 60 / 61.A -- phase61_top_users_pivot
# Top-users with group_by + optional reverse-DNS enrichment.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/observability/top-users', methods=['GET'])
def phase60_top_users():
    """Top users by upgrade-signal count, with multiple grouping keys.

    Query params:
      group_by            email (default), ip, user_agent, session_id, mcp_client
      reverse_dns         1 to enrich IP rows with reverse DNS + provider guess
      limit               int, default 50, max 1000
      format              json (default) or csv
      include_converted   1 to include already-converted groups
      include_contacted   1 to include groups already contacted
      tier                filter to specific tier_required
      mcp_client          filter to specific AI client
      token               required if TOP_USERS_TOKEN env is set
    """
    import os, csv, io, traceback, socket
    from concurrent.futures import ThreadPoolExecutor
    from flask import request, jsonify, Response

    GROUP_BY_MAP = {
        'email': 'user_email',
        'ip': 'ip_address',
        'session_id': 'session_id',
        'session': 'session_id',
        'user_agent': 'user_agent',
        'agent': 'user_agent',
        'mcp_client': 'mcp_client',
        'client': 'mcp_client',
    }

    debug_steps = []
    def _step(msg): debug_steps.append(msg)

    try:
        _step("entered handler")

        admin_token = os.environ.get('TOP_USERS_TOKEN')
        if admin_token:
            provided = request.headers.get('X-Admin-Token') or request.args.get('token')
            if provided != admin_token:
                return jsonify({'error': 'unauthorized'}), 401

        group_by = (request.args.get('group_by') or 'email').lower()
        group_col = GROUP_BY_MAP.get(group_by, 'user_email')
        try:
            limit = int(request.args.get('limit', '50'))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 1000))
        fmt = (request.args.get('format') or 'json').lower()
        debug = request.args.get('debug') == '1'
        do_rdns = request.args.get('reverse_dns') == '1'
        include_converted = request.args.get('include_converted') == '1'
        include_contacted = request.args.get('include_contacted') == '1'
        tier_filter = request.args.get('tier')
        client_filter = request.args.get('mcp_client')

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'no DB url configured', 'phase': '61'}), 500

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
            return jsonify({'error': 'no postgres driver', 'last_error': last_err, 'phase': '61'}), 500
        _step(f"connected via {connector}; group_by={group_by} ({group_col})")

        try:
            cur = conn.cursor()

            where_clauses = [group_col + " IS NOT NULL"]
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

            sql = (
                "SELECT "
                "  " + group_col + " AS identifier, "
                "  COUNT(*) AS signal_count, "
                "  COUNT(DISTINCT tool_requested) AS distinct_tools, "
                "  STRING_AGG(DISTINCT tool_requested::text, ',' ORDER BY tool_requested::text) AS tools_csv, "
                "  STRING_AGG(DISTINCT COALESCE(mcp_client, 'unknown')::text, ',') AS clients_csv, "
                "  STRING_AGG(DISTINCT COALESCE(tier_required, '')::text, ',') AS tiers_csv, "
                "  STRING_AGG(DISTINCT COALESCE(user_email, '')::text, ',') AS emails_csv, "
                "  STRING_AGG(DISTINCT COALESCE(ip_address, '')::text, ',') AS ips_csv, "
                "  BOOL_OR(COALESCE(converted, false)) AS has_converted, "
                "  MAX(converted_at) AS converted_at, "
                "  BOOL_OR(COALESCE(outreach_sent, false)) AS outreach_done, "
                "  MAX(outreach_sent_at) AS outreach_sent_at, "
                "  MIN(created_at) AS first_seen, "
                "  MAX(created_at) AS last_seen "
                "FROM mcp_upgrade_signals "
                "WHERE " + where_sql + " "
                "GROUP BY " + group_col + " "
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
                emails_csv = r[6] or ''
                ips_csv = r[7] or ''
                top_users.append({
                    'identifier': r[0],
                    'group_by': group_by,
                    'signal_count': int(r[1] or 0),
                    'distinct_tools': int(r[2] or 0),
                    'tools_tried': [s.strip() for s in tools_csv.split(',') if s.strip()],
                    'mcp_clients': [s.strip() for s in clients_csv.split(',') if s.strip()],
                    'tiers_required': [s.strip() for s in tiers_csv.split(',') if s.strip()],
                    'emails_seen': [s for s in (emails_csv.split(',') if emails_csv else []) if s and s != ''],
                    'ips_seen': [s for s in (ips_csv.split(',') if ips_csv else []) if s and s != ''],
                    'converted': bool(r[8]),
                    'converted_at': r[9].isoformat() if r[9] else None,
                    'outreach_sent': bool(r[10]),
                    'outreach_sent_at': r[11].isoformat() if r[11] else None,
                    'first_seen': r[12].isoformat() if r[12] else None,
                    'last_seen': r[13].isoformat() if r[13] else None,
                })

            # If group_by=ip + reverse_dns=1, do parallel reverse-DNS lookups
            if group_by == 'ip' and do_rdns and top_users:
                def _lookup(ip):
                    try:
                        socket.setdefaulttimeout(1.5)
                        hostname = socket.gethostbyaddr(ip)[0]
                        return ip, hostname
                    except Exception:
                        return ip, None

                ips_to_lookup = [u['identifier'] for u in top_users[:50] if u['identifier']]
                hostmap = {}
                with ThreadPoolExecutor(max_workers=10) as ex:
                    for ip, host in ex.map(_lookup, ips_to_lookup):
                        hostmap[ip] = host

                def _classify(host):
                    if not host: return None
                    h = host.lower()
                    if 'amazonaws.com' in h or 'compute-1' in h: return 'AWS'
                    if 'googleusercontent' in h or 'googleapis' in h or '1e100.net' in h: return 'GCP'
                    if 'azure' in h or 'cloudapp.net' in h: return 'Azure'
                    if 'cloudflare' in h or 'cdn-cgi' in h: return 'Cloudflare'
                    if 'github' in h: return 'GitHub'
                    if 'digitalocean' in h: return 'DigitalOcean'
                    if 'linode' in h: return 'Linode'
                    if 'vercel' in h or 'netlify' in h: return 'Vercel/Netlify'
                    if 'comcast' in h or 'spectrum' in h or 'verizon' in h or 'att.net' in h or 'cox.net' in h:
                        return 'Residential ISP'
                    # Try second-level domain as company guess
                    parts = h.split('.')
                    if len(parts) >= 2:
                        return parts[-2]
                    return 'unknown'

                for u in top_users:
                    h = hostmap.get(u['identifier'])
                    u['hostname'] = h
                    u['provider_guess'] = _classify(h)
                _step(f"reverse DNS done for {len(hostmap)} ips")

            # Top-level totals
            cur.execute("SELECT COUNT(*) FROM mcp_upgrade_signals")
            total_signals = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(DISTINCT " + group_col + ") FROM mcp_upgrade_signals WHERE " + group_col + " IS NOT NULL")
            total_distinct = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT COUNT(DISTINCT " + group_col + ") FROM mcp_upgrade_signals "
                "WHERE " + group_col + " IS NOT NULL AND COALESCE(converted, false) = true"
            )
            converted_groups = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT COUNT(DISTINCT " + group_col + ") FROM mcp_upgrade_signals "
                "WHERE " + group_col + " IS NOT NULL AND COALESCE(outreach_sent, false) = true"
            )
            contacted_groups = int(cur.fetchone()[0] or 0)

            # Always-included breakdowns (regardless of group_by)
            cur.execute(
                "SELECT COALESCE(mcp_client, 'unknown') AS c, COUNT(*) AS n "
                "FROM mcp_upgrade_signals GROUP BY c ORDER BY n DESC LIMIT 20"
            )
            by_client = [{'mcp_client': r[0], 'signals': int(r[1])} for r in cur.fetchall()]

            cur.execute(
                "SELECT COALESCE(tool_requested, 'unknown') AS tr, COUNT(*) AS n "
                "FROM mcp_upgrade_signals GROUP BY tr ORDER BY n DESC LIMIT 20"
            )
            by_tool = [{'tool_requested': r[0], 'signals': int(r[1])} for r in cur.fetchall()]
            _step("aggregates done")
        finally:
            try: conn.close()
            except Exception: pass

        if fmt == 'csv':
            sio = io.StringIO()
            writer = csv.writer(sio)
            base_cols = ['identifier', 'group_by', 'signal_count', 'distinct_tools',
                         'tools_tried', 'mcp_clients', 'emails_seen', 'ips_seen',
                         'tiers_required', 'converted', 'outreach_sent',
                         'first_seen', 'last_seen']
            if group_by == 'ip' and do_rdns:
                base_cols.extend(['hostname', 'provider_guess'])
            writer.writerow(base_cols)
            for u in top_users:
                row = [
                    u.get('identifier'), u.get('group_by'),
                    u.get('signal_count'), u.get('distinct_tools'),
                    '|'.join(u.get('tools_tried') or []),
                    '|'.join(u.get('mcp_clients') or []),
                    '|'.join(u.get('emails_seen') or []),
                    '|'.join(u.get('ips_seen') or []),
                    '|'.join(u.get('tiers_required') or []),
                    u.get('converted'), u.get('outreach_sent'),
                    u.get('first_seen'), u.get('last_seen'),
                ]
                if group_by == 'ip' and do_rdns:
                    row.extend([u.get('hostname'), u.get('provider_guess')])
                writer.writerow(row)
            return Response(sio.getvalue(), mimetype='text/csv', headers={
                'Content-Disposition': 'attachment; filename="dchub-top-' + group_by + '.csv"'
            })

        payload = {
            'phase': '61',
            'connector': connector,
            'group_by': group_by,
            'group_col': group_col,
            'count': len(top_users),
            'limit': limit,
            'reverse_dns_applied': bool(group_by == 'ip' and do_rdns),
            'filters': {
                'include_converted': include_converted,
                'include_contacted': include_contacted,
                'tier': tier_filter,
                'mcp_client': client_filter,
            },
            'totals': {
                'total_signals': total_signals,
                'distinct_groups': total_distinct,
                'converted_groups': converted_groups,
                'contacted_groups': contacted_groups,
                'conversion_rate_pct': round(100.0 * converted_groups / total_distinct, 2) if total_distinct else 0,
            },
            'by_mcp_client': by_client,
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
            'phase': '61',
        }), 500


# ----------------------------------------------------------------------------
# Phase 62f -- phase62f_recent_signals
# Dump the N most recent raw rows from mcp_upgrade_signals.
# Tells us which columns are populated by the active writer.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/observability/recent-signals', methods=['GET'])
def phase62f_recent_signals():
    """Most recent rows from mcp_upgrade_signals, all columns, no aggregation."""
    import os, traceback
    from flask import request, jsonify

    try:
        try:
            limit = int(request.args.get('limit', '10'))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 100))

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'no DB url'}), 500

        conn = None
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                conn = mod.connect(neon)
                break
            except Exception:
                continue
        if not conn:
            return jsonify({'error': 'no postgres driver'}), 500

        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'mcp_upgrade_signals' "
                "ORDER BY ordinal_position"
            )
            cols = [r[0] for r in cur.fetchall()]

            cur.execute(
                "SELECT * FROM mcp_upgrade_signals "
                "ORDER BY created_at DESC NULLS LAST LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()

            out = []
            for r in rows:
                row = {}
                for i, col in enumerate(cols):
                    v = r[i]
                    if hasattr(v, 'isoformat'):
                        v = v.isoformat()
                    row[col] = v
                out.append(row)

            # Also a per-column populated count over the last 100 rows
            cur.execute("SELECT * FROM mcp_upgrade_signals ORDER BY created_at DESC NULLS LAST LIMIT 100")
            recent_100 = cur.fetchall()
            populated = {col: 0 for col in cols}
            for r in recent_100:
                for i, col in enumerate(cols):
                    if r[i] is not None and r[i] != '':
                        populated[col] += 1
        finally:
            try: conn.close()
            except Exception: pass

        return jsonify({
            'phase': '62f',
            'columns': cols,
            'population_last_100_rows': populated,
            'count': len(out),
            'recent_signals': out,
        })

    except Exception as e:
        return jsonify({
            'error': 'unhandled',
            'type': type(e).__name__,
            'message': str(e),
            'traceback': traceback.format_exc(),
        }), 500


# ----------------------------------------------------------------------------
# Phase 64c -- phase64c_dev_keys
# Schema-discover the dev_keys table and surface emails for outreach.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/observability/dev-keys', methods=['GET'])
def phase64c_dev_keys():
    """List active dev keys with their emails.

    Schema-discovers the table by looking for one with both 'email' and
    a tier-like column. Optional gate: TOP_USERS_TOKEN env var.
    """
    import os, traceback
    from flask import request, jsonify

    try:
        admin_token = os.environ.get('TOP_USERS_TOKEN')
        if admin_token:
            provided = request.headers.get('X-Admin-Token') or request.args.get('token')
            if provided != admin_token:
                return jsonify({'error': 'unauthorized'}), 401

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'no DB url'}), 500

        conn = None
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                conn = mod.connect(neon); break
            except Exception:
                continue
        if not conn:
            return jsonify({'error': 'no postgres driver'}), 500

        try:
            cur = conn.cursor()

            # Find candidate tables (anything with key/dev/api in name)
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND (table_name ILIKE %s OR table_name ILIKE %s OR table_name ILIKE %s) "
                "ORDER BY table_name",
                ('%key%', '%dev%', '%api%')
            )
            candidates = [r[0] for r in cur.fetchall()]

            chosen = None
            chosen_cols = []
            for tbl in candidates:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = 'public' "
                    "ORDER BY ordinal_position",
                    (tbl,)
                )
                cols = [r[0] for r in cur.fetchall()]
                has_email = 'email' in cols
                has_tier = any(c in cols for c in ('tier', 'plan', 'subscription_tier', 'tier_level'))
                has_keylike = any(c in cols for c in ('key', 'api_key', 'token', 'dev_key', 'key_value'))
                if has_email and (has_tier or has_keylike):
                    chosen = tbl
                    chosen_cols = cols
                    break

            if not chosen:
                return jsonify({
                    'error': 'no dev key table with email+tier found',
                    'candidates': candidates,
                    'phase': '64c',
                }), 500

            # Build a SELECT with whatever useful columns exist
            preferred = ['id', 'email', 'tier', 'plan', 'subscription_tier',
                         'is_active', 'active', 'enabled',
                         'key_id', 'created_at', 'last_used_at',
                         'last_seen_at', 'last_used', 'usage_count']
            select_cols = [c for c in preferred if c in chosen_cols]
            if not select_cols:
                select_cols = ['email']

            order_by = 'created_at DESC' if 'created_at' in chosen_cols else 'email'
            sql = (
                'SELECT ' + ', '.join('"' + c + '"' for c in select_cols)
                + ' FROM "' + chosen + '" '
                + 'ORDER BY ' + order_by
                + ' LIMIT 200'
            )
            cur.execute(sql)
            rows = cur.fetchall()

            keys = []
            for r in rows:
                d = {}
                for i, col in enumerate(select_cols):
                    v = r[i]
                    if hasattr(v, 'isoformat'):
                        v = v.isoformat()
                    d[col] = v
                keys.append(d)

            # Also a per-tier rollup if a tier-like column exists
            tier_col = next((c for c in ('tier', 'plan', 'subscription_tier', 'tier_level') if c in chosen_cols), None)
            tier_rollup = []
            if tier_col:
                cur.execute(
                    'SELECT "' + tier_col + '" AS tier, COUNT(*) AS n '
                    'FROM "' + chosen + '" GROUP BY tier ORDER BY n DESC'
                )
                tier_rollup = [{'tier': r[0], 'count': int(r[1])} for r in cur.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass

        return jsonify({
            'phase': '64c',
            'table': chosen,
            'columns_used': select_cols,
            'count': len(keys),
            'tier_rollup': tier_rollup,
            'keys': keys,
        })

    except Exception as e:
        return jsonify({
            'error': 'unhandled',
            'type': type(e).__name__,
            'message': str(e),
            'traceback': traceback.format_exc(),
        }), 500


# ----------------------------------------------------------------------------
# Phase 73 -- phase73_discovery_freshness
# Daily breakdown of newly-discovered records across all data tables.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/discovery/last-7d', methods=['GET'])
def phase73_discovery_freshness():
    """For each table with a created_at column, report new rows in last 7d.

    Useful as a morning glance: is auto-discovery actually finding things?
    """
    import os, traceback
    from flask import request, jsonify

    try:
        try:
            limit_days = int(request.args.get('days', '7'))
        except (TypeError, ValueError):
            limit_days = 7
        limit_days = max(1, min(limit_days, 30))

        # Tables of interest (empty list => discover automatically)
        candidates = [
            'facilities', 'main_facilities', 'discovered_facilities',
            'substations', 'eia_generators', 'fiber_routes',
            'gas_pipelines', 'transmission_lines', 'power_plants',
            'mcp_upgrade_signals', 'mcp_tool_calls',
            'nepa_filings',
        ]

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'no DB url'}), 500

        conn = None
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                conn = mod.connect(neon); break
            except Exception:
                continue
        if not conn:
            return jsonify({'error': 'no postgres driver'}), 500

        try:
            cur = conn.cursor()

            # Filter to tables that actually exist + have created_at
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (candidates,)
            )
            existing = {r[0] for r in cur.fetchall()}

            tables_with_created = []
            for tbl in candidates:
                if tbl not in existing:
                    continue
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "AND column_name = 'created_at' LIMIT 1",
                    (tbl,)
                )
                if cur.fetchone():
                    tables_with_created.append(tbl)

            results = []
            # phase73b_interval_fix -- embed sanitized int into INTERVAL literal
            interval_clause = "NOW() - INTERVAL '" + str(int(limit_days)) + " days'"
            for tbl in tables_with_created:
                # Count last N days
                cur.execute(
                    'SELECT COUNT(*) FROM "' + tbl + '" '
                    'WHERE created_at >= ' + interval_clause
                )
                total_recent = int(cur.fetchone()[0] or 0)

                # Per-day breakdown
                cur.execute(
                    'SELECT DATE(created_at) AS d, COUNT(*) FROM "' + tbl + '" '
                    'WHERE created_at >= ' + interval_clause + ' '
                    'GROUP BY d ORDER BY d DESC'
                )
                per_day = [{'date': str(r[0]), 'count': int(r[1])} for r in cur.fetchall()]

                # Source breakdown (if column exists)
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "AND column_name = 'source' LIMIT 1",
                    (tbl,)
                )
                by_source = []
                if cur.fetchone():
                    cur.execute(
                        'SELECT COALESCE(source, \'unknown\') AS s, COUNT(*) FROM "' + tbl + '" '
                        'WHERE created_at >= ' + interval_clause + ' '
                        'GROUP BY s ORDER BY 2 DESC LIMIT 15'
                    )
                    by_source = [{'source': r[0], 'count': int(r[1])} for r in cur.fetchall()]

                results.append({
                    'table': tbl,
                    'total_last_' + str(limit_days) + 'd': total_recent,
                    'per_day': per_day,
                    'by_source': by_source,
                })
        finally:
            try: conn.close()
            except Exception: pass

        return jsonify({
            'phase': '73',
            'days': limit_days,
            'tables_checked': len(tables_with_created),
            'results': results,
        })

    except Exception as e:
        return jsonify({
            'error': 'unhandled',
            'type': type(e).__name__,
            'message': str(e),
            'traceback': traceback.format_exc(),
        }), 500


# ----------------------------------------------------------------------------
# Phase 75 -- phase75_nepa_endpoint
# Read recent NEPA filings + optionally trigger a refresh scrape.
# ----------------------------------------------------------------------------
@observability_bp.route('/api/v1/discovery/nepa', methods=['GET'])
def phase75_nepa_filings():
    """Recent NEPA filings related to data center / AI infrastructure projects.

    Query params:
      limit       int, default 25, max 200
      refresh     1 to trigger a fresh scrape (admin token required if set)
      token       admin token if NEPA_ADMIN_TOKEN env is set
    """
    import os, traceback
    from flask import request, jsonify

    try:
        try:
            limit = int(request.args.get('limit', '25'))
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 200))

        triggered = False
        new_count = 0
        if request.args.get('refresh') == '1':
            admin_token = os.environ.get('NEPA_ADMIN_TOKEN')
            if admin_token:
                provided = request.headers.get('X-Admin-Token') or request.args.get('token')
                if provided != admin_token:
                    return jsonify({'error': 'unauthorized'}), 401
            try:
                from services.nepa_scraper import scrape_recent_filings
                new_count = scrape_recent_filings(max_pages=2)
                triggered = True
            except Exception as e:
                return jsonify({
                    'error': 'scraper failed',
                    'type': type(e).__name__,
                    'message': str(e),
                }), 500

        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return jsonify({'error': 'no DB url'}), 500
        conn = None
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                conn = mod.connect(neon); break
            except Exception:
                continue
        if not conn:
            return jsonify({'error': 'no postgres driver'}), 500

        try:
            cur = conn.cursor()
            # Make sure the table exists (the scraper creates it,
            # but the read endpoint should not crash if no scrape has run)
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'nepa_filings' LIMIT 1"
            )
            if not cur.fetchone():
                return jsonify({
                    'phase': '75',
                    'message': 'nepa_filings table does not exist yet; run with ?refresh=1 to create + populate',
                    'count': 0,
                    'filings': [],
                })

            # phase75b_filter -- default to high+medium relevance, opt-in to all
            min_relevance = (request.args.get('min_relevance') or 'medium').lower()
            allowed_rel = {
                'high':    ("'high'",),
                'medium':  ("'high'", "'medium'"),
                'all':     ("'high'", "'medium'", "'low'", "'unknown'"),
            }.get(min_relevance, ("'high'", "'medium'"))
            in_clause = "(" + ", ".join(allowed_rel) + ")"
            cur.execute(
                "SELECT id, document_id, docket_id, agency, title, summary, "
                "posted_date, document_type, url, keyword_matched, created_at, relevance "
                "FROM nepa_filings "
                "WHERE COALESCE(relevance, 'unknown') IN " + in_clause + " "
                "ORDER BY posted_date DESC NULLS LAST, id DESC "
                "LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
            cols = ['id','document_id','docket_id','agency','title','summary',
                    'posted_date','document_type','url','keyword_matched','created_at','relevance']
            filings = []
            for r in rows:
                d = {}
                for i, c in enumerate(cols):
                    v = r[i]
                    if hasattr(v, 'isoformat'):
                        v = v.isoformat()
                    d[c] = v
                filings.append(d)

            cur.execute("SELECT COUNT(*) FROM nepa_filings")
            total = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT agency, COUNT(*) FROM nepa_filings "
                "GROUP BY agency ORDER BY 2 DESC LIMIT 10"
            )
            by_agency = [{'agency': r[0], 'count': int(r[1])} for r in cur.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass

        return jsonify({
            'phase': '75',
            'total_filings': total,
            'returned': len(filings),
            'refresh_triggered': triggered,
            'new_inserted_this_call': new_count,
            'by_agency': by_agency,
            'filings': filings,
        })

    except Exception as e:
        return jsonify({
            'error': 'unhandled',
            'type': type(e).__name__,
            'message': str(e),
            'traceback': traceback.format_exc(),
        }), 500

