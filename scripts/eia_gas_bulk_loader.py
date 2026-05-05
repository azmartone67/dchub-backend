"""
eia_gas_bulk_loader.py — Pull all US gas pipelines from EIA ArcGIS into Neon
"""
import os, sys, json, time, math, urllib.request, psycopg2

EIA_URL = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0/query"

STATE_BOXES = {
    'AL':(30.2,35.0,-88.5,-84.9),'AZ':(31.3,37.0,-114.8,-109.0),'AR':(33.0,36.5,-94.6,-89.6),
    'CA':(32.5,42.0,-124.4,-114.1),'CO':(37.0,41.0,-109.1,-102.0),'CT':(41.0,42.1,-73.7,-71.8),
    'DE':(38.5,39.8,-75.8,-75.0),'FL':(24.5,31.0,-87.6,-80.0),'GA':(30.4,35.0,-85.6,-80.8),
    'ID':(42.0,49.0,-117.2,-111.0),'IL':(37.0,42.5,-91.5,-87.5),'IN':(37.8,41.8,-88.1,-84.8),
    'IA':(40.4,43.5,-96.6,-90.1),'KS':(37.0,40.0,-102.1,-94.6),'KY':(36.5,39.1,-89.6,-81.9),
    'LA':(29.0,33.0,-94.0,-89.0),'ME':(43.1,47.5,-71.1,-66.9),'MD':(38.0,39.7,-79.5,-75.0),
    'MA':(41.2,42.9,-73.5,-69.9),'MI':(41.7,48.3,-90.4,-82.4),'MN':(43.5,49.4,-97.2,-89.5),
    'MS':(30.2,35.0,-91.7,-88.1),'MO':(36.0,40.6,-95.8,-89.1),'MT':(44.4,49.0,-116.0,-104.0),
    'NE':(40.0,43.0,-104.1,-95.3),'NV':(35.0,42.0,-120.0,-114.0),'NH':(42.7,45.3,-72.6,-70.7),
    'NJ':(38.9,41.4,-75.6,-73.9),'NM':(31.3,37.0,-109.1,-103.0),'NY':(40.5,45.0,-79.8,-71.9),
    'NC':(33.8,36.6,-84.3,-75.5),'ND':(45.9,49.0,-104.0,-96.6),'OH':(38.4,42.0,-84.8,-80.5),
    'OK':(33.6,37.0,-103.0,-94.4),'OR':(42.0,46.3,-124.6,-116.5),'PA':(39.7,42.3,-80.5,-74.7),
    'SC':(32.0,35.2,-83.4,-78.5),'SD':(42.5,45.9,-104.1,-96.4),'TN':(35.0,36.7,-90.3,-81.6),
    'TX':(25.8,36.5,-106.6,-93.5),'UT':(37.0,42.0,-114.1,-109.0),'VT':(42.7,45.0,-73.4,-71.5),
    'VA':(36.5,39.5,-83.7,-75.2),'WA':(45.5,49.0,-124.8,-116.9),'WV':(37.2,40.6,-82.6,-77.7),
    'WI':(42.5,47.1,-92.9,-86.8),'WY':(41.0,45.0,-111.1,-104.1),
}

def lat_lng_to_state(lat, lng):
    best, best_dist = None, 999
    for st, (s,n,w,e) in STATE_BOXES.items():
        if s<=lat<=n and w<=lng<=e:
            d = math.sqrt((lat-(s+n)/2)**2 + (lng-(w+e)/2)**2)
            if d < best_dist: best_dist, best = d, st
    return best or ''

def fetch_batch(fid_start, fid_end):
    all_f, offset = [], 0
    while True:
        url = f"{EIA_URL}?where=FID>{fid_start}+AND+FID<={fid_end}&outFields=Operator,TYPEPIPE,Status,FID&returnGeometry=true&resultOffset={offset}&resultRecordCount=1000&f=json"
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'DCHub/1.0')
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            feats = data.get('features', [])
            if not feats: break
            all_f.extend(feats)
            if len(feats) < 1000: break
            offset += 1000
        except Exception as e:
            print(f"  err: {e}"); break
    return all_f

def main():
    neon_url = os.environ.get('NEON_URL') or os.environ.get('DATABASE_URL','')
    if not neon_url:
        print("Set NEON_URL"); sys.exit(1)
    conn = psycopg2.connect(neon_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM gas_pipelines")
    before = cur.fetchone()[0]
    print(f"Before: {before}")
    # Find max FID
    try:
        url = f"{EIA_URL}?where=1=1&outFields=FID&returnGeometry=false&orderByFields=FID+DESC&resultRecordCount=1&f=json"
        req = urllib.request.Request(url); req.add_header('User-Agent','DCHub/1.0')
        with urllib.request.urlopen(req, timeout=30) as resp:
            max_fid = json.loads(resp.read())['features'][0]['attributes']['FID']
        print(f"Max FID: {max_fid}")
    except: max_fid = 200000
    total_ins, total_fetch = 0, 0
    fid = 0
    while fid < max_fid:
        fe = fid + 2000
        print(f"FID {fid}-{fe}...", end=" ", flush=True)
        feats = fetch_batch(fid, fe)
        total_fetch += len(feats)
        bi = 0
        for ft in feats:
            a, g = ft.get('attributes',{}), ft.get('geometry',{})
            op = a.get('Operator','Unknown'); tp = a.get('TYPEPIPE','Interstate')
            st = a.get('Status','Operating'); fv = a.get('FID','')
            if str(st).lower() not in ('operating','active','in service'): continue
            lat=lng=None
            if g:
                if 'paths' in g and g['paths']:
                    p=g['paths'][0]; m=p[len(p)//2] if p else None
                    if m and len(m)>=2: lng,lat=m[0],m[1]
                elif 'x' in g and 'y' in g: lng,lat=g['x'],g['y']
            if not lat or not lng: continue
            state = lat_lng_to_state(lat, lng)
            pt = 'interstate' if 'Interstate' in str(tp) else 'intrastate'
            try:
                cur.execute("""INSERT INTO gas_pipelines (name,operator,pipeline_type,diameter_inches,capacity_mcf,status,lat,lng,city,state,source,source_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source_id) DO NOTHING""",
                    (f"{op} ({tp})"[:200], str(op)[:100], pt, None, None, 'active', lat, lng, '', state, 'eia', f"eia_gas_{fv}"[:100]))
                if cur.rowcount > 0: bi += 1; total_ins += 1
            except: pass
        print(f"{len(feats)} fetched, {bi} new")
        fid = fe
        time.sleep(0.5)
    cur.execute("SELECT COUNT(*) FROM gas_pipelines")
    after = cur.fetchone()[0]
    print(f"\nDone! Before:{before} Fetched:{total_fetch} Inserted:{total_ins} After:{after}")
    cur.execute("SELECT state,COUNT(*) FROM gas_pipelines WHERE source='eia' GROUP BY state ORDER BY count DESC LIMIT 10")
    for s,c in cur.fetchall(): print(f"  {s or '??'}: {c}")
    conn.close()

if __name__ == '__main__': main()
