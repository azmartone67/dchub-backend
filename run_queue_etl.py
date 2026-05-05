"""
run_queue_etl.py — interconnect queue loader v4
Usage: python run_queue_etl.py --file LBNL_Ix_Queue_Data_File_thru2024_v2.xlsx
"""
import os, io, random, argparse, psycopg2, requests
import pandas as pd
import numpy as np
from psycopg2.extras import execute_values
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("Set DATABASE_URL environment variable")

QUEUE_SHEET = "03. Complete Queue Data"
HEADER_ROW  = 1

REGION_TO_ISO = {
    "MISO":"MISO","West":"WECC","ERCOT":"ERCOT","CAISO":"CAISO",
    "PJM":"PJM","Southeast":"SERC","SPP":"SPP","NYISO":"NYISO","ISO-NE":"ISONE",
}
STATUS_MAP = {
    "active":"active","withdrawn":"withdrawn",
    "completed":"completed","operational":"completed","online":"completed",
}
STATE_CENTROIDS = {
    "AL":(32.80,-86.79),"AK":(61.37,-152.40),"AZ":(34.17,-111.09),
    "AR":(34.75,-92.29),"CA":(36.78,-119.42),"CO":(39.11,-105.36),
    "CT":(41.60,-72.69),"DE":(38.91,-75.53),"FL":(27.99,-81.76),
    "GA":(33.25,-83.44),"HI":(20.80,-156.33),"ID":(44.07,-114.74),
    "IL":(40.35,-88.99),"IN":(39.85,-86.26),"IA":(42.01,-93.21),
    "KS":(38.53,-96.73),"KY":(37.67,-84.67),"LA":(31.17,-91.87),
    "ME":(45.25,-69.00),"MD":(39.06,-76.80),"MA":(42.23,-71.53),
    "MI":(44.18,-84.48),"MN":(46.39,-94.64),"MS":(32.74,-89.68),
    "MO":(38.46,-92.29),"MT":(46.88,-110.36),"NE":(41.49,-99.90),
    "NV":(38.50,-117.02),"NH":(43.69,-71.58),"NJ":(40.06,-74.41),
    "NM":(34.84,-106.25),"NY":(42.17,-74.95),"NC":(35.63,-79.81),
    "ND":(47.53,-99.78),"OH":(40.39,-82.76),"OK":(35.56,-96.93),
    "OR":(43.94,-120.56),"PA":(40.59,-77.21),"RI":(41.68,-71.51),
    "SC":(33.84,-80.90),"SD":(44.37,-100.34),"TN":(35.86,-86.66),
    "TX":(31.52,-99.33),"UT":(39.32,-111.09),"VT":(44.06,-72.71),
    "VA":(37.77,-78.17),"WA":(47.40,-121.49),"WV":(38.49,-80.95),
    "WI":(44.27,-89.62),"WY":(42.76,-107.30),
    "DC":(38.91,-77.02),"PR":(18.22,-66.59),
}

