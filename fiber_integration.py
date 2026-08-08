"""
DC Hub — Fiber Intelligence Integration Layer
═══════════════════════════════════════════════
Wires up all fiber/carrier ingestion modules into main.py and adds
admin sync endpoints + scheduler hooks.

How to integrate:
  1. Copy subsea_cable_ingestion.py and carrier_facility_ingestion.py
     to your project root alongside main.py
  2. Add this file as fiber_integration.py
  3. In main.py, add near the top:
       from fiber_integration import register_fiber_intelligence
  4. After app creation and get_db definition, call:
       register_fiber_intelligence(app, get_db)

v1.0 — March 2026
"""

import logging
import os
from functools import wraps
from datetime import datetime

from flask import jsonify, request

logger = logging.getLogger('dchub-fiber')


def register_fiber_intelligence(app, get_db):
    """
    Master registration: tables, API routes, admin sync endpoints.
    Call once from main.py after app + get_db are available.
    """
    from subsea_cable_ingestion import (
        init_subsea_tables,
        run_subsea_sync,
        register_subsea_routes,
    )
    from carrier_facility_ingestion import (
        init_carrier_tables,
        run_carrier_sync,
        register_carrier_routes,
    )

    # ── Initialize tables on startup ──────────────────────────
    try:
        init_subsea_tables(get_db)
        init_carrier_tables(get_db)
        logger.info("✅ Fiber intelligence tables ready")
    except Exception as e:
        logger.warning(f"Fiber table init (non-fatal): {e}")

    # ── Register public API routes ────────────────────────────
    register_subsea_routes(app, get_db)
    register_carrier_routes(app, get_db)

    # ── Auth helper for admin endpoints ───────────────────────
    def require_internal_key(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            key = (request.headers.get('X-Internal-Key', '') or
                   request.args.get('key', ''))
            valid_keys = [
                k for k in [
                    os.environ.get('DCHUB_INTERNAL_KEY', ''),
                    os.environ.get('DCHUB_SYNC_KEY', ''),
                    os.environ.get('DCHUB_ADMIN_KEY', ''),
                ] if k
            ]
            if not key or key not in valid_keys:
                return jsonify({'error': 'Unauthorized'}), 401
            return f(*args, **kwargs)
        return decorated

    # ── Direct DB factory for the minutes-long sync jobs ──────
    # The app pool's forced-reclaim loop kills any pooled connection held
    # >60s (_CONN_MAX_HOLD_SECONDS), which severed the 600k-row
    # carrier-facility upsert mid-loop ("connection already closed",
    # observed 2026-07-16). Bulk jobs open their own direct connection —
    # same pattern as fiber_network_discovery — while the lightweight
    # read routes and boot-time table init keep the pooled get_db.
    def _job_db():
        import psycopg2
        return psycopg2.connect(os.environ.get('DATABASE_URL', ''),
                                connect_timeout=10)

    # ── Admin Sync Endpoints ──────────────────────────────────

    @app.route('/api/jobs/subsea-sync', methods=['POST'])
    @require_internal_key
    def admin_subsea_sync():
        """Trigger TeleGeography submarine cable data sync."""
        result = run_subsea_sync(_job_db)
        return jsonify(result)

    @app.route('/api/jobs/carrier-sync', methods=['POST'])
    @require_internal_key
    def admin_carrier_sync():
        """Trigger PeeringDB carrier-facility data sync."""
        result = run_carrier_sync(_job_db)
        return jsonify(result)

    @app.route('/api/jobs/fiber-full-sync', methods=['POST'])
    @require_internal_key
    def admin_fiber_full_sync():
        """Run full fiber intelligence sync (subsea + carriers + coverage zones)."""
        results = {
            'timestamp': datetime.utcnow().isoformat(),
            'job': 'fiber-full-sync',
        }

        # Subsea cables
        subsea = run_subsea_sync(_job_db)
        results['subsea'] = subsea

        # Carriers + facilities + coverage zones
        carrier = run_carrier_sync(_job_db)
        results['carriers'] = carrier

        results['success'] = subsea.get('success', False) and carrier.get('success', False)
        results['total_records'] = (
            subsea.get('total_new', 0) +
            carrier.get('total_records', 0)
        )

        return jsonify(results)

    # ── Fiber Intelligence Summary ────────────────────────────

    @app.route('/api/v1/fiber/summary', methods=['GET'])
    def fiber_intelligence_summary():
        """High-level stats for fiber intelligence dashboard.

        ★★★ 2026-08-08 — THIS ENDPOINT WAS PUBLISHING SIX CONFIDENT ZEROS.
        Measured live, keyless (it is allow-listed in free_tier_gate.py:187, so
        anyone can read it):

            fiber_routes: 0            while /api/v1/stats said 55,079 and the
                                       server card, llms.txt and the MCP
                                       handshake all advertise 55,064
            legacy_fiber_routes: 0
            legacy_metro_dark_fiber: 0 while /api/v1/stats said 59
            major_hubs: 0
            subsea_planned: 0
            last_sync: null

        TWO INDEPENDENT CAUSES, and the second is the dangerous one:

        1. WRONG TABLE. `fiber_routes` was counted from `fiber_route_geometry`.
           The canonical count — the one /api/v1/stats publishes and every
           agent surface advertises — is `COUNT(*) FROM fiber_routes`
           (main.py:20472). Two surfaces, two tables, one name.

        2. EVERY COUNT SAT IN `except Exception: stats[...] = 0`. A missing
           table, a permission error and a genuinely empty table all produced
           the identical output: a confident `0`. That is why nobody could tell
           which of the six zeros were real — and why `metro_dark_fiber`
           returned 59 on one endpoint and 0 here from the SAME table name.
           An absent number must be null, never zero. A zero is a measurement.

        Also fixed: the old grouped except blocks assigned only SOME of the
        keys they covered, so a failure part-way through the subsea group left
        `subsea_planned` and `major_hubs` completely absent from the response
        rather than null — a third distinct way to be wrong.

        Each count is now issued independently with a rollback on failure, so
        one bad statement cannot poison the rest of the transaction and cascade
        into a row of zeros.

        ★★★ 2026-08-08, SECOND PASS — THE FIX ABOVE LEFT TWO ZEROS STANDING.
        `fiber_routes` was corrected and cross-checks against /api/v1/stats,
        but the same response still published:

            major_hubs: 0        from subsea_landing_points.is_major_hub,
                                 which is FALSE on all 1,927 rows and TRUE on
                                 none — ?major_only=true returns nothing
            subsea_planned: 0    from subsea_cables.is_planned, which is NULL
                                 on all 699 rows — ?planned=true AND
                                 ?planned=false BOTH return 0, which only
                                 happens when every value is NULL

        These reads SUCCEED. There was no exception to catch and no null to
        publish, so the previous fix could not see them: it separated "the read
        failed" from "we counted", and this is neither. The query is correct,
        the 0 is arithmetically right, and the claim it makes — "we checked
        1,927 landing points and none is a major hub" — was never measured.
        The column was never populated. That is a coverage gap wearing a
        measurement's clothes, and it contradicted this response's own
        basis_note.

        A count over a predicate is only a measurement if the column behind the
        predicate carries data. `util.db_honesty.zero_is_measured` checks that
        against the whole table whenever a filtered count comes back 0, and a
        column that never varies demotes the count to null with a reason. A
        column that DOES vary keeps its 0 — that is a real finding.
        """
        from util.db_honesty import zero_is_measured

        conn = None

        def _count(cur, sql, source_table):
            """(value, error) — never a fabricated zero.

            Rolls back on failure: in PostgreSQL a failed statement aborts the
            transaction and every subsequent statement on that connection then
            fails too, which would turn one missing table into a page of
            zeros.
            """
            try:
                cur.execute(sql)
                row = cur.fetchone()
                return (int(row[0]) if row and row[0] is not None else 0), None
            except Exception as exc:                       # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:
                    pass
                return None, f"{source_table}: {type(exc).__name__}"

        try:
            conn = get_db()
            c = conn.cursor()

            stats = {}
            basis = {}
            unavailable = []

            # (key, sql, table-for-provenance, probe)
            #
            # `probe` is (table, column, kind) for every count whose WHERE
            # clause filters on ONE column — the shape where a 0 might be
            # counting rows nothing could ever match. It is checked only when
            # the count comes back 0. An unfiltered COUNT(*) needs no probe:
            # its 0 would mean the table is genuinely empty.
            COUNTS = (
                ('subsea_cables',
                 "SELECT COUNT(*) FROM subsea_cables", 'subsea_cables', None),
                ('subsea_planned',
                 "SELECT COUNT(*) FROM subsea_cables WHERE is_planned = TRUE",
                 'subsea_cables.is_planned',
                 ('subsea_cables', 'is_planned', 'flag')),
                ('landing_points',
                 "SELECT COUNT(*) FROM subsea_landing_points",
                 'subsea_landing_points', None),
                ('major_hubs',
                 "SELECT COUNT(*) FROM subsea_landing_points "
                 "WHERE is_major_hub = TRUE",
                 'subsea_landing_points.is_major_hub',
                 ('subsea_landing_points', 'is_major_hub', 'flag')),
                ('carriers',
                 "SELECT COUNT(*) FROM carrier_profiles", 'carrier_profiles',
                 None),
                ('carrier_facility_links',
                 "SELECT COUNT(*) FROM carrier_facility_presence",
                 'carrier_facility_presence', None),
                ('dchub_facilities_with_carriers',
                 "SELECT COUNT(DISTINCT dchub_facility_id) "
                 "FROM carrier_facility_presence "
                 "WHERE dchub_facility_id IS NOT NULL",
                 'carrier_facility_presence.dchub_facility_id',
                 ('carrier_facility_presence', 'dchub_facility_id', 'flag')),
                # ★ Canonical fiber-route count. Must stay the same table
                # /api/v1/stats reads (main.py:20472) or the two surfaces
                # publish different numbers under one name again.
                ('fiber_routes',
                 "SELECT COUNT(*) FROM fiber_routes", 'fiber_routes', None),
                ('coverage_zones',
                 "SELECT COUNT(*) FROM fiber_coverage_zones",
                 'fiber_coverage_zones', None),
                ('dark_fiber_zones',
                 "SELECT COUNT(*) FROM fiber_coverage_zones "
                 "WHERE dark_fiber_available = TRUE",
                 'fiber_coverage_zones.dark_fiber_available',
                 ('fiber_coverage_zones', 'dark_fiber_available', 'flag')),
                ('legacy_fiber_routes',
                 "SELECT COUNT(*) FROM long_haul_fiber_routes",
                 'long_haul_fiber_routes', None),
                ('legacy_metro_dark_fiber',
                 "SELECT COUNT(*) FROM metro_dark_fiber", 'metro_dark_fiber',
                 None),
            )

            for key, sql, table, probe in COUNTS:
                value, err = _count(c, sql, table)

                # A 0 over a filtered column is only a measurement if the
                # column behind the filter was ever populated. Checked here
                # and not at ingest time because the answer can change under
                # us: a column that starts carrying data must start counting
                # again with no code change.
                if err is None and value == 0 and probe:
                    ok, why = zero_is_measured(c, *probe)
                    if not ok:
                        value, err = None, why

                stats[key] = value          # None when the read failed
                if err:
                    unavailable.append({'field': key, 'reason': err})
                else:
                    basis[key] = table

            return jsonify({
                'success': True,
                'stats': stats,
                # ★ null means "could not read", not "zero". Anything listed
                # in `unavailable` is null above and MUST NOT be rendered as 0.
                'unavailable': unavailable,
                'basis': basis,
                'basis_note': (
                    'Every figure is a live COUNT() at request time, and each '
                    'names the table it came from. A null value is never a '
                    'measured zero: it means either the read failed or the '
                    'column behind the filter was never populated, so a 0 '
                    'would have counted rows nothing could match. Either way '
                    'the field is listed in `unavailable` with the reason. A '
                    '0 that IS published means we looked and found none. '
                    'fiber_routes is the same table '
                    '/api/v1/stats.total_fiber_routes counts, so the two '
                    'surfaces cannot disagree.'),
                'data_sources': [
                    'TeleGeography Submarine Cable Map',
                    'PeeringDB Carrier Database',
                    'FCC Broadband Data Collection',
                    'DC Hub Internal Data',
                ],
                # Was `stats.get('last_sync')` — a key nothing in this function
                # ever set, so it was unconditionally null. Stated honestly
                # instead of implying a sync clock exists.
                'last_sync': None,
                'last_sync_note': (
                    'Not tracked on this endpoint; counts are live at request '
                    'time. Use /api/v1/freshness for ingest recency.'),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    logger.info("🌐 Fiber Intelligence fully registered — subsea, carriers, coverage, sync endpoints")

# === phase 92: source-registry heartbeat (auto-fires on clean module exit) ===
# Non-invasive: never crashes the script if the registry is unreachable.
# Source ID: backend-fiber-integration
_phase92_heartbeat_registered = True
try:
    import atexit as _phase92_atexit
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    def _phase92_emit():
        try:
            _phase92_heartbeat("backend-fiber-integration", status="success",
                              metadata={"trigger": "atexit"})
        except Exception:
            pass
    _phase92_atexit.register(_phase92_emit)
except Exception:
    pass  # heartbeat module unavailable; extractor continues normally
