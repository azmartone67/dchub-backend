#!/usr/bin/env python3
"""
fix_state_boxes.py — expand _AP_STATE_BOXES from 16 to 51.

The scoring function uses a separate STATE_BOXES dict to resolve which US
state a lat/lon falls into. Without all 50 + DC covered, parcels in
e.g. Alabama return state=None and state_context="" even though the
extras module has context for them.

This script:
  1. Appends STATE_BOXES (51 entries) to air_permitting_extras.py
  2. Patches main.py to import STATE_BOXES
  3. Removes the inline _AP_STATE_BOXES = {...} block

Usage:
    python3 fix_state_boxes.py
"""
from __future__ import annotations
import json
import re
import shutil
import sys
import py_compile
from pathlib import Path

STATE_BOXES = {
    "AL": [[30.2, -88.5], [35.0, -84.9]], "AK": [[51.2, -179.1], [71.4, -129.9]],
    "AZ": [[31.3, -115.0], [37.0, -109.0]], "AR": [[33.0, -94.6], [36.5, -89.6]],
    "CA": [[32.5, -124.5], [42.0, -114.0]], "CO": [[36.9, -109.1], [41.1, -102.0]],
    "CT": [[40.9, -73.8], [42.1, -71.7]],   "DE": [[38.4, -75.8], [39.9, -74.9]],
    "DC": [[38.8, -77.2], [39.0, -76.9]],   "FL": [[24.4, -87.7], [31.0, -79.9]],
    "GA": [[30.3, -85.6], [35.0, -80.7]],   "HI": [[18.8, -160.4], [22.3, -154.7]],
    "ID": [[41.9, -117.3], [49.0, -111.0]], "IL": [[36.9, -91.5], [42.5, -87.0]],
    "IN": [[37.7, -88.1], [41.8, -84.7]],   "IA": [[40.3, -96.7], [43.5, -90.1]],
    "KS": [[36.9, -102.1], [40.0, -94.6]],  "KY": [[36.5, -89.6], [39.2, -81.9]],
    "LA": [[28.9, -94.1], [33.1, -88.8]],   "ME": [[42.9, -71.1], [47.5, -66.9]],
    "MD": [[37.9, -79.5], [39.7, -75.0]],   "MA": [[41.2, -73.5], [42.9, -69.9]],
    "MI": [[41.6, -90.4], [48.3, -82.1]],   "MN": [[43.5, -97.3], [49.4, -89.5]],
    "MS": [[30.1, -91.7], [35.0, -88.1]],   "MO": [[35.9, -95.8], [40.6, -89.1]],
    "MT": [[44.3, -116.1], [49.0, -104.0]], "NE": [[40.0, -104.1], [43.0, -95.3]],
    "NV": [[35.0, -120.0], [42.0, -114.0]], "NH": [[42.7, -72.6], [45.3, -70.6]],
    "NJ": [[38.9, -75.6], [41.4, -73.9]],   "NM": [[31.3, -109.1], [37.0, -103.0]],
    "NY": [[40.4, -79.8], [45.0, -71.8]],   "NC": [[33.8, -84.4], [36.6, -75.4]],
    "ND": [[45.9, -104.1], [49.0, -96.6]],  "OH": [[38.4, -84.8], [41.9, -80.5]],
    "OK": [[33.6, -103.0], [37.0, -94.4]],  "OR": [[42.0, -124.6], [46.3, -116.5]],
    "PA": [[39.7, -80.5], [42.3, -74.7]],   "RI": [[41.1, -71.9], [42.0, -71.1]],
    "SC": [[32.0, -83.4], [35.2, -78.5]],   "SD": [[42.5, -104.1], [45.9, -96.4]],
    "TN": [[35.0, -90.3], [36.7, -81.6]],   "TX": [[25.8, -106.7], [36.5, -93.5]],
    "UT": [[36.9, -114.1], [42.0, -109.0]], "VT": [[42.7, -73.4], [45.0, -71.5]],
    "VA": [[36.5, -83.7], [39.5, -75.2]],   "WA": [[45.5, -124.8], [49.0, -116.9]],
    "WV": [[37.2, -82.6], [40.6, -77.7]],   "WI": [[42.5, -92.9], [47.1, -86.8]],
    "WY": [[40.9, -111.1], [45.0, -104.0]],
}

