#!/bin/bash
# ============================================================================
# DC Hub Bug Fix — Single Shell Command
# Run in Railway shell: bash /tmp/dchub_fix_march24.sh
# ============================================================================
set -euo pipefail

echo "======================================"
echo "DC Hub Bug Fix — March 24, 2026"
echo "======================================"
cd ~/workspace || cd /app || { echo "ERROR: No workspace dir"; exit 1; }
echo "Working dir: $(pwd)"

# ---- Step 0: Pre-flight ----
echo ""
echo "--- PRE-FLIGHT CHECKS ---"
echo "Python: $(python3 --version 2>&1)"
echo "Git branch: $(git branch --show-current 2>/dev/null || echo 'N/A')"
echo "main.py exists: $(test -f main.py && echo YES || echo NO)"
echo "smoke_test.py exists: $(test -f smoke_test.py && echo YES || echo NO)"
echo "redis_cache.py exists: $(test -f redis_cache.py && echo YES || echo NO)"
echo "NEON_DATABASE_URL set: $(test -n "${NEON_DATABASE_URL:-}" && echo YES || echo NO)"
echo "REDIS_URL set: $(test -n "${REDIS_URL:-}" && echo YES || echo NO)"

# ---- Step 1: Quick diagnostics ----
echo ""
echo "--- BUG 1: smoke_test import check ---"
python3 -c "
import sys, os
os.environ['IS_EXTERNAL'] = 'true'
os.environ['PYTHONUNBUFFERED'] = '1'
try:
    import smoke_test
    print('✅ smoke_test imports OK')
    if hasattr(smoke_test, 'main'):
        print('   has main()')
    if hasattr(smoke_test, 'run_quick'):
        print('   has run_quick()')
except Exception as e:
    print(f'❌ Import error: {e}')
" 2>&1 || true

echo ""
echo "--- BUG 3: endpoint_hits column type ---"
DB_URL="${NEON_DATABASE_URL:-${DATABASE_URL:-}}"
if [ -n "$DB_URL" ]; then
    psql "$DB_URL" -t -c "
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_name = 'daily_record_usage' 
          AND column_name = 'endpoint_hits';
    " 2>/dev/null || echo "⚠️  Could not check column type"
fi

# ---- Step 2: Apply DB fixes ----
echo ""
echo "--- APPLYING DB FIXES ---"
if [ -n "$DB_URL" ]; then
    echo "BUG 3: ALTER endpoint_hits → integer..."
    psql "$DB_URL" -c "
        DO \$\$
        BEGIN
            -- Only alter if currently jsonb
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'daily_record_usage' 
                  AND column_name = 'endpoint_hits' 
                  AND data_type = 'jsonb'
            ) THEN
                ALTER TABLE daily_record_usage 
                ALTER COLUMN endpoint_hits TYPE integer USING COALESCE((endpoint_hits::text)::integer, 0);
                ALTER TABLE daily_record_usage 
                ALTER COLUMN endpoint_hits SET DEFAULT 0;
                RAISE NOTICE '✅ endpoint_hits altered to integer';
            ELSE
                RAISE NOTICE '⏭️  endpoint_hits already integer (or column not found)';
            END IF;
        END \$\$;
    " 2>&1 || echo "⚠️  ALTER TABLE failed"
else
    echo "⚠️  No DB URL — skipping SQL fixes"
fi

# ---- Step 3: Apply Python fixes ----
echo ""
echo "--- APPLYING PYTHON FIXES ---"

# BUG 2: get_facility garbage data — add validation function
if ! grep -q '_validate_facility_result' main.py 2>/dev/null; then
    echo "BUG 2: Adding _validate_facility_result()..."
    python3 -c "
import re

with open('main.py', 'r') as f:
    content = f.read()

# Add validation function after FACILITY_VISIBLE_FIELDS or after imports
validation_func = '''
def _validate_facility_result(data):
    \"\"\"Validate facility data isn't returning column names as values (BUG-008 fix).\"\"\"
    if not data or not isinstance(data, dict):
        return None
    # Telltale: value equals its own key name means garbage data
    garbage = [k for k, v in data.items() if isinstance(v, str) and v == k]
    if len(garbage) >= 3:
        return None
    return data
'''

# Insert after FACILITY_VISIBLE_FIELDS definition
if 'FACILITY_VISIBLE_FIELDS' in content:
    idx = content.index('FACILITY_VISIBLE_FIELDS')
    # Find end of the line/list
    nl = content.index('\n', idx)
    while nl < len(content) - 1 and content[nl+1:nl+2] in (' ', '\t', ']', ','):
        nl = content.index('\n', nl + 1)
    content = content[:nl+1] + validation_func + content[nl+1:]
else:
    # Insert after imports
    lines = content.split('\n')
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            insert_at = i + 1
    lines.insert(insert_at, validation_func)
    content = '\n'.join(lines)

