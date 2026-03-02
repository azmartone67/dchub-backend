"""
DC Hub — AI Agent Tracking Diagnostic
======================================
Run on Replit: python3 ai_tracking_diagnostic.py

Checks ALL tracking tables (ai_cumulative, ai_usage_tracking, ai_daily_stats)
and compares pre-fix vs post-fix traffic to measure the uptick.
"""

import os
import json
from datetime import datetime, timedelta, timezone

# ── Connect to Neon ──────────────────────────────────────────
NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')

if not NEON_URL:
    print("❌ No NEON_DATABASE_URL or DATABASE_URL found in environment")
    print("   Add your Neon connection string as a Replit secret")
    exit(1)

import psycopg2
conn = psycopg2.connect(NEON_URL)
cur = conn.cursor()

FIX_DATE = "2026-02-23"  # <-- Change this to the date you applied the fix
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

print("=" * 65)
print("  DC Hub — AI Agent Tracking Diagnostic")
print(f"  Fix date: {FIX_DATE}  |  Today: {TODAY}")
print("=" * 65)


# ── 1. ai_cumulative — Lifetime totals per platform ─────────
print("\n📊 1. LIFETIME TOTALS (ai_cumulative)")
print("-" * 50)
try:
    cur.execute("""
        SELECT platform, total_requests, 
               first_seen::text, last_seen::text
        FROM ai_cumulative 
        ORDER BY total_requests DESC
    """)
    rows = cur.fetchall()
    total_all = 0
    ai_only = 0
    print(f"{'Platform':<20} {'Requests':>10} {'Last Seen':>22}")
    print(f"{'─'*20} {'─'*10} {'─'*22}")
    for r in rows:
        platform, reqs, first, last = r
        total_all += reqs
        if platform not in ('direct', 'seo_bot', 'media_crawler', 'unknown_ai'):
            ai_only += reqs
        last_short = last[:19] if last else "—"
        print(f"{platform:<20} {reqs:>10,} {last_short:>22}")
    print(f"\n  Total all:        {total_all:>10,}")
    print(f"  AI platforms only: {ai_only:>10,}")
except Exception as e:
    print(f"  ❌ Error: {e}")


# ── 2. ai_usage_tracking — Recent individual requests ───────
print("\n\n📈 2. RECENT REQUESTS (ai_usage_tracking)")
print("-" * 50)
try:
    # Count total rows
    cur.execute("SELECT COUNT(*) FROM ai_usage_tracking")
    total_rows = cur.fetchone()[0]
    print(f"  Total rows in table: {total_rows:,}")

    # Today's requests by platform
    cur.execute("""
        SELECT platform, COUNT(*) as cnt
        FROM ai_usage_tracking
        WHERE tracked_at::date = CURRENT_DATE
        GROUP BY platform
        ORDER BY cnt DESC
    """)
    today_rows = cur.fetchall()
    if today_rows:
        print(f"\n  Today ({TODAY}):")
        for platform, cnt in today_rows:
            print(f"    {platform:<20} {cnt:>6}")
    else:
        # Try with text timestamp column
        cur.execute("""
            SELECT platform, COUNT(*) as cnt
            FROM ai_usage_tracking
            WHERE timestamp::date = CURRENT_DATE
               OR tracked_at::date = CURRENT_DATE
            GROUP BY platform
            ORDER BY cnt DESC
        """)
        today_rows = cur.fetchall()
        if today_rows:
            print(f"\n  Today ({TODAY}):")
            for platform, cnt in today_rows:
                print(f"    {platform:<20} {cnt:>6}")
        else:
            print(f"\n  ⚠️  No requests logged today")

    # Yesterday's requests
    cur.execute("""
        SELECT platform, COUNT(*) as cnt
        FROM ai_usage_tracking
        WHERE tracked_at::date = CURRENT_DATE - INTERVAL '1 day'
        GROUP BY platform
        ORDER BY cnt DESC
    """)
    yest_rows = cur.fetchall()
    if yest_rows:
        print(f"\n  Yesterday ({YESTERDAY}):")
        for platform, cnt in yest_rows:
            print(f"    {platform:<20} {cnt:>6}")
    else:
        print(f"\n  ⚠️  No requests logged yesterday")

except Exception as e:
    print(f"  ❌ Error: {e}")


# ── 3. ai_daily_stats — Daily aggregates ────────────────────
print("\n\n📅 3. DAILY STATS (ai_daily_stats)")
print("-" * 50)
try:
    cur.execute("""
        SELECT date, platform, request_count 
        FROM ai_daily_stats 
        ORDER BY date DESC, request_count DESC
        LIMIT 50
    """)
    rows = cur.fetchall()
    if rows:
        current_date = None
        for date, platform, cnt in rows:
            d = str(date)
            if d != current_date:
                current_date = d
                print(f"\n  {d}:")
            print(f"    {platform:<20} {cnt:>6}")
    else:
        print("  ⚠️  No daily stats found")
except Exception as e:
    print(f"  ❌ Error: {e}")


