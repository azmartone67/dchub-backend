#!/usr/bin/env python3
"""
Connection Leak Fixer v2 — Line-Based Approach
===============================================
Run in Replit Shell:  python fix_warnings_leaks.py
Dry run first:       python fix_warnings_leaks.py --dry-run

Wraps get_db() calls in try/finally blocks.
Skips getter functions that return the connection.
Uses pure line-based parsing (no AST) for reliability.
"""

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
    'dchub-backend-fix.py', 'cleanup_deals.py', 'cleanup_duplicates_v2.py',
    'cleanup_railways.py', 'delete_duplicates_now.py', 'final_cleanup.py',
    'fix_capacity.py', 'fix_duplicates_direct.py', 'capacity_cleanup.py',
    'global_facilities_expansion.py', 'db_utils.py', 'kmz_auto_discovery.py',
}

BACKUP_DIR = '.pg_migration_backups'

stats = {
    'files_scanned': 0, 'files_fixed': 0, 'functions_fixed': 0,
    'getters_skipped': 0, 'already_safe': 0, 'skipped': 0,
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


def find_function_end(lines, func_start, func_indent_len):
    """Find the last line of a function (exclusive index)."""
    for i in range(func_start + 1, len(lines)):
        line = lines[i]
        stripped = line.rstrip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        # Hit a line at same or lower indent that starts a new block
        if indent <= func_indent_len and (
            stripped.startswith('def ') or
            stripped.startswith('class ') or
            stripped.startswith('@') or
            (indent == 0 and not stripped.startswith('#'))
        ):
            return i
    return len(lines)


def fix_file(filepath):
    """Fix all connection leaks in a file."""
    filename = os.path.basename(filepath)

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    # Find all functions and their get_db() calls
    targets = []
    i = 0
    while i < len(lines):
        m = re.match(r'^(\s*)def\s+(\w+)\s*\(', lines[i])
        if not m:
            i += 1
            continue

        func_indent = m.group(1)
        func_name = m.group(2)
        func_indent_len = len(func_indent)
        body_indent = func_indent + '    '
        func_end = find_function_end(lines, i, func_indent_len)

        # Search for get_db() call in function body
        get_db_idx = None
        conn_var = None
        for j in range(i + 1, func_end):
            gm = re.match(r'^(\s*)(\w+)\s*=\s*(?:get_db|self\.get_db)\(\)', lines[j])
            if gm:
                get_db_idx = j
                conn_var = gm.group(2)
                break

        if get_db_idx is None or conn_var is None:
            i += 1
            continue

        # Check if it's a getter function (returns the connection variable)
        is_getter = False
        for j in range(get_db_idx + 1, func_end):
            if re.search(r'\breturn\s+' + re.escape(conn_var) + r'\b', lines[j]):
                is_getter = True
                break

        if is_getter:
            stats['getters_skipped'] += 1
            i = func_end
            continue

        # Check if already has try: after get_db()
        has_try = False
        for j in range(get_db_idx + 1, min(get_db_idx + 4, func_end)):
            if lines[j].strip() == 'try:':
                has_try = True
                break

        if has_try:
            stats['already_safe'] += 1
            i = func_end
            continue

        targets.append({
            'func_name': func_name,
            'func_start': i,
            'func_end': func_end,
            'get_db_idx': get_db_idx,
            'conn_var': conn_var,
            'body_indent': body_indent,
        })

        i = func_end

    if not targets:
        return 0

    if VERBOSE or DRY_RUN:
        print(f"\n  📄 {filename}: {len(targets)} function(s) to fix")
        for t in targets:
            print(f"      → {t['func_name']}() line {t['get_db_idx']+1}")

    if DRY_RUN:
        return len(targets)

    backup_file(filepath)

    # Apply fixes from bottom to top so line numbers don't shift
    fixed = 0
    for t in sorted(targets, key=lambda x: x['get_db_idx'], reverse=True):
        get_db_idx = t['get_db_idx']
        func_end = t['func_end']
        conn_var = t['conn_var']
        body_indent = t['body_indent']
        try_indent = body_indent + '    '

        # Find existing conn.close() in function
        close_idx = None
        for j in range(get_db_idx + 1, func_end):
            stripped = lines[j].strip()
            if stripped == f'{conn_var}.close()':
                close_idx = j
                break
            # Also match: try: conn.close()
            if stripped == f'try: {conn_var}.close()':
                close_idx = j
                break

        # Determine the range to wrap in try block
        wrap_start = get_db_idx + 1
        wrap_end = close_idx if close_idx is not None else func_end

        # Remove existing close line if found
        if close_idx is not None:
            # Check if there's an except after the close (like try: conn.close() / except: pass)
            # In that case remove those lines too
            remove_lines = {close_idx}
            if close_idx + 1 < func_end:
                next_stripped = lines[close_idx + 1].strip() if close_idx + 1 < len(lines) else ''
                if next_stripped.startswith('except'):
                    remove_lines.add(close_idx + 1)
                    if close_idx + 2 < len(lines) and lines[close_idx + 2].strip() == 'pass':
                        remove_lines.add(close_idx + 2)
        else:
            remove_lines = set()

        # Build the new lines
        new_lines = []

        # Keep everything up to and including get_db line
        new_lines.extend(lines[:wrap_start])

        # Add try:
        new_lines.append(body_indent + 'try:\n')

        # Add indented body (skip removed lines)
        for j in range(wrap_start, wrap_end):
            if j in remove_lines:
                continue
            line = lines[j]
            if line.strip():  # Non-empty line
                # Calculate relative indent from body_indent
                current_indent = len(line) - len(line.lstrip())
                relative = current_indent - len(body_indent)
                if relative < 0:
                    relative = 0
                new_lines.append(try_indent + ' ' * relative + line.lstrip())
            else:
                new_lines.append('\n')

        # Also include any lines between close_idx and func_end that we removed
        for j in range(wrap_end, func_end):
            if j in remove_lines:
                continue
            line = lines[j]
            if line.strip():
                current_indent = len(line) - len(line.lstrip())
                relative = current_indent - len(body_indent)
                if relative < 0:
                    relative = 0
                new_lines.append(try_indent + ' ' * relative + line.lstrip())
            else:
                new_lines.append('\n')

        # Add finally block
        new_lines.append(body_indent + 'finally:\n')
        new_lines.append(try_indent + f'{conn_var}.close()\n')

        # Add everything after the function
        skip_to = func_end
        for j in range(wrap_end, func_end + 1):
            if j in remove_lines:
                skip_to = max(skip_to, j + 1)
        new_lines.extend(lines[skip_to:])

        lines = new_lines
        fixed += 1

        if VERBOSE:
            print(f"      ✅ Fixed {t['func_name']}()")

    if fixed > 0:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        stats['files_fixed'] += 1

    stats['functions_fixed'] += fixed
    return fixed


def main():
    print("=" * 60)
    print("  Connection Leak Fixer v2 (line-based)")
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
    print(f"  Files scanned:     {stats['files_scanned']}")
    print(f"  Files fixed:       {stats['files_fixed']}")
    print(f"  Functions fixed:   {stats['functions_fixed']}")
    print(f"  Getters skipped:   {stats['getters_skipped']} (return conn — intentional)")
    print(f"  Already safe:      {stats['already_safe']} (already have try/finally)")
    print(f"  Skipped:           {stats['skipped']}")

    if DRY_RUN:
        print("\n  ℹ️  Run without --dry-run to apply fixes")
    else:
        if stats['files_fixed'] > 0:
            print(f"\n  ✅ Backups saved to {BACKUP_DIR}/")
        print("\n  Next: run 'python pre_deploy_check.py' to verify")


if __name__ == '__main__':
    main()
