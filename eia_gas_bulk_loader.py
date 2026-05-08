"""
eia_gas_bulk_loader.py — Pull all US gas pipelines from EIA ArcGIS into Neon
═══════════════════════════════════════════════════════════════════════════════
Run in Railway shell:
  export NEON_URL="postgresql://neondb_owner:...@ep-old-waterfall-aa2rwjzs-pooler.westus3.azure.neon.tech/neondb%ssslmode=require"
  python eia_gas_bulk_loader.py

Pulls from EIA Natural Gas Interstate and Intrastate Pipelines FeatureServer.
Extracts midpoint coordinates from polyline geometry.
Inserts into gas_pipelines table with ON CONFLICT DO NOTHING.
"""
import os
import sys
import json
import time
import math
import urllib.request
import psycopg2

EIA_URL = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0/query"

# State lookup by lat/lng (approximate bounding boxes)
STATE_BOXES = {
    'AL': (30.2, 35.0, -88.5, -84.9), 'AK': (51.2, 71.4, -179.1, -129.9),
    'AZ': (31.3, 37.0, -114.8, -109.0), 'AR': (33.0, 36.5, -94.6, -89.6),
    'CA': (32.5, 42.0, -124.4, -114.1), 'CO': (37.0, 41.0, -109.1, -102.0),
    'CT': (41.0, 42.1, -73.7, -71.8), 'DE': (38.5, 39.8, -75.8, -75.0),
    'FL': (24.5, 31.0, -87.6, -80.0), 'GA': (30.4, 35.0, -85.6, -80.8),
    'HI': (18.9, 22.2, -160.2, -154.8), 'ID': (42.0, 49.0, -117.2, -111.0),
    'IL': (37.0, 42.5, -91.5, -87.5), 'IN': (37.8, 41.8, -88.1, -84.8),
    'IA': (40.4, 43.5, -96.6, -90.1), 'KS': (37.0, 40.0, -102.1, -94.6),
    'KY': (36.5, 39.1, -89.6, -81.9), 'LA': (29.0, 33.0, -94.0, -89.0),
    'ME': (43.1, 47.5, -71.1, -66.9), 'MD': (38.0, 39.7, -79.5, -75.0),
    'MA': (41.2, 42.9, -73.5, -69.9), 'MI': (41.7, 48.3, -90.4, -82.4),
    'MN': (43.5, 49.4, -97.2, -89.5), 'MS': (30.2, 35.0, -91.7, -88.1),
    'MO': (36.0, 40.6, -95.8, -89.1), 'MT': (44.4, 49.0, -116.0, -104.0),
    'NE': (40.0, 43.0, -104.1, -95.3), 'NV': (35.0, 42.0, -120.0, -114.0),
    'NH': (42.7, 45.3, -72.6, -70.7), 'NJ': (38.9, 41.4, -75.6, -73.9),
    'NM': (31.3, 37.0, -109.1, -103.0), 'NY': (40.5, 45.0, -79.8, -71.9),
    'NC': (33.8, 36.6, -84.3, -75.5), 'ND': (45.9, 49.0, -104.0, -96.6),
    'OH': (38.4, 42.0, -84.8, -80.5), 'OK': (33.6, 37.0, -103.0, -94.4),
    'OR': (42.0, 46.3, -124.6, -116.5), 'PA': (39.7, 42.3, -80.5, -74.7),
    'RI': (41.1, 42.0, -71.9, -71.1), 'SC': (32.0, 35.2, -83.4, -78.5),
    'SD': (42.5, 45.9, -104.1, -96.4), 'TN': (35.0, 36.7, -90.3, -81.6),
    'TX': (25.8, 36.5, -106.6, -93.5), 'UT': (37.0, 42.0, -114.1, -109.0),
    'VT': (42.7, 45.0, -73.4, -71.5), 'VA': (36.5, 39.5, -83.7, -75.2),
    'WA': (45.5, 49.0, -124.8, -116.9), 'WV': (37.2, 40.6, -82.6, -77.7),
    'WI': (42.5, 47.1, -92.9, -86.8), 'WY': (41.0, 45.0, -111.1, -104.1),
}

def lat_lng_to_state(lat, lng):
    best = None
    best_dist = 999
    for state, (s, n, w, e) in STATE_BOXES.items():
        if s <= lat <= n and w <= lng <= e:
            # Center distance for tiebreaking
            clat = (s + n) / 2
            clng = (w + e) / 2
            dist = math.sqrt((lat - clat)**2 + (lng - clng)**2)
            if dist < best_dist:
                best_dist = dist
                best = state
    return best or ''

def fetch_batch(fid_start, fid_end, batch_size=1000):
    """Fetch a batch of pipelines from EIA ArcGIS"""
    all_features = []
    offset = 0
    while True:
        params = (
            f"%swhere=FID>{fid_start}+AND+FID<={fid_end}"
            f"&outFields=Operator,TYPEPIPE,Status,FID"
            f"&returnGeometry=true"
            f"&resultOffset={offset}"
            f"&resultRecordCount={batch_size}"
            f"&f=json"
        )
        url = EIA_URL + params
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'DCHub-GasPipelineLoader/1.0')
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            features = data.get('features', [])
            if not features:
                break
            all_features.extend(features)
            if len(features) < batch_size:
                break
            offset += batch_size
        except Exception as e:
            print(f"  ⚠️ Fetch error at offset {offset}: {e}")
            break
    return all_features

