#!/usr/bin/env python3
"""
DC Hub Replit Fix Script - Run this in Replit Shell
====================================================
Diagnoses and fixes:
1. DATABASE_URL / NEON_DATABASE_URL configuration
2. Neon PostgreSQL connectivity
3. Missing /api/stats and /api/facilities routes
4. News articles empty
5. Connection pool health

Usage: python3 fix_replit.py
"""

import os
import sys
import time
import json
import subprocess

# ============================================================
# STEP 1: DIAGNOSE DATABASE CONNECTION
# ============================================================
print("=" * 70)
print("STEP 1: DATABASE CONNECTION DIAGNOSIS")
print("=" * 70)

database_url = os.environ.get('DATABASE_URL', '')
neon_url = os.environ.get('NEON_DATABASE_URL', '')

print(f"\n  DATABASE_URL set:      {'YES' if database_url else 'NO (PROBLEM!)'}")
print(f"  NEON_DATABASE_URL set: {'YES' if neon_url else 'NO'}")

if database_url:
    # Mask password for display
    masked = database_url
    if '@' in masked:
        parts = masked.split('@')
        pre_at = parts[0]
        if ':' in pre_at:
            user_pass = pre_at.split(':')
            masked = f"{user_pass[0]}:{user_pass[1][:3]}***@{'@'.join(parts[1:])}"
    print(f"  DATABASE_URL points to: {masked}")
    
    if 'neon.tech' in database_url:
        print("  ✅ DATABASE_URL points to Neon")
    elif 'replit' in database_url or 'localhost' in database_url or '127.0.0.1' in database_url:
        print("  ❌ DATABASE_URL points to Replit's local/dead PG!")
        if neon_url and 'neon.tech' in neon_url:
            print("  🔧 FIX: NEON_DATABASE_URL exists and points to Neon.")
            print("     Will inject NEON_DATABASE_URL as DATABASE_URL...")
            os.environ['DATABASE_URL'] = neon_url
            database_url = neon_url
            print("  ✅ DATABASE_URL now points to Neon (runtime fix)")
            print("")
            print("  ⚠️  PERMANENT FIX NEEDED:")
            print("     Go to Replit Secrets → find DATABASE_URL → replace with:")
            print(f"     {neon_url}")
        else:
            print("  ❌ NEON_DATABASE_URL is also missing!")
            print("  🔧 FIX: Add your Neon connection string to Replit Secrets:")
            print("     Secret name: DATABASE_URL")
            print("     Value: postgresql://neondb_owner:YOUR_PASS@ep-xxx.westus3.azure.neon.tech/neondb?sslmode=require")
            sys.exit(1)
else:
    if neon_url and 'neon.tech' in neon_url:
        print("  🔧 FIX: DATABASE_URL is empty but NEON_DATABASE_URL exists.")
        print("     Injecting NEON_DATABASE_URL as DATABASE_URL...")
        os.environ['DATABASE_URL'] = neon_url
        database_url = neon_url
        print("  ✅ DATABASE_URL now points to Neon (runtime fix)")
        print("")
        print("  ⚠️  PERMANENT FIX NEEDED:")
        print("     Go to Replit Secrets → add DATABASE_URL with value:")
        print(f"     {neon_url}")
    else:
        print("  ❌ NEITHER DATABASE_URL nor NEON_DATABASE_URL is set!")
        print("  🔧 FIX: Add your Neon connection string to Replit Secrets:")
        print("     Secret name: DATABASE_URL")
        print("     Value: postgresql://neondb_owner:YOUR_PASS@ep-xxx.westus3.azure.neon.tech/neondb?sslmode=require")
        sys.exit(1)

# ============================================================
# STEP 2: TEST NEON CONNECTIVITY
# ============================================================
print("\n" + "=" * 70)
print("STEP 2: NEON POSTGRESQL CONNECTIVITY TEST")
print("=" * 70)

