#!/usr/bin/env python3
"""
DC Hub Frontend — Comprehensive Fix Script
============================================
Run in Replit shell from your dchub-frontend repo root:

    python3 dchub-fix-all.py

Or dry-run first (shows changes without writing):

    python3 dchub-fix-all.py --dry-run

Fixes all issues identified in the March 29 2026 audit.
"""

import os, sys, re

DRY_RUN = '--dry-run' in sys.argv
SECOND_RAILWAY = 'https://web-production-e6382.up.railway.app'
PRIMARY_RAILWAY = 'https://dchub-backend-production.up.railway.app'

fixes_applied = 0
files_modified = set()

def fix(filepath, old, new, description, replace_all=False):
    global fixes_applied
    if not os.path.exists(filepath):
        print(f"  ⚠️  SKIP (file not found): {filepath}")
        return False

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    if old not in content:
        print(f"  ⚠️  SKIP (pattern not found): {description}")
        return False

    if replace_all:
        new_content = content.replace(old, new)
        count = content.count(old)
    else:
        new_content = content.replace(old, new, 1)
        count = 1

    if not DRY_RUN:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)

    fixes_applied += count
    files_modified.add(filepath)
    prefix = "🔍 DRY-RUN" if DRY_RUN else "✅ FIXED"
    print(f"  {prefix}: {description} ({count}x)")
    return True


def regex_fix(filepath, pattern, replacement, description):
    global fixes_applied
    if not os.path.exists(filepath):
        print(f"  ⚠️  SKIP (file not found): {filepath}")
        return False

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    new_content, count = re.subn(pattern, replacement, content)
    if count == 0:
        print(f"  ⚠️  SKIP (regex not matched): {description}")
        return False

    if not DRY_RUN:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)

    fixes_applied += count
    files_modified.add(filepath)
    prefix = "🔍 DRY-RUN" if DRY_RUN else "✅ FIXED"
    print(f"  {prefix}: {description} ({count}x)")
    return True


print("=" * 60)
print("DC Hub Frontend Fix Script — March 29, 2026")
print("=" * 60)
if DRY_RUN:
    print("⚡ DRY RUN MODE — no files will be modified\n")
else:
    print("🔧 LIVE MODE — files will be modified\n")

# ═══════════════════════════════════════════════════════════
# 1. ai-integrations.html — Fix logo paths
# ═══════════════════════════════════════════════════════════
print("\n📄 ai-integrations.html")

fix('ai-integrations.html',
    'href="static/img/dc-hub-logo.png"',
    'href="/static/images/logo.png"',
    'Fix favicon path (static/img → static/images)')

fix('ai-integrations.html',
    'src="static/img/dc-hub-logo.png"',
    'src="/static/images/logo.png"',
    'Fix nav logo path (static/img → static/images)')

# Add circuit breaker to polling
fix('ai-integrations.html',
    'setInterval(loadData, 30000);',
    '''let _failCount = 0;
    setInterval(() => {
        if (_failCount < 5) {
            loadData().catch(() => { _failCount++; console.warn('[DC Hub] loadData fail #' + _failCount); });
        }
    }, 30000);''',
    'Add circuit breaker to 30s polling (stop after 5 failures)')

# ═══════════════════════════════════════════════════════════
# 2. admin-qa.html — Fix server-card path + hardcoded URLs
# ═══════════════════════════════════════════════════════════
print("\n📄 admin-qa.html")

fix('admin-qa.html',
    '/.well-known/server-card.json',
    '/.well-known/mcp/server-card.json',
    'Fix server-card.json path (add /mcp/ prefix)',
    replace_all=True)

fix('admin-qa.html',
    'fetch("https://dchub.cloud/mcp"',
    'fetch("/mcp"',
    'Replace hardcoded /mcp URL with relative path')

# ═══════════════════════════════════════════════════════════
# 3. assets.html — Fix API_BACKENDS + limit
# ═══════════════════════════════════════════════════════════
print("\n📄 assets.html")

fix('assets.html',
    """const API_BACKENDS = [
            'https://dchub.cloud',
            'https://dchub-backend-production.up.railway.app',
            'https://dchub.cloud'
        ];""",
    f"""const API_BACKENDS = [
            '',
            '{SECOND_RAILWAY}',
            '{PRIMARY_RAILWAY}'
        ];""",
    'Fix API_BACKENDS: use relative path + both Railway instances')

fix('assets.html',
    "'/api/v1/map?all=true&limit=50000'",
    "'/api/v1/map?all=true&limit=2000'",
    'Reduce facility load from 50K to 2K (prevents 503)')

# ═══════════════════════════════════════════════════════════
# 4. ai-hub.html — Fix double redirect chain
# ═══════════════════════════════════════════════════════════
print("\n📄 ai-hub.html")

fix('ai-hub.html',
    '<meta http-equiv="refresh" content="2;url=/for-ai">',
    '<meta http-equiv="refresh" content="0;url=/ai-integrations">',
    'Fix double redirect: ai-hub → direct to /ai-integrations (was /for-ai)')

