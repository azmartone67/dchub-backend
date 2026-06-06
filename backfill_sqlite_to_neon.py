"""
DC Hub - Backfill SQLite → Neon PostgreSQL
==========================================
One-time migration script. Run on Replit where the SQLite files live.

Usage:
    python backfill_sqlite_to_neon.py

Reads from:
    - ai_tracking.db (34MB) — historical AI platform request logs
    - mcp_gateway.db (24MB) — MCP gateway tracking data

Writes to Neon PostgreSQL tables:
    - ai_requests
    - ai_daily_stats
    - ai_cumulative
    - mcp_connections

Safe to run multiple times — uses ON CONFLICT to avoid duplicates.
"""

import os
import sys
import sqlite3
import time
from datetime import datetime, timezone

# ── Neon connection ────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    print("❌ Set DATABASE_URL environment variable to your Neon connection string")
    print("   Example: export DATABASE_URL='postgresql://neondb_owner:...@ep-old-waterfall-aa2rwjzs-pooler.westus3.azure.neon.tech/neondb?sslmode=require'")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing psycopg2-binary...")
    os.system("pip install psycopg2-binary --break-system-packages -q")
    import psycopg2
    import psycopg2.extras


def get_neon():
    """Get Neon PostgreSQL connection."""
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    conn.autocommit = False
    return conn


