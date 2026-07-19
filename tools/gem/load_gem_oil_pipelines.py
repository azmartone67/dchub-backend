"""Load GEM GGIT gas-pipeline GeoJSON -> Neon gem_gas_pipelines (geometry + per-feature bbox)."""
import os, json
import psycopg2
from psycopg2.extras import execute_values

DSN=os.environ["DATABASE_URL"]; SRC="gem_goit_oil_ngl"
GJ="GEM-GOIT-Oil-NGL-Pipelines-2026-06.geojson"

def coords_of(geom):
    if not geom: return
    t=geom.get("type")
    if t=="GeometryCollection":
        for g in geom.get("geometries",[]):
            yield from coords_of(g)
        return
    def walk(c):
        if not c: return
        if isinstance(c[0],(int,float)):
            if len(c)>=2: yield (c[0],c[1])
        else:
            for x in c: yield from walk(x)
    yield from walk(geom.get("coordinates"))

def num(v):
    try:
        f=float(v); return f if f==f else None
    except (TypeError,ValueError): return None
def s(v,cap):
    return "" if v is None else str(v)[:cap]

print("reading", GJ, "...", flush=True)
d=json.load(open(GJ))
feats=d.get("features",[])
rows=[]; skip=0
for f in feats:
    g=f.get("geometry"); p=f.get("properties",{}) or {}
    pts=list(coords_of(g)) if g else []
    if not pts:
        skip+=1; continue
    lngs=[c[0] for c in pts]; lats=[c[1] for c in pts]
    yr=num(p.get("StartYear1"))
    rows.append((
        s(p.get("ProjectID"),40),
        s(p.get("PipelineName"),250),
        s(p.get("SegmentName"),200),
        s(p.get("Status"),60),
        s(p.get("Fuel"),40),
        s(p.get("CountriesOrAreas"),200),
        s(p.get("Owner"),200),
        yr,
        json.dumps(g, separators=(",",":")),
        min(lngs), min(lats), max(lngs), max(lats),
        SRC))
print(f"parsed {len(rows)} pipelines with geometry (skipped {skip} no-geom)", flush=True)

conn=psycopg2.connect(DSN, sslmode="require", connect_timeout=15); conn.autocommit=False
cur=conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS gem_gas_pipelines (
  id SERIAL PRIMARY KEY, project_id TEXT, name TEXT, segment TEXT, status TEXT, fuel TEXT,
  countries TEXT, owner TEXT, start_year NUMERIC, geom_json TEXT,
  min_lng DOUBLE PRECISION, min_lat DOUBLE PRECISION, max_lng DOUBLE PRECISION, max_lat DOUBLE PRECISION,
  source TEXT, ingested_at TIMESTAMPTZ DEFAULT NOW())""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gempipe_bbox ON gem_gas_pipelines(min_lng,max_lng,min_lat,max_lat)")
cur.execute("DELETE FROM gem_gas_pipelines WHERE source=%s",(SRC,))
cols=["project_id","name","segment","status","fuel","countries","owner","start_year",
      "geom_json","min_lng","min_lat","max_lng","max_lat","source"]
execute_values(cur, f"INSERT INTO gem_gas_pipelines ({','.join(cols)}) VALUES %s", rows, page_size=200)
conn.commit()
cur.execute("SELECT count(*), count(*) FILTER (WHERE status ILIKE '%%operating%%') FROM gem_gas_pipelines WHERE source=%s",(SRC,))
tot,op=cur.fetchone()
print(f"LOADED gem_gas_pipelines: {tot} ({op} operating)", flush=True)
cur.close(); conn.close()
