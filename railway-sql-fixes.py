"""
DC Hub Railway SQL Fixes — March 2, 2026 RFO
=============================================
Root cause: knowledge_sync crawler fires broken SQL queries that exhaust
the Neon connection pool (14/8 = 175%), causing Railway to timeout.

This file contains:
  1. All broken SQL queries identified in logs → fixed versions
  2. A connection pool semaphore wrapper to prevent pool exhaustion
  3. A patched knowledge_sync snippet with pool-safe execution

Deploy: Apply these fixes to your Railway backend (knowledge_sync crawler module)
"""

# ============================================================
# FIX 1: SQL Query Patches
# ============================================================
# 
# Each fix below maps the broken query from the logs to the corrected version.
# Apply these to wherever the queries live in your codebase (likely knowledge_sync.py
# or a similar crawler module).

SQL_FIXES = {

    # ---------------------------------------------------------
    # BUG: last_updated is TEXT, not TIMESTAMP
    # ERROR: operator does not exist: text > timestamp with time zone
    # Appears in: facilities freshness check, 30-day city aggregation
    # ---------------------------------------------------------
    "fix_1_facilities_freshness_7d": {
        "broken": """
            SELECT COUNT(*) FROM facilities 
            WHERE last_updated > (NOW() - INTERVAL '7 days')
        """,
        "fixed": """
            SELECT COUNT(*) FROM facilities 
            WHERE last_updated IS NOT NULL 
              AND last_updated != ''
              AND last_updated::timestamptz > (NOW() - INTERVAL '7 days')
        """,
        "long_term": "ALTER TABLE facilities ALTER COLUMN last_updated TYPE TIMESTAMPTZ USING last_updated::timestamptz;"
    },

    "fix_2_facilities_freshness_30d": {
        "broken": """
            SELECT city, COUNT(*) as cnt FROM facilities 
            WHERE last_updated > (NOW() - INTERVAL '30 days')
        """,
        "fixed": """
            SELECT city, COUNT(*) as cnt FROM facilities 
            WHERE last_updated IS NOT NULL 
              AND last_updated != ''
              AND last_updated::timestamptz > (NOW() - INTERVAL '30 days')
            GROUP BY city
            ORDER BY cnt DESC
        """,
    },

    # ---------------------------------------------------------
    # BUG: GROUP_CONCAT is MySQL, not PostgreSQL
    # ERROR: function group_concat(text) does not exist
    # ---------------------------------------------------------
    "fix_3_group_concat": {
        "broken": """
            SELECT provider, COUNT(*) as cnt, 
                   GROUP_CONCAT(DISTINCT city) as cities
        """,
        "fixed": """
            SELECT provider, COUNT(*) as cnt, 
                   STRING_AGG(DISTINCT city, ', ') as cities
        """,
        "note": "STRING_AGG is the PostgreSQL equivalent of MySQL GROUP_CONCAT"
    },

    # ---------------------------------------------------------
    # BUG: Column alias 'cnt' used in HAVING clause
    # ERROR: column "cnt" does not exist (in HAVING)
    # PostgreSQL doesn't allow SELECT aliases in HAVING
    # ---------------------------------------------------------
    "fix_4_having_alias": {
        "broken": """
            SELECT city, state, country, COUNT(*) as cnt,
                   SUM(CASE WHEN power_mw IS NOT NULL THEN power_mw ELSE 0 END) as total_mw
            FROM facilities
            GROUP BY city, state, country
            HAVING cnt >= 3
        """,
        "fixed": """
            SELECT city, state, country, COUNT(*) as cnt,
                   SUM(CASE WHEN power_mw IS NOT NULL THEN power_mw::float ELSE 0 END) as total_mw
            FROM facilities
            GROUP BY city, state, country
            HAVING COUNT(*) >= 3
            ORDER BY cnt DESC
        """,
    },

    # ---------------------------------------------------------
    # BUG: MAX(real, numeric) type mismatch in UPSERT
    # ERROR: function max(real, numeric) does not exist
    # The confidence column is REAL but the inserted value is NUMERIC
    # This appears in both industry_glossary and knowledge_items tables
    # ---------------------------------------------------------
    "fix_5a_glossary_upsert": {
        "broken": """
            INSERT INTO industry_glossary (term, definition, category, confidence)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (term) DO UPDATE SET
                definition = EXCLUDED.definition,
                category = EXCLUDED.category,
                confidence = MAX(industry_glossary.confidence, EXCLUDED.confidence)
        """,
        "fixed": """
            INSERT INTO industry_glossary (term, definition, category, confidence)
            VALUES (%s, %s, %s, %s::real)
            ON CONFLICT (term) DO UPDATE SET
                definition = EXCLUDED.definition,
                category = EXCLUDED.category,
                confidence = GREATEST(industry_glossary.confidence, EXCLUDED.confidence::real)
        """,
        "note": "MAX() is an aggregate, not a scalar comparison. Use GREATEST() for row-level max. Also cast EXCLUDED.confidence to match column type."
    },

    "fix_5b_knowledge_items_upsert": {
        "broken": """
            INSERT INTO knowledge_items (category, key, confidence, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (...) DO UPDATE SET
                confidence = MAX(knowledge_items.confidence, EXCLUDED.confidence)
        """,
        "fixed": """
            INSERT INTO knowledge_items (category, key, confidence, source)
            VALUES (%s, %s, %s::real, %s)
            ON CONFLICT (category, key) DO UPDATE SET
                confidence = GREATEST(knowledge_items.confidence, EXCLUDED.confidence::real),
                source = EXCLUDED.source
        """,
        "note": "Same fix: GREATEST() instead of MAX(), explicit ::real cast"
    },
}


