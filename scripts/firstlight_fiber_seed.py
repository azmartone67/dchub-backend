"""
firstlight_fiber_seed.py
========================
Loads FirstLight Fiber network data into DC Hub Neon:
  1. Fiber routes from KML → fiber_routes table
  2. Serviceable buildings from Excel → fiber_serviceable_buildings table (new)

Run: python3 firstlight_fiber_seed.py
"""

import os
import re
import json
import logging
import psycopg2
import psycopg2.extras
import xml.etree.ElementTree as ET
import openpyxl
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [firstlight] %(levelname)s %(message)s')
log = logging.getLogger('firstlight')

DATABASE_URL = os.environ.get('NEON_DATABASE_URL') or os.environ['DATABASE_URL']
KML_FILE     = os.path.expanduser('~/workspace/firstlight/FLF Network 3-4-26 CONFIDENTIAL.kml')
EXCEL_FILE   = os.path.expanduser('~/workspace/firstlight/FLF Building List 1-26-26.xlsx')

CARRIER = 'FirstLight Fiber'
CARRIER_SHORT = 'firstlight'

STATE_ABBR = {
    'new york': 'NY', 'pennsylvania': 'PA', 'new hampshire': 'NH',
    'maine': 'ME', 'vermont': 'VT', 'massachusetts': 'MA',
    'connecticut': 'CT', 'virginia': 'VA', 'rhode island': 'RI',
    'new jersey': 'NJ', 'maryland': 'MD', 'delaware': 'DE',
    'quebec': 'QC',
}

# ── Schema setup ──────────────────────────────────────────────────────────────

