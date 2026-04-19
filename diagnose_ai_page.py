"""
Diagnose why /ai page shows zeros — check ai_usage_tracking data
Run: python3 diagnose_ai_page.py
"""
import os, psycopg2, json

NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(NEON_URL)
cur = conn.cursor()

print("=== ai_usage_tracking contents ===")
cur.execute("SELECT COUNT(*) FROM ai_usage_tracking")
print(f"Total rows: {cur.fetchone()[0]}")

cur.execute("SELECT platform, COUNT(*) FROM ai_usage_tracking GROUP BY platform ORDER BY COUNT(*) DESC LIMIT 20")
print("\nBy platform:")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

cur.execute("SELECT MIN(tracked_at), MAX(tracked_at) FROM ai_usage_tracking")
row = cur.fetchone()
print(f"\nDate range: {row[0]} to {row[1]}")

cur.execute("SELECT COUNT(*) FROM ai_usage_tracking WHERE tracked_at >= NOW() - INTERVAL '1 day'")
print(f"Last 24h: {cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM ai_usage_tracking WHERE tracked_at >= NOW() - INTERVAL '7 days'")
print(f"Last 7 days: {cur.fetchone()[0]}")

print("\n=== ai_platforms check ===")
cur.execute("SELECT id, status, mcp_active FROM ai_platforms ORDER BY id")
for row in cur.fetchall():
    print(f"  {row[0]}: status={row[1]}, mcp_active={row[2]}")

print("\n=== platform_cards check ===")
cur.execute("SELECT COUNT(*) FROM platform_cards")
print(f"Total cards: {cur.fetchone()[0]}")

# Check what the Worker would return for /ai/platforms
print("\n=== Simulated Worker /ai/platforms response ===")
cur.execute("""SELECT id, name, status, integration_type, description, company, color, mcp_active
    FROM ai_platforms ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, name ASC""")
cols = [d[0] for d in cur.description]
rows = [dict(zip(cols, r)) for r in cur.fetchall()]
print(f"Would return {len(rows)} platforms, {len([r for r in rows if r['status']=='active'])} active")

conn.close()