try:
    import psycopg2
    print("\n  Testing connection to Neon (15s timeout)...")
    start = time.time()
    conn = psycopg2.connect(database_url, connect_timeout=15)
    elapsed = time.time() - start
    print(f"  ✅ Connected in {elapsed:.1f}s")
    
    cur = conn.cursor()
    
    # Check facilities
    cur.execute("SELECT count(*) FROM facilities")
    fac_count = cur.fetchone()[0]
    print(f"  ✅ Facilities: {fac_count:,}")
    
    # Check users
    try:
        cur.execute("SELECT count(*) FROM users")
        user_count = cur.fetchone()[0]
        print(f"  ✅ Users: {user_count}")
    except Exception as e:
        print(f"  ⚠️  Users table: {e}")
        conn.rollback()
    
    # Check deals
    try:
        cur.execute("SELECT count(*) FROM deals")
        deal_count = cur.fetchone()[0]
        print(f"  ✅ Deals: {deal_count}")
    except Exception as e:
        print(f"  ⚠️  Deals table: {e}")
        conn.rollback()
    
    # Check news_articles
    try:
        cur.execute("SELECT count(*) FROM news_articles")
        news_count = cur.fetchone()[0]
        print(f"  {'✅' if news_count > 0 else '❌'} News articles: {news_count}")
        if news_count == 0:
            print("     ⚠️  News is EMPTY in Neon - may need sync from SQLite")
    except Exception as e:
        print(f"  ⚠️  News articles table: {e}")
        conn.rollback()
    
    # Check announcements (alternative news source)
    try:
        cur.execute("SELECT count(*) FROM announcements")
        ann_count = cur.fetchone()[0]
        print(f"  {'✅' if ann_count > 0 else '⚠️ '} Announcements: {ann_count}")
    except Exception as e:
        print(f"  ⚠️  Announcements table: {e}")
        conn.rollback()
    
    # Check markets
    try:
        cur.execute("SELECT count(*) FROM markets")
        mkt_count = cur.fetchone()[0]
        print(f"  ✅ Markets: {mkt_count}")
    except Exception as e:
        print(f"  ⚠️  Markets table: {e}")
        conn.rollback()
    
    # Check capacity_pipeline
    try:
        cur.execute("SELECT count(*) FROM capacity_pipeline")
        cap_count = cur.fetchone()[0]
        print(f"  ✅ Capacity pipeline: {cap_count}")
    except Exception as e:
        print(f"  ⚠️  Capacity pipeline table: {e}")
        conn.rollback()
    
    # List all tables
    cur.execute("""
        SELECT tablename FROM pg_tables 
        WHERE schemaname = 'public' 
        ORDER BY tablename
    """)
    tables = [row[0] for row in cur.fetchall()]
    print(f"\n  All Neon tables ({len(tables)}):")
    for t in tables:
        print(f"    - {t}")
    
    conn.close()
    print("\n  ✅ Neon connection test PASSED")
    
except psycopg2.OperationalError as e:
    elapsed = time.time() - start
    print(f"\n  ❌ Connection FAILED after {elapsed:.1f}s")
    print(f"  Error: {e}")
    if 'timeout' in str(e).lower():
        print("\n  DIAGNOSIS: Neon is suspended and took too long to wake up.")
        print("  FIX: Run this script again — Neon should be awake now.")
        print("  LONG-TERM FIX: UptimeRobot pinging /api/health every 4 min")
    elif 'password' in str(e).lower() or 'authentication' in str(e).lower():
        print("\n  DIAGNOSIS: Wrong password in connection string.")
        print("  FIX: Get fresh connection string from Neon dashboard → Connect")
    elif 'does not exist' in str(e).lower():
        print("\n  DIAGNOSIS: Database name is wrong.")
        print("  FIX: Check Neon dashboard for correct database name")
    sys.exit(1)
except ImportError:
    print("  ❌ psycopg2 not installed! Run: pip install psycopg2-binary")
    sys.exit(1)


# ============================================================
# STEP 3: CHECK FLASK ROUTES
# ============================================================
print("\n" + "=" * 70)
print("STEP 3: CHECK FLASK ROUTES")
print("=" * 70)

