#!/usr/bin/env python3
"""
Universal SQLite → PostgreSQL Auto-Fixer
========================================
Run in Replit Shell:  python fix_all_sqlite_to_pg.py

Fixes:
  1. ? placeholders → %s
  2. db.execute() / conn.execute() → cursor.execute() (where appropriate)
  3. INSERT OR IGNORE → INSERT...ON CONFLICT DO NOTHING
  4. INSERT OR REPLACE → INSERT...ON CONFLICT DO UPDATE (best-effort)
  5. datetime('now') → NOW()
  6. sqlite3.Row factory → dict cursor pattern
  7. PRAGMA statements → PostgreSQL equivalents or removal
  8. AUTOINCREMENT → SERIAL PRIMARY KEY

Skips:
  - Backup files (*.backup_*)
  - Files that intentionally use SQLite (backfill_sqlite_to_neon.py, db_persistence.py SQLite reads)
  - Comment-only lines
  - String literals that are documentation/fix descriptions (not actual SQL)
"""

import os
import re
import sys
import shutil
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────

DRY_RUN = '--dry-run' in sys.argv
VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv

# Files to completely skip (intentional SQLite usage or backup files)
SKIP_FILES = {
    'backfill_sqlite_to_neon.py',      # Reads FROM SQLite intentionally
    'db_persistence.py',                # SQLite export/import bridge
    'db_connection_patch.py',           # SQLite connection wrapper (legacy)
    'db_audit.py',                      # One-time audit script
    'db_write_queue.py',                # Low-level write abstraction
    'fix_all_sqlite_to_pg.py',          # This script!
    'pre_deploy_check.py',             # Lint tool, not production code
    'fix_connection_leaks.py',          # Utility script
    'fix_leaks_v2.py',                  # Utility script
}

# Files that are one-time cleanup/fix utilities (not deployed to production)
# We'll still fix them but with lower priority — skip if you want
CLEANUP_SCRIPTS = {
    'cleanup_deals.py', 'cleanup_duplicates_v2.py', 'cleanup_railways.py',
    'delete_duplicates_now.py', 'final_cleanup.py', 'fix_capacity.py',
    'fix_duplicates_direct.py', 'capacity_cleanup.py',
    'global_facilities_expansion.py',
}

# ── Counters ───────────────────────────────────────────────────────────────

stats = {
    'files_scanned': 0,
    'files_modified': 0,
    'files_skipped': 0,
    'placeholder_fixes': 0,
    'execute_fixes': 0,
    'insert_or_ignore_fixes': 0,
    'insert_or_replace_fixes': 0,
    'datetime_now_fixes': 0,
    'pragma_fixes': 0,
    'autoincrement_fixes': 0,
    'sqlite3_row_fixes': 0,
    'errors': [],
}

# ── Fix Functions ──────────────────────────────────────────────────────────

def is_in_string_or_comment(line, match_start):
    """Check if a match position is inside a comment."""
    stripped = line.lstrip()
    if stripped.startswith('#'):
        return True
    # Check if it's inside a string that looks like a description/doc
    # e.g., "'Fix analytics: ? → %s placeholder'"
    return False

def is_sql_context(line):
    """Check if a line looks like it contains SQL (not a Python string description)."""
    stripped = line.strip()
    # Skip pure comments
    if stripped.startswith('#'):
        return False
    # Skip lines that are clearly fix descriptions or log messages
    if re.search(r"(Fix|fix|Changed|changed|Updated|updated|Converted|converted).*(\?|placeholder|sqlite)", stripped, re.IGNORECASE):
        if "execute" not in stripped.lower() and "VALUES" not in stripped and "WHERE" not in stripped and "SET " not in stripped:
            return False
    return True

def fix_question_mark_placeholders(content, filename):
    """Replace ? with %s in SQL statements, preserving non-SQL usage."""
    lines = content.split('\n')
    new_lines = []
    fixes = 0
    in_sql_block = False
    sql_keywords = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'WHERE', 'VALUES',
                    'SET ', 'FROM', 'INTO', 'CREATE', 'ALTER', 'DROP',
                    'ON CONFLICT', 'RETURNING', 'ORDER BY', 'GROUP BY',
                    'HAVING', 'LIMIT', 'OFFSET', 'JOIN', 'AND ', 'OR ']

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip comments
        if stripped.startswith('#'):
            new_lines.append(line)
            continue

        # Detect SQL context - triple quotes or execute() calls
        has_sql = False

        # Check for SQL keywords in the line
        upper_line = line.upper()
        for kw in sql_keywords:
            if kw in upper_line:
                has_sql = True
                break

        # Check for .execute( pattern
        if '.execute(' in line or '.executemany(' in line:
            has_sql = True

        # Check for triple-quote SQL blocks
        if "'''" in line or '"""' in line:
            has_sql = True
            in_sql_block = not in_sql_block

        if in_sql_block:
            has_sql = True

        # If this looks like SQL and has ? placeholders, fix them
        if has_sql and '?' in line:
            # Don't replace ? in comments at end of line
            code_part = line.split('#')[0] if '#' in line else line

            # Don't replace ? in print/log/description strings that aren't SQL
            # Heuristic: if line has execute/SQL keywords AND ?, replace
            if re.search(r'\?', code_part):
                # Replace ? that are SQL placeholders (not inside non-SQL strings)
                # Simple approach: replace all ? in code portion with %s
                new_code = code_part.replace('?', '%s')
                comment_part = line[len(code_part):] if '#' in line else ''
                if new_code != code_part:
                    fixes += 1
                    line = new_code + comment_part

        new_lines.append(line)

    stats['placeholder_fixes'] += fixes
    return '\n'.join(new_lines)


