"""Static wiring guard for the SCHEDULE <-> _RUNNERS contract.

WHY THIS EXISTS
---------------
f996d441 reverted crawler_scheduler.py to a stale copy and orphaned 7 SCHEDULE
entries -- jobs still listed in SCHEDULE whose _RUNNERS registry line had been
deleted. The tick loop dispatches through

    if should_run and name in _RUNNERS:          # crawler_scheduler.py

so an orphaned entry is SILENTLY SKIPPED: no exception, no log line, no metric.
Seven jobs simply stopped running in production and nothing noticed until the
revert was found by hand. Restored in #1799.

The only coverage that existed was a hardcoded grep for one job name
(test_white_glove_drift_detector.py::test_schedule_wired_and_kill_switch_present
greps the source for "white_glove_propagate"); the gate tests in
test_mcp_url_rediscovery.py are per-job source greps in the same style. None of
them would have flagged any of the 7, because none of the 7 is the job they name.
This module checks EVERY entry instead of a hand-picked few.

The scheduler is parsed with `ast`, never imported: importing crawler_scheduler
pulls in Flask, the DB and the network at module scope, and no test in this repo
imports main or the scheduler modules.
"""
import ast
import functools
import pathlib

# Resolve from the test file, never the cwd -- the suite gets invoked from both
# the repo root and from tests/, and a cwd-relative path silently reads the
# wrong file (or none) depending on which. See #1797.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULER_SRC = REPO_ROOT / "crawler_scheduler.py"

# _RUNNERS keys deliberately absent from SCHEDULE: registered so the job can be
# fired by hand, but intentionally never put on the clock. Keep this set SHORT
# and justified -- every entry is a job that nothing will ever run on its own.
UNSCHEDULED_BY_DESIGN = {
    # "api_discovery EXCLUDED -- too heavy, available via manual trigger only"
    # -- comment sitting directly above SCHEDULE in crawler_scheduler.py
    "api_discovery",
}

# Tripwire against a VACUOUS pass. If a refactor renames these structures or
# changes their shape so the extractor comes back empty, every assertion below
# would hold trivially and this guard would go quietly useless -- which is the
# same class of silent failure it exists to catch. These floors are not a policy
# on how many jobs should exist (69 scheduled / 70 registered as of #1799); they
# only prove the guard actually parsed something.
_MIN_SCHEDULE_ENTRIES = 40
_MIN_RUNNER_ENTRIES = 40


def _module_assign(tree, name):
    """Value node of the single module-level ``name = ...`` assignment.

    Scans ``tree.body`` rather than ``ast.walk`` so a same-named local inside
    some function body can never be mistaken for the real thing.
    """
    hits = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    assert hits, (
        "no module-level `{n} = ...` found in {f}. It was renamed or moved "
        "inside a function, so this guard can no longer see the scheduler "
        "wiring -- failing instead of passing vacuously. Point the extractor at "
        "the new structure.".format(n=name, f=SCHEDULER_SRC.name)
    )
    assert len(hits) == 1, (
        "{c} module-level assignments to `{n}` in {f} -- the last one wins at "
        "runtime and this guard cannot tell which is authoritative.".format(
            c=len(hits), n=name, f=SCHEDULER_SRC.name)
    )
    return hits[0]


@functools.lru_cache(maxsize=1)
def _wiring():
    """Parse (schedule, runners, defined_functions) out of crawler_scheduler.py.

    Deliberately NOT run at import time. A raise during module import is a
    pytest COLLECTION error, which takes down the whole run rather than failing
    one test -- exactly what #1797 had to undo. Calling this from inside each
    test keeps any breakage reportable as a normal failure.

    Returns:
        schedule: list of (job_name, handler_name) in SCHEDULE order
        runners:  dict of job_name -> registered function name
        defs:     set of every function name defined in the module
    """
    assert SCHEDULER_SRC.is_file(), (
        "{p} not found -- this guard resolves the scheduler relative to the "
        "test file, so a missing file means the layout moved.".format(
            p=SCHEDULER_SRC)
    )
    tree = ast.parse(SCHEDULER_SRC.read_text(encoding="utf-8"))

    defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    sched_node = _module_assign(tree, "SCHEDULE")
    assert isinstance(sched_node, ast.List), (
        "SCHEDULE is a {t}, expected a list literal.".format(
            t=type(sched_node).__name__)
    )

    schedule = []
    for idx, elt in enumerate(sched_node.elts):
        # Do NOT isinstance-filter here. Quietly skipping a malformed entry
        # would drop that job out of the orphan check without a word, which is
        # the precise failure mode this file exists to prevent.
        assert isinstance(elt, ast.Tuple) and len(elt.elts) == 4, (
            "SCHEDULE[{i}] (line {ln}) is not a 4-tuple of "
            "(hour1, hour2, name, handler). The scheduler unpacks exactly four "
            "fields (`for hour1, hour2, name, _ in SCHEDULE`), and this guard "
            "reads fields 3 and 4 -- update both together.".format(
                i=idx, ln=getattr(elt, "lineno", "?"))
        )
        name_node, handler_node = elt.elts[2], elt.elts[3]
        for label, node in (("name", name_node), ("handler", handler_node)):
            assert isinstance(node, ast.Constant) and isinstance(node.value, str), (
                "SCHEDULE[{i}] (line {ln}) has a non-literal {lb}; it must be a "
                "plain string so this guard can check it statically.".format(
                    i=idx, ln=getattr(elt, "lineno", "?"), lb=label)
            )
        schedule.append((name_node.value, handler_node.value))

    runners_node = _module_assign(tree, "_RUNNERS")
    assert isinstance(runners_node, ast.Dict), (
        "_RUNNERS is a {t}, expected a dict literal.".format(
            t=type(runners_node).__name__)
    )

    runners = {}
    for key, val in zip(runners_node.keys, runners_node.values):
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "_RUNNERS has a non-literal key at line {ln}; keys are matched "
            "against SCHEDULE job names and must be plain strings.".format(
                ln=getattr(key, "lineno", "?"))
        )
        assert isinstance(val, ast.Name), (
            "_RUNNERS[{k!r}] is not a bare function reference (got {t}); this "
            "guard resolves it by name.".format(
                k=key.value, t=type(val).__name__)
        )
        runners[key.value] = val.id

    return schedule, runners, defs


