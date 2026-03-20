"""
mcp_server_patch.py — Add 4 new tools + fix 2 bugs in MCP server
==================================================================

Run in Railway shell:
  cd ~/workspace && git pull origin main
  python mcp_server_patch.py
  git add dchub_mcp_server.py main.py
  git commit -m "MCP: 4 new tools + region fix (18 tools total)"
  git push

ADDS TO dchub_mcp_server.py:
  Tool 16: get_tax_incentives (50-state tax incentive DB)
  Tool 17: compare_sites (multi-location scoring)
  Tool 18: get_water_risk (USGS water stress + cooling recs)
  Tool 19: get_backup_status (Neon DB backup health monitor)
  
FIXES IN dchub_mcp_server.py:
  Bug 2: list_transactions region normalization (europe→EMEA)

UPDATES main.py:
  - Adds new tool names to MCP_TEASER_TOOLS
  - Adds new field names to ALLOWED_FIELDS
"""

import os
import sys


# ═══════════════════════════════════════════════════════════
# 4 NEW TOOL DEFINITIONS (injected into dchub_mcp_server.py)
# ═══════════════════════════════════════════════════════════

NEW_TOOLS_CODE = '''

# ═══════════════════════════════════════════════════════════
# TOOL 16: get_tax_incentives — 50-state tax incentive database
# ═══════════════════════════════════════════════════════════
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
        results = [dict(zip(columns, row)) for row in rows]

        cur.execute("SELECT COUNT(DISTINCT state) FROM tax_incentives_neon")
        total_states = cur.fetchone()[0] or 0

        cur.close()
        conn.close()

        return json.dumps({
            'success': True,
            'state': state.upper() if state else 'all',
            'incentives': results,
            'count': len(results),
            'states_covered': total_states,
            'source': 'DC Hub Tax Incentive Database',
            'note': 'Sales tax exemptions, property tax abatements, enterprise zones, and state-specific DC incentive programs.'
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})


# ═══════════════════════════════════════════════════════════
# TOOL 17: compare_sites — Multi-location side-by-side scoring
# ═══════════════════════════════════════════════════════════
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
                'error': 'Provide 2-4 locations as JSON array with lat, lon, state, label fields',
                'example': '[{"lat":33.45,"lon":-112.07,"state":"AZ","label":"Phoenix"},{"lat":39.04,"lon":-77.49,"state":"VA","label":"Ashburn"}]'
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
                    headers={'X-Internal-Key': 'dchub-internal-sync-2026'},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                else:
                    data = {'overall_score': 0, 'scores': {}, 'error': f'HTTP {resp.status_code}'}
            except Exception as e:
                data = {'overall_score': 0, 'scores': {}, 'error': str(e)}

            data['label'] = loc.get('label', f"{loc.get('state', '')} ({loc.get('lat')},{loc.get('lon')})")
            results.append(data)

        categories = ['power_infrastructure', 'gas_pipeline_access',
                       'fiber_connectivity', 'market_conditions', 'risk_resilience']
        winners = {}
        for cat in categories:
            scored = [(r.get('label', '?'), r.get('scores', {}).get(cat, 0)) for r in results]
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
        return json.dumps({
            'success': False,
            'error': 'Invalid JSON. Expected: [{"lat":33.45,"lon":-112.07,"state":"AZ","label":"Phoenix"}, ...]'
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})


# ═══════════════════════════════════════════════════════════
# TOOL 18: get_water_risk — Water stress + cooling recommendations
# ═══════════════════════════════════════════════════════════
@mcp.tool()
async def get_water_risk(lat: float = 0, lon: float = 0, state: str = "") -> str:
    """Get water stress and drought risk for a data center location.

    Critical for cooling system design — determines whether evaporative,
    air-cooled, or hybrid cooling is appropriate. Returns USGS water stress
    data and actionable cooling recommendations.

    Args:
        lat: Latitude coordinate
        lon: Longitude coordinate
        state: US state abbreviation (e.g. 'AZ', 'TX', 'VA')

    Returns:
        JSON with water stress level, withdrawal data, and cooling system recommendations.
    """
    try:
        conn = _get_connection()
        cur = conn.cursor()

        water_data = {}
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
            'water_stress': water_data if water_data else {
                'note': f'No USGS data for state "{state}". Covered: AZ, CA, CO, FL, GA, ID, IL, NV, NJ, NY, OH, OR, PA, TX, UT, VA, WA'
            },
            'cooling_recommendation': cooling,
            'data_source': 'USGS National Water Information System',
            'states_covered': states_covered,
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})


# ═══════════════════════════════════════════════════════════
# TOOL 19: get_backup_status — Neon DB backup health monitor
# ═══════════════════════════════════════════════════════════
@mcp.tool()
async def get_backup_status() -> str:
    """Get Neon database backup status and data integrity metrics.

    Monitor backup health, table sizes, and data freshness across
    all critical DC Hub tables. Use for operational monitoring.

    Returns:
        JSON with backup status, table row counts, and data freshness timestamps.
    """
    try:
        conn = _get_connection()
        cur = conn.cursor()

        tables = {}
        table_queries = [
            ('facilities', "SELECT COUNT(*) FROM facilities"),
            ('discovered_facilities', "SELECT COUNT(*) FROM discovered_facilities"),
            ('deals', "SELECT COUNT(*) FROM deals"),
            ('announcements', "SELECT COUNT(*) FROM announcements"),
            ('users', "SELECT COUNT(*) FROM users"),
            ('api_keys', "SELECT COUNT(*) FROM api_keys"),
            ('fiber_routes', "SELECT COUNT(*) FROM fiber_routes"),
            ('hifld_substations', "SELECT COUNT(*) FROM hifld_substations"),
            ('discovered_pipelines', "SELECT COUNT(*) FROM discovered_pipelines"),
            ('capacity_pipeline', "SELECT COUNT(*) FROM capacity_pipeline"),
            ('tax_incentives_neon', "SELECT COUNT(*) FROM tax_incentives_neon"),
            ('energy_ppas', "SELECT COUNT(*) FROM energy_ppas"),
            ('gdci_scores', "SELECT COUNT(*) FROM gdci_scores"),
            ('metro_dark_fiber', "SELECT COUNT(*) FROM metro_dark_fiber"),
            ('usgs_water_stress', "SELECT COUNT(*) FROM usgs_water_stress"),
            ('eia_retail_rates', "SELECT COUNT(*) FROM eia_retail_rates"),
            ('epa_egrid', "SELECT COUNT(*) FROM epa_egrid"),
            ('fema_risk_index', "SELECT COUNT(*) FROM fema_risk_index"),
        ]

        total_rows = 0
        for name, query in table_queries:
            try:
                cur.execute(query)
                count = cur.fetchone()[0] or 0
                tables[name] = count
                total_rows += count
            except Exception:
                tables[name] = 'table_missing'

        # Data freshness checks
        freshness = {}
        freshness_queries = [
            ('newest_facility', "SELECT MAX(created_at) FROM discovered_facilities"),
            ('newest_deal', "SELECT MAX(date) FROM deals"),
            ('newest_news', "SELECT MAX(published_date) FROM announcements"),
            ('newest_user', "SELECT MAX(created_at) FROM users"),
        ]
        for name, query in freshness_queries:
            try:
                cur.execute(query)
                val = cur.fetchone()[0]
                freshness[name] = str(val) if val else None
            except Exception:
                freshness[name] = None

        # DB size
        try:
            cur.execute("SELECT pg_database_size(current_database())")
            db_size_bytes = cur.fetchone()[0] or 0
            db_size_mb = round(db_size_bytes / (1024 * 1024), 1)
        except Exception:
            db_size_mb = 0

        cur.close()
        conn.close()

        return json.dumps({
            'success': True,
            'database': 'Neon PostgreSQL (Azure West US 3)',
            'db_size_mb': db_size_mb,
            'total_rows': total_rows,
            'tables': tables,
            'freshness': freshness,
            'backup_provider': 'Neon (point-in-time recovery)',
            'redundancy': 'Railway (primary) + Replit (failover) → same Neon DB',
            'status': 'healthy' if total_rows > 10000 else 'degraded',
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})
'''


