"""
DC Hub Rankings Series API — v3 (Production)
==============================================
Dynamic infrastructure rankings by US state.

Rankings data is FREE — this is viral marketing content for LinkedIn/social.
The analysis tool overlay on the page is pro-gated client-side.

Categories:
  - construction: Pipeline projects under construction (capacity_pipeline + PIPELINE_DATA fallback)
  - power: Operational power capacity (facilities table)
  - gas: Gas pipeline infrastructure (gas_pipelines table, direct query)
  - fiber: Fiber route density (fiber_routes table, lat/lng → state mapping)
"""

from flask import Blueprint, jsonify, request
from datetime import datetime

rankings_bp = Blueprint('rankings', __name__)

# ---------------------------------------------------------------
# Constants
# ---------------------------------------------------------------
STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'Washington DC'
}

VALID_STATE_CODES = set(STATE_NAMES.keys())

MARKET_TO_STATE = {
    'abilene': 'Texas', 'dallas': 'Texas', 'houston': 'Texas', 'austin': 'Texas',
    'san antonio': 'Texas', 'el paso': 'Texas', 'texas': 'Texas',
    'shackelford county': 'Texas',
    'ashburn': 'Virginia', 'richmond': 'Virginia', 'n. virginia': 'Virginia',
    'northern virginia': 'Virginia', 'virginia': 'Virginia',
    'ohio': 'Ohio', 'columbus': 'Ohio',
    'phoenix': 'Arizona', 'mesa': 'Arizona', 'arizona': 'Arizona',
    'atlanta': 'Georgia', 'georgia': 'Georgia',
    'hillsboro': 'Oregon', 'portland': 'Oregon', 'oregon': 'Oregon',
    'new jersey': 'New Jersey',
    'memphis': 'Tennessee', 'nashville': 'Tennessee', 'tennessee': 'Tennessee',
    'denver': 'Colorado', 'colorado': 'Colorado',
    'chicago': 'Illinois', 'illinois': 'Illinois',
    'lauderdale county': 'Mississippi', 'meridian': 'Mississippi', 'mississippi': 'Mississippi',
    'new mexico': 'New Mexico',
    'port washington': 'Wisconsin', 'mount pleasant': 'Wisconsin', 'wisconsin': 'Wisconsin',
    'richland parish': 'Louisiana', 'louisiana': 'Louisiana',
    'kansas city': 'Kansas', 'kansas': 'Kansas',
    'las vegas': 'Nevada', 'reno': 'Nevada', 'nevada': 'Nevada',
    'santa clara': 'California', 'los angeles': 'California', 'silicon valley': 'California',
    'california': 'California',
    'indianapolis': 'Indiana', 'indiana': 'Indiana',
    'iowa': 'Iowa', 'south carolina': 'South Carolina',
    'north carolina': 'North Carolina', 'charlotte': 'North Carolina',
    'maryland': 'Maryland', 'new york': 'New York',
    'salt lake': 'Utah', 'utah': 'Utah', 'pennsylvania': 'Pennsylvania',
    'miami': 'Florida', 'florida': 'Florida', 'alabama': 'Alabama',
    'west memphis': 'Arkansas', 'arkansas': 'Arkansas', 'washington': 'Washington',
}

