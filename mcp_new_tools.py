"""
mcp_new_tools.py — 3 new MCP tools for DC Hub
===============================================

These tool definitions go into dchub_mcp_server.py, inside the
section where other @mcp.tool() functions are defined.

Tools:
  16. get_tax_incentives — 50-state tax incentive database
  17. compare_sites — Multi-location side-by-side scoring
  18. get_water_risk — Water stress + cooling recommendations

Also includes:
  - Region normalization fix for list_transactions (Bug 2)
  - Data for MCP_TEASER_TOOLS + ALLOWED_FIELDS updates in main.py

INSTALL:
  1. Copy tool functions into dchub_mcp_server.py
  2. Add 'get_tax_incentives', 'compare_sites', 'get_water_risk' to
     MCP_TEASER_TOOLS set in main.py
  3. Add new field names to ALLOWED_FIELDS in main.py
  4. git commit + push
"""


# ═══════════════════════════════════════════════════════════
# TOOL 16: get_tax_incentives
# ═══════════════════════════════════════════════════════════
# Copy this into dchub_mcp_server.py alongside other @mcp.tool() defs

GET_TAX_INCENTIVES = '''
@mcp.tool()
async def get_tax_incentives(state: str = "") -> str:
    """Get data center tax incentives by US state.

    Returns tax credits, property tax abatements, sales tax exemptions,
    enterprise zones, and incentive programs for data center development.

    Args:
        state: US state abbreviation (e.g. 'VA', 'TX', 'OH'). Leave empty for all states summary.

    Returns:
        JSON with tax incentive programs, qualifying criteria, and estimated savings.
    """
    try:
        conn = _get_connection()
        cur = conn.cursor()

        if state and len(state) <= 3:
            cur.execute("""
                SELECT state, incentive_type, program_name, description,
                       qualifying_criteria, estimated_savings, expiration_date,
                       source_url
                FROM tax_incentives_neon
                WHERE UPPER(state) = UPPER(%s)
                ORDER BY incentive_type
            """, (state.upper(),))
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            incentives = [dict(zip(columns, row)) for row in rows]
        else:
            cur.execute("""
                SELECT state, COUNT(*) as program_count,
                       STRING_AGG(DISTINCT incentive_type, ', ') as types
                FROM tax_incentives_neon
                GROUP BY state
                ORDER BY program_count DESC
            """)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            incentives = [dict(zip(columns, row)) for row in rows]

        cur.execute("SELECT COUNT(DISTINCT state) FROM tax_incentives_neon")
        total_states = cur.fetchone()[0] or 0

        cur.close()
        conn.close()

        return json.dumps({
            'success': True,
            'state': state.upper() if state else 'all',
            'incentives': incentives,
            'count': len(incentives),
            'states_covered': total_states,
            'source': 'DC Hub Tax Incentive Database (tax_incentives_neon)',
            'note': 'Covers sales tax exemptions, property tax abatements, enterprise zones, and state-specific DC incentive programs.'
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})
'''


# ═══════════════════════════════════════════════════════════
# TOOL 17: compare_sites
# ═══════════════════════════════════════════════════════════

