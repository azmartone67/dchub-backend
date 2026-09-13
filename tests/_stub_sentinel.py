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

The per-test half (2026-09-13)
------------------------------
The ledger is blind to a library being DELETED at run time. It pins
``absent`` for any watched name not imported by ``pytest_configure`` — the
usual case — and an ``absent -> module`` change is a first import, so it does
not re-baseline. ``del sys.modules["requests"]`` then reads as absent ->
absent, and the re-import that follows as one more first import. That is
exactly what tests/test_envelope_migration.py did. :func:`identity_changes`
closes it by comparing the registered OBJECTS around every single test —
tests/conftest.py::_watched_libraries_keep_their_identity.
"""
from __future__ import annotations

import sys
import types

__all__ = ["WATCHED", "snapshot", "checkpoint", "compare",
           "violations", "fingerprint", "reset",
           "identity_changes", "describe_identity_changes"]

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


# ═════════════════════════════════════════════════════════════════════
# Per test: the library a test swapped must be the SAME object afterwards
# ═════════════════════════════════════════════════════════════════════
def _file_of(obj):
    """``__file__`` read from a module's own namespace, else None.

    Not ``getattr``: a PEP 562 module answers ``__file__`` like any other name
    (tests._import_shims placeholders do), so getattr can hand back a value for
    a module that was never loaded from a file.
    """
    if isinstance(obj, types.ModuleType):
        return vars(obj).get("__file__")
    return None


def _label(obj) -> str:
    if obj is None:
        return "absent"
    path = _file_of(obj)
    what = f"module from {path}" if path else f"{type(obj).__name__} with no __file__"
    return f"{what} (id 0x{id(obj):x})"


def identity_changes(before: dict, after: dict) -> list[dict]:
    """What one test did to the watched names and did not put back.

    ``before`` and ``after`` map each name to the object registered under it
    (``None`` when absent), read immediately around a single test. Objects,
    compared by identity, because the incident leaves a module from the SAME
    file under the name: after ``del sys.modules["requests"]`` the next
    ``import requests`` builds a second module object — same ``__file__``,
    different ``id``. Everything that bound the first (test modules at
    collection, route modules at import) keeps it, so a patch applied through
    one reference is invisible through the other.

    ========================  ======================================
    before -> after           verdict
    ========================  ======================================
    the same object           fine
    absent -> real module     fine: an ordinary first import
    present -> absent         ``deleted``
    present -> another obj    ``replaced``
    absent -> no __file__     ``stub left behind``
    ========================  ======================================
    """
    out = []
    for name, was in before.items():
        now = after.get(name)
        if now is was:
            continue
        if was is None:
            if _file_of(now):
                continue  # a genuine first import
            change = "stub left behind"
        elif now is None:
            change = "deleted"
        else:
            change = "replaced"
        out.append({"module": name, "change": change,
                    "was": _label(was), "now": _label(now)})
    return out


_WHY = {
    "deleted": (
        "Deleting an entry does not restore it. The next import builds a SECOND "
        "module object, while every module that bound the first (test modules "
        "at collection, route modules at import) keeps it. A later "
        "monkeypatch.setattr(requests, 'get', fake) then patches an object that "
        "code importing requests inside a function no longer sees, and that "
        "code reaches the real network."),
    "replaced": (
        "Every module that bound the original keeps it, and every module "
        "imported afterwards gets the replacement, so a patch applied through "
        "one reference is invisible through the other."),
    "stub left behind": (
        "Every later import of that name in this process gets the stub, so a "
        "file that needs the library fails — but only in a run where nothing "
        "had imported the real one first, which is why a full run stays green."),
}


def describe_identity_changes(changes: list[dict], test: str) -> str:
    """The teardown failure. Names the test, the module, and the fix."""
    lines = [
        f"{test} changed what sys.modules holds for a real library and did "
        f"not put it back:",
        "",
    ]
    for c in changes:
        lines.append(f"  · sys.modules[{c['module']!r}] {c['change'].upper()}")
        lines.append(f"      before the test: {c['was']}")
        lines.append(f"      after the test:  {c['now']}")
    for change in dict.fromkeys(c["change"] for c in changes):
        lines += ["", _WHY[change]]
    lines += [
        "",
        "Fix: monkeypatch.setitem(sys.modules, name, fake), or "
        "monkeypatch.delitem(sys.modules, name) to simulate a missing library; "
        "pytest puts the ORIGINAL object back on teardown. To import a module "
        "that needs a library which may not be installed, use "
        "tests._import_shims.real_or_stub. A hand-written finally must "
        "re-assign the saved object, and `del` only a name that was absent "
        "before.",
    ]
    return "\n".join(lines)
