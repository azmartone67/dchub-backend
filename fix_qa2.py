"""
Run this in Replit Shell:  python3 fix_qa2.py
Patches ALL copies of deals_routes.py that have the enterprise gate,
removes /.well-known/mcp.json from AUTO_REGISTER_PATHS, and prints
a diagnostic so we can see exactly which file Flask is using.
"""
import re, sys, os

errors = []
OLD_GATE = "@_lazy_require_plan('enterprise')\ndef get_live_news():"
NEW_GATE = "def get_live_news():"

# ── Find and fix EVERY deals_routes.py that still has the gate ─────────────
candidates = [
    'routes/deals_routes.py',
    'deals_routes.py',           # root-level copy the user may have uploaded
]

fixed_any = False
for path in candidates:
    if not os.path.exists(path):
        print(f"   (not present: {path})")
        continue
    content = open(path).read()
    if OLD_GATE in content:
        open(path, 'w').write(content.replace(OLD_GATE, NEW_GATE))
        print(f"✅ Fixed enterprise gate in: {path}")
        fixed_any = True
    else:
        if "@_lazy_require_plan('enterprise')" not in content:
            print(f"✅ Already clean: {path}")
        else:
            # Different whitespace — try regex
            new_content = re.sub(
                r"@_lazy_require_plan\('enterprise'\)\s*\ndef get_live_news\(\):",
                "def get_live_news():",
                content
            )
            if new_content != content:
                open(path, 'w').write(new_content)
                print(f"✅ Fixed (regex) enterprise gate in: {path}")
                fixed_any = True
            else:
                print(f"⚠️  Could not fix automatically: {path} — check manually")
                errors.append(path)

# ── Fix main.py AUTO_REGISTER_PATHS ────────────────────────────────────────
path2 = 'main.py'
if os.path.exists(path2):
    content = open(path2).read()
    if "'/.well-known/mcp.json'" in content:
        content2 = re.sub(r"'/.well-known/mcp\.json'\s*,?\s*", '', content)
        content2 = re.sub(r',\s*,', ',', content2)
        open(path2, 'w').write(content2)
        print(f"✅ Removed '/.well-known/mcp.json' from AUTO_REGISTER_PATHS in {path2}")
    else:
        print(f"✅ AUTO_REGISTER_PATHS already clean in {path2}")
else:
    print(f"❌ {path2} not found")
    errors.append('main.py')

# ── Diagnostic: show which module Python would actually import ─────────────
print()
print("── Diagnostic ──────────────────────────────────────────────────────")
try:
    import importlib, importlib.util
    spec = importlib.util.find_spec('routes.deals_routes')
    if spec:
        print(f"  routes.deals_routes resolves to: {spec.origin}")
        src = open(spec.origin).read()
        if "@_lazy_require_plan('enterprise')" in src and 'get_live_news' in src:
            # find the line
            for i, line in enumerate(src.splitlines(), 1):
                if 'get_live_news' in line:
                    print(f"  Line {i}: {line.strip()}")
                    if i > 1:
                        prev = src.splitlines()[i-2].strip()
                        print(f"  Line {i-1}: {prev}")
            print("  ❌ Enterprise gate STILL PRESENT in the resolved module!")
            errors.append('resolved module still gated')
        else:
            print("  ✅ No enterprise gate in the resolved module")
    else:
        print("  ⚠️  routes.deals_routes not importable from current directory")
except Exception as e:
    print(f"  (diagnostic error: {e})")

# ── Summary ─────────────────────────────────────────────────────────────────
print()
if errors:
    print(f"⚠️  Issues remaining: {errors}")
    sys.exit(1)
else:
    print("🎉 All fixes applied. Republish Replit to deploy.")
