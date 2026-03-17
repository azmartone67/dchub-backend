"""One-time HIFLD substation loader — run on Railway via scheduler or manual trigger"""
import urllib.request, urllib.parse, json, os, psycopg2

DB_URL = os.environ.get('DATABASE_URL')
HIFLD_URL = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query"
states = ['AZ','TX','VA','GA','NV','UT','OH','IA','IL','CA','NJ','WA','OR','CO','FL','MN','MO','TN','NC','NY','MA','PA','IN','MI','WI','NE','KS','OK','AR','LA']

def load():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    total = 0
    for state in states:
        try:
            params = urllib.parse.urlencode({
                'where': f"STATE='{state}'",
                'outFields': 'NAME,STATE,MAX_VOLT,MIN_VOLT,LATITUDE,LONGITUDE,OWNER,CITY',
                'returnGeometry': 'false',
                'f': 'json',
                'resultRecordCount': '2000'
            }).encode()
            req = urllib.request.Request(HIFLD_URL, data=params, headers={
                'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'DCHub/1.0'
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            features = data.get('features', [])
            if not features: continue
            batch = 0
            for f in features:
                a = f.get('attributes', {})
                lat, lng = a.get('LATITUDE'), a.get('LONGITUDE')
                if not lat or not lng: continue
                cur.execute("""INSERT INTO substations (name, operator, voltage_kv, lat, lng, city, state, country)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'US') ON CONFLICT DO NOTHING""",
                    ((a.get('NAME') or 'Unknown')[:200], (a.get('OWNER') or 'Unknown')[:200],
                     a.get('MAX_VOLT') or a.get('MIN_VOLT') or 0, lat, lng,
                     (a.get('CITY') or '')[:100], state))
                batch += 1
            conn.commit()
            total += batch
            print(f"  {state}: {batch} substations")
        except Exception as e:
            print(f"  {state}: ERROR - {e}")
            conn.rollback()
    cur.close()
    conn.close()
    print(f"\nTotal: {total} substations loaded")
    return total

if __name__ == '__main__':
    load()
