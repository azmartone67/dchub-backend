#!/usr/bin/env python3
"""
fix_state_resolver.py — fix bbox-overlap bug in _ap_resolve_state.

Problem: Milwaukee (43.04, -87.91) resolves to MI instead of WI because
Michigan's bounding box includes Lake Michigan, which overlaps Wisconsin's
coastal area. Dict iteration picks whichever state appears first.

Fix: collect ALL matching states, return the one with the smallest bbox
area. Smaller bboxes are (almost always) the correct specific-state match
when multiple overlap.

Run once:
    python3 fix_state_resolver.py
"""
from __future__ import annotations
import re
import shutil
import sys
import py_compile
from pathlib import Path

SRC = Path("main.py")
if not SRC.exists():
    print("ERROR: main.py not found.")
    sys.exit(1)

shutil.copy2(SRC, "main.py.bak.resolver")
text = SRC.read_text()

# Find the def _ap_resolve_state function body
# Handles the common shape we shipped originally:
#   def _ap_resolve_state(lat, lon):
#       for state, box in _AP_STATE_BOXES.items():
#           if _ap_in_bounds(lat, lon, box):
#               return state
#       return None

pattern = re.compile(
    r"(def _ap_resolve_state\(lat, lon\):\s*\n)"           # 1: signature
    r"(\s+)for state, box in _AP_STATE_BOXES\.items\(\):\s*\n"  # 2: indent
    r"\s+if _ap_in_bounds\(lat, lon, box\):\s*\n"
    r"\s+return state\s*\n"
    r"\s+return None\s*\n",
    re.MULTILINE,
)

match = pattern.search(text)
if not match:
    # Check if already patched
    if "# smallest-bbox-wins" in text:
        print("· _ap_resolve_state already patched — nothing to do.")
        sys.exit(0)
    print("ERROR: could not find the original _ap_resolve_state function shape.")
    print("       main.py may have been hand-edited. Aborting without writing.")
    sys.exit(2)

indent = match.group(2)
new_body = (
    f"{match.group(1)}"
    f"{indent}# smallest-bbox-wins tie-breaker — prevents MI/WI Lake Michigan overlap\n"
    f"{indent}matches = []\n"
    f"{indent}for state, box in _AP_STATE_BOXES.items():\n"
    f"{indent}    if _ap_in_bounds(lat, lon, box):\n"
    f"{indent}        (mn_lat, mn_lon), (mx_lat, mx_lon) = box\n"
    f"{indent}        area = (mx_lat - mn_lat) * (mx_lon - mn_lon)\n"
    f"{indent}        matches.append((area, state))\n"
    f"{indent}if not matches:\n"
    f"{indent}    return None\n"
    f"{indent}matches.sort()\n"
    f"{indent}return matches[0][1]\n"
)

text = text[:match.start()] + new_body + text[match.end():]
SRC.write_text(text)
print("✓ Patched _ap_resolve_state with smallest-bbox-wins logic")

try:
    py_compile.compile(str(SRC), doraise=True)
    print("✓ Syntax OK — safe to commit.")
except py_compile.PyCompileError as e:
    print(f"✗ Syntax error:\n{e}")
    shutil.copy2("main.py.bak.resolver", SRC)
    print("  Restored backup.")
    sys.exit(3)

print("\nNext:")
print("  git pull --rebase                              # sync any remote commits first")
print("  git add main.py")
print("  git commit -m 'fix(air-permitting): smallest-bbox-wins state resolver (MI/WI overlap)'")
print("  git push")
print("\nAfter Railway redeploys (~60s), retest Milwaukee:")
print("  curl -s -X POST https://dchub.cloud/api/infrastructure/air-permitting/score \\")
print("    -H 'Content-Type: application/json' \\")
print("    -d '{\"lat\":43.04,\"lon\":-87.91,\"capacity_mw\":100}' | python3 -m json.tool | grep state")
print("  # expect: state=WI")