def test_extractor_actually_parsed_the_wiring():
    """Guard the guard: prove the checks below are running on real data."""
    schedule, runners, defs = _wiring()
    assert len(schedule) >= _MIN_SCHEDULE_ENTRIES, (
        "only {n} SCHEDULE entries parsed (floor {f}). Either the scheduler was "
        "gutted or the extractor stopped matching -- both make the orphan check "
        "below meaningless.".format(n=len(schedule), f=_MIN_SCHEDULE_ENTRIES)
    )
    assert len(runners) >= _MIN_RUNNER_ENTRIES, (
        "only {n} _RUNNERS entries parsed (floor {f}) -- see above.".format(
            n=len(runners), f=_MIN_RUNNER_ENTRIES)
    )
    assert defs, "no function definitions parsed out of the scheduler at all."


def test_every_scheduled_job_has_a_runner():
    """THE regression guard for the f996d441 orphan class.

    A SCHEDULE entry with no _RUNNERS line does not raise and does not log --
    `if should_run and name in _RUNNERS` just falls through and the job never
    runs again.
    """
    schedule, runners, _ = _wiring()
    orphans = [name for name, _ in schedule if name not in runners]
    assert not orphans, (
        "{n} SCHEDULE job(s) have no _RUNNERS entry: {o}\n"
        "The scheduler dispatches via `if should_run and name in _RUNNERS`, so "
        "these are SILENTLY SKIPPED -- no error, no log, no metric; they simply "
        "never run. Add each to the _RUNNERS dict in crawler_scheduler.py (or "
        "remove it from SCHEDULE if the job is retired).".format(
            n=len(orphans), o=", ".join(sorted(orphans)))
    )


def test_every_scheduled_handler_resolves_to_a_function():
    """SCHEDULE's 4th field names a function that must actually exist."""
    schedule, _, defs = _wiring()
    missing = sorted(
        {"{0} -> {1}()".format(name, handler)
         for name, handler in schedule if handler not in defs}
    )
    assert not missing, (
        "{n} SCHEDULE entr(y/ies) name a handler that is not defined in "
        "crawler_scheduler.py: {m}\n"
        "The handler name is a plain string and is never resolved at import, so "
        "a typo or a deleted function stays invisible until the job is due."
        .format(n=len(missing), m=", ".join(missing))
    )


def test_schedule_handler_matches_the_registered_runner():
    """SCHEDULE's declared handler and _RUNNERS' target must not disagree.

    Dispatch uses _RUNNERS, so when the two drift apart SCHEDULE's 4th field
    becomes documentation that lies about what actually runs.
    """
    schedule, runners, _ = _wiring()
    mismatched = sorted(
        "{0}: SCHEDULE says {1}(), _RUNNERS calls {2}()".format(
            name, handler, runners[name])
        for name, handler in schedule
        if name in runners and runners[name] != handler
    )
    assert not mismatched, (
        "SCHEDULE and _RUNNERS disagree on which function a job runs:\n  "
        + "\n  ".join(mismatched)
    )


def test_no_duplicate_schedule_job_names():
    """Two SCHEDULE rows sharing a name collapse into one job.

    The tick loop keys its per-day bookkeeping by name
    (`last_run_hours = {s[2]: set() for s in SCHEDULE}`), so a duplicate silently
    merges both rows' hour slots into a single tracker.
    """
    schedule, _, _ = _wiring()
    seen, dupes = set(), []
    for name, _handler in schedule:
        if name in seen:
            dupes.append(name)
        seen.add(name)
    assert not dupes, (
        "duplicate SCHEDULE job name(s): {d} -- both rows share one entry in "
        "last_run_hours, so their slots interfere.".format(
            d=", ".join(sorted(set(dupes))))
    )


def test_no_unexpected_registered_but_unscheduled_runners():
    """Reverse direction: a _RUNNERS entry nothing ever fires.

    Not automatically a bug -- some jobs are registered purely so they can be
    triggered by hand -- so this asserts against a documented allowlist instead
    of demanding an exact match. Subset, not equality: scheduling one of the
    allowlisted jobs later is a fine thing to do and should not fail here.
    """
    schedule, runners, _ = _wiring()
    scheduled = {name for name, _ in schedule}
    unscheduled = set(runners) - scheduled
    unexpected = unscheduled - UNSCHEDULED_BY_DESIGN
    assert not unexpected, (
        "{n} _RUNNERS entr(y/ies) are registered but never scheduled: {u}\n"
        "Nothing will ever fire these on a timer. Either add a SCHEDULE row, or "
        "-- if it is manual-trigger-only on purpose -- add it to "
        "UNSCHEDULED_BY_DESIGN in this file with a one-line reason.".format(
            n=len(unexpected), u=", ".join(sorted(unexpected)))
    )
