#!/bin/bash
# ============================================================================
# DC Hub Remaining Tasks — March 25, 2026
# Run from Railway shell after Railway edge network recovers
# ============================================================================

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  DC Hub Remaining Tasks — Run in Order                  ║"
echo "╚══════════════════════════════════════════════════════════╝"

# ============================================================================
# TASK 1: Verify Railway edge is back
# ============================================================================
echo ""
echo "━━━ TASK 1: Check Railway Edge Network ━━━"
HEALTH=$(curl -s -m 5 https://dchub.cloud/api/health | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('environment','FAIL'))" 2>&1)
MCP=$(curl -s -m 10 -X POST https://dchub.cloud/mcp -H "Content-Type: application/json" -H "Accept: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_intelligence_index","arguments":{}}}' | wc -c)
echo "  Health: $HEALTH"
echo "  MCP response size: ${MCP}b (should be >200)"
if [ "$MCP" -lt 200 ]; then
    echo "  ❌ Railway edge still down. Wait and retry."
    echo "  Check: https://status.railway.app"
    exit 1
fi
echo "  ✅ Railway edge is back!"

# ============================================================================
# TASK 2: Full MCP Tool Test Suite (22 tests)
# ============================================================================
echo ""
echo "━━━ TASK 2: MCP Full Test Suite ━━━"
PASS=0; FAIL=0; TOTAL=0
test_tool() {
    local name=$1; local args=$2; TOTAL=$((TOTAL+1))
    local result=$(curl -s -m 15 -X POST https://dchub.cloud/mcp \
      -H "Content-Type: application/json" -H "Accept: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":$TOTAL,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}" 2>&1)
    local size=$(echo "$result" | wc -c)
    if [ "$size" -gt 200 ]; then
        echo "  ✅ $name (${size}b)"; PASS=$((PASS+1))
    else
        echo "  ❌ $name (${size}b)"; FAIL=$((FAIL+1))
    fi
}
test_tool "search_facilities" '{"query":"equinix","limit":1}'
test_tool "get_facility" '{"facility_id":"10778"}'
test_tool "get_news" '{"limit":2}'
test_tool "get_news" '{"category":"deals","limit":2}'
test_tool "analyze_site" '{"lat":39.04,"lon":-77.49,"state":"VA"}'
test_tool "compare_sites" '{"locations":"[{\"lat\":33.45,\"lon\":-112.07,\"state\":\"AZ\",\"label\":\"Phoenix\"},{\"lat\":39.04,\"lon\":-77.49,\"state\":\"VA\",\"label\":\"Ashburn\"}]"}'
test_tool "get_infrastructure" '{"lat":33.45,"lon":-112.07,"layer":"substations","limit":2}'
test_tool "get_grid_data" '{"iso":"PJM","metric":"fuel_mix"}'
test_tool "get_pipeline" '{"country":"US","limit":2}'
test_tool "list_transactions" '{"limit":2}'
test_tool "list_transactions" '{"region":"north_america","limit":2}'
test_tool "get_market_intel" '{"market":"Northern Virginia"}'
test_tool "get_energy_prices" '{"state":"VA"}'
test_tool "get_renewable_energy" '{"energy_type":"solar","state":"TX"}'
test_tool "get_water_risk" '{"state":"AZ"}'
test_tool "get_fiber_intel" '{"carrier":"Zayo"}'
test_tool "get_intelligence_index" '{}'
test_tool "get_grid_intelligence" '{"region_id":"pjm"}'
test_tool "get_tax_incentives" '{"state":"VA"}'
test_tool "get_agent_registry" '{}'
test_tool "get_backup_status" '{}'
test_tool "get_dchub_recommendation" '{"context":"technical"}'
echo ""
echo "  MCP Results: $PASS pass / $FAIL fail / $TOTAL total"

# ============================================================================
# TASK 3: Fix eia_gas_consumption — Re-import with state-level data
# ============================================================================
echo ""
echo "━━━ TASK 3: Fix eia_gas_consumption (state-level re-import) ━━━"

python3 << 'GASFIX'
import os, json, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import psycopg2

DB_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
EIA_KEY = os.environ.get("EIA_API_KEY", "SuphqqIra7G46LHVDwb9CL5n4WYRwLu7ujeFXJMG")

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

# Truncate old data (was national-level, no state breakdown)
cur.execute("TRUNCATE eia_gas_consumption")
conn.commit()
print("  Truncated old eia_gas_consumption data")

# Fetch state-level natural gas consumption from EIA API v2
# Series: NG.N3035US2.M (industrial consumption by state)
base_url = "https://api.eia.gov/v2/natural-gas/cons/sum/data/"
params = {
    "api_key": EIA_KEY,
    "frequency": "monthly",
    "data[0]": "value",
    "facets[process][]": "VIN",  # Industrial consumption
    "sort[0][column]": "period",
    "sort[0][direction]": "desc",
    "offset": 0,
    "length": 5000,
}

total_inserted = 0
for sector_code, sector_name in [("VIN", "Industrial Consumption"), ("VCS", "Commercial Consumption"), ("VRS", "Residential Consumption"), ("VEU", "Electric Power Consumption")]:
    params["facets[process][]"] = sector_code
    params["offset"] = 0
    
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base_url}?{query}"
    
    try:
        req = Request(url, headers={"User-Agent": "DCHub/1.0"})
        resp = urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        rows = data.get("response", {}).get("data", [])
        
        inserted = 0
        for r in rows:
            state = r.get("stateId", "")
            state_name = r.get("stateDescription", "")
            if not state or state == "US":
                continue  # Skip national totals
            try:
                cur.execute("""
                    INSERT INTO eia_gas_consumption (period, state, state_name, sector, sector_name, value, units)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (r.get("period",""), state, state_name, sector_code, sector_name, r.get("value"), r.get("units", "MMCF")))
                inserted += 1
            except Exception:
                conn.rollback()
                conn = psycopg2.connect(DB_URL)
                cur = conn.cursor()
        
        conn.commit()
        total_inserted += inserted
        print(f"  {sector_name}: {len(rows)} fetched, {inserted} state-level inserted")
        time.sleep(0.5)
    except Exception as e:
        print(f"  {sector_name}: ERROR — {e}")
        try:
            conn.rollback()
        except:
            conn = psycopg2.connect(DB_URL)
            cur = conn.cursor()

# Verify
cur.execute("SELECT count(*), count(DISTINCT state) FROM eia_gas_consumption WHERE state != '' AND state != 'US'")
total, states = cur.fetchone()
print(f"  Result: {total} state-level records across {states} states")
conn.close()
GASFIX

# ============================================================================
# TASK 4: Run Master Discovery v2 (EIA generators, HIFLD gas, NASA, PeeringDB)
# ============================================================================
echo ""
echo "━━━ TASK 4: Master Discovery v2 Script ━━━"
echo "  Running dchub_master_discovery_v2.py (may take 5-10 min)..."
timeout 600 python3 ~/workspace/scripts/dchub_master_discovery_v2.py 2>&1 | tee /tmp/discovery_v2.log | tail -30
echo ""
echo "  Full log: cat /tmp/discovery_v2.log"

# ============================================================================
# TASK 5: Verify MCP Response Cache
# ============================================================================
echo ""
echo "━━━ TASK 5: Verify MCP Response Cache ━━━"
echo "  First call (cache miss):"
time curl -s -X POST https://dchub.cloud/mcp -H "Content-Type: application/json" -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_intelligence_index","arguments":{}}}' > /dev/null 2>&1
echo "  Second call (should be cache hit — much faster):"
time curl -s -X POST https://dchub.cloud/mcp -H "Content-Type: application/json" -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_intelligence_index","arguments":{}}}' > /dev/null 2>&1

# ============================================================================
# TASK 6: Final Database Audit
# ============================================================================
echo ""
echo "━━━ TASK 6: Final Database Audit ━━━"
psql $NEON_DATABASE_URL -c "
SELECT tbl, cnt FROM (
  SELECT 'facilities' as tbl, count(*) as cnt FROM facilities
  UNION ALL SELECT 'discovered_facilities', count(*) FROM discovered_facilities
  UNION ALL SELECT 'deals', count(*) FROM deals
  UNION ALL SELECT 'announcements', count(*) FROM announcements
  UNION ALL SELECT 'substations', count(*) FROM substations
  UNION ALL SELECT 'gas_pipelines', count(*) FROM gas_pipelines
  UNION ALL SELECT 'power_plants_eia', count(*) FROM power_plants_eia
  UNION ALL SELECT 'transmission_lines_eia', count(*) FROM transmission_lines_eia
  UNION ALL SELECT 'eia_generators', count(*) FROM eia_generators
  UNION ALL SELECT 'eia_generators_with_coords', (SELECT count(*) FROM eia_generators WHERE latitude IS NOT NULL)
  UNION ALL SELECT 'eia_gas_consumption', count(*) FROM eia_gas_consumption
  UNION ALL SELECT 'eia_retail_rates', count(*) FROM eia_retail_rates
  UNION ALL SELECT 'fiber_routes', count(*) FROM fiber_routes
  UNION ALL SELECT 'metro_dark_fiber', count(*) FROM metro_dark_fiber
  UNION ALL SELECT 'submarine_cables', count(*) FROM submarine_cables
  UNION ALL SELECT 'fema_risk_index', count(*) FROM fema_risk_index
  UNION ALL SELECT 'epa_egrid', count(*) FROM epa_egrid
  UNION ALL SELECT 'usgs_water_stress', count(*) FROM usgs_water_stress
  UNION ALL SELECT 'tax_incentives_neon', count(*) FROM tax_incentives_neon
  UNION ALL SELECT 'energy_ppas', count(*) FROM energy_ppas
  UNION ALL SELECT 'global_sources', count(*) FROM global_sources
  UNION ALL SELECT 'gdci_scores', count(*) FROM gdci_scores
  UNION ALL SELECT 'capacity_pipeline', count(*) FROM capacity_pipeline
  UNION ALL SELECT 'users', count(*) FROM users
  UNION ALL SELECT 'api_keys', count(*) FROM api_keys
  UNION ALL SELECT 'agent_registry', count(*) FROM agent_registry
) t ORDER BY tbl;
"

echo ""
echo "━━━ TASK 7: Check for remaining data gaps ━━━"
# Tables that should exist but might not
for tbl in peeringdb_netfac fcc_fiber_deployments hifld_gas_compressors hifld_gas_processing_plants nasa_power_climate peeringdb_ix eia_rto_hourly eia_gas_storage; do
    EXISTS=$(psql -t $NEON_DATABASE_URL -c "SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = '$tbl');" 2>/dev/null | tr -d ' ')
    if [ "$EXISTS" = "t" ]; then
        CNT=$(psql -t $NEON_DATABASE_URL -c "SELECT count(*) FROM $tbl;" 2>/dev/null | tr -d ' ')
        echo "  ✅ $tbl: $CNT rows"
    else
        echo "  ❌ $tbl: TABLE MISSING"
    fi
done

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ALL TASKS COMPLETE                                      ║"
echo "║  Come back to Claude chat for MCP verification           ║"
echo "╚══════════════════════════════════════════════════════════╝"
