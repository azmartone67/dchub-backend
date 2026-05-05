#!/usr/bin/env python3
"""
DC Hub Database Audit Script
=============================
PASTE THIS IN REPLIT SHELL to identify all code paths
still hitting local SQLite instead of Neon PostgreSQL.

This is the #1 cause of blank data after redeploys:
  - Replit redeploy wipes local filesystem
  - Code paths hitting local .db files find empty databases
  - API returns 0 results
  - Frontend shows blank

Run: python3 db_audit.py
"""

import os
import re
import sys
import json
import sqlite3
from pathlib import Path
from collections import defaultdict

# ANSI colors
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
BOLD = '\033[1m'
RESET = '\033[0m'

def header(text):
    print(f"\n{'='*60}")
    print(f"{BOLD}{text}{RESET}")
    print(f"{'='*60}")

# ============================================================
# 1. Find all .db files
# ============================================================
header("1. LOCAL DATABASE FILES FOUND")
db_files = {}
for root, dirs, files in os.walk('/home/runner/workspace'):
    # Skip node_modules, .cache, etc
    dirs[:] = [d for d in dirs if d not in ('node_modules', '.cache', '__pycache__', '.git', '.upm')]
    for f in files:
        if f.endswith('.db') or f.endswith('.sqlite'):
            path = os.path.join(root, f)
            size = os.path.getsize(path)
            db_files[f] = {'path': path, 'size': size}
            
            # Check tables
            tables = []
            try:
                conn = sqlite3.connect(path)
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]
                conn.close()
            except:
                tables = ['(error reading)']
            
            db_files[f]['tables'] = tables
            
            size_str = f"{size / 1024 / 1024:.1f}MB" if size > 1024*1024 else f"{size / 1024:.0f}KB"
            print(f"  {YELLOW}{f}{RESET} ({size_str})")
            print(f"    Path: {path}")
            print(f"    Tables: {', '.join(tables[:10])}")
            if len(tables) > 10:
                print(f"    ... and {len(tables) - 10} more tables")

# ============================================================
# 2. Find all SQLite references in Python code
# ============================================================
header("2. CODE PATHS STILL USING SQLITE (potential problems)")

sqlite_refs = []
py_files = list(Path('/home/runner/workspace').rglob('*.py'))

# Patterns that indicate SQLite usage
patterns = [
    (r"sqlite3\.connect\(['\"]([^'\"]+)['\"]", "Direct sqlite3.connect()"),
    (r"DB_PATH\s*=\s*['\"]([^'\"]+\.db)['\"]", "DB_PATH constant"),
    (r"db_path\s*=\s*['\"]([^'\"]+\.db)['\"]", "db_path variable"),
    (r"connect\(['\"]([^'\"]*\.db)['\"]", "Database connect to .db file"),
    (r"['\"]([a-zA-Z_]+\.db)['\"]", "Reference to .db filename"),
]

problem_files = defaultdict(list)

for py_file in py_files:
    try:
        content = py_file.read_text(errors='ignore')
        lines = content.split('\n')
        
        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
                
            for pattern, desc in patterns:
                matches = re.findall(pattern, line)
                for match in matches:
                    # Filter out obvious non-issues
                    if '.cache' in str(py_file) or '__pycache__' in str(py_file):
                        continue
                    
                    sqlite_refs.append({
                        'file': str(py_file).replace('/home/runner/workspace/', ''),
                        'line': i,
                        'code': stripped[:120],
                        'db': match,
                        'type': desc
                    })
                    problem_files[str(py_file).replace('/home/runner/workspace/', '')].append({
                        'line': i,
                        'db': match,
                        'type': desc,
                        'code': stripped[:120]
                    })
    except:
        pass

# Group by file and severity
critical = []
warning = []
info = []

for ref in sqlite_refs:
    # Critical: main.py or any route handler hitting SQLite
    if ref['file'] == 'main.py':
        critical.append(ref)
    elif 'route' in ref['file'] or 'endpoint' in ref['file'] or 'api' in ref['file']:
        critical.append(ref)
    elif ref['type'] in ('Direct sqlite3.connect()', 'DB_PATH constant'):
        warning.append(ref)
    else:
        info.append(ref)

if critical:
    print(f"\n  {RED}{BOLD}🔴 CRITICAL - These endpoints will break after redeploy:{RESET}")
    for ref in critical:
        print(f"    {RED}{ref['file']}:{ref['line']}{RESET}")
        print(f"      DB: {ref['db']} | Type: {ref['type']}")
        print(f"      Code: {ref['code']}")
        print()

