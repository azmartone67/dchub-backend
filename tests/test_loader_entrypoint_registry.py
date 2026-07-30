"""Every loader/job name we REGISTER must resolve to something that exists.

The recurring failure class this guards against: a job or loader is named in a
schedule or an admin registry under an entry point the module never defines, so
it fires nothing, while the endpoint that "ran" it still returns
{"success": true} and every dashboard stays green.

Measured on 2026-07-29 before this guard existed, across the three registration
sites in main.py, FOUR of the eleven distinct registered modules resolved to
nothing:

    hifld_communications    — not a loader at all (blueprint/query module)
    eia860_bulk_loader      — defines load_eia860(csv_path)
    subsea_cable_ingestion  — defines run_subsea_sync(get_db)
    energy_auto_discovery   — defines run_full_sync(conn)

Name resolution alone is not enough: THREE of those four real entry points take
a REQUIRED argument while both admin endpoints invoked f() with none, so a bare
rename would have swapped "no callable entry point" for a TypeError. This guard
checks the name AND the arity the runner actually supplies.

Everything here is pure `ast` over source text. Nothing is imported — main.py
opens DB pools and registers ~200 blueprints. Nothing runs at module scope.
"""

import ast
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(REPO_ROOT, 'main.py')
SCHEDULER_PY = os.path.join(REPO_ROOT, 'dchub-scheduler.py')

# How many positional arguments the runner supplies for each `needs` value.
# main.py::_run_standalone_loader is the authority; keep these in step.
NEEDS_ARITY = {
    'none': 0,
    'get_db': 1,
    'conn': 1,
    # Declared-but-unrunnable entry points still get their arity checked so the
    # registered name stays honest.
    'csv_path': 1,
}


# ─────────────────────────────────────────────────────────────
# AST helpers
# ─────────────────────────────────────────────────────────────
def _parse(path):
    """Parse a source file and PROVE the parse produced something.

    An empty or unparsed module makes every downstream `for` loop iterate zero
    times, which passes every assertion vacuously. Assert the body is non-empty
    before trusting anything extracted from it.
    """
    assert os.path.exists(path), 'expected source file missing: %s' % path
    with open(path, 'r', encoding='utf-8') as fh:
        src = fh.read()
    assert src.strip(), 'source file is empty: %s' % path
    tree = ast.parse(src, filename=path)
    assert isinstance(tree, ast.Module), '%s did not parse to a Module' % path
    assert tree.body, '%s parsed to an EMPTY module body' % path
    return tree


def _module_level_assign(tree, name):
    """Return the literal value of a module-level `name = <literal>`."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError('no module-level assignment named %r found' % name)


def _module_level_dict_of_dicts(tree, name):
    """Extract `name = {key: {subkey: ...}}` tolerantly.

    Not literal_eval: the scheduler's JOBS dict contains f-string endpoints,
    which literal_eval rejects outright — and a guard that cannot read the
    schedule is a guard that silently checks nothing. Non-literal values become
    the sentinel '<non-literal>'; keys and structure are still exact.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        assert isinstance(node.value, ast.Dict), '%s is not a dict literal' % name
        out = {}
        for k, v in zip(node.value.keys, node.value.values):
            if not isinstance(k, ast.Constant):
                continue
            spec = {}
            if isinstance(v, ast.Dict):
                for sk, sv in zip(v.keys, v.values):
                    if not isinstance(sk, ast.Constant):
                        continue
                    try:
                        spec[sk.value] = ast.literal_eval(sv)
                    except (ValueError, SyntaxError):
                        spec[sk.value] = '<non-literal>'
            out[k.value] = spec
        return out
    raise AssertionError('no module-level assignment named %r found' % name)


