"""
Merge discovered_facilities into facilities — fixed ID generation
Run: python3 merge_discovered_v2.py
"""
import os, psycopg2

NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(NEON_URL)
conn.autocommit = True
cur = conn.cursor()

# Check if id column has a default/sequence
cur.execute("""SELECT column_default FROM information_schema.columns 
    WHERE table_name='facilities' AND column_name='id'""")
id_default = cur.fetchone()[0]
print(f"facilities.id default: {id_default}")

# Get current max ID
cur.execute("SELECT MAX(id) FROM facilities")
max_id = cur.fetchone()[0] or 0
print(f"Current max facility id: {max_id}")

# If no auto-increment, we need to generate IDs manually
# First, add a sequence if there isn't one
if not id_default or 'nextval' not in str(id_default):
    print("No auto-increment — creating sequence...")
    try:
        cur.execute(f"CREATE SEQUENCE IF NOT EXISTS facilities_id_seq START WITH {max_id + 1}")
        cur.execute("ALTER TABLE facilities ALTER COLUMN id SET DEFAULT nextval('facilities_id_seq')")
        cur.execute(f"SELECT setval('facilities_id_seq', {max_id})")
        print(f"  ✅ Sequence created, starting at {max_id + 1}")
    except Exception as e:
        print(f"  ⚠️ Sequence setup: {e}")
        conn.rollback()
        conn.autocommit = True
        # Fallback: manually assign IDs
        print("  Using manual ID assignment instead...")

# Now try the merge
print(f"\n=== MERGING NEW FACILITIES ===")

cur.execute("""
    INSERT INTO facilities (id, name, provider, city, state, country, latitude, longitude, power_mw, status, address, region)
    SELECT 
        nextval('facilities_id_seq'),
        df.name, 
        df.provider, 
        df.city, 
        df.state, 
        df.country, 
        df.latitude, 
        df.longitude, 
        df.power_mw, 
        COALESCE(df.status, 'active'),
        df.address,
        CASE 
            WHEN df.country IN ('US', 'CA', 'MX') THEN 'North America'
            WHEN df.country IN ('UK', 'GB', 'DE', 'FR', 'NL', 'IE', 'SE', 'NO', 'DK', 'FI', 'CH', 'AT', 'BE', 'IT', 'ES', 'PT', 'PL', 'CZ') THEN 'Europe'
            WHEN df.country IN ('SG', 'JP', 'AU', 'IN', 'HK', 'KR', 'TW', 'MY', 'ID', 'TH', 'CN', 'NZ') THEN 'Asia Pacific'
            WHEN df.country IN ('BR', 'CL', 'CO', 'AR') THEN 'Latin America'
            WHEN df.country IN ('AE', 'SA', 'IL', 'QA', 'BH', 'KW') THEN 'Middle East'
            WHEN df.country IN ('ZA', 'NG', 'KE', 'EG') THEN 'Africa'
            ELSE 'Other'
        END
    FROM discovered_facilities df
    WHERE df.merged_at IS NULL 
    AND df.is_duplicate = 0
    AND df.name IS NOT NULL 
    AND df.name != ''
    AND NOT EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(df.name))
        AND (LOWER(TRIM(COALESCE(f.city,''))) = LOWER(TRIM(COALESCE(df.city,'')))
             OR LOWER(TRIM(COALESCE(f.country,''))) = LOWER(TRIM(COALESCE(df.country,''))))
    )
""")
inserted = cur.rowcount
print(f"  ✅ Merged {inserted} new facilities")

# Mark as merged
cur.execute("""
    UPDATE discovered_facilities SET merged_at = NOW()
    WHERE merged_at IS NULL AND is_duplicate = 0
    AND name IS NOT NULL AND name != ''
""")
print(f"  ✅ Marked {cur.rowcount} as merged")

# Mark remaining dupes
cur.execute("""
    UPDATE discovered_facilities SET is_duplicate = 1, merged_at = NOW()
    WHERE merged_at IS NULL
    AND EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(discovered_facilities.name))
    )
""")

# Final count
cur.execute("SELECT COUNT(*) FROM facilities")
total = cur.fetchone()[0]
print(f"\n=== FINAL COUNT: {total} facilities ===")
print(f"Hard refresh dchub.cloud to see updated count.")

conn.close()
