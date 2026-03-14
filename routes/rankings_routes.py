"""
DC Hub Rankings Series API
===========================
Dynamic, always-current infrastructure rankings by US state.

Categories:
  - construction: Pipeline projects under construction by state (count + MW)
  - power: Total operational power capacity by state (MW)
  - gas: Gas pipeline infrastructure by state (miles of pipeline)
  - fiber: Fiber route density by state (route count)

Endpoints:
  GET /api/rankings/<category>
    Query params:
      - country: ISO code (default 'US')
      - limit: Number of states to return (default 25)
    
  GET /api/rankings
    Returns list of available ranking categories with metadata

Blueprint: rankings_bp
"""

from flask import Blueprint, jsonify, request
from datetime import datetime

rankings_bp = Blueprint('rankings', __name__)

# ---------------------------------------------------------------
# State mapping helper
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

# Market string -> State name mapping for pipeline data
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
    'iowa': 'Iowa',
    'south carolina': 'South Carolina',
    'north carolina': 'North Carolina', 'charlotte': 'North Carolina',
    'maryland': 'Maryland',
    'new york': 'New York',
    'salt lake': 'Utah', 'utah': 'Utah',
    'pennsylvania': 'Pennsylvania',
    'miami': 'Florida', 'florida': 'Florida',
    'alabama': 'Alabama',
    'west memphis': 'Arkansas', 'arkansas': 'Arkansas',
    'washington': 'Washington',
}


def _resolve_state(market_str):
    """Resolve a market string to a US state name."""
    if not market_str:
        return None
    market_lower = market_str.lower().strip()
    # Try exact match first
    if market_lower in MARKET_TO_STATE:
        return MARKET_TO_STATE[market_lower]
    # Try substring match
    for key, state in MARKET_TO_STATE.items():
        if key in market_lower:
            return state
    return None


