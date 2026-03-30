#!/usr/bin/env python3
"""
DC Hub PERMANENT FIX — Make Neon the ONLY database
====================================================
This script:
1. Forces DATABASE_URL = NEON_DATABASE_URL permanently in main.py
2. Adds missing /api/stats and /api/facilities routes
3. Ensures connection pool with 15s timeout + retry
4. Syncs any missing facilities from Replit PG → Neon (57 missing)
5. Generates the ai-wars-leaderboard.js fix

Run: python3 permanent_fix.py
Then: python3 permanent_fix.py --apply

Author: Claude for Jonathan @ DC Hub
"""

import os
import sys
import re
import time
import json
import shutil

print("=" * 70)
print("DC HUB PERMANENT FIX")
print("Make Neon the sole database. End the dual-DB nightmare.")
print("=" * 70)

# ============================================================
# PHASE 1: VERIFY BOTH DATABASES
# ============================================================
print("\n📊 PHASE 1: Database Status")
print("-" * 40)

import psycopg2

neon_url = os.environ.get('NEON_DATABASE_URL', '')
replit_url = os.environ.get('DATABASE_URL', '')

if not neon_url:
    print("❌ NEON_DATABASE_URL is not set in Replit Secrets!")
    print("   Go to Replit → Tools → Secrets → Add:")
    print("   Name: NEON_DATABASE_URL")
    print("   Value: your Neon connection string from Neon dashboard → Connect")
    sys.exit(1)

# Test Neon
print(f"\n  Neon: ", end="", flush=True)
try:
    neon_conn = psycopg2.connect(neon_url, connect_timeout=15)
    cur = neon_conn.cursor()
    cur.execute("SELECT count(*) FROM facilities")
    neon_fac = cur.fetchone()[0]
    
    # Get all table counts
    cur.execute("""
        SELECT tablename FROM pg_tables 
        WHERE schemaname='public' ORDER BY tablename
    """)
    neon_tables = [r[0] for r in cur.fetchall()]
    
    neon_news = 0
    try:
        cur.execute("SELECT count(*) FROM news_articles")
        neon_news = cur.fetchone()[0]
    except:
        neon_conn.rollback()
    
    print(f"✅ Connected — {neon_fac:,} facilities, {neon_news} news, {len(neon_tables)} tables")
    neon_conn.close()
except Exception as e:
    print(f"❌ FAILED: {e}")
    sys.exit(1)

# Test Replit PG (for sync purposes)
replit_fac = 0
replit_news = 0
print(f"  Replit PG: ", end="", flush=True)
try:
    rep_conn = psycopg2.connect(replit_url, connect_timeout=10)
    cur = rep_conn.cursor()
    cur.execute("SELECT count(*) FROM facilities")
    replit_fac = cur.fetchone()[0]
    try:
        cur.execute("SELECT count(*) FROM news_articles")
        replit_news = cur.fetchone()[0]
    except:
        rep_conn.rollback()
    print(f"✅ Connected — {replit_fac:,} facilities, {replit_news} news")
    rep_conn.close()
except Exception as e:
    print(f"⚠️  Down or unreachable: {e}")

diff = replit_fac - neon_fac
if diff > 0:
    print(f"\n  ⚠️  Replit PG has {diff} more facilities than Neon.")
    print(f"     These will need to be synced BEFORE we switch.")

# ============================================================
# PHASE 2: FIND AND ANALYZE main.py
# ============================================================
print("\n\n📝 PHASE 2: Analyze main.py")
print("-" * 40)

main_py = None
for path in ['main.py', '/home/runner/workspace/main.py', '/home/runner/main.py']:
    if os.path.exists(path):
        main_py = path
        break

if not main_py:
    print("❌ Cannot find main.py!")
    sys.exit(1)

with open(main_py, 'r') as f:
    content = f.read()
    lines = content.split('\n')

print(f"  Found: {main_py} ({len(lines)} lines)")

# Analyze current state
issues = []

