import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='press_releases' ORDER BY ordinal_position")
print("── COLUMNS ──")
for r in cur.fetchall():
    print(f"  {r[0]:30s} {r[1]}")
cur.close(); conn.close()