EXTRAS = Path("air_permitting_extras.py")
MAIN   = Path("main.py")
if not EXTRAS.exists() or not MAIN.exists():
    print("ERROR: need air_permitting_extras.py and main.py in current directory")
    sys.exit(1)

# 1. Append STATE_BOXES to extras (if not already there)
extras_text = EXTRAS.read_text()
if "STATE_BOXES = " in extras_text:
    print("· STATE_BOXES already in air_permitting_extras.py — skipping append")
else:
    new_block = "\n\nSTATE_BOXES = " + json.dumps(STATE_BOXES, indent=2) + "\n"
    EXTRAS.write_text(extras_text.rstrip() + new_block)
    print(f"✓ Appended STATE_BOXES ({len(STATE_BOXES)} entries) to {EXTRAS}")

# 2. Patch main.py
shutil.copy2(MAIN, "main.py.bak.boxes")
main_text = MAIN.read_text()
original_len = len(main_text)

if "STATE_BOXES as _AP_STATE_BOXES" in main_text:
    print("· main.py already imports STATE_BOXES — skipping")
else:
    # Try to add to existing extras import block
    pat = re.compile(
        r"(from air_permitting_extras import \(\s*\n\s*STATE_CONTEXT as _AP_STATE_CONTEXT,\s*\n\s*NEI_SOURCES\s+as _AP_NEI,\s*\n)(\))",
        re.MULTILINE
    )
    m = pat.search(main_text)
    if m:
        main_text = pat.sub(r"\1    STATE_BOXES  as _AP_STATE_BOXES,\n\2", main_text)
        print("✓ Added STATE_BOXES to existing extras import block in main.py")
    else:
        # Fallback: standalone import line right after the extras import
        anchor = "from air_permitting_extras import"
        idx = main_text.find(anchor)
        if idx == -1:
            print("ERROR: can't find extras import in main.py. Aborting.")
            sys.exit(2)
        close = main_text.find(")", idx)
        eol = main_text.find("\n", close) + 1
        insert = "from air_permitting_extras import STATE_BOXES as _AP_STATE_BOXES\n"
        main_text = main_text[:eol] + insert + main_text[eol:]
        print("✓ Added standalone STATE_BOXES import in main.py")

# Remove inline _AP_STATE_BOXES = {...}
def remove_balanced(s, marker, opener, closer):
    idx = s.find(marker)
    if idx == -1: return s, False
    op = s.find(opener, idx); depth = 0; i = op
    while i < len(s):
        if s[i] == opener: depth += 1
        elif s[i] == closer:
            depth -= 1
            if depth == 0:
                end = i + 1
                while end < len(s) and s[end] in "\r\n": end += 1
                return s[:idx] + s[end:], True
        i += 1
    return s, False

main_text, removed = remove_balanced(main_text, "_AP_STATE_BOXES = {", "{", "}")
print("✓ Removed inline _AP_STATE_BOXES block" if removed else "· _AP_STATE_BOXES already removed")

MAIN.write_text(main_text)
print(f"\nBefore: {original_len:,} bytes")
print(f"After:  {len(main_text):,} bytes  (delta {len(main_text)-original_len:+,})")

try:
    py_compile.compile(str(MAIN), doraise=True)
    print("✓ Syntax OK — safe to commit.")
except py_compile.PyCompileError as e:
    print(f"✗ Syntax error: {e}")
    shutil.copy2("main.py.bak.boxes", MAIN)
    print("  Restored backup.")
    sys.exit(3)

print("\nNext:")
print("  git add air_permitting_extras.py main.py")
print("  git commit -m 'fix(air-permitting): expand STATE_BOXES 16->51 so all states resolve'")
print("  git push")
