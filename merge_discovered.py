"""
Merge discovered_facilities into main facilities table in Neon
Step 1: Check what we have
Step 2: Merge non-duplicate discovered facilities
Run: python3 merge_discovered.py
"""
import os, psycopg2

NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(NEON_URL)
conn.autocommit = True
cur = conn.cursor()

# ═══════════════════════════════════════════════════════════
# STEP 1: Analyze discovered_facilities
# ═══════════════════════════════════════════════════════════
print("=== ANALYZING discovered_facilities ===")

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='discovered_facilities' ORDER BY ordinal_position")
disc_cols = [r[0] for r in cur.fetchall()]
print(f"Columns: {', '.join(disc_cols)}")

cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NULL AND is_duplicate = 0")
mergeable = cur.fetchone()[0]
print(f"Mergeable (unmerged, not duplicate): {mergeable}")

cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NULL AND is_duplicate = 1")
dupes = cur.fetchone()[0]
print(f"Flagged as duplicate: {dupes}")

cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NOT NULL")
already_merged = cur.fetchone()[0]
print(f"Already merged: {already_merged}")

# Check sources
cur.execute("""SELECT source, COUNT(*) FROM discovered_facilities 
    WHERE merged_at IS NULL AND is_duplicate = 0 
    GROUP BY source ORDER BY COUNT(*) DESC""")
print("\nMergeable by source:")
for r in cur.fetchall():
    print(f"  {r[0]}: {r[1]}")

# Check what columns facilities table has
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='facilities' ORDER BY ordinal_position")
fac_cols = [r[0] for r in cur.fetchall()]
print(f"\nFacilities table columns: {', '.join(fac_cols[:15])}...")

# ═══════════════════════════════════════════════════════════
# STEP 2: Check for actual duplicates by name+city
# ═══════════════════════════════════════════════════════════
print("\n=== DEDUP CHECK ===")
cur.execute("""
    SELECT COUNT(*) FROM discovered_facilities df
    WHERE df.merged_at IS NULL AND df.is_duplicate = 0
    AND EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(df.name))
        AND (LOWER(TRIM(COALESCE(f.city,''))) = LOWER(TRIM(COALESCE(df.city,'')))
             OR LOWER(TRIM(COALESCE(f.country,''))) = LOWER(TRIM(COALESCE(df.country,''))))
    )
""")
actual_dupes = cur.fetchone()[0]
print(f"Would-be duplicates (name+city/country match): {actual_dupes}")

truly_new = mergeable - actual_dupes
print(f"Truly new facilities to add: {truly_new}")

# ═══════════════════════════════════════════════════════════
# STEP 3: Merge truly new facilities
# ═══════════════════════════════════════════════════════════
print(f"\n=== MERGING {truly_new} NEW FACILITIES ===")

# Find common columns between discovered_facilities and facilities
common = [c for c in ['name', 'provider', 'city', 'state', 'country', 'latitude', 'longitude', 
                        'power_mw', 'status', 'tier_level', 'region', 'address'] 
          if c in disc_cols and c in fac_cols]
print(f"Mapping columns: {', '.join(common)}")

col_list = ', '.join(common)
# Add source tracking
if 'source' in fac_cols:
    insert_cols = col_list + ', source'
    select_cols = col_list + ", COALESCE(source, 'discovered')"
elif 'data_source' in fac_cols:
    insert_cols = col_list + ', data_source'
    select_cols = col_list + ", COALESCE(source, 'discovered')"
else:
    insert_cols = col_list
    select_cols = col_list

cur.execute(f"""
    INSERT INTO facilities ({insert_cols})
    SELECT {select_cols}
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
print(f"  ✅ Inserted {inserted} new facilities")

# Mark them as merged
cur.execute("""
    UPDATE discovered_facilities 
    SET merged_at = NOW()
    WHERE merged_at IS NULL 
    AND is_duplicate = 0
    AND name IS NOT NULL AND name != ''
    AND NOT EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(discovered_facilities.name))
        AND (LOWER(TRIM(COALESCE(f.city,''))) = LOWER(TRIM(COALESCE(discovered_facilities.city,'')))
             OR LOWER(TRIM(COALESCE(f.country,''))) = LOWER(TRIM(COALESCE(discovered_facilities.country,''))))
    )
""")

# Mark actual dupes
cur.execute("""
    UPDATE discovered_facilities 
    SET is_duplicate = 1, merged_at = NOW()
    WHERE merged_at IS NULL
    AND EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(discovered_facilities.name))
        AND (LOWER(TRIM(COALESCE(f.city,''))) = LOWER(TRIM(COALESCE(discovered_facilities.city,'')))
             OR LOWER(TRIM(COALESCE(f.country,''))) = LOWER(TRIM(COALESCE(discovered_facilities.country,''))))
    )
""")
print(f"  ✅ Marked duplicates")

# ═══════════════════════════════════════════════════════════
# VERIFY
# ═══════════════════════════════════════════════════════════
print("\n=== FINAL COUNTS ===")
cur.execute("SELECT COUNT(*) FROM facilities")
total = cur.fetchone()[0]
print(f"facilities: {total}")

cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NULL")
remaining = cur.fetchone()[0]
print(f"discovered_facilities still unmerged: {remaining}")

print(f"\n🎉 Homepage should now show {total} facilities!")
print("Hard refresh dchub.cloud to see updated count.")

conn.close()
