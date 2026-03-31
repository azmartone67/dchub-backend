#!/usr/bin/env python3
"""
fix_comprehensive_v2.py — Safe, comprehensive fixes for DCHub backend
=====================================================================
Fixes (all safe text replacements — NO indentation changes):
  1. energy_discovery_status duplicate endpoint (rename inline version)
  2. AUTOINCREMENT → SERIAL PRIMARY KEY (PostgreSQL compat)
  3. Remove unused sqlite3 imports (only api_monetization.py, mcp_auto_register.py)
  4. sqlite3 strftime() → PostgreSQL to_char()

EXCLUDES (intentional SQLite files):
  - mcp_gateway.py, db_persistence.py, backfill_sqlite_to_neon.py
  - crawler_visits files, fix_*.py scripts, pre_deploy_check.py

Run:  python fix_comprehensive_v2.py          (dry run)
      python fix_comprehensive_v2.py --fix    (apply fixes)
"""

import re
import sys
import os
import glob

DRY_RUN = "--fix" not in sys.argv

# Files that intentionally use SQLite — never modify these
SKIP_FILES = {
    'mcp_gateway.py', 'db_persistence.py', 'backfill_sqlite_to_neon.py',
    'fix_connection_leaks_bulk.py', 'fix_remove_sqlite3_imports.py',
    'fix_insert_or_replace.py', 'fix_warnings_leaks.py', 'fix_warnings_sqlite3.py',
    'fix_all_sqlite_to_pg.py', 'fix_targeted_bugs.py', 'fix_comprehensive_v2.py',
    'pre_deploy_check.py', 'repair_broken_try.py',
}

# Files with parentheses or numbered copies — skip
def should_skip(filename):
    base = os.path.basename(filename)
    if base in SKIP_FILES:
        return True
    if '(' in base or ')' in base:
        return True
    if not base.endswith('.py'):
        return True
    return False

stats = {
    'energy_dup': 0,
    'autoincrement': 0,
    'sqlite3_import': 0,
    'strftime': 0,
    'files_modified': set(),
}


def fix_energy_discovery_duplicate():
    """Rename the inline energy_discovery_status to avoid endpoint collision."""
    path = "main.py"
    if not os.path.exists(path):
        print(f"  ❌ {path} not found")
        return

    with open(path, "r") as f:
        content = f.read()

    # Find the inline definition that conflicts
    # Pattern: def energy_discovery_status(): right after @app.route('/api/v1/energy/discovery/status')
    old = "def energy_discovery_status():\n    \"\"\"Energy infrastructure auto-discovery status and counts\"\"\""
    new = "def energy_discovery_status_inline():\n    \"\"\"Energy infrastructure auto-discovery status and counts (inline v1)\"\"\""

    if old in content:
        if DRY_RUN:
            print(f"  📋 Would rename energy_discovery_status → energy_discovery_status_inline in main.py")
        else:
            content = content.replace(old, new, 1)  # Only replace first occurrence
            with open(path, "w") as f:
                f.write(content)
            print(f"  ✅ Renamed energy_discovery_status → energy_discovery_status_inline in main.py")
        stats['energy_dup'] += 1
        stats['files_modified'].add(path)
    else:
        # Check if already fixed
        if "energy_discovery_status_inline" in content:
            print(f"  ✅ Already fixed — energy_discovery_status_inline exists in main.py")
        else:
            print(f"  ⚠️  Pattern not found in main.py — may need manual review")


def fix_autoincrement():
    """Replace AUTOINCREMENT with SERIAL PRIMARY KEY pattern for PostgreSQL."""
    py_files = glob.glob("*.py")
    total = 0

    for filepath in sorted(py_files):
        if should_skip(filepath):
            continue

        with open(filepath, "r") as f:
            content = f.read()

        if "AUTOINCREMENT" not in content:
            continue

        # Pattern: "id INTEGER PRIMARY KEY AUTOINCREMENT" → "id SERIAL PRIMARY KEY"
        new_content = re.sub(
            r'id\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
            'id SERIAL PRIMARY KEY',
            content
        )

        changes = content.count("AUTOINCREMENT") - new_content.count("AUTOINCREMENT")

        if changes > 0:
            if DRY_RUN:
                print(f"  📋 {filepath}: {changes} AUTOINCREMENT → SERIAL PRIMARY KEY")
            else:
                with open(filepath, "w") as f:
                    f.write(new_content)
                print(f"  ✅ {filepath}: {changes} AUTOINCREMENT → SERIAL PRIMARY KEY")
            total += changes
            stats['files_modified'].add(filepath)

    stats['autoincrement'] = total
    if total == 0:
        print(f"  ✅ No AUTOINCREMENT instances found (already fixed)")
    else:
        print(f"  {'📋' if DRY_RUN else '✅'} Total: {total} AUTOINCREMENT replacements across {len([f for f in stats['files_modified']])} files")