# ── 4. PRE-FIX vs POST-FIX comparison ───────────────────────
print("\n\n🔄 4. PRE-FIX vs POST-FIX COMPARISON")
print("-" * 50)
try:
    # Pre-fix: 7 days before fix date
    cur.execute("""
        SELECT COALESCE(SUM(request_count), 0)
        FROM ai_daily_stats
        WHERE date >= (%s::date - INTERVAL '7 days')
          AND date < %s::date
    """, (FIX_DATE, FIX_DATE))
    pre_fix = cur.fetchone()[0]

    # Post-fix: fix date to now
    cur.execute("""
        SELECT COALESCE(SUM(request_count), 0)
        FROM ai_daily_stats
        WHERE date >= %s::date
    """, (FIX_DATE,))
    post_fix = cur.fetchone()[0]

    days_pre = 7
    days_post = max(1, (datetime.now(timezone.utc).date() - datetime.strptime(FIX_DATE, "%Y-%m-%d").date()).days)

    avg_pre = pre_fix / days_pre if days_pre > 0 else 0
    avg_post = post_fix / days_post if days_post > 0 else 0

    print(f"  Pre-fix  (7d before {FIX_DATE}):  {pre_fix:>8,} total  |  {avg_pre:>8,.0f}/day avg")
    print(f"  Post-fix ({days_post}d since {FIX_DATE}):  {post_fix:>8,} total  |  {avg_post:>8,.0f}/day avg")

    if avg_pre > 0 and avg_post > 0:
        change = ((avg_post - avg_pre) / avg_pre) * 100
        emoji = "📈" if change > 0 else "📉"
        print(f"\n  {emoji} Change: {change:+.1f}% daily average")
    elif avg_post > 0:
        print(f"\n  📈 Post-fix traffic detected! (no pre-fix baseline to compare)")
    else:
        print(f"\n  ⚠️  No daily stats data — check if ai_daily_stats is being populated")

    # Also try using ai_usage_tracking directly
    print("\n  (Also checking ai_usage_tracking rows directly...)")
    cur.execute("""
        SELECT COUNT(*) FROM ai_usage_tracking
        WHERE tracked_at >= (%s::date - INTERVAL '7 days')
          AND tracked_at < %s::date
    """, (FIX_DATE, FIX_DATE))
    pre_rows = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM ai_usage_tracking
        WHERE tracked_at >= %s::date
    """, (FIX_DATE,))
    post_rows = cur.fetchone()[0]

    print(f"  Pre-fix rows:  {pre_rows:>8,}")
    print(f"  Post-fix rows: {post_rows:>8,}")

except Exception as e:
    print(f"  ❌ Error: {e}")


# ── 5. MCP-specific traffic ─────────────────────────────────
print("\n\n🔌 5. MCP ENDPOINT TRAFFIC")
print("-" * 50)
try:
    cur.execute("""
        SELECT platform, COUNT(*) as cnt,
               MAX(tracked_at)::text as last_hit
        FROM ai_usage_tracking
        WHERE endpoint LIKE '%%/mcp%%'
           OR endpoint LIKE '%%.well-known%%'
           OR endpoint LIKE '%%llms%%'
        GROUP BY platform
        ORDER BY cnt DESC
    """)
    rows = cur.fetchall()
    if rows:
        for platform, cnt, last in rows:
            last_short = last[:19] if last else "—"
            print(f"  {platform:<20} {cnt:>6}  (last: {last_short})")
    else:
        print("  ⚠️  No MCP/discovery endpoint hits found")
        print("  Checking for any endpoint data...")
        cur.execute("""
            SELECT DISTINCT endpoint FROM ai_usage_tracking LIMIT 20
        """)
        eps = cur.fetchall()
        if eps:
            print("  Available endpoints:")
            for ep in eps:
                print(f"    {ep[0]}")
        else:
            print("  No endpoint data recorded at all")
except Exception as e:
    print(f"  ❌ Error: {e}")


# ── 6. Last 24h activity feed ────────────────────────────────
print("\n\n🕐 6. LAST 24h ACTIVITY (most recent 25)")
print("-" * 50)
try:
    cur.execute("""
        SELECT platform, endpoint, tracked_at::text
        FROM ai_usage_tracking
        WHERE tracked_at >= NOW() - INTERVAL '24 hours'
        ORDER BY tracked_at DESC
        LIMIT 25
    """)
    rows = cur.fetchall()
    if rows:
        for platform, endpoint, ts in rows:
            ts_short = ts[:19] if ts else "—"
            ep_short = (endpoint[:35] + "...") if endpoint and len(endpoint) > 38 else (endpoint or "—")
            print(f"  {ts_short}  {platform:<15} {ep_short}")
        print(f"\n  ({len(rows)} shown of last 24h)")
    else:
        print("  ⚠️  No activity in last 24 hours")
        cur.execute("""
            SELECT MAX(tracked_at)::text FROM ai_usage_tracking
        """)
        last = cur.fetchone()[0]
        print(f"  Last recorded activity: {last or 'never'}")
except Exception as e:
    print(f"  ❌ Error: {e}")


# ── 7. Table inventory ───────────────────────────────────────
print("\n\n📋 7. TRACKING TABLE INVENTORY")
print("-" * 50)
for table in ['ai_cumulative', 'ai_usage_tracking', 'ai_daily_stats', 'ai_requests']:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        cnt = cur.fetchone()[0]
        print(f"  ✅ {table:<25} {cnt:>8,} rows")
    except Exception:
        conn.rollback()
        print(f"  ❌ {table:<25} NOT FOUND")

conn.close()

print("\n" + "=" * 65)
print("  Diagnostic complete!")
print("  Fix date set to:", FIX_DATE)
print("  Update FIX_DATE variable if your fix was on a different day")
print("=" * 65)
