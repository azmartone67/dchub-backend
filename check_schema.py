import os, psycopg2
conn = psycopg2.connect(os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL'))
cur = conn.cursor()
cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='ai_usage_tracking' ORDER BY ordinal_position")
print("ai_usage_tracking columns:")
for r in cur.fetchall():
    print(f"  {r[0]} ({r[1]})")
cur.execute("SELECT * FROM ai_usage_tracking LIMIT 3")
cols = [d[0] for d in cur.description]
print(f"\nColumns: {cols}")
for row in cur.fetchall():
    print(dict(zip(cols, row)))
conn.close()
