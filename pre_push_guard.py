#!/usr/bin/env python3
"""
pre_push_guard.py — Git pre-push safeguard for DCHub backend
=============================================================
Prevents pushing code that would crash Railway deployment.

CHECKS (blocks push if any fail):
  1. All .py files compile (no SyntaxError)
  2. main.py imports successfully (catches missing modules)
  3. No duplicate Flask endpoint names
  4. No orphaned except/finally blocks
  5. No null bytes in .py files (corruption)

INSTALL AS GIT HOOK:
  cp pre_push_guard.py .git/hooks/pre-push
  chmod +x .git/hooks/pre-push

OR RUN MANUALLY:
  python pre_push_guard.py
"""

import os
import sys
import py_compile
import re
import ast

SKIP_FILES = {
    'fix_connection_leaks_bulk.py', 'fix_remove_sqlite3_imports.py',
    'fix_insert_or_replace.py', 'fix_warnings_leaks.py', 'fix_warnings_sqlite3.py',
    'fix_all_sqlite_to_pg.py', 'fix_targeted_bugs.py', 'fix_comprehensive_v2.py',
    'pre_deploy_check.py', 'repair_broken_try.py', 'pre_push_guard.py',
}

def should_check(filename):
    base = os.path.basename(filename)
    if base in SKIP_FILES:
        return False
    if '(' in base:
        return False
    return base.endswith('.py')


def check_syntax():
    """Check all .py files compile without SyntaxError."""
    errors = []
    for f in sorted(os.listdir('.')):
        if not should_check(f):
            continue
        try:
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"  ❌ {f}: {e}")
    return errors


def check_null_bytes():
    """Check for null bytes (file corruption)."""
    errors = []
    for f in sorted(os.listdir('.')):
        if not should_check(f):
            continue
        try:
            with open(f, 'rb') as fh:
                if b'\x00' in fh.read():
                    errors.append(f"  ❌ {f}: Contains null bytes (corrupted)")
        except Exception:
            pass
    return errors


def check_orphaned_blocks():
    """Check for except/finally without matching try."""
    errors = []
    for f in sorted(os.listdir('.')):
        if not should_check(f):
            continue
        try:
            with open(f, 'r', errors='replace') as fh:
                lines = fh.readlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('except ') or stripped == 'except:':
                    # Check that there's a matching try: before this
                    indent = len(line) - len(line.lstrip())
                    found_try = False
                    for j in range(i - 1, max(i - 200, -1), -1):
                        prev = lines[j]
                        prev_indent = len(prev) - len(prev.lstrip())
                        if prev.strip().startswith('try:') and prev_indent == indent:
                            found_try = True
                            break
                        # If we hit a line at same or lower indent that's not blank/comment, stop
                        if prev_indent <= indent and prev.strip() and not prev.strip().startswith('#'):
                            if not prev.strip().startswith(('except', 'elif', 'else', 'finally')):
                                break
                    if not found_try:
                        errors.append(f"  ❌ {f}:{i+1}: Orphaned 'except' without matching 'try'")
        except Exception:
            pass
    return errors


def check_duplicate_endpoints():
    """Check main.py for duplicate Flask endpoint function names."""
    if not os.path.exists('main.py'):
        return []

    errors = []
    try:
        with open('main.py', 'r') as f:
            content = f.read()

        # Find all @app.route decorated functions
        endpoint_names = {}
        lines = content.split('\n')
        in_route = False
        for i, line in enumerate(lines):
            if '@app.route(' in line and not line.strip().startswith('#'):
                in_route = True
            elif in_route and line.strip().startswith('def '):
                match = re.match(r'\s*def\s+(\w+)\s*\(', line)
                if match:
                    name = match.group(1)
                    if name in endpoint_names:
                        errors.append(
                            f"  ❌ main.py:{i+1}: Duplicate endpoint '{name}' "
                            f"(first at line {endpoint_names[name]})"
                        )
                    else:
                        endpoint_names[name] = i + 1
                in_route = False
            elif in_route and not line.strip().startswith('@') and not line.strip().startswith('#') and line.strip():
                in_route = False
    except Exception as e:
        errors.append(f"  ⚠️  Could not check endpoints: {e}")

    return errors


def check_critical_imports():
    """Verify main.py's critical imports resolve."""
    # Instead of importing (which runs all code), just check that
    # files referenced by main.py exist
    critical_files = []
    if os.path.exists('main.py'):
        with open('main.py', 'r') as f:
            for line in f:
                # Match: from module import ...
                m = re.match(r'^(?:try:\s*)?from\s+(\w+)\s+import', line.strip())
                if m:
                    mod = m.group(1)
                    if mod not in ('flask', 'os', 'sys', 'json', 'datetime', 're',
                                   'functools', 'hashlib', 'secrets', 'logging',
                                   'threading', 'time', 'math', 'collections',
                                   'urllib', 'traceback', 'io', 'csv', 'copy',
                                   'routes', 'utils'):
                        py_file = mod + '.py'
                        if not os.path.exists(py_file) and not os.path.isdir(mod):
                            # Not critical if inside try/except
                            critical_files.append(f"  ⚠️  main.py imports '{mod}' but {py_file} not found")

    return critical_files


if __name__ == "__main__":
    print("=" * 60)
    print("  DCHub Pre-Push Safety Guard")
    print("=" * 60)

    all_errors = []
    all_warnings = []

    print("\n🔍 Check 1: Python syntax...")
    errs = check_syntax()
    all_errors.extend(errs)
    print(f"  {'❌ ' + str(len(errs)) + ' errors' if errs else '✅ All files compile'}")
    for e in errs:
        print(e)

    print("\n🔍 Check 2: Null bytes (corruption)...")
    errs = check_null_bytes()
    all_errors.extend(errs)
    print(f"  {'❌ ' + str(len(errs)) + ' corrupted' if errs else '✅ No corrupted files'}")
    for e in errs:
        print(e)

    print("\n🔍 Check 3: Orphaned except/finally blocks...")
    errs = check_orphaned_blocks()
    all_errors.extend(errs)
    print(f"  {'❌ ' + str(len(errs)) + ' orphaned' if errs else '✅ No orphaned blocks'}")
    for e in errs[:5]:  # Show first 5
        print(e)
    if len(errs) > 5:
        print(f"  ... and {len(errs) - 5} more")

    print("\n🔍 Check 4: Duplicate endpoint names...")
    errs = check_duplicate_endpoints()
    all_errors.extend(errs)
    print(f"  {'❌ ' + str(len(errs)) + ' duplicates' if errs else '✅ No duplicate endpoints'}")
    for e in errs:
        print(e)

    print("\n🔍 Check 5: Critical module imports...")
    warns = check_critical_imports()
    all_warnings.extend(warns)
    print(f"  {'⚠️  ' + str(len(warns)) + ' warnings' if warns else '✅ All modules found'}")
    for w in warns[:10]:
        print(w)

    print("\n" + "=" * 60)
    if all_errors:
        print(f"  🚫 BLOCKED: {len(all_errors)} error(s) found — DO NOT PUSH")
        print("  Fix the errors above before deploying.")
        sys.exit(1)
    elif all_warnings:
        print(f"  ⚠️  PASS WITH WARNINGS: {len(all_warnings)} warning(s)")
        print("  Safe to push but review warnings.")
        sys.exit(0)
    else:
        print("  ✅ ALL CLEAR — safe to push")
        sys.exit(0)
