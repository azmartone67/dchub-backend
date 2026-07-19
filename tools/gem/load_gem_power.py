"""One-time bulk load: GEM Global Integrated Power xlsx -> Neon gem_power.
Mirrors the /api/v1/admin/ingest/gem-power schema. Idempotent (full-replace by source)."""
import os, sys
import openpyxl
import psycopg2
from psycopg2.extras import execute_values

DSN = os.environ["DATABASE_URL"]
SRC = "gem_integrated_power"
XLSX = "Global-Integrated-Power-March-2026-II.xlsx"

# header name -> our column
COLMAP = {
    "GEM unit/phase ID": "gem_id",
    "Type": "fuel_type",
    "Plant / Project name": "plant_name",
    "Unit / Phase name": "unit_name",
    "Capacity (MW)": "capacity_mw",
    "Status": "status",
    "Start year": "start_year",
    "Technology": "technology",
    "Country/area": "country",
    "Region": "region",
    "Operator(s)": "operator",
    "Owner(s)": "owner",
    "Latitude": "lat",
    "Longitude": "lng",
    "GEM.Wiki URL": "wiki_url",
}
FIELDS = ["gem_id","fuel_type","plant_name","unit_name","capacity_mw","status",
          "start_year","technology","country","region","operator","owner",
          "lat","lng","wiki_url"]
CAPS = {"gem_id":40,"fuel_type":40,"plant_name":250,"unit_name":150,"status":60,
        "technology":120,"country":100,"region":80,"operator":200,"owner":200,"wiki_url":250}
NUMS = {"capacity_mw","start_year","lat","lng"}

def num(v):
    try:
        f=float(v); return f if f==f else None
    except (TypeError,ValueError): return None

def s(v,cap):
    if v is None: return ""
    return str(v)[:cap]

print("reading", XLSX, "...", flush=True)
wb=openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
ws=wb["Power facilities"]
it=ws.iter_rows(values_only=True)
hdr=list(next(it))
idx={h:i for i,h in enumerate(hdr)}
# verify all mapped headers present
missing=[h for h in COLMAP if h not in idx]
if missing:
    print("MISSING HEADERS:", missing); sys.exit(1)

rows=[]; skipped=0
for r in it:
    lat=num(r[idx["Latitude"]]); lng=num(r[idx["Longitude"]])
    if lat is None or lng is None:
        skipped+=1; continue
    rec={}
    for h,col in COLMAP.items():
        v=r[idx[h]]
        rec[col]= num(v) if col in NUMS else s(v, CAPS.get(col,200))
    rec["lat"]=lat; rec["lng"]=lng
    rows.append(tuple(rec.get(f) for f in FIELDS)+(SRC,))
wb.close()
print(f"parsed {len(rows)} geocoded rows (skipped {skipped} no-coord)", flush=True)

conn=psycopg2.connect(DSN, sslmode="require", connect_timeout=15)
conn.autocommit=False
cur=conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS gem_power (
  id SERIAL PRIMARY KEY, gem_id TEXT, fuel_type TEXT, plant_name TEXT, unit_name TEXT,
  capacity_mw NUMERIC, status TEXT, start_year NUMERIC, technology TEXT, country TEXT,
  region TEXT, operator TEXT, owner TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
  wiki_url TEXT, source TEXT, ingested_at TIMESTAMPTZ DEFAULT NOW())""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gempow_status ON gem_power(status)")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gempow_fuel ON gem_power(fuel_type)")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gempow_bbox ON gem_power(lng, lat)")
cur.execute("DELETE FROM gem_power WHERE source=%s", (SRC,))
cols=FIELDS+["source"]
execute_values(cur,
    f"INSERT INTO gem_power ({','.join(cols)}) VALUES %s",
    rows, page_size=2000)
conn.commit()
cur.execute("SELECT count(*), count(*) FILTER (WHERE status ILIKE '%%construction%%' OR status ILIKE '%%announced%%') FROM gem_power WHERE source=%s",(SRC,))
tot,fwd=cur.fetchone()
print(f"LOADED gem_power: {tot} rows ({fwd} forward/planned)", flush=True)
cur.close(); conn.close()
