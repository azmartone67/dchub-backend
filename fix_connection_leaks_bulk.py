#!/usr/bin/env python3
"""
Bulk Connection Leak Fixer
==========================
Run in Replit Shell:  python fix_connection_leaks_bulk.py
Dry run first:       python fix_connection_leaks_bulk.py --dry-run

Wraps get_db() calls in try/finally blocks to prevent connection pool exhaustion.

Pattern:
  BEFORE:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(...)
    ...
    conn.close()

  AFTER:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(...)
        ...
    finally:
        conn.close()
"""

import ast
import os
import re
import sys
import shutil
import textwrap
from datetime import datetime

DRY_RUN = '--dry-run' in sys.argv
VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv

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
    'cleanup_deals.py',
    'cleanup_duplicates_v2.py',
    'cleanup_railways.py',
    'delete_duplicates_now.py',
    'final_cleanup.py',
    'fix_capacity.py',
    'fix_duplicates_direct.py',
    'capacity_cleanup.py',
    'global_facilities_expansion.py',
    'db_utils.py',
    'kmz_auto_discovery.py',
}

BACKUP_DIR = '.pg_migration_backups'

# ── Stats ──────────────────────────────────────────────────────────────────

stats = {
    'files_scanned': 0,
    'files_fixed': 0,
    'functions_fixed': 0,
    'already_safe': 0,
    'skipped': 0,
}


def should_skip(filename):
    """Check if file should be skipped."""
    if filename in SKIP_FILES:
        return True
    if '.backup_' in filename or filename.startswith('main.backup'):
        return True
    if re.search(r'\(\d+\)', filename):  # "file (1).py" duplicates
        return True
    return False


def backup_file(filepath):
    """Create a backup before modifying."""
    if DRY_RUN:
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f"{os.path.basename(filepath)}.backup_{ts}"
    shutil.copy2(filepath, os.path.join(BACKUP_DIR, backup_name))


