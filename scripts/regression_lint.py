#!/usr/bin/env python3
"""Phase 22 regression lint.

Catches the 5 bug patterns we hit repeatedly across Phases 9–20:
  1. URL with literal '%s'           — leftover from f-string mistakes
  2. INSERT without ON CONFLICT      — risks duplicate rows on retry
  3. sys.exit() inside async runner  — kills the worker, not the task
  4. Duplicate route registration    — Flask uses whichever loads first
  5. urllib.request.urlopen          — IPv6 fail on Railway, use requests

Usage: python3 scripts/regression_lint.py [paths...]
Exit code: 0 clean, 1 violations found.
"""
import os, re, sys, pathlib, ast, collections

VIOLATIONS = []

def add(path, line, rule, msg):
    VIOLATIONS.append({'path': str(path), 'line': line, 'rule': rule, 'msg': msg})


def lint_url_format(p, src):
    for i, line in enumerate(src.split('\n'), 1):
        # /api/v1/foo/%s/bar inside a string literal that's not an f-string
        if re.search(r"['\"]/api/[^'\"]*%s[^'\"]*['\"]", line) and 'f"' not in line and "f'" not in line:
            add(p, i, 'url-format-typo', 'literal "%s" in URL — likely meant an f-string')


def lint_insert_on_conflict(p, src):
    # INSERT INTO foo... missing ON CONFLICT
    for m in re.finditer(r"INSERT\s+INTO\s+(\w+)[^;]*?(?:;|\"\"\"|\"|'''|')", src, re.I | re.S):
        body = m.group(0)
        if 'ON CONFLICT' not in body.upper() and 'EXCLUDED' not in body.upper():
            line = src[:m.start()].count('\n') + 1
            # Whitelist tables that legitimately should NOT use ON CONFLICT
            tbl = m.group(1).lower()
            if tbl in {'mcp_tool_calls', 'observability_metrics', 'daily_anomalies', 'audit_log'}:
                continue
            add(p, line, 'insert-no-on-conflict',
                f'INSERT INTO {tbl} without ON CONFLICT — risks duplicates on retry')


def lint_sys_exit_in_async(p, src):
    # sys.exit() inside a function that has 'async def' or scheduled job decorator
    if 'sys.exit' not in src: return
    try:
        tree = ast.parse(src)
    except Exception:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef,)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if isinstance(sub.func.value, ast.Name) and sub.func.value.id == 'sys' and sub.func.attr == 'exit':
                        add(p, sub.lineno, 'sys-exit-in-async',
                            f'sys.exit() inside async function {node.name} — kills the worker')


def lint_urllib_request(p, src):
    for i, line in enumerate(src.split('\n'), 1):
        if re.search(r"\burllib\.request\.urlopen\b", line):
            add(p, i, 'urllib-request-on-railway',
                'urllib.request.urlopen — defaults to IPv6, fails on Railway. Use `requests` instead.')


def lint_duplicate_routes(targets):
    seen = collections.defaultdict(list)
    pat = re.compile(r"@\w+\.route\s*\(\s*['\"]([^'\"]+)['\"]")
    for p in targets:
        try:
            src = pathlib.Path(p).read_text()
        except Exception:
            continue
        for m in pat.finditer(src):
            line = src[:m.start()].count('\n') + 1
            seen[m.group(1)].append((str(p), line))
    for path, hits in seen.items():
        if len(hits) > 1:
            for fp, ln in hits:
                add(fp, ln, 'duplicate-route',
                    f'route {path!r} registered in {len(hits)} places: '
                    + ', '.join(f'{a}:{b}' for a, b in hits))


def main():
    roots = sys.argv[1:] or ['.']
    targets = []
    for r in roots:
        for dp, dirs, files in os.walk(r):
            if any(s in dp for s in ('.git', 'node_modules', '__pycache__', '.venv')):
                continue
            for f in files:
                if f.endswith('.py'):
                    targets.append(pathlib.Path(dp) / f)

    for p in targets:
        try:
            src = p.read_text()
        except Exception:
            continue
        lint_url_format(p, src)
        lint_insert_on_conflict(p, src)
        lint_sys_exit_in_async(p, src)
        lint_urllib_request(p, src)

    lint_duplicate_routes(targets)

    if not VIOLATIONS:
        print("✓ regression lint clean")
        return 0

    by_rule = collections.defaultdict(list)
    for v in VIOLATIONS:
        by_rule[v['rule']].append(v)

    for rule, items in sorted(by_rule.items()):
        print(f"\n[{rule}] {len(items)} violation(s):")
        for v in items[:10]:
            print(f"  {v['path']}:{v['line']}  {v['msg']}")
        if len(items) > 10:
            print(f"  ... +{len(items)-10} more")

    print(f"\nTOTAL: {len(VIOLATIONS)} violation(s) across {len(by_rule)} rule(s)")
    return 1


if __name__ == '__main__':
    sys.exit(main())
