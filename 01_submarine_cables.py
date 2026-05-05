"""
ETL: TeleGeography Submarine Cable Data → Neon  (v2 - fixed owners bug)
"""

import os, time, requests, psycopg2
from psycopg2.extras import Json, execute_values

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("Set DATABASE_URL environment variable")

CABLES_URL       = "https://www.submarinecablemap.com/api/v3/cable/all.json"
LANDING_URL      = "https://www.submarinecablemap.com/api/v3/landing-point/all.json"
CABLE_DETAIL_URL = "https://www.submarinecablemap.com/api/v3/cable/{slug}.json"


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠️  Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


def parse_owners(owners_raw):
    """Handle owners as list of dicts OR list of strings (API returns both)."""
    if not owners_raw:
        return ""
    result = []
    for o in owners_raw:
        if isinstance(o, dict):
            result.append(o.get("name") or o.get("owner") or str(o))
        else:
            result.append(str(o))
    return ", ".join(filter(None, result))


def load_cables(conn):
    print("📡 Fetching cable list...")
    data = fetch_json(CABLES_URL)
    if not data:
        print("❌ Failed to fetch cables"); return

    cables = data if isinstance(data, list) else data.get("cables", [])
    print(f"   Found {len(cables)} cable systems")

    rows = []
    for i, c in enumerate(cables):
        slug   = c.get("id") or c.get("slug") or c.get("cable_id", "")
        name   = c.get("name", "")
        detail = fetch_json(CABLE_DETAIL_URL.format(slug=slug))

        owners = ""
        rfs_year = None
        length_km = None
        status = "active"
        route_geojson = None

        if detail:
            owners    = parse_owners(detail.get("owners"))
            rfs_year  = detail.get("rfs") or detail.get("ready_for_service")
            length_km = detail.get("length_km") or detail.get("length")
            status    = detail.get("status", "active")
            geo       = detail.get("cable_geo") or detail.get("geometry")
            if geo:
                route_geojson = geo

        rows.append((slug, name, owners, rfs_year, length_km, status,
                     Json(route_geojson) if route_geojson else None))

        if (i + 1) % 20 == 0:
            print(f"   Fetched {i+1}/{len(cables)} cables...")
            time.sleep(0.5)

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO submarine_cables
                (cable_id, cable_name, owners, rfs_year, length_km, status, route_geojson)
            VALUES %s
            ON CONFLICT (cable_id) DO UPDATE SET
                cable_name=EXCLUDED.cable_name, owners=EXCLUDED.owners,
                rfs_year=EXCLUDED.rfs_year, length_km=EXCLUDED.length_km,
                status=EXCLUDED.status, route_geojson=EXCLUDED.route_geojson,
                loaded_at=NOW()
        """, rows)
    conn.commit()
    print(f"✅ Loaded {len(rows)} cables into submarine_cables")


def load_landing_points(conn):
    print("📍 Fetching landing points...")
    data = fetch_json(LANDING_URL)
    if not data:
        print("❌ Failed to fetch landing points"); return

    points = data if isinstance(data, list) else data.get("landing_points", [])
    print(f"   Found {len(points)} landing points")

    rows = []
    for lp in points:
        cables  = lp.get("cables") or []
        coords  = (lp.get("geometry") or {}).get("coordinates", [None, None])
        lng     = lp.get("lng") or lp.get("longitude") or (coords[0] if len(coords) > 0 else None)
        lat     = lp.get("lat") or lp.get("latitude")  or (coords[1] if len(coords) > 1 else None)
        country = lp.get("country", "")
        city    = lp.get("name") or lp.get("city", "")

        for cable in (cables if cables else [{}]):
            if isinstance(cable, dict):
                cable_id   = cable.get("id") or cable.get("slug", "")
                cable_name = cable.get("name", "")
            else:
                cable_id, cable_name = str(cable), str(cable)
            if lat and lng:
                rows.append((cable_id, cable_name, country, city, float(lat), float(lng)))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO cable_landing_points (cable_id, cable_name, country, city, lat, lng)
            VALUES %s
        """, rows)
    conn.commit()
    print(f"✅ Loaded {len(rows)} landing points into cable_landing_points")


if __name__ == "__main__":
    print("🌊 Submarine Cable ETL starting...")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        load_cables(conn)
        load_landing_points(conn)
        print("🎉 Submarine cables complete!")
    finally:
        conn.close()