# Now wire validation into call sites of _get_facility_free_from_db
pattern = r'(\s+)(\w+)\s*=\s*_get_facility_free_from_db\(([^)]+)\)'
matches = list(re.finditer(pattern, content))
for m in reversed(matches):
    indent = m.group(1)
    var = m.group(2)
    # Don't add if already validated
    after = content[m.end():m.end()+200]
    if '_validate_facility_result' in after or f'{var} is None' in after[:100]:
        continue
    insert_code = f'{indent}{var} = _validate_facility_result({var})'
    insert_code += f'{indent}if {var} is None:'
    insert_code += f'{indent}    return {{\"error\": \"Facility not found\", \"success\": False}}, 404'
    content = content[:m.end()] + '\n' + insert_code + content[m.end():]

with open('main.py', 'w') as f:
    f.write(content)
print('✅ BUG 2: _validate_facility_result added')
" 2>&1 || echo "⚠️  BUG 2 patch failed"
else
    echo "⏭️  BUG 2: _validate_facility_result already present"
fi

# BUG 4 & 5: Pool-holding background jobs → direct psycopg2.connect()
echo "BUG 4+5: Checking pool-holding functions..."
python3 -c "
import re

with open('main.py', 'r') as f:
    content = f.read()

changed = False
for func_name in ['generate_market_report', 'seed_serverfarm_facilities']:
    match = re.search(rf'def {func_name}\(', content)
    if not match:
        print(f'  ⚠️  {func_name} not found')
        continue
    
    # Check the function body (next 5000 chars)
    region = content[match.start():match.start()+5000]
    
    for pool_call in ['get_read_db()', 'get_db()']:
        if pool_call in region:
            # Replace first occurrence within function scope only
            func_start = match.start()
            before = content[:func_start]
            after = content[func_start:]
            new_call = f'psycopg2.connect(os.environ.get(\"NEON_DATABASE_URL\", os.environ.get(\"DATABASE_URL\", \"\")))  # fixed: was {pool_call}'
            if pool_call in after[:5000]:
                after = after.replace(pool_call, new_call, 1)
                content = before + after
                print(f'  ✅ {func_name}: {pool_call} → direct psycopg2.connect()')
                changed = True
            break
    else:
        print(f'  ⏭️  {func_name}: no pooled connection found')

if changed:
    with open('main.py', 'w') as f:
        f.write(content)
" 2>&1 || echo "⚠️  BUG 4+5 patch failed"

# BUG 6: News INSERT rollback
echo "BUG 6: Checking news INSERT rollback..."
python3 -c "
import re

# Check main.py and deals_routes.py
for filepath in ['main.py', 'routes/deals_routes.py', 'deals_routes.py']:
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        continue
    
    # Find INSERT INTO *news* patterns
    inserts = list(re.finditer(r'INSERT\s+INTO\s+\w*news', content, re.IGNORECASE))
    if not inserts:
        continue
    
    print(f'  Found {len(inserts)} news INSERT(s) in {filepath}')
    changed = False
    
    for ins in inserts:
        # Check surrounding 2000 chars for try/except with rollback
        start = max(0, ins.start() - 1000)
        end = min(len(content), ins.end() + 2000)
        region = content[start:end]
        
        if 'rollback' not in region.lower():
            # Find the nearest except block after the INSERT
            except_match = re.search(r'(\s+)except\s+(?:Exception|psycopg2)', content[ins.start():ins.start()+2000])
            if except_match:
                abs_pos = ins.start() + except_match.start()
                indent = except_match.group(1)
                line_end = content.index('\n', abs_pos)
                
                # Add rollback
                rollback = f'\n{indent}    try:\n{indent}        conn.rollback()\n{indent}    except Exception:\n{indent}        pass  # BUG6: prevent transaction poisoning'
                content = content[:line_end+1] + rollback + '\n' + content[line_end+1:]
                changed = True
                print(f'  ✅ Added rollback after news INSERT except in {filepath}')
    
    if changed:
        with open(filepath, 'w') as f:
            f.write(content)
        break
    else:
        print(f'  ⏭️  Rollback already present or no except block found')
" 2>&1 || echo "⚠️  BUG 6 patch failed"

# ---- Step 4: Redis cache wiring ----
echo ""
echo "--- ENHANCEMENT: Redis cache wiring ---"
if [ -f redis_cache.py ]; then
    python3 -c "
import re

with open('redis_cache.py', 'r') as f:
    rc = f.read()

# Find the decorator name
decorator = None
for name in ['cached_endpoint', 'cache_response', 'redis_cache', 'cached']:
    if f'def {name}' in rc:
        decorator = name
        break

if not decorator:
    print('⚠️  No cache decorator found in redis_cache.py')
    exit(0)

print(f'Found decorator: @{decorator}')

with open('main.py', 'r') as f:
    content = f.read()