COMPARE_SITES = '''
@mcp.tool()
async def compare_sites(locations: str = "") -> str:
    """Compare 2-4 locations for data center suitability side-by-side.

    Much more efficient than calling analyze_site multiple times.
    Scores each location on power, fiber, gas, market, and risk.

    Args:
        locations: JSON array of locations. Example:
            [{"lat":33.45,"lon":-112.07,"state":"AZ","label":"Phoenix"},
             {"lat":39.04,"lon":-77.49,"state":"VA","label":"Ashburn"}]

    Returns:
        JSON comparison table with scores per location and winner per category.
    """
    import json as _json
    try:
        locs = _json.loads(locations)
        if not isinstance(locs, list) or len(locs) < 2:
            return _json.dumps({
                'success': False,
                'error': 'Provide 2-4 locations as JSON array with lat, lon, state, label fields'
            })
        if len(locs) > 4:
            locs = locs[:4]

        import requests as _req
        results = []
        for loc in locs:
            try:
                resp = _req.get(
                    f"{API_BASE}/api/site-score",
                    params={
                        'lat': loc.get('lat', 0),
                        'lon': loc.get('lon', 0),
                        'state': loc.get('state', ''),
                        'capacity': loc.get('capacity_mw', 0),
                    },
                    headers={'X-Internal-Key': get_internal_key_for_client()},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                else:
                    data = {'overall_score': 0, 'error': f'HTTP {resp.status_code}'}
            except Exception as e:
                data = {'overall_score': 0, 'error': str(e)}

            data['label'] = loc.get('label', f"{loc.get('state', '')} ({loc.get('lat')},{loc.get('lon')})")
            results.append(data)

        # Determine winners per category
        categories = ['power_infrastructure', 'gas_pipeline_access',
                       'fiber_connectivity', 'market_conditions', 'risk_resilience']
        winners = {}
        for cat in categories:
            scored = [(r.get('label', '%s'), r.get('scores', {}).get(cat, 0)) for r in results]
            best = max(scored, key=lambda x: x[1])
            winners[cat] = {'winner': best[0], 'score': best[1]}

        overall_winner = max(results, key=lambda r: r.get('overall_score', 0))

        comparison = []
        for r in results:
            comparison.append({
                'label': r.get('label'),
                'overall_score': r.get('overall_score'),
                'interpretation': r.get('interpretation'),
                'scores': r.get('scores', {}),
                'nearby': r.get('nearby', {}),
            })

        return _json.dumps({
            'success': True,
            'comparison': comparison,
            'winners_by_category': winners,
            'overall_winner': overall_winner.get('label'),
            'overall_winner_score': overall_winner.get('overall_score'),
            'locations_compared': len(results),
            'source': 'DC Hub Site Intelligence'
        })
    except _json.JSONDecodeError:
        return _json.dumps({
            'success': False,
            'error': 'Invalid JSON in locations parameter. Expected: [{"lat":33.45,"lon":-112.07,"state":"AZ","label":"Phoenix"}, ...]'
        })
    except Exception as e:
        return _json.dumps({'success': False, 'error': str(e)})
'''


# ═══════════════════════════════════════════════════════════
# TOOL 18: get_water_risk
# ═══════════════════════════════════════════════════════════

GET_WATER_RISK = '''
@mcp.tool()
async def get_water_risk(
    lat: float = 0,
    lon: float = 0,
    state: str = "",
) -> str:
    """Get water stress and drought risk for a data center location.

    Critical for cooling system design — determines whether evaporative,
    air-cooled, or hybrid cooling is appropriate. Returns USGS water stress
    data and actionable cooling recommendations.

    Args:
        lat: Latitude coordinate
        lon: Longitude coordinate
        state: US state abbreviation (e.g. 'AZ', 'TX', 'VA')

    Returns:
        JSON with water stress level, withdrawal data, and cooling recommendations.
    """
    try:
        conn = _get_connection()
        cur = conn.cursor()

        water_data = {}

        # USGS water stress by state
        if state:
            cur.execute("""
                SELECT state, stress_level, withdrawal_mgd,
                       population_served, primary_source
                FROM usgs_water_stress
                WHERE UPPER(state) = UPPER(%s)
                LIMIT 1
            """, (state.upper(),))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                water_data = dict(zip(cols, row))

        # Count total states with data
        cur.execute("SELECT COUNT(DISTINCT state) FROM usgs_water_stress")
        states_covered = cur.fetchone()[0] or 0

        cur.close()
        conn.close()

        # Cooling recommendation engine
        stress = (water_data.get('stress_level', '') or '').lower()
        if 'extreme' in stress or 'very high' in stress:
            cooling = {
                'recommendation': 'Air-cooled or closed-loop dry cooling required.',
                'avoid': 'Evaporative cooling — water scarcity makes it unsustainable.',
                'best_pue_achievable': '1.25-1.35',
                'risk_level': 'high',
            }
        elif 'high' in stress:
            cooling = {
                'recommendation': 'Hybrid cooling (air + minimal evaporative) recommended.',
                'avoid': 'Large-scale evaporative without water recycling.',
                'best_pue_achievable': '1.20-1.30',
                'risk_level': 'moderate-high',
            }
        elif 'moderate' in stress:
            cooling = {
                'recommendation': 'Hybrid or evaporative with water recycling.',
                'avoid': 'Open-loop once-through cooling.',
                'best_pue_achievable': '1.15-1.25',
                'risk_level': 'moderate',
            }
        else:
            cooling = {
                'recommendation': 'All cooling methods viable. Evaporative offers best PUE.',
                'avoid': 'No restrictions — water supply adequate.',
                'best_pue_achievable': '1.10-1.20',
                'risk_level': 'low',
            }

        return json.dumps({
            'success': True,
            'location': {'lat': lat, 'lon': lon, 'state': state.upper() if state else ''},
            'water_stress': water_data if water_data else {'note': f'No USGS data for state {state}. Covered states: AZ, CA, CO, FL, GA, ID, IL, NV, NJ, NY, OH, OR, PA, TX, UT, VA, WA'},
            'cooling_recommendation': cooling,
            'data_source': 'USGS National Water Information System',
            'states_covered': states_covered,
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})
'''


