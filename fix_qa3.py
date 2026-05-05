"""
Run in Replit Shell:  python3 fix_qa3.py
Scans every .py file for /api/news/live route definitions,
shows their auth decorators, and patches any that still have
the enterprise gate.
"""
import os, re, sys

print("── Scanning all .py files for /api/news/live ────────────────────")

found = []
for root_dir, dirs, files in os.walk('.'):
    # Skip hidden dirs, __pycache__, venv
    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'venv', 'node_modules', '.git')]
    for fname in files:
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(root_dir, fname)
        try:
            lines = open(fpath).readlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if 'news/live' in line and ('route' in line or 'app.' in line or 'bp.' in line):
                # Grab 3 lines of context before and after
                start = max(0, i-3)
                end = min(len(lines), i+4)
                ctx = ''.join(lines[start:end])
                found.append((fpath, i+1, ctx))

if not found:
    print("  No /api/news/live route found in any .py file!")
else:
    for fpath, lineno, ctx in found:
        print(f"\n  📄 {fpath} (line {lineno}):")
        for l in ctx.splitlines():
            print(f"     {l}")

print()
print("── Patching any remaining enterprise gates ─────────────────────")

fixed = 0
for fpath, lineno, ctx in found:
    content = open(fpath).read()
    # Check if this file has the enterprise gate on get_live_news
    has_gate = bool(re.search(
        r"@_lazy_require_plan\(['\"]enterprise['\"]\)\s*\n\s*def get_live_news\(",
        content
    )) or bool(re.search(
        r"@require_plan\(['\"]enterprise['\"]\)\s*\n\s*def get_live_news\(",
        content
    ))
    if has_gate:
        # Remove the decorator line above get_live_news
        new_lines = []
        raw_lines = content.splitlines(keepends=True)
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i]
            # If next line is def get_live_news and this line is a require_plan enterprise decorator
            next_line = raw_lines[i+1] if i+1 < len(raw_lines) else ''
            if ('enterprise' in line and ('require_plan' in line or '_lazy_require_plan' in line)
                    and 'get_live_news' in next_line):
                print(f"  ✂️  Removing line {i+1}: {line.rstrip()}")
                i += 1
                continue
            new_lines.append(line)
            i += 1
        open(fpath, 'w').writelines(new_lines)
        print(f"  ✅ Fixed: {fpath}")
        fixed += 1
    else:
        if 'get_live_news' in ctx:
            print(f"  ✅ Clean: {fpath}")

print()
if fixed > 0:
    print(f"✅ Patched {fixed} file(s). Republish Replit to deploy.")
elif found:
    print("✅ All /api/news/live routes are already clean.")
else:
    print("⚠️  No /api/news/live routes found at all — check blueprint registration.")