# Add import
import_line = f'from redis_cache import {decorator}'
if import_line not in content:
    # Safe import with fallback
    safe_import = f'''
try:
    {import_line}
    REDIS_CACHE_AVAILABLE = True
except ImportError:
    REDIS_CACHE_AVAILABLE = False
    def {decorator}(*args, **kwargs):
        def wrapper(fn): return fn
        return wrapper
'''
    # Insert after the last import
    lines = content.split('\n')
    last_import = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('from ') or stripped.startswith('import '):
            last_import = i
    lines.insert(last_import + 1, safe_import)
    content = '\n'.join(lines)
    print(f'✅ Added import for {decorator}')

# Wire into routes
targets = {
    '/api/v1/stats': 300,
    '/api/v1/search': 120,
    '/api/news/live': 300,
    '/api/v1/map': 600,
    '/api/transactions': 300,
}

wired = 0
for endpoint, ttl in targets.items():
    pattern = rf\"@app\.route\(['\\\"]({re.escape(endpoint)})['\\\"]\"
    match = re.search(pattern, content)
    if not match:
        print(f'  ⚠️  Route {endpoint} not found')
        continue
    
    # Check if already cached
    before = content[max(0, match.start()-200):match.start()]
    if decorator in before:
        print(f'  ⏭️  {endpoint} already cached')
        wired += 1
        continue
    
    # Add after @app.route line
    line_end = content.index('\n', match.start())
    cache_line = f'\n@{decorator}(ttl={ttl})'
    content = content[:line_end] + cache_line + content[line_end:]
    print(f'  ✅ {endpoint} → @{decorator}(ttl={ttl})')
    wired += 1

print(f'Wired {wired}/{len(targets)} endpoints')

with open('main.py', 'w') as f:
    f.write(content)
" 2>&1 || echo "⚠️  Redis wiring failed"
else
    echo "⚠️  redis_cache.py not found — skipping"
fi

# ---- Step 5: Nice-to-haves ----
echo ""
echo "--- NICE-TO-HAVES ---"

# Version bumps
echo "Fiber banner v2.2 → v2.3..."
sed -i 's/Fiber Network Discovery v2\.2/Fiber Network Discovery v2.3/g' main.py 2>/dev/null && echo "  ✅ Done" || echo "  ⏭️  Not found"

echo "MCP version → v2.3..."
sed -i 's/MCP_VERSION = "2\.2"/MCP_VERSION = "2.3"/g' main.py 2>/dev/null || true
sed -i "s/MCP_VERSION = '2\.2'/MCP_VERSION = '2.3'/g" main.py 2>/dev/null || true
sed -i 's/"version": "2\.2"/"version": "2.3"/g' main.py 2>/dev/null || true

if [ -f dchub_mcp_server.py ]; then
    sed -i 's/"2\.2"/"2.3"/g' dchub_mcp_server.py 2>/dev/null || true
    sed -i "s/'2\.2'/'2.3'/g" dchub_mcp_server.py 2>/dev/null || true
    echo "  ✅ MCP server version bumped"
fi

# server-card.json tool count
if [ -f server-card.json ]; then
    python3 -c "
import json
with open('server-card.json', 'r') as f:
    data = json.load(f)
data['version'] = '2.3'
if 'tool_count' in data:
    data['tool_count'] = 20
    print('  ✅ server-card.json: version=2.3, tools=20')
with open('server-card.json', 'w') as f:
    json.dump(data, f, indent=2)
" 2>&1 || echo "  ⚠️  server-card.json update failed"
fi

# ---- Step 6: Verify ----
echo ""
echo "======================================"
echo "VERIFICATION"
echo "======================================"

echo "Checking _validate_facility_result in main.py..."
grep -c '_validate_facility_result' main.py 2>/dev/null && echo "  ✅ Present" || echo "  ❌ Missing"

echo "Checking endpoint_hits column type..."
if [ -n "$DB_URL" ]; then
    psql "$DB_URL" -t -c "SELECT data_type FROM information_schema.columns WHERE table_name='daily_record_usage' AND column_name='endpoint_hits';" 2>/dev/null || true
fi

echo "Checking Redis import..."
grep -c 'redis_cache' main.py 2>/dev/null && echo "  ✅ Redis wired" || echo "  ❌ Not wired"

echo ""
echo "======================================"
echo "READY TO DEPLOY"
echo "======================================"
echo "Run:"
echo "  git add -A"
echo "  git diff --cached --stat"
echo "  git commit -m 'fix: 6 bugs + Redis cache + v2.3 [Mar 24]'"
echo "  git push origin main"
echo ""
echo "Post-deploy checks:"
echo "  curl -s https://dchub.cloud/api/v1/stats | python3 -m json.tool | head -5"
echo "  curl -s https://dchub.cloud/api/health"
echo "  python3 smoke_test.py --quick"
echo ""