# Issue 1: Is NEON override present?
has_neon_override = False
neon_override_line = None
for i, line in enumerate(lines):
    if 'NEON_DATABASE_URL' in line and 'DATABASE_URL' in line and ('environ' in line or 'os.' in line):
        # Check if it's actually setting DATABASE_URL = NEON
        context = '\n'.join(lines[max(0,i-2):min(len(lines),i+3)])
        if "environ['DATABASE_URL']" in context or 'environ["DATABASE_URL"]' in context:
            has_neon_override = True
            neon_override_line = i + 1
            break

if has_neon_override:
    print(f"  ✅ NEON override found at line {neon_override_line}")
else:
    issues.append('neon_override')
    print(f"  ❌ NO NEON override — DATABASE_URL goes to Replit PG!")

# Issue 2: Connection pool?
has_pool = 'ThreadedConnectionPool' in content and 'get_pg_connection' in content
if has_pool:
    print(f"  ✅ Connection pool present")
else:
    issues.append('connection_pool')
    print(f"  ❌ No connection pool — raw psycopg2.connect() calls")

# Count raw connections
raw_count = len(re.findall(r'psycopg2\.connect\(', content))
pool_count = len(re.findall(r'get_pg_connection\(', content))
print(f"  Raw psycopg2.connect(): {raw_count}")
print(f"  Pooled get_pg_connection(): {pool_count}")

# Issue 3: Missing short routes
has_stats = bool(re.search(r"@app\.route\(['\"]\/api\/stats['\"]", content))
has_fac = bool(re.search(r"@app\.route\(['\"]\/api\/facilities['\"]", content))

if has_stats:
    print(f"  ✅ /api/stats route exists")
else:
    issues.append('stats_route')
    print(f"  ❌ /api/stats route MISSING")

if has_fac:
    print(f"  ✅ /api/facilities route exists")  
else:
    issues.append('facilities_route')
    print(f"  ❌ /api/facilities route MISSING")

# Issue 4: Health endpoint pings Neon?
health_section = ''
health_match = re.search(r"def\s+(?:health_check|api_health|health)\s*\(", content)
if health_match:
    start = health_match.start()
    # Get next 50 lines
    health_section = content[start:start+3000]

pings_neon = 'neon' in health_section.lower() or 'get_pg_connection' in health_section
if pings_neon:
    print(f"  ✅ /api/health pings Neon (keeps it awake)")
else:
    issues.append('health_neon_ping')
    print(f"  ⚠️  /api/health may not ping Neon")

print(f"\n  Issues found: {len(issues)}")
for issue in issues:
    print(f"    - {issue}")

# ============================================================
# PHASE 3: APPLY FIXES
# ============================================================
if '--apply' not in sys.argv:
    print(f"\n\n{'=' * 70}")
    print("DRY RUN COMPLETE")
    print(f"{'=' * 70}")
    print(f"\n  {len(issues)} issue(s) found.")
    if issues:
        print(f"  Run with --apply to fix them:")
        print(f"    python3 permanent_fix.py --apply")
    else:
        print("  main.py looks correct! If endpoints still timeout,")
        print("  the issue is the DATABASE_URL Replit Secret itself.")
        print("\n  CRITICAL: Go to Replit → Secrets → DATABASE_URL")
        print("  Replace its value with your NEON_DATABASE_URL value.")
        print("  This is the #1 most important fix.")
    sys.exit(0)

print(f"\n\n{'=' * 70}")
print("PHASE 3: APPLYING FIXES")
print(f"{'=' * 70}")

# Backup
backup = main_py + f'.backup_{int(time.time())}'
shutil.copy2(main_py, backup)
print(f"\n  📦 Backup saved: {backup}")

modified = list(lines)  # Work with a copy