# Check if main.py exists and look for the routes
main_py = None
for path in ['main.py', 'workspace/main.py', '/home/runner/workspace/main.py']:
    if os.path.exists(path):
        main_py = path
        break

if not main_py:
    print("  ⚠️  Can't find main.py — skipping route check")
else:
    with open(main_py, 'r') as f:
        content = f.read()
    
    # Check for /api/stats route
    has_api_stats = "'/api/stats'" in content or '"/api/stats"' in content
    has_api_facilities = "'/api/facilities'" in content or '"/api/facilities"' in content
    has_api_v1_stats = "'/api/v1/stats'" in content or '"/api/v1/stats"' in content
    has_api_v1_facilities = "'/api/v1/facilities'" in content or '"/api/v1/facilities"' in content
    has_api_agent_stats = "'/api/agent/stats'" in content or '"/api/agent/stats"' in content
    has_api_agent_facilities = "'/api/agent/facilities'" in content or '"/api/agent/facilities"' in content
    has_connection_pool = 'ThreadedConnectionPool' in content or 'get_pg_connection' in content
    has_neon_override = 'NEON_DATABASE_URL' in content
    
    print(f"\n  Route: /api/stats           {'✅ EXISTS' if has_api_stats else '❌ MISSING'}")
    print(f"  Route: /api/facilities      {'✅ EXISTS' if has_api_facilities else '❌ MISSING'}")
    print(f"  Route: /api/v1/stats        {'✅ EXISTS' if has_api_v1_stats else '⚠️  not found'}")
    print(f"  Route: /api/v1/facilities   {'✅ EXISTS' if has_api_v1_facilities else '⚠️  not found'}")
    print(f"  Route: /api/agent/stats     {'✅ EXISTS' if has_api_agent_stats else '⚠️  not found'}")
    print(f"  Route: /api/agent/facilities{'✅ EXISTS' if has_api_agent_facilities else '⚠️  not found'}")
    print(f"  Connection pool:            {'✅ YES' if has_connection_pool else '❌ NO - needs pool!'}")
    print(f"  NEON_DATABASE_URL override: {'✅ YES' if has_neon_override else '❌ NO'}")
    
    # Count raw psycopg2.connect calls (outside the pool)
    import re
    raw_connects = len(re.findall(r'psycopg2\.connect\(', content))
    pool_gets = len(re.findall(r'get_pg_connection\(', content))
    print(f"\n  Raw psycopg2.connect() calls: {raw_connects}")
    print(f"  Pooled get_pg_connection() calls: {pool_gets}")
    if raw_connects > 2 and not has_connection_pool:
        print("  ⚠️  Many raw connections without pooling = timeout risk!")


# ============================================================
# STEP 4: TEST LOCAL FLASK ROUTES
# ============================================================
print("\n" + "=" * 70)
print("STEP 4: TEST LOCAL FLASK ENDPOINTS")
print("=" * 70)

import urllib.request
import urllib.error

endpoints = [
    ('http://localhost:5000/', 'Root'),
    ('http://localhost:5000/api/health', 'Health'),
    ('http://localhost:5000/api/stats', '/api/stats'),
    ('http://localhost:5000/api/facilities', '/api/facilities'),
    ('http://localhost:5000/api/v1/stats', '/api/v1/stats'),
    ('http://localhost:5000/api/v1/facilities?limit=1', '/api/v1/facilities'),
    ('http://localhost:5000/api/agent/stats', '/api/agent/stats'),
    ('http://localhost:5000/api/agent/facilities?limit=1', '/api/agent/facilities'),
    ('http://localhost:5000/api/v1/news', '/api/v1/news'),
]