def fix_unused_sqlite3_imports():
    """Remove sqlite3 imports from files that don't actually use sqlite3."""
    # Only these 2 files have verified unused sqlite3 imports
    safe_files = ['api_monetization.py', 'mcp_auto_register.py']

    total = 0
    for filepath in safe_files:
        if not os.path.exists(filepath):
            continue

        with open(filepath, "r") as f:
            content = f.read()

        if "import sqlite3" not in content:
            continue

        new_content = content.replace("import sqlite3\n", "")
        # Also fix any except sqlite3.Error/OperationalError → except Exception
        new_content = re.sub(r'except\s+sqlite3\.\w+', 'except Exception', new_content)

        if new_content != content:
            if DRY_RUN:
                print(f"  📋 {filepath}: Remove unused sqlite3 import")
            else:
                with open(filepath, "w") as f:
                    f.write(new_content)
                print(f"  ✅ {filepath}: Removed unused sqlite3 import")
            total += 1
            stats['files_modified'].add(filepath)

    stats['sqlite3_import'] = total
    if total == 0:
        print(f"  ✅ No unused sqlite3 imports found")


def fix_strftime():
    """Replace SQLite strftime() with PostgreSQL equivalents in SQL strings."""
    py_files = glob.glob("*.py")
    total = 0

    for filepath in sorted(py_files):
        if should_skip(filepath):
            continue

        with open(filepath, "r") as f:
            content = f.read()

        if "strftime" not in content:
            continue

        new_content = content

        # Common SQLite → PostgreSQL date function replacements in SQL strings
        # strftime('%Y-%m-%d', column) → to_char(column, 'YYYY-MM-DD')
        # strftime('%Y-%m', column) → to_char(column, 'YYYY-MM')
        # strftime('%Y-%m-%d', 'now') → to_char(NOW(), 'YYYY-MM-DD')
        # strftime('%s', column) → EXTRACT(EPOCH FROM column)

        # Only replace inside SQL strings (between triple quotes or regular quotes)
        # Be conservative — only replace well-known patterns
        replacements = [
            (r"strftime\('%Y-%m-%d',\s*'now'\)", "to_char(NOW(), 'YYYY-MM-DD')"),
            (r"strftime\('%Y-%m',\s*'now'\)", "to_char(NOW(), 'YYYY-MM')"),
            (r"strftime\('%Y-%m-%d %H:%M:%S',\s*'now'\)", "to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')"),
        ]

        for pattern, replacement in replacements:
            new_content = re.sub(pattern, replacement, new_content)

        if new_content != content:
            changes = sum(1 for p, _ in replacements if re.search(p, content))
            if DRY_RUN:
                print(f"  📋 {filepath}: {changes} strftime() → PostgreSQL date functions")
            else:
                with open(filepath, "w") as f:
                    f.write(new_content)
                print(f"  ✅ {filepath}: {changes} strftime() → PostgreSQL date functions")
            total += changes
            stats['files_modified'].add(filepath)

    stats['strftime'] = total
    if total == 0:
        print(f"  ✅ No strftime() patterns found (or already fixed)")


if __name__ == "__main__":
    print("=" * 65)
    print("  DCHub Comprehensive Fix v2 — Safe Text Replacements Only")
    print("=" * 65)

    if DRY_RUN:
        print("\n  🔍 DRY RUN MODE — no files will be changed")
        print("  Run with --fix to apply changes\n")
    else:
        print("\n  🔧 FIX MODE — applying changes\n")

    print("── 1. Energy Discovery Duplicate Endpoint ──")
    fix_energy_discovery_duplicate()

    print("\n── 2. AUTOINCREMENT → SERIAL PRIMARY KEY ──")
    fix_autoincrement()

    print("\n── 3. Unused sqlite3 Imports ──")
    fix_unused_sqlite3_imports()

    print("\n── 4. strftime() → PostgreSQL Date Functions ──")
    fix_strftime()

    print("\n" + "=" * 65)
    total_fixes = stats['energy_dup'] + stats['autoincrement'] + stats['sqlite3_import'] + stats['strftime']
    files_count = len(stats['files_modified'])

    if DRY_RUN:
        print(f"  DRY RUN SUMMARY: {total_fixes} fixes across {files_count} files")
        print(f"    Energy duplicate:   {stats['energy_dup']}")
        print(f"    AUTOINCREMENT:      {stats['autoincrement']}")
        print(f"    Unused sqlite3:     {stats['sqlite3_import']}")
        print(f"    strftime():         {stats['strftime']}")
        print(f"\n  To apply: python fix_comprehensive_v2.py --fix")
    else:
        print(f"  APPLIED: {total_fixes} fixes across {files_count} files")
        print(f"    Energy duplicate:   {stats['energy_dup']}")
        print(f"    AUTOINCREMENT:      {stats['autoincrement']}")
        print(f"    Unused sqlite3:     {stats['sqlite3_import']}")
        print(f"    strftime():         {stats['strftime']}")
        print(f"\n  Next: python pre_deploy_check.py")
    print("=" * 65)