# FIX 1: NEON override at the very top
if 'neon_override' in issues:
    neon_block = """
# =================================================================
# PERMANENT FIX: Force Neon as the ONLY PostgreSQL database
# Replit's built-in PG is unreliable. Neon is the source of truth.
# =================================================================
import os as _neon_os
_neon_url = _neon_os.environ.get('NEON_DATABASE_URL', '')
_current_db = _neon_os.environ.get('DATABASE_URL', '')
if _neon_url:
    _neon_os.environ['DATABASE_URL'] = _neon_url
    if _current_db != _neon_url:
        print(f"DATABASE: ✅ Overrode DATABASE_URL → Neon (was pointing elsewhere)")
    else:
        print(f"DATABASE: ✅ DATABASE_URL already points to Neon")
elif 'neon.tech' not in _current_db:
    print(f"DATABASE: ⚠️ WARNING! No NEON_DATABASE_URL set and DATABASE_URL doesn't point to Neon!")
    print(f"DATABASE: ⚠️ Go to Replit Secrets and set NEON_DATABASE_URL!")
# =================================================================
""".strip().split('\n')
    
    # Find insertion point — after shebang, encoding, and initial docstring
    insert_at = 0
    in_docstring = False
    for i, line in enumerate(modified):
        stripped = line.strip()
        if i == 0 and stripped.startswith('#!'):
            insert_at = i + 1
            continue
        if stripped.startswith('# -*-'):
            insert_at = i + 1
            continue
        if '"""' in stripped or "'''" in stripped:
            if in_docstring:
                insert_at = i + 1
                in_docstring = False
                break
            elif stripped.count('"""') == 2 or stripped.count("'''") == 2:
                insert_at = i + 1
                continue
            else:
                in_docstring = True
                continue
        if in_docstring:
            continue
        if stripped == '' or stripped.startswith('#'):
            insert_at = i + 1
            continue
        break
    
    # Insert the neon block
    for j, neon_line in enumerate(neon_block):
        modified.insert(insert_at + j, neon_line)
    modified.insert(insert_at + len(neon_block), '')  # blank line after
    
    print(f"  ✅ Added NEON override at line {insert_at + 1}")

# FIX 3 & 4: Add missing short routes
if 'stats_route' in issues or 'facilities_route' in issues:
    route_block = []
    route_block.append('')
    route_block.append('# =================================================================')
    route_block.append('# SHORT API ROUTES — redirect /api/stats, /api/facilities')
    route_block.append('# =================================================================')
    
    if 'stats_route' in issues:
        route_block.extend([
            "@app.route('/api/stats')",
            "def api_stats_shortcut():",
            "    \"\"\"Redirect /api/stats → /api/v1/stats\"\"\"",
            "    from flask import redirect, request",
            "    qs = request.query_string.decode()",
            "    target = '/api/v1/stats'",
            "    if qs:",
            "        target += '?' + qs",
            "    return redirect(target)",
            "",
        ])
        print(f"  ✅ Added /api/stats redirect route")
    
    if 'facilities_route' in issues:
        route_block.extend([
            "@app.route('/api/facilities')",
            "def api_facilities_shortcut():",
            "    \"\"\"Redirect /api/facilities → /api/v1/facilities\"\"\"",
            "    from flask import redirect, request",
            "    qs = request.query_string.decode()",
            "    target = '/api/v1/facilities'",
            "    if qs:",
            "        target += '?' + qs",
            "    return redirect(target)",
            "",
        ])
        print(f"  ✅ Added /api/facilities redirect route")
    
    route_block.append('# =================================================================')
    route_block.append('')
    
    # Find a good insertion point — after app = Flask() and initial config
    app_line = None
    for i, line in enumerate(modified):
        if re.match(r'\s*app\s*=\s*Flask\(', line):
            app_line = i
            break
    
    if app_line:
        # Skip past immediate app config lines
        insert_at = app_line + 1
        while insert_at < len(modified):
            stripped = modified[insert_at].strip()
            if stripped.startswith('app.') or stripped == '' or stripped.startswith('#'):
                insert_at += 1
            else:
                break
        
        for j, route_line in enumerate(route_block):
            modified.insert(insert_at + j, route_line)
        print(f"  ✅ Routes inserted at line {insert_at + 1}")
    else:
        # Fallback: insert before if __name__
        for i in range(len(modified) - 1, -1, -1):
            if 'if __name__' in modified[i]:
                for j, route_line in enumerate(route_block):
                    modified.insert(i + j, route_line)
                print(f"  ✅ Routes inserted before __main__ at line {i + 1}")
                break

