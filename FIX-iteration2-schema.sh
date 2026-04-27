#!/usr/bin/env bash
# Iteration 2 schema fix — replaces dchub_iteration_2_routes.py with v2
# (correct columns: provider/latitude/longitude, lat/lng, no geom; tolerant
# table-name discovery for transmission and pipeline).
set -e
cd ~/workspace

# Extract the corrected blueprint and overwrite the existing file
echo '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' | base64 -d | tar xzf -
mv dchub_iteration_2_routes_v2.py dchub_iteration_2_routes.py
echo "Replaced dchub_iteration_2_routes.py ($(wc -l < dchub_iteration_2_routes.py) lines)"

# Sanity check imports cleanly
python3 -c "
import sys; sys.path.insert(0, '.')
from dchub_iteration_2_routes import register_iteration_2_routes
print('LOCAL IMPORT OK')
"

# Commit + push
git add dchub_iteration_2_routes.py
git commit -m "fix(iteration2): match real Neon schema; add tolerant table discovery

Discovered schema mismatches via Railway logs and a dump-information_schema query:
- facilities uses provider (not operator), latitude/longitude (not lat/lon), power_mw
- substations/gas_pipelines use lat/lng (not lat/lon) and have no geom column
- fiber_routes is line segments with start_lat/lng + end_lat/lng, no geom
- transmission_lines table doesn't exist under that name — tolerant lookup
  across (transmission_lines, hifld_transmission_lines, transmission, ...)
- dc_pipeline doesn't exist — discover among capacity_pipeline,
  discovered_pipelines, ps_pipeline, dc_properties; introspect columns

Replaces PostGIS ST_DWithin/ST_DistanceSphere with bounding-box prefilter +
haversine since no geom column exists. Same UX (radius_km param) on the
facility infrastructure route; bbox snapshot unchanged from caller's POV.

Wires real ingestion entry: deal_scraper.run_scrape(dry_run=False) -> Dict
with deal_ingestion_scheduler.run_ingestion(get_db) as fallback."
git push origin main

echo ""
echo "=========================================================="
echo "Wait 90s for Railway, then verify with these:"
echo ""
echo "  curl -s -X POST https://dchub-backend-production.up.railway.app/api/v1/transactions/ingest | python3 -m json.tool | head -40"
echo ""
echo "  curl -s 'https://dchub-backend-production.up.railway.app/api/v1/land-power/snapshot?bbox=-78,38.5,-77,39.5' | python3 -m json.tool | head -30"
echo ""
echo "  curl -s 'https://dchub-backend-production.up.railway.app/api/v1/facilities/<some-slug>/infrastructure' | python3 -m json.tool | head -30"
echo ""
echo "Expected counts on the snapshot for that NoVA bbox:"
echo "  facilities>0, substations>0, fiber>0, pipeline maybe, gas=0 (table empty)"
echo "  transmission depends on tolerant lookup — meta.transmission_table tells us which name worked"
echo "=========================================================="
