"""
Grid Intelligence Briefs — API Routes
======================================
Endpoint: GET /api/v1/grid-intelligence/<region_id>
          GET /api/v1/grid-intelligence (list all regions)

Aggregates data from existing DC Hub tables:
  - grid_regions + grid_corridors (new tables)
  - eia_retail_rates (energy pricing)
  - discovered_facilities (facility counts)
  - substations, transmission_lines_eia, gas_pipelines, power_plants_eia (infrastructure)
  - tax_incentives_neon (incentives)
  - fema_risk_index (risk data)
  - epa_egrid (carbon data)

Tier gating:
  - Free: region summary + 2 corridor headlines + redacted scores + upgrade CTA
  - Developer ($49/mo): all corridors + aggregate scores + energy rates + infra counts
  - Pro ($99/mo): full sub-scores + facility names + coordinates + CSV export
"""

import json
import traceback
from flask import Blueprint, request, jsonify

grid_intel_bp = Blueprint('grid_intel', __name__)


# ─── Tier gating constants ───
GRID_INTEL_TIER_CONFIG = {
    'free': {
        'max_corridors': 2,
        'show_scores': False,
        'show_infra_details': False,
        'show_facility_names': False,
        'show_coordinates': False,
        'show_energy_rates': False,
        'show_tax_incentives': False,
    },
    'developer': {
        'max_corridors': 99,
        'show_scores': True,
        'show_infra_details': True,
        'show_facility_names': False,
        'show_coordinates': False,
        'show_energy_rates': True,
        'show_tax_incentives': True,
    },
    'pro': {
        'max_corridors': 99,
        'show_scores': True,
        'show_infra_details': True,
        'show_facility_names': True,
        'show_coordinates': True,
        'show_energy_rates': True,
        'show_tax_incentives': True,
    },
    'enterprise': {
        'max_corridors': 99,
        'show_scores': True,
        'show_infra_details': True,
        'show_facility_names': True,
        'show_coordinates': True,
        'show_energy_rates': True,
        'show_tax_incentives': True,
    }
}


def _get_conn():
    """Get a database connection from the pool or direct."""
    import os
    import psycopg2
    db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    return psycopg2.connect(db_url)


def _determine_tier(api_key):
    """Determine user tier from API key. Returns tier string."""
    if not api_key:
        return 'free'
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT u.plan FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key = %s AND ak.is_active = true
        """, (api_key,))
        row = cur.fetchone()
        if row:
            plan = (row[0] or 'free').lower()
            # Map plan names to tier names
            if plan in ('pro', 'enterprise'):
                return plan
            elif plan in ('developer', 'dev'):
                return 'developer'
            else:
                return 'free'
        return 'free'
    except Exception:
        return 'free'
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_infra_counts(lat, lon, radius_km=50, conn=None):
    """Get infrastructure counts near a corridor point."""
    counts = {
        'substations': 0,
        'transmission_lines': 0,
        'power_plants': 0,
        'gas_pipelines': 0,
    }
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()

        # Substations within radius (using simple distance approximation)
        cur.execute("""
            SELECT COUNT(*) FROM substations
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            AND ABS(latitude - %s) < %s AND ABS(longitude - %s) < %s
        """, (lat, radius_km / 111.0, lon, radius_km / (111.0 * 0.85)))
        row = cur.fetchone()
        counts['substations'] = row[0] if row else 0

        # Transmission lines
        try:
            cur.execute("""
                SELECT COUNT(*) FROM transmission_lines_eia
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                AND ABS(latitude - %s) < %s AND ABS(longitude - %s) < %s
            """, (lat, radius_km / 111.0, lon, radius_km / (111.0 * 0.85)))
            row = cur.fetchone()
            counts['transmission_lines'] = row[0] if row else 0
        except Exception:
            pass

        # Power plants
        try:
            cur.execute("""
                SELECT COUNT(*) FROM power_plants_eia
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                AND ABS(latitude - %s) < %s AND ABS(longitude - %s) < %s
            """, (lat, radius_km / 111.0, lon, radius_km / (111.0 * 0.85)))
            row = cur.fetchone()
            counts['power_plants'] = row[0] if row else 0
        except Exception:
            pass

        # Gas pipelines
        try:
            cur.execute("""
                SELECT COUNT(*) FROM gas_pipelines
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                AND ABS(latitude - %s) < %s AND ABS(longitude - %s) < %s
            """, (lat, radius_km / 111.0, lon, radius_km / (111.0 * 0.85)))
            row = cur.fetchone()
            counts['gas_pipelines'] = row[0] if row else 0
        except Exception:
            pass

        return counts
    except Exception:
        return counts
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_energy_rates(states, conn=None):
    """Get average industrial energy rate for a list of states."""
    rates = {}
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()
        for st in states:
            cur.execute("""
                SELECT AVG(rate_cents_kwh) FROM eia_retail_rates
                WHERE (state_abbr = %s OR state_name ILIKE %s)
                AND sector = 'industrial'
            """, (st, f'%{st}%'))
            row = cur.fetchone()
            if row and row[0]:
                rates[st] = round(float(row[0]), 2)
        return rates
    except Exception:
        return rates
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_facility_count(state, conn=None):
    """Get facility count for a state."""
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM discovered_facilities
            WHERE state = %s OR state_full ILIKE %s
        """, (state, f'%{state}%'))
        row = cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_tax_incentives(states, conn=None):
    """Get tax incentives for states."""
    incentives = {}
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()
        for st in states:
            cur.execute("""
                SELECT state_name, sales_tax_exempt, property_tax_abatement,
                       data_center_specific, qualifying_investment, incentive_details
                FROM tax_incentives_neon
                WHERE state_abbr = %s
            """, (st,))
            row = cur.fetchone()
            if row:
                incentives[st] = {
                    'state_name': row[0],
                    'sales_tax_exempt': row[1],
                    'property_tax_abatement': row[2],
                    'data_center_specific': row[3],
                    'qualifying_investment': row[4],
                    'summary': row[5][:200] + '...' if row[5] and len(row[5]) > 200 else row[5]
                }
        return incentives
    except Exception:
        return incentives
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


