"""Check capacity_pipeline schema and verify stats endpoint data"""
import os, psycopg2
conn = psycopg2.connect(os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL'))
cur = conn.cursor()

print("=== capacity_pipeline columns ===")
cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='capacity_pipeline' ORDER BY ordinal_position")
for r in cur.fetchall():
    print(f"  {r[0]}: {r[1]}")

print("\n=== What /api/v1/stats should return ===")
cur.execute("SELECT COUNT(*) FROM facilities")
fac = cur.fetchone()[0]
print(f"total_facilities: {fac}")

cur.execute("SELECT COUNT(*) FROM deals")
deals = cur.fetchone()[0]
print(f"total_deals: {deals}")

cur.execute("SELECT COUNT(*) FROM news_articles")
news = cur.fetchone()[0]
print(f"total_news: {news}")

cur.execute("SELECT COUNT(*) FROM capacity_pipeline")
pipe = cur.fetchone()[0]
print(f"total_pipeline: {pipe}")

combined = fac + deals + news + pipe
print(f"combined_records: {combined}")

cur.execute("SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL AND country != ''")
countries = cur.fetchone()[0]
print(f"countries: {countries}")

conn.close()
