#!/usr/bin/env python3
"""
DC Hub Pre-Deploy Lint Check
=============================
Run this BEFORE every deploy to catch common bugs:
  - SQLite syntax left over from the PostgreSQL migration
  - Connection leaks (missing finally blocks)
  - Missing rollbacks in except handlers
  - SQLite-only functions in SQL strings
  - INSERT OR IGNORE (not valid PostgreSQL)

Usage:
    python pre_deploy_check.py                    # Check all .py files in current dir
    python pre_deploy_check.py main.py routes.py  # Check specific files
"""

import re
import sys
import os
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# RULES — each rule is (pattern, description, severity, exceptions)
# ═══════════════════════════════════════════════════════════════

RULES = [
    # --- SQLite placeholder syntax ---
    {
        'pattern': r"VALUES\s*\(\s*\?",
        'description': "SQLite '?' placeholder in VALUES — use '%s' for PostgreSQL",
        'severity': 'ERROR',
        'exclude_comments': True,
        'exclude_patterns': ['CRAWLER_DB_PATH', 'sqlite3', '# SQLite'],  # Intentional SQLite usage
    },
    {
        'pattern': r"WHERE\s+\w+\s*[=><!]+\s*\?",
        'description': "SQLite '?' placeholder in WHERE — use '%s' for PostgreSQL",
        'severity': 'ERROR',
        'exclude_comments': True,
        'exclude_patterns': ['CRAWLER_DB_PATH', 'sqlite3', '# SQLite', 'crawler_visits'],
    },
    {
        'pattern': r"SET\s+\w+\s*=\s*\?",
        'description': "SQLite '?' placeholder in SET — use '%s' for PostgreSQL",
        'severity': 'ERROR',
        'exclude_comments': True,
        'exclude_patterns': ['CRAWLER_DB_PATH', 'sqlite3', '# SQLite'],
    },

    # --- Direct connection.execute() instead of cursor.execute() ---
    {
        'pattern': r"(?:conn|db)\.(execute|executemany)\(",
        'description': "Direct conn.execute() — PostgreSQL requires cursor.execute()",
        'severity': 'ERROR',
        'exclude_comments': True,
        'exclude_patterns': ['CRAWLER_DB_PATH', 'sqlite3', '# SQLite', 'row_factory', 'crawler_visits', 'init_crawler_db'],
    },

    # --- SQLite-only SQL functions ---
    {
        'pattern': r"datetime\s*\(\s*['\"]now['\"]\s*\)",
        'description': "SQLite datetime('now') — use NOW() or CURRENT_TIMESTAMP for PostgreSQL",
        'severity': 'ERROR',
        'exclude_comments': True,
    },
    {
        'pattern': r"strftime\s*\(",
        'description': "SQLite strftime() in SQL — use to_char() or date_trunc() for PostgreSQL",
        'severity': 'WARNING',
        'exclude_comments': True,
        'exclude_patterns': ['datetime.', '.strftime(', 'time.strftime'],  # Python strftime is fine
    },

    # --- INSERT OR IGNORE / INSERT OR REPLACE ---
    {
        'pattern': r"INSERT\s+OR\s+IGNORE",
        'description': "SQLite INSERT OR IGNORE — use INSERT...ON CONFLICT DO NOTHING",
        'severity': 'ERROR',
        'exclude_comments': True,
    },
    {
        'pattern': r"INSERT\s+OR\s+REPLACE",
        'description': "SQLite INSERT OR REPLACE — use INSERT...ON CONFLICT DO UPDATE",
        'severity': 'ERROR',
        'exclude_comments': True,
    },

    # --- import sqlite3 in PostgreSQL code ---
    {
        'pattern': r"^\s*import\s+sqlite3",
        'description': "Importing sqlite3 — verify this isn't used with PostgreSQL connections",
        'severity': 'WARNING',
        'exclude_comments': True,
    },
    {
        'pattern': r"row_factory\s*=\s*sqlite3",
        'description': "sqlite3.Row factory — not compatible with psycopg2 connections",
        'severity': 'ERROR',
        'exclude_comments': True,
    },

    # --- Connection leak patterns ---
    {
        'pattern': r"conn\.close\(\)\s*$",
        'description': "conn.close() outside finally block? Verify it's in a finally clause",
        'severity': 'INFO',
        'exclude_comments': True,
    },

    # --- Missing rollback risk ---
    {
        'pattern': r"except.*(?:Exception|BaseException).*:\s*\n\s*(?:logger|print|pass)",
        'description': "Exception handler without rollback — may cause cascading 'transaction aborted' errors",
        'severity': 'WARNING',
        'exclude_comments': False,
    },

    # --- AUTOINCREMENT (SQLite-only) ---
    {
        'pattern': r"AUTOINCREMENT",
        'description': "SQLite AUTOINCREMENT — use SERIAL or BIGSERIAL for PostgreSQL",
        'severity': 'WARNING',
        'exclude_comments': True,
        'exclude_patterns': ['CRAWLER_DB_PATH', 'sqlite3', '# SQLite', 'crawler_visits', 'init_crawler_db'],
    },

    # --- Mixed placeholder danger ---
    {
        'pattern': r"%s.*\?|\?.*%s",
        'description': "CRITICAL: Mixed %s and ? placeholders in same statement",
        'severity': 'ERROR',
        'exclude_comments': True,
    },
]