# ============================================================
# FIX 2: Connection Pool Semaphore
# ============================================================
# 
# The crawler was firing 15+ concurrent DB queries, blowing the pool
# from 8 → 14 connections (175%). This semaphore limits concurrency.

import asyncio
from contextlib import asynccontextmanager


class PoolSafeSemaphore:
    """
    Limits concurrent database operations to prevent pool exhaustion.
    
    Your Neon pool has 8 connections. The crawler should use at most 4,
    leaving headroom for API requests.
    
    Usage:
        db_limiter = PoolSafeSemaphore(max_concurrent=4)
        
        async with db_limiter.acquire("knowledge_items upsert"):
            await db.execute(query)
    """
    
    def __init__(self, max_concurrent: int = 4):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0
        self._max = max_concurrent
        self._total_queued = 0
    
    @asynccontextmanager
    async def acquire(self, label: str = ""):
        self._total_queued += 1
        if self._active >= self._max:
            print(f"[POOL-LIMITER] Queuing: {label} (active: {self._active}/{self._max})")
        
        async with self._semaphore:
            self._active += 1
            try:
                yield
            finally:
                self._active -= 1
    
    @property
    def stats(self):
        return {
            "active": self._active,
            "max": self._max,
            "total_queued": self._total_queued,
        }


# For synchronous code (if your crawler uses sync DB calls):
import threading


class PoolSafeSemaphoreSync:
    """Synchronous version for non-async crawlers."""
    
    def __init__(self, max_concurrent: int = 4):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._active = 0
        self._max = max_concurrent
        self._lock = threading.Lock()
    
    def __enter__(self):
        with self._lock:
            if self._active >= self._max:
                print(f"[POOL-LIMITER] Queuing (active: {self._active}/{self._max})")
        self._semaphore.acquire()
        with self._lock:
            self._active += 1
        return self
    
    def __exit__(self, *args):
        with self._lock:
            self._active -= 1
        self._semaphore.release()


# ============================================================
# FIX 3: Patched Crawler Snippet
# ============================================================
#
# Wrap all DB operations in the semaphore. Example integration:

"""
# At module level:
db_limiter = PoolSafeSemaphoreSync(max_concurrent=4)

# Before (broken — fires all queries concurrently):
def sync_knowledge_items(items):
    for item in items:
        cursor.execute(INSERT_QUERY, (item['category'], item['key'], item['confidence'], item['source']))

# After (fixed — limits concurrent DB access):
def sync_knowledge_items(items):
    for item in items:
        with db_limiter:
            cursor.execute(FIXED_KNOWLEDGE_ITEMS_UPSERT, (
                item['category'], 
                item['key'], 
                item['confidence'],  # will be cast to ::real in the query
                item['source']
            ))

# Or for batch operations, batch them into chunks:
def sync_knowledge_items_batched(items, batch_size=50):
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        with db_limiter:
            # Use executemany or a single multi-row INSERT
            values_list = []
            params = []
            for item in batch:
                values_list.append("(%s, %s, %s::real, %s)")
                params.extend([item['category'], item['key'], item['confidence'], item['source']])
            
            query = f'''
                INSERT INTO knowledge_items (category, key, confidence, source)
                VALUES {', '.join(values_list)}
                ON CONFLICT (category, key) DO UPDATE SET
                    confidence = GREATEST(knowledge_items.confidence, EXCLUDED.confidence::real),
                    source = EXCLUDED.source
            '''
            cursor.execute(query, params)
"""


# ============================================================
# FIX 4: Quick Column Type Migration (run once)
# ============================================================

MIGRATION_SQL = """
-- Run this against Neon to fix the last_updated column type permanently.
-- This eliminates the need for ::timestamptz casts in every query.
-- 
-- IMPORTANT: Test on a backup first. If any rows have unparseable dates,
-- the ALTER will fail. Clean those rows first.

-- Step 1: Check for unparseable values
SELECT last_updated FROM facilities 
WHERE last_updated IS NOT NULL 
  AND last_updated != ''
  AND last_updated !~ '^\\d{4}-\\d{2}-\\d{2}';

-- Step 2: If Step 1 returns rows, clean them:
-- UPDATE facilities SET last_updated = NULL 
-- WHERE last_updated !~ '^\\d{4}-\\d{2}-\\d{2}';

-- Step 3: Migrate the column
ALTER TABLE facilities 
ALTER COLUMN last_updated TYPE TIMESTAMPTZ 
USING CASE 
    WHEN last_updated IS NULL OR last_updated = '' THEN NULL
    ELSE last_updated::timestamptz 
END;

-- Verify
SELECT column_name, data_type FROM information_schema.columns 
WHERE table_name = 'facilities' AND column_name = 'last_updated';
"""


# ============================================================
# SUMMARY OF ALL FIXES
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("DC Hub Railway SQL Fixes — RFO March 2, 2026")
    print("=" * 60)
    print()
    for name, fix in SQL_FIXES.items():
        print(f"📌 {name}")
        print(f"   Fixed: {fix['fixed'].strip()[:80]}...")
        if 'note' in fix:
            print(f"   Note: {fix['note']}")
        print()
    print("=" * 60)
    print("Pool limiter: PoolSafeSemaphoreSync(max_concurrent=4)")
    print("Migration: Run MIGRATION_SQL to convert last_updated to TIMESTAMPTZ")
    print("=" * 60)