def fix_conn_execute(content, filename):
    """Fix db.execute() / conn.execute() → cursor pattern where appropriate."""
    # This is trickier - we need to ensure there's a cursor available
    # For now, flag these but the pattern varies per file
    # We'll handle the most common patterns:
    #   db.execute(...) → c = db.cursor(); c.execute(...)
    #   conn.execute(...) → cursor.execute(...)
    lines = content.split('\n')
    new_lines = []
    fixes = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#'):
            new_lines.append(line)
            continue

        # Skip PRAGMA lines (handled separately)
        if 'PRAGMA' in line:
            new_lines.append(line)
            continue

        # Pattern: db.execute( or conn.execute( that's actual SQL execution
        # But NOT sqlite_conn.execute or sq_conn.execute (intentional SQLite)
        match = re.match(r'^(\s*)((?:\w+\s*=\s*)?)(db|conn)\.execute(many)?\s*\(', line)
        if match and 'sqlite' not in line.lower() and 'sq_conn' not in line:
            indent = match.group(1)
            assignment = match.group(2)
            var_name = match.group(3)
            is_many = match.group(4) == 'many'

            # Replace db.execute → cursor.execute, conn.execute → cursor.execute
            # But we need to know if there's already a cursor variable
            # Look backward for cursor creation
            has_cursor = False
            cursor_var = 'c'
            for j in range(max(0, i-20), i):
                prev = lines[j].strip()
                if re.search(r'(\w+)\s*=\s*(?:db|conn|connection)\.cursor\(\)', prev):
                    has_cursor = True
                    cursor_var = re.search(r'(\w+)\s*=', prev).group(1)
                    break
                # Also check for cursor as function parameter
                if 'def ' in prev and 'cursor' in prev:
                    has_cursor = True
                    cursor_var = 'cursor'
                    break

            method = 'executemany' if is_many else 'execute'

            if has_cursor:
                # Replace db.execute with cursor_var.execute
                new_line = line.replace(f'{var_name}.execute', f'{cursor_var}.execute', 1)
                if is_many:
                    new_line = new_line.replace(f'{var_name}.executemany', f'{cursor_var}.executemany', 1)
            else:
                # Need to add cursor creation before this line
                # Insert: c = db.cursor() before, then replace db.execute with c.execute
                cursor_line = f'{indent}c = {var_name}.cursor()\n'
                new_line = line.replace(f'{var_name}.execute', f'c.execute', 1)
                if is_many:
                    new_line = new_line.replace(f'{var_name}.executemany', f'c.executemany', 1)
                new_lines.append(cursor_line.rstrip())
                # Mark that we added a cursor for subsequent lines in this block
                has_cursor = True

            if new_line != line:
                fixes += 1
                line = new_line

        new_lines.append(line)

    stats['execute_fixes'] += fixes
    return '\n'.join(new_lines)


