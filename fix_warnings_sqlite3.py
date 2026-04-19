#!/usr/bin/env python3
"""
sqlite3 Import Warning Fixer v2
================================
Run in Replit Shell:  python fix_warnings_sqlite3.py
Dry run first:       python fix_warnings_sqlite3.py --dry-run

Removes 'import sqlite3' from files that no longer need it.
Also converts 'except sqlite3.Error' → 'except Exception' when sqlite3
is only used for exception handling.

Uses AST for accurate detection of actual sqlite3 code usage vs string references.
"""

import ast
import os
import re
import sys
import shutil
from datetime import datetime

DRY_RUN = '--dry-run' in sys.argv
VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv

SKIP_FILES = {
    'backfill_sqlite_to_neon.py', 'db_persistence.py', 'db_connection_patch.py',
    'db_audit.py', 'db_write_queue.py', 'fix_all_sqlite_to_pg.py',
    'fix_connection_leaks.py', 'fix_connection_leaks_bulk.py', 'fix_leaks_v2.py',
    'pre_deploy_check.py', 'railway-sql-fixes.py', 'fix_remove_sqlite3_imports.py',
    'fix_insert_or_replace.py', 'fix_warnings_leaks.py', 'fix_warnings_sqlite3.py',
    'crawler_scheduler.py',  # Uses CRAWLER_DB_PATH SQLite intentionally
}

BACKUP_DIR = '.pg_migration_backups'

stats = {
    'files_scanned': 0, 'imports_removed': 0, 'excepts_converted': 0,
    'imports_kept': 0, 'skipped': 0,
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
    shutil.copy2(filepath, os.path.join(BACKUP_DIR, f"{os.path.basename(filepath)}.backup_{ts}"))


def analyze_sqlite3_usage(source):
    """
    Use AST to find actual sqlite3 code references (not strings/comments).
    Returns:
      'none' — sqlite3 not used in code at all
      'except_only' — only used in except clauses (except sqlite3.Error etc)
      'code' — used in actual code (sqlite3.connect, sqlite3.Row, etc)
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 'code'  # Can't parse, play safe

    usages = []

    for node in ast.walk(tree):
        # Check for sqlite3.Something attribute access
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == 'sqlite3':
                # Determine context: is this in an except handler?
                usages.append(('attr', node.attr, node.lineno))

        # Check for bare 'sqlite3' name reference (not as module in attribute)
        if isinstance(node, ast.Name) and node.id == 'sqlite3':
            # Could be: import sqlite3 (skip), except sqlite3.X (keep tracking)
            usages.append(('name', 'sqlite3', node.lineno))

    if not usages:
        return 'none'

    # Now check if all usages are in except handlers
    # Parse again to find except handler line ranges
    except_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type:
                # Get the line range of the except handler type expression
                except_ranges.append(node.type.lineno)

    # Check each attribute usage
    code_usages = []
    except_usages = []
    for usage_type, name, lineno in usages:
        if usage_type == 'attr':
            if lineno in except_ranges:
                except_usages.append((name, lineno))
            else:
                code_usages.append((name, lineno))

    if code_usages:
        return 'code'
    elif except_usages:
        return 'except_only'
    else:
        return 'none'


def fix_file(filepath):
    """Fix sqlite3 imports in a file."""
    filename = os.path.basename(filepath)

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()

    lines = source.split('\n')

    # Find import sqlite3 lines
    import_indices = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == 'import sqlite3' or stripped.startswith('import sqlite3 '):
            import_indices.append(i)

    if not import_indices:
        return 0

    usage = analyze_sqlite3_usage(source)

    if usage == 'code':
        stats['imports_kept'] += 1
        if VERBOSE:
            print(f"  ⚡ {filename}: keeping import (sqlite3 used in code)")
        return 0

    changes = []

    if usage == 'none':
        # Just remove the import line(s)
        for idx in import_indices:
            changes.append(('remove', idx, None))
            if VERBOSE or DRY_RUN:
                print(f"  🗑  {filename} line {idx+1}: removing 'import sqlite3' (unused)")
        stats['imports_removed'] += len(import_indices)

    elif usage == 'except_only':
        # Convert except sqlite3.Error/OperationalError → except Exception
        # Then remove import
        for i, line in enumerate(lines):
            m = re.match(r'^(\s*)except\s+sqlite3\.(\w+)(\s+as\s+\w+)%s:', line)
            if m:
                indent = m.group(1)
                as_clause = m.group(3) or ''
                new_line = f"{indent}except Exception{as_clause}:"
                changes.append(('replace', i, new_line))
                stats['excepts_converted'] += 1
                if VERBOSE or DRY_RUN:
                    print(f"  🔄 {filename} line {i+1}: except sqlite3.{m.group(2)} → except Exception")

        for idx in import_indices:
            changes.append(('remove', idx, None))
            if VERBOSE or DRY_RUN:
                print(f"  🗑  {filename} line {idx+1}: removing 'import sqlite3'")
        stats['imports_removed'] += len(import_indices)

    if not changes:
        return 0

    if DRY_RUN:
        return len(changes)

    backup_file(filepath)

    # Apply changes
    # First do replacements
    for action, idx, new_val in changes:
        if action == 'replace':
            lines[idx] = new_val

    # Then remove lines (from bottom to top)
    remove_indices = sorted([idx for action, idx, _ in changes if action == 'remove'], reverse=True)
    for idx in remove_indices:
        lines.pop(idx)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return len(changes)


def main():
    print("=" * 60)
    print("  sqlite3 Import Warning Fixer v2 (AST-based)")
    if DRY_RUN:
        print("  🔍 DRY RUN — no files will be modified")
    print("=" * 60)

    py_files = sorted([f for f in os.listdir('.') if f.endswith('.py')])

    for filename in py_files:
        if should_skip(filename):
            stats['skipped'] += 1
            continue
        stats['files_scanned'] += 1
        fix_file(os.path.join('.', filename))

    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Files scanned:      {stats['files_scanned']}")
    print(f"  Imports removed:    {stats['imports_removed']}")
    print(f"  Excepts converted:  {stats['excepts_converted']} (sqlite3.Error → Exception)")
    print(f"  Imports kept:       {stats['imports_kept']} (sqlite3 still used in code)")
    print(f"  Skipped:            {stats['skipped']}")

    if DRY_RUN:
        print("\n  ℹ️  Run without --dry-run to apply fixes")
    else:
        print("\n  Next: run 'python pre_deploy_check.py' to verify")


if __name__ == '__main__':
    main()
