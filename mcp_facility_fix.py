"""
mcp_facility_fix.py — Fix get_facility broken ID lookup
========================================================

TWO BUGS:
  1. MCP_FREE_FIELDS missing 'id' — search_facilities strips it,
     so users have no ID to pass to get_facility
  2. get_facility only handles int/hex IDs, not name-based lookups

FIX: Apply this patch to main.py (2 small changes)

CHANGE 1: Add 'id' to MCP_FREE_FIELDS (~line 2541)
  BEFORE:
    MCP_FREE_FIELDS = {'name', 'city', 'state', 'country', 'provider', 'operator', 'status'}
  AFTER:
    MCP_FREE_FIELDS = {'id', 'name', 'city', 'state', 'country', 'provider', 'operator', 'status'}

CHANGE 2: Add name-based fallback to /api/v1/facilities/<facility_id>
  In the get_facility_by_id() function, after the hex lookup fails,
  add a name-based search as final fallback.

Apply via Railway shell:
  python mcp_facility_fix.py
"""

import os
import sys
import re

def apply_fix():
    main_path = os.path.expanduser('~/workspace/main.py')
    if not os.path.exists(main_path):
        main_path = '/app/main.py'
    if not os.path.exists(main_path):
        print("ERROR: main.py not found")
        return False

    with open(main_path, 'r') as f:
        content = f.read()

    changes = 0

    # ── FIX 1: Add 'id' to MCP_FREE_FIELDS ──
    old_fields = "MCP_FREE_FIELDS = {'name', 'city', 'state', 'country', 'provider', 'operator', 'status'}"
    new_fields = "MCP_FREE_FIELDS = {'id', 'name', 'city', 'state', 'country', 'provider', 'operator', 'status'}"

    if old_fields in content:
        content = content.replace(old_fields, new_fields, 1)
        changes += 1
        print(f"✅ FIX 1: Added 'id' to MCP_FREE_FIELDS")
    elif "'id'" in content.split('MCP_FREE_FIELDS')[1][:100] if 'MCP_FREE_FIELDS' in content else False:
        print(f"⏭️  FIX 1: 'id' already in MCP_FREE_FIELDS")
    else:
        print(f"⚠️  FIX 1: MCP_FREE_FIELDS not found (manual check needed)")

    # ── FIX 2: Add name-based fallback to get_facility_by_id ──
    # Look for the 404 return in get_facility_by_id and add a name fallback before it
    old_404 = '''    row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Facility not found", "id": facility_id}), 404'''

    new_404 = '''    row = cur.fetchone()
        if not row:
            # Fallback: name-based search (MCP tools pass names, not IDs)
            cur.execute("""
                SELECT df.id, df.name, df.provider, df.city, df.state, df.country,
                       df.market AS region, df.latitude, df.longitude, df.power_mw,
                       df.status, df.address, df.source,
                       f.permit_date, f.approval_date, f.co_date,
                       f.permit_source, f.permit_confidence::float AS permit_confidence
                FROM discovered_facilities df
                LEFT JOIN facilities f ON f.id = df.merged_facility_id
                WHERE df.name ILIKE %s OR df.source_id = %s
                LIMIT 1
            """, (f'%{facility_id}%', facility_id))
            row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Facility not found", "id": facility_id}), 404'''

    if old_404 in content:
        content = content.replace(old_404, new_404, 1)
        changes += 1
        print(f"✅ FIX 2: Added name-based fallback to get_facility_by_id")
    else:
        print(f"⚠️  FIX 2: Could not find exact 404 pattern (manual check needed)")

    if changes > 0:
        with open(main_path, 'w') as f:
            f.write(content)
        print(f"\n✅ Applied {changes} fix(es) to main.py")
        print(f"   Run: git add main.py && git commit -m 'Fix get_facility: add id to search + name fallback' && git push")
        return True
    else:
        print(f"\n⚠️  No changes applied — check manually")
        return False


if __name__ == '__main__':
    apply_fix()
