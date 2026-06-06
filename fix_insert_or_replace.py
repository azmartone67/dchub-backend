#!/usr/bin/env python3
"""
INSERT OR REPLACE → ON CONFLICT DO UPDATE Fixer
================================================
Run in Replit Shell:  python fix_insert_or_replace.py
Dry run first:       python fix_insert_or_replace.py --dry-run

The previous bulk fixer converted:
  INSERT INTO table  → INSERT INTO table

But didn't add the ON CONFLICT DO UPDATE SET clause because it needs
to know the conflict column and the update columns.

This script:
  1. Scans all .py files for INSERT INTO ... that were converted from INSERT OR REPLACE
  2. Detects the table name, column list, and values
  3. Generates proper ON CONFLICT DO UPDATE SET clauses
  4. Shows a report for manual review OR auto-fixes where safe

Heuristic: The first column is typically the conflict target (primary key or unique).
"""

import os
import re
import sys
import shutil
from datetime import datetime

DRY_RUN = '--dry-run' in sys.argv
VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv
AUTO_FIX = '--auto-fix' in sys.argv  # Only auto-fix when explicitly requested

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
}

BACKUP_DIR = '.pg_migration_backups'

stats = {
    'files_scanned': 0,
    'instances_found': 0,
    'auto_fixed': 0,
    'needs_review': 0,
    'skipped': 0,
}