# Lat/lng bounding boxes for US states (approximate)
STATE_BOUNDS = {
    'AL': (30.2, -88.5, 35.0, -84.9), 'AZ': (31.3, -114.8, 37.0, -109.0),
    'AR': (33.0, -94.6, 36.5, -89.6), 'CA': (32.5, -124.4, 42.0, -114.1),
    'CO': (37.0, -109.1, 41.0, -102.0), 'CT': (41.0, -73.7, 42.1, -71.8),
    'DE': (38.5, -75.8, 39.8, -75.0), 'FL': (24.5, -87.6, 31.0, -80.0),
    'GA': (30.4, -85.6, 35.0, -80.8), 'ID': (42.0, -117.2, 49.0, -111.0),
    'IL': (37.0, -91.5, 42.5, -87.0), 'IN': (37.8, -88.1, 41.8, -84.8),
    'IA': (40.4, -96.6, 43.5, -90.1), 'KS': (37.0, -102.1, 40.0, -94.6),
    'KY': (36.5, -89.6, 39.1, -82.0), 'LA': (29.0, -94.0, 33.0, -89.0),
    'ME': (43.1, -71.1, 47.5, -67.0), 'MD': (38.0, -79.5, 39.7, -75.0),
    'MA': (41.2, -73.5, 42.9, -69.9), 'MI': (41.7, -90.4, 48.3, -82.1),
    'MN': (43.5, -97.2, 49.4, -89.5), 'MS': (30.2, -91.7, 35.0, -88.1),
    'MO': (36.0, -95.8, 40.6, -89.1), 'MT': (44.4, -116.0, 49.0, -104.0),
    'NE': (40.0, -104.1, 43.0, -95.3), 'NV': (35.0, -120.0, 42.0, -114.0),
    'NH': (42.7, -72.6, 45.3, -70.7), 'NJ': (38.9, -75.6, 41.4, -73.9),
    'NM': (31.3, -109.1, 37.0, -103.0), 'NY': (40.5, -79.8, 45.0, -71.9),
    'NC': (33.8, -84.3, 36.6, -75.5), 'ND': (45.9, -104.0, 49.0, -96.6),
    'OH': (38.4, -84.8, 42.0, -80.5), 'OK': (33.6, -103.0, 37.0, -94.4),
    'OR': (42.0, -124.6, 46.3, -116.5), 'PA': (39.7, -80.5, 42.3, -74.7),
    'RI': (41.1, -71.9, 42.0, -71.1), 'SC': (32.0, -83.4, 35.2, -78.5),
    'SD': (42.5, -104.1, 46.0, -96.4), 'TN': (35.0, -90.3, 36.7, -81.6),
    'TX': (25.8, -106.6, 36.5, -93.5), 'UT': (37.0, -114.1, 42.0, -109.0),
    'VT': (42.7, -73.4, 45.0, -71.5), 'VA': (36.5, -83.7, 39.5, -75.2),
    'WA': (45.5, -124.8, 49.0, -116.9), 'WV': (37.2, -82.6, 40.6, -77.7),
    'WI': (42.5, -92.9, 47.1, -86.8), 'WY': (41.0, -111.1, 45.0, -104.1),
}


def _resolve_state(market_str):
    if not market_str:
        return None
    market_lower = market_str.lower().strip()
    if market_lower in MARKET_TO_STATE:
        return MARKET_TO_STATE[market_lower]
    for key, state in MARKET_TO_STATE.items():
        if key in market_lower:
            return state
    return None


def _lat_lng_to_state(lat, lng):
    if lat is None or lng is None:
        return None
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    for code in ["DC", "VA", "MD"] + [k for k in STATE_BOUNDS if k not in ("DC", "VA", "MD")]:
        min_lat, min_lng, max_lat, max_lng = STATE_BOUNDS[code]
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return code
    return None


