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


def _require_obs_admin():
    """Mandatory, fail-CLOSED admin gate for PII-bearing observability reads.

    ★ 2026-08-10 SECURITY. Two endpoints here guarded themselves with
    `admin_token = os.environ.get('TOP_USERS_TOKEN'); if admin_token:` — an
    OPTIONAL gate. TOP_USERS_TOKEN is not set on Railway, so both were fully
    public. Measured live from a clean, unauthenticated client:

        /api/v1/observability/dev-keys   200 rows · 24 distinct owner emails
                                         with tier + last_used_at
        /api/v1/observability/top-users    9 emails

    No key MATERIAL leaked (verified: zero dch_live_* strings in the payloads),
    but "who our developers are, what tier they pay for, and when they last
    called" is customer data, not telemetry.

    Fails CLOSED: if no credential is configured at all, refuse. An optional
    gate on PII is not a gate — it is a gate that is open by default.
    Accepts TOP_USERS_TOKEN (back-compat with existing callers) or the standard
    DCHUB_ADMIN_KEY, via header or query param. Returns None when authorised,
    otherwise the (body, status) tuple to return directly.
    """
    import os as _os
    tok = (_os.environ.get('TOP_USERS_TOKEN') or '').strip()
    key = (_os.environ.get('DCHUB_ADMIN_KEY') or '').strip()
    got = (request.headers.get('X-Admin-Token')
           or request.headers.get('X-Admin-Key')
           or request.args.get('token')
           or request.args.get('admin_key') or '').strip()
    if got and ((tok and got == tok) or (key and got == key)):
        return None
    return jsonify({'error': 'unauthorized',
                    'hint': 'X-Admin-Key required — this endpoint exposes '
                            'customer email addresses'}), 401


# Phase plant-count-truth (2026-07-29): 'total_power_plants' and
# 'total_capacity_mw' were recorded from the 66-row `power_plants` stub. They
# are renamed at the writer to name their table (…_stub_table) and the real US
# EIA fleet is recorded as …_eia. The anomaly detector below watches the EIA
# series — the stub series is still written so its history stays readable, but
# an anomaly in a 66-row stub is not a signal worth alerting on.
CRITICAL_METRICS = [
    'total_substations', 'total_pipelines', 'total_power_plants_eia',
    'total_fiber_routes', 'total_capacity_mw_eia',
    'mcp_tool_calls_24h', 'mcp_conversions_24h', 'agent_requests_24h',
    'health_score', 'linkedin_impressions_24h',
    'pricing_page_views_24h', 'upgrade_signals_24h',
]


# Phase FF+11-schemafix (2026-05-19) — lift the observability_metrics
# CREATE TABLE out of the per-request handler. Previously the handler
# did CREATE TABLE then a loop of SELECTs from source tables (one of
# which had the called_at typo and aborted the transaction), then
# INSERTs into observability_metrics — which then surfaced as
# "relation observability_metrics does not exist" because the
# transaction had been aborted by the failed SELECT. Fixing the
# called_at typo on its own should resolve this, but a module-level
# init makes us robust to any future query in the loop that aborts
# the transaction.
def _ensure_observability_metrics_table():
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS observability_metrics (
                    metric TEXT NOT NULL,
                    value DOUBLE PRECISION NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            try: cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_obs_metric_recorded "
                "ON observability_metrics (metric, recorded_at DESC)"
            )
            except Exception: pass
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        pass  # best-effort; per-request fallback CREATE still in place


try:
    _ensure_observability_metrics_table()
