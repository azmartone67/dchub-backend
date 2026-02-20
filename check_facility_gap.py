"""
Check facility count gap between Neon and Replit
Run: python3 check_facility_gap.py
"""
import os, psycopg2, sqlite3

# Check Neon
print("=== NEON (what the website shows) ===")
NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
if NEON_URL:
    conn = psycopg2.connect(NEON_URL)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM facilities")
    print(f"facilities table: {cur.fetchone()[0]}")
    try:
        cur.execute("SELECT COUNT(*) FROM discovered_facilities")
        print(f"discovered_facilities table: {cur.fetchone()[0]}")
    except:
        conn.rollback()
        print("discovered_facilities: table doesn't exist in Neon")
    try:
        cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NULL")
        print(f"  unmerged: {cur.fetchone()[0]}")
    except:
        conn.rollback()
    conn.close()

# Check Replit SQLite
print("\n=== REPLIT SQLite ===")
for db_path in ['dchub.db', 'dc_nexus.db', 'data.db', 'facilities.db']:
    if os.path.exists(db_path):
        try:
            sconn = sqlite3.connect(db_path)
            sc = sconn.cursor()
            # List tables
            sc.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%facilit%'")
            tables = [r[0] for r in sc.fetchall()]
            for t in tables:
                sc.execute(f"SELECT COUNT(*) FROM {t}")
                count = sc.fetchone()[0]
                print(f"  {db_path} → {t}: {count}")
            sconn.close()
        except Exception as e:
            print(f"  {db_path}: error - {e}")

# Check what Replit's Flask app returns
print("\n=== What Replit backend reports ===")
try:
    import requests
    r = requests.get('http://localhost:5000/api/v1/stats', timeout=5)
    if r.ok:
        data = r.json()
        print(f"  /api/v1/stats facilities: {data.get('stats', {}).get('facilities', data.get('facilities', 'N/A'))}")
except Exception as e:
    print(f"  Could not reach local backend: {e}")
