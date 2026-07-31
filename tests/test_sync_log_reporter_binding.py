"""Guard: a sync-log reporter must not read a name bound inside the try it reports on.

THE BUG THIS EXISTS FOR (shipped in #1990, caught in production the same hour)
─────────────────────────────────────────────────────────────────────────────
crawl_power_plants ends with

    try:
        ...
        dropped_no_plant_id = 0        # <- bound at line 441, INSIDE the try
        ...
    except Exception as e:
        errors += 1
    ...
    _log_sync(get_db, 'eia-860-plants', fetched, upserted,
              dropped_no_plant_id, ...)   # <- read at line 585, OUTSIDE it

When the EIA fetch failed — the exact case the log exists to record — control
left the try before line 441 ever ran, and the reporting line raised

    NameError: name 'dropped_no_plant_id' is not defined

That propagated out of crawl_power_plants and was swallowed by
run_land_power_sync's per-step `except`, so the failing feed logged NOTHING.
Measured in production 2026-07-31: a sync run at 02:30 logged hifld-substations
(02:30:52) and hifld-transmission (02:31:05) — whose counters are bound at the
top of their own functions — and no eia-860-plants row at all, despite plants
running FIRST. The prior run at 02:09, before #1990, had logged all four.

#1990's stated purpose was to make this feed's failures visible. It made them
invisible. The failure mode is silent by construction: an exception raised
while reporting an exception has nowhere left to go.

THE RULE
────────
  R1. Every name a _log_sync call reads must be bound BEFORE the try block it
      sits after — i.e. at function scope, before the first statement that can
      raise. This is checked structurally for EVERY crawler in the module, not
      just the one that broke.
  R2. crawl_power_plants specifically binds dropped_no_plant_id and
      sample_dropped_keys at function scope (regression pin).
  R3. The counter is not re-initialised inside the try, which would shadow the
      hoist and silently restore the bug.

★ WHY STRUCTURAL, NOT A CASE TEST. A test that calls crawl_power_plants with a
  broken feed would pin this one function. The defect is a SHAPE — "reporter
  reads a name the try owns" — and it is available to every crawler in this file
  that follows the same try/except/report layout. R1 walks all of them.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ 968c7475, i.e. WITH #1990's regression in):
    3 failed, 0 passed, 1 xfailed
PATCHED (this branch):
    0 failed, 3 passed, 1 xfailed

`1 xfailed` in both runs — strict-xfail must-fail control.

No network, no DB, no main.py import; nothing runs at module scope.

Run:  python3 -m pytest tests/test_sync_log_reporter_binding.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "land_power_crawler.py")


def _tree():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t, src


def _crawlers():
    """Every module-level function that calls _log_sync after a try block."""
    t, _ = _tree()
    out = []
    for fn in t.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        assert fn.body, f"{fn.name} parsed with an EMPTY body"
        tries = [n for n in fn.body if isinstance(n, ast.Try)]
        if not tries:
            continue
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_log_sync"]
        # only the reporters that sit AFTER the try
        after = [c for c in calls if c.lineno > tries[-1].end_lineno]
        if after:
            out.append((fn, tries[-1], after))
    assert out, "no crawler with a post-try _log_sync found — harness is blind"
    return out


def _bound_before(fn, lineno):
    """Names bound at function scope strictly before `lineno`."""
    names = {a.arg for a in fn.args.args}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.lineno < lineno:
            names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno < lineno:
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _bound_between(fn, lo, hi):
    """Names bound after the try ends and before the reporting call.

    ★ These are SAFE and must not be flagged. `duration = time.time() - started`
    sits here in every crawler in this module: the try has already unwound, so
    the assignment always runs whether the body succeeded or raised. A first
    draft of this guard only allowed names bound before the try and flagged
    `duration` in all four crawlers — a false positive that would have made the
    guard unshippable. The rule is about names the try OWNS, not about position.
    """
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and lo < n.lineno < hi:
            names.add(n.id)
    return names


# ── R1 ────────────────────────────────────────────────────────────────────────
def test_no_reporter_reads_a_name_the_try_owns():
    """The shape check, across every crawler in the module."""
    import builtins
    problems = []
    t, _ = _tree()
    module_names = {n.targets[0].id for n in t.body
                    if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", None)}
    module_names |= {n.name for n in t.body
                     if isinstance(n, (ast.FunctionDef, ast.ClassDef))}

    for fn, tr, calls in _crawlers():
        for call in calls:
            # Safe = bound at function scope BEFORE the try (so a raise inside
            # cannot skip it), or bound AFTER the try unwound but before the
            # reporting call (which always runs). Anything else is a name the
            # try OWNS, and reading it while reporting a failure is a NameError.
            safe = (_bound_before(fn, tr.lineno)
                    | _bound_between(fn, tr.end_lineno, call.lineno)
                    | module_names | set(dir(builtins)))
            for arg in call.args:
                for nd in ast.walk(arg):
                    if isinstance(nd, ast.Name) and isinstance(nd.ctx, ast.Load):
                        if nd.id not in safe:
                            problems.append(
                                f"{fn.name}: _log_sync (line {call.lineno}) reads "
                                f"`{nd.id}`, which is only bound inside the try at "
                                f"line {tr.lineno} — a failure before that binding "
                                f"raises NameError ON THE REPORTING LINE and the "
                                f"run logs nothing")
    assert not problems, "\n".join(problems)


# ── R2 ────────────────────────────────────────────────────────────────────────
def test_plant_crawler_binds_its_drop_counters_at_function_scope():
    t, _ = _tree()
    fn = next((n for n in t.body if isinstance(n, ast.FunctionDef)
               and n.name == "crawl_power_plants"), None)
    assert fn is not None, "crawl_power_plants not found"
    assert fn.body, "crawl_power_plants parsed with an EMPTY body"
    tries = [n for n in fn.body if isinstance(n, ast.Try)]
    assert tries, "crawl_power_plants has no try block — harness assumption broken"
    before = _bound_before(fn, tries[0].lineno)
    for name in ("dropped_no_plant_id", "sample_dropped_keys", "fetched",
                 "upserted", "errors", "error_detail"):
        assert name in before, (
            f"`{name}` is not bound before the try at line {tries[0].lineno}. "
            f"The _log_sync after the try reads it, so an upstream failure "
            f"raises NameError while reporting the failure.")


# ── R3 ────────────────────────────────────────────────────────────────────────
def test_the_hoisted_counter_is_not_shadowed_back_inside_the_try():
    """Re-initialising inside the try would restore the bug silently."""
    t, _ = _tree()
    fn = next(n for n in t.body if isinstance(n, ast.FunctionDef)
              and n.name == "crawl_power_plants")
    tries = [n for n in fn.body if isinstance(n, ast.Try)]
    tr = tries[0]
    inside = [n for n in ast.walk(tr)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
              and n.id == "dropped_no_plant_id"]
    # `+= 1` is a Store too, and is fine — only a bare `= 0` reset is the problem
    resets = []
    for n in ast.walk(tr):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if getattr(tgt, "id", None) == "dropped_no_plant_id":
                    resets.append(n.lineno)
    assert not resets, (
        f"dropped_no_plant_id is re-assigned inside the try at line(s) {resets}. "
        f"That shadows the function-scope hoist and restores the exact NameError "
        f"this file exists to prevent. The `+= 1` increment is fine; a `= 0` "
        f"reset is not.")
    assert inside, (
        "the counter is never incremented inside the try — the drop accounting "
        "has been removed rather than hoisted")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