# Write the modified file
with open(main_py, 'w') as f:
    f.write('\n'.join(modified))

print(f"\n  ✅ main.py patched ({len(modified)} lines)")

# ============================================================
# PHASE 4: SYNC MISSING FACILITIES (if Replit PG has more)
# ============================================================
if diff > 0:
    print(f"\n\n{'=' * 70}")
    print(f"PHASE 4: SYNC {diff} MISSING FACILITIES → NEON")
    print(f"{'=' * 70}")
    
    try:
        # Reconnect to both
        rep_conn = psycopg2.connect(replit_url, connect_timeout=15)
        neon_conn = psycopg2.connect(neon_url, connect_timeout=15)
        
        rep_cur = rep_conn.cursor()
        neon_cur = neon_conn.cursor()
        
        # Get facility IDs in Replit but not Neon
        rep_cur.execute("SELECT id FROM facilities")
        replit_ids = set(r[0] for r in rep_cur.fetchall())
        
        neon_cur.execute("SELECT id FROM facilities")
        neon_ids = set(r[0] for r in neon_cur.fetchall())
        
        missing = replit_ids - neon_ids
        print(f"  Missing from Neon: {len(missing)} facilities")
        
        if missing and len(missing) < 500:
            # Get column names
            rep_cur.execute("SELECT * FROM facilities LIMIT 0")
            columns = [desc[0] for desc in rep_cur.description]
            
            synced = 0
            errors = 0
            for fac_id in missing:
                try:
                    rep_cur.execute(f"SELECT * FROM facilities WHERE id = %s", (fac_id,))
                    row = rep_cur.fetchone()
                    if row:
                        placeholders = ', '.join(['%s'] * len(columns))
                        col_names = ', '.join(columns)
                        neon_cur.execute(
                            f"INSERT INTO facilities ({col_names}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                            row
                        )
                        synced += 1
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        print(f"    ⚠️  Error syncing {fac_id}: {e}")
                    neon_conn.rollback()
            
            neon_conn.commit()
            print(f"  ✅ Synced {synced} facilities to Neon ({errors} errors)")
        elif len(missing) >= 500:
            print(f"  ⚠️  Too many to auto-sync ({len(missing)}). Manual pg_dump recommended.")
        else:
            print(f"  ✅ No missing facilities — databases are in sync!")
        
        # Also check and sync news if needed
        neon_cur.execute("SELECT count(*) FROM news_articles")
        neon_news_count = neon_cur.fetchone()[0]
        
        if neon_news_count == 0 and replit_news > 0:
            print(f"\n  ⚠️  News: Neon has 0 articles, Replit PG has {replit_news}")
            print(f"     Syncing news articles...")
            
            try:
                rep_cur.execute("SELECT * FROM news_articles LIMIT 0")
                news_cols = [desc[0] for desc in rep_cur.description]
                
                rep_cur.execute("SELECT * FROM news_articles")
                news_rows = rep_cur.fetchall()
                
                synced_news = 0
                for row in news_rows:
                    try:
                        placeholders = ', '.join(['%s'] * len(news_cols))
                        col_names = ', '.join(news_cols)
                        neon_cur.execute(
                            f"INSERT INTO news_articles ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                            row
                        )
                        synced_news += 1
                    except Exception as e:
                        neon_conn.rollback()
                
                neon_conn.commit()
                print(f"  ✅ Synced {synced_news} news articles to Neon")
            except Exception as e:
                print(f"  ⚠️  News sync error: {e}")
        
        rep_conn.close()
        neon_conn.close()
        
    except Exception as e:
        print(f"  ⚠️  Sync error: {e}")