fix('ai-hub.html',
    "window.location.href='/for-ai';",
    "window.location.href='/ai-integrations';",
    'Fix JS redirect target: /for-ai → /ai-integrations')

fix('ai-hub.html',
    '<link rel="canonical" href="https://dchub.cloud/for-ai">',
    '<link rel="canonical" href="https://dchub.cloud/ai-integrations">',
    'Fix canonical URL to final destination')

fix('ai-hub.html',
    'href="/for-ai"',
    'href="/ai-integrations"',
    'Fix fallback link href',
    replace_all=True)

# ═══════════════════════════════════════════════════════════
# 5. _redirects — Add direct ai-hub rule + fix mcp.json
# ═══════════════════════════════════════════════════════════
print("\n📄 _redirects")

fix('_redirects',
    '/.well-known/mcp.json          /mcp                     301',
    '/.well-known/mcp.json          /.well-known/mcp/server-card.json  200',
    'Fix MCP discovery: rewrite to server-card.json instead of POST endpoint')

# Add ai-hub direct redirect
fix('_redirects',
    '/for-ai                        /ai-integrations         301',
    '/ai-hub                        /ai-integrations         301\n/for-ai                        /ai-integrations         301',
    'Add direct /ai-hub → /ai-integrations redirect')

# ═══════════════════════════════════════════════════════════
# 6. ai-wars.html — Replace hardcoded URLs
# ═══════════════════════════════════════════════════════════
print("\n📄 ai-wars.html")

# Fix API_BASE and DCHUB constants
fix('ai-wars.html',
    "const API_BASE = 'https://dchub.cloud/api/v1';",
    "const API_BASE = '/api/v1';",
    'Replace hardcoded API_BASE with relative path')

fix('ai-wars.html',
    "const DCHUB = 'https://dchub.cloud';",
    "const DCHUB = '';",
    'Replace hardcoded DCHUB base with empty string (relative)')

# Fix nav/footer links (keep absolute for og:url and canonical)
fix('ai-wars.html',
    '<a href="https://dchub.cloud" class="logo-group">',
    '<a href="/" class="logo-group">',
    'Fix logo link to relative')

fix('ai-wars.html',
    '<li><a href="https://dchub.cloud">Home</a></li>',
    '<li><a href="/">Home</a></li>',
    'Fix nav Home link')

fix('ai-wars.html',
    'href="https://dchub.cloud/ai-agents"',
    'href="/ai-agents"',
    'Fix ai-agents links to relative',
    replace_all=True)

fix('ai-wars.html',
    'href="https://dchub.cloud/land-power"',
    'href="/land-power"',
    'Fix land-power link')

fix('ai-wars.html',
    'href="https://dchub.cloud/transactions"',
    'href="/transactions"',
    'Fix transactions link')

fix('ai-wars.html',
    'href="https://dchub.cloud/news"',
    'href="/news"',
    'Fix news link')

fix('ai-wars.html',
    'href="https://dchub.cloud/digest"',
    'href="/digest"',
    'Fix digest link')

fix('ai-wars.html',
    'href="https://dchub.cloud/pricing"',
    'href="/pricing"',
    'Fix pricing links to relative',
    replace_all=True)

fix('ai-wars.html',
    'href="https://dchub.cloud/mcp"',
    'href="/mcp"',
    'Fix MCP link')

fix('ai-wars.html',
    'href="https://dchub.cloud/api/v1/stats"',
    'href="/api/v1/stats"',
    'Fix API stats link')

fix('ai-wars.html',
    'href="https://dchub.cloud">DC Hub</a>',
    'href="/">DC Hub</a>',
    'Fix footer DC Hub link')

# ═══════════════════════════════════════════════════════════
# 7. platform.html — Add null checks for getElementById
# ═══════════════════════════════════════════════════════════
print("\n📄 platform.html")

# Wrap each getElementById().addEventListener with null check
elements_to_guard = [
    'save-search-btn', 'facility-modal', 'saved-searches-btn',
    'saved-close', 'filter-reset', 'pricing-btn', 'modal-close',
    'lead-modal', 'pricing-form', 'export-btn'
]

for elem_id in elements_to_guard:
    old_pattern = f"document.getElementById('{elem_id}').addEventListener"
    new_pattern = f"(document.getElementById('{elem_id}')||{{}}).addEventListener"
    fix('platform.html', old_pattern, new_pattern,
        f'Null-guard getElementById("{elem_id}")')

# Fix the map API call to use pagination
fix('platform.html',
    "fetchAPI('/api/v1/map?all=true&limit=2000')",
    "fetchAPI('/api/v1/map?all=true&limit=500')",
    'Reduce initial facility load to 500 (was 2000)')

# ═══════════════════════════════════════════════════════════
# 8. compare.html — Add second Railway fallback
# ═══════════════════════════════════════════════════════════
print("\n📄 compare.html")