if warning:
    print(f"\n  {YELLOW}{BOLD}⚠️  WARNING - May cause issues:{RESET}")
    for ref in warning:
        print(f"    {YELLOW}{ref['file']}:{ref['line']}{RESET}")
        print(f"      DB: {ref['db']} | Type: {ref['type']}")
        print(f"      Code: {ref['code']}")
        print()

if info:
    print(f"\n  {BLUE}ℹ️  INFO - References found (may be fine):{RESET}")
    for ref in info[:20]:  # Limit to 20
        print(f"    {ref['file']}:{ref['line']} → {ref['db']}")
    if len(info) > 20:
        print(f"    ... and {len(info) - 20} more")

if not sqlite_refs:
    print(f"  {GREEN}✅ No SQLite references found - all code uses Neon/PostgreSQL{RESET}")

# ============================================================
# 3. Check DATABASE_URL and NEON_DATABASE_URL
# ============================================================
header("3. DATABASE ENVIRONMENT VARIABLES")

db_url = os.environ.get('DATABASE_URL', '')
neon_url = os.environ.get('NEON_DATABASE_URL', '')

if neon_url:
    # Mask the password
    masked = re.sub(r':([^@]+)@', ':****@', neon_url)
    print(f"  {GREEN}✅ NEON_DATABASE_URL is set: {masked}{RESET}")
else:
    print(f"  {RED}🔴 NEON_DATABASE_URL is NOT set!{RESET}")
    print(f"     This means Neon won't be used as the database.")

if db_url:
    masked = re.sub(r':([^@]+)@', ':****@', db_url)
    if 'neon' in db_url.lower() or 'azure' in db_url.lower():
        print(f"  {GREEN}✅ DATABASE_URL points to Neon: {masked}{RESET}")
    elif 'localhost' in db_url or 'replit' in db_url:
        print(f"  {RED}🔴 DATABASE_URL points to local/Replit PG: {masked}{RESET}")
        print(f"     Should be switched to Neon connection string!")
    else:
        print(f"  {YELLOW}⚠️  DATABASE_URL: {masked}{RESET}")
else:
    print(f"  {YELLOW}⚠️  DATABASE_URL is NOT set{RESET}")

# ============================================================
# 4. Check if main.py sets DATABASE_URL to Neon at startup
# ============================================================
header("4. DATABASE_URL OVERRIDE CHECK")

main_py = Path('/home/runner/workspace/main.py')
if main_py.exists():
    content = main_py.read_text(errors='ignore')
    
    # Check for the Neon URL injection at startup
    if 'NEON_DATABASE_URL' in content and "os.environ['DATABASE_URL']" in content:
        print(f"  {GREEN}✅ main.py overrides DATABASE_URL with NEON_DATABASE_URL at startup{RESET}")
    elif 'NEON_DATABASE_URL' in content:
        print(f"  {YELLOW}⚠️  main.py references NEON_DATABASE_URL but may not override DATABASE_URL{RESET}")
    else:
        print(f"  {RED}🔴 main.py does NOT reference NEON_DATABASE_URL{RESET}")
        print(f"     Code may still use Replit's built-in PG or local SQLite!")
    
    # Check for sqlite3 import
    sqlite_import = 'import sqlite3' in content
    if sqlite_import:
        count = content.count('sqlite3.connect')
        print(f"  {YELLOW}⚠️  main.py imports sqlite3 and has {count} connect() calls{RESET}")
    
    # Count lines
    lines = content.count('\n')
    print(f"  ℹ️  main.py is {lines} lines long")
else:
    print(f"  {RED}🔴 main.py not found!{RESET}")

# ============================================================
# 5. Test Neon connection
# ============================================================
header("5. NEON DATABASE CONNECTION TEST")

try:
    import psycopg2
    
    conn_str = neon_url or db_url
    if conn_str and ('neon' in conn_str.lower() or 'azure' in conn_str.lower()):
        try:
            conn = psycopg2.connect(conn_str, connect_timeout=10)
            cursor = conn.cursor()
            
            # Get table list
            cursor.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'public' 
                ORDER BY table_name
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            print(f"  {GREEN}✅ Neon connection successful!{RESET}")
            print(f"  Tables ({len(tables)}): {', '.join(tables[:15])}")
            if len(tables) > 15:
                print(f"  ... and {len(tables) - 15} more")
            
            # Check key tables for data
            key_tables = ['facilities', 'deals', 'users', 'announcements', 
                         'news_articles', 'capacity_pipeline', 'ecosystem_companies']
            print(f"\n  {BOLD}Key table row counts:{RESET}")
            for table in key_tables:
                if table in tables:
                    try:
                        cursor.execute(f"SELECT COUNT(*) FROM {table}")
                        count = cursor.fetchone()[0]
                        color = GREEN if count > 0 else RED
                        print(f"    {color}{table}: {count:,} rows{RESET}")
                    except:
                        print(f"    {YELLOW}{table}: (error reading){RESET}")
                else:
                    print(f"    {RED}{table}: TABLE MISSING!{RESET}")
            
            conn.close()
            
        except Exception as e:
            print(f"  {RED}🔴 Neon connection FAILED: {e}{RESET}")
    else:
        print(f"  {YELLOW}⚠️  No Neon connection string found to test{RESET}")
        