# ═══════════════════════════════════════════════════════════════
# CHECKER
# ═══════════════════════════════════════════════════════════════

class Colors:
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    BOLD = '\033[1m'
    RESET = '\033[0m'


def check_file(filepath):
    """Check a single file for all rules. Returns list of findings."""
    findings = []
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception as e:
        return [{'file': filepath, 'line': 0, 'severity': 'ERROR', 'message': f'Could not read: {e}'}]

    for rule in RULES:
        pattern = re.compile(rule['pattern'], re.IGNORECASE if 'IGNORECASE' in rule.get('flags', '') else 0)

        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if rule.get('exclude_comments') and stripped.startswith('#'):
                continue

            # Skip excluded patterns (intentional SQLite usage)
            if any(exc in line for exc in rule.get('exclude_patterns', [])):
                continue

            if pattern.search(line):
                findings.append({
                    'file': filepath,
                    'line': i,
                    'severity': rule['severity'],
                    'message': rule['description'],
                    'code': stripped[:120],
                })

    return findings


def check_connection_leaks(filepath):
    """Advanced check: find get_db() calls without finally blocks."""
    findings = []
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            lines = content.split('\n')
    except Exception:
        return findings

    # Find functions that call get_db() or get_pg_connection()
    func_pattern = re.compile(r'^(def\s+\w+)\s*\(', re.MULTILINE)
    db_call_pattern = re.compile(r'(?:get_db|get_pg_connection|psycopg2\.connect)\s*\(')
    finally_pattern = re.compile(r'^\s*finally\s*:', re.MULTILINE)

    functions = list(func_pattern.finditer(content))

    # Pool/wrapper functions that manage connections — not consumers
    skip_funcs = {'def get_db', 'def get_pg_connection', 'def try_get_pg_connection',
                  'def get_read_db', 'def _get_db', 'def _db', 'def _get_pg_connection',
                  'def _reset_all_pools', 'def return_pg_connection', 'def pg_connection',
                  'def try_get_db'}

    for idx, match in enumerate(functions):
        func_start = match.start()
        func_end = functions[idx + 1].start() if idx + 1 < len(functions) else len(content)
        func_body = content[func_start:func_end]
        func_name = match.group(1)

        # Skip pool/wrapper functions and SQLite functions
        if func_name in skip_funcs:
            continue
        if 'CRAWLER_DB_PATH' in func_body or 'sqlite3' in func_body:
            continue

        # Check if the db call is real (not just in a comment) and not a context manager
        has_real_db_call = False
        for line in func_body.split('\n'):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue  # Skip comment-only lines
            if db_call_pattern.search(line):
                # Check if it's in a 'with' context manager (auto-closes)
                if re.search(r'\bwith\b.*(?:get_db|get_pg_connection|pg_connection)\s*\(', line):
                    continue
                has_real_db_call = True
                break

        if has_real_db_call and not finally_pattern.search(func_body):
            line_no = content[:func_start].count('\n') + 1
            findings.append({
                'file': filepath,
                'line': line_no,
                'severity': 'WARNING',
                'message': f'{func_name}() calls get_db() but has no finally block — potential connection leak',
                'code': func_name,
            })

    return findings