def setup_schema(conn):
    with conn.cursor() as cur:
        # fiber_routes — check existing
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'fiber_routes' LIMIT 1
        """)
        has_fiber = cur.fetchone() is not None

        if not has_fiber:
            log.info("Creating fiber_routes table...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fiber_routes (
                    id          SERIAL PRIMARY KEY,
                    carrier     TEXT NOT NULL,
                    name        TEXT,
                    route_type  TEXT,
                    status      TEXT DEFAULT 'active',
                    states      TEXT[],
                    markets     TEXT[],
                    coordinates JSONB,
                    route_miles NUMERIC(10,2),
                    source      TEXT DEFAULT 'kmz',
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(carrier, name)
                )
            """)

        # fiber_serviceable_buildings — new table for on/near-net buildings
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fiber_serviceable_buildings (
                id              SERIAL PRIMARY KEY,
                carrier         TEXT NOT NULL,
                carrier_id      TEXT,
                address         TEXT,
                city            TEXT,
                state           TEXT,
                state_abbr      CHAR(2),
                zip_code        TEXT,
                status          TEXT,   -- 'OnNet' | 'Near Net'
                cost_market     TEXT,
                zone            TEXT,
                facility_id     TEXT,   -- linked DC Hub facility if matched
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(carrier, carrier_id)
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_carrier ON fiber_serviceable_buildings(carrier)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_state ON fiber_serviceable_buildings(state_abbr)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_status ON fiber_serviceable_buildings(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_city ON fiber_serviceable_buildings(city)")

    conn.commit()
    log.info("Schema ready")


# ── KML → fiber_routes ────────────────────────────────────────────────────────

def haversine_miles(coords):
    """Approximate route length from coordinate list."""
    import math
    total = 0.0
    for i in range(1, len(coords)):
        lon1, lat1 = coords[i-1]
        lon2, lat2 = coords[i]
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        total += 3958.8 * 2 * math.asin(math.sqrt(a))
    return round(total, 2)


def parse_kml_routes(kml_file):
    """
    Parse the FirstLight KML. Two placemarks, each a MultiGeometry of LineStrings.
    Returns list of route dicts.
    """
    log.info("Parsing KML: %s", kml_file)
    routes = []

    context = ET.iterparse(kml_file, events=('end',))
    for event, elem in context:
        tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag

        if tag == 'Placemark':
            name = ''
            layer = ''
            for n in elem.iter('{http://www.opengis.net/kml/2.2}n'):
                name = n.text or ''
            for sd in elem.iter('{http://www.opengis.net/kml/2.2}SimpleData'):
                if sd.get('name') == 'layer':
                    layer = sd.text or ''

            # Collect all LineString coordinate arrays
            all_coords = []
            total_pairs = 0
            for coord_elem in elem.iter('{http://www.opengis.net/kml/2.2}coordinates'):
                if not coord_elem.text:
                    continue
                pairs = []
                for token in coord_elem.text.strip().split():
                    parts = token.split(',')
                    if len(parts) >= 2:
                        try:
                            lon, lat = float(parts[0]), float(parts[1])
                            # Filter out obviously bad coords (FirstLight is Northeast US)
                            if -80 <= lon <= -65 and 40 <= lat <= 50:
                                pairs.append([lon, lat])
                        except ValueError:
                            pass
                if pairs:
                    all_coords.append(pairs)
                    total_pairs += len(pairs)

            if all_coords:
                # Determine status from name
                status = 'active' if 'Active' in name or 'FPA' in layer else 'planned'
                route_type = 'aerial' if 'Aerial' in layer else 'underground'

                # Sample coords for storage (every 10th point to keep JSON size manageable)
                sample_coords = []
                for seg in all_coords:
                    sample_coords.extend(seg[::10])

                # Estimate mileage from sample
                flat_coords = [p for seg in all_coords for p in seg]
                miles = haversine_miles(flat_coords[::5]) if flat_coords else 0

                routes.append({
                    'carrier':      CARRIER,
                    'name':         f"FirstLight {name} Network",
                    'route_type':   route_type,
                    'status':       status,
                    'states':       ['NY', 'PA', 'NH', 'ME', 'VT', 'MA', 'CT'],
                    'markets':      ['New York', 'Albany', 'Buffalo', 'Boston', 'Manchester', 'Portland ME'],
                    'coordinates':  json.dumps(sample_coords[:5000]),  # cap at 5K points
                    'route_miles':  miles,
                    'source':       'kmz',
                    'segments':     len(all_coords),
                    'total_pairs':  total_pairs,
                })
                log.info("  Route '%s': %d segments, %d coord pairs, ~%.0f miles",
                         name, len(all_coords), total_pairs, miles)
            elem.clear()

    log.info("Parsed %d routes from KML", len(routes))
    return routes


def load_routes(conn, routes):
    with conn.cursor() as cur:
        inserted = 0
        for r in routes:
            cur.execute("""
                INSERT INTO fiber_routes (carrier, name, route_type, status, states, markets, coordinates, route_miles, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (carrier, name) DO UPDATE SET
                    route_miles = EXCLUDED.route_miles,
                    coordinates = EXCLUDED.coordinates,
                    status      = EXCLUDED.status
            """, (
                r['carrier'], r['name'], r['route_type'], r['status'],
                r['states'], r['markets'],
                r['coordinates'], r['route_miles'], r['source']
            ))
            inserted += 1
    conn.commit()
    log.info("Loaded %d fiber routes", inserted)
    return inserted


# ── Excel → fiber_serviceable_buildings ──────────────────────────────────────

def load_buildings(conn, excel_file):
    log.info("Loading buildings from Excel: %s", excel_file)
    wb = openpyxl.load_workbook(excel_file, read_only=True, data_only=True)
    ws = wb['Sheet1']
    rows = list(ws.iter_rows(values_only=True))
    log.info("  %d rows to process", len(rows) - 1)

    batch = []
    inserted = 0
    skipped = 0

    for i, row in enumerate(rows[1:], 1):  # skip header
        if not row[0]:
            continue

        state_name = (row[0] or '').strip().lower()
        city       = (row[1] or '').strip().title()
        address    = (row[2] or '').strip().title()
        zip_code   = str(row[3] or '').strip()
        status     = (row[4] or '').strip()
        carrier_id = (row[5] or '').strip()
        cost_mkt   = (row[6] or '').strip()
        zone       = (row[7] or '').strip()
        state_abbr = STATE_ABBR.get(state_name, state_name[:2].upper())

        if not carrier_id or not address:
            skipped += 1
            continue

        batch.append((
            CARRIER, carrier_id, address, city,
            state_name.title(), state_abbr, zip_code,
            status, cost_mkt, zone
        ))

        if len(batch) >= 5000:
            _insert_buildings_batch(conn, batch)
            inserted += len(batch)
            batch = []
            log.info("  Inserted %d buildings...", inserted)

    if batch:
        _insert_buildings_batch(conn, batch)
        inserted += len(batch)

    conn.commit()
    log.info("Loaded %d buildings (%d skipped)", inserted, skipped)
    return inserted


def _insert_buildings_batch(conn, batch):
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO fiber_serviceable_buildings
                (carrier, carrier_id, address, city, state, state_abbr, zip_code, status, cost_market, zone)
            VALUES %s
            ON CONFLICT (carrier, carrier_id) DO NOTHING
        """, batch, page_size=1000)


# ── Summary stats ─────────────────────────────────────────────────────────────

def print_summary(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fiber_routes WHERE carrier = %s", (CARRIER,))
        route_count = cur.fetchone()[0]

        cur.execute("""
            SELECT status, COUNT(*), state_abbr
            FROM fiber_serviceable_buildings
            WHERE carrier = %s
            GROUP BY status, state_abbr
            ORDER BY status, COUNT(*) DESC
        """, (CARRIER,))
        rows = cur.fetchall()

    log.info("=== FirstLight Summary ===")
    log.info("  Fiber routes: %d", route_count)
    log.info("  Serviceable buildings:")
    for status, count, state in rows:
        log.info("    %s - %s: %d", state, status, count)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("── FirstLight Fiber Seed starting ──")
    conn = psycopg2.connect(DATABASE_URL)

    try:
        setup_schema(conn)

        # Load KML routes
        if os.path.exists(KML_FILE):
            routes = parse_kml_routes(KML_FILE)
            load_routes(conn, routes)
        else:
            log.warning("KML not found at %s — skipping routes", KML_FILE)

        # Load buildings
        if os.path.exists(EXCEL_FILE):
            load_buildings(conn, EXCEL_FILE)
        else:
            log.warning("Excel not found at %s — skipping buildings", EXCEL_FILE)

        print_summary(conn)
        log.info("── FirstLight Fiber Seed complete ──")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
