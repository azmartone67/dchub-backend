"""Load GEM GGIT LNG terminals -> Neon gem_gas (points). Idempotent full-replace."""
import os, sys
import openpyxl, psycopg2
from psycopg2.extras import execute_values

DSN=os.environ["DATABASE_URL"]; SRC="gem_gas_infra"
XLSX="GEM-GGIT-LNG-Teminals-2025-09.xlsx"
FIELDS=["gem_id","kind","name","unit_name","fuel","capacity","capacity_units",
        "status","start_year","country","region","owner","lat","lng","wiki_url"]
def num(v):
    try:
        f=float(v); return f if f==f else None
    except (TypeError,ValueError): return None
def s(v,cap):
    return "" if v is None else str(v)[:cap]

wb=openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
ws=wb["LNG Terminals"]; it=ws.iter_rows(values_only=True)
hdr=list(next(it)); ix={h:i for i,h in enumerate(hdr)}
def g(r,h): return r[ix[h]] if h in ix else None
rows=[]; skip=0
for r in it:
    lat=num(g(r,"Latitude")); lng=num(g(r,"Longitude"))
    if lat is None or lng is None: skip+=1; continue
    fac=s(g(r,"FacilityType"),40)  # import/export
    yr=num(g(r,"ActualStartYear")) or num(g(r,"LatestPlannedStartYear"))
    rows.append((
        s(g(r,"UnitID") or g(r,"ProjectID"),40),
        "lng_terminal",
        s(g(r,"TerminalName"),250),
        s(g(r,"UnitName") or fac,150),
        s(g(r,"Fuel") or "LNG",40),
        num(g(r,"CapacityinMtpa")) or num(g(r,"Capacity")),
        "Mtpa" if num(g(r,"CapacityinMtpa")) else s(g(r,"CapacityUnits"),30),
        s(g(r,"Status"),60), yr,
        s(g(r,"Country/Area"),100), s(g(r,"Region"),80),
        s(g(r,"Owner"),200), lat, lng, s(g(r,"Wiki"),250), SRC))
wb.close()
print(f"LNG terminals parsed: {len(rows)} (skipped {skip} no-coord)", flush=True)

conn=psycopg2.connect(DSN, sslmode="require", connect_timeout=15); conn.autocommit=False
cur=conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS gem_gas (
  id SERIAL PRIMARY KEY, gem_id TEXT, kind TEXT, name TEXT, unit_name TEXT, fuel TEXT,
  capacity NUMERIC, capacity_units TEXT, status TEXT, start_year NUMERIC, country TEXT,
  region TEXT, owner TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, wiki_url TEXT,
  source TEXT, ingested_at TIMESTAMPTZ DEFAULT NOW())""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gemgas_kind ON gem_gas(kind)")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gemgas_bbox ON gem_gas(lng, lat)")
cur.execute("DELETE FROM gem_gas WHERE source=%s",(SRC,))
execute_values(cur, f"INSERT INTO gem_gas ({','.join(FIELDS+['source'])}) VALUES %s", rows, page_size=1000)
conn.commit()
cur.execute("SELECT count(*) FROM gem_gas WHERE source=%s",(SRC,)); print("LOADED gem_gas:",cur.fetchone()[0],flush=True)
cur.close(); conn.close()