def _assign_in_function(tree, func_name, var_name):
    """Return the literal value of `var_name = <literal>` inside `func_name`.

    Returns the sentinel string '<non-literal>' when the assignment exists but
    is computed (e.g. `list(_STANDALONE_LOADERS.keys())`) — that is a PASS for
    this guard, because a computed list cannot drift from the registry.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == var_name:
                            try:
                                return ast.literal_eval(sub.value)
                            except (ValueError, SyntaxError):
                                return '<non-literal>'
            raise AssertionError('%s() has no assignment to %r'
                                 % (func_name, var_name))
    raise AssertionError('no function named %s() found' % func_name)


def _top_level_callables(module_name):
    """Map top-level def name -> number of REQUIRED positional args.

    Only top-level defs count: `getattr(module, name)` after a plain
    `__import__` cannot reach anything nested.
    """
    path = os.path.join(REPO_ROOT, module_name + '.py')
    if not os.path.exists(path):
        return None
    tree = _parse(path)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = list(getattr(args, 'posonlyargs', [])) + list(args.args)
            required = len(positional) - len(args.defaults)
            out[node.name] = max(required, 0)
    return out


def _phase12g_call_sites(tree):
    """Extract phase12g_loader_async('<mod>', [<candidates>], '<key>') calls."""
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == 'phase12g_loader_async'):
            continue
        if len(node.args) < 2:
            continue
        try:
            mod = ast.literal_eval(node.args[0])
            candidates = ast.literal_eval(node.args[1])
        except (ValueError, SyntaxError):
            continue
        sites.append((mod, list(candidates), node.lineno))
    return sites


def _registry():
    tree = _parse(MAIN_PY)
    reg = _module_level_assign(tree, '_STANDALONE_LOADERS')
    assert isinstance(reg, dict), '_STANDALONE_LOADERS is not a dict'
    assert reg, '_STANDALONE_LOADERS extracted EMPTY — extraction is broken, ' \
                'not the registry (an empty dict would pass every check below)'
    return tree, reg


# ─────────────────────────────────────────────────────────────
# The guard
# ─────────────────────────────────────────────────────────────
def test_extraction_itself_works():
    """Control: prove the AST extraction found real data before trusting it."""
    tree, reg = _registry()
    assert len(reg) >= 8, 'expected >=8 registered loader modules, got %d' % len(reg)
    sites = _phase12g_call_sites(tree)
    assert len(sites) >= 4, ('expected >=4 phase12g_loader_async call sites, '
                             'found %d — extraction is broken' % len(sites))
    for mod in reg:
        assert _top_level_callables(mod) is not None, \
            'registered module %s.py does not exist in the repo' % mod


def test_every_registered_entry_point_is_defined():
    """A registered entry-point NAME must resolve to a top-level def."""
    _tree, reg = _registry()
    unresolved = []
    for mod, spec in sorted(reg.items()):
        entry = spec.get('entry')
        if entry is None:
            # Explicitly "this module has no loader entry point" — legal only
            # when it also states why.
            assert spec.get('unrunnable'), (
                '%s declares entry=None without an unrunnable reason' % mod)
            continue
        defined = _top_level_callables(mod)
        assert defined, '%s.py defines no top-level functions at all' % mod
        if entry not in defined:
            unresolved.append('%s: registered entry %r not defined (defines: %s)'
                              % (mod, entry, ', '.join(sorted(defined))))
    assert not unresolved, (
        'registered loader entry points that resolve to NOTHING (these fire no '
        'code while the admin endpoint still reports success):\n  '
        + '\n  '.join(unresolved))


def test_entry_point_arity_matches_what_the_runner_supplies():
    """The runner calls f() / f(get_db) / f(conn) — the def must accept that."""
    _tree, reg = _registry()
    bad = []
    for mod, spec in sorted(reg.items()):
        entry = spec.get('entry')
        needs = spec.get('needs')
        if entry is None:
            continue
        assert needs in NEEDS_ARITY, \
            '%s declares unknown needs=%r' % (mod, needs)
        defined = _top_level_callables(mod) or {}
        if entry not in defined:
            continue  # reported by the previous test
        supplied = NEEDS_ARITY[needs]
        required = defined[entry]
        if required != supplied:
            bad.append('%s.%s requires %d positional arg(s) but needs=%r '
                       'supplies %d' % (mod, entry, required, needs, supplied))
    assert not bad, (
        'entry points whose arity does not match the call the runner makes — '
        'these raise TypeError at run time, which the old code reported as an '
        'ordinary loader error:\n  ' + '\n  '.join(bad))


def test_admin_endpoints_register_only_registry_modules():
    """No admin endpoint may name a loader outside _STANDALONE_LOADERS."""
    tree, reg = _registry()

    c12 = _assign_in_function(tree, 'phase12c_admin_load_all', 'loaders')
    assert c12 != '<non-literal>', 'phase 12c loader list is no longer a literal'
    assert c12, 'phase 12c loader list extracted EMPTY'
    for mod in c12:
        assert isinstance(mod, str), \
            'phase 12c still carries tuple entries: %r (entry points belong ' \
            'in _STANDALONE_LOADERS, not inline)' % (mod,)
        assert mod in reg, \
            'phase 12c registers %r, which is absent from _STANDALONE_LOADERS' % mod

    f12 = _assign_in_function(tree, 'phase12f_run_all_loaders', 'loaders')
    # Derived from the registry by construction — cannot drift.
    if f12 != '<non-literal>':
        for mod in f12:
            assert mod in reg, \
                'phase 12f registers %r, absent from _STANDALONE_LOADERS' % mod


def test_phase12g_async_loaders_resolve():
    """The async per-loader endpoints try candidate names — one must resolve.

    phase12g_loader_async invokes the resolved function with NO arguments, so
    the candidate that resolves must also require zero positional args.
    """
    tree, _reg = _registry()
    broken = []
    for mod, candidates, lineno in _phase12g_call_sites(tree):
        defined = _top_level_callables(mod)
        if defined is None:
            broken.append('main.py:%d %s.py does not exist' % (lineno, mod))
            continue
        ok = [c for c in candidates if defined.get(c) == 0]
        if not ok:
            near = [c for c in candidates if c in defined]
            detail = ('candidates %s resolve but all require arguments'
                      % near) if near else 'no candidate is defined'
            broken.append('main.py:%d %s(%s): %s — module defines: %s'
                          % (lineno, mod, ', '.join(candidates), detail,
                             ', '.join(sorted(defined))))
    assert not broken, ('phase 12g async loader endpoints that call nothing:\n  '
                        + '\n  '.join(broken))


# ─────────────────────────────────────────────────────────────
# Same failure class, one layer up: the scheduler
# ─────────────────────────────────────────────────────────────
# Measured baseline, dchub-scheduler.py at 2026-07-29: 48 DISABLED_JOBS
# entries, of which 43 carry a full hours/minute/day_of_week schedule and NO
# disabled_reason — i.e. they read as live weekly/daily jobs and fire nothing,
# because the run loop iterates JOBS only. subsea_sync was the 44th and is
# fixed; the rest are pinned here rather than silently accepted.
#
# This is a RATCHET, not an endorsement: the assertion below fails the moment a
# NEW reasonless entry is added. Each name should be given a reason (or moved
# back into JOBS) as its owner gets to it, and deleted from this list when they
# do — shrinking this list is the point.
_REASONLESS_DISABLED_BASELINE = frozenset({
    'activation_nudge', 'agent_adoption_digest', 'agent_adoption_tick',
    'agent_iteration_packet', 'agent_pay_shell_tick', 'ai_citation_press_draft',
    'brain_auto_action', 'brain_auto_code_scan', 'brain_causal_analyze',
    'brain_draft_pr_expire', 'brain_expansion_snapshot', 'brain_issue_janitor',
    'brain_memory_consolidate_DISABLED', 'brain_narrative_refresh',
    'brain_orchestrator_refresh', 'brain_press_loop',
    'brain_qa_agent_sweep_DISABLED', 'brain_self_critique_DISABLED',
    'campus_sync', 'competitor_scan', 'customer_white_glove_digest',
    'customer_white_glove_tick', 'daily_aggregation', 'health_probe',
    'heartbeat_auto_drain', 'heartbeat_refresh', 'industry_pulse_refresh',
    'ix_sync', 'jobs_expansion_signals', 'jobs_market_heat', 'jobs_skills',
    'jobs_summary', 'jobs_trends', 'market_alerts_send',
    'marketing_publish_now', 'media_pending_drafts_digest', 'neon_health',
    'network_sync', 'peeringdb_full_sync', 'planner_grading_panel',
    'sentinel_master_tick', 'weekly_public_newsletter', 'winback_delivery',
})


def test_no_new_reasonless_disabled_job():
    """A DISABLED_JOBS entry with no disabled_reason reads as a live job.

    subsea_sync sat in DISABLED_JOBS with a full weekly hours/minute/
    day_of_week schedule and no reason, under a comment describing it as
    "weekly Wednesday 03:00+". It fired nothing; the run loop iterates JOBS
    only. 43 further entries are in the same shape today — pinned in
    _REASONLESS_DISABLED_BASELINE so no NEW one can be added quietly.
    """
    tree = _parse(SCHEDULER_PY)
    disabled = _module_level_dict_of_dicts(tree, 'DISABLED_JOBS')
    assert isinstance(disabled, dict) and disabled, \
        'DISABLED_JOBS extracted empty — extraction is broken'
    missing = {k for k, v in disabled.items()
               if not str((v or {}).get('disabled_reason') or '').strip()}
    new = sorted(missing - _REASONLESS_DISABLED_BASELINE)
    assert not new, (
        'new DISABLED_JOBS entries with no disabled_reason — they carry a '
        'schedule and fire nothing, which is exactly how subsea_sync stayed '
        'dead: %s' % ', '.join(new))
    # Keep the baseline honest in the other direction too: once an entry gets a
    # reason or moves into JOBS, drop it from the frozen set.
    stale = sorted(_REASONLESS_DISABLED_BASELINE - missing)
    assert not stale, (
        'these no longer lack a disabled_reason — remove them from '
        '_REASONLESS_DISABLED_BASELINE: %s' % ', '.join(stale))


def test_fiber_and_subsea_jobs_state_their_status():
    """The three jobs under the "Fiber/subsea sync" comment must be explicit."""
    tree = _parse(SCHEDULER_PY)
    jobs = _module_level_dict_of_dicts(tree, 'JOBS')
    disabled = _module_level_dict_of_dicts(tree, 'DISABLED_JOBS')
    for name in ('fiber_full_sync', 'carrier_sync'):
        assert name in disabled, '%s left DISABLED_JOBS unexpectedly' % name
        assert str(disabled[name].get('disabled_reason') or '').strip(), \
            '%s is disabled with no stated reason' % name
    assert 'subsea_sync' in jobs, 'subsea_sync must be live in JOBS'


def test_scheduler_job_is_in_exactly_one_dict():
    tree = _parse(SCHEDULER_PY)
    jobs = _module_level_dict_of_dicts(tree, 'JOBS')
    disabled = _module_level_dict_of_dicts(tree, 'DISABLED_JOBS')
    assert jobs, 'JOBS extracted empty — extraction is broken'
    both = sorted(set(jobs) & set(disabled))
    assert not both, ('jobs present in BOTH JOBS and DISABLED_JOBS (the run '
                      'loop reads JOBS, so the disabled copy is a lie): %s'
                      % ', '.join(both))


def test_subsea_sync_is_scheduled():
    """Regression pin for the specific dead job this guard was written for."""
    tree = _parse(SCHEDULER_PY)
    jobs = _module_level_dict_of_dicts(tree, 'JOBS')
    assert 'subsea_sync' in jobs, \
        'subsea_sync is not in JOBS — the run loop will never fire it'
    spec = jobs['subsea_sync']
    assert spec.get('endpoint') == '/api/jobs/subsea-sync', \
        'subsea_sync endpoint changed: %r' % (spec.get('endpoint'),)


def test_subsea_ingest_is_polite_before_it_is_scheduled():
    """A re-enabled weekly job must honour robots.txt and space its requests."""
    tree = _parse(os.path.join(REPO_ROOT, 'subsea_cable_ingestion.py'))
    names = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for required in ('_robots_allows', '_throttle'):
        assert required in names, \
            'subsea_cable_ingestion.py must define %s() before its job is ' \
            'scheduled' % required
    ua = _module_level_assign(tree, 'USER_AGENT')
    assert 'dchub.cloud' in ua, 'UA must identify us: %r' % ua
    assert 'contact' in ua.lower() or '@' in ua, \
        'UA must carry a contact: %r' % ua
    spacing = _module_level_assign(tree, '_MIN_REQUEST_SPACING_S')
    assert spacing >= 2.0, 'request spacing must be >= 2s, got %r' % spacing


# ─────────────────────────────────────────────────────────────
# Behavioural: the politeness controls must actually STOP a fetch, not merely
# exist. A comment or a defined-but-unreachable check satisfies grep and
# nothing else.
# ─────────────────────────────────────────────────────────────
def test_robots_disallow_actually_blocks_the_fetch():
    """A Disallow must abandon the request — with NO network call made."""
    from urllib.robotparser import RobotFileParser

    import subsea_cable_ingestion as sci

    calls = []

    class _Boom:
        @staticmethod
        def get(*a, **kw):
            calls.append(a)
            raise AssertionError('network call attempted despite robots Disallow')

    rp = RobotFileParser()
    rp.parse(['User-agent: *', 'Disallow: /'])

    saved_cache = dict(sci._robots_cache)
    saved_requests = sci.requests
    saved_spacing = sci._MIN_REQUEST_SPACING_S
    try:
        sci._robots_cache.clear()
        sci._robots_cache['example.test'] = rp
        sci.requests = _Boom
        sci._MIN_REQUEST_SPACING_S = 0  # keep the test fast; not what is under test
        assert sci._robots_allows('https://example.test/api/v3/cable.json') is False
        # Both primary and backup are disallowed -> None, and requests.get was
        # never reached.
        got = sci._fetch_json('https://example.test/a.json',
                              'https://example.test/b.json')
        assert got is None, 'a disallowed fetch must yield None, got %r' % (got,)
        assert not calls, 'requests.get was called %d time(s) anyway' % len(calls)
    finally:
        sci.requests = saved_requests
        sci._MIN_REQUEST_SPACING_S = saved_spacing
        sci._robots_cache.clear()
        sci._robots_cache.update(saved_cache)


def test_robots_allow_all_permits_the_fetch():
    """Control for the test above: an empty Disallow must NOT block.

    Without this, a _robots_allows() that returned False unconditionally would
    pass the blocking test and silently disable the whole ingest — the guard
    would 'work' while the job fetched nothing. This is the exact shape of
    submarinecablemap.com's real robots.txt, measured 2026-07-29.
    """
    from urllib.robotparser import RobotFileParser

    import subsea_cable_ingestion as sci

    rp = RobotFileParser()
    rp.parse(['User-agent: *', 'Disallow:'])

    saved_cache = dict(sci._robots_cache)
    try:
        sci._robots_cache.clear()
        sci._robots_cache['example.test'] = rp
        assert sci._robots_allows('https://example.test/api/v3/cable.json') is True
    finally:
        sci._robots_cache.clear()
        sci._robots_cache.update(saved_cache)


def test_throttle_enforces_spacing_between_requests():
    """Two back-to-back requests to one host must be >= the spacing apart."""
    import time as _t

    import subsea_cable_ingestion as sci

    saved = dict(sci._last_request_at)
    try:
        sci._last_request_at.clear()
        sci._throttle('https://example.test/one')   # first call: no wait
        t0 = _t.monotonic()
        sci._throttle('https://example.test/two')   # second: must wait
        elapsed = _t.monotonic() - t0
        assert elapsed >= sci._MIN_REQUEST_SPACING_S - 0.05, (
            'second request to the same host waited only %.2fs, expected >= %.2fs'
            % (elapsed, sci._MIN_REQUEST_SPACING_S))
    finally:
        sci._last_request_at.clear()
        sci._last_request_at.update(saved)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