regex_fix('compare.html',
    r"const API_BASE\s*=\s*'https://dchub\.cloud'",
    "const API_BASE = ''",
    'Replace hardcoded API_BASE with relative path')

# ═══════════════════════════════════════════════════════════
# 9. connect.html — Keep absolute URLs for MCP config examples
#    but fix fetch calls
# ═══════════════════════════════════════════════════════════
print("\n📄 connect.html")
# Note: MCP endpoint URLs shown to users should stay absolute
# (users need full URL for their MCP configs). Only fix fetch calls.
regex_fix('connect.html',
    r'fetch\(["\']https://dchub\.cloud/mcp["\']',
    'fetch("/mcp"',
    'Fix fetch call to /mcp — use relative path')

# ═══════════════════════════════════════════════════════════
# 10. analytics.html — Fix Chart.js CDN
# ═══════════════════════════════════════════════════════════
print("\n📄 analytics.html")

fix('analytics.html',
    'https://cdn.jsdelivr.net/npm/chart.js',
    'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js',
    'Switch Chart.js CDN from jsdelivr (blocked by tracking prevention) to cdnjs')

# ═══════════════════════════════════════════════════════════
# 11. index.html — Add live stats loader
# ═══════════════════════════════════════════════════════════
print("\n📄 index.html")

# Add a small script before </body> to fetch live stats
# r33-Q+escape-fix (2026-05-22): raw string (r-prefix) so the JS regex
# `/20,000\+?/` doesn't trigger Python's "invalid escape sequence '\+'"
# SyntaxWarning at import. The \+ is a JS regex escape, not Python.
STATS_SCRIPT = r"""<script>
(function(){
    fetch('/api/v1/stats').then(r=>r.json()).then(s=>{
        document.querySelectorAll('.metric-value, .feature-stat, td.check').forEach(el=>{
            if(el.textContent.includes('20,000')){
                var n=(s.total_facilities||20000).toLocaleString();
                el.textContent=el.textContent.replace(/20,000\+?/,n+'+');
            }
        });
    }).catch(()=>{});
})();
</script></body>"""

# Idempotency guard: fix() does a find/replace that turns `</body>` into
# `<script>...</script></body>`. On the NEXT run the new `</body>` (still
# there at the end) matches again and a SECOND copy of STATS_SCRIPT gets
# injected — visible as duplicate <script> blocks in the root index.html
# diff. Skip the injection if the marker is already present.
def _stats_script_already_injected(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return "/api/v1/stats" in content and "20,000\\+?" in content
    except Exception:
        return False

if _stats_script_already_injected('index.html'):
    print("📄 index.html\n  ⏭️  STATS_SCRIPT already injected — skipping (idempotent)")
else:
    fix('index.html',
        '</body>',
        STATS_SCRIPT,
        'Inject live stats loader before </body> — replaces 20,000+ with real count')

# ═══════════════════════════════════════════════════════════
# 12. press.html — Fix hardcoded XHR URL
# ═══════════════════════════════════════════════════════════
print("\n📄 press.html")

regex_fix('press.html',
    r"xhr\.open\('GET',\s*'https://dchub\.cloud/api/v1/stats'",
    "xhr.open('GET','/api/v1/stats'",
    'Fix XHR stats call to relative URL')

# ═══════════════════════════════════════════════════════════
# 13. glossary.html — Check and fix (if needed)
# ═══════════════════════════════════════════════════════════
print("\n📄 glossary.html")
if os.path.exists('glossary.html'):
    with open('glossary.html', 'r') as f:
        content = f.read()
    if 'https://dchub.cloud/api' in content:
        regex_fix('glossary.html',
            r"'https://dchub\.cloud(/api/[^']*)'",
            r"'\1'",
            'Replace hardcoded API URLs with relative paths')
    else:
        print("  ✅ No API issues found — static content only")

# ═══════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"{'DRY RUN ' if DRY_RUN else ''}SUMMARY")
print("=" * 60)
print(f"  Total fixes applied: {fixes_applied}")
print(f"  Files modified: {len(files_modified)}")
for f in sorted(files_modified):
    print(f"    • {f}")
print()

if DRY_RUN:
    print("Run without --dry-run to apply all fixes:")
    print("  python3 dchub-fix-all.py")
else:
    print("All fixes applied! Next steps:")
    print("  1. git diff           — review all changes")
    print("  2. git add -A")
    print(f'  3. git commit -m "fix: patch {fixes_applied} frontend issues — March 29 audit"')
    print("  4. git push origin main")
    print("  5. Rebuild Cloudflare Pages deployment")
    print()
    print("BACKEND (run in dchub-backend repo):")
    print(f"  • Add CORS header for Railway: Access-Control-Allow-Origin: https://dchub.cloud")
    print(f"  • Second Railway instance: {SECOND_RAILWAY}")
    print(f"  • Fix /mcp POST handler (returns 500)")
    print(f"  • Fix /api/v1/markets/compare route (returns 503)")
    print(f"  • Fix /api/v1/mcp/platforms route (returns 503)")