def ensure_tables(pg):
    """Create tables if they don't exist."""
    cur = pg.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_requests (
            id SERIAL PRIMARY KEY,
            platform VARCHAR(50) NOT NULL,
            endpoint VARCHAR(500),
            user_agent VARCHAR(500),
            ip_address VARCHAR(50),
            status_code INTEGER DEFAULT 200,
            response_ms INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_daily_stats (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            platform VARCHAR(50) NOT NULL,
            request_count INTEGER DEFAULT 0,
            UNIQUE(date, platform)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_cumulative (
            platform VARCHAR(50) PRIMARY KEY,
            total_requests INTEGER DEFAULT 0,
            first_seen TIMESTAMPTZ,
            last_seen TIMESTAMPTZ,
            requests_7d INTEGER DEFAULT 0,
            name VARCHAR(100),
            color VARCHAR(20),
            company VARCHAR(100)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mcp_connections (
            id SERIAL PRIMARY KEY,
            platform VARCHAR(50),
            method VARCHAR(100),
            user_agent VARCHAR(500),
            ip_address VARCHAR(50),
            tool_name VARCHAR(200),
            params TEXT,
            status_code INTEGER DEFAULT 200,
            response_ms INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_created ON ai_requests(created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_platform ON ai_requests(platform)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_daily_date ON ai_daily_stats(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_connections_created ON mcp_connections(created_at DESC)")
    pg.commit()
    print("✅ Neon tables verified/created")


def backfill_ai_tracking(pg):
    """Migrate ai_tracking.db → Neon."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_tracking.db")
    if not os.path.exists(db_path):
        print(f"⚠️  {db_path} not found — skipping ai_tracking backfill")
        return

    print(f"\n📊 Backfilling from {db_path} ({os.path.getsize(db_path) / 1024 / 1024:.1f} MB)...")

    sqlite_conn = sqlite3.connect(db_path, timeout=30)
    sqlite_conn.row_factory = sqlite3.Row

    # ── 1. Backfill ai_requests ────────────────────────────
    cur = pg.cursor()

    # Check how many rows already exist in Neon
    cur.execute("SELECT COUNT(*) FROM ai_requests")
    existing = cur.fetchone()[0]
    print(f"   Neon ai_requests currently has {existing:,} rows")

    # Count source rows
    total_source = sqlite_conn.execute("SELECT COUNT(*) FROM ai_requests").fetchone()[0]
    print(f"   SQLite ai_requests has {total_source:,} rows")

    if total_source == 0:
        print("   Nothing to backfill from ai_requests")
    else:
        BATCH = 500
        offset = 0
        inserted = 0
        skipped = 0
        start = time.time()

        while True:
            rows = sqlite_conn.execute(
                "SELECT platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at "
                "FROM ai_requests ORDER BY id LIMIT ? OFFSET ?",
                (BATCH, offset)
            ).fetchall()

            if not rows:
                break

            batch_data = []
            for r in rows:
                created = r["created_at"]
                # Normalize timestamp
                if created and "T" not in str(created):
                    created = str(created).replace(" ", "T")
                if created and "+" not in str(created) and "Z" not in str(created):
                    created = str(created) + "+00:00"

                batch_data.append((
                    r["platform"],
                    (r["endpoint"] or "")[:500],
                    (r["user_agent"] or "")[:500],
                    r["ip_address"] or "",
                    r["status_code"] or 200,
                    r["response_ms"] or 0,
                    created,
                ))

            try:
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO ai_requests (platform, endpoint, user_agent, ip_address, status_code, response_ms, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, batch_data, page_size=100)
                pg.commit()
                inserted += len(batch_data)
            except Exception as e:
                pg.rollback()
                print(f"   ⚠️  Batch at offset {offset} failed: {e}")
                skipped += len(batch_data)

            offset += BATCH
            if offset % 5000 == 0:
                elapsed = time.time() - start
                rate = inserted / elapsed if elapsed > 0 else 0
                print(f"   ... {inserted:,} inserted, {skipped:,} skipped ({rate:.0f} rows/sec)")

        elapsed = time.time() - start
        print(f"   ✅ ai_requests: {inserted:,} inserted, {skipped:,} skipped in {elapsed:.1f}s")

    # ── 2. Rebuild ai_daily_stats from Neon ai_requests ────
    print("\n   Rebuilding ai_daily_stats from Neon ai_requests...")
    cur.execute("""
        INSERT INTO ai_daily_stats (date, platform, request_count)
        SELECT created_at::date as date, platform, COUNT(*) as cnt
        FROM ai_requests
        GROUP BY created_at::date, platform
        ON CONFLICT (date, platform) DO UPDATE SET
            request_count = EXCLUDED.request_count
    """)
    pg.commit()
    cur.execute("SELECT COUNT(*) FROM ai_daily_stats")
    daily_count = cur.fetchone()[0]
    print(f"   ✅ ai_daily_stats rebuilt: {daily_count:,} rows")

    # ── 3. Rebuild ai_cumulative from Neon ai_requests ─────
    print("   Rebuilding ai_cumulative from Neon ai_requests...")

    # Platform metadata
    platforms_meta = {
        "chatgpt":    ("ChatGPT",    "#10a37f", "OpenAI"),
        "claude":     ("Claude",     "#7c3aed", "Anthropic"),
        "gemini":     ("Gemini",     "#4285f4", "Google"),
        "perplexity": ("Perplexity", "#1fb8cd", "Perplexity"),
        "copilot":    ("Copilot",    "#0078d4", "Microsoft"),
        "grok":       ("Grok",       "#1d9bf0", "xAI"),
        "deepseek":   ("DeepSeek",   "#0066ff", "DeepSeek"),
        "cursor":     ("Cursor",     "#00e5a0", "Cursor"),
        "windsurf":   ("Windsurf",   "#00d4aa", "Codeium"),
        "meta":       ("Meta AI",    "#0668E1", "Meta"),
        "cohere":     ("Cohere",     "#39594d", "Cohere"),
        "you":        ("You.com",    "#6c5ce7", "You.com"),
        "smithery":   ("Smithery",   "#f59e0b", "Smithery"),
        "mcp":        ("MCP Client", "#8b5cf6", "Various"),
    }

    cur.execute("""
        SELECT platform, COUNT(*) as total, MIN(created_at) as first_seen, MAX(created_at) as last_seen
        FROM ai_requests
        GROUP BY platform
    """)
    for row in cur.fetchall():
        platform, total, first_seen, last_seen = row
        meta = platforms_meta.get(platform, (platform.title(), "#64748b", "Unknown"))
        cur.execute("""
            INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen, name, color, company)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform) DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                first_seen = LEAST(ai_cumulative.first_seen, EXCLUDED.first_seen),
                last_seen = GREATEST(ai_cumulative.last_seen, EXCLUDED.last_seen),
                name = EXCLUDED.name,
                color = EXCLUDED.color,
                company = EXCLUDED.company
        """, (platform, total, first_seen, last_seen, meta[0], meta[1], meta[2]))
    pg.commit()
    print("   ✅ ai_cumulative rebuilt")

    sqlite_conn.close()


def backfill_mcp_gateway(pg):
    """Migrate mcp_gateway.db → Neon mcp_connections."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_gateway.db")
    if not os.path.exists(db_path):
        print(f"\n⚠️  {db_path} not found — skipping MCP gateway backfill")
        return

    print(f"\n🌐 Backfilling from {db_path} ({os.path.getsize(db_path) / 1024 / 1024:.1f} MB)...")

    sqlite_conn = sqlite3.connect(db_path, timeout=30)
    sqlite_conn.row_factory = sqlite3.Row

    # Check what tables exist
    tables = [r[0] for r in sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"   Tables found: {', '.join(tables)}")

    cur = pg.cursor()

    # Try to find request/connection logs
    for table in ["requests", "mcp_connections", "request_log", "gateway_requests"]:
        if table not in tables:
            continue

        # Get column names
        cols = [r[1] for r in sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()]
        print(f"   Processing {table} (columns: {', '.join(cols)})")

        count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"   {count:,} rows to process")

        if count == 0:
            continue

        BATCH = 500
        offset = 0
        inserted = 0

        while True:
            rows = sqlite_conn.execute(f"SELECT * FROM {table} LIMIT ? OFFSET ?", (BATCH, offset)).fetchall()
            if not rows:
                break

            batch_data = []
            for r in rows:
                rd = dict(r)
                created = rd.get("created_at") or rd.get("timestamp") or datetime.now(timezone.utc).isoformat()
                if "T" not in str(created):
                    created = str(created).replace(" ", "T")
                if "+" not in str(created) and "Z" not in str(created):
                    created = str(created) + "+00:00"

                batch_data.append((
                    rd.get("platform", "unknown"),
                    rd.get("method") or rd.get("rpc_method", ""),
                    (rd.get("user_agent", "") or "")[:500],
                    rd.get("ip_address") or rd.get("ip", ""),
                    rd.get("tool_name") or rd.get("tool", ""),
                    (rd.get("params", "") or "")[:1000],
                    rd.get("status_code", 200) or 200,
                    rd.get("response_ms") or rd.get("latency_ms", 0) or 0,
                    created,
                ))

            try:
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO mcp_connections (platform, method, user_agent, ip_address, tool_name, params, status_code, response_ms, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, batch_data, page_size=100)
                pg.commit()
                inserted += len(batch_data)
            except Exception as e:
                pg.rollback()
                print(f"   ⚠️  Batch at offset {offset} failed: {e}")

            offset += BATCH

        print(f"   ✅ {table}: {inserted:,} rows inserted into mcp_connections")

    sqlite_conn.close()


def print_summary(pg):
    """Print final counts."""
    cur = pg.cursor()
    print("\n" + "=" * 60)
    print("BACKFILL COMPLETE — Final Neon Counts:")
    print("=" * 60)

    cur.execute("SELECT COUNT(*) FROM ai_requests")
    print(f"   ai_requests:     {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM ai_daily_stats")
    print(f"   ai_daily_stats:  {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM ai_cumulative")
    print(f"   ai_cumulative:   {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM mcp_connections")
    print(f"   mcp_connections: {cur.fetchone()[0]:>10,}")

    print("\n   Top platforms by total requests:")
    cur.execute("""
        SELECT platform, total_requests, first_seen::date, last_seen::date
        FROM ai_cumulative
        WHERE platform NOT IN ('direct', 'seo_bot', 'media_crawler', 'unknown_ai')
        ORDER BY total_requests DESC LIMIT 10
    """)
    for row in cur.fetchall():
        print(f"     {row[0]:15s} {row[1]:>8,}  ({row[2]} → {row[3]})")

    print("\n   All-time total:")
    cur.execute("SELECT COALESCE(SUM(total_requests), 0) FROM ai_cumulative")
    print(f"     {cur.fetchone()[0]:,} requests tracked")


if __name__ == "__main__":
    print("=" * 60)
    print("DC Hub — SQLite → Neon PostgreSQL Backfill")
    print("=" * 60)
    print(f"Target: {DATABASE_URL[:50]}...")

    pg = get_neon()
    ensure_tables(pg)
    backfill_ai_tracking(pg)
    backfill_mcp_gateway(pg)
    print_summary(pg)
    pg.close()

    print("\n✅ Done! The AI Analytics page should now show historical data.")