# ═══════════════════════════════════════════════════════════
# BUG 2 FIX: Region normalization for list_transactions
# ═══════════════════════════════════════════════════════════

REGION_NORMALIZATION_PATCH = '''
# Add this BEFORE the SQL query in the list_transactions tool handler:
REGION_MAP = {
    'europe': 'EMEA', 'eu': 'EMEA', 'emea': 'EMEA',
    'north_america': 'North America', 'na': 'North America',
    'us': 'North America', 'usa': 'North America',
    'north america': 'North America', 'americas': 'North America',
    'asia': 'APAC', 'apac': 'APAC', 'asia_pacific': 'APAC',
    'asia pacific': 'APAC', 'pacific': 'APAC',
    'latam': 'LATAM', 'latin_america': 'LATAM',
    'south_america': 'LATAM', 'latin america': 'LATAM',
    'middle_east': 'MEA', 'mea': 'MEA', 'africa': 'MEA',
    'middle east': 'MEA',
}
if region:
    region = REGION_MAP.get(region.lower().strip(), region)
'''


# ═══════════════════════════════════════════════════════════
# main.py ADDITIONS (MCP_TEASER_TOOLS + ALLOWED_FIELDS)
# ═══════════════════════════════════════════════════════════

MAIN_PY_ADDITIONS = '''
# ADD to MCP_TEASER_TOOLS set (around line 2548):
#   'get_tax_incentives', 'compare_sites', 'get_water_risk'

# ADD to ALLOWED_FIELDS set (around line 2810):
#   'incentives', 'states_covered', 'program_count', 'types',
#   'comparison', 'winners_by_category', 'overall_winner',
#   'overall_winner_score', 'locations_compared',
#   'water_stress', 'cooling_recommendation',
#   'comparisons', 'carrier_filter', 'carrier_routes_found', 'carrier_note'
'''


if __name__ == '__main__':
    print("═══════════════════════════════════════════════════")
    print("DC Hub MCP — New Tools + Bug Fixes")
    print("═══════════════════════════════════════════════════")
    print()
    print("FILES TO EDIT:")
    print("  1. dchub_mcp_server.py — Add 3 new @mcp.tool() functions")
    print("     + region normalization in list_transactions")
    print("  2. main.py — Add to MCP_TEASER_TOOLS + ALLOWED_FIELDS")
    print()
    print("NEW TOOLS:")
    print("  16. get_tax_incentives (50 states)")
    print("  17. compare_sites (multi-location)")
    print("  18. get_water_risk (USGS + cooling recs)")
    print()
    print("BUG FIXES:")
    print("  Bug 1: get_facility id + name fallback (mcp_facility_fix.py)")
    print("  Bug 2: list_transactions region normalization")
    print("  Bug 3: get_market_intel comparisons whitelist")
    print("  Bug 4: get_energy_prices data_type routing (TODO)")
    print()
    print("Tool code is in this file as string constants.")
    print("Copy GET_TAX_INCENTIVES, COMPARE_SITES, GET_WATER_RISK")
    print("into dchub_mcp_server.py.")