def val_clean(v):
    """Return None for NaN/None/empty, else strip string."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    s = str(v).strip()
    return None if s in ("", "nan", "NaN", "None") else s

def load_county_centroids():
    print("🗺️  Loading county centroids...")
    local = "CenPop2020_Mean_CO.txt"
    url = "https://www2.census.gov/geo/docs/reference/cenpop2020/county/CenPop2020_Mean_CO.txt"
    try:
        df = pd.read_csv(local) if os.path.exists(local) else \
             pd.read_csv(io.StringIO(requests.get(url, timeout=30).text))
        df.columns = [c.strip().upper() for c in df.columns]
        sc = next((c for c in df.columns if c in ('STATEFP','STATE','STATEFIPS')), None)
        cc = next((c for c in df.columns if c in ('COUNTYFP','COUNTY','COUNTYFIPS')), None)
        lc = next((c for c in df.columns if c.startswith('LAT') or c == 'LATITUDE'), None)
        nc = next((c for c in df.columns if 'LON' in c or 'LNG' in c), None)
        if not all([sc,cc,lc,nc]):
            raise ValueError(f"cols: {list(df.columns)}")
        out = {}
        for _, r in df.iterrows():
            try:
                fips = int(float(r[sc]))*1000 + int(float(r[cc]))
                out[fips] = (float(r[lc]), float(r[nc]))
            except Exception:
                pass
        print(f"   Loaded {len(out)} county centroids")
        # Debug: show first 3 entries
        for k, v in list(out.items())[:3]:
            print(f"   Sample centroid: fips={k} → lat={v[0]}, lng={v[1]}")
        return out
    except Exception as e:
        print(f"  ⚠️  Centroid load failed: {e}")
        return {}

def main(file_path):
    centroids = load_county_centroids()

    print(f"   Reading: {file_path}")
    df = pd.read_excel(file_path, sheet_name=QUEUE_SHEET, header=HEADER_ROW)
    print(f"   {len(df)} rows, columns: {list(df.columns[:10])}...")

    # Debug: sample state values
    if 'state' in df.columns:
        sample = df['state'].dropna().head(5).tolist()
        print(f"   Sample state values: {sample}")
        print(f"   State non-null count: {df['state'].notna().sum()}")
    else:
        print("   ⚠️  No 'state' column found!")

    rows = []
    skip_no_state = skip_no_coord = skip_bounds = 0

    for _, row in df.iterrows():
        # --- State ---
        raw_state = row.get('state') if hasattr(row, 'get') else row['state']
        state = val_clean(raw_state)
        if state is None:
            skip_no_state += 1
            continue
        state = state.upper()[:2]
        if state in ("", "NA") or (len(state) < 2 and state not in STATE_CENTROIDS):
            skip_no_state += 1
            continue

        # --- FIPS geocode ---
        lat, lng = None, None
        fips_raw = val_clean(row.get('fips_codes') if hasattr(row, 'get') else row['fips_codes'])
        if fips_raw:
            # fips_codes may be comma-separated; use first FIPS
            first_fips = fips_raw.split(',')[0].strip()
            try:
                fips = int(float(first_fips))
                result = centroids.get(fips)
                if result:
                    lat, lng = result
            except Exception:
                pass

        # --- State centroid fallback ---
        if lat is None:
            base = STATE_CENTROIDS.get(state)
            if base:
                lat = base[0] + random.uniform(-1.5, 1.5)
                lng = base[1] + random.uniform(-1.8, 1.8)
            else:
                skip_no_coord += 1
                continue

        # --- Bounds check ---
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            if skip_bounds < 5:
                print(f"  ⚠️  Bad coords: lat={lat}, lng={lng}, state={state}, fips_raw={fips_raw}")
            skip_bounds += 1
            continue

        # --- Fields ---
        status_raw = val_clean(row.get('q_status') if hasattr(row, 'get') else row['q_status']) or ""
        queue_status = STATUS_MAP.get(status_raw.lower(), status_raw.lower() or "unknown")

        region_raw = val_clean(row.get('region') if hasattr(row, 'get') else row['region']) or ""
        iso = REGION_TO_ISO.get(region_raw, region_raw or None)

        mw1_raw = val_clean(row.get('mw1') if hasattr(row, 'get') else row['mw1'])
        try:
            capacity_mw = float(mw1_raw) if mw1_raw else None
        except Exception:
            capacity_mw = None

        qd_raw = val_clean(row.get('q_date') if hasattr(row, 'get') else row['q_date'])
        try:
            queue_date = pd.to_datetime(qd_raw).date() if qd_raw else None
        except Exception:
            queue_date = None

        q_id      = (val_clean(row.get('q_id') if hasattr(row,'get') else row['q_id']) or "")[:50]
        poi_name  = (val_clean(row.get('poi_name') if hasattr(row,'get') else row['poi_name']) or "")[:200]
        proj_raw  = val_clean(row.get('project_name') if hasattr(row,'get') else row['project_name'])
        proj_name = (proj_raw or poi_name)[:200]
        county    = (val_clean(row.get('county') if hasattr(row,'get') else row['county']) or "")[:100]
        tc_raw    = val_clean(row.get('type_clean') if hasattr(row,'get') else row['type_clean'])
        t1_raw    = val_clean(row.get('type1') if hasattr(row,'get') else row['type1'])
        fuel_type = (tc_raw or t1_raw or "")[:100]

        rows.append((q_id, proj_name, iso, state, county, fuel_type,
                     capacity_mw, queue_status, queue_date, poi_name,
                     float(lat), float(lng)))

    print(f"   Geocoded: {len(rows)}")
    print(f"   Skipped — no state: {skip_no_state}, no coord: {skip_no_coord}, bounds: {skip_bounds}")

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS interconnect_queue")
            cur.execute("""
                CREATE TABLE interconnect_queue (
                    id           SERIAL PRIMARY KEY,
                    queue_id     TEXT, project_name TEXT,
                    iso          TEXT, state CHAR(2), county TEXT, fuel_type TEXT,
                    capacity_mw  DOUBLE PRECISION, queue_status TEXT, queue_date DATE,
                    poi_name     TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()
        print("   Table created")

        BATCH, total = 2000, 0
        with conn.cursor() as cur:
            for i in range(0, len(rows), BATCH):
                execute_values(cur, """
                    INSERT INTO interconnect_queue
                        (queue_id,project_name,iso,state,county,fuel_type,
                         capacity_mw,queue_status,queue_date,poi_name,lat,lng)
                    VALUES %s
                """, rows[i:i+BATCH])
                total += len(rows[i:i+BATCH])
                conn.commit()
                print(f"   {total}/{len(rows)} rows inserted...")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT iso, COUNT(*) projects,
                       ROUND(SUM(capacity_mw)::NUMERIC / 1000, 1) gw
                FROM interconnect_queue WHERE queue_status='active'
                GROUP BY iso ORDER BY gw DESC NULLS LAST
            """)
            print("\n📊 Active queue by ISO:")
            for r in cur.fetchall():
                print(f"   {str(r[0] or 'N/A'):10s}  {r[1]:5d} projects  {r[2]} GW")

        print(f"\n✅ Done — {total} rows loaded into interconnect_queue")
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    print("⚡ Interconnect Queue ETL starting...")
    main(args.file)