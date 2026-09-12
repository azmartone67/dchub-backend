"""Session sentinel: catch a test module that swaps a real library for a fake.

Companion to :mod:`tests._import_shims`. That module makes the shim safe; this
one proves no shim (present or future, here or in a file nobody has written
yet) escapes the module that installed it.

How attribution works
---------------------
The damage is done at IMPORT time, during collection, so a check that only runs
at session end can say *that* a library was replaced but not *by whom*.
:func:`checkpoint` is therefore called from the same conftest hooks that already
track the current file for the coverage floors — ``pytest_collectstart``,
``pytest_runtest_setup`` and ``pytest_sessionfinish``. Each call compares
``sys.modules`` against the last known-good state and blames the file that was
current when the swap appeared.

Why identity and not truthiness
-------------------------------
``sys.modules["flask"]`` is a module either way; a placeholder is a module too.
What separates them is *which object* it is, so the sentinel pins ``id()`` and
the resolved ``__file__`` at session start and compares both. A placeholder has
no ``__file__`` at all — see ``feedback_suite_stubs_a_module_into_sys_modules``:
"repr w/o from=stub".

Limits, stated rather than assumed
----------------------------------
In a FULL-directory run this sentinel is expected to stay quiet forever: an
alphabetically early module imports the real libraries before any shim gets the
chance, so the swap never happens. That makes it a backstop, not a detector —
it cannot be the only guard, and it is not. The detector is the static scan in
``tests/test_no_import_time_module_stubs.py``, which reads the SHAPE of the
code and so fires in a full run and a subset run alike. This file also ships
:func:`compare` as a pure function so its own can-it-fail control does not
depend on a leak actually occurring.
"""
from __future__ import annotations

import sys

__all__ = ["WATCHED", "snapshot", "checkpoint", "compare",
           "violations", "fingerprint", "reset"]

# Third-party libraries that live in requirements.txt and are installed in CI
# (see .github/workflows/pre-merge.yml, job `unit-tests`), so a fake standing in
# for one of them is always wrong rather than sometimes necessary.
WATCHED = ("flask", "psycopg2", "requests")

_baseline: dict[str, tuple] = {}
_current_file = {"name": "<collection>"}
violations: list[dict] = []


def fingerprint(mod) -> tuple:
    """Identity of whatever is registered under a name.

    ``(id, __file__)``. ``None`` for a name that is absent. The file path is
    carried alongside the id because a placeholder has none, which makes the
    failure message able to say *what* replaced the library, not just that
    something did.
    """
    if mod is None:
        return ("absent", None)
    return (id(mod), getattr(mod, "__file__", None))


def snapshot(names=WATCHED) -> None:
    """Pin the known-good state. Called once, at ``pytest_configure``."""
    _baseline.clear()
    violations.clear()
    for name in names:
        _baseline[name] = fingerprint(sys.modules.get(name))


def reset() -> None:
    """Drop all state. For the sentinel's own tests."""
    _baseline.clear()
    violations.clear()
    _current_file["name"] = "<collection>"


def compare(baseline: dict, now: dict, blame: str) -> list[dict]:
    """Pure diff of two fingerprint tables — the testable core.

    Returns one record per name whose registered object changed. A name that
    was ABSENT and is now a real importable module is not a violation: that is
    an ordinary first import, which is what is supposed to happen.
    """
    out = []
    for name, was in baseline.items():
        is_now = now.get(name, ("absent", None))
        if is_now == was:
            continue
        if was[0] == "absent" and is_now[1]:
            # absent -> a module with a real __file__: a genuine first import.
            continue
        out.append({
            "module": name,
            "blamed_file": blame,
            "was": was,
            "now": is_now,
            "looks_like_a_stub": is_now[0] != "absent" and is_now[1] is None,
        })
    return out


def set_current_file(name: str) -> None:
    _current_file["name"] = name


def checkpoint(blame: str | None = None) -> list[dict]:
    """Compare against the baseline and RE-BASELINE, blaming ``blame``.

    Re-baselining is deliberate: without it a single swap would be re-reported
    against every file collected afterwards, and the first (correct) name would
    be lost in the noise. Each swap is attributed once, to the file that was
    current when it appeared.
    """
    if not _baseline:
        return []
    who = blame or _current_file["name"]
    now = {name: fingerprint(sys.modules.get(name)) for name in _baseline}
    found = compare(_baseline, now, who)
    if found:
        violations.extend(found)
        _baseline.update(now)
    return found


def describe(records=None) -> str:
    """The failure message. Names the module AND the file that swapped it."""
    records = violations if records is None else records
    if not records:
        return ""
    lines = [
        f"{len(records)} test module(s) replaced a real, installed library in "
        f"sys.modules and did not put it back:",
        "",
    ]
    for r in records:
        kind = "a STUB (no __file__)" if r["looks_like_a_stub"] else "a different object"
        lines.append(
            f"  · sys.modules[{r['module']!r}] became {kind} while collecting "
            f"{r['blamed_file']}"
        )
        lines.append(f"      was: {r['was']}")
        lines.append(f"      now: {r['now']}")
    lines += [
        "",
        "Every module imported after that point got the fake instead of the "
        "library. In a whole-directory run the damage is usually invisible; in "
        "a SUBSET run the affected files are skipped or error, so a developer "
        "verifying 'just the suites my change touches' reads a false green.",
        "",
        "Fix: import the real module (tests._import_shims.real_or_stub) or "
        "install the fake through a fixture that restores it "
        "(monkeypatch.setitem(sys.modules, ...)). Never assign at module scope: "
        "collection has no teardown hook, so nothing can undo it.",
    ]
    return "\n".join(lines)
