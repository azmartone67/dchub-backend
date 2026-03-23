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
    
    # Substations — already indexed by voltage, add geo index
    ("idx_substations_geo", 
     "CREATE INDEX IF NOT EXISTS idx_substations_geo ON substations (latitude, longitude) WHERE latitude IS NOT NULL"),
    
    # Gas pipelines — geo index for spatial queries  
    ("idx_gas_pipelines_geo", 
     "CREATE INDEX IF NOT EXISTS idx_gas_pipelines_geo ON gas_pipelines (latitude, longitude) WHERE latitude IS NOT NULL"),
    
    # Power plants — geo + capacity
    ("idx_power_plants_geo", 
     "CREATE INDEX IF NOT EXISTS idx_power_plants_geo ON discovered_power_plants (latitude, longitude) WHERE latitude IS NOT NULL"),
    
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
print("""
EXPECTED IMPACT:
  /api/v1/search:  5,210ms → <500ms (trigram index on name/provider/city)
  /api/site-score: 1,200ms → <400ms (geo indexes on facilities + substations)
  get_market_stats: faster provider/status aggregation
  list_transactions: faster date/region filtering
  tier gating: faster API key lookup on every request
""")