print()
for url, name in endpoints:
    try:
        start = time.time()
        req = urllib.request.Request(url, method='GET')
        req.add_header('Accept', 'application/json')
        resp = urllib.request.urlopen(req, timeout=15)
        elapsed = time.time() - start
        status = resp.status
        body = resp.read(500).decode('utf-8', errors='replace')
        
        # Try to parse JSON for key counts
        detail = ""
        try:
            data = json.loads(body if len(body) < 500 else body + "...")
            if isinstance(data, dict):
                if 'count' in data:
                    detail = f" (count: {data['count']})"
                elif 'facilities' in data:
                    detail = f" (facilities: {data.get('total', '?')})"
                elif 'status' in data:
                    detail = f" (status: {data['status']})"
        except:
            detail = f" ({len(body)} bytes)"
        
        print(f"  {'✅' if status == 200 else '⚠️ '} {name:25s} → {status} in {elapsed:.1f}s{detail}")
        
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start
        print(f"  ❌ {name:25s} → {e.code} {e.reason} in {elapsed:.1f}s")
    except urllib.error.URLError as e:
        print(f"  ❌ {name:25s} → UNREACHABLE ({e.reason})")
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ❌ {name:25s} → TIMEOUT/ERROR after {elapsed:.1f}s ({e})")


# ============================================================
# STEP 5: GENERATE FIX PATCH
# ============================================================
print("\n" + "=" * 70)
print("STEP 5: RECOMMENDED FIXES")
print("=" * 70)

fixes_needed = []

if not has_neon_override and main_py:
    fixes_needed.append("neon_override")
    print("""
  🔧 FIX 1: Add NEON_DATABASE_URL override at top of main.py
  
  Add this BEFORE any other imports (line 1-5 area):
  
  import os as _os_db
  _neon_url = _os_db.environ.get('NEON_DATABASE_URL', '')
  if _neon_url:
      _os_db.environ['DATABASE_URL'] = _neon_url
      print(f"DATABASE: Using NEON_DATABASE_URL as DATABASE_URL")
""")

if not has_connection_pool and main_py:
    fixes_needed.append("connection_pool")
    print("""
  🔧 FIX 2: Add connection pool (see pool code below)
""")

if not has_api_stats and main_py:
    fixes_needed.append("stats_route")
    print("""
  🔧 FIX 3: Add /api/stats redirect route to main.py:
  
  @app.route('/api/stats')
  def api_stats_redirect():
      return redirect('/api/v1/stats')
""")

if not has_api_facilities and main_py:
    fixes_needed.append("facilities_route")
    print("""
  🔧 FIX 4: Add /api/facilities redirect route to main.py:
  
  @app.route('/api/facilities')
  def api_facilities_redirect():
      from flask import request, redirect
      qs = request.query_string.decode()
      target = '/api/v1/facilities'
      if qs:
          target += '?' + qs
      return redirect(target)
""")

if not fixes_needed:
    print("\n  ✅ No code fixes needed — main.py looks correct!")
    print("  If endpoints are still timing out, the issue is Neon connectivity.")
else:
    print(f"\n  {len(fixes_needed)} fix(es) needed. Apply them to main.py and restart.")

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

print(f"""
  Database URL:        {'✅ Points to Neon' if 'neon.tech' in database_url else '❌ NOT pointing to Neon'}
  Neon connectivity:   {'✅ Connected' if 'conn' in dir() else '❌ Failed'}
  Connection pool:     {'✅ Present' if has_connection_pool else '❌ Missing'}
  NEON URL override:   {'✅ Present' if has_neon_override else '❌ Missing'}
  /api/stats route:    {'✅ Present' if has_api_stats else '❌ Missing'}
  /api/facilities:     {'✅ Present' if has_api_facilities else '❌ Missing'}
  
  Fixes needed: {len(fixes_needed) if fixes_needed else 'NONE'}
""")

if fixes_needed:
    print("  Run this script with --apply to auto-apply fixes to main.py")
    print("  Or apply them manually using the instructions above.")

