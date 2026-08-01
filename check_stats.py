"""Check actual numbers vs what homepage shows"""
import os, psycopg2
from util.capacity_pipeline import CP_OK
from util.deals import DEALS_OK
conn = psycopg2.connect(os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL'))
cur = conn.cursor()

checks = [
    ("Facilities", "SELECT COUNT(*) FROM facilities"),
    # Guarded: the homepage publishes the deduped canon ("1,400+"), so the
    # raw 4,711 made the one script whose job is comparing actual data to the
    # homepage report a 2.6x disagreement that was really its own missing
    # filter. See util/deals.
    ("Deals", f"SELECT COUNT(*) FROM deals WHERE {DEALS_OK}"),
    ("News articles", "SELECT COUNT(*) FROM news_articles"),
    ("Pipeline projects", f"SELECT COUNT(*) FROM capacity_pipeline WHERE {CP_OK}"),
    ("Ecosystem companies", "SELECT COUNT(*) FROM ecosystem_companies"),
    ("Countries", "SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL AND country != ''"),
    ("Markets (cities)", "SELECT COUNT(DISTINCT city) FROM facilities WHERE city IS NOT NULL AND city != ''"),
    # 2026-07-31: was SUM(power_mw) — no such column on capacity_pipeline, so
    # this row printed "ERROR: column power_mw does not exist" on every run of
    # a script whose whole job is comparing actual data to the homepage. Real
    # column is capacity_mw (real, not text — the NULLIF/CAST dance was for a
    # text column that never existed either). Quarantine guard matches the
    # published surfaces; see util/capacity_pipeline.
    ("Pipeline total GW", f"SELECT ROUND((COALESCE(SUM(capacity_mw),0)/1000)::numeric, 1) FROM capacity_pipeline WHERE capacity_mw IS NOT NULL AND {CP_OK}"),
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
