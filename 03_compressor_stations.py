"""
ETL: HIFLD — Natural Gas Compressor Stations → Neon  (v2)

Tries multiple ArcGIS FeatureServer URLs. If all fail, downloads
the CSV directly from HIFLD Open Data.

Run:  python 03_compressor_stations.py
Env:  DATABASE_URL=postgresql://...

Manual fallback:
  1. Go to: https://hifld-geoplatform.opendata.arcgis.com/datasets/geoplatform::natural-gas-compressor-stations/explore
  2. Click Download → CSV
  3. Run: python 03_compressor_stations.py --file natural_gas_compressor_stations.csv
"""

import os, io, time, requests, psycopg2, argparse, csv
from psycopg2.extras import execute_values

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("Set DATABASE_URL environment variable")

ARCGIS_URLS = [
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query",
    "https://services.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query",
    "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/13/query",
]

# Direct CSV download (HIFLD Open Data)
CSV_DOWNLOAD_URL = (
    "https://opendata.arcgis.com/api/v3/datasets/"
    "natural-gas-compressor-stations/downloads/data?format=csv&spatialRefId=4326"
)
# Alternate direct download with item ID
CSV_DOWNLOAD_ALT = (
    "https://opendata.arcgis.com/datasets/"
    "4c9e4e79b4a04d78a6524d927900e623_0.csv"
)

BATCH_SIZE = 1000


def try_arcgis_rest(url):
    params = {
        "where": "1=1", "outFields": "*",
        "returnGeometry": "true", "geometryType": "esriGeometryPoint",
        "f": "json", "resultOffset": 0,
        "resultRecordCount": BATCH_SIZE,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        if "error" in data:
            return None
        features = data.get("features", [])
        if not features:
            return None

        all_features = list(features)
        while len(features) == BATCH_SIZE:
            params["resultOffset"] += BATCH_SIZE
            r2 = requests.get(url, params=params, timeout=20)
            features = r2.json().get("features", [])
            all_features.extend(features)
            print(f"   Fetched {len(all_features)} so far...")
            time.sleep(0.3)
        return all_features
    except Exception as e:
        print(f"   Failed ({url[:60]}...): {e}")
        return None


def features_to_rows(features):
    rows, skipped = [], 0
    for feat in features:
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        lng = geom.get("x"); lat = geom.get("y")
        if not lat or not lng or lat == -9999 or lng == -9999:
            skipped += 1; continue

        hifld_id     = str(attrs.get("OBJECTID") or attrs.get("FID") or "")
        station_name = (attrs.get("NAME") or attrs.get("OPERATOR") or
                        attrs.get("name") or "Unknown")
        operator     = attrs.get("OPERATOR") or attrs.get("operator") or ""
        county       = attrs.get("COUNTY") or attrs.get("county") or ""
        state        = attrs.get("STATE") or attrs.get("state") or ""

        rows.append((hifld_id, station_name, operator, county, state,
                     round(float(lat), 6), round(float(lng), 6)))
    return rows, skipped


def csv_to_rows(content):
    rows, skipped = [], 0
    text   = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    print(f"   CSV columns: {reader.fieldnames}")

    for i, row in enumerate(reader):
        lat = row.get("Y") or row.get("LATITUDE") or row.get("lat") or row.get("y")
        lng = row.get("X") or row.get("LONGITUDE") or row.get("lng") or row.get("x")
        if not lat or not lng:
            skipped += 1; continue
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            skipped += 1; continue
        if lat == 0 and lng == 0:
            skipped += 1; continue

        hifld_id     = str(row.get("OBJECTID") or row.get("FID") or i)
        station_name = row.get("NAME") or row.get("OPERATOR") or "Unknown"
        operator     = row.get("OPERATOR") or ""
        county       = row.get("COUNTY") or ""
        state        = row.get("STATE") or row.get("State") or ""

        rows.append((hifld_id, station_name, operator, county, state,
                     round(lat, 6), round(lng, 6)))
    return rows, skipped


def insert_rows(conn, rows):
    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO gas_compressor_stations
                (hifld_id, station_name, operator, county, state, lat, lng)
            VALUES %s
            ON CONFLICT (hifld_id) DO UPDATE SET
                station_name=EXCLUDED.station_name, operator=EXCLUDED.operator,
                county=EXCLUDED.county, state=EXCLUDED.state,
                lat=EXCLUDED.lat, lng=EXCLUDED.lng, loaded_at=NOW()
        """, rows)
    conn.commit()
    print(f"✅ Loaded {len(rows)} compressor stations into Neon")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to manually downloaded CSV", default=None)
    args = parser.parse_args()

    print("💨 Gas Compressor Stations ETL starting...")
    conn = psycopg2.connect(DATABASE_URL)

    try:
        if args.file:
            print(f"   Reading local file: {args.file}")
            with open(args.file, "rb") as f:
                rows, skipped = csv_to_rows(f.read())
            print(f"   Rows: {len(rows)}, skipped: {skipped}")
            insert_rows(conn, rows)
        else:
            # Try ArcGIS REST
            features = None
            for url in ARCGIS_URLS:
                print(f"   Trying: {url[:70]}...")
                features = try_arcgis_rest(url)
                if features:
                    print(f"   ✅ Got {len(features)} features")
                    rows, skipped = features_to_rows(features)
                    print(f"   Valid: {len(rows)}, skipped: {skipped}")
                    insert_rows(conn, rows)
                    break

            if not features:
                # Try direct CSV downloads
                for csv_url in [CSV_DOWNLOAD_URL, CSV_DOWNLOAD_ALT]:
                    print(f"   Trying CSV download...")
                    try:
                        r = requests.get(csv_url, timeout=60)
                        if r.status_code == 200 and len(r.content) > 1000:
                            rows, skipped = csv_to_rows(r.content)
                            print(f"   Rows: {len(rows)}, skipped: {skipped}")
                            insert_rows(conn, rows)
                            break
                    except Exception as e:
                        print(f"   CSV failed: {e}")
                else:
                    print("\n❌ All auto-fetch methods failed.")
                    print("   Manual steps:")
                    print("   1. Go to: https://hifld-geoplatform.opendata.arcgis.com/datasets/geoplatform::natural-gas-compressor-stations/explore")
                    print("   2. Click Download → CSV")
                    print("   3. Run: python 03_compressor_stations.py --file <downloaded.csv>")

        print("🎉 Compressor stations ETL complete!")
    finally:
        conn.close()
