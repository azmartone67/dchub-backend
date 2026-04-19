#!/usr/bin/env python3
"""
patch_monitors_import.py — wire MONITORS from air_permitting_data into main.py.

The earlier patcher kept _AP_MONITORS inline because we didn't have AQS
data at the time. Now that upgrade_air_permitting.py pulled 23,319 live
monitors into air_permitting_data.py, swap the inline seed for the import.
"""
from __future__ import annotations
import re
import shutil
import sys
import py_compile
from pathlib import Path

SRC = Path("main.py")
if not SRC.exists():
    print("ERROR: main.py not found in current directory.")
    sys.exit(1)

shutil.copy2(SRC, "main.py.bak.monitors")
text = SRC.read_text()
original_len = len(text)

if "MONITORS" in text and re.search(r"from air_permitting_data import\s*\([^)]*MONITORS[^)]*\)", text):
    print("· MONITORS already imported from air_permitting_data — nothing to do.")
    sys.exit(0)

# Add MONITORS to the existing `from air_permitting_data import (...)` block
pat = re.compile(
    r"(from air_permitting_data import \(\s*\n"
    r"\s*NONATTAINMENT as _AP_NONATTAINMENT,\s*\n)"
    r"(\s*CLASS1 as _AP_CLASS1,\s*\n\))",
    re.MULTILINE,
)
m = pat.search(text)
if m:
    text = pat.sub(r"\1    MONITORS     as _AP_MONITORS,\n\2", text)
    print("✓ Added MONITORS to air_permitting_data import block")
else:
    print("ERROR: couldn't find the expected import block shape in main.py.")
    print("       Check with: grep -A3 'from air_permitting_data import' main.py")
    sys.exit(2)

# Remove the inline _AP_MONITORS = [ ... ] block
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

text, removed = remove_balanced(text, "_AP_MONITORS = [", "[", "]")
print("✓ Removed inline _AP_MONITORS seed block" if removed else "· _AP_MONITORS inline block not found (maybe already removed)")

SRC.write_text(text)
print(f"\nBefore: {original_len:,} bytes")
print(f"After:  {len(text):,} bytes  (delta {len(text) - original_len:+,})")

try:
    py_compile.compile(str(SRC), doraise=True)
    print("✓ Syntax OK — safe to commit.")
except py_compile.PyCompileError as e:
    print(f"✗ Syntax error: {e}")
    shutil.copy2("main.py.bak.monitors", SRC)
    print("  Restored backup.")
    sys.exit(3)

print("\nNext:")
print("  git add main.py")
print("  git commit -m 'feat(air-permitting): wire 23,319 live AQS monitors'")
print("  git push")
