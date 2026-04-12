"""
ETL: EIA Energy Atlas — Natural Gas Processing Plants → Neon  (v2)

The ArcGIS FeatureServer URLs change periodically.
This script tries 3 URL patterns + a direct CSV download fallback.

Run:  python 02_gas_processing_plants.py
Env:  DATABASE_URL=postgresql://...

If all auto-fetches fail, download the CSV manually:
  1. Go to: https://atlas.eia.gov/datasets/eia::natural-gas-processing-plants/explore
  2. Click Export → CSV
  3. Run: python 02_gas_processing_plants.py --file natural_gas_processing_plants.csv
"""

import os, io, time, requests, psycopg2, argparse, csv
from psycopg2.extras import execute_values

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("Set DATABASE_URL environment variable")

# Try these ArcGIS FeatureServer URLs in order
ARCGIS_URLS = [
    # EIA Energy Atlas (ArcGIS Online hosted)
    "https://services.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services/NaturalGasProcessingPlants/FeatureServer/0/query",
    "https://services7.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0/query",
    # ArcGIS Hub direct dataset query
    "https://services1.arcgis.com/4yjifSiIG17X0gW4/arcgis/rest/services/NaturalGasProcessingPlants/FeatureServer/0/query",
]

# Direct CSV download from ArcGIS Hub (most reliable fallback)
CSV_DOWNLOAD_URL = (
    "https://opendata.arcgis.com/api/v3/datasets/1603bffa90024b9eb280215af7edc3bd"
    "/downloads/data?format=csv&spatialRefId=4326"
)

BATCH_SIZE = 1000


def try_arcgis_rest(url):
    """Try fetching from an ArcGIS FeatureServer REST endpoint."""
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

        # Paginate
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
        print(f"   URL failed ({url[:60]}...): {e}")
        return None


def try_csv_download(url):
    """Download direct CSV from ArcGIS Hub."""
    try:
        print(f"   Trying direct CSV download...")
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            return None
        return r.content
    except Exception as e:
        print(f"   CSV download failed: {e}")
        return None


def features_to_rows(features):
    rows = []
    skipped = 0
    for feat in features:
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        lng = geom.get("x")
        lat = geom.get("y")
        if not lat or not lng or lat == -9999 or lng == -9999:
            skipped += 1; continue

        eia_id     = str(attrs.get("OBJECTID") or attrs.get("FID") or "")
        plant_name = (attrs.get("PLANT_NAME") or attrs.get("Name") or
                      attrs.get("name") or attrs.get("FACILITY_NAME") or "Unknown")
        operator   = attrs.get("OPERATOR") or attrs.get("COMPANY") or attrs.get("operator") or ""
        state      = attrs.get("STATE") or attrs.get("State") or attrs.get("state") or ""
        county     = attrs.get("COUNTY") or attrs.get("County") or ""
        capacity   = attrs.get("CAPACITY") or attrs.get("CAP_MMCFD") or None
        status     = attrs.get("STATUS") or attrs.get("status") or "active"

        try:
            capacity = float(capacity) if capacity else None
        except (TypeError, ValueError):
            capacity = None

        rows.append((eia_id, plant_name, operator, state, county, capacity, status,
                     round(float(lat), 6), round(float(lng), 6)))
    return rows, skipped


def csv_to_rows(content):
    """Parse CSV download from ArcGIS Hub."""
    rows = []
    skipped = 0
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    print(f"   CSV columns: {reader.fieldnames}")

    for i, row in enumerate(reader):
        # Try common lat/lng field names
        lat = (row.get("Y") or row.get("LATITUDE") or row.get("lat") or
               row.get("Latitude") or row.get("y"))
        lng = (row.get("X") or row.get("LONGITUDE") or row.get("lng") or
               row.get("Longitude") or row.get("x"))
        if not lat or not lng:
            skipped += 1; continue
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            skipped += 1; continue
        if lat == 0 and lng == 0:
            skipped += 1; continue

        eia_id     = str(row.get("OBJECTID") or row.get("FID") or i)
        plant_name = (row.get("PLANT_NAME") or row.get("Name") or
                      row.get("name") or "Unknown")
        operator   = row.get("OPERATOR") or row.get("COMPANY") or ""
        state      = row.get("STATE") or row.get("State") or ""
        county     = row.get("COUNTY") or row.get("County") or ""
        capacity   = row.get("CAPACITY") or row.get("CAP_MMCFD") or None
        status     = row.get("STATUS") or "active"
        try:
            capacity = float(capacity) if capacity else None
        except (TypeError, ValueError):
            capacity = None

        rows.append((eia_id, plant_name, operator, state, county, capacity, status,
                     round(lat, 6), round(lng, 6)))
    return rows, skipped


def load_from_file(file_path):
    """Load from manually downloaded CSV file."""
    with open(file_path, "rb") as f:
        return csv_to_rows(f.read())


def insert_rows(conn, rows):
    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO gas_processing_plants
                (eia_id, plant_name, operator, state, county,
                 capacity_mmcfd, status, lat, lng)
            VALUES %s
            ON CONFLICT (eia_id) DO UPDATE SET
                plant_name=EXCLUDED.plant_name, operator=EXCLUDED.operator,
                state=EXCLUDED.state, county=EXCLUDED.county,
                capacity_mmcfd=EXCLUDED.capacity_mmcfd,
                status=EXCLUDED.status, lat=EXCLUDED.lat, lng=EXCLUDED.lng,
                loaded_at=NOW()
        """, rows)
    conn.commit()
    print(f"✅ Loaded {len(rows)} gas processing plants into Neon")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to manually downloaded CSV", default=None)
    args = parser.parse_args()

    print("🏭 Gas Processing Plants ETL starting...")
    conn = psycopg2.connect(DATABASE_URL)

    try:
        if args.file:
            print(f"   Reading local file: {args.file}")
            rows, skipped = load_from_file(args.file)
            print(f"   Rows: {len(rows)}, skipped: {skipped}")
            insert_rows(conn, rows)
        else:
            # Try ArcGIS REST endpoints
            features = None
            for url in ARCGIS_URLS:
                print(f"   Trying: {url[:70]}...")
                features = try_arcgis_rest(url)
                if features:
                    print(f"   ✅ Got {len(features)} features from ArcGIS REST")
                    rows, skipped = features_to_rows(features)
                    print(f"   Valid: {len(rows)}, skipped: {skipped}")
                    insert_rows(conn, rows)
                    break

            if not features:
                # Try direct CSV download
                csv_content = try_csv_download(CSV_DOWNLOAD_URL)
                if csv_content:
                    rows, skipped = csv_to_rows(csv_content)
                    print(f"   CSV rows: {len(rows)}, skipped: {skipped}")
                    insert_rows(conn, rows)
                else:
                    print("\n❌ All auto-fetch methods failed.")
                    print("   Manual download steps:")
                    print("   1. Go to: https://atlas.eia.gov/datasets/eia::natural-gas-processing-plants/explore")
                    print("   2. Click Export → Download → CSV")
                    print("   3. Run: python 02_gas_processing_plants.py --file <downloaded_file.csv>")

        print("🎉 Gas processing plants ETL complete!")
    finally:
        conn.close()
