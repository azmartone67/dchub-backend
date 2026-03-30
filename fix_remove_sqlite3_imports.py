#!/usr/bin/env python3
"""
Remove Unused sqlite3 Imports
==============================
Run in Replit Shell:  python fix_remove_sqlite3_imports.py
Dry run first:       python fix_remove_sqlite3_imports.py --dry-run

Removes 'import sqlite3' lines from files that no longer use sqlite3.
Keeps the import if the file actually references sqlite3.Something or sqlite3.connect().
"""

import os
import re
import sys
import shutil
from datetime import datetime

DRY_RUN = '--dry-run' in sys.argv
VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv

# Files that intentionally use sqlite3 — DO NOT touch
SKIP_FILES = {
    'backfill_sqlite_to_neon.py',
    'db_persistence.py',
    'db_connection_patch.py',
    'db_audit.py',
    'db_write_queue.py',
    'fix_all_sqlite_to_pg.py',
    'fix_connection_leaks.py',
    'fix_connection_leaks_bulk.py',
    'fix_leaks_v2.py',
    'pre_deploy_check.py',
    'railway-sql-fixes.py',
    'fix_remove_sqlite3_imports.py',
    'fix_insert_or_replace.py',
    'dchub-backend-fix.py',
    'crawler_scheduler.py',      # Uses CRAWLER_DB_PATH SQLite
    'fiber_network_discovery.py', # May use local SQLite cache
}

BACKUP_DIR = '.pg_migration_backups'

stats = {
    'files_scanned': 0,
    'imports_removed': 0,
    'imports_kept': 0,
    'skipped': 0,
}


def should_skip(filename):
    if filename in SKIP_FILES:
        return True
    if '.backup_' in filename or filename.startswith('main.backup'):
        return True
    if re.search(r'\(\d+\)', filename):
        return True
    return False


def backup_file(filepath):
    if DRY_RUN:
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f"{os.path.basename(filepath)}.backup_{ts}"
    shutil.copy2(filepath, os.path.join(BACKUP_DIR, backup_name))


def file_uses_sqlite3(lines, import_line_idx):
    """
    Check if sqlite3 is actually used beyond the import line.
    Returns True if sqlite3 is referenced elsewhere in the file.
    """
    for i, line in enumerate(lines):
        if i == import_line_idx:
            continue
        stripped = line.strip()
        # Skip comments
        if stripped.startswith('#'):
            continue
        # Check for actual sqlite3 usage (not in strings that are descriptions)
        if 'sqlite3.' in line:
            # Make sure it's not in a string like "SQLite sqlite3.Row"
            # If it appears as code (e.g., sqlite3.connect, sqlite3.Row, sqlite3.Error)
            if re.search(r'(?<!["\'])sqlite3\.\w+', line):
                return True
        if 'sqlite3' in line and i != import_line_idx:
            # Check for: from sqlite3 import, sqlite3.connect, etc.
            if re.match(r'\s*from\s+sqlite3\s+import', line):
                return True
            # Actual variable usage like: except sqlite3.Error
            if re.search(r'except\s+sqlite3\.', line):
                return True
            if re.search(r'sqlite3\.connect\(', line):
                return True
            if re.search(r'sqlite3\.Row', line):
                return True
    return False


def fix_file(filepath):
    """Remove unused sqlite3 imports from a file."""
    filename = os.path.basename(filepath)

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    # Find import sqlite3 lines
    import_lines = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == 'import sqlite3' or stripped == 'import sqlite3  # noqa':
            import_lines.append(i)

    if not import_lines:
        return 0

    # Check if sqlite3 is actually used
    removable = []
    for idx in import_lines:
        if not file_uses_sqlite3(lines, idx):
            removable.append(idx)
        else:
            stats['imports_kept'] += 1
            if VERBOSE:
                print(f"  ⚡ {filename}: keeping import sqlite3 (still used)")

    if not removable:
        return 0

    if VERBOSE or DRY_RUN:
        for idx in removable:
            print(f"  🗑  {filename} line {idx+1}: removing 'import sqlite3'")

    if DRY_RUN:
        stats['imports_removed'] += len(removable)
        return len(removable)

    backup_file(filepath)

    # Remove the import lines (process from bottom to top)
    for idx in sorted(removable, reverse=True):
        lines.pop(idx)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    stats['imports_removed'] += len(removable)
    return len(removable)


def main():
    print("=" * 60)
    print("  Remove Unused sqlite3 Imports")
    if DRY_RUN:
        print("  🔍 DRY RUN — no files will be modified")
    print("=" * 60)

    py_files = sorted([f for f in os.listdir('.') if f.endswith('.py')])

    for filename in py_files:
        if should_skip(filename):
            stats['skipped'] += 1
            continue

        stats['files_scanned'] += 1
        filepath = os.path.join('.', filename)
        fix_file(filepath)

    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Files scanned:     {stats['files_scanned']}")
    print(f"  Imports removed:   {stats['imports_removed']}")
    print(f"  Imports kept:      {stats['imports_kept']} (sqlite3 still used)")
    print(f"  Skipped:           {stats['skipped']}")

    if DRY_RUN:
        print("\n  ℹ️  Run without --dry-run to apply fixes")
    else:
        print("\n  Next: run 'python pre_deploy_check.py' to verify")


if __name__ == '__main__':
    main()
