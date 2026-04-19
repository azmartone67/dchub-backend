"""
Run this in Replit Shell:  python3 fix_qa.py
Fixes both QA failures in-place.
"""
import re, sys

errors = []

# ── Fix 1: Remove enterprise gate from /api/news/live ──────────────────────
path1 = 'routes/deals_routes.py'
try:
    content = open(path1).read()
    old = ("@deals_bp.route('/api/news/live', methods=['GET'])\n"
           "@_lazy_require_plan('enterprise')\n"
           "def get_live_news():")
    new = ("@deals_bp.route('/api/news/live', methods=['GET'])\n"
           "def get_live_news():")
    if old in content:
        open(path1, 'w').write(content.replace(old, new))
        print(f"✅ Fix 1 applied: removed @_lazy_require_plan('enterprise') from get_live_news in {path1}")
    else:
        # Already fixed or different whitespace — check
        if "@_lazy_require_plan('enterprise')" not in content:
            print(f"✅ Fix 1 already applied (no enterprise gate found in {path1})")
        else:
            print(f"⚠️  Fix 1: pattern not found exactly — check {path1} manually")
            errors.append('fix1')
except FileNotFoundError:
    print(f"❌ Fix 1: {path1} not found — are you running from the project root?")
    errors.append('fix1')

# ── Fix 2: Remove /.well-known/mcp.json from AUTO_REGISTER_PATHS ───────────
path2 = 'main.py'
try:
    content = open(path2).read()
    # Match the line regardless of surrounding whitespace
    pattern = r"'/.well-known/mcp\.json'\s*,?\s*"
    if re.search(pattern, content):
        content2 = re.sub(pattern, '', content)
        # Clean up any double commas or trailing comma before closing brace
        content2 = re.sub(r',\s*,', ',', content2)
        open(path2, 'w').write(content2)
        print(f"✅ Fix 2 applied: removed '/.well-known/mcp.json' from AUTO_REGISTER_PATHS in {path2}")
    else:
        print(f"✅ Fix 2 already applied ('/.well-known/mcp.json' not in AUTO_REGISTER_PATHS)")
except FileNotFoundError:
    print(f"❌ Fix 2: {path2} not found — are you running from the project root?")
    errors.append('fix2')

# ── Summary ─────────────────────────────────────────────────────────────────
print()
if errors:
    print(f"⚠️  {len(errors)} fix(es) need manual attention: {errors}")
    sys.exit(1)
else:
    print("🎉 All fixes applied. Republish Replit to deploy.")
