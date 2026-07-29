#!/usr/bin/env python3
"""
DC Hub Performance Indexes — Run in Railway shell
==================================================
Adds missing database indexes to eliminate full table scans.

Target: /api/v1/search taking 5,210ms → should be <500ms

Usage:
  python /tmp/add_performance_indexes.py
"""

import os
import sys
import psycopg2

db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
if not db_url:
    print("❌ No DATABASE_URL found")
    sys.exit(1)

conn = psycopg2.connect(db_url)
conn.autocommit = True
cur = conn.cursor()

print("=" * 60)
print("DC Hub Performance Indexes")
print("=" * 60)

# Check existing indexes
cur.execute("""
    SELECT indexname FROM pg_indexes 
    WHERE tablename = 'facilities'
    ORDER BY indexname
""")
existing = [r[0] for r in cur.fetchall()]
print(f"\nExisting facilities indexes: {len(existing)}")
for idx in existing:
    print(f"  - {idx}")

indexes = [
    # Search performance — the big win
    # ILIKE '%query%' needs trigram index for fast substring search
    ("idx_facilities_name_lower", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_name_lower ON facilities (LOWER(name))"),
    
    ("idx_facilities_provider_lower", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_provider_lower ON facilities (LOWER(provider))"),
    
    # State/country filtering — used by search_facilities with state= param
    ("idx_facilities_state", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_state ON facilities (state)"),
    
    ("idx_facilities_country", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_country ON facilities (country)"),
    
    # Provider lookup — used by get_market_stats, top_providers queries
    ("idx_facilities_provider", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_provider ON facilities (provider)"),
    
    # Status filtering — used by pipeline queries, by_status aggregation
    ("idx_facilities_status", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_status ON facilities (status)"),
    
    # Composite index for common search pattern: state + provider
    ("idx_facilities_state_provider", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_state_provider ON facilities (state, provider)"),
    
    # Power capacity range queries
    ("idx_facilities_power_mw", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_power_mw ON facilities (power_mw) WHERE power_mw IS NOT NULL"),
    
    # Geo lookups for nearby facility queries
    ("idx_facilities_lat_lng", 
     "CREATE INDEX IF NOT EXISTS idx_facilities_lat_lng ON facilities (latitude, longitude) WHERE latitude IS NOT NULL"),
    
    # discovered_facilities — used by site-score spatial queries
    ("idx_discovered_fac_geo", 
     "CREATE INDEX IF NOT EXISTS idx_discovered_fac_geo ON discovered_facilities (latitude, longitude) WHERE latitude IS NOT NULL"),

    # r-facility-resolver (2026-07-14): /facility/<id_or_slug> (routes/seo_pages.py:371)
    # matches on a 3-branch OR that was UN-indexable → a 21k-row seq scan per hit, which
    # held pooler connections and turned into the ~3k /facility 5xx under pool pressure.
    # These expression indexes let the planner do a BitmapOr instead. Each MUST match the
    # query expression byte-for-byte — the MD5 slug mirrors hash_sql() (routes/facility_slug.py:13),
    # the name-slug mirrors the LOWER(REPLACE(REPLACE(...))) branch. All functions are
    # IMMUTABLE (MD5/LEFT/COALESCE/LOWER/REPLACE/int::text), so the expression indexes build.
    # Verify adoption after apply: EXPLAIN the resolver SELECT and confirm Bitmap Index Scan.
    ("idx_df_id_text",
     "CREATE INDEX IF NOT EXISTS idx_df_id_text ON discovered_facilities ((CAST(id AS TEXT)))"),
    ("idx_df_md5slug",
     "CREATE INDEX IF NOT EXISTS idx_df_md5slug ON discovered_facilities ((LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8)))"),

    # facilities-table twin of idx_df_md5slug — facility_by_slug's fallback
    # query (WHERE LEFT(MD5(provider|name),8)=%s over `facilities`) was
    # unindexed, seq-scanning under crawler load and starving the read pool
    # (the /api/v1/facilities/<slug> hard_burn). 2026-07-16.
    ("idx_facilities_md5slug",
     "CREATE INDEX IF NOT EXISTS idx_facilities_md5slug ON facilities ((LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8)))"),
    ("idx_df_nameslug",
     "CREATE INDEX IF NOT EXISTS idx_df_nameslug ON discovered_facilities ((LOWER(REPLACE(REPLACE(COALESCE(name,''),' ','-'),',',''))))"),
    ("idx_df_canonical_slug",
     "CREATE INDEX IF NOT EXISTS idx_df_canonical_slug ON discovered_facilities (canonical_slug)"),

    # News articles — used by /api/news/live
    ("idx_news_published", 
     "CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles (published_at DESC)"),
    
    ("idx_news_category", 
     "CREATE INDEX IF NOT EXISTS idx_news_category ON news_articles (category)"),
    
    # Transactions — used by list_transactions
    ("idx_transactions_date", 
     "CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date DESC) WHERE date IS NOT NULL"),
    
    ("idx_transactions_region", 
     "CREATE INDEX IF NOT EXISTS idx_transactions_region ON transactions (region)"),
    
    # ★★ shell#41 WS5 (2026-07-29) — COLUMN-NAME CORRECTION. The three geo
    # indexes below named `latitude, longitude` on tables whose LIVE columns
    # are `lat, lng` (and `power_plants.lat, lon`), and every statement in this
    # script runs inside a swallowing try/except (see the loop at the bottom),
    # so all three have been failing SILENTLY. Live schema, read from
    # /api/v1/admin/schema on 2026-07-29:
    #   substations      → lat, lng      (NOT latitude/longitude)
    #   gas_pipelines    → lat, lng      (a duplicate `lon` also exists)
    #   power_plants     → lat, LON      (and the table is power_plants —
    #                                     `discovered_power_plants` is not the
    #                                     live table name)
    # The same wrong names are still present at scripts/hifld_csv_loader.py:64
    # and scripts/hifld_substations_loader.py:105.
    # Corroborating timing (not proof — no EXPLAIN access):
    # /api/v1/grid/transmission-proximity over EMPTY rural Montana at
    # radius_km=10 costs 0.89-0.93 s against a 0.16-0.25 s /health baseline,
    # and stays flat as radius grows to 200 km. Flat-cost-regardless-of-
    # selectivity over 126,840 rows is the seq-scan signature.
    # DO NOT run this script from a boot path (DDL storms are a filed
    # incident); run it as an admin one-shot and ASSERT afterwards — see the
    # pg_indexes verification block below. CREATE INDEX CONCURRENTLY cannot
    # run inside a transaction, which is why this script sets autocommit=True.
    ("idx_substations_lat_lng",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_substations_lat_lng ON substations (lat, lng) WHERE lat IS NOT NULL AND lng IS NOT NULL"),

    # Gas pipelines — geo index for spatial queries (live cols: lat, lng)
    ("idx_gas_pipelines_lat_lng",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gas_pipelines_lat_lng ON gas_pipelines (lat, lng) WHERE lat IS NOT NULL"),

    # WS5 cross-layer fiber + carrier proximity reads (set-wide bbox scans).
    ("idx_fcc_fiber_hex_lat_lng",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fcc_fiber_hex_lat_lng ON fcc_fiber_hex (lat, lng)"),
    ("idx_cfp_lat_lng",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cfp_lat_lng ON carrier_facility_presence (facility_lat, facility_lng)"),

    # r-facility-resolver (2026-07-14): the /api/v1/gas-pipelines sort
    # (deals_routes.py:1207 ORDER BY diameter_inches DESC NULLS LAST) was a
    # full-table sort without this.
    # ★ WS5 CAVEAT (2026-07-29): `diameter_inches` is populated on 0 of 400
    # live rows, and the live schema carries a SECOND column `diameter_in`
    # (double precision). Indexing an all-NULL column is a no-op, and the
    # ORDER BY it supports is a no-op sort that promises "biggest pipeline
    # first". Do NOT delete either without first checking which column holds
    # the data — that check belongs in its own change, not this one.
    ("idx_gas_pipelines_diam",
     "CREATE INDEX IF NOT EXISTS idx_gas_pipelines_diam ON gas_pipelines (diameter_inches DESC NULLS LAST)"),

    # Power plants — geo + capacity (live table power_plants; cols lat, lon)
    ("idx_power_plants_lat_lon",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_power_plants_lat_lon ON power_plants (lat, lon) WHERE lat IS NOT NULL"),

    # API keys — used on every authenticated request
    ("idx_api_keys_value", 
     "CREATE INDEX IF NOT EXISTS idx_api_keys_value ON api_keys (key_value) WHERE is_active = TRUE"),
    
    # Daily record usage — used by tier gating on every MCP call
    ("idx_daily_usage_key_date", 
     "CREATE INDEX IF NOT EXISTS idx_daily_usage_key_date ON daily_record_usage (api_key, usage_date)"),
    
    # Capacity pipeline — used by get_pipeline
    ("idx_pipeline_status", 
     "CREATE INDEX IF NOT EXISTS idx_pipeline_status ON capacity_pipeline (status)"),
    
    ("idx_pipeline_capacity", 
     "CREATE INDEX IF NOT EXISTS idx_pipeline_capacity ON capacity_pipeline (capacity_mw DESC)"),
]

# Try trigram extension for fuzzy search (massive speedup for ILIKE)
print("\n--- Trigram Extension ---")
try:
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    print("✅ pg_trgm extension enabled")
    
    # Add trigram indexes for fast ILIKE search
    indexes.append(
        ("idx_facilities_name_trgm",
         "CREATE INDEX IF NOT EXISTS idx_facilities_name_trgm ON facilities USING gin (name gin_trgm_ops)")
    )
    indexes.append(
        ("idx_facilities_provider_trgm",
         "CREATE INDEX IF NOT EXISTS idx_facilities_provider_trgm ON facilities USING gin (provider gin_trgm_ops)")
    )
    indexes.append(
        ("idx_facilities_city_trgm",
         "CREATE INDEX IF NOT EXISTS idx_facilities_city_trgm ON facilities USING gin (city gin_trgm_ops)")
    )
except Exception as e:
    print(f"⚠️ pg_trgm not available: {e} (ILIKE will use btree indexes as fallback)")

print("\n--- Creating Indexes ---")
created = 0
skipped = 0
failed = 0

for name, sql in indexes:
    try:
        if name in existing:
            skipped += 1
            continue
        cur.execute(sql)
        print(f"  ✅ {name}")
        created += 1
    except Exception as e:
        print(f"  ⚠️ {name}: {str(e)[:80]}")
        failed += 1

# ★ ASSERT the geo indexes actually exist (shell#41 WS5, 2026-07-29).
# Every CREATE above sits inside a swallowing try/except — that is exactly how
# idx_substations_geo / idx_gas_pipelines_geo / idx_power_plants_geo "existed"
# for months while naming columns that do not. A creation loop that prints OK
# is not evidence; pg_indexes is. This block is deliberately OUTSIDE the
# try/except and drives a non-zero exit so a runner cannot report green on a
# silent failure.
print("\n--- Verifying Geo Indexes (pg_indexes) ---")
_GEO_EXPECTED = [
    ('substations',               'idx_substations_lat_lng'),
    ('gas_pipelines',             'idx_gas_pipelines_lat_lng'),
    ('fcc_fiber_hex',             'idx_fcc_fiber_hex_lat_lng'),
    ('carrier_facility_presence', 'idx_cfp_lat_lng'),
    ('power_plants',              'idx_power_plants_lat_lon'),
]
_geo_missing = []
for _tbl, _idx in _GEO_EXPECTED:
    try:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
            (_tbl, _idx))
        _row = cur.fetchone()
    except Exception as _e:
        _row = None
        print(f"  warn {_idx}: verification query failed: {str(_e)[:80]}")
    if _row and ('lat' in _row[0]):
        print(f"  OK   {_idx} present on {_tbl}")
    else:
        _geo_missing.append(f"{_tbl}.{_idx}")
        print(f"  FAIL {_idx} MISSING on {_tbl} — the CREATE was swallowed")

# Analyze tables to update query planner statistics
print("\n--- Analyzing Tables ---")
for table in ['facilities', 'discovered_facilities', 'news_articles', 
              'transactions', 'substations', 'gas_pipelines', 'capacity_pipeline',
              'api_keys', 'daily_record_usage']:
    try:
        cur.execute(f"ANALYZE {table}")
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]
        print(f"  ✅ {table}: {count:,} rows analyzed")
    except Exception as e:
        print(f"  ⚠️ {table}: {str(e)[:60]}")

conn.close()

print(f"\n{'=' * 60}")
print(f"SUMMARY: {created} created, {skipped} already existed, {failed} failed")
print(f"{'=' * 60}")
if _geo_missing:
    # Non-zero exit so a runner cannot report green while the geo indexes are
    # absent — the exact failure mode this script shipped with for months.
    print(f"GEO INDEXES MISSING: {', '.join(_geo_missing)}")
    sys.exit(1)
print("""
EXPECTED IMPACT:
  /api/v1/search:  5,210ms → <500ms (trigram index on name/provider/city)
  /api/site-score: 1,200ms → <400ms (geo indexes on facilities + substations)
  get_market_stats: faster provider/status aggregation
  list_transactions: faster date/region filtering
  tier gating: faster API key lookup on every request
""")
