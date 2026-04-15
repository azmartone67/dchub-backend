#!/usr/bin/env python3
"""
patch_press_release_route.py — fix /api/press-releases/<slug> "bad date" bug.

Root cause: line 9856 registers `@app.route('/api/press-releases/<date_slug>')`
as a catch-all on the digest handler. Flask matches it before the proper
slug handler at line 14518, so any non-date slug feeds into the date parser
and triggers "bad date".

Fix: remove the conflicting decorator. Keep line 9855
(`@app.route('/api/press-releases/digest-<date_slug>')`) so digest-by-date
URLs still work. Slug URLs then fall through to the proper handler.

Idempotent. Creates main.py.bak.pressroute. Auto-restores on syntax error.
"""
from __future__ import annotations
import sys
import shutil
import py_compile
from pathlib import Path

SRC = Path("main.py")
if not SRC.exists():
    print("ERROR: main.py not found.")
    sys.exit(1)

shutil.copy2(SRC, "main.py.bak.pressroute")
text = SRC.read_text()

target = "@app.route('/api/press-releases/<date_slug>', methods=['GET'])"
if target not in text:
    if "# REMOVED conflicting" in text:
        print("· Already patched.")
        sys.exit(0)
    print("ERROR: target route line not found. Check with:")
    print("       grep -n \"@app.route('/api/press-releases/<date_slug>'\" main.py")
    sys.exit(2)

# Replace the decorator line with a comment explaining why it's gone
new_line = (
    "# REMOVED conflicting decorator: was @app.route('/api/press-releases/<date_slug>', ...)\n"
    "# It caught slug-based requests (e.g. dc-hub-global-...) before they reached the\n"
    "# proper slug handler at line ~14518, causing 'bad date' errors.\n"
    "# Date-based access still works via the digest- prefix above."
)

text = text.replace(target, new_line)
SRC.write_text(text)
print("✓ Removed conflicting /api/press-releases/<date_slug> decorator")

try:
    py_compile.compile(str(SRC), doraise=True)
    print("✓ Syntax OK — safe to commit.")
except py_compile.PyCompileError as e:
    print(f"✗ Syntax error: {e}")
    shutil.copy2("main.py.bak.pressroute", SRC)
    print("  Restored backup.")
    sys.exit(3)

print("\nNext:")
print("  git add main.py")
print("  git commit -m 'fix(press-releases): remove date_slug catch-all that broke slug lookups'")
print("  git push")
print()
print("After Railway redeploys (~60s), verify the slug detail API works:")
print("  curl -s 'https://dchub.cloud/api/press-releases/dc-hub-global-infrastructure-1-29m-records-live' | python3 -m json.tool | head -10")
print("  # Should return real data, not 'bad date'")