except Exception:
    pass


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
        try:
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

            # Phase QA-sweep (2026-05-16): probe each source table with
            # to_regclass FIRST so missing tables degrade silently instead
            # of dumping "relation X does not exist" to Railway logs every
            # cycle (the schema_drift detector was repeatedly flagging
            # pipelines, gas_compressors, wind_projects which don't exist
            # on this deploy).
            # Phase plant-count-truth (2026-07-29): the two power-plant metrics
            # below are recorded from the bare `power_plants` table, which holds
            # 66 rows / 9,609 MW for the entire United States — the same US
            # EIA-860 population as power_plants_eia (13,446), loaded to 0.5% by
            # a crawler whose dedup step silently drops every record lacking a
            # 'plantid' key. This is a TIME SERIES, so the metrics are NOT
            # silently repointed: doing that would splice a 204x step change
            # into history with no marker. They are RENAMED to say which table
            # they measure, and the real fleet figure is recorded alongside
            # under its own name, so the discontinuity is visible as two series
            # rather than hidden inside one.
            samples = {}
            for label, table, sql in [
                ('total_substations',    'substations',    "SELECT COUNT(*) FROM substations"),
                ('total_pipelines',      'pipelines',      "SELECT COUNT(*) FROM pipelines"),
                ('total_power_plants_stub_table', 'power_plants',
                 "SELECT COUNT(*) FROM power_plants"),
                ('total_power_plants_eia', 'power_plants_eia',
                 "SELECT COUNT(*) FROM power_plants_eia"),
                ('total_fiber_routes',   'fiber_routes',   "SELECT COUNT(*) FROM fiber_routes"),
                ('total_capacity_mw_stub_table', 'power_plants',
                 "SELECT COALESCE(SUM(capacity_mw),0) FROM power_plants"),
                ('total_capacity_mw_eia', 'power_plants_eia',
                 "SELECT COALESCE(SUM(nameplate_capacity_mw),0) FROM power_plants_eia"),
                # Phase FF+11-schemafix (2026-05-19): column is `created_at`,
                # not `called_at`. The wrong name aborted the request's
                # transaction, which is why subsequent INSERT INTO
                # observability_metrics statements then ALSO failed with
                # "relation does not exist" — psycopg2 surfaces aborted-
                # transaction errors that way until rollback. Two bugs,
                # one root cause.
                ('mcp_tool_calls_24h',   'mcp_tool_calls', "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '24 hours'"),
                ('mcp_conversions_24h',  'mcp_conversions',"SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '24 hours'"),
            ]:
                try:
                    # Probe first — silently skip if table doesn't exist
                    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                    if not (cur.fetchone() or [None])[0]:
                        continue
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
        finally:
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
        try:
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
        finally:
            try: conn.close()
            except Exception: pass
        flagged = [m for m in out['data']['metrics'] if m.get('drift')]
        out['data']['drift_count'] = len(flagged)
        out['data']['healthy'] = len(flagged) == 0
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    return jsonify(out)


_daily_anomalies_ready = False