def _register_rankings_routes(rankings_bp, db_pool=None, get_db_connection=None, require_plan=None):
    """
    Late-binding route registration following Phase 2 pattern.
    Called from main.py with injected dependencies.
    """

    def get_conn():
        """Get a database connection from available sources."""
        if get_db_connection:
            return get_db_connection()
        if db_pool:
            return db_pool.getconn()
        raise Exception("No database connection available")

    def release_conn(conn):
        """Release connection back to pool."""
        if db_pool:
            try:
                db_pool.putconn(conn)
            except Exception:
                pass
        elif get_db_connection:
            try:
                conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------------
    # Category index
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings', methods=['GET'])
    def rankings_index():
        """List available ranking categories."""
        categories = [
            {
                "id": "construction",
                "title": "Data Centers Under Construction",
                "subtitle": "Pipeline projects by US state",
                "description": "Number of data center projects currently under construction, ranked by state. Includes total MW capacity and investment.",
                "metrics": ["project_count", "total_mw", "total_investment_millions"],
                "update_frequency": "daily",
                "endpoint": "/api/rankings/construction"
            },
            {
                "id": "power",
                "title": "Data Center Power Capacity",
                "subtitle": "Operational MW by US state",
                "description": "Total operational data center power capacity in megawatts, ranked by state.",
                "metrics": ["facility_count", "total_mw", "avg_mw_per_facility"],
                "update_frequency": "daily",
                "endpoint": "/api/rankings/power"
            },
            {
                "id": "gas",
                "title": "Gas Pipeline Infrastructure",
                "subtitle": "Pipeline miles by US state",
                "description": "Natural gas transmission pipeline infrastructure density, ranked by state. Critical for behind-the-meter power generation.",
                "metrics": ["pipeline_count", "total_miles"],
                "update_frequency": "weekly",
                "endpoint": "/api/rankings/gas"
            },
            {
                "id": "fiber",
                "title": "Fiber Network Density",
                "subtitle": "Fiber routes by US state",
                "description": "Fiber optic network route density, ranked by state. Key connectivity indicator for data center site selection.",
                "metrics": ["route_count", "provider_count"],
                "update_frequency": "weekly",
                "endpoint": "/api/rankings/fiber"
            }
        ]
        return jsonify({
            "success": True,
            "categories": categories,
            "series_name": "DC Hub Rankings Series",
            "source": "DC Hub | dchub.cloud",
            "generated_at": datetime.utcnow().isoformat()
        })

    # ---------------------------------------------------------------
    # Construction Rankings
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/construction', methods=['GET'])
    def rankings_construction():
        """
        Rank US states by data center construction activity.
        Strategy: Try capacity_pipeline table first, fall back to PIPELINE_DATA from deals_routes.
        """
        limit = min(int(request.args.get('limit', 25)), 50)
        
        conn = None
        results = []
        data_source = None
        
        # --- Attempt 1: Query capacity_pipeline table ---
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
                # Aggregate by state
                state_data = {}
                for operator, market, capacity_mw, status in rows:
                    state = _resolve_state(market)
                    if not state:
                        continue
                    if state not in state_data:
                        state_data[state] = {'project_count': 0, 'total_mw': 0, 'operators': set(), 'projects': []}
                    state_data[state]['project_count'] += 1
                    state_data[state]['total_mw'] += float(capacity_mw or 0)
                    if operator:
                        state_data[state]['operators'].add(operator)
                
                sorted_states = sorted(state_data.items(), key=lambda x: x[1]['total_mw'], reverse=True)[:limit]
                
                for rank, (state_name, data) in enumerate(sorted_states, 1):
                    results.append({
                        'rank': rank,
                        'state_name': state_name,
                        'project_count': data['project_count'],
                        'total_mw': round(data['total_mw']),
                        'operators': list(data['operators'])
                    })
                data_source = 'capacity_pipeline_table'
                
        except Exception as e:
            print(f"⚠️ Rankings: capacity_pipeline query failed: {e}")
        finally:
            if conn:
                release_conn(conn)
        
        # --- Attempt 2: Fall back to PIPELINE_DATA from deals_routes ---
        if not results:
            try:
                from routes.deals_routes import PIPELINE_DATA
                
                state_data = {}
                for project in PIPELINE_DATA:
                    if project.get('status') != 'construction':
                        continue
                    market = project.get('market', '')
                    state = _resolve_state(market)
                    if not state:
                        continue
                    if state not in state_data:
                        state_data[state] = {
                            'project_count': 0, 'total_mw': 0,
                            'total_investment_millions': 0,
                            'operators': set(), 'projects': []
                        }
                    state_data[state]['project_count'] += 1
                    state_data[state]['total_mw'] += float(project.get('capacity', 0))
                    state_data[state]['total_investment_millions'] += float(project.get('investment', 0))
                    if project.get('company'):
                        state_data[state]['operators'].add(project['company'])
                    state_data[state]['projects'].append(project.get('project', ''))
                
                sorted_states = sorted(state_data.items(), key=lambda x: x[1]['total_mw'], reverse=True)[:limit]
                
                for rank, (state_name, data) in enumerate(sorted_states, 1):
                    results.append({
                        'rank': rank,
                        'state_name': state_name,
                        'project_count': data['project_count'],
                        'total_mw': round(data['total_mw']),
                        'total_investment_millions': round(data['total_investment_millions']),
                        'operators': list(data['operators']),
                        'projects': data['projects']
                    })
                data_source = 'pipeline_data_hardcoded'
                
            except Exception as e2:
                print(f"⚠️ Rankings: PIPELINE_DATA fallback also failed: {e2}")
                return jsonify({"success": False, "error": "No pipeline data source available"}), 500

        total_projects = sum(r['project_count'] for r in results)
        total_mw = sum(r['total_mw'] for r in results)

        return jsonify({
            "success": True,
            "category": "construction",
            "title": "Data Centers Under Construction",
            "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
            "metric_label": "Pipeline MW Under Construction",
            "primary_metric": "total_mw",
            "secondary_metric": "project_count",
            "rankings": results,
            "summary": {
                "total_states": len(results),
                "total_projects": total_projects,
                "total_mw": float(total_mw),
            },
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
        """
        Rank US states by total operational data center power capacity.
        Uses facilities table.
        """
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
                  AND state IS NOT NULL
                  AND state != ''
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
                "success": True,
                "category": "power",
                "title": "Data Center Power Capacity",
                "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                "metric_label": "Total Operational MW",
                "primary_metric": "total_mw",
                "secondary_metric": "facility_count",
                "rankings": results,
                "summary": {
                    "total_states": len(results),
                    "total_facilities": sum(r['facility_count'] for r in results),
                    "total_mw": float(sum(r['total_mw'] for r in results)),
                },
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from DC Hub facility database. Non-duplicate, operational facilities with power capacity data, grouped by US state."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                release_conn(conn)

    # ---------------------------------------------------------------
    # Gas Pipeline Rankings
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/gas', methods=['GET'])
    def rankings_gas():
        """
        Rank US states by gas pipeline infrastructure.
        Tries gas_pipelines table, then eia_gas_pipelines.
        """
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            # Try to find the right gas table
            table_name = None
            columns = []
            for tbl in ['gas_pipelines', 'eia_gas_pipelines', 'gas_pipeline_data']:
                try:
                    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position", (tbl,))
                    cols = [r[0] for r in cur.fetchall()]
                    if cols:
                        table_name = tbl
                        columns = cols
                        break
                except Exception:
                    continue

            if not table_name:
                cur.close()
                return jsonify({
                    "success": True,
                    "category": "gas",
                    "title": "Gas Pipeline Infrastructure",
                    "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                    "rankings": [],
                    "note": "Gas pipeline ranking data is being compiled. Check back soon.",
                    "source": "DC Hub | dchub.cloud",
                    "generated_at": datetime.utcnow().isoformat()
                })

            state_col = next((c for c in ['state', 'state_name', 'state_code'] if c in columns), None)
            miles_col = next((c for c in ['miles', 'length_miles', 'shape_length', 'total_miles'] if c in columns), None)
            operator_col = next((c for c in ['operator', 'name', 'pipeline_name', 'company'] if c in columns), None)

            if not state_col:
                cur.close()
                return jsonify({
                    "success": True, "category": "gas",
                    "title": "Gas Pipeline Infrastructure",
                    "rankings": [],
                    "note": f"Table '{table_name}' found but missing state column. Columns: {columns}",
                    "source": "DC Hub | dchub.cloud",
                    "generated_at": datetime.utcnow().isoformat()
                })

            miles_expr = f"COALESCE(SUM({miles_col}), 0)" if miles_col else "COUNT(*)"
            operator_expr = f"COUNT(DISTINCT {operator_col})" if operator_col else "0"

            cur.execute(f"""
                SELECT 
                    {state_col} as state,
                    COUNT(*) as pipeline_count,
                    ROUND(({miles_expr})::numeric, 0) as total_miles,
                    {operator_expr} as operator_count
                FROM {table_name}
                WHERE {state_col} IS NOT NULL AND {state_col} != ''
                GROUP BY {state_col}
                ORDER BY total_miles DESC
                LIMIT %s
            """, (limit,))

            rows = cur.fetchall()
            cols_out = [desc[0] for desc in cur.description]

            results = []
            for rank, row in enumerate(rows, 1):
                entry = dict(zip(cols_out, row))
                entry['rank'] = rank
                entry['state_name'] = STATE_NAMES.get(entry.get('state', ''), entry.get('state', ''))
                results.append(entry)

            cur.close()

            return jsonify({
                "success": True,
                "category": "gas",
                "title": "Gas Pipeline Infrastructure",
                "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                "metric_label": "Pipeline Miles",
                "primary_metric": "total_miles",
                "secondary_metric": "pipeline_count",
                "rankings": results,
                "summary": {
                    "total_states": len(results),
                    "total_pipelines": sum(r['pipeline_count'] for r in results),
                    "total_miles": float(sum(r['total_miles'] for r in results)),
                },
                "data_table": table_name,
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from EIA natural gas pipeline data. Transmission pipelines grouped by US state."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                release_conn(conn)

    # ---------------------------------------------------------------
    # Fiber Rankings
    # ---------------------------------------------------------------
    @rankings_bp.route('/api/rankings/fiber', methods=['GET'])
    def rankings_fiber():
        """
        Rank US states by fiber network density.
        Tries fiber_routes, then fiber_data.
        """
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            table_name = None
            columns = []
            for tbl in ['fiber_routes', 'fiber_data', 'fiber_infrastructure']:
                try:
                    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position", (tbl,))
                    cols = [r[0] for r in cur.fetchall()]
                    if cols:
                        table_name = tbl
                        columns = cols
                        break
                except Exception:
                    continue

            if not table_name:
                cur.close()
                return jsonify({
                    "success": True,
                    "category": "fiber",
                    "title": "Fiber Network Density",
                    "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                    "rankings": [],
                    "note": "Fiber route ranking data is being compiled. Check back soon.",
                    "source": "DC Hub | dchub.cloud",
                    "generated_at": datetime.utcnow().isoformat()
                })

            state_col = next((c for c in ['state', 'state_name', 'state_code'] if c in columns), None)
            provider_col = next((c for c in ['provider', 'carrier', 'operator', 'company', 'name'] if c in columns), None)

            if not state_col:
                cur.close()
                return jsonify({
                    "success": True, "category": "fiber",
                    "title": "Fiber Network Density",
                    "rankings": [],
                    "note": f"Table '{table_name}' found but missing state column. Columns: {columns}",
                    "source": "DC Hub | dchub.cloud",
                    "generated_at": datetime.utcnow().isoformat()
                })

            provider_expr = f"COUNT(DISTINCT {provider_col})" if provider_col else "0"

            cur.execute(f"""
                SELECT 
                    {state_col} as state,
                    COUNT(*) as route_count,
                    {provider_expr} as provider_count
                FROM {table_name}
                WHERE {state_col} IS NOT NULL AND {state_col} != ''
                GROUP BY {state_col}
                ORDER BY route_count DESC
                LIMIT %s
            """, (limit,))

            rows = cur.fetchall()
            cols_out = [desc[0] for desc in cur.description]

            results = []
            for rank, row in enumerate(rows, 1):
                entry = dict(zip(cols_out, row))
                entry['rank'] = rank
                entry['state_name'] = STATE_NAMES.get(entry.get('state', ''), entry.get('state', ''))
                results.append(entry)

            cur.close()

            return jsonify({
                "success": True,
                "category": "fiber",
                "title": "Fiber Network Density",
                "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                "metric_label": "Fiber Routes",
                "primary_metric": "route_count",
                "secondary_metric": "provider_count",
                "rankings": results,
                "summary": {
                    "total_states": len(results),
                    "total_routes": sum(r['route_count'] for r in results),
                },
                "data_table": table_name,
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from DC Hub fiber route discovery data. Unique fiber routes grouped by US state."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                release_conn(conn)

    return rankings_bp
# Sat Mar 14 10:01:21 PM UTC 2026