# ─── List all regions ───
@grid_intel_bp.route('/api/v1/grid-intelligence', methods=['GET'])
def list_grid_regions():
    """List all grid intelligence regions with basic info."""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, iso, status, headline, description,
                   key_states, total_queue_gw, page_url, sort_order
            FROM grid_regions
            ORDER BY sort_order
        """)
        rows = cur.fetchall()

        regions = []
        for r in rows:
            # Get corridor count
            cur.execute("SELECT COUNT(*) FROM grid_corridors WHERE region_id = %s", (r[0],))
            corridor_count = cur.fetchone()[0]

            regions.append({
                'id': r[0],
                'name': r[1],
                'iso': r[2],
                'status': r[3],
                'headline': r[4],
                'description': r[5],
                'key_states': r[6],
                'total_queue_gw': float(r[7]) if r[7] else None,
                'page_url': r[8],
                'corridor_count': corridor_count,
            })

        return jsonify({
            'success': True,
            'regions': regions,
            'total': len(regions),
            'source': 'DC Hub Grid Intelligence',
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─── Get single region with full data ───
@grid_intel_bp.route('/api/v1/grid-intelligence/<region_id>', methods=['GET'])
def get_grid_region(region_id):
    """
    Get full grid intelligence for a region.
    Tier-gated: free sees headlines, developer sees scores, pro sees everything.
    """
    conn = None
    try:
        # Determine tier
        api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
        tier = _determine_tier(api_key)
        tier_config = GRID_INTEL_TIER_CONFIG.get(tier, GRID_INTEL_TIER_CONFIG['free'])

        conn = _get_conn()
        cur = conn.cursor()

        # Get region
        cur.execute("""
            SELECT id, name, iso, status, headline, description,
                   key_states, total_queue_gw, page_url
            FROM grid_regions WHERE id = %s
        """, (region_id,))
        region_row = cur.fetchone()

        if not region_row:
            return jsonify({'success': False, 'error': f'Region "{region_id}" not found'}), 404

        key_states = region_row[6] or []
        region = {
            'id': region_row[0],
            'name': region_row[1],
            'iso': region_row[2],
            'status': region_row[3],
            'headline': region_row[4],
            'description': region_row[5],
            'key_states': key_states,
            'total_queue_gw': float(region_row[7]) if region_row[7] else None,
            'page_url': region_row[8],
        }

        # Get corridors
        cur.execute("""
            SELECT label, lat, lon, state, utility, queue_gw, system_peak_gw,
                   congestion_level, transmission_capacity, excess_generation,
                   dilution_risk, notes, sort_order
            FROM grid_corridors
            WHERE region_id = %s
            ORDER BY sort_order
        """, (region_id,))
        corridor_rows = cur.fetchall()

        # Apply tier gating to corridors
        max_corridors = tier_config['max_corridors']
        total_corridors = len(corridor_rows)

        corridors = []
        for i, c in enumerate(corridor_rows):
            if i >= max_corridors:
                break

            corridor = {
                'label': c[0],
                'state': c[3],
                'utility': c[4],
                'congestion_level': c[7],
                'dilution_risk': c[10],
            }

            # Queue / peak data (always visible — these are public numbers)
            corridor['queue_gw'] = float(c[5]) if c[5] else None
            corridor['system_peak_gw'] = float(c[6]) if c[6] else None
            corridor['transmission_capacity'] = c[8]
            corridor['excess_generation'] = c[9]

            # Scores + infrastructure (developer+)
            if tier_config['show_scores']:
                lat, lon = float(c[1]), float(c[2])
                infra = _get_infra_counts(lat, lon, radius_km=50, conn=conn)
                corridor['infrastructure'] = infra
                corridor['notes'] = c[11]

                # Queue/peak ratio
                if c[5] and c[6] and float(c[6]) > 0:
                    corridor['queue_peak_ratio'] = round(float(c[5]) / float(c[6]), 1)
            else:
                corridor['infrastructure'] = '██ upgrade to see'
                corridor['notes'] = '██ upgrade to see'

            # Coordinates (pro+)
            if tier_config['show_coordinates']:
                corridor['lat'] = float(c[1])
                corridor['lon'] = float(c[2])

            corridors.append(corridor)

        # Energy rates (developer+)
        energy_rates = {}
        if tier_config['show_energy_rates']:
            energy_rates = _get_energy_rates(key_states, conn=conn)
        else:
            energy_rates = {st: '██ upgrade to see' for st in key_states}

        # Tax incentives (developer+)
        tax_incentives = {}
        if tier_config['show_tax_incentives']:
            tax_incentives = _get_tax_incentives(key_states, conn=conn)
        else:
            tax_incentives = {st: '██ upgrade to see' for st in key_states}

        # Facility counts per state (always visible as aggregate)
        facility_counts = {}
        for st in key_states:
            facility_counts[st] = _get_facility_count(st, conn=conn)

        # Build response
        response = {
            'success': True,
            'tier': tier,
            'region': region,
            'corridors': corridors,
            'total_corridors': total_corridors,
            'energy_rates_cents_kwh': energy_rates,
            'tax_incentives': tax_incentives,
            'facility_counts_by_state': facility_counts,
            'data_sources': [
                'HIFLD (substations)',
                'EIA (transmission, power plants, gas pipelines, retail rates)',
                'EPA eGRID (carbon intensity)',
                'FEMA (risk index)',
                'USGS (water stress)',
                'DC Hub (facilities, market intel)',
            ],
        }

        # Upgrade CTA for free/developer
        if tier == 'free':
            response['_upgrade'] = {
                'message': f'Showing {min(max_corridors, total_corridors)} of {total_corridors} corridors with limited data. Developer plan ($49/mo) unlocks all corridors, scores, energy rates, and infrastructure counts.',
                'url': 'https://dchub.cloud/pricing#developer',
                'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
                'corridors_hidden': max(0, total_corridors - max_corridors),
            }
        elif tier == 'developer':
            response['_upgrade'] = {
                'message': 'Developer plan active. Upgrade to Pro ($99/mo) for facility names, exact coordinates, and CSV export.',
                'url': 'https://dchub.cloud/pricing#pro',
            }

        return jsonify(response)

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def register_grid_intel_routes(app):
    """Register grid intelligence routes with the Flask app."""
    app.register_blueprint(grid_intel_bp)
    print("[grid_intel] Registered /api/v1/grid-intelligence routes")