def find_get_db_functions(source, filename):
    """
    Use AST to find functions that call get_db() but don't have try/finally.
    Returns list of (func_name, lineno) tuples.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        if VERBOSE:
            print(f"  ⚠ Syntax error in {filename}, skipping AST parse")
        return []

    unsafe_functions = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        has_get_db = False
        has_try_finally = False
        get_db_var = None

        for child in ast.walk(node):
            # Check for get_db() call
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name) and child.func.id == 'get_db':
                    has_get_db = True
                elif isinstance(child.func, ast.Attribute) and child.func.attr == 'get_db':
                    has_get_db = True

            # Check for try/finally
            if isinstance(child, ast.Try) and child.finalbody:
                has_try_finally = True

        if has_get_db and not has_try_finally:
            unsafe_functions.append((node.name, node.lineno))

    return unsafe_functions


def fix_function_leak(lines, func_start_idx, filename):
    """
    Fix a single function by wrapping get_db() usage in try/finally.
    Uses regex-based approach for reliability.
    Returns (new_lines, was_fixed).
    """
    # Find the function boundaries
    func_line = lines[func_start_idx]
    func_indent = len(func_line) - len(func_line.lstrip())
    body_indent = func_indent + 4  # Standard 4-space indent

    # Find function end (next line at same or lower indent, or EOF)
    func_end_idx = len(lines)
    for i in range(func_start_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.rstrip()
        if not stripped:  # Empty line
            continue
        line_indent = len(line) - len(line.lstrip())
        # If we hit a line at the same or lower indent that's not a decorator
        if line_indent <= func_indent and not stripped.startswith('@') and not stripped.startswith('#'):
            func_end_idx = i
            break

    # Find the get_db() assignment line
    get_db_idx = None
    conn_var = 'conn'
    for i in range(func_start_idx + 1, func_end_idx):
        line = lines[i].strip()
        # Match: conn = get_db() or db = get_db() or connection = get_db()
        m = re.match(r'(\w+)\s*=\s*get_db\(\)', line)
        if m:
            get_db_idx = i
            conn_var = m.group(1)
            break

    if get_db_idx is None:
        return lines, False

    # Find conn.close() line within this function
    close_idx = None
    for i in range(get_db_idx + 1, func_end_idx):
        stripped = lines[i].strip()
        if stripped == f'{conn_var}.close()':
            close_idx = i
            break

    # Check if there's already a try block right after get_db
    next_code_idx = None
    for i in range(get_db_idx + 1, func_end_idx):
        if lines[i].strip():
            next_code_idx = i
            break

    if next_code_idx and lines[next_code_idx].strip().startswith('try:'):
        return lines, False  # Already has try block

    # Build the fix
    get_db_line_indent = len(lines[get_db_idx]) - len(lines[get_db_idx].lstrip())
    indent_str = ' ' * get_db_line_indent
    try_indent = indent_str + '    '

    new_lines = list(lines)

    if close_idx is not None:
        # Case 1: There's a conn.close() - wrap everything between get_db() and close() in try/finally
        # Remove the old conn.close() line
        new_lines[close_idx] = None  # Mark for removal

        # Add try: after get_db()
        # Indent all lines between get_db and close
        for i in range(get_db_idx + 1, close_idx):
            if new_lines[i] is not None and new_lines[i].strip():
                # Add 4 spaces of indent
                current_indent = len(new_lines[i]) - len(new_lines[i].lstrip())
                extra = get_db_line_indent  # Base indent of the get_db line
                relative_indent = current_indent - extra
                if relative_indent < 0:
                    relative_indent = 0
                new_lines[i] = try_indent + ' ' * relative_indent + new_lines[i].strip() + '\n'
            elif new_lines[i] is not None:
                new_lines[i] = '\n'  # Keep blank lines

        # Insert try: after get_db line
        new_lines.insert(get_db_idx + 1, indent_str + 'try:\n')

        # Find where close was and add finally block
        # The close_idx shifted by 1 because we inserted a line
        finally_insert_idx = close_idx + 1  # +1 for the try: we inserted
        new_lines.insert(finally_insert_idx, indent_str + 'finally:\n')
        new_lines.insert(finally_insert_idx + 1, try_indent + f'{conn_var}.close()\n')

        # Remove None entries
        new_lines = [l for l in new_lines if l is not None]
        return new_lines, True

    else:
        # Case 2: No conn.close() found - add try/finally with close
        # Indent all lines from get_db+1 to func_end
        for i in range(get_db_idx + 1, func_end_idx):
            if new_lines[i] is not None and new_lines[i].strip():
                current_indent = len(new_lines[i]) - len(new_lines[i].lstrip())
                extra = get_db_line_indent
                relative_indent = current_indent - extra
                if relative_indent < 0:
                    relative_indent = 0
                new_lines[i] = try_indent + ' ' * relative_indent + new_lines[i].strip() + '\n'
            elif new_lines[i] is not None:
                new_lines[i] = '\n'

        # Insert try: after get_db line
        new_lines.insert(get_db_idx + 1, indent_str + 'try:\n')

        # Insert finally before function end (shifted by 1)
        insert_idx = func_end_idx + 1
        new_lines.insert(insert_idx, indent_str + 'finally:\n')
        new_lines.insert(insert_idx + 1, try_indent + f'{conn_var}.close()\n')

        new_lines = [l for l in new_lines if l is not None]
        return new_lines, True


def fix_file(filepath):
    """Fix all connection leaks in a single file."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()

    filename = os.path.basename(filepath)
    unsafe = find_get_db_functions(source, filename)

    if not unsafe:
        stats['already_safe'] += 1
        return 0

    if VERBOSE or DRY_RUN:
        print(f"\n  📄 {filename}: {len(unsafe)} unsafe function(s)")
        for name, lineno in unsafe:
            print(f"      → {name}() at line {lineno}")

    if DRY_RUN:
        return len(unsafe)

    backup_file(filepath)

    lines = source.split('\n')
    lines = [l + '\n' for l in lines]  # Add newlines back
    if lines and lines[-1] == '\n':
        lines[-1] = ''  # Handle trailing newline

    fixed_count = 0
    # Process from bottom to top so line numbers don't shift
    for func_name, lineno in sorted(unsafe, key=lambda x: x[1], reverse=True):
        func_idx = lineno - 1  # Convert to 0-based
        lines, was_fixed = fix_function_leak(lines, func_idx, filename)
        if was_fixed:
            fixed_count += 1
            if VERBOSE:
                print(f"      ✅ Fixed {func_name}()")

    if fixed_count > 0:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(''.join(lines))
        stats['files_fixed'] += 1

    stats['functions_fixed'] += fixed_count
    return fixed_count


def main():
    print("=" * 60)
    print("  Connection Leak Bulk Fixer")
    print("  Wraps get_db() calls in try/finally blocks")
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
    print(f"  Files scanned:    {stats['files_scanned']}")
    print(f"  Files fixed:      {stats['files_fixed']}")
    print(f"  Functions fixed:  {stats['functions_fixed']}")
    print(f"  Already safe:     {stats['already_safe']}")
    print(f"  Skipped:          {stats['skipped']}")

    if DRY_RUN:
        print("\n  ℹ️  Run without --dry-run to apply fixes")
    else:
        if stats['files_fixed'] > 0:
            print(f"\n  ✅ Backups saved to {BACKUP_DIR}/")
        print("\n  Next: run 'python pre_deploy_check.py' to verify")


if __name__ == '__main__':
    main()
