"""
firstlight_fiber_seed.py - loads FirstLight fiber data into DC Hub Neon
"""
import os, re, json, logging, psycopg2, psycopg2.extras, xml.etree.ElementTree as ET, openpyxl, math
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [firstlight] %(levelname)s %(message)s")
log = logging.getLogger("firstlight")

DATABASE_URL = os.environ.get("NEON_DATABASE_URL") or os.environ["DATABASE_URL"]
SCRIPTS = os.path.expanduser("~/workspace/scripts/")
KML_FILE   = SCRIPTS + "FLF Network 3-4-26 CONFIDENTIAL.kml"
EXCEL_FILE = SCRIPTS + "FLF Building List 1-26-26.xlsx"
CARRIER = "FirstLight Fiber"

STATE_ABBR = {"new york":"NY","pennsylvania":"PA","new hampshire":"NH","maine":"ME","vermont":"VT","massachusetts":"MA","connecticut":"CT","virginia":"VA","rhode island":"RI","new jersey":"NJ","maryland":"MD","delaware":"DE","quebec":"QC"}

def setup_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS fiber_routes (id SERIAL PRIMARY KEY, carrier TEXT NOT NULL, name TEXT, route_type TEXT, status TEXT DEFAULT 'active', states TEXT[], markets TEXT[], coordinates JSONB, route_miles NUMERIC(10,2), source TEXT DEFAULT 'kmz', created_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE(carrier, name))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS fiber_serviceable_buildings (id SERIAL PRIMARY KEY, carrier TEXT NOT NULL, carrier_id TEXT, address TEXT, city TEXT, state TEXT, state_abbr CHAR(2), zip_code TEXT, status TEXT, cost_market TEXT, zone TEXT, facility_id TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE(carrier, carrier_id))""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_carrier ON fiber_serviceable_buildings(carrier)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_state ON fiber_serviceable_buildings(state_abbr)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_status ON fiber_serviceable_buildings(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fsb_city ON fiber_serviceable_buildings(city)")
    conn.commit()
    log.info("Schema ready")

def haversine(coords):
    total = 0.0
    for i in range(1, len(coords)):
        lo1,la1=coords[i-1]; lo2,la2=coords[i]
        dlat=math.radians(la2-la1); dlon=math.radians(lo2-lo1)
        a=math.sin(dlat/2)**2+math.cos(math.radians(la1))*math.cos(math.radians(la2))*math.sin(dlon/2)**2
        total+=3958.8*2*math.asin(math.sqrt(a))
    return round(total,2)

def load_kml(conn):
    if not os.path.exists(KML_FILE):
        log.warning("KML not found: %s", KML_FILE); return 0
    log.info("Parsing KML...")
    routes = []
    for event, elem in ET.iterparse(KML_FILE, events=("end",)):
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "Placemark":
            name = ""
            for n in elem.iter("{http://www.opengis.net/kml/2.2}n"): name = n.text or ""
            all_coords = []; total_pairs = 0
            for ce in elem.iter("{http://www.opengis.net/kml/2.2}coordinates"):
                if not ce.text: continue
                pairs = []
                for token in ce.text.strip().split():
                    parts = token.split(",")
                    if len(parts) >= 2:
                        try:
                            lon,lat = float(parts[0]),float(parts[1])
                            if -80<=lon<=-65 and 40<=lat<=50: pairs.append([lon,lat])
                        except: pass
                if pairs: all_coords.append(pairs); total_pairs+=len(pairs)
            if all_coords:
                flat = [p for seg in all_coords for p in seg]
                sample = flat[::10][:5000]
                miles = haversine(flat[::5])
                routes.append({"name": f"FirstLight {name} Network", "route_type": "aerial", "status": "active", "miles": miles, "coords": json.dumps(sample), "pairs": total_pairs})
                log.info("  Route '%s': %d segments, %d pairs, ~%.0f miles", name, len(all_coords), total_pairs, miles)
            elem.clear()
    with conn.cursor() as cur:
        for r in routes:
            cur.execute("""INSERT INTO fiber_routes (provider,name,route_type,status,source,distance_miles) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (CARRIER,r["name"],r["route_type"],r["status"],"kmz",r["miles"]))
    conn.commit()
    log.info("Loaded %d routes", len(routes))
    return len(routes)

def load_buildings(conn):
    if not os.path.exists(EXCEL_FILE):
        log.warning("Excel not found: %s", EXCEL_FILE); return 0
    log.info("Loading buildings from Excel...")
    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    log.info("  %d rows", len(rows)-1)
    batch=[]; inserted=0; skipped=0
    for row in rows[1:]:
        if not row[0]: continue
        state_name=(row[0] or "").strip().lower(); city=(row[1] or "").strip().title()
        address=(row[2] or "").strip().title(); zip_code=str(row[3] or "").strip()
        status=(row[4] or "").strip(); carrier_id=(row[5] or "").strip()
        cost_mkt=(row[6] or "").strip(); zone=(row[7] or "").strip()
        state_abbr=STATE_ABBR.get(state_name,state_name[:2].upper())
        if not carrier_id or not address: skipped+=1; continue
        batch.append((CARRIER,carrier_id,address,city,state_name.title(),state_abbr,zip_code,status,cost_mkt,zone))
        if len(batch)>=5000:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur,"""INSERT INTO fiber_serviceable_buildings (carrier,carrier_id,address,city,state,state_abbr,zip_code,status,cost_market,zone) VALUES %s ON CONFLICT (carrier,carrier_id) DO NOTHING""",batch,page_size=1000)
            conn.commit(); inserted+=len(batch); batch=[]
            log.info("  %d buildings inserted...", inserted)
    if batch:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur,"""INSERT INTO fiber_serviceable_buildings (carrier,carrier_id,address,city,state,state_abbr,zip_code,status,cost_market,zone) VALUES %s ON CONFLICT (carrier,carrier_id) DO NOTHING""",batch,page_size=1000)
        conn.commit(); inserted+=len(batch)
    log.info("Loaded %d buildings (%d skipped)", inserted, skipped)
    return inserted

def main():
    log.info("── FirstLight Fiber Seed starting ──")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        setup_schema(conn)
        load_kml(conn)
        load_buildings(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fiber_routes WHERE provider=%s",(CARRIER,))
            r=cur.fetchone()[0]
            cur.execute("SELECT status,COUNT(*) FROM fiber_serviceable_buildings WHERE carrier=%s GROUP BY status",(CARRIER,))
            b=cur.fetchall()
        log.info("Routes: %d | Buildings: %s", r, b)
        log.info("── Done ──")
    finally:
        conn.close()

if __name__=="__main__":
    main()