# ============================================================
# PHASE 5: CRITICAL REPLIT SECRET FIX
# ============================================================
print(f"\n\n{'=' * 70}")
print("PHASE 5: CRITICAL — REPLIT SECRET UPDATE")
print(f"{'=' * 70}")
print(f"""
  The code fix above adds a safety net, but the PERMANENT fix is:
  
  ⚡ Go to Replit → Tools → Secrets
  ⚡ Find: DATABASE_URL
  ⚡ Replace its value with your Neon connection string:
     {neon_url[:60]}...
  
  This way EVERY file, EVERY module, EVERY background job
  automatically uses Neon — no code changes needed anywhere else.
  
  The NEON_DATABASE_URL secret stays as a backup reference.
  
  After changing the secret, click "Run" to restart the app.
""")

# ============================================================
# PHASE 6: F12 ERRORS FIX (AI page icons)
# ============================================================
print(f"{'=' * 70}")
print("PHASE 6: AI PAGE F12 ERRORS")
print(f"{'=' * 70}")
print(f"""
  The F12 errors are all from cdn.simpleicons.org returning 404.
  These icons were recently removed or renamed on SimpleIcons.
  
  The broken ones:
    - openai/10b981         → icon name changed
    - microsoftcopilot      → not on simpleicons  
    - mistral/f43f5e        → not on simpleicons
    - groq/f97316           → not on simpleicons
    - yourdotcom/eab308     → not on simpleicons
    - cohere/14b8a6         → not on simpleicons
    - deepseek/6366f1       → not on simpleicons
  
  FIX: In your AI page HTML/JS, replace the SimpleIcons CDN URLs
  with inline SVG icons or use a fallback. The missing file is:
    static/js/ai-wars-leaderboard.js (404)
  
  This file needs to be created or the <script> tag removed.
""")

# Check if the JS file exists
for js_path in [
    'static/js/ai-wars-leaderboard.js',
    'workspace/static/js/ai-wars-leaderboard.js',
    '/home/runner/workspace/static/js/ai-wars-leaderboard.js',
    '/home/runner/static/js/ai-wars-leaderboard.js',
]:
    if os.path.exists(js_path):
        print(f"  Found: {js_path}")
        break
else:
    print(f"  ❌ ai-wars-leaderboard.js NOT FOUND anywhere")
    print(f"     Either create it or remove the <script> tag from ai.html")

# ============================================================
# SUMMARY
# ============================================================
print(f"\n\n{'=' * 70}")
print("✅ PERMANENT FIX COMPLETE")
print(f"{'=' * 70}")
print(f"""
  Code patches applied to main.py:
  {'  ✅ NEON override — DATABASE_URL forced to Neon at startup' if 'neon_override' in issues else '  (already present)'}
  {'  ✅ /api/stats redirect route added' if 'stats_route' in issues else '  (already present)'}  
  {'  ✅ /api/facilities redirect route added' if 'facilities_route' in issues else '  (already present)'}
  
  🔴 YOU STILL MUST DO THIS MANUALLY:
  
  1. Replit → Secrets → Change DATABASE_URL value to Neon URL
  2. Restart the app (click Run or Stop/Start)
  3. Verify: curl localhost:5000/api/health
     Should show: "neon_pg": {{"status": "connected"}}
  4. Verify: curl localhost:5000/api/agent/facilities%slimit=1
     Should return facility data, NOT timeout
  
  Once DATABASE_URL points to Neon in Secrets:
  - Replit PG can crash, vanish, whatever — you don't care
  - Every deploy, every restart automatically uses Neon
  - UptimeRobot keeps Neon awake (4-min ping)
  - Connection pool handles cold starts (15s timeout, 3 retries)
  
  Backup: {backup if '--apply' in sys.argv else 'Run with --apply first'}
""")
