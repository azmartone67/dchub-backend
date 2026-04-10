import psycopg2, psycopg2.extras, csv, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')
cur.execute('TRUNCATE infrastructure')
print('Truncated - loading...')

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

def clean_row(row):
    out = []
    for c in COLS:
        v = row[c] if row[c] != '' else None
        if c == 'voltage_kv' and v is not None:
            try:
                v = int(str(v).split(';')[0].replace(',','').strip())
                if v > 2000000000: v = None
            except: v = None
        out.append(v)
    return tuple(out)

batch, total = [], 0
with open('infrastructure_output/emea_apac_infrastructure.csv', newline='') as f:
    for row in csv.DictReader(f):
        batch.append(clean_row(row))
        if len(batch) >= 5000:
            psycopg2.extras.execute_values(cur, sql, batch, page_size=5000)
            total += len(batch)
            batch = []
            print(f'  {total:,} rows...')

if batch:
    psycopg2.extras.execute_values(cur, sql, batch, page_size=5000)
    total += len(batch)

print(f'Done: {total:,} rows')
cur.execute('SELECT infra_type, region, COUNT(*) FROM infrastructure GROUP BY 1,2 ORDER BY 1,2')
for r in cur.fetchall(): print(f'  {r[0]:<20} {r[1]:<6} {r[2]:,}')
conn.close()
