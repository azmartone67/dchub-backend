"""cron_declarations — read cron jobs a module declares about ITSELF.

★ 2026-09-04. STEP ONE of decentralising routes/cron_heartbeat.py::_DISPATCH.

THE PROBLEM THIS PREPARES FOR. Adding a feature today means appending to
_DISPATCH, a single 120-entry literal in one file, so two concurrent PRs adding
a feature conflict there every time. main takes ~47 commits/24h and the unit
suite runs 25 minutes, so PRs stay open long enough for that to be a certainty:
three of four PRs open on 2026-09-04 hit exactly this class of conflict.

The fix is for a module to declare its own job (a module-level `CRON_JOBS`
list), so adding a feature touches only its own file. But shipping the collector
first would have punched a hole in two existing guards, both of which read the
_DISPATCH LITERAL:

  · routes/audit_closure_master_shell.scan_beat_scheduler_gaps reads
    cron_heartbeat.py as TEXT and substring-matches tick paths. A job declared
    elsewhere reads as unscheduled — red in CI (tests/test_shell_scheduler_
    coverage.py) and red live on shell #52 lane J. LOUD, so at least visible.

  · tests/test_shell_beat_reports_red.test_every_dispatched_shell_beats_the_
    ledger AST-walks the _DISPATCH literal for labels containing "shell". A
    shell declared elsewhere silently drops out of the dead-man coverage check.
    SILENT GREEN — strictly worse, and precisely the failure this repo keeps
    rediscovering.

So the guards learn to see declarations BEFORE anything declares one. Today no
module defines CRON_JOBS, so every consumer behaves exactly as it did; what
changes is that they can no longer be blindsided when one appears.

★ AST ONLY — NEVER import, never exec.
Importing a routes module to read its declaration would (a) run import-time side
effects in modules main.py may never import (routes/global_infra.py starts a
daemon thread at import), and (b) make one ImportError able to take the whole
scan down. Parsing is inert: a syntactically broken module yields nothing and is
reported, rather than raising through the caller.

★ WHY IT EXTRACTS ONLY THE CRON_JOBS NODE.
The tempting shortcut for scan_beat_scheduler_gaps is to concatenate the
declaring module's source into the text it already greps. That is VACUOUS: the
module's own `@bp.route(".../master-tick")` decorator would satisfy the
substring test for every module, so the guard would pass for everything forever.
Only the values inside the CRON_JOBS assignment count as a declaration.
"""
from __future__ import annotations

import ast
import glob
import os

# The one field a scheduler needs to prove a route is driven. `label` is carried
# too because the dead-man guard keys on it.
_WANT = ("label", "path")


def _literal(node):
    """Constant value of an AST node, or None if it is not a plain literal.

    A computed path (an f-string, a name, a concatenation) is deliberately NOT
    resolved: this runs without importing, so it cannot know what a name holds,
    and guessing would let a guard believe a route is scheduled when the value
    is something else entirely. Unresolvable entries are reported by
    `collect_problems` rather than silently skipped.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _jobs_from_tree(tree) -> tuple[list[dict], list[str]]:
    """(jobs, problems) from one parsed module."""
    jobs, problems = [], []
    for node in tree.body:            # module level only — never nested
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CRON_JOBS"
                   for t in node.targets):
            continue
        if not isinstance(node.value, ast.List):
            problems.append("CRON_JOBS is not a list literal")
            continue
        for el in node.value.elts:
            if not isinstance(el, ast.Dict):
                problems.append("CRON_JOBS entry is not a dict literal")
                continue
            entry = {}
            for k, v in zip(el.keys, el.values):
                if isinstance(k, ast.Constant) and k.value in _WANT:
                    lit = _literal(v)
                    if lit is None:
                        problems.append(
                            f"CRON_JOBS entry has a non-literal {k.value!r} — "
                            f"a scheduler guard cannot verify a computed value")
                    else:
                        entry[k.value] = lit
            if entry.get("path"):
                jobs.append(entry)
            else:
                problems.append("CRON_JOBS entry has no literal 'path'")
    return jobs, problems


def collect_declared_jobs(root: str) -> list[dict]:
    """Every job declared by a module about itself, across routes/*.py.

    Each entry: {"label": str|None, "path": str, "module": "routes/x.py"}.
    Empty today — nothing declares CRON_JOBS yet — and that is the point: the
    guards gain the capability before the first declaration exists.
    """
    out = []
    for p in sorted(glob.glob(os.path.join(root, "routes", "*.py"))):
        rel = os.path.relpath(p, root)
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except Exception:            # unparseable module declares nothing
            continue
        jobs, _ = _jobs_from_tree(tree)
        for j in jobs:
            out.append({"label": j.get("label"), "path": j["path"], "module": rel})
    return out


def collect_problems(root: str) -> list[str]:
    """Declarations a guard could NOT verify — reported, never silently dropped.

    A CRON_JOBS entry whose path is computed rather than literal is exactly the
    shape that would let a job look scheduled to a human and be invisible to
    every scanner. Surfacing it keeps the failure loud.
    """
    out = []
    for p in sorted(glob.glob(os.path.join(root, "routes", "*.py"))):
        rel = os.path.relpath(p, root)
        try:
            src = open(p, encoding="utf-8").read()
        except Exception as e:       # noqa: BLE001
            out.append(f"{rel}: unreadable ({e})")
            continue
        if "CRON_JOBS" not in src:   # cheap gate; parsing 800 files is not free
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            out.append(f"{rel}: unparseable, so its CRON_JOBS is invisible ({e})")
            continue
        _, problems = _jobs_from_tree(tree)
        out.extend(f"{rel}: {m}" for m in problems)
    return out


def declared_paths(root: str) -> set:
    """Just the tick paths — what a scheduler-coverage scan needs."""
    return {j["path"] for j in collect_declared_jobs(root)}


def declared_labels(root: str) -> set:
    """Just the labels — what the dead-man coverage check keys on."""
    return {j["label"] for j in collect_declared_jobs(root) if j.get("label")}
