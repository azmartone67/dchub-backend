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

    # ── Admin Sync Endpoints ──────────────────────────────────

    @app.route('/api/jobs/subsea-sync', methods=['POST'])
    @require_internal_key
    def admin_subsea_sync():
        """Trigger TeleGeography submarine cable data sync."""
        result = run_subsea_sync(get_db)
        return jsonify(result)

    @app.route('/api/jobs/carrier-sync', methods=['POST'])
    @require_internal_key
    def admin_carrier_sync():
        """Trigger PeeringDB carrier-facility data sync."""
        result = run_carrier_sync(get_db)
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
        subsea = run_subsea_sync(get_db)
        results['subsea'] = subsea

        # Carriers + facilities + coverage zones
        carrier = run_carrier_sync(get_db)
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
        """High-level stats for fiber intelligence dashboard."""
        conn = None
        try:
            conn = get_db()
    try:
                c = conn.cursor()

                stats = {}

                # Subsea cables
                try:
                    c.execute("SELECT COUNT(*) FROM subsea_cables")
                    stats['subsea_cables'] = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM subsea_cables WHERE is_planned = TRUE")
                    stats['subsea_planned'] = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM subsea_landing_points")
                    stats['landing_points'] = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM subsea_landing_points WHERE is_major_hub = TRUE")
                    stats['major_hubs'] = c.fetchone()[0]
                except Exception:
                    stats['subsea_cables'] = 0
                    stats['landing_points'] = 0

                # Carriers
                try:
                    c.execute("SELECT COUNT(*) FROM carrier_profiles")
                    stats['carriers'] = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM carrier_facility_presence")
                    stats['carrier_facility_links'] = c.fetchone()[0]
                    c.execute("SELECT COUNT(DISTINCT dchub_facility_id) FROM carrier_facility_presence WHERE dchub_facility_id IS NOT NULL")
                    stats['dchub_facilities_with_carriers'] = c.fetchone()[0]
                except Exception:
                    stats['carriers'] = 0
                    stats['carrier_facility_links'] = 0

                # Fiber routes
                try:
                    c.execute("SELECT COUNT(*) FROM fiber_route_geometry")
                    stats['fiber_routes'] = c.fetchone()[0]
                except Exception:
                    stats['fiber_routes'] = 0

                # Coverage zones
                try:
                    c.execute("SELECT COUNT(*) FROM fiber_coverage_zones")
                    stats['coverage_zones'] = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM fiber_coverage_zones WHERE dark_fiber_available = TRUE")
                    stats['dark_fiber_zones'] = c.fetchone()[0]
                except Exception:
                    stats['coverage_zones'] = 0

                # Existing fiber data (from main tables)
                try:
                    c.execute("SELECT COUNT(*) FROM long_haul_fiber_routes")
                    stats['legacy_fiber_routes'] = c.fetchone()[0]
                except Exception:
                    stats['legacy_fiber_routes'] = 0

                try:
                    c.execute("SELECT COUNT(*) FROM metro_dark_fiber")
                    stats['legacy_metro_dark_fiber'] = c.fetchone()[0]
                except Exception:
                    stats['legacy_metro_dark_fiber'] = 0

                return jsonify({
                    'success': True,
                    'stats': stats,
                    'data_sources': [
                        'TeleGeography Submarine Cable Map',
                        'PeeringDB Carrier Database',
                        'FCC Broadband Data Collection',
                        'DC Hub Internal Data',
                    ],
                    'last_sync': stats.get('last_sync'),
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
            finally:
                if conn:
                    try:

        logger.info("🌐 Fiber Intelligence fully registered — subsea, carriers, coverage, sync endpoints")
    finally:
        conn.close()
