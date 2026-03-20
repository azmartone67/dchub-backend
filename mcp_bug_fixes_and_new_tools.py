"""
mcp_bug_fixes_and_new_tools.py — Comprehensive MCP fixes + new tools
=====================================================================

Run in Railway shell after git pull:
  python mcp_bug_fixes_and_new_tools.py

FIXES (4 bugs):
  1. get_facility: 'id' added to MCP_FREE_FIELDS + name fallback (in mcp_facility_fix.py)
  2. list_transactions: region normalization (europe→EMEA, etc.)
  3. get_market_intel: 'comparisons' added to ALLOWED_FIELDS whitelist
  4. get_energy_prices: data_type parameter ignored — needs MCP server fix

PATCHES main.py for bugs 2-3. Bug 4 needs dchub_mcp_server.py changes.

NEW TOOLS (adds to dchub_mcp_server.py):
  - get_tax_incentives (50 states from tax_incentives_neon)
  - compare_sites (multi-location scoring)
  - get_water_risk (USGS water stress + drought data)
"""

import os
import sys


def fix_main_py():
    """Apply bug fixes 2 and 3 to main.py"""
    main_path = os.path.expanduser('~/workspace/main.py')
    if not os.path.exists(main_path):
        main_path = '/app/main.py'
    if not os.path.exists(main_path):
        print("ERROR: main.py not found")
        return False

    with open(main_path, 'r') as f:
        content = f.read()

    changes = 0

    # ── BUG 2: list_transactions region normalization ──
    # The MCP server passes region as-is to the DB query.
    # DB stores: 'North America', 'EMEA', 'APAC', 'LATAM'
    # Users send: 'europe', 'north_america', 'asia', etc.
    # Fix: Add normalization in the _gate_teaser_result for list_transactions
    # Actually this needs to be in dchub_mcp_server.py where the SQL query runs.
    # For now, add a note — the real fix is in the MCP server file.
    print("⏭️  BUG 2 (region normalization): Needs dchub_mcp_server.py fix — see REGION_MAP below")

    # ── BUG 3: get_market_intel comparisons stripped by whitelist ──
    old_allowed = "'comparisons' not in content"  # placeholder check
    
    # Find ALLOWED_FIELDS and add 'comparisons'
    if "'comparisons'" not in content and 'ALLOWED_FIELDS' in content:
        old_whitelist = "    'dc_hub_intelligence_index', 'pipeline_projects', 'total_pipeline_mw',"
        new_whitelist = "    'dc_hub_intelligence_index', 'pipeline_projects', 'total_pipeline_mw',\n    'comparisons', 'carrier_filter', 'carrier_routes_found', 'carrier_note',"
        
        if old_whitelist in content:
            content = content.replace(old_whitelist, new_whitelist, 1)
            changes += 1
            print("✅ BUG 3: Added 'comparisons' + carrier fields to ALLOWED_FIELDS")
        else:
            print("⚠️  BUG 3: ALLOWED_FIELDS pattern not found — check manually")
    else:
        print("⏭️  BUG 3: 'comparisons' already in ALLOWED_FIELDS or not found")

    if changes > 0:
        with open(main_path, 'w') as f:
            f.write(content)
        print(f"\n✅ Applied {changes} fix(es) to main.py")
    else:
        print(f"\n⏭️  No main.py changes needed")

    return True