def main():
    neon_url = os.environ.get('NEON_URL')
    if not neon_url:
        # Fall back to DATABASE_URL
        neon_url = os.environ.get('DATABASE_URL', '')
    
    if not neon_url or 'neon' not in neon_url:
        print("❌ Set NEON_URL to your Neon connection string")
        sys.exit(1)
    
    print(f"🔌 Connecting to Neon...")
    conn = psycopg2.connect(neon_url)
    conn.autocommit = True
    cur = conn.cursor()
    
    # Check current count
    cur.execute("SELECT COUNT(*) FROM gas_pipelines")
    before = cur.fetchone()[0]
    print(f"📊 Current gas_pipelines count: {before}")
    
    # First, find the max FID in the EIA dataset
    print("🔍 Finding max FID...")
    try:
        url = EIA_URL + "%swhere=1=1&outFields=FID&returnGeometry=false&orderByFields=FID+DESC&resultRecordCount=1&f=json"
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'DCHub-GasPipelineLoader/1.0')
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        max_fid = data['features'][0]['attributes']['FID']
        print(f"   Max FID: {max_fid}")
    except Exception as e:
        print(f"   ⚠️ Could not get max FID: {e}, using 200000")
        max_fid = 200000
    
    # Pull in batches of 2000
    batch_size = 2000
    total_inserted = 0
    total_skipped = 0
    total_fetched = 0
    
    fid = 0
    while fid < max_fid:
        fid_end = fid + batch_size
        print(f"📡 Fetching FID {fid}-{fid_end}...", end=" ", flush=True)
        
        features = fetch_batch(fid, fid_end)
        total_fetched += len(features)
        
        batch_inserted = 0
        for feat in features:
            attrs = feat.get('attributes', {})
            geom = feat.get('geometry', {})
            operator = attrs.get('Operator', 'Unknown')
            typepipe = attrs.get('TYPEPIPE', 'Interstate')
            status = attrs.get('Status', 'Operating')
            fid_val = attrs.get('FID', '')
            
            if str(status).lower() not in ('operating', 'active', 'in service'):
                continue
            
            # Extract midpoint from polyline geometry
            lat = lng = None
            if geom:
                if 'paths' in geom and geom['paths']:
                    path = geom['paths'][0]
                    mid = path[len(path) // 2] if path else None
                    if mid and len(mid) >= 2:
                        lng, lat = mid[0], mid[1]
                elif 'x' in geom and 'y' in geom:
                    lng, lat = geom['x'], geom['y']
            
            if not lat or not lng:
                continue
            
            state = lat_lng_to_state(lat, lng)
            pipe_type = 'interstate' if 'Interstate' in str(typepipe) else 'intrastate'
            name = f"{operator} ({typepipe})"[:200]
            source_id = f"eia_gas_{fid_val}"
            
            try:
                cur.execute("""
                    INSERT INTO gas_pipelines
                    (name, operator, pipeline_type, diameter_inches, capacity_mcf, status,
                     lat, lng, city, state, source, source_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_id) DO NOTHING
                """, (
                    name, str(operator)[:100], pipe_type, None, None, 'active',
                    lat, lng, '', state, 'eia', source_id[:100]
                ))
                if cur.rowcount > 0:
                    batch_inserted += 1
                    total_inserted += 1
                else:
                    total_skipped += 1
            except Exception as e:
                total_skipped += 1
        
        print(f"{len(features)} fetched, {batch_inserted} inserted")
        fid = fid_end
        
        # Be nice to the API
        time.sleep(0.5)
    
    # Final count
    cur.execute("SELECT COUNT(*) FROM gas_pipelines")
    after = cur.fetchone()[0]
    
    cur.execute("SELECT state, COUNT(*) FROM gas_pipelines WHERE source='eia' GROUP BY state ORDER BY count DESC LIMIT 10")
    top_states = cur.fetchall()
    
    conn.close()
    
    print(f"\n{'='*60}")
    print(f"✅ EIA Gas Pipeline Bulk Load Complete!")
    print(f"   Before: {before} | Fetched: {total_fetched} | Inserted: {total_inserted} | Skipped: {total_skipped}")
    print(f"   After: {after}")
    print(f"\n   Top states:")
    for state, count in top_states:
        print(f"     {state or '%s%s'}: {count}")

if __name__ == '__main__':
    main()

# === phase 92: source-registry heartbeat (auto-fires on clean module exit) ===
# Non-invasive: never crashes the script if the registry is unreachable.
# Source ID: backend-eia-bulk-loader
_phase92_heartbeat_registered = True
try:
    import atexit as _phase92_atexit
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    def _phase92_emit():
        try:
            _phase92_heartbeat("backend-eia-bulk-loader", status="success",
                              metadata={"trigger": "atexit"})
        except Exception:
            pass
    _phase92_atexit.register(_phase92_emit)
except Exception:
    pass  # heartbeat module unavailable; extractor continues normally
