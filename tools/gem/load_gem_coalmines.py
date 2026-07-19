"""Load GEM Coal Mine Boundaries & Methane Sources geojson -> Neon gem_coal_mines."""
import os, json
import psycopg2
from psycopg2.extras import execute_values

DSN=os.environ["DATABASE_URL"]; SRC="gem_coal_mines"
GJ=os.environ["GJ"]
def coords_of(geom):
    if not geom: return
    if geom.get("type")=="GeometryCollection":
        for g in geom.get("geometries",[]): yield from coords_of(g)
        return
    def walk(c):
        if not c: return
        if isinstance(c[0],(int,float)):
            if len(c)>=2: yield (c[0],c[1])
        else:
            for x in c: yield from walk(x)
    yield from walk(geom.get("coordinates"))
def s(v,cap): return "" if v is None else str(v)[:cap]

print("reading", os.path.basename(GJ), flush=True)
d=json.load(open(GJ)); feats=d.get("features",[])
rows=[]; skip=0
for f in feats:
    g=f.get("geometry"); p=f.get("properties",{}) or {}
    pts=list(coords_of(g)) if g else []
    if not pts: skip+=1; continue
    lngs=[c[0] for c in pts]; lats=[c[1] for c in pts]
    rows.append((
        s(p.get("GEM Mine ID"),40), s(p.get("Mine Name"),200),
        s(p.get("mine feature category"),60), s(p.get("mine feature subcategory"),80),
        s(p.get("Coal Grade"),60), s(p.get("Owners"),250), s(p.get("Parent Company"),200),
        s(p.get("Country / Area"),100), s(p.get("GEM Wiki Page (ENG)"),250),
        json.dumps(g, separators=(",",":")),
        min(lngs),min(lats),max(lngs),max(lats), SRC))
print(f"parsed {len(rows)} features (skipped {skip})", flush=True)

conn=psycopg2.connect(DSN, sslmode="require", connect_timeout=15); conn.autocommit=False
cur=conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS gem_coal_mines (
  id SERIAL PRIMARY KEY, gem_mine_id TEXT, mine_name TEXT, category TEXT, subcategory TEXT,
  coal_grade TEXT, owners TEXT, parent TEXT, country TEXT, wiki TEXT, geom_json TEXT,
  min_lng DOUBLE PRECISION, min_lat DOUBLE PRECISION, max_lng DOUBLE PRECISION, max_lat DOUBLE PRECISION,
  source TEXT, ingested_at TIMESTAMPTZ DEFAULT NOW())""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_gemcm_bbox ON gem_coal_mines(min_lng,max_lng,min_lat,max_lat)")
cur.execute("DELETE FROM gem_coal_mines WHERE source=%s",(SRC,))
cols=["gem_mine_id","mine_name","category","subcategory","coal_grade","owners","parent",
      "country","wiki","geom_json","min_lng","min_lat","max_lng","max_lat","source"]
execute_values(cur, f"INSERT INTO gem_coal_mines ({','.join(cols)}) VALUES %s", rows, page_size=300)
conn.commit()
cur.execute("SELECT count(*), count(DISTINCT gem_mine_id), count(*) FILTER (WHERE category='mine boundary') FROM gem_coal_mines WHERE source=%s",(SRC,))
tot,mines,bnd=cur.fetchone()
print(f"LOADED gem_coal_mines: {tot} features · {mines} mines · {bnd} boundary polygons", flush=True)
cur.close(); conn.close()
