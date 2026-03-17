"""HIFLD substation bulk loader — GET method with short URLs"""
import urllib.request, json, os, psycopg2

DB_URL = os.environ.get('DATABASE_URL')
BASE = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query"
STATES = ['AZ','TX','VA','GA','NV','UT','OH','IA','IL','CA','NJ','WA','OR','CO','FL','MN','MO','TN','NC','NY','MA','PA','IN','MI','WI','NE','KS','OK','AR','LA','SC','KY','MD','DE','ID','NM','MT','WY','AL','MS']

def load():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    total = 0
    for state in STATES:
        try:
            url = f"{BASE}?where=STATE%3D%27{state}%27&outFields=NAME%2CMAX_VOLT%2CLATITUDE%2CLONGITUDE%2COWNER%2CCITY&returnGeometry=false&f=json&resultRecordCount=2000"
            req = urllib.request.Request(url, headers={"User-Agent": "DCHub/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if data.get("error"):
                print(f"  {state}: API error - {data['error'].get('message','?')}")
                continue
            features = data.get("features", [])
            batch = 0
            for feat in features:
                a = feat.get("attributes", {})
                lat, lng = a.get("LATITUDE"), a.get("LONGITUDE")
                if not lat or not lng: continue
                cur.execute("""INSERT INTO substations (name, operator, voltage_kv, lat, lng, city, state, country)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    ((a.get("NAME") or "Unknown")[:200], (a.get("OWNER") or "Unknown")[:200],
                     a.get("MAX_VOLT") or 0, lat, lng, (a.get("CITY") or "")[:100], state, "US"))
                batch += 1
            conn.commit()
            total += batch
            if batch > 0:
                print(f"  {state}: {batch} substations loaded")
        except Exception as e:
            print(f"  {state}: ERROR - {e}")
            conn.rollback()
    cur.close()
    conn.close()
    print(f"Total: {total}")
    return total

if __name__ == "__main__":
    load()