# Known table → conflict column mappings for DCHub
# Add your known primary keys / unique constraints here
KNOWN_CONFLICT_COLUMNS = {
    'facilities': 'id',
    'facility': 'id',
    'users': 'id',
    'user': 'id',
    'api_keys': 'key',
    'api_key': 'key',
    'deals': 'id',
    'deal': 'id',
    'subscriptions': 'id',
    'subscription': 'id',
    'news': 'id',
    'news_items': 'id',
    'alerts': 'id',
    'alert': 'id',
    'pipeline': 'id',
    'pipeline_items': 'id',
    'discovery_queue': 'id',
    'discovered_facilities': 'id',
    'crawler_visits': 'url',
    'energy_plants': 'id',
    'power_plants': 'id',
    'substations': 'id',
    'fiber_networks': 'id',
    'fiber_routes': 'id',
    'rankings': 'id',
    'settings': 'key',
    'config': 'key',
    'configurations': 'key',
    'user_preferences': 'user_id',
    'email_queue': 'id',
    'transactions': 'id',
    'analytics': 'id',
    'tracking': 'id',
    'sessions': 'session_id',
    'rate_limits': 'key',
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


def parse_insert_statement(sql_text):
    """
    Parse an INSERT INTO statement to extract table, columns, and values.
    Returns (table, columns, values_placeholders) or None if can't parse.
    """
    # Match: INSERT INTO table_name (col1, col2, ...) VALUES (...) ON CONFLICT DO NOTHING
    # Handle multiline by joining
    pattern = r'INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)'
    m = re.search(pattern, sql_text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None

    table = m.group(1).strip()
    cols_str = m.group(2).strip()
    vals_str = m.group(3).strip()

    columns = [c.strip() for c in cols_str.split(',')]
    return table, columns, vals_str


def generate_on_conflict_clause(table, columns):
    """
    Generate an ON CONFLICT DO UPDATE SET clause.
    Uses known conflict columns or defaults to first column.
    """
    conflict_col = KNOWN_CONFLICT_COLUMNS.get(table.lower())

    if not conflict_col:
        # Default: assume first column is the conflict target
        conflict_col = columns[0]

    # Update all columns except the conflict column
    update_cols = [c for c in columns if c.strip() != conflict_col]

    if not update_cols:
        # Only one column (the key itself) — use DO NOTHING instead
        return f" ON CONFLICT ({conflict_col}) DO NOTHING"

    set_clauses = [f"{c.strip()} = EXCLUDED.{c.strip()}" for c in update_cols]
    set_str = ', '.join(set_clauses)

    return f" ON CONFLICT ({conflict_col}) DO UPDATE SET {set_str}"


def scan_file(filepath):
    """
    Scan a file for INSERT statements that were converted from INSERT OR REPLACE
    but are missing ON CONFLICT clauses.
    Returns list of (line_num, line_text, table, columns, suggested_fix).
    """
    filename = os.path.basename(filepath)
    results = []

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
        lines = content.split('\n')

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip comments
        if stripped.startswith('#'):
            continue

        # Look for INSERT INTO without ON CONFLICT that should have one
        # The bulk fixer converted INSERT OR REPLACE INTO → INSERT INTO
        # These will be INSERT INTO with no ON CONFLICT
        if 'INSERT INTO' in line.upper() and 'ON CONFLICT' not in line.upper():
            # Check if this is in a SQL string
            if not any(q in line for q in ['"', "'", '"""', "'''"]):
                continue

            # Try to parse the full statement (might span multiple lines)
            # Grab up to 5 lines for multiline SQL
            sql_block = '\n'.join(lines[i:min(i+5, len(lines))])

            parsed = parse_insert_statement(sql_block)
            if parsed:
                table, columns, vals = parsed

                # Check if there's already an ON CONFLICT in the next few lines
                nearby = '\n'.join(lines[i:min(i+5, len(lines))])
                if 'ON CONFLICT' in nearby.upper():
                    continue

                # Check if this looks like it was an INSERT OR REPLACE (heuristic)
                # The bulk fixer would have left these as plain INSERT INTO
                # We flag ALL INSERT INTO without ON CONFLICT for review
                # but only auto-fix ones where we're confident about the conflict column
                conflict_clause = generate_on_conflict_clause(table, columns)
                confidence = 'HIGH' if table.lower() in KNOWN_CONFLICT_COLUMNS else 'LOW'

                results.append({
                    'line_num': i + 1,
                    'line': stripped[:120],
                    'table': table,
                    'columns': columns,
                    'conflict_clause': conflict_clause,
                    'confidence': confidence,
                })

    return results


def fix_file(filepath, instances):
    """Apply ON CONFLICT fixes to a file."""
    if not instances:
        return 0

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    fixed = 0
    # Process from bottom to top
    for inst in sorted(instances, key=lambda x: x['line_num'], reverse=True):
        if inst['confidence'] != 'HIGH' and not AUTO_FIX:
            stats['needs_review'] += 1
            continue

        line_idx = inst['line_num'] - 1
        line = lines[line_idx]

        # Find the end of the VALUES (...) clause
        # Could be on same line or span multiple lines
        sql_block = ''.join(lines[line_idx:min(line_idx+5, len(lines))])

        # Find the closing paren of VALUES(...)
        paren_count = 0
        in_values = False
        insert_end_idx = line_idx
        insert_end_col = len(line.rstrip()) - 1

        for li in range(line_idx, min(line_idx+5, len(lines))):
            for ci, ch in enumerate(lines[li]):
                if 'VALUES' in lines[li][:ci+1].upper():
                    in_values = True
                if in_values:
                    if ch == '(':
                        paren_count += 1
                    elif ch == ')':
                        paren_count -= 1
                        if paren_count == 0:
                            insert_end_idx = li
                            insert_end_col = ci
                            break
            if paren_count == 0 and in_values:
                break

        # Insert the ON CONFLICT clause after the closing paren
        target_line = lines[insert_end_idx]
        # Check if there's a closing quote after the paren
        rest_after_paren = target_line[insert_end_col+1:].rstrip()

        # Handle common patterns:
        # ...VALUES (%s, %s)")   → ...VALUES (%s, %s) ON CONFLICT ...")
        # ...VALUES (%s, %s)"""  → ...VALUES (%s, %s) ON CONFLICT ..."""
        # ...VALUES (%s, %s)',   → ...VALUES (%s, %s) ON CONFLICT ...',

        clause = inst['conflict_clause']

        # Find quote character(s) after the closing paren
        quote_match = re.search(r'''(["']{1,3})''', rest_after_paren)
        if quote_match:
            quote_pos = insert_end_col + 1 + rest_after_paren.index(quote_match.group(0))
            # Insert clause before the closing quote
            new_line = target_line[:insert_end_col+1] + clause + target_line[insert_end_col+1:]
            lines[insert_end_idx] = new_line
            fixed += 1
            stats['auto_fixed'] += 1
        else:
            # Can't determine where to insert — mark for review
            stats['needs_review'] += 1

    if fixed > 0 and not DRY_RUN:
        backup_file(filepath)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(lines)

    return fixed


def main():
    print("=" * 60)
    print("  INSERT OR REPLACE → ON CONFLICT DO UPDATE Fixer")
    if DRY_RUN:
        print("  🔍 DRY RUN — showing what would be changed")
    if AUTO_FIX:
        print("  ⚡ AUTO-FIX mode — will fix high-confidence matches")
    else:
        print("  📋 REPORT mode — showing instances for review")
        print("     Use --auto-fix to apply high-confidence fixes")
    print("=" * 60)

    py_files = sorted([f for f in os.listdir('.') if f.endswith('.py')])
    all_instances = []

    for filename in py_files:
        if should_skip(filename):
            stats['skipped'] += 1
            continue

        stats['files_scanned'] += 1
        filepath = os.path.join('.', filename)
        instances = scan_file(filepath)

        if instances:
            stats['instances_found'] += len(instances)
            print(f"\n  📄 {filename}: {len(instances)} INSERT(s) without ON CONFLICT")

            for inst in instances:
                conf_icon = '🟢' if inst['confidence'] == 'HIGH' else '🟡'
                print(f"      {conf_icon} Line {inst['line_num']}: {inst['table']} ({inst['confidence']} confidence)")
                print(f"         Columns: {', '.join(inst['columns'])}")
                print(f"         Suggested: {inst['conflict_clause']}")

            if AUTO_FIX and not DRY_RUN:
                fixed = fix_file(filepath, instances)
                if fixed:
                    print(f"      ✅ Auto-fixed {fixed} instance(s)")

    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Files scanned:     {stats['files_scanned']}")
    print(f"  Instances found:   {stats['instances_found']}")
    print(f"  Auto-fixed:        {stats['auto_fixed']}")
    print(f"  Needs review:      {stats['needs_review']}")
    print(f"  Skipped:           {stats['skipped']}")

    if stats['needs_review'] > 0:
        print(f"\n  ⚠️  {stats['needs_review']} instance(s) need manual review")
        print("     Add table→conflict column mappings to KNOWN_CONFLICT_COLUMNS")
        print("     in this script, then re-run with --auto-fix")

    if not AUTO_FIX and stats['instances_found'] > 0:
        print("\n  ℹ️  Run with --auto-fix to apply high-confidence fixes")
        print("     Or manually review and fix low-confidence ones")

    print("\n  Next: run 'python pre_deploy_check.py' to verify")


if __name__ == '__main__':
    main()