def _ensure_daily_anomalies():
    """Create daily_anomalies on a DIRECT cursor. Once per process.

    ★ 2026-08-04: the CREATE used to sit inline in the /anomalies handler on a
    db_utils.try_get_db() cursor, which SKIPs DDL under SKIP_DDL (default on,
    unset in prod). The boot audit confirmed against the live database that the
    table does NOT exist — so the handler's SELECT has been raising on every
    call and the endpoint has returned its empty `{'anomalies': []}` fallback
    since it was written. An empty anomaly list reads exactly like a healthy
    system, which is why nobody noticed. Idempotent; never raises."""
    global _daily_anomalies_ready
    if _daily_anomalies_ready:
        return
    try:
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_anomalies (
                    id SERIAL PRIMARY KEY,
                    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    severity TEXT NOT NULL DEFAULT 'info',
                    summary TEXT NOT NULL,
                    details JSONB
                )
            """)
        _daily_anomalies_ready = True
    except Exception:
        pass  # best-effort; the handler still degrades to an empty list


@observability_bp.route('/api/v1/observability/anomalies', methods=['GET'])
def anomalies():
    out = {'success': True, 'data': {'anomalies': []}}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn: return jsonify(out)
        _ensure_daily_anomalies()
        try:
            cur = conn.cursor()
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
        finally:
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
# ── reverse-DNS enrichment for /top-users?group_by=ip&reverse_dns=1 ──────────
#
# ★ 2026-08-26. This used to call socket.setdefaulttimeout(1.5) inside the
#   ThreadPoolExecutor worker and never restore it. setdefaulttimeout() is
#   PROCESS-GLOBAL: one admin GET of
#   /api/v1/observability/top-users?group_by=ip&reverse_dns=1 left EVERY socket
#   created afterwards in that process on a 1.5s default — psycopg2 connections,
#   requests calls with no explicit timeout=, the brain's and the watchdog's
#   I/O — for the remaining life of the process. The worker runs the brain, the
#   watchdog and the scheduler in sibling threads, so the blast radius was the
#   whole process, not this request, and nothing here ever put it back.
#
#   And it bought nothing for the lookup it was meant to bound.
#   setdefaulttimeout() is applied when a socket OBJECT is created;
#   gethostbyaddr() goes to the platform resolver and never consults it.
#   Measured: under socket.setdefaulttimeout(0.001), gethostbyaddr('1.1.1.1')
#   SUCCEEDED in 0.022s — 22x a bound that would have aborted it — and the
#   no-PTR cases raised herror ('Unknown host'), not TimeoutError. So the old
#   line was pure cost: no bound on the DNS, a 1.5s default on everything else.
#
#   gethostbyaddr() accepts no timeout argument either, so there is no per-call
#   knob to pass instead. The bound therefore moves off the socket and onto the
#   WAIT: submit every lookup, wait ONCE for the whole batch, and abandon
#   whatever has not answered. This is the FIRST real bound on this path. A
#   slow resolver costs that row its hostname, not the request — and no code
#   outside this function observes a changed default.
#
#   Compare routes/email_validation.py, which sets the same global but restores
#   it in a finally. That is safer and still racy: a sibling thread creating a
#   socket inside the window gets the wrong default. Do not copy it here.

_RDNS_BATCH_BUDGET_S = 6.0   # whole-batch wall clock, NOT per lookup
_RDNS_MAX_WORKERS = 10
_RDNS_MAX_IPS = 50


def _reverse_dns_map(ips, budget_s=_RDNS_BATCH_BUDGET_S,
                     max_workers=_RDNS_MAX_WORKERS):
    """{ip: hostname_or_None} for as many of `ips` as answer within `budget_s`.

    Bounded WITHOUT socket.setdefaulttimeout(). An IP whose lookup has not
    finished when the budget expires is simply absent from the returned map —
    the caller renders hostname=None for it — and its worker is abandoned
    rather than waited on. Callers that care how much was dropped should
    compare len(result) against len(ips); this function never pretends a
    timed-out lookup was a negative answer to the caller's counter.
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait

    ips = [ip for ip in ips if ip]
    if not ips:
        return {}

    def _lookup(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0]
        except Exception:
            # No PTR record, NXDOMAIN, resolver error — all "no hostname".
            return ip, None

    hostmap = {}
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = [ex.submit(_lookup, ip) for ip in ips]
        done, not_done = _futures_wait(futures, timeout=budget_s)
        for fut in done:
            try:
                ip, host = fut.result()
            except Exception:
                continue
            hostmap[ip] = host
        for fut in not_done:
            fut.cancel()          # skips the ones that never started
    finally:
        # wait=False is the whole point. `with ThreadPoolExecutor(...)` joins
        # every worker on exit, which would re-impose the unbounded wait this
        # function exists to remove.
        ex.shutdown(wait=False)
    return hostmap


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
    import os, csv, io, traceback
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

        _denied = _require_obs_admin()
        if _denied:
            return _denied

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

            # If group_by=ip + reverse_dns=1, do parallel reverse-DNS lookups.
            # Bounded by _reverse_dns_map's batch wait — never by the socket
            # module's process-global default. See the note above the helper.
            if group_by == 'ip' and do_rdns and top_users:
                ips_to_lookup = [u['identifier'] for u in top_users[:_RDNS_MAX_IPS] if u['identifier']]
                hostmap = _reverse_dns_map(ips_to_lookup)

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
                # Report the DENOMINATOR too: with a batch budget, a short
                # map means "some lookups were abandoned", not "fewer IPs".
                _step(f"reverse DNS: {len(hostmap)}/{len(ips_to_lookup)} answered "
                      f"within {_RDNS_BATCH_BUDGET_S}s")

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
        _denied = _require_obs_admin()
        if _denied:
            return _denied

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
# ── /api/v1/discovery/last-7d SWR cache (fix agenda #100095, lane 1) ──────────
# _compute_discovery_freshness fans ~40 COUNT/GROUP-BY queries across ~12 large
# tables (facilities, discovered_facilities, transmission_lines, power_plants…)
# filtered on created_at — 2–5s cold, spiking past the radar's 5s frontend_
# endpoint_slow cap (finding #9235-class @ /snapshot; surfaced live by the
# frontend-reliability shell). The result changes only when discovery ingests new
# rows (daily), so serve it stale-while-revalidate: every request returns the last
# good copy instantly; a stale copy (>10min) fires one background single-flight
# refresh. Only the first request after a cache eviction pays the fan-out. Same
# pattern as /api/pipeline (routes/deals_routes.py get_public_pipeline).
import threading as _threading
import time as _time_swr
try:
    from redis_cache import cache_get as _dfc_get, cache_set as _dfc_set
