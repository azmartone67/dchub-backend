#!/usr/bin/env python3
"""
patch_mcp_air_permitting.py — append get_air_permitting as MCP tool #21.

What it does (idempotent):
  1. Finds dchub_mcp_server.py in the current directory.
  2. Inserts a new @mcp.tool() function right BEFORE any
     `if __name__ == "__main__":` block (so it registers before mcp.run()).
  3. If no such block exists, appends to end of file.
  4. Validates syntax with py_compile; restores backup on failure.

Safe to run more than once — detects an existing get_air_permitting and skips.

Usage:
    python3 patch_mcp_air_permitting.py
"""
from __future__ import annotations
import re
import sys
import shutil
import py_compile
from pathlib import Path

SRC = Path("dchub_mcp_server.py")
if not SRC.exists():
    print("ERROR: dchub_mcp_server.py not found in current directory.")
    print("       Run this from the directory that has dchub_mcp_server.py.")
    sys.exit(1)

BAK = SRC.with_suffix(".py.bak.mcp")
shutil.copy2(SRC, BAK)
print(f"✓ Backup: {BAK}")

text = SRC.read_text()

if "def get_air_permitting" in text:
    print("· get_air_permitting already defined — nothing to do.")
    sys.exit(0)

# The tool body — string literal, so docstring triple-quotes are OK inside
TOOL_SNIPPET = '''

@mcp.tool()
def get_air_permitting(lat: float, lon: float, capacity_mw: float = 100) -> dict:
    """Return air-permitting profile for a US data-center parcel.

    Composite 0-100 score weighted across EPA Green Book nonattainment
    (ozone/PM2.5/PM10), AQS monitor design values, Class I proximity,
    NEI source density, and state agency posture. Returns expected
    permit pathway (Minor / Synthetic Minor / NNSR / PSD), per-pollutant
    status chips (red/yellow/green), FLM consultation flags, and NNSR
    offset cost estimate.

    Args:
        lat: Latitude (WGS84)
        lon: Longitude (WGS84)
        capacity_mw: Data-center load in MW (default 100)

    Returns:
        dict with score, verdict_short, pathway, offset_estimate_usd,
        pollutants, class1, nei, state, state_context, factors
    """
    import urllib.request
    import urllib.parse
    import json as _json
    url = ("https://dchub.cloud/api/infrastructure/air-permitting/score?"
           + urllib.parse.urlencode({
               "lat": lat, "lon": lon, "capacity_mw": capacity_mw
           }))
    with urllib.request.urlopen(url, timeout=15) as r:
        payload = _json.loads(r.read())
    return payload.get("data", payload)
'''

# Find an insertion point — prefer BEFORE `if __name__ == "__main__":`
m = re.search(r'\n(?=if\s+__name__\s*==\s*["\']__main__["\'])', text)
if m:
    idx = m.start()
    text = text[:idx] + TOOL_SNIPPET + text[idx:]
    print("✓ Inserted tool before `if __name__ == '__main__':`")
else:
    # Next-best: before any `mcp.run(` call at module top level
    m = re.search(r'\n(?=mcp\.run\()', text)
    if m:
        idx = m.start()
        text = text[:idx] + TOOL_SNIPPET + text[idx:]
        print("✓ Inserted tool before `mcp.run(...)`")
    else:
        text = text.rstrip() + TOOL_SNIPPET + "\n"
        print("· No __main__ / mcp.run() anchor found — appended to end of file")

SRC.write_text(text)

# Validate syntax
try:
    py_compile.compile(str(SRC), doraise=True)
    print("✓ Syntax OK — safe to commit.")
except py_compile.PyCompileError as e:
    print(f"✗ SYNTAX ERROR after patch:\n{e}")
    print(f"  Restoring backup from {BAK}...")
    shutil.copy2(BAK, SRC)
    print("  main file restored.")
    sys.exit(2)

print()
print("Next:")
print("    python3 -c 'import dchub_mcp_server' 2>&1 | head   # smoke-test")
print("    git add dchub_mcp_server.py")
print("    git commit -m 'feat(mcp): expose get_air_permitting as tool #21'")
print("    git push")