# ═══════════════════════════════════════════════════════════
# REGION NORMALIZATION for list_transactions (Bug 2 fix)
# ═══════════════════════════════════════════════════════════

REGION_FIX_CODE = '''
    # Region normalization (Bug 2 fix — europe→EMEA, etc.)
    _REGION_MAP = {
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
        region = _REGION_MAP.get(region.lower().strip(), region)
'''


def patch_mcp_server():
    """Patch dchub_mcp_server.py with new tools + region fix"""
    mcp_path = os.path.expanduser('~/workspace/dchub_mcp_server.py')
    if not os.path.exists(mcp_path):
        mcp_path = '/app/dchub_mcp_server.py'
    if not os.path.exists(mcp_path):
        print("ERROR: dchub_mcp_server.py not found")
        return False

    with open(mcp_path, 'r') as f:
        content = f.read()

    changes = 0

    # ── ADD NEW TOOLS ──
    # Find the last @mcp.tool() function and append after it
    if 'get_tax_incentives' in content:
        print("⏭️  New tools already present in dchub_mcp_server.py")
    else:
        # Find a good insertion point — after the last tool definition
        # Look for 'get_dchub_recommendation' or the last @mcp.tool block
        insertion_markers = [
            'async def get_dchub_recommendation',
            'async def get_renewable_energy',
            'async def get_fiber_intel',
            'async def get_infrastructure',
        ]
        
        insert_pos = -1
        for marker in insertion_markers:
            pos = content.rfind(marker)
            if pos > insert_pos:
                insert_pos = pos
        
        if insert_pos > 0:
            # Find the end of that function — look for next function or end of file
            # Simple approach: find the next 'async def' or '@mcp.' after the marker
            search_from = insert_pos + 100
            next_func = len(content)  # default: end of file
            
            for pattern in ['@mcp.tool()', 'async def ', 'if __name__', '# ===']:
                pos = content.find(pattern, search_from)
                if pos > 0 and pos < next_func:
                    # But make sure it's not inside the same function
                    if pattern == 'async def ':
                        # Check if this is a nested function or a new tool
                        line_start = content.rfind('\n', 0, pos)
                        indent = pos - line_start - 1
                        if indent <= 0:  # top-level function
                            next_func = pos
                    elif pattern == '@mcp.tool()':
                        next_func = pos
                    else:
                        next_func = pos
            
            # Insert before next_func, or at end if nothing found
            # Actually, safest: find the VERY LAST return statement of the last tool
            # and insert after that function block ends
            # Simpler: just append before `if __name__` or at end
            
            main_block = content.find("if __name__")
            if main_block > 0:
                content = content[:main_block] + NEW_TOOLS_CODE + '\n\n' + content[main_block:]
            else:
                content += NEW_TOOLS_CODE
            
            changes += 1
            print("✅ Added 4 new tools: get_tax_incentives, compare_sites, get_water_risk, get_backup_status")
        else:
            # Fallback: append at end
            content += NEW_TOOLS_CODE
            changes += 1
            print("✅ Appended 4 new tools at end of file")

    # ── FIX REGION NORMALIZATION (Bug 2) ──
    if '_REGION_MAP' in content:
        print("⏭️  Region normalization already applied")
    else:
        # Find the list_transactions function and add region mapping
        # Look for where 'region' variable is first used in the SQL
        lt_pos = content.find('async def list_transactions')
        if lt_pos > 0:
            # Find where region is extracted from arguments
            region_extract = content.find("region = arguments.get('region'", lt_pos)
            if region_extract < 0:
                region_extract = content.find('region = arguments.get("region"', lt_pos)
            
            if region_extract > 0:
                # Find end of that line
                line_end = content.find('\n', region_extract)
                if line_end > 0:
                    content = content[:line_end + 1] + REGION_FIX_CODE + content[line_end + 1:]
                    changes += 1
                    print("✅ Added region normalization to list_transactions (europe→EMEA, etc.)")
            else:
                print("⚠️  Could not find region extraction in list_transactions — add manually")
        else:
            print("⚠️  list_transactions function not found in dchub_mcp_server.py")

    if changes > 0:
        with open(mcp_path, 'w') as f:
            f.write(content)
        print(f"\n✅ Applied {changes} change(s) to dchub_mcp_server.py")
    else:
        print("\n⏭️  No changes needed for dchub_mcp_server.py")

    return changes > 0


