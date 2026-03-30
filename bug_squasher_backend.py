#!/usr/bin/env python3
"""
DC Hub Bug Squasher — Backend Fixes
=====================================
Run this in Railway shell or Replit to apply all P0/P1 bug fixes.

Usage:
  python3 bug_squasher_backend.py --check     # Dry run: show what needs fixing
  python3 bug_squasher_backend.py --fix       # Apply all fixes
  python3 bug_squasher_backend.py --fix-one BUG-003   # Fix specific bug

Bugs addressed:
  BUG-003  P1  429 rate limit for pro users (api_data_protection.py + rate_limiter.py)
  BUG-005  P1  validate_api_key() returns None via PGConnectionWrapper
  BUG-006  P1  get_facility MCP returns 0 (missing id field)
  BUG-016  P1  get_pipeline min_capacity_mw filter after tier limit
"""

import os
import sys
import re
import subprocess
from datetime import datetime

# ─── Configuration ──────────────────────────────────────────────
FIXES = {}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-003: 429 rate limit for pro users on map
# Root cause: api_data_protection.py and rate_limiter.py both have
# per-IP rate tracking that doesn't check dchub.cloud Origin.
# Fix: Add Origin bypass for dchub.cloud requests.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BUG003_PATCH_PROTECTION = '''
# ──── BUG-003 FIX: dchub.cloud Origin bypass ────
# Added by Bug Squasher {date}
def _is_first_party_request(request):
    """Check if request comes from dchub.cloud frontend (bypass rate limits)."""
    origin = request.headers.get('Origin', '') or request.headers.get('Referer', '')
    return 'dchub.cloud' in origin

'''

BUG003_SEARCH_PROTECTION = 'def protect_data('
BUG003_INSERT_AFTER_PROTECTION = '''    # BUG-003 FIX: Skip rate limiting for dchub.cloud frontend requests
    if _is_first_party_request(request):
        return None  # No rate limit for first-party requests
'''

BUG003_SEARCH_RATELIMITER = 'def check_rate_limit('  # or similar entry point
BUG003_INSERT_AFTER_RATELIMITER = '''    # BUG-003 FIX: Skip rate limiting for dchub.cloud frontend requests
    origin = request.headers.get('Origin', '') or request.headers.get('Referer', '')
    if 'dchub.cloud' in origin:
        return None  # No rate limit for first-party requests