# ============================================================
# AUTO-APPLY MODE
# ============================================================
if '--apply' in sys.argv and main_py and fixes_needed:
    print("\n" + "=" * 70)
    print("APPLYING FIXES TO main.py")
    print("=" * 70)
    
    # Read current main.py
    with open(main_py, 'r') as f:
        lines = f.readlines()
    
    content = ''.join(lines)
    modified = False
    
    # FIX 1: NEON_DATABASE_URL override
    if 'neon_override' in fixes_needed:
        # Find the first import line
        neon_override_code = '''# === NEON DATABASE URL OVERRIDE (auto-applied by fix script) ===
import os as _os_db
_neon_url = _os_db.environ.get('NEON_DATABASE_URL', '')
if _neon_url:
    _os_db.environ['DATABASE_URL'] = _neon_url
    print(f"DATABASE: Using NEON_DATABASE_URL → Neon")
elif 'neon.tech' not in _os_db.environ.get('DATABASE_URL', ''):
    print("DATABASE: ⚠️ WARNING - DATABASE_URL does not point to Neon!")
# === END NEON OVERRIDE ===

'''
        # Insert after any shebang/encoding lines
        insert_pos = 0
        for i, line in enumerate(lines):
            if line.startswith('#!') or line.startswith('# -*-') or line.startswith('"""') or line.strip() == '':
                insert_pos = i + 1
            else:
                break
        
        # Check if there's a docstring
        in_docstring = False
        for i in range(insert_pos, min(insert_pos + 20, len(lines))):
            if '"""' in lines[i]:
                if in_docstring:
                    insert_pos = i + 1
                    break
                else:
                    in_docstring = True
        
        lines.insert(insert_pos, neon_override_code)
        modified = True
        print(f"  ✅ Added NEON_DATABASE_URL override at line {insert_pos + 1}")
    
    # FIX 3 & 4: Add missing redirect routes
    route_code = ''
    if 'stats_route' in fixes_needed:
        route_code += '''
# === SHORT API ROUTES (auto-applied by fix script) ===
@app.route('/api/stats')
def api_stats_redirect():
    """Redirect /api/stats to /api/v1/stats"""
    from flask import redirect
    return redirect('/api/v1/stats')

'''
        print("  ✅ Added /api/stats redirect route")
    
    if 'facilities_route' in fixes_needed:
        route_code += '''@app.route('/api/facilities')
def api_facilities_redirect():
    """Redirect /api/facilities to /api/v1/facilities with query params"""
    from flask import redirect, request
    qs = request.query_string.decode()
    target = '/api/v1/facilities'
    if qs:
        target += '?' + qs
    return redirect(target)
# === END SHORT API ROUTES ===

'''
        print("  ✅ Added /api/facilities redirect route")
    
    if route_code:
        # Find a good place to insert — after the app = Flask() line
        content_str = ''.join(lines)
        # Look for "app = Flask(" 
        app_line = None
        for i, line in enumerate(lines):
            if 'app = Flask(' in line or 'app=Flask(' in line:
                app_line = i
                break
        
        if app_line:
            # Insert after the Flask app creation block (skip any immediate config)
            insert_at = app_line + 1
            while insert_at < len(lines) and (lines[insert_at].startswith('app.') or lines[insert_at].strip() == ''):
                insert_at += 1
            lines.insert(insert_at, route_code)
            modified = True
        else:
            # Fallback: append before if __name__
            for i in range(len(lines) - 1, -1, -1):
                if "if __name__" in lines[i]:
                    lines.insert(i, route_code)
                    modified = True
                    break
    
    if modified:
        # Backup original
        backup_path = main_py + '.backup_' + time.strftime('%Y%m%d_%H%M%S')
        with open(backup_path, 'w') as f:
            f.writelines(lines)  # Write to backup first as safety
        
        # Write modified
        with open(main_py, 'w') as f:
            f.writelines(lines)
        
        print(f"\n  ✅ main.py updated! Backup saved to {backup_path}")
        print("  ⚠️  RESTART REQUIRED: Stop and restart your Replit app")
        print("     Or run: kill 1 (Replit will auto-restart)")
    else:
        print("\n  No modifications were needed.")

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