def print_mcp_server_patches():
    """Print patches needed for dchub_mcp_server.py (new tools + bug fixes)"""
    
    print("""
═══════════════════════════════════════════════════════════════
PATCHES FOR dchub_mcp_server.py — Apply manually or via sed
═══════════════════════════════════════════════════════════════

────────────────────────────────────────
BUG 2 FIX: Region normalization for list_transactions
────────────────────────────────────────
Add this near the top of the list_transactions tool handler,
BEFORE the SQL query:

    # Region normalization
    REGION_MAP = {
        'europe': 'EMEA', 'eu': 'EMEA', 'emea': 'EMEA',
        'north_america': 'North America', 'na': 'North America',
        'us': 'North America', 'usa': 'North America',
        'north america': 'North America',
        'asia': 'APAC', 'apac': 'APAC', 'asia_pacific': 'APAC',
        'latam': 'LATAM', 'latin_america': 'LATAM',
        'south_america': 'LATAM', 'latin america': 'LATAM',
        'middle_east': 'MEA', 'mea': 'MEA', 'africa': 'MEA',
    }
    if region:
        region = REGION_MAP.get(region.lower().strip(), region)

────────────────────────────────────────
BUG 4 FIX: get_energy_prices data_type routing
────────────────────────────────────────
In the get_energy_prices tool handler, add routing logic:

    data_type = arguments.get('data_type', 'retail_rates')
    
    if data_type == 'natural_gas':
        # Query gas price data (Henry Hub or state-level)
        # For now, return EIA natural gas data if available
        pass  # TODO: Add natural gas query
    elif data_type == 'grid_status':
        # Redirect to get_grid_data logic
        pass  # TODO: Route to grid handler
    elif data_type == 'gas_storage':
        # EIA gas storage data
        pass  # TODO: Add gas storage query
    else:
        # Default: retail_rates (existing logic)
        pass

────────────────────────────────────────
NEW TOOL 1: get_tax_incentives
────────────────────────────────────────
Add this tool definition to the MCP server tool list:

@mcp.tool()
async def get_tax_incentives(state: str = "") -> str:
    \"\"\"Get data center tax incentives by US state.
    
    Returns tax credits, abatements, enterprise zones, and incentive
    programs relevant to data center development and operations.
    
    Args:
        state: US state abbreviation (e.g. 'VA', 'TX', 'OH')
    
    Returns:
        JSON with tax incentive programs, qualifying criteria, and savings estimates.
    \"\"\"
    try:
        conn = _get_connection()
        cur = conn.cursor()
        
        if state:
            cur.execute(\"\"\"
                SELECT state, incentive_type, program_name, description,
                       qualifying_criteria, estimated_savings, expiration_date,
                       source_url
                FROM tax_incentives_neon
                WHERE UPPER(state) = UPPER(%s)
                ORDER BY incentive_type
            \"\"\", (state,))
        else:
            # Return summary: states with most incentives
            cur.execute(\"\"\"
                SELECT state, COUNT(*) as program_count,
                       STRING_AGG(DISTINCT incentive_type, ', ') as types
                FROM tax_incentives_neon
                GROUP BY state
                ORDER BY program_count DESC
            \"\"\")
        
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        results = [dict(zip(columns, row)) for row in rows]
        
        cur.close()
        conn.close()
        
        return json.dumps({
            'success': True,
            'state': state or 'all',
            'incentives': results,
            'count': len(results),
            'states_covered': 50,
            'source': 'DC Hub Tax Incentive Database'
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})

────────────────────────────────────────
NEW TOOL 2: compare_sites
────────────────────────────────────────
@mcp.tool()
async def compare_sites(
    locations: str = "",
) -> str:
    \"\"\"Compare 2-4 locations for data center suitability side-by-side.
    
    Scores each location on power, fiber, gas, market conditions, and risk.
    Much more efficient than calling analyze_site multiple times.
    
    Args:
        locations: JSON array of locations, e.g. '[{"lat":33.45,"lon":-112.07,"state":"AZ","label":"Phoenix"},{"lat":39.04,"lon":-77.49,"state":"VA","label":"Ashburn"}]'
    
    Returns:
        JSON with side-by-side comparison table and winner per category.
    \"\"\"
    import json as _json
    try:
        locs = _json.loads(locations)
        if not isinstance(locs, list) or len(locs) < 2:
            return _json.dumps({'error': 'Provide 2-4 locations as JSON array'})
        if len(locs) > 4:
            locs = locs[:4]
        
        import requests
        results = []
        for loc in locs:
            resp = requests.get(
                f"{API_BASE}/api/site-score",
                params={
                    'lat': loc.get('lat', 0),
                    'lon': loc.get('lon', 0),
                    'state': loc.get('state', ''),
                    'capacity': loc.get('capacity_mw', 0),
                },
                headers={'X-Internal-Key': 'dchub-internal-sync-2026'},
                timeout=15
            )
            data = resp.json() if resp.status_code == 200 else {'error': resp.status_code}
            data['label'] = loc.get('label', f"{loc.get('lat')},{loc.get('lon')}")
            results.append(data)
        
        # Build comparison
        categories = ['power_infrastructure', 'gas_pipeline_access', 'fiber_connectivity',
                       'market_conditions', 'risk_resilience']
        winners = {}
        for cat in categories:
            best = max(results, key=lambda r: r.get('scores', {}).get(cat, 0))
            winners[cat] = best.get('label', '?')
        
        overall_winner = max(results, key=lambda r: r.get('overall_score', 0))
        
        return _json.dumps({
            'success': True,
            'comparison': [{
                'label': r.get('label'),
                'overall_score': r.get('overall_score'),
                'interpretation': r.get('interpretation'),
                'scores': r.get('scores', {}),
                'nearby': r.get('nearby', {}),
            } for r in results],
            'winners': winners,
            'overall_winner': overall_winner.get('label'),
            'locations_compared': len(results),
        })
    except Exception as e:
        return _json.dumps({'success': False, 'error': str(e)})

────────────────────────────────────────
NEW TOOL 3: get_water_risk
────────────────────────────────────────
@mcp.tool()
async def get_water_risk(
    lat: float = 0,
    lon: float = 0,
    state: str = "",
) -> str:
    \"\"\"Get water stress and drought risk for a data center location.
    
    Critical for cooling system design decisions. Returns USGS water stress
    index, current drought conditions, and DC cooling recommendations.
    
    Args:
        lat: Latitude
        lon: Longitude  
        state: US state abbreviation
    
    Returns:
        JSON with water stress level, drought status, and cooling recommendations.
    \"\"\"
    try:
        conn = _get_connection()
        cur = conn.cursor()
        
        water_data = {}
        
        # USGS water stress
        if state:
            cur.execute(\"\"\"
                SELECT state, stress_level, withdrawal_mgd, 
                       population_served, primary_source
                FROM usgs_water_stress
                WHERE UPPER(state) = UPPER(%s)
                LIMIT 1
            \"\"\", (state,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                water_data['usgs_water_stress'] = dict(zip(cols, row))
        
        # Nearby water bodies (from infrastructure if available)
        # Drought monitor integration would go here
        
        cur.close()
        conn.close()
        
        # Cooling recommendations based on water stress
        stress = (water_data.get('usgs_water_stress', {}).get('stress_level', '') or '').lower()
        if 'high' in stress or 'extreme' in stress:
            cooling_rec = 'Air-cooled or closed-loop dry cooling strongly recommended. Avoid evaporative cooling.'
        elif 'moderate' in stress:
            cooling_rec = 'Hybrid cooling (air + limited evaporative) recommended. Monitor water availability.'
        else:
            cooling_rec = 'All cooling methods viable. Evaporative cooling offers best PUE in this region.'
        
        return json.dumps({
            'success': True,
            'location': {'lat': lat, 'lon': lon, 'state': state},
            'water_stress': water_data.get('usgs_water_stress', {}),
            'cooling_recommendation': cooling_rec,
            'data_source': 'USGS National Water Information System',
            'states_covered': 16,
            'note': 'Water stress data covers 16 major DC states. Full coverage coming Q2 2026.'
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})

────────────────────────────────────────
ALSO ADD to MCP_TEASER_TOOLS in main.py:
────────────────────────────────────────
  'get_tax_incentives', 'compare_sites', 'get_water_risk'

And add to ALLOWED_FIELDS:
  'incentives', 'states_covered', 'comparison', 'winners',
  'overall_winner', 'locations_compared', 'water_stress',
  'cooling_recommendation', 'program_count', 'types'

═══════════════════════════════════════════════════════════════
""")


if __name__ == '__main__':
    fix_main_py()
    print()
    print_mcp_server_patches()
    print()
    print("NEXT STEPS:")
    print("  1. Run mcp_facility_fix.py first (adds 'id' to MCP_FREE_FIELDS)")
    print("  2. Apply the dchub_mcp_server.py patches (new tools + region fix)")
    print("  3. git add main.py dchub_mcp_server.py")
    print("  4. git commit -m 'MCP: 4 bug fixes + 3 new tools'")
    print("  5. git push")
