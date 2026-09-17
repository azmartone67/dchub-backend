"""
Merge discovered_facilities into facilities — text ID version
Run: python3 merge_discovered_v3.py
"""
import os, psycopg2, re

NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(NEON_URL)
conn.autocommit = True
cur = conn.cursor()

def make_slug(name, city, country):
    """Generate a URL-safe slug ID from facility name + location"""
    parts = [name or '', city or '', country or '']
    raw = '-'.join(p.strip() for p in parts if p.strip())
    slug = re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')
    return slug[:100] if slug else None

# Get existing IDs to avoid collisions
cur.execute("SELECT id FROM facilities")
existing_ids = set(r[0] for r in cur.fetchall())
print(f"Existing facilities: {len(existing_ids)}")

# Get mergeable discovered facilities
cur.execute("""
    SELECT df.id, df.name, df.provider, df.city, df.state, df.country, 
           df.latitude, df.longitude, df.power_mw, df.status, df.address
    FROM discovered_facilities df
    WHERE df.merged_at IS NULL 
    AND df.is_duplicate = 0
    AND df.name IS NOT NULL AND df.name != ''
    AND NOT EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(df.name))
        AND (LOWER(TRIM(COALESCE(f.city,''))) = LOWER(TRIM(COALESCE(df.city,'')))
             OR LOWER(TRIM(COALESCE(f.country,''))) = LOWER(TRIM(COALESCE(df.country,''))))
    )
""")
rows = cur.fetchall()
print(f"Candidates to merge: {len(rows)}")

inserted = 0
skipped = 0
merged_ids = []

for row in rows:
    df_id, name, provider, city, state, country, lat, lng, power, status, address = row
    
    slug = make_slug(name, city, country)
    if not slug:
        skipped += 1
        continue
    
    # Handle collisions
    final_slug = slug
    counter = 2
    while final_slug in existing_ids:
        final_slug = f"{slug}-{counter}"
        counter += 1
    
    region = 'Other'
    if country in ('US', 'CA', 'MX'): region = 'North America'
    elif country in ('UK', 'GB', 'DE', 'FR', 'NL', 'IE', 'SE', 'NO', 'DK', 'FI', 'CH', 'AT', 'BE', 'IT', 'ES', 'PT', 'PL', 'CZ', 'RO', 'HU'): region = 'Europe'
    elif country in ('SG', 'JP', 'AU', 'IN', 'HK', 'KR', 'TW', 'MY', 'ID', 'TH', 'CN', 'NZ', 'PH', 'VN'): region = 'Asia Pacific'
    elif country in ('BR', 'CL', 'CO', 'AR', 'PE'): region = 'Latin America'
    elif country in ('AE', 'SA', 'IL', 'QA', 'BH', 'KW', 'OM'): region = 'Middle East'
    elif country in ('ZA', 'NG', 'KE', 'EG', 'GH', 'MA'): region = 'Africa'
    
    try:
        cur.execute("""
            INSERT INTO facilities (id, name, provider, city, state, country, latitude, longitude, power_mw, status, address, region)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (final_slug, name, provider, city, state, country, lat, lng, power, status or 'active', address, region))
        existing_ids.add(final_slug)
        merged_ids.append(df_id)
        inserted += 1
    except Exception as e:
        skipped += 1

print(f"  ✅ Inserted {inserted} new facilities")
print(f"  ⏭️  Skipped {skipped}")

# Mark merged
if merged_ids:
    cur.execute("UPDATE discovered_facilities SET merged_at = NOW() WHERE id = ANY(%s)", (merged_ids,))
    print(f"  ✅ Marked {len(merged_ids)} as merged")

# Mark remaining unmerged as duplicates
cur.execute("""
    UPDATE discovered_facilities SET is_duplicate = 1, merged_at = NOW()
    WHERE merged_at IS NULL AND name IS NOT NULL AND name != ''
    AND EXISTS (
        SELECT 1 FROM facilities f 
        WHERE LOWER(TRIM(f.name)) = LOWER(TRIM(discovered_facilities.name))
    )
""")

# Final count
cur.execute("SELECT COUNT(*) FROM facilities")
total = cur.fetchone()[0]
print(f"\n=== FINAL COUNT: {total} facilities ===")
print("Hard refresh dchub.cloud to see updated count.")

conn.close()