def main():
    # Determine which files to check
    if len(sys.argv) > 1:
        files = [f for f in sys.argv[1:] if f.endswith('.py') and os.path.isfile(f)]
    else:
        # Check all .py files in current directory (not recursively into old copies)
        files = sorted(Path('.').glob('*.py'))

    if not files:
        print(f"{Colors.YELLOW}No Python files found to check.{Colors.RESET}")
        sys.exit(0)

    # Files to skip entirely (backup files, intentional SQLite, utility scripts)
    SKIP_FILES = {
        'backfill_sqlite_to_neon.py',      # Intentionally reads FROM SQLite
        'db_persistence.py',                # SQLite export/import bridge
        'db_connection_patch.py',           # Legacy SQLite connection wrapper
        'db_audit.py',                      # One-time audit script
        'db_write_queue.py',                # Low-level abstraction
        'fix_all_sqlite_to_pg.py',          # Migration fixer script
        'fix_connection_leaks.py',          # Utility script
        'fix_leaks_v2.py',                  # Utility script
        'dchub-backend-fix.py',             # One-time fix script
        'cleanup_deals.py',                 # One-time cleanup
        'cleanup_duplicates_v2.py',         # One-time cleanup
        'cleanup_railways.py',              # One-time cleanup
        'delete_duplicates_now.py',         # One-time cleanup
        'final_cleanup.py',                 # One-time cleanup
        'fix_capacity.py',                  # One-time fix
        'fix_duplicates_direct.py',         # One-time fix
        'capacity_cleanup.py',              # One-time cleanup
        'global_facilities_expansion.py',   # One-time expansion script
        'db_utils.py',                      # Contains SQL translation maps (not SQL itself)
        'kmz_auto_discovery.py',            # Comment-only references
        'pre_deploy_check.py',              # This lint script (contains error descriptions as strings)
        'railway-sql-fixes.py',             # One-time fix script
        'fix_all_sqlite_to_pg.py',          # Migration fixer script
        'fix_connection_leaks_bulk.py',     # Connection leak fixer
        'fix_remove_sqlite3_imports.py',    # sqlite3 import cleaner
        'fix_insert_or_replace.py',         # INSERT OR REPLACE fixer
    }

    all_findings = []
    for filepath in files:
        filepath = str(filepath)
        basename = os.path.basename(filepath)
        # Skip numbered backup copies like "file (1).py"
        if re.search(r'\(\d+\)', filepath):
            continue
        # Skip backup files
        if '.backup_' in basename or basename.startswith('main.backup'):
            continue
        # Skip excluded utility/intentional-SQLite files
        if basename in SKIP_FILES:
            continue
        findings = check_file(filepath)
        findings += check_connection_leaks(filepath)
        all_findings.extend(findings)

    # Sort by severity
    severity_order = {'ERROR': 0, 'WARNING': 1, 'INFO': 2}
    all_findings.sort(key=lambda f: (severity_order.get(f['severity'], 3), f['file'], f['line']))

    # Print results
    errors = sum(1 for f in all_findings if f['severity'] == 'ERROR')
    warnings = sum(1 for f in all_findings if f['severity'] == 'WARNING')
    infos = sum(1 for f in all_findings if f['severity'] == 'INFO')

    print(f"\n{Colors.BOLD}{'='*70}")
    print(f"  DC Hub Pre-Deploy Check — {len(files)} files scanned")
    print(f"{'='*70}{Colors.RESET}\n")

    if not all_findings:
        print(f"  {Colors.GREEN}✅ All clear! No issues found.{Colors.RESET}\n")
        sys.exit(0)

    for finding in all_findings:
        sev = finding['severity']
        if sev == 'ERROR':
            color = Colors.RED
            icon = '❌'
        elif sev == 'WARNING':
            color = Colors.YELLOW
            icon = '⚠️'
        else:
            color = Colors.CYAN
            icon = 'ℹ️'

        print(f"  {icon} {color}{sev}{Colors.RESET}  {finding['file']}:{finding['line']}")
        print(f"     {finding['message']}")
        if finding.get('code'):
            print(f"     {Colors.CYAN}{finding['code']}{Colors.RESET}")
        print()

    # Summary
    print(f"{Colors.BOLD}{'─'*70}")
    print(f"  Summary: {Colors.RED}{errors} errors{Colors.RESET}, "
          f"{Colors.YELLOW}{warnings} warnings{Colors.RESET}, "
          f"{Colors.CYAN}{infos} info{Colors.RESET}")
    print(f"{'─'*70}{Colors.RESET}\n")

    if errors > 0:
        print(f"  {Colors.RED}{Colors.BOLD}🚫 DEPLOY BLOCKED — fix {errors} error(s) first{Colors.RESET}\n")
        sys.exit(1)
    elif warnings > 0:
        print(f"  {Colors.YELLOW}⚠️  Deploy OK but review {warnings} warning(s){Colors.RESET}\n")
        sys.exit(0)
    else:
        print(f"  {Colors.GREEN}✅ Ready to deploy{Colors.RESET}\n")
        sys.exit(0)


if __name__ == '__main__':
    main()
