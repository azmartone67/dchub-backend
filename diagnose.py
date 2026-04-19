#!/usr/bin/env python3
"""DC Hub Health Diagnostic - Run from Railway shell"""
import os, time, json, sys

try:
    import psycopg2
except ImportError:
    os.system("pip install psycopg2-binary -q --break-system-packages")
    import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DCHUB_API_BASE = os.environ.get("DCHUB_API_BASE", "NOT SET")

print("=" * 60)
print("DC HUB DIAGNOSTIC")
print("=" * 60)

# 1. Check critical env vars
print("\n📋 ENVIRONMENT")
print(f"  DCHUB_API_BASE = {DCHUB_API_BASE}")
print(f"  DATABASE_URL set = {bool(DATABASE_URL)}")
print(f"  RAILWAY_ENVIRONMENT = {os.environ.get('RAILWAY_ENVIRONMENT', 'NOT SET')}")

if DCHUB_API_BASE == "http://127.0.0.1:8080" or "127.0.0.1" in str(DCHUB_API_BASE):
    print("  🚨 DCHUB_API_BASE is localhost — DEADLOCK RISK! Must be Railway URL")

# 2. DB connection & slow query check
print("\n📊 DATABASE DIAGNOSTICS")
try:
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    cur = conn.cursor()
    
    # Check active connections
    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
    active = cur.fetchone()[0]
    print(f"  Active DB connections: {active}")
    
    cur.execute("SELECT count(*) FROM pg_stat_activity")
    total = cur.fetchone()[0]
    print(f"  Total DB connections: {total}")
    
    # Check for long-running queries
    cur.execute("""
        SELECT pid, now() - pg_stat_activity.query_start AS duration, 
               left(query, 80) as query_preview, state
        FROM pg_stat_activity 
        WHERE state != 'idle' 
          AND query NOT LIKE '%pg_stat_activity%'
          AND now() - pg_stat_activity.query_start > interval '5 seconds'
        ORDER BY duration DESC
        LIMIT 5
    """)
    long_queries = cur.fetchall()
    if long_queries:
        print(f"  🚨 {len(long_queries)} LONG-RUNNING QUERIES (>5s):")
        for pid, dur, q, state in long_queries:
            print(f"    PID {pid} [{state}] {dur}: {q}")
    else:
        print("  ✅ No long-running queries")
    
    # Test the slow endpoints' underlying queries
    slow_tests = [
        ("facilities count", "SELECT count(*) FROM facilities"),
        ("transactions count", "SELECT count(*) FROM deals"),
        ("agent_registry count", "SELECT count(*) FROM agent_registry" ),
        ("map facilities", "SELECT id, name, latitude, longitude FROM facilities WHERE latitude IS NOT NULL LIMIT 100"),
        ("ai_tracking cumulative", "SELECT count(*) FROM ai_platform_tracking"),
    ]
    
    for name, query in slow_tests:
        start = time.time()
        try:
            cur.execute(query)
            result = cur.fetchone()
            elapsed = (time.time() - start) * 1000
            status = "🚨 SLOW" if elapsed > 5000 else "⚠️" if elapsed > 2000 else "✅"
            print(f"  {status} {name}: {result[0]} rows [{elapsed:.0f}ms]")
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            err_str = str(e).split('\n')[0][:80]
            print(f"  ❌ {name}: {err_str} [{elapsed:.0f}ms]")
    
    # Check table sizes
    print("\n📏 TABLE SIZES (top 10)")
    cur.execute("""
        SELECT relname, n_live_tup 
        FROM pg_stat_user_tables 
        ORDER BY n_live_tup DESC 
        LIMIT 10
    """)
    for table, count in cur.fetchall():
        print(f"  {table}: {count:,} rows")
    
    # Check for missing indexes on big tables
    print("\n🔍 SEQUENTIAL SCANS ON LARGE TABLES")
    cur.execute("""
        SELECT relname, seq_scan, idx_scan, n_live_tup
        FROM pg_stat_user_tables 
        WHERE n_live_tup > 1000 
          AND seq_scan > idx_scan * 2
        ORDER BY seq_scan DESC
        LIMIT 5
    """)
    scan_issues = cur.fetchall()
    if scan_issues:
        for table, seq, idx, rows in scan_issues:
            print(f"  ⚠️ {table}: {seq} seq scans vs {idx} idx scans ({rows:,} rows)")
    else:
        print("  ✅ No major sequential scan issues")
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f"  ❌ DB connection failed: {e}")

# 3. Check MCP proxy routing
print("\n🔌 MCP PROXY CHECK")
try:
    import urllib.request
    # Internal health check
    start = time.time()
    req = urllib.request.Request("http://127.0.0.1:8080/health")
    resp = urllib.request.urlopen(req, timeout=10)
    elapsed = (time.time() - start) * 1000
    print(f"  ✅ Local /health: {resp.status} [{elapsed:.0f}ms]")
except Exception as e:
    print(f"  ❌ Local /health: {str(e)[:80]}")

try:
    start = time.time()
    req = urllib.request.Request("http://127.0.0.1:8080/mcp")
    req.add_header("Content-Type", "application/json")
    data = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}).encode()
    resp = urllib.request.urlopen(req, data, timeout=10)
    elapsed = (time.time() - start) * 1000
    body = resp.read().decode()[:200]
    print(f"  ✅ Local /mcp tools/list: {resp.status} [{elapsed:.0f}ms]")
    print(f"     Response: {body}")
except Exception as e:
    print(f"  ❌ Local /mcp: {str(e)[:80]}")

# 4. Check if the /mcp route is using X-Internal-Key bypass
print("\n🔐 MCP TIER GATING STATUS")
print("  Check main.py around line 2728 for /mcp proxy route")
print("  If X-Internal-Key is forwarded on all MCP calls, tier gating is BYPASSED")

# 5. Worker version check via internal
print("\n🔧 SUMMARY")
print("  If DCHUB_API_BASE = 127.0.0.1 → Fix to Railway URL and redeploy")
print("  If long-running queries exist → Need query optimization or indexes")
print("  If MCP proxy fails locally → Backend /mcp route is broken")
print("  If Worker returns 503 on /health → Worker health route is proxying to slow Railway endpoint")