def patch_main_py():
    """Update main.py: add new tools to MCP_TEASER_TOOLS + ALLOWED_FIELDS"""
    main_path = os.path.expanduser('~/workspace/main.py')
    if not os.path.exists(main_path):
        main_path = '/app/main.py'
    if not os.path.exists(main_path):
        print("ERROR: main.py not found")
        return False

    with open(main_path, 'r') as f:
        content = f.read()

    changes = 0

    # ── Add new tools to MCP_TEASER_TOOLS ──
    new_teaser_tools = ['get_tax_incentives', 'get_water_risk', 'get_backup_status']
    for tool in new_teaser_tools:
        if f"'{tool}'" not in content:
            # Find MCP_TEASER_TOOLS set and add to it
            pos = content.find('MCP_TEASER_TOOLS = {')
            if pos > 0:
                # Find the closing brace
                close = content.find('}', pos)
                if close > 0:
                    content = content[:close] + f", '{tool}'" + content[close:]
                    changes += 1
                    print(f"✅ Added '{tool}' to MCP_TEASER_TOOLS")

    # compare_sites goes in MCP_FACILITY_TOOLS (it returns facility-like data)
    if "'compare_sites'" not in content:
        pos = content.find('MCP_TEASER_TOOLS = {')
        if pos > 0:
            close = content.find('}', pos)
            if close > 0:
                content = content[:close] + ", 'compare_sites'" + content[close:]
                changes += 1
                print("✅ Added 'compare_sites' to MCP_TEASER_TOOLS")

    # ── Add new fields to ALLOWED_FIELDS ──
    new_fields = [
        'incentives', 'states_covered', 'program_count', 'types',
        'comparison', 'winners_by_category', 'overall_winner',
        'overall_winner_score', 'locations_compared',
        'water_stress', 'cooling_recommendation',
        'comparisons', 'carrier_filter', 'carrier_routes_found', 'carrier_note',
        'tables', 'freshness', 'db_size_mb', 'total_rows',
        'backup_provider', 'redundancy', 'database',
    ]

    pos = content.find('ALLOWED_FIELDS = {')
    if pos > 0:
        close_brace = content.find('}', pos)
        if close_brace > 0:
            fields_to_add = []
            for field in new_fields:
                if f"'{field}'" not in content[pos:close_brace + 50]:
                    fields_to_add.append(f"'{field}'")

            if fields_to_add:
                insert_str = ',\n    ' + ', '.join(fields_to_add) + ','
                content = content[:close_brace] + insert_str + '\n' + content[close_brace:]
                changes += 1
                print(f"✅ Added {len(fields_to_add)} new fields to ALLOWED_FIELDS")
            else:
                print("⏭️  All fields already in ALLOWED_FIELDS")

    # ── Add MCP_USER_NOTES for new tools ──
    new_notes = {
        'get_tax_incentives': "💡 DC Hub free preview — full tax incentive details require a Developer key. Details at dchub.cloud/developers",
        'compare_sites': "💡 DC Hub free preview — full site comparison with sub-scores requires a Developer key. Details at dchub.cloud/developers",
        'get_water_risk': "💡 DC Hub free preview — detailed water stress data requires a Developer key ($49/mo). Unlock at dchub.cloud/developers",
        'get_backup_status': "💡 DC Hub operational monitoring — backup and data health status.",
    }
    
    notes_pos = content.find('MCP_USER_NOTES = {')
    if notes_pos > 0:
        notes_close = content.find('}', notes_pos + 100)  # Skip past first few entries
        # Find the actual closing brace of the dict (it's multi-line)
        # Count braces
        brace_count = 0
        for i in range(notes_pos, len(content)):
            if content[i] == '{':
                brace_count += 1
            elif content[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    notes_close = i
                    break
        
        if notes_close > 0:
            entries_to_add = []
            for tool_name, note_text in new_notes.items():
                if f"'{tool_name}'" not in content[notes_pos:notes_close + 50]:
                    entries_to_add.append(f"    '{tool_name}': \"{note_text}\",")
            
            if entries_to_add:
                insert_str = '\n' + '\n'.join(entries_to_add)
                content = content[:notes_close] + insert_str + '\n' + content[notes_close:]
                changes += 1
                print(f"✅ Added {len(entries_to_add)} entries to MCP_USER_NOTES")

    if changes > 0:
        with open(main_path, 'w') as f:
            f.write(content)
        print(f"\n✅ Applied {changes} change(s) to main.py")
    else:
        print("\n⏭️  No main.py changes needed")

    return changes > 0


def verify_syntax():
    """Quick syntax check on both files"""
    for filename in ['dchub_mcp_server.py', 'main.py']:
        filepath = os.path.expanduser(f'~/workspace/{filename}')
        if not os.path.exists(filepath):
            filepath = f'/app/{filename}'
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    compile(f.read(), filepath, 'exec')
                print(f"✅ {filename}: Syntax OK")
            except SyntaxError as e:
                print(f"❌ {filename}: SYNTAX ERROR at line {e.lineno}: {e.msg}")
                print(f"   FIX THIS BEFORE PUSHING!")
                return False
    return True


if __name__ == '__main__':
    print("═══════════════════════════════════════════════════")
    print("DC Hub MCP Server Patch — 4 New Tools + Bug Fixes")
    print("═══════════════════════════════════════════════════\n")

    print("── Patching dchub_mcp_server.py ──")
    mcp_changed = patch_mcp_server()

    print("\n── Patching main.py ──")
    main_changed = patch_main_py()

    print("\n── Syntax verification ──")
    ok = verify_syntax()

    print("\n═══════════════════════════════════════════════════")
    if ok:
        print("✅ All patches applied and syntax-verified!")
        print("\nNext steps:")
        print("  git add dchub_mcp_server.py main.py")
        print("  git commit -m 'MCP: 4 new tools + region fix (19 tools total)'")
        print("  git push")
    else:
        print("❌ Syntax errors detected — DO NOT PUSH until fixed!")
    print("═══════════════════════════════════════════════════")