except ImportError:
    print(f"  {YELLOW}⚠️  psycopg2 not installed - can't test Neon directly{RESET}")
    print(f"     Run: pip install psycopg2-binary")

# ============================================================
# 6. Check which endpoints exist and what DB they use
# ============================================================
header("6. API ENDPOINT → DATABASE MAPPING")

if main_py.exists():
    content = main_py.read_text(errors='ignore')
    lines = content.split('\n')
    
    # Find Flask route definitions
    routes = []
    current_route = None
    
    for i, line in enumerate(lines, 1):
        route_match = re.search(r'@app\.route\([\'"]([^\'"]+)[\'"]', line)
        if route_match:
            current_route = {
                'path': route_match.group(1),
                'line': i,
                'uses_sqlite': False,
                'sqlite_refs': [],
                'uses_pg': False
            }
            routes.append(current_route)
        
        if current_route and i <= current_route['line'] + 50:  # Check next 50 lines
            if 'sqlite3.connect' in line or '.db' in line:
                current_route['uses_sqlite'] = True
                current_route['sqlite_refs'].append(line.strip()[:80])
            if 'get_db_connection' in line or 'psycopg2' in line or 'DATABASE_URL' in line:
                current_route['uses_pg'] = True
    
    # Show API routes that use SQLite
    api_routes = [r for r in routes if r['path'].startswith('/api')]
    sqlite_routes = [r for r in api_routes if r['uses_sqlite']]
    
    if sqlite_routes:
        print(f"  {RED}{BOLD}🔴 API routes still using SQLite:{RESET}")
        for r in sqlite_routes:
            print(f"    {RED}{r['path']}{RESET} (line {r['line']})")
            for ref in r['sqlite_refs'][:3]:
                print(f"      → {ref}")
        print(f"\n  These routes will return EMPTY DATA after a Replit redeploy!")
    else:
        print(f"  {GREEN}✅ No API routes found using SQLite directly{RESET}")
    
    print(f"\n  Total API routes: {len(api_routes)}")
    pg_routes = [r for r in api_routes if r['uses_pg']]
    print(f"  Using PostgreSQL: {len(pg_routes)}")
    print(f"  Using SQLite: {len(sqlite_routes)}")
    print(f"  Unknown/static: {len(api_routes) - len(pg_routes) - len(sqlite_routes)}")

# ============================================================
# 7. RECOMMENDATIONS
# ============================================================
header("7. RECOMMENDED FIXES")

issues = []

if critical:
    issues.append({
        'severity': 'CRITICAL',
        'title': f'{len(critical)} code paths in main.py still use SQLite',
        'fix': 'Replace sqlite3.connect() calls with PostgreSQL connection using DATABASE_URL'
    })

if not neon_url and not ('neon' in db_url.lower() if db_url else False):
    issues.append({
        'severity': 'CRITICAL',
        'title': 'NEON_DATABASE_URL not set',
        'fix': 'Add NEON_DATABASE_URL to Replit Secrets with your Neon connection string'
    })

if db_files:
    issues.append({
        'severity': 'WARNING',
        'title': f'{len(db_files)} local SQLite files found',
        'fix': 'Verify these are not being used by any API endpoints. Remove if no longer needed.'
    })

if not issues:
    print(f"  {GREEN}{BOLD}✅ No critical issues found!{RESET}")
    print(f"  Your database configuration appears correct.")
else:
    for issue in issues:
        color = RED if issue['severity'] == 'CRITICAL' else YELLOW
        print(f"  {color}{BOLD}[{issue['severity']}] {issue['title']}{RESET}")
        print(f"    Fix: {issue['fix']}")
        print()

print(f"\n{'='*60}")
print(f"{BOLD}AUDIT COMPLETE{RESET}")
print(f"{'='*60}")
print(f"Run this after every major change to verify database hygiene.")
print(f"The #1 rule: NO API endpoint should use sqlite3.connect().")
print(f"Everything must go through Neon PostgreSQL via DATABASE_URL.")