except ImportError:
    _dfc_get = lambda k: None
    _dfc_set = lambda k, v, ttl=300: None
_DISCOVERY_FRESH_TTL = 600     # serve without refresh for 10 min
_DISCOVERY_HARD_TTL = 3600     # keep a stale copy up to 1h for SWR fallback
_discovery_refresh_lock = _threading.Lock()


def _compute_discovery_freshness(limit_days: int) -> dict:
    """Per-table new-rows-in-last-N-days fan-out. Context-free (touches no Flask
    request/g) so it is safe to run in a background SWR refresh thread. Raises on
    hard failure (no DB url/driver) so the caller records the miss."""
    import os
    candidates = [
        'facilities', 'main_facilities', 'discovered_facilities',
        'substations', 'eia_generators', 'fiber_routes',
        'gas_pipelines', 'transmission_lines', 'power_plants',
        'mcp_upgrade_signals', 'mcp_tool_calls',
        'nepa_filings',
    ]
    neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not neon:
        raise RuntimeError('no DB url')
    conn = None
    for modname in ('psycopg', 'psycopg2'):
        try:
            mod = __import__(modname)
            conn = mod.connect(neon); break
        except Exception:
            continue
    if not conn:
        raise RuntimeError('no postgres driver')
    try:
        cur = conn.cursor()
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
            cur.execute('SELECT COUNT(*) FROM "' + tbl + '" WHERE created_at >= ' + interval_clause)
            total_recent = int(cur.fetchone()[0] or 0)
            cur.execute(
                'SELECT DATE(created_at) AS d, COUNT(*) FROM "' + tbl + '" '
                'WHERE created_at >= ' + interval_clause + ' GROUP BY d ORDER BY d DESC'
            )
            per_day = [{'date': str(r[0]), 'count': int(r[1])} for r in cur.fetchall()]
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
                    'WHERE created_at >= ' + interval_clause + ' GROUP BY s ORDER BY 2 DESC LIMIT 15'
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
    return {'phase': '73', 'days': limit_days,
            'tables_checked': len(tables_with_created), 'results': results}


def _refresh_discovery_freshness(limit_days: int, key: str):
    """Background single-flight recompute + re-cache. Never raises."""
    if not _discovery_refresh_lock.acquire(blocking=False):
        return
    try:
        payload = _compute_discovery_freshness(limit_days)
        payload['_computed_at'] = _time_swr.time()
        _dfc_set(key, payload, ttl=_DISCOVERY_HARD_TTL)
    except Exception:
        pass
    finally:
        _discovery_refresh_lock.release()


@observability_bp.route('/api/v1/discovery/last-7d', methods=['GET'])
def phase73_discovery_freshness():
    """For each table with a created_at column, report new rows in last Nd.
    Served stale-while-revalidate (see cache note above) — the fan-out spiked
    past the 5s radar cap cold; now only a post-eviction miss pays it."""
    from flask import request, jsonify
    try:
        limit_days = int(request.args.get('days', '7'))
    except (TypeError, ValueError):
        limit_days = 7
    limit_days = max(1, min(limit_days, 30))

    key = "discovery_freshness:v1:%d" % limit_days
    now = _time_swr.time()
    cached = _dfc_get(key)
    if cached is not None:
        age = now - (cached.get('_computed_at') or 0)
        if age >= _DISCOVERY_FRESH_TTL:
            try:
                _threading.Thread(target=_refresh_discovery_freshness,
                                  args=(limit_days, key),
                                  name="discovery-freshness-refresh", daemon=True).start()
            except Exception:
                pass
        resp = jsonify({k: v for k, v in cached.items() if k != '_computed_at'})
        resp.headers['X-Cache'] = 'HIT' if age < _DISCOVERY_FRESH_TTL else 'STALE'
        return resp

    # Cold cache (first request / after eviction): compute once, synchronously.
    try:
        payload = _compute_discovery_freshness(limit_days)
    except Exception as e:
        import traceback
        return jsonify({'error': 'unhandled', 'type': type(e).__name__,
                        'message': str(e), 'traceback': traceback.format_exc()}), 500
    payload['_computed_at'] = now
    _dfc_set(key, payload, ttl=_DISCOVERY_HARD_TTL)
    resp = jsonify({k: v for k, v in payload.items() if k != '_computed_at'})
    resp.headers['X-Cache'] = 'MISS'
    return resp


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