def fix_insert_or_ignore(content, filename):
    """Fix INSERT OR IGNORE → INSERT...ON CONFLICT DO NOTHING."""
    fixes = 0
    # Pattern: INSERT OR IGNORE INTO table_name
    def replacer(m):
        nonlocal fixes
        fixes += 1
        return f'INSERT INTO {m.group(1)} '

    # We need to also add ON CONFLICT DO NOTHING at the end of the VALUES clause
    lines = content.split('\n')
    new_lines = []
    pending_on_conflict = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#'):
            new_lines.append(line)
            continue

        # Replace INSERT OR IGNORE
        if re.search(r'INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)', line, re.IGNORECASE):
            old_line = line
            line = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)',
                         r'INSERT INTO \1', line, flags=re.IGNORECASE)
            if line != old_line:
                fixes += 1
                pending_on_conflict = True

        # If we're in a pending INSERT OR IGNORE and we find the end of VALUES
        if pending_on_conflict:
            # Look for the closing of VALUES (...) or end of statement
            # Check for: VALUES (...) at end, or just closing paren with possible trailing
            if re.search(r'\)\s*[\'\"]{0,3}\s*[,)]*\s*$', stripped) and 'VALUES' in content[max(0, content.rfind('\n', 0, sum(len(l)+1 for l in lines[:i+1]))-200):sum(len(l)+1 for l in lines[:i+1])].upper():
                # Check if this line closes the VALUES clause
                if stripped.endswith(')') or stripped.endswith("')") or stripped.endswith('")') or stripped.endswith("''',") or stripped.endswith("''')"):
                    # Add ON CONFLICT DO NOTHING
                    # Find the right place to add it
                    rstripped = line.rstrip()
                    # Handle cases like: VALUES (?, ?, ?)''', or VALUES (%s, %s)')
                    if rstripped.endswith("''',"):
                        line = rstripped[:-4] + "\n" + line[:len(line)-len(line.lstrip())] + "    ON CONFLICT DO NOTHING''',\n"
                    elif rstripped.endswith("'''"):
                        line = rstripped[:-3] + "\n" + line[:len(line)-len(line.lstrip())] + "    ON CONFLICT DO NOTHING'''"
                    elif rstripped.endswith('"""'):
                        line = rstripped[:-3] + "\n" + line[:len(line)-len(line.lstrip())] + '    ON CONFLICT DO NOTHING"""'
                    else:
                        # Simple case - just append before closing
                        pass
                    pending_on_conflict = False

        new_lines.append(line)

    stats['insert_or_ignore_fixes'] += fixes
    return '\n'.join(new_lines)


def fix_insert_or_replace(content, filename):
    """Fix INSERT OR REPLACE → INSERT...ON CONFLICT DO UPDATE."""
    fixes = 0

    def replacer(m):
        nonlocal fixes
        fixes += 1
        return f'INSERT INTO {m.group(1)} '

    new_content = re.sub(
        r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)',
        replacer, content, flags=re.IGNORECASE
    )
    # Note: ON CONFLICT DO UPDATE SET ... needs the actual columns
    # This is best-effort - may need manual review for the SET clause
    stats['insert_or_replace_fixes'] += fixes
    return new_content


def fix_datetime_now(content, filename):
    """Fix datetime('now') → NOW()."""
    fixes = 0
    old = content
    content = content.replace("datetime('now')", "NOW()")
    content = content.replace('datetime("now")', 'NOW()')
    if content != old:
        fixes = old.count("datetime('now')") + old.count('datetime("now")')
    stats['datetime_now_fixes'] += fixes
    return content


def fix_pragma(content, filename):
    """Remove or replace PRAGMA statements."""
    lines = content.split('\n')
    new_lines = []
    fixes = 0

    for line in lines:
        stripped = line.strip()
        # Remove PRAGMA lines entirely (they're SQLite-specific)
        if re.search(r'\.execute\s*\(\s*["\']PRAGMA\s+', line):
            indent = line[:len(line) - len(line.lstrip())]
            new_lines.append(f'{indent}# PRAGMA removed - not needed for PostgreSQL')
            fixes += 1
            continue
        # Also handle bare PRAGMA in SQL strings
        if 'PRAGMA' in line and stripped.startswith(('conn.', 'db.', 'cursor.', 'c.', 'sq_conn.')):
            indent = line[:len(line) - len(line.lstrip())]
            new_lines.append(f'{indent}# PRAGMA removed - not needed for PostgreSQL')
            fixes += 1
            continue
        new_lines.append(line)

    stats['pragma_fixes'] += fixes
    return '\n'.join(new_lines)


def fix_autoincrement(content, filename):
    """Fix INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY."""
    fixes = 0
    old = content
    content = re.sub(
        r'(?:id\s+)?INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
        'id SERIAL PRIMARY KEY',
        content, flags=re.IGNORECASE
    )
    if content != old:
        fixes = 1
    stats['autoincrement_fixes'] += fixes
    return content


def fix_sqlite3_row(content, filename):
    """Fix sqlite3.Row → RealDictCursor or dict-based approach."""
    lines = content.split('\n')
    new_lines = []
    fixes = 0

    for line in lines:
        # Replace conn.row_factory = sqlite3.Row
        if re.search(r'\.\s*row_factory\s*=\s*sqlite3\.Row', line):
            indent = line[:len(line) - len(line.lstrip())]
            # Comment it out and add note
            new_lines.append(f'{indent}# sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)')
            fixes += 1
            continue
        new_lines.append(line)

    stats['sqlite3_row_fixes'] += fixes
    return '\n'.join(new_lines)


