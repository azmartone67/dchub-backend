"""Check actual numbers vs what homepage shows"""
import os, psycopg2
conn = psycopg2.connect(os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL'))
cur = conn.cursor()

checks = [
    ("Facilities", "SELECT COUNT(*) FROM facilities"),
    ("Deals", "SELECT COUNT(*) FROM deals"),
    ("News articles", "SELECT COUNT(*) FROM news_articles"),
    ("Pipeline projects", "SELECT COUNT(*) FROM capacity_pipeline"),
    ("Ecosystem companies", "SELECT COUNT(*) FROM ecosystem_companies"),
    ("Countries", "SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL AND country != ''"),
    ("Markets (cities)", "SELECT COUNT(DISTINCT city) FROM facilities WHERE city IS NOT NULL AND city != ''"),
    ("Pipeline total GW", "SELECT ROUND(COALESCE(SUM(CAST(NULLIF(power_mw,'') AS FLOAT)),0)/1000, 1) FROM capacity_pipeline WHERE power_mw IS NOT NULL"),
]

print("=== ACTUAL NEON DATA vs HOMEPAGE ===\n")
print(f"{'Metric':<25} {'Actual':<15} {'Homepage Shows'}")
print("-" * 60)
for label, sql in checks:
    try:
        cur.execute(sql)
        val = cur.fetchone()[0]
        print(f"{label:<25} {str(val):<15}")
    except Exception as e:
        conn.rollback()
        print(f"{label:<25} ERROR: {e}")

# Check discovered_facilities too
cur.execute("SELECT COUNT(*) FROM facilities")
fac = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM discovered_facilities")
disc = cur.fetchone()[0]
print(f"\n{'facilities + discovered':<25} {fac + disc}")

conn.close()
