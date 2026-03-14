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
      - format: 'json' (default) or 'html' (returns shareable embed)
    
  GET /api/rankings
    Returns list of available ranking categories with metadata

Blueprint: rankings_bp
"""

from flask import Blueprint, jsonify, request
from datetime import datetime

rankings_bp = Blueprint('rankings', __name__)


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
        Uses pipeline table for under-construction projects.
        """
        country = request.args.get('country', 'US').upper()
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            # Query pipeline table for construction projects, aggregate by state
            # Market field contains state info - we need to extract state
            cur.execute("""
                WITH pipeline_states AS (
                    SELECT 
                        CASE
                            WHEN market ILIKE '%%texas%%' OR market ILIKE '%%TX%%' OR market ILIKE '%%dallas%%' 
                                OR market ILIKE '%%houston%%' OR market ILIKE '%%austin%%' OR market ILIKE '%%san antonio%%'
                                OR market ILIKE '%%abilene%%' OR market ILIKE '%%el paso%%' THEN 'Texas'
                            WHEN market ILIKE '%%virginia%%' OR market ILIKE '%%VA%%' OR market ILIKE '%%ashburn%%' 
                                OR market ILIKE '%%richmond%%' OR market ILIKE '%%n. virginia%%' THEN 'Virginia'
                            WHEN market ILIKE '%%ohio%%' OR market ILIKE '%%OH%%' OR market ILIKE '%%columbus%%' THEN 'Ohio'
                            WHEN market ILIKE '%%arizona%%' OR market ILIKE '%%AZ%%' OR market ILIKE '%%phoenix%%' 
                                OR market ILIKE '%%mesa%%' THEN 'Arizona'
                            WHEN market ILIKE '%%georgia%%' OR market ILIKE '%%GA%%' OR market ILIKE '%%atlanta%%' THEN 'Georgia'
                            WHEN market ILIKE '%%oregon%%' OR market ILIKE '%%OR%%' OR market ILIKE '%%hillsboro%%' 
                                OR market ILIKE '%%portland%%' THEN 'Oregon'
                            WHEN market ILIKE '%%new jersey%%' OR market ILIKE '%%NJ%%' THEN 'New Jersey'
                            WHEN market ILIKE '%%tennessee%%' OR market ILIKE '%%TN%%' OR market ILIKE '%%memphis%%' 
                                OR market ILIKE '%%nashville%%' THEN 'Tennessee'
                            WHEN market ILIKE '%%colorado%%' OR market ILIKE '%%CO%%' OR market ILIKE '%%denver%%' THEN 'Colorado'
                            WHEN market ILIKE '%%illinois%%' OR market ILIKE '%%IL%%' OR market ILIKE '%%chicago%%' THEN 'Illinois'
                            WHEN market ILIKE '%%mississippi%%' OR market ILIKE '%%MS%%' OR market ILIKE '%%lauderdale%%' 
                                OR market ILIKE '%%meridian%%' THEN 'Mississippi'
                            WHEN market ILIKE '%%new mexico%%' OR market ILIKE '%%NM%%' THEN 'New Mexico'
                            WHEN market ILIKE '%%wisconsin%%' OR market ILIKE '%%WI%%' OR market ILIKE '%%port washington%%' THEN 'Wisconsin'
                            WHEN market ILIKE '%%louisiana%%' OR market ILIKE '%%LA%%' OR market ILIKE '%%richland%%' THEN 'Louisiana'
                            WHEN market ILIKE '%%kansas%%' OR market ILIKE '%%KS%%' OR market ILIKE '%%kansas city%%' THEN 'Kansas'
                            WHEN market ILIKE '%%nevada%%' OR market ILIKE '%%NV%%' OR market ILIKE '%%las vegas%%' 
                                OR market ILIKE '%%reno%%' THEN 'Nevada'
                            WHEN market ILIKE '%%california%%' OR market ILIKE '%%CA%%' OR market ILIKE '%%santa clara%%' 
                                OR market ILIKE '%%los angeles%%' OR market ILIKE '%%silicon valley%%' THEN 'California'
                            WHEN market ILIKE '%%washington%%' AND NOT market ILIKE '%%port washington%%' 
                                AND NOT market ILIKE '%%DC%%' THEN 'Washington'
                            WHEN market ILIKE '%%indiana%%' OR market ILIKE '%%IN%%' OR market ILIKE '%%indianapolis%%' THEN 'Indiana'
                            WHEN market ILIKE '%%iowa%%' OR market ILIKE '%%IA%%' THEN 'Iowa'
                            WHEN market ILIKE '%%south carolina%%' OR market ILIKE '%%SC%%' THEN 'South Carolina'
                            WHEN market ILIKE '%%north carolina%%' OR market ILIKE '%%NC%%' OR market ILIKE '%%charlotte%%' THEN 'North Carolina'
                            WHEN market ILIKE '%%maryland%%' OR market ILIKE '%%MD%%' THEN 'Maryland'
                            WHEN market ILIKE '%%new york%%' OR market ILIKE '%%NY%%' THEN 'New York'
                            WHEN market ILIKE '%%utah%%' OR market ILIKE '%%UT%%' OR market ILIKE '%%salt lake%%' THEN 'Utah'
                            WHEN market ILIKE '%%pennsylvania%%' OR market ILIKE '%%PA%%' THEN 'Pennsylvania'
                            WHEN market ILIKE '%%florida%%' OR market ILIKE '%%FL%%' OR market ILIKE '%%miami%%' THEN 'Florida'
                            WHEN market ILIKE '%%alabama%%' OR market ILIKE '%%AL%%' THEN 'Alabama'
                            ELSE NULL
                        END AS state_name,
                        capacity_mw,
                        investment_millions,
                        project_name,
                        operator
                    FROM pipeline
                    WHERE LOWER(status) IN ('construction', 'under_construction')
                      AND (country = %s OR country IS NULL)
                )
                SELECT 
                    state_name,
                    COUNT(*) as project_count,
                    ROUND(COALESCE(SUM(capacity_mw), 0)::numeric, 0) as total_mw,
                    ROUND(COALESCE(SUM(investment_millions), 0)::numeric, 0) as total_investment_millions,
                    ARRAY_AGG(DISTINCT operator) as operators,
                    ARRAY_AGG(project_name) as projects
                FROM pipeline_states
                WHERE state_name IS NOT NULL
                GROUP BY state_name
                ORDER BY total_mw DESC, project_count DESC
                LIMIT %s
            """, (country, limit))

            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            results = []
            for rank, row in enumerate(rows, 1):
                entry = dict(zip(cols, row))
                entry['rank'] = rank
                # Convert arrays to lists for JSON serialization
                if entry.get('operators'):
                    entry['operators'] = list(entry['operators'])
                if entry.get('projects'):
                    entry['projects'] = list(entry['projects'])
                results.append(entry)

            cur.close()
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
                "source": "DC Hub | dchub.cloud",
                "generated_at": datetime.utcnow().isoformat(),
                "methodology": "Aggregated from DC Hub pipeline tracking database. Projects with status 'under construction' grouped by US state."
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                release_conn(conn)

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
                  AND is_duplicate = 0
                GROUP BY state
                ORDER BY total_mw DESC
                LIMIT %s
            """, (country, limit))

            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]

            # State abbreviation to full name mapping
            state_names = {
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

            results = []
            for rank, row in enumerate(rows, 1):
                entry = dict(zip(cols, row))
                entry['rank'] = rank
                entry['state_name'] = state_names.get(entry['state'], entry['state'])
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
        Uses gas_pipelines table.
        """
        country = request.args.get('country', 'US').upper()
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            # Check if gas_pipelines table exists and get column info
            cur.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'gas_pipelines' 
                ORDER BY ordinal_position
            """)
            columns = [r[0] for r in cur.fetchall()]

            if not columns:
                return jsonify({
                    "success": True,
                    "category": "gas",
                    "title": "Gas Pipeline Infrastructure",
                    "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                    "rankings": [],
                    "note": "Gas pipeline data table not yet populated.",
                    "source": "DC Hub | dchub.cloud",
                    "generated_at": datetime.utcnow().isoformat()
                })

            # Determine the right column names based on what exists
            state_col = 'state' if 'state' in columns else 'state_name'
            miles_col = 'miles' if 'miles' in columns else ('length_miles' if 'length_miles' in columns else 'shape_length')
            name_col = 'name' if 'name' in columns else ('pipeline_name' if 'pipeline_name' in columns else 'operator')
            operator_col = 'operator' if 'operator' in columns else name_col

            cur.execute(f"""
                SELECT 
                    {state_col} as state,
                    COUNT(*) as pipeline_count,
                    ROUND(COALESCE(SUM(CASE WHEN {miles_col} IS NOT NULL THEN {miles_col} ELSE 0 END), 0)::numeric, 0) as total_miles,
                    COUNT(DISTINCT {operator_col}) as operator_count
                FROM gas_pipelines
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
        Uses fiber_routes table.
        """
        country = request.args.get('country', 'US').upper()
        limit = min(int(request.args.get('limit', 25)), 50)

        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()

            # Check fiber_routes table structure
            cur.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'fiber_routes' 
                ORDER BY ordinal_position
            """)
            columns = [r[0] for r in cur.fetchall()]

            if not columns:
                return jsonify({
                    "success": True,
                    "category": "fiber",
                    "title": "Fiber Network Density",
                    "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
                    "rankings": [],
                    "note": "Fiber route data table not yet populated.",
                    "source": "DC Hub | dchub.cloud",
                    "generated_at": datetime.utcnow().isoformat()
                })

            state_col = 'state' if 'state' in columns else 'state_name'
            provider_col = 'provider' if 'provider' in columns else ('carrier' if 'carrier' in columns else 'operator')

            cur.execute(f"""
                SELECT 
                    {state_col} as state,
                    COUNT(*) as route_count,
                    COUNT(DISTINCT {provider_col}) as provider_count
                FROM fiber_routes
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
                    "total_providers": max((r['provider_count'] for r in results), default=0),
                },
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
