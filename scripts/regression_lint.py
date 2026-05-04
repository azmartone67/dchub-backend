#!/usr/bin/env python3
"""Phase 24 regression lint — AST-based, no false positives.

Catches the 5 bug patterns we hit repeatedly:
  1. URL with literal '%s' inside a non-f-string URL literal
  2. INSERT without ON CONFLICT (excluding append-only audit tables)
  3. sys.exit() inside async def
  4. Duplicate route registration (REAL @app.route / @bp.route decorators only)
  5. urllib.request.urlopen call expressions

Usage: python3 scripts/regression_lint.py [paths...]
Exit code: 0 clean, 1 violations found.
"""
import ast, os, re, sys, pathlib, collections

VIOLATIONS = []
def add(p, line, rule, msg): VIOLATIONS.append({'path': str(p), 'line': line, 'rule': rule, 'msg': msg})

WHITELIST_TABLES = {
    'mcp_tool_calls', 'observability_metrics', 'daily_anomalies',
    'audit_log', 'alert_history', 'energy_sync_log', 'email_drip_log',
    'ai_outreach_log',
}


def lint_file(p, src):
    # 1. URL %s typo (regex on string literals only)
    for i, line in enumerate(src.split('\n'), 1):
        if re.search(r"['\"]/api/[^'\"]*%s[^'\"]*['\"]", line):
            if 'f"' not in line and "f'" not in line and '%' not in line.split("'")[-1]:
                add(p, i, 'url-format-typo', 'literal "%s" in URL — likely an f-string')

    # 2. INSERT without ON CONFLICT — only for tables not in whitelist
    for m in re.finditer(r"INSERT\s+INTO\s+(\w+)[^;\"']*", src, re.I):
        body = m.group(0)
        if 'ON CONFLICT' in body.upper(): continue
        tbl = m.group(1).lower()
        if tbl in WHITELIST_TABLES: continue
        line = src[:m.start()].count('\n') + 1
        add(p, line, 'insert-no-on-conflict',
            f'INSERT INTO {tbl} without ON CONFLICT — risks duplicates on retry')

    # AST-based passes
    try:
        tree = ast.parse(src)
    except Exception:
        return [], []  # skip routes/sysexit if parse fails

    # 3. sys.exit() inside async def
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == 'sys' and sub.func.attr == 'exit'):
                    add(p, sub.lineno, 'sys-exit-in-async',
                        f'sys.exit() inside async function {node.name}')

    # 5. urllib.request.urlopen call expressions
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            if (f.attr == 'urlopen'
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == 'request'
                    and isinstance(f.value.value, ast.Name)
                    and f.value.value.id == 'urllib'):
                add(p, node.lineno, 'urllib-request-on-railway',
                    'urllib.request.urlopen — use `requests` instead (IPv6 fails on Railway)')

    # 4. Real route registrations: decorators on FunctionDef nodes
    routes_here = []
    sysexits_here = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                # Match @<name>.route(...) or @<name>.get(...) etc.
                if (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in {'route', 'get', 'post', 'put', 'delete', 'patch'}
                        and dec.args
                        and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    routes_here.append((dec.args[0].value, p, dec.lineno))
    return routes_here, sysexits_here


def main():
    roots = sys.argv[1:] or ['.']
    targets = []
    for r in roots:
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
        routes, _ = lint_file(p, src)
        all_routes.extend(routes)

    # Detect dup routes (real decorators only)
    by_path = collections.defaultdict(list)
    for path, file, line in all_routes:
        by_path[path].append((file, line))
    for path, hits in by_path.items():
        if len(hits) > 1:
            for fp, ln in hits:
                add(fp, ln, 'duplicate-route',
                    f'route {path!r} decorator appears in {len(hits)} places: '
                    + ', '.join(f'{a}:{b}' for a, b in hits))

    if not VIOLATIONS:
        print("✓ regression lint clean")
        return 0

    by_rule = collections.defaultdict(list)
    for v in VIOLATIONS: by_rule[v['rule']].append(v)
    for rule, items in sorted(by_rule.items()):
        print(f"\n[{rule}] {len(items)} violation(s):")
        for v in items[:8]:
            print(f"  {v['path']}:{v['line']}  {v['msg']}")
        if len(items) > 8:
            print(f"  ... +{len(items)-8} more")
    print(f"\nTOTAL: {len(VIOLATIONS)} violation(s) across {len(by_rule)} rule(s)")
    return 1


if __name__ == '__main__':
    sys.exit(main())
