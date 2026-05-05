#!/usr/bin/env python3
"""
DCHub Map Fix Script
Fixes: missing city geocoding, news article entries, null slugs, wrong country codes
Usage: python fix_map.py
Requires: DATABASE_URL env var (set automatically in Replit)
"""
import os, re, sys, time, json, hashlib, urllib.request, urllib.parse

# ── auto-install psycopg2 if needed ──────────────────────────────────────────
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])
    import psycopg2
    from psycopg2.extras import RealDictCursor

# ── config ────────────────────────────────────────────────────────────────────
DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    sys.exit("❌  DATABASE_URL env var not set")

# Known country/city corrections  (name LIKE pattern, correct_country, correct_city, correct_state)
COUNTRY_FIXES = [
    ("%Clonee%",           "IE", "Clonee",  ""),
    ("%Lule%",             "SE", "Luleå",   ""),
    ("%Gallatin%Meta%",    "US", "Gallatin","TN"),
    ("%Gallatin%Meta%",    "US", "Gallatin","TN"),   # duplicate rows
    ("%Altoona%",          "US", "Altoona", "PA"),
]

# Entries whose names look like news headlines (no location + long name = junk row)
NEWS_MIN_NAME_LEN = 55

# ── helpers ───────────────────────────────────────────────────────────────────
def slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"[-\s]+", "-", s).strip("-")

def make_slug(provider: str, name: str, row_id: int) -> str:
    suffix = hashlib.md5(f"{provider}{name}{row_id}".encode()).hexdigest()[:8]
    return f"{slugify(provider or 'unknown')}-{slugify(name)}-{suffix}"

def nominatim_geocode(query: str) -> dict | None:
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": query, "format": "json",
                                     "addressdetails": "1", "limit": "1"}))
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-mapfix/1.0 (admin)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return data[0] if data else None
    except Exception:
        return None

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(DB_URL)
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    # Detect coordinate columns (schema varies)
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'facilities' ORDER BY ordinal_position
    """)
    all_cols = [r["column_name"] for r in cur.fetchall()]
    if not all_cols:
        sys.exit("❌  Table 'facilities' not found — check table name")
    lat_col = next((c for c in all_cols if c in ("lat", "latitude")),  None)
    lon_col = next((c for c in all_cols if c in ("lon", "longitude")), None)
    has_coords = lat_col and lon_col
    print(f"📋  Columns detected: {all_cols}")
    print(f"📍  Coord columns: {lat_col}, {lon_col}\n")

    # ── 1. Remove news-article entries ───────────────────────────────────────
    print("━━━  STEP 1 / 4  Remove news-article entries  ━━━")
    cur.execute("""
        SELECT id, name FROM facilities
        WHERE (city  IS NULL OR city  = '')
          AND (state IS NULL OR state = '')
          AND (country IS NULL OR country = '')
          AND char_length(name) >= %s
        ORDER BY id
    """, (NEWS_MIN_NAME_LEN,))
    junk = cur.fetchall()
    for row in junk:
        print(f"  🗑  [{row['id']:>6}]  {row['name'][:90]}")
        cur.execute("DELETE FROM facilities WHERE id = %s", (row["id"],))
    print(f"  → Removed {len(junk)} rows\n")

    # ── 2. Fix wrong countries ────────────────────────────────────────────────
    print("━━━  STEP 2 / 4  Fix wrong country codes  ━━━")
    seen_patterns = set()
    total_country = 0
    for pattern, country, city, state in COUNTRY_FIXES:
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        cur.execute("""
            UPDATE facilities
               SET country = %s,
                   city    = CASE WHEN city IS NULL OR city = '' THEN %s ELSE city END,
                   state   = CASE WHEN state IS NULL OR state = '' THEN %s ELSE state END
             WHERE name LIKE %s
               AND (country != %s OR city IS NULL OR city = '')
        """, (country, city, state, pattern, country))
        if cur.rowcount:
            print(f"  🌍  {cur.rowcount} row(s)  '{pattern}'  →  {city}, {country}")
            total_country += cur.rowcount
    print(f"  → Fixed {total_country} rows\n")

    # ── 3. Geocode missing cities ─────────────────────────────────────────────
    print("━━━  STEP 3 / 4  Geocode missing city/state  ━━━")
    cur.execute("""
        SELECT id, name, city, state, country
          FROM facilities
         WHERE (city IS NULL OR city = '')
           AND country IS NOT NULL AND country != ''
         ORDER BY power_mw DESC NULLS LAST
    """)
    to_geocode = cur.fetchall()
    print(f"  Found {len(to_geocode)} facilities needing geocoding…")

    ok = fail = 0
    for row in to_geocode:
        # Build a useful query: strip building numbers, use name + state + country
        query = re.sub(r"(Building|Phase|Campus)\s*\d+", "", row["name"])
        query = " ".join([query.strip(), row.get("state") or "", row["country"]])
        query = re.sub(r"\s{2,}", " ", query).strip()

        result = nominatim_geocode(query)
        time.sleep(1.1)   # Nominatim: max 1 req/sec

        if result:
            addr  = result.get("address", {})
            city  = (addr.get("city") or addr.get("town") or
                     addr.get("village") or addr.get("county") or "")
            state = addr.get("state", "")
            lat   = float(result["lat"])
            lon   = float(result["lon"])

            if has_coords:
                cur.execute(f"""
                    UPDATE facilities
                       SET city=%s, state=%s,
                           {lat_col}=%s, {lon_col}=%s
                     WHERE id=%s
                """, (city, state, lat, lon, row["id"]))
            else:
                cur.execute("""
                    UPDATE facilities SET city=%s, state=%s WHERE id=%s
                """, (city, state, row["id"]))

            status = f"→ {city}, {state} ({lat:.3f}, {lon:.3f})" if has_coords else f"→ {city}, {state}"
            print(f"  ✓ [{row['id']:>6}]  {row['name'][:45]:<45}  {status}")
            ok += 1
        else:
            print(f"  ✗ [{row['id']:>6}]  {row['name'][:45]:<45}  no result")
            fail += 1

    print(f"  → Geocoded {ok} / {len(to_geocode)}  ({fail} failed)\n")

    # ── 4. Generate missing slugs ─────────────────────────────────────────────
    print("━━━  STEP 4 / 4  Generate null slugs  ━━━")
    cur.execute("""
        SELECT id, name, provider FROM facilities WHERE slug IS NULL ORDER BY id
    """)
    null_slug_rows = cur.fetchall()
    print(f"  Found {len(null_slug_rows)} facilities with null slugs…")

    for row in null_slug_rows:
        slug = make_slug(row["provider"] or "", row["name"], row["id"])
        cur.execute("UPDATE facilities SET slug=%s WHERE id=%s", (slug, row["id"]))

    print(f"  → Generated {len(null_slug_rows)} slugs\n")

    conn.commit()
    cur.close()
    conn.close()
    print("✅  All done! Commit complete — map should now render all geocoded facilities.")

if __name__ == "__main__":
    main()