def _register_rankings_routes(rankings_bp, db_pool=None, get_db_connection=None, require_plan=None):

    def get_conn():
        if get_db_connection:
            return get_db_connection()
        if db_pool:
            return db_pool.getconn()
        raise Exception("No database connection available")

    def release_conn(conn):
        if db_pool:
            try: db_pool.putconn(conn)
            except Exception: pass
        elif get_db_connection:
            try: conn.close()
            except Exception: pass

    # ---------------------------------------------------------------
    # Category index
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings', methods=['GET'])
    def rankings_index():
        categories = [
            {"id": "construction", "title": "Data Centers Under Construction", "subtitle": "Pipeline projects by US state", "endpoint": "/api/rankings/construction", "update_frequency": "daily"},
            {"id": "power", "title": "Data Center Power Capacity", "subtitle": "Operational MW by US state", "endpoint": "/api/rankings/power", "update_frequency": "daily"},
            {"id": "gas", "title": "Gas Pipeline Infrastructure", "subtitle": "Pipeline segments by US state", "endpoint": "/api/rankings/gas", "update_frequency": "weekly"},
            {"id": "fiber", "title": "Fiber Network Density", "subtitle": "Fiber routes by US state", "endpoint": "/api/rankings/fiber", "update_frequency": "weekly"},
        ]
        return jsonify({
            "success": True, "categories": categories,
            "series_name": "DC Hub Rankings Series",
            "source": "DC Hub | dchub.cloud",
            "generated_at": datetime.utcnow().isoformat()
        })

    # ---------------------------------------------------------------
    # Construction Rankings
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/construction', methods=['GET'])
    def rankings_construction():
        limit = min(int(request.args.get('limit', 25)), 50)
        conn = None
        results = []
        data_source = None

        # Attempt 1: capacity_pipeline table
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT operator, market, capacity_mw, status
                FROM capacity_pipeline
                WHERE LOWER(status) IN ('construction', 'under_construction', 'under construction')
                ORDER BY capacity_mw DESC
            """)
            rows = cur.fetchall()
            cur.close()

            if rows and len(rows) >= 5:
                state_data = {}
                for operator, market, capacity_mw, status in rows:
                    state = _resolve_state(market)
                    if not state:
                        continue
                    if state not in state_data:
                        state_data[state] = {'project_count': 0, 'total_mw': 0, 'operators': set()}
                    state_data[state]['project_count'] += 1
                    state_data[state]['total_mw'] += float(capacity_mw or 0)
                    if operator:
                        state_data[state]['operators'].add(operator)

                sorted_states = sorted(state_data.items(), key=lambda x: x[1]['total_mw'], reverse=True)[:limit]
                for rank, (state_name, data) in enumerate(sorted_states, 1):
                    results.append({
                        'rank': rank, 'state_name': state_name,
                        'project_count': data['project_count'],
                        'total_mw': round(data['total_mw']),
                        'operators': list(data['operators'])
                    })
                data_source = 'capacity_pipeline_table'
        except Exception as e:
            print(f"⚠️ Rankings: capacity_pipeline failed: {e}")
        finally:
            if conn: release_conn(conn)

        # Attempt 2: PIPELINE_DATA fallback
        if not results:
            try:
                from routes.deals_routes import PIPELINE_DATA
                state_data = {}
                for project in PIPELINE_DATA:
                    if project.get('status') != 'construction':
                        continue
                    state = _resolve_state(project.get('market', ''))
                    if not state:
                        continue
                    if state not in state_data:
                        state_data[state] = {'project_count': 0, 'total_mw': 0, 'total_investment_millions': 0, 'operators': set(), 'projects': []}
                    state_data[state]['project_count'] += 1
                    state_data[state]['total_mw'] += float(project.get('capacity', 0))
                    state_data[state]['total_investment_millions'] += float(project.get('investment', 0))
                    if project.get('company'):
                        state_data[state]['operators'].add(project['company'])
                    state_data[state]['projects'].append(project.get('project', ''))

                sorted_states = sorted(state_data.items(), key=lambda x: x[1]['total_mw'], reverse=True)[:limit]
                for rank, (state_name, data) in enumerate(sorted_states, 1):
                    results.append({
                        'rank': rank, 'state_name': state_name,
                        'project_count': data['project_count'],
                        'total_mw': round(data['total_mw']),
                        'total_investment_millions': round(data['total_investment_millions']),
                        'operators': list(data['operators']),
                        'projects': data['projects']
                    })
                data_source = 'pipeline_data_hardcoded'
            except Exception as e2:
                return jsonify({"success": False, "error": f"No pipeline data: {e2}"}), 500

        total_projects = sum(r['project_count'] for r in results)
        total_mw = sum(r['total_mw'] for r in results)

        return jsonify({
            "success": True, "category": "construction",
            "title": "Data Centers Under Construction",
            "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
            "metric_label": "Pipeline MW Under Construction",
            "primary_metric": "total_mw", "secondary_metric": "project_count",
            "rankings": results,
            "summary": {"total_states": len(results), "total_projects": total_projects, "total_mw": float(total_mw)},
            "data_source": data_source,
            "source": "DC Hub | dchub.cloud",
            "generated_at": datetime.utcnow().isoformat(),
            "methodology": "Aggregated from DC Hub pipeline tracking. Projects with status 'under construction' grouped by US state, ranked by total MW."
        })

    # ---------------------------------------------------------------
    # Power Capacity Rankings
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/power', methods=['GET'])
    def rankings_power():
        country = request.args.get('country', 'US').upper()
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    state,
                    COUNT(*) as facility_count,
                    ROUND(COALESCE(SUM(power_mw), 0)::numeric, 1) as total_mw,
                    ROUND(COALESCE(AVG(power_mw), 0)::numeric, 1) as avg_mw_per_facility,
                    ROUND(COALESCE(MAX(power_mw), 0)::numeric, 1) as max_facility_mw,
                    COUNT(DISTINCT provider) as provider_count
                FROM facilities
                WHERE country = %s
                  AND state IS NOT NULL AND state != ''
                  AND LENGTH(state) = 2
                  AND LOWER(COALESCE(status, 'operational')) NOT IN ('decommissioned', 'closed')
                GROUP BY state
                ORDER BY total_mw DESC
                LIMIT %s
            """, (country, limit))

            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            results = []
            for rank, row in enumerate(rows, 1):
                entry = dict(zip(cols, row))
                entry['rank'] = rank
                entry['state_name'] = STATE_NAMES.get(entry['state'], entry['state'])
                results.append(entry)
            cur.close()

            return jsonify({
                "success": True, "category": "power",
                "title": "Data Center Power Capacity",
                "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                "metric_label": "Total Operational MW",
                "primary_metric": "total_mw", "secondary_metric": "facility_count",
                "rankings": results,
                "summary": {
                    "total_states": len(results),
                    "total_facilities": sum(r['facility_count'] for r in results),
                    "total_mw": float(sum(r['total_mw'] for r in results)),
                },
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from DC Hub facility database. Operational facilities grouped by US state."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn: release_conn(conn)

    # ---------------------------------------------------------------
    # Gas Pipeline Rankings — direct query, no schema discovery
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/gas', methods=['GET'])
    def rankings_gas():
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            cur.execute("""
                SELECT 
                    state,
                    COUNT(*) as pipeline_count,
                    COUNT(DISTINCT operator) as operator_count
                FROM gas_pipelines
                WHERE state IS NOT NULL AND state != ''
                  AND LENGTH(state) = 2
                  AND state NOT IN ('GOM', 'GM', 'OCS')
                GROUP BY state
                ORDER BY pipeline_count DESC
                LIMIT %s
            """, (limit,))

            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            results = []
            for rank, row in enumerate(rows, 1):
                entry = dict(zip(cols, row))
                entry['rank'] = rank
                entry['state_name'] = STATE_NAMES.get(entry.get('state', ''), entry.get('state', ''))
                results.append(entry)
            cur.close()

            return jsonify({
                "success": True, "category": "gas",
                "title": "Gas Pipeline Infrastructure",
                "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                "metric_label": "Pipeline Segments",
                "primary_metric": "pipeline_count", "secondary_metric": "operator_count",
                "rankings": results,
                "summary": {
                    "total_states": len(results),
                    "total_pipelines": sum(r['pipeline_count'] for r in results),
                },
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from EIA natural gas pipeline data. Transmission pipeline segments grouped by US state."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn: release_conn(conn)

    # ---------------------------------------------------------------
    # Fiber Rankings — lat/lng → state mapping, no schema discovery
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/fiber', methods=['GET'])
    def rankings_fiber():
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            cur.execute("""
                SELECT start_lat, start_lng, provider
                FROM fiber_routes
                WHERE start_lat IS NOT NULL AND start_lng IS NOT NULL
            """)
            rows = cur.fetchall()
            cur.close()

            # Map lat/lng to states in Python
            state_data = {}
            for lat, lng, provider in rows:
                state_code = _lat_lng_to_state(lat, lng)
                if not state_code or state_code not in VALID_STATE_CODES:
                    continue
                if state_code not in state_data:
                    state_data[state_code] = {'route_count': 0, 'providers': set()}
                state_data[state_code]['route_count'] += 1
                if provider:
                    state_data[state_code]['providers'].add(provider)

            sorted_states = sorted(state_data.items(), key=lambda x: x[1]['route_count'], reverse=True)[:limit]

            results = []
            for rank, (code, data) in enumerate(sorted_states, 1):
                results.append({
                    'rank': rank,
                    'state': code,
                    'state_name': STATE_NAMES.get(code, code),
                    'route_count': data['route_count'],
                    'provider_count': len(data['providers']),
                })

            return jsonify({
                "success": True, "category": "fiber",
                "title": "Fiber Network Density",
                "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                "metric_label": "Fiber Routes",
                "primary_metric": "route_count", "secondary_metric": "provider_count",
                "rankings": results,
                "summary": {
                    "total_states": len(results),
                    "total_routes": sum(r['route_count'] for r in results),
                },
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from DC Hub fiber route discovery data. Routes mapped to states via coordinates."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn: release_conn(conn)

    return rankings_bp
