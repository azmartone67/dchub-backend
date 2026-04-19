"""
ETL: LBNL Queued Up — Interconnection Queue → Neon  (v3 - auto-create table)
"""

import os, sys, io, time, requests, psycopg2, argparse
import pandas as pd
from psycopg2.extras import execute_values
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("Set DATABASE_URL environment variable")

QUEUE_SHEET = "03. Complete Queue Data"
HEADER_ROW  = 1

REGION_TO_ISO = {
    "MISO": "MISO", "West": "WECC", "ERCOT": "ERCOT", "CAISO": "CAISO",
    "PJM": "PJM", "Southeast": "SERC", "SPP": "SPP",
    "NYISO": "NYISO", "ISO-NE": "ISONE",
}

STATUS_MAP = {
    "active": "active", "withdrawn": "withdrawn",
    "completed": "completed", "operational": "completed", "online": "completed",
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
}


def load_county_centroids():
    print("🗺️  Loading county centroids from Census Bureau...")
    url = "https://www2.census.gov/geo/docs/reference/cenpop2020/county/CenPop2020_Mean_CO.txt"
    local_path = "CenPop2020_Mean_CO.txt"
    try:
        if os.path.exists(local_path):
            print(f"   Using local file: {local_path}")
            df = pd.read_csv(local_path)
        else:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip().upper() for c in df.columns]
        state_col  = next((c for c in df.columns if c in ('STATEFP','STATE','STATEFIPS')), None)
        county_col = next((c for c in df.columns if c in ('COUNTYFP','COUNTY','COUNTYFIPS')), None)
        lat_col    = next((c for c in df.columns if 'LAT' in c), None)
        lng_col    = next((c for c in df.columns if 'LON' in c or 'LNG' in c), None)
        if not all([state_col, county_col, lat_col, lng_col]):
            raise ValueError(f"Unexpected columns: {list(df.columns)}")
        centroids = {}
        for _, row in df.iterrows():
            try:
                fips = int(float(row[state_col])) * 1000 + int(float(row[county_col]))
                centroids[fips] = (float(row[lat_col]), float(row[lng_col]))
            except (TypeError, ValueError):
                pass
        print(f"   Loaded {len(centroids)} county centroids")
        return centroids
    except Exception as e:
        print(f"  ⚠️  Census centroid load failed: {e} — will use state centroids")
        return {}


def safe_get(row, col):
    val = row.get(col)
    if val is None:
        return None
    if not isinstance(val, str):
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
    return val


def excel_date_to_date(val):
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            return (datetime(1899, 12, 30) + pd.Timedelta(days=int(val))).date()
        return pd.to_datetime(val).date()
    except Exception:
        return None


def process_queue_df(df, county_centroids):
    import random
    rows, skipped = [], 0
    for _, row in df.iterrows():
        state = str(safe_get(row, "state") or "").strip().upper()[:2]
        if not state:
            skipped += 1
            continue
        lat, lng = None, None
        fips_raw = safe_get(row, "fips_codes")
        if fips_raw is not None:
            try:
                fips = int(float(fips_raw))
                lat, lng = county_centroids.get(fips, (None, None))
            except (TypeError, ValueError):
                pass
        if lat is None:
            base = STATE_CENTROIDS.get(state)
            if base:
                lat = base[0] + random.uniform(-1.8, 1.8)
                lng = base[1] + random.uniform(-2.0, 2.0)
            else:
                skipped += 1
                continue
        status_raw = str(safe_get(row, "q_status") or "").strip().lower()
        queue_status = STATUS_MAP.get(status_raw, status_raw or "unknown")
        region_raw = str(safe_get(row, "region") or "").strip()
        iso = REGION_TO_ISO.get(region_raw, region_raw or None)
        mw1 = safe_get(row, "mw1")
        try:
            capacity_mw = float(mw1) if mw1 is not None else None
        except (TypeError, ValueError):
            capacity_mw = None
        queue_date = excel_date_to_date(safe_get(row, "q_date"))
        q_id      = str(safe_get(row, "q_id") or "")[:50]
        poi_name  = str(safe_get(row, "poi_name") or "")[:200]
        proj_name = str(safe_get(row, "project_name") or poi_name)[:200]
        county    = str(safe_get(row, "county") or "")[:100]
        fuel_type = str(safe_get(row, "type_clean") or safe_get(row, "type1") or "")[:100]
        rows.append((
            q_id, proj_name, iso, state, county, fuel_type,
            capacity_mw, queue_status, queue_date, poi_name,
            round(lat, 6), round(lng, 6),
        ))
    return rows, skipped


def load_interconnect_queue(conn, file_path=None):
    print("⚡ Loading LBNL Interconnect Queue data...")
    county_centroids = load_county_centroids()

    if file_path:
        print(f"   Reading local file: {file_path}")
        df = pd.read_excel(file_path, sheet_name=QUEUE_SHEET, header=HEADER_ROW)
    else:
        raise ValueError("Please provide --file argument with local LBNL Excel file")

    print(f"   Rows: {len(df)}, columns: {list(df.columns)}")
    print(f"   Status counts:\n{df['q_status'].value_counts().head()}")

    rows, skipped = process_queue_df(df, county_centroids)
    print(f"   Geocoded: {len(rows)}, skipped: {skipped}")

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS interconnect_queue (
                id SERIAL PRIMARY KEY, queue_id TEXT, project_name TEXT,
                iso TEXT, state CHAR(2), county TEXT, fuel_type TEXT,
                capacity_mw NUMERIC(10,2), queue_status TEXT, queue_date DATE,
                poi_name TEXT, lat NUMERIC(10,6), lng NUMERIC(10,6),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("DELETE FROM interconnect_queue")
    conn.commit()

    BATCH = 2000
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH):
            execute_values(cur, """
                INSERT INTO interconnect_queue
                    (queue_id, project_name, iso, state, county, fuel_type,
                     capacity_mw, queue_status, queue_date, poi_name, lat, lng)
                VALUES %s
            """, rows[i:i+BATCH])
            total += len(rows[i:i+BATCH])
            conn.commit()
            print(f"   {total}/{len(rows)} rows inserted...")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT iso, COUNT(*) projects, ROUND(SUM(capacity_mw)/1000,1) gw
            FROM interconnect_queue WHERE queue_status = 'active'
            GROUP BY iso ORDER BY gw DESC NULLS LAST
        """)
        print("\n📊 Active queue summary:")
        for r in cur.fetchall():
            print(f"   {str(r[0] or 'N/A'):10s}  {r[1]:5d} projects  {r[2]} GW")

    print(f"\n✅ Loaded {total} total queue records into Neon")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Local LBNL Excel file")
    args = parser.parse_args()

    print("⚡ Interconnect Queue ETL starting...")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        load_interconnect_queue(conn, file_path=args.file)
        print("🎉 Interconnect queue ETL complete!")
    finally:
        conn.close()