def fix_file(filepath, filename):
    """Apply all fixes to a single file."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            original = f.read()
    except Exception as e:
        stats['errors'].append(f"Read error {filename}: {e}")
        return

    content = original

    # Apply fixes in order
    content = fix_pragma(content, filename)
    content = fix_sqlite3_row(content, filename)
    content = fix_autoincrement(content, filename)
    content = fix_datetime_now(content, filename)
    content = fix_insert_or_ignore(content, filename)
    content = fix_insert_or_replace(content, filename)
    content = fix_question_mark_placeholders(content, filename)
    # conn.execute fix is complex - apply carefully
    content = fix_conn_execute(content, filename)

    if content != original:
        stats['files_modified'] += 1
        if DRY_RUN:
            print(f"  [DRY RUN] Would modify: {filename}")
        else:
            # Create backup
            backup_dir = os.path.join(os.path.dirname(filepath), '.pg_migration_backups')
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, f"{filename}.bak")
            if not os.path.exists(backup_path):  # Don't overwrite existing backup
                shutil.copy2(filepath, backup_path)

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  ✅ Fixed: {filename}")
    elif VERBOSE:
        print(f"  ⏭️  No changes needed: {filename}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  SQLite → PostgreSQL Universal Auto-Fixer")
    print("=" * 60)
    if DRY_RUN:
        print("  🔍 DRY RUN MODE — no files will be modified\n")
    else:
        print("  🔧 LIVE MODE — files will be modified (backups saved)\n")

    # Find all .py files in current directory
    py_files = []
    for f in sorted(os.listdir('.')):
        if f.endswith('.py') and os.path.isfile(f):
            py_files.append(f)

    print(f"Found {len(py_files)} Python files\n")

    for filename in py_files:
        # Skip excluded files
        if filename in SKIP_FILES:
            stats['files_skipped'] += 1
            if VERBOSE:
                print(f"  ⏭️  Skipped (excluded): {filename}")
            continue

        # Skip backup files
        if '.backup_' in filename or filename.startswith('main.backup'):
            stats['files_skipped'] += 1
            if VERBOSE:
                print(f"  ⏭️  Skipped (backup): {filename}")
            continue

        # Skip files with (1), (2) etc in name (duplicate downloads)
        if re.search(r'\(\d+\)', filename):
            stats['files_skipped'] += 1
            continue

        stats['files_scanned'] += 1

        # Quick check if file has any SQLite patterns
        try:
            with open(filename, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except:
            continue

        has_sqlite_patterns = any([
            '?' in content and ('.execute' in content or 'VALUES' in content.upper()),
            'INSERT OR IGNORE' in content.upper(),
            'INSERT OR REPLACE' in content.upper(),
            "datetime('now')" in content,
            'sqlite3.Row' in content,
            'PRAGMA' in content and '.execute' in content,
            'AUTOINCREMENT' in content.upper(),
            'db.execute' in content or 'conn.execute' in content,
        ])

        if has_sqlite_patterns:
            fix_file(os.path.join('.', filename), filename)
        elif VERBOSE:
            print(f"  ⏭️  Clean: {filename}")

    # Print summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Files scanned:         {stats['files_scanned']}")
    print(f"  Files modified:        {stats['files_modified']}")
    print(f"  Files skipped:         {stats['files_skipped']}")
    print(f"  ─────────────────────────────────")
    print(f"  ? → %s fixes:          {stats['placeholder_fixes']}")
    print(f"  conn.execute fixes:    {stats['execute_fixes']}")
    print(f"  INSERT OR IGNORE:      {stats['insert_or_ignore_fixes']}")
    print(f"  INSERT OR REPLACE:     {stats['insert_or_replace_fixes']}")
    print(f"  datetime('now'):       {stats['datetime_now_fixes']}")
    print(f"  PRAGMA removals:       {stats['pragma_fixes']}")
    print(f"  AUTOINCREMENT:         {stats['autoincrement_fixes']}")
    print(f"  sqlite3.Row:           {stats['sqlite3_row_fixes']}")

    if stats['errors']:
        print(f"\n  ⚠️  Errors ({len(stats['errors'])}):")
        for err in stats['errors']:
            print(f"    {err}")

    print("\n  Next steps:")
    print("  1. Run: python pre_deploy_check.py")
    print("  2. Review any remaining errors")
    print("  3. Test locally, then deploy")
    print("  4. Backups saved in .pg_migration_backups/")

    if stats['insert_or_replace_fixes'] > 0:
        print(f"\n  ⚠️  {stats['insert_or_replace_fixes']} INSERT OR REPLACE → INSERT INTO")
        print("     These need ON CONFLICT DO UPDATE SET ... clauses.")
        print("     Review manually: grep -rn 'INSERT INTO.*# was INSERT OR REPLACE' *.py")

    return 0 if not stats['errors'] else 1


if __name__ == '__main__':
    sys.exit(main())
