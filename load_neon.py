import psycopg2, csv, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')
cur.execute('TRUNCATE infrastructure')
print('Truncated - loading...')

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

sql = 'INSERT INTO infrastructure (' + ','.join(COLS) + ') VALUES (' + ','.join(['%s']*len(COLS)) + ') ON CONFLICT (osm_type, osm_id) DO NOTHING'

batch, total = [], 0
with open('infrastructure_output/emea_apac_infrastructure.csv', newline='') as f:
    for row in csv.DictReader(f):
        batch.append(tuple(None if row[c]=='' else row[c] for c in COLS))
        if len(batch) >= 1000:
            cur.executemany(sql, batch)
            total += len(batch)
            batch = []
            if total % 20000 == 0:
                print(f'  {total:,} rows...')

if batch:
    cur.executemany(sql, batch)
    total += len(batch)

print(f'Done: {total:,} rows')
cur.execute('SELECT infra_type, region, COUNT(*) FROM infrastructure GROUP BY 1,2 ORDER BY 1,2')
for r in cur.fetchall(): print(f'  {r[0]:<20} {r[1]:<6} {r[2]:,}')
conn.close()