'''


def fix_bug003(check_only=False):
    """Fix 429 rate limits for pro users on dchub.cloud."""
    results = []
    
    # Fix api_data_protection.py
    for path in ['api_data_protection.py', 'routes/api_data_protection.py']:
        if os.path.exists(path):
            with open(path, 'r') as f:
                content = f.read()
            
            if '_is_first_party_request' in content:
                results.append(f"  ✅ {path}: Already patched")
                continue
            
            if 'def protect_data(' in content:
                if check_only:
                    results.append(f"  🔧 {path}: Needs Origin bypass in protect_data()")
                else:
                    # Add helper function at top of file (after imports)
                    patch = BUG003_PATCH_PROTECTION.format(date=datetime.now().strftime('%Y-%m-%d'))
                    
                    # Insert bypass at start of protect_data function
                    lines = content.split('\n')
                    new_lines = []
                    inserted_helper = False
                    inserted_bypass = False
                    
                    for i, line in enumerate(lines):
                        new_lines.append(line)
                        
                        # Insert helper function after last import
                        if not inserted_helper and (line.startswith('import ') or line.startswith('from ')):
                            # Check if next line is NOT an import
                            if i + 1 < len(lines) and not (lines[i+1].startswith('import ') or lines[i+1].startswith('from ') or lines[i+1].strip() == ''):
                                new_lines.append(patch)
                                inserted_helper = True
                        
                        # Insert bypass at start of protect_data body
                        if not inserted_bypass and 'def protect_data(' in line:
                            # Find the next non-empty, non-docstring line
                            j = i + 1
                            while j < len(lines) and (lines[j].strip() == '' or lines[j].strip().startswith('"""') or lines[j].strip().startswith("'''")):
                                j += 1
                            # We'll insert after the function def line
                            new_lines.append(BUG003_INSERT_AFTER_PROTECTION)
                            inserted_bypass = True
                    
                    with open(path, 'w') as f:
                        f.write('\n'.join(new_lines))
                    results.append(f"  ✅ {path}: Patched with Origin bypass")
    
    # Fix rate_limiter.py
    for path in ['rate_limiter.py', 'routes/rate_limiter.py']:
        if os.path.exists(path):
            with open(path, 'r') as f:
                content = f.read()
            
            if "'dchub.cloud' in origin" in content and 'BUG-003' in content:
                results.append(f"  ✅ {path}: Already patched")
                continue
            
            if check_only:
                results.append(f"  🔧 {path}: Needs Origin bypass")
            else:
                # Find the main rate limit check function and add bypass
                lines = content.split('\n')
                new_lines = []
                patched = False
                
                for i, line in enumerate(lines):
                    new_lines.append(line)
                    # Look for the rate limit enforcement function
                    if not patched and ('def check_rate_limit(' in line or 'def enforce_rate_limit(' in line or 'def enforce_tier_rate_limits(' in line):
                        new_lines.append(BUG003_INSERT_AFTER_RATELIMITER)
                        patched = True
                
                if patched:
                    with open(path, 'w') as f:
                        f.write('\n'.join(new_lines))
                    results.append(f"  ✅ {path}: Patched with Origin bypass")
                else:
                    results.append(f"  ⚠️  {path}: Could not find rate limit function to patch")
    
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-005: validate_api_key() returns None via PGConnectionWrapper
# Root cause: db_utils wrapper doesn't return values properly
# Fix: Use direct psycopg2 connection in validate_api_key()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BUG005_REPLACEMENT = '''
def validate_api_key(api_key):
    """Validate API key and return user info. BUG-005 FIX: Direct psycopg2 connection."""
    if not api_key:
        return None
    
    import psycopg2
    conn = None
    try:
        database_url = os.environ.get('DATABASE_URL') or os.environ.get('DATABASE_READ_URL')
        if not database_url:
            print("[BUG-005] No DATABASE_URL found")
            return None
        
        conn = psycopg2.connect(database_url, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.email, u.plan, u.role, ak.rate_limit_tier
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key = %s AND ak.is_active = true
            LIMIT 1
        """, (api_key,))
        row = cur.fetchone()
        cur.close()
        
        if row:
            return {
                'user_id': row[0],
                'email': row[1],
                'plan': row[2],
                'role': row[3],
                'rate_limit_tier': row[4] or row[2]  # fallback to plan
            }
        return None
    except Exception as e:
        print(f"[BUG-005] validate_api_key error: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass
'''


def fix_bug005(check_only=False):
    """Fix validate_api_key() returning None via PGConnectionWrapper."""
    results = []
    
    for path in ['api_tier_gating.py', 'routes/api_tier_gating.py']:
        if not os.path.exists(path):
            continue
        
        with open(path, 'r') as f:
            content = f.read()
        
        if 'BUG-005 FIX' in content:
            results.append(f"  ✅ {path}: Already patched")
            continue
        
        if 'def validate_api_key(' not in content:
            results.append(f"  ⚠️  {path}: No validate_api_key function found")
            continue
        
        if check_only:
            results.append(f"  🔧 {path}: validate_api_key needs direct psycopg2 bypass")
            continue
        
        # Replace the entire validate_api_key function
        # Find the function and its end
        pattern = r'def validate_api_key\([^)]*\):.*%s(%s=\ndef |\nclass |\Z)'
        match = re.search(pattern, content, re.DOTALL)
        
        if match:
            content = content[:match.start()] + BUG005_REPLACEMENT + '\n' + content[match.end():]
            
            # Ensure os import exists
            if 'import os' not in content:
                content = 'import os\n' + content
            
            with open(path, 'w') as f:
                f.write(content)
            results.append(f"  ✅ {path}: Replaced validate_api_key with direct psycopg2")
        else:
            results.append(f"  ⚠️  {path}: Could not match validate_api_key function boundary")
    
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-006: get_facility MCP returns 0 results
# Root cause: search_facilities response missing 'id' field
# Fix: Add 'id' to the SELECT and response mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fix_bug006(check_only=False):
    """Fix get_facility returning 0 results by adding id to search_facilities."""
    results = []
    
    for path in ['main.py', 'dchub_mcp_server.py']:
        if not os.path.exists(path):
            continue
        
        with open(path, 'r') as f:
            content = f.read()
        
        # Check if search_facilities query already includes id
        # Look for the SQL query in search_facilities handler
        if 'search_facilities' not in content:
            continue
        
        # Pattern: SELECT that doesn't include f.id or facilities.id
        # This is tricky because the query structure varies
        # Let's look for the response mapping instead
        
        has_id_in_response = bool(re.search(r"['\"]id['\"]:\s*(?:row|r|facility)\[", content))
        has_id_in_select = bool(re.search(r"SELECT.*%s\bf\.id\b.*%sFROM.*%sfacilities", content, re.DOTALL | re.IGNORECASE))
        
        if has_id_in_response and has_id_in_select:
            results.append(f"  ✅ {path}: search_facilities already includes id field")
            continue
        
        if check_only:
            if not has_id_in_select:
                results.append(f"  🔧 {path}: search_facilities SQL needs f.id in SELECT")
            if not has_id_in_response:
                results.append(f"  🔧 {path}: search_facilities response needs 'id' key")
            continue
        
        # We need to be surgical here — this requires manual review
        results.append(f"  ⚠️  {path}: Needs manual fix — add f.id to search_facilities SELECT")
        results.append(f"       and 'id': row[0] to the response dict.")
        results.append(f"       Also check get_facility — its ID lookup query may need fixing.")
    
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-016: get_pipeline min_capacity_mw filter applied after tier limit
# Root cause: SQL LIMIT applied before WHERE min_capacity_mw filter
# Fix: Move capacity filter into SQL WHERE clause
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fix_bug016(check_only=False):
    """Fix get_pipeline capacity filter ordering."""
    results = []
    
    for path in ['main.py', 'dchub_mcp_server.py']:
        if not os.path.exists(path):
            continue
        
        with open(path, 'r') as f:
            content = f.read()
        
        if 'get_pipeline' not in content:
            continue
        
        # Look for post-query filtering pattern
        # Common antipattern: fetch results with LIMIT, then filter in Python
        post_filter = re.search(
            r'min_capacity.*%s=.*%sarguments.*%sget.*%smin_capacity.*%s\n.*%s(%s:results|data|rows).*%s=.*%s\[.*%sfor.*%sif.*%scapacity.*%s>',
            content, re.DOTALL
        )
        
        if post_filter:
            if check_only:
                results.append(f"  🔧 {path}: get_pipeline has post-query capacity filter (needs SQL WHERE)")
            else:
                results.append(f"  ⚠️  {path}: Needs manual fix — move min_capacity_mw into SQL WHERE clause")
                results.append(f"       Before: SELECT ... LIMIT n → then Python filter")
                results.append(f"       After:  SELECT ... WHERE capacity_mw >= %s LIMIT n")
        else:
            # Check if it's already in the SQL
            sql_filter = re.search(r"capacity_mw\s*>=\s*%s", content)
            if sql_filter:
                results.append(f"  ✅ {path}: get_pipeline already has SQL-level capacity filter")
            else:
                if check_only:
                    results.append(f"  🔧 {path}: get_pipeline may need capacity filter in SQL")
                else:
                    results.append(f"  ⚠️  {path}: Review get_pipeline — add WHERE capacity_mw >= %s")
    
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-013: layers.transmission undefined in land-power-app.js
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fix_bug013(check_only=False):
    """Fix layers.transmission undefined error."""
    results = []
    
    path = 'js/land-power-app.js'
    if not os.path.exists(path):
        # Try from the zip extraction
        path = 'land-power-app.js'
    if not os.path.exists(path):
        results.append(f"  ⚠️  land-power-app.js: Not found in current directory")
        return results
    
    with open(path, 'r') as f:
        content = f.read()
    
    # Check for the initialization
    if 'layers.transmission = ' in content or "layers['transmission']" in content:
        results.append(f"  ✅ {path}: layers.transmission already initialized")
        return results
    
    if 'loadDCHubTransmissionLines' in content:
        if check_only:
            results.append(f"  🔧 {path}: layers.transmission not initialized before addLayer call")
        else:
            # Find where other layers are initialized and add transmission
            # Look for pattern like: layers.powerPlants = or var layers = {
            match = re.search(r'(layers\.\w+\s*=\s*(?:L\.layerGroup|null|undefined)\(\))', content)
            if match:
                insert_point = match.end()
                fix = "\nlayers.transmission = L.layerGroup(); // BUG-013 FIX\n"
                content = content[:insert_point] + fix + content[insert_point:]
                with open(path, 'w') as f:
                    f.write(content)
                results.append(f"  ✅ {path}: Added layers.transmission initialization")
            else:
                results.append(f"  ⚠️  {path}: Could not find layer initialization pattern")
    
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALL_FIXES = {
    'BUG-003': ('P1', '429 rate limit for pro users', fix_bug003),
    'BUG-005': ('P1', 'validate_api_key() returns None', fix_bug005),
    'BUG-006': ('P1', 'get_facility MCP returns 0', fix_bug006),
    'BUG-013': ('P2', 'layers.transmission undefined', fix_bug013),
    'BUG-016': ('P1', 'get_pipeline capacity filter', fix_bug016),
}


def main():
    args = sys.argv[1:]
    
    if not args or '--help' in args:
        print(__doc__)
        return
    
    check_only = '--check' in args
    fix_one = None
    if '--fix-one' in args:
        idx = args.index('--fix-one')
        if idx + 1 < len(args):
            fix_one = args[idx + 1].upper()
    
    print("=" * 60)
    print("🪲 DC Hub Bug Squasher — Backend Fixes")
    print(f"   Mode: {'CHECK (dry run)' if check_only else 'FIX (applying changes)'}")
    print(f"   Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   CWD:  {os.getcwd()}")
    print("=" * 60)
    
    for bug_id, (severity, title, fix_fn) in ALL_FIXES.items():
        if fix_one and bug_id != fix_one:
            continue
        
        print(f"\n{'🔴' if severity == 'P0' else '🟡'} [{bug_id}] {severity} — {title}")
        
        try:
            results = fix_fn(check_only=check_only)
            for r in results:
                print(r)
        except Exception as e:
            print(f"  ❌ Error: {e}")
    
    print("\n" + "=" * 60)
    if check_only:
        print("Run with --fix to apply changes")
    else:
        print("Done! Remember to: git add . && git commit -m 'Bug Squasher fixes' && git push")
    print("=" * 60)


if __name__ == '__main__':
    main()
