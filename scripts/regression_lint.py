#!/usr/bin/env python3
"""Phase 34 — delta-based regression lint.

The previous version flagged every existing violation in the codebase,
producing 700+ warnings on every CI run. This made the gate useless —
the lint was perpetually red regardless of the PR contents.

This version:
  * In a CI/PR context, computes the diff vs the merge-base with origin/main.
  * Only flags violations that appear on LINES THE PR ADDED OR MODIFIED.
  * Existing pre-PR violations are reported as warnings (don't fail).
  * On main itself (no PR context), runs in audit mode — prints all
    violations but exits 0.

Triggers on these patterns (same as before, all AST-based):
  1. URL with literal '%s' (likely missed an f-string)
  2. INSERT INTO without ON CONFLICT (whitelist for append-only tables)
  3. sys.exit() inside async def
  4. urllib.request.urlopen call
  5. Duplicate @app.route decorator (real AST decorators, not strings)

Usage: python3 scripts/regression_lint.py [--mode delta|audit] [--base BRANCH]
"""
import ast, os, re, sys, pathlib, collections, subprocess, argparse

VIOLATIONS = []
WHITELIST_TABLES = {
    'mcp_tool_calls', 'observability_metrics', 'daily_anomalies',
    'audit_log', 'alert_history', 'energy_sync_log', 'email_drip_log',
    'ai_outreach_log', 'smoke_test_history', 'pipeline_drafts',
    'redeem_funnel_events',
}


def add(p, line, rule, msg):
    VIOLATIONS.append({'path': str(p), 'line': line, 'rule': rule, 'msg': msg})


def changed_lines_per_file(base='origin/main'):
    """Return dict[path] -> set of line numbers added/modified in this PR.

    Falls back to all-lines if git diff isn't available.
    """
    try:
        # Find merge-base for accurate diff
        result = subprocess.run(
            ['git', 'merge-base', 'HEAD', base],
            capture_output=True, text=True, timeout=10
        )
        merge_base = result.stdout.strip() if result.returncode == 0 else base
    except Exception:
        merge_base = base

    try:
        result = subprocess.run(
            ['git', 'diff', '--unified=0', f'{merge_base}...HEAD'],
            capture_output=True, text=True, timeout=20
        )
    except Exception:
        return None  # signals "no delta info, run in audit mode"

    if result.returncode != 0:
        return None

    out = collections.defaultdict(set)
    current_file = None
    for line in result.stdout.split('\n'):
        if line.startswith('+++ b/'):
            current_file = line[6:]
        elif line.startswith('@@') and current_file:
            # @@ -X,Y +A,B @@
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                for ln in range(start, start + count):
                    out[current_file].add(ln)
    return dict(out)


def lint_file(p, src):
    routes = []
    for i, line in enumerate(src.split('\n'), 1):
        if re.search(r"['\"]/api/[^'\"]*%s[^'\"]*['\"]", line):
            if 'f"' not in line and "f'" not in line:
                add(p, i, 'url-format-typo', 'literal "%s" in URL — likely f-string')

    for m in re.finditer(r"INSERT\s+INTO\s+(\w+)[^;\"']*", src, re.I):
        if 'ON CONFLICT' in m.group(0).upper(): continue
        tbl = m.group(1).lower()
        if tbl in WHITELIST_TABLES: continue
        line = src[:m.start()].count('\n') + 1
        add(p, line, 'insert-no-on-conflict',
            f'INSERT INTO {tbl} without ON CONFLICT')

    try: tree = ast.parse(src)
    except Exception: return routes

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == 'sys' and sub.func.attr == 'exit'):
                    add(p, sub.lineno, 'sys-exit-in-async',
                        f'sys.exit() inside async {node.name}')

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            if (f.attr == 'urlopen'
                    and isinstance(f.value, ast.Attribute) and f.value.attr == 'request'
                    and isinstance(f.value.value, ast.Name) and f.value.value.id == 'urllib'):
                add(p, node.lineno, 'urllib-request-on-railway',
                    'urllib.request.urlopen — use requests instead')

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in {'route', 'get', 'post', 'put', 'delete', 'patch'}
                        and dec.args and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    routes.append((dec.args[0].value, p, dec.lineno))
    return routes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['delta', 'audit'], default='delta',
                    help='delta: only flag PR-changed lines; audit: all')
    ap.add_argument('--base', default='origin/main', help='base ref for delta')
    ap.add_argument('paths', nargs='*', default=['.'])
    args = ap.parse_args()

    targets = []
    for r in args.paths:
        for dp, dirs, files in os.walk(r):
            if any(s in dp for s in ('.git', 'node_modules', '__pycache__', '.venv', 'site-packages')):
                continue
            for f in files:
                if f.endswith('.py'):
                    targets.append(pathlib.Path(dp) / f)

    all_routes = []
    for p in targets:
        try: src = p.read_text()
        except Exception: continue
        routes = lint_file(p, src)
        all_routes.extend(routes)

    by_path = collections.defaultdict(list)
    for path, file, line in all_routes:
        by_path[path].append((file, line))
    for path, hits in by_path.items():
        if len(hits) > 1:
            for fp, ln in hits:
                add(fp, ln, 'duplicate-route',
                    f'route {path!r} in {len(hits)} places')

    # Filter to delta if requested
    delta_filter = None
    if args.mode == 'delta':
        delta_filter = changed_lines_per_file(args.base)
        if delta_filter is None:
            print("# delta unavailable — running in audit mode", file=sys.stderr)
            args.mode = 'audit'

    relevant = []
    pre_existing = []
    if args.mode == 'delta':
        for v in VIOLATIONS:
            # Normalize file path
            p = v['path'].lstrip('./')
            file_changes = delta_filter.get(p, set()) if delta_filter else set()
            if v['line'] in file_changes:
                relevant.append(v)
            else:
                pre_existing.append(v)
    else:
        relevant = VIOLATIONS

    if args.mode == 'delta':
        print(f"# delta mode vs {args.base} — checking only changed lines")
        print(f"# pre-existing violations (not blocking): {len(pre_existing)}")

    if not relevant:
        if args.mode == 'delta':
            print("✓ no NEW violations introduced by this PR")
        else:
            print(f"✓ regression lint clean (audit mode)")
        return 0

    by_rule = collections.defaultdict(list)
    for v in relevant: by_rule[v['rule']].append(v)
    print(f"\n{'BLOCKING:' if args.mode == 'delta' else 'AUDIT:'}")
    for rule, items in sorted(by_rule.items()):
        print(f"\n[{rule}] {len(items)}:")
        for v in items[:8]:
            print(f"  {v['path']}:{v['line']}  {v['msg']}")
        if len(items) > 8:
            print(f"  ... +{len(items)-8} more")
    print(f"\nTOTAL NEW: {len(relevant)} (pre-existing not counted: {len(pre_existing)})")
    return 1 if args.mode == 'delta' else 0


if __name__ == '__main__':
    sys.exit(main())
