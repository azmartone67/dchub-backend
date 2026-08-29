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
  6. cursor-shadow — `with conn.cursor() as cur:` shadowing a
     function-wide cursor (HARD rule: always blocking, see HARD_RULES)

Usage: python3 scripts/regression_lint.py [--mode delta|audit] [--base BRANCH]
"""
import ast, os, re, sys, pathlib, collections, subprocess, argparse

VIOLATIONS = []
WHITELIST_TABLES = {
    'mcp_tool_calls', 'observability_metrics', 'daily_anomalies',
    'audit_log', 'alert_history', 'energy_sync_log', 'email_drip_log',
    'ai_outreach_log', 'smoke_test_history', 'pipeline_drafts',
    'redeem_funnel_events',
    # brain_llm_usage is append-only LLM usage/cache telemetry (shell #31,
    # serial PK, no natural key). The SQL carries ON CONFLICT DO NOTHING
    # anyway, but split across adjacent string fragments — this rule's regex
    # window stops at the first quote, so it can't see it.
    'brain_llm_usage',
    # slow_requests is the same append-only class (shell #32 perf capture) —
    # and the same adjacent-fragment blindness applies to its ON CONFLICT.
    'slow_requests',
    # Digest wave 2026-07-27, same append-only class: relay_opens (human-relay
    # open log; carries fragment-split ON CONFLICT), entitlement_repairs
    # (admin audit trail), autopilot_recidivism_escalations (escalate-once
    # ledger; its ON CONFLICT upsert is also fragment-split).
    'relay_opens', 'entitlement_repairs', 'autopilot_recidivism_escalations',
    # media_story_queue is the operator review queue (serial PK, no natural
    # key): each detection run writes a distinct draft row, queued OR rejected
    # -with-reason, and re-queue suppression is the lane's own cooldown query
    # (media_expansion_stories._on_cooldown) plus the publish-guard dedup —
    # there is no column set an ON CONFLICT could target. Same append-only
    # class as pipeline_drafts above; media_data_story_factory's identical
    # INSERT predates delta enforcement.
    'media_story_queue',
    # white_glove_agent_runs is the onboarding agent's cadence ledger (serial
    # PK, 2026-08-29): one row per run recording each lane's verdict. Same
    # append-only class as white_glove_runs above — two runs on the same day
    # are two distinct observations, and an ON CONFLICT collapsing them would
    # erase the earlier verdict set, which is the whole record. The agent's
    # OTHER writes (brain findings) go through upsert_brain_finding and ARE
    # idempotent; this ledger deliberately is not.
    'white_glove_agent_runs',
    # brain_actuator_runs is the autonomy shell's fire ledger (BIGSERIAL PK,
    # 2026-08-17): one row per actuator fire, carrying the rollback payload
    # written BEFORE the mutation. It is deliberately append-only and has no
    # natural key — two fires of the same actuator on the same day are two
    # distinct events, and an ON CONFLICT that collapsed them would destroy
    # the earlier fire's rollback record. The table is ALSO the budget ledger
    # (_budget_ok counts live rows in 24h), so an upsert would silently reset
    # the budget instead of spending it.
    'brain_actuator_runs',
    # brain_action_class_runs is the squasher action-class run ledger
    # (BIGSERIAL PK, 2026-08-22, claim loop step 2): one row per execution
    # carrying the pre/post verifier counts, and — exactly like
    # brain_actuator_runs above — the per-day budget counter. Two runs of one
    # class against one row are two events; an upsert would erase the earlier
    # run's evidence and silently reset the budget.
    'brain_action_class_runs',
    # market_power_scores is upserted via an explicit UPDATE-or-INSERT
    # (the INSERT only runs when the UPDATE matched 0 rows) — not an
    # accidental bare insert. ON CONFLICT was removed deliberately
    # because the live table's slug uniqueness wasn't enforceable.
    'market_power_scores',
    # eia_retail_rates is a full-refresh mirror of eia_electricity_rates
    # (DELETE + INSERT...SELECT each EIA ingest) — a bulk rebuild, not an
    # upsert, so ON CONFLICT semantics don't apply.
    'eia_retail_rates',
    # mcp_unlock_tokens: every INSERT is a freshly-minted random PK
    # (secrets.token_urlsafe) gated by a prior SELECT — a conflict is
    # statistically impossible, ON CONFLICT would be noise.
    'mcp_unlock_tokens',
    # market_movement_snapshots / market_movement_events: append-only
    # time-series logs (one row per market per detection pass / one row
    # per detected movement). No natural key to conflict on — they're
    # history, not state, so ON CONFLICT semantics don't apply.
    'market_movement_snapshots',
    'market_movement_events',
    # conversion_loop_snapshots: append-only conversion-loop health log (one
    # row per master-tick, BIGSERIAL PK). History, not state — no natural key.
    'conversion_loop_snapshots',
    # welcome_email_log: append-only send-attempt log (SERIAL PK, one row per
    # attempt — 'skipped_duplicate' rows are part of the record). The
    # founder-note reservation INSERT dedupes via WHERE NOT EXISTS, which is
    # the intended semantics (no natural key to conflict on).
    'welcome_email_log',
    # shell#41 WS6 change-capture spine: entity_changes is the append-only
    # event log (one row per detected appearance / changed field) and
    # entity_capture_runs the append-only run ledger. History, not state —
    # no natural key to conflict on. (entity_state IS state and carries its
    # own ON CONFLICT (layer, entity_key) DO UPDATE, so it is not listed.)
    'entity_changes', 'entity_capture_runs',
    # shell#41 WS6 queue-delta capture. TWO DIFFERENT REASONS, kept distinct so
    # a future reader does not assume both are append-only:
    #   interconnect_queue_runs   — genuinely append-only (one row per ingest
    #     run, the ledger that makes `loaded_at` interpretable). No natural key.
    #   interconnect_queue_events — DOES carry `ON CONFLICT DO NOTHING`
    #     (routes/iso_queue_ingest.py:1432). The rule cannot see it: its window
    #     regex stops at the first quote, so a clause living in a later string
    #     fragment is invisible — the same blindness documented at lines 40/43/45.
    #     Whitelisted because the rule is wrong here, NOT because the guard is
    #     missing. If that INSERT is ever rewritten as one string, drop this.
    'interconnect_queue_runs', 'interconnect_queue_events',
}

# ── HARD rules (r-fixpack 2026-07-02) ────────────────────────────────
# Rules listed here are ALWAYS blocking: they fail the run in BOTH delta
# and audit mode, regardless of whether the offending line was touched
# by the current PR. Reserve HARD status for bug classes that have
# shipped repeatedly AND have a zero-violation baseline today (so the
# gate stays green until someone actually reintroduces the bug).
HARD_RULES = {'cursor-shadow'}

# Inline opt-out for hard rules: append `# lint-ok: <rule>` to the
# flagged line after a human has verified it is safe, e.g.
#   with conn.cursor() as cur:  # lint-ok: cursor-shadow (no outer cur)
CURSOR_SHADOW_OPTOUT = 'lint-ok: cursor-shadow'


def add(p, line, rule, msg):
    VIOLATIONS.append({'path': str(p), 'line': line, 'rule': rule, 'msg': msg})


def lint_cursor_shadow(p, src):
    """cursor-shadow — routes/funnel_health.py shipped the SAME bug three
    times (last fixed in commit 2ac9b095): an inner
    `with conn.cursor() as cur:` shadows the function-wide cursor `cur`
    opened earlier in the function, and the with-block CLOSES it on exit,
    so every later cur.execute() raises InterfaceError('cursor already
    closed') — silently swallowed into zeros by the fail-soft except
    blocks. Enforcement (routes/ only):

      * funnel_health.py (three-time offender): ANY
        `with <x>.cursor(...) as cur:` is flagged. Use a private cursor
        name (e.g. `as _sd_cur` — see the r-cursor-shadow fix) instead.
      * every other routes/*.py: flagged only when the SAME function also
        binds a plain `cur = <x>.cursor(...)` on an earlier line (true
        shadowing risk).

    Opt-out (after human verification that no function-wide `cur` is
    live): append `# lint-ok: cursor-shadow` to the flagged line.
    """
    path = pathlib.Path(p)
    if 'routes' not in path.parts:
        return
    lines = src.split('\n')
    with_re = re.compile(r'\bwith\s+[\w.]+\.cursor\([^)]*\)\s+as\s+cur\s*:')

    def _flag(lineno):
        if CURSOR_SHADOW_OPTOUT in lines[lineno - 1]:
            return
        add(p, lineno, 'cursor-shadow',
            "`with ....cursor() as cur:` shadows/CLOSES the function-wide "
            "cursor named 'cur' — every later cur.execute() silently returns "
            "zeros (bug class hit routes/funnel_health.py 3x; see commit "
            "2ac9b095). Rename the inner cursor (e.g. `as _sd_cur`) or, if "
            "verified safe, append `# lint-ok: cursor-shadow` to the line.")

    if path.name == 'funnel_health.py':
        for i, line in enumerate(lines, 1):
            if with_re.search(line):
                _flag(i)
        return

    # AST pass for the rest of routes/: only flag TRUE shadowing — a
    # `with X.cursor() as cur:` inside a function that already bound
    # `cur = <x>.cursor(...)` on an earlier line.
    try:
        tree = ast.parse(src)
    except Exception:
        return

    def _is_cursor_call(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'cursor')

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigns, withs = [], []
        stack = list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue   # nested defs = own scope (ast.walk visits them)
            if isinstance(n, ast.Assign) and _is_cursor_call(n.value):
                if any(isinstance(t, ast.Name) and t.id == 'cur'
                       for t in n.targets):
                    assigns.append(n.lineno)
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if (_is_cursor_call(item.context_expr)
                            and isinstance(item.optional_vars, ast.Name)
                            and item.optional_vars.id == 'cur'):
                        withs.append(n.lineno)
            stack.extend(ast.iter_child_nodes(n))
        for wl in withs:
            if any(a < wl for a in assigns):
                _flag(wl)


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
    lint_cursor_shadow(p, src)
    for i, line in enumerate(src.split('\n'), 1):
        if re.search(r"['\"]/api/[^'\"]*%s[^'\"]*['\"]", line):
            if 'f"' not in line and "f'" not in line:
                add(p, i, 'url-format-typo', 'literal "%s" in URL — likely f-string')

    # 2026-07-29: tests are exempt from insert-no-on-conflict. The rule exists
    # to make production ingest idempotent, and a test that ASSERTS ON SQL TEXT
    # necessarily contains SQL strings it never executes — the WS6 guard test
    # asserts that the capture path contains no write verb against the queue
    # table, and was flagged for naming the verbs it forbids.
    # Flagging those trains people to whitelist the real table name, which is
    # strictly worse: it would suppress genuine violations in production code.
    # ★ Do not spell a bare write-verb-plus-table literal anywhere in this file:
    # scripts/ is not a test path, so this module is linted by its own rule and
    # a comment quoting the pattern trips it. (It did, once.)
    _is_test = 'tests/' in str(p).replace(os.sep, '/') or p.name.startswith('test_')
    if not _is_test:
        for m in re.finditer(r"INSERT\s+INTO\s+(\w+)[^;\"']*", src, re.I):
            if 'ON CONFLICT' in m.group(0).upper(): continue
            tbl = m.group(1).lower()
            if tbl in WHITELIST_TABLES: continue
            line = src[:m.start()].count('\n') + 1
            add(p, line, 'insert-no-on-conflict',
                f'INSERT INTO {tbl} without ON CONFLICT')

    try: tree = ast.parse(src)
    except Exception: return routes

    # name -> url_prefix for every Blueprint declared in this file, so the
    # duplicate-route check below compares real URLs rather than bare paths.
    # A blueprint with no url_prefix maps to '' and behaves exactly as before.
    _BP_PREFIXES = {}
    for _n in ast.walk(tree):
        if not (isinstance(_n, ast.Assign) and isinstance(_n.value, ast.Call)):
            continue
        _fn = _n.value.func
        _fname = getattr(_fn, 'id', None) or getattr(_fn, 'attr', None)
        if _fname != 'Blueprint':
            continue
        _pfx = ''
        for _kw in _n.value.keywords:
            if (_kw.arg == 'url_prefix' and isinstance(_kw.value, ast.Constant)
                    and isinstance(_kw.value.value, str)):
                _pfx = _kw.value.value
        for _t in _n.targets:
            if isinstance(_t, ast.Name):
                _BP_PREFIXES[_t.id] = _pfx

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
                    # 2026-07-29: qualify the path with the blueprint's
                    # url_prefix before calling anything a duplicate. Without
                    # this the rule compared bare decorator strings, so every
                    # correctly-prefixed blueprint that exposes a conventional
                    # sub-path collided with every other one — `/latest` was
                    # reported "in 14 places" while the 14 real URLs
                    # (/api/v1/iso/tva/latest, .../ieso/latest, ...) are all
                    # distinct. A rule that cannot be satisfied by correct code
                    # trains people to ignore it.
                    _obj = dec.func.value
                    _pfx = (_BP_PREFIXES.get(_obj.id, '')
                            if isinstance(_obj, ast.Name) else '')
                    routes.append((_pfx + dec.args[0].value, p, dec.lineno))
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
            # '.claude' excluded (r-fixpack 2026-07-02): local session
            # worktrees under .claude/worktrees/ carry full repo copies
            # that would double-report every violation.
            #
            # ★ Compared against the path RELATIVE to `r`, and per path
            # SEGMENT. Comparing against `dp` itself only worked because `r`
            # defaults to '.'; passing an absolute path under one of these
            # names — e.g. regression_lint.py ~/dchub-backend/.claude/
            # worktrees/<name> — matched every directory and linted nothing,
            # exiting 0 for having checked no files at all.
            rel = os.path.relpath(dp, r).replace(os.sep, '/')
            if any(s in rel.split('/') for s in ('.git', 'node_modules', '__pycache__', '.venv', 'site-packages', '.claude')):
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
            # HARD rules bypass the delta filter: they block even when
            # the offending line pre-dates this PR (zero-baseline rules).
            if v['line'] in file_changes or v['rule'] in HARD_RULES:
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
    # HARD-rule violations fail the run in ANY mode (audit included) —
    # they are zero-baseline regression fences, not advisories.
    hard_hits = [v for v in relevant if v['rule'] in HARD_RULES]
    if hard_hits and args.mode != 'delta':
        print(f"BLOCKING even in audit mode: {len(hard_hits)} HARD-rule "
              f"violation(s) ({', '.join(sorted({v['rule'] for v in hard_hits}))})")
    return 1 if (args.mode == 'delta' or hard_hits) else 0


if __name__ == '__main__':
    sys.exit(main())
