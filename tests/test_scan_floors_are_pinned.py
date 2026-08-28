"""META-GUARD: every repo-scanning test must be pinned in scan_floors.json.

Without this, the floors decay the way every convention in this suite has: the
next scanning guard gets written without a floor, fails open from birth, and
nobody notices because it is green.

The runtime mechanism (tests/_scan_floors.py) can only enforce a floor it has.
A scanning file absent from the manifest is silently unprotected — this test is
what makes that absence loud.

It must run LAST, so the observation table is populated by the time it reads
it. That ordering is now FORCED by pytest_collection_modifyitems in
tests/conftest.py. It used to be left to the filename "sorting late", which was
simply false: 108 test files sort after this one, and an unpinned scanner among
them could never be seen by the check meant to find it. test_substations_columns.py
(802 files via Path.glob) lived in that blind zone unpinned and green from #3149
to #3279. Under -k filtering or a single-file run it sees only what actually
ran, so it checks what it observed and says so rather than pretending to a full
sweep.
"""
from __future__ import annotations

import os

from tests import _scan_floors

_HERE = os.path.dirname(os.path.abspath(__file__))


def test_scanning_tests_are_pinned():
    floors = _scan_floors.load_floors()
    observed = _scan_floors.observations

    unpinned = []
    for test_file, scan in sorted(observed.items()):
        if not test_file or test_file == "?":
            continue
        if test_file == os.path.basename(__file__):
            continue
        _kind, n = _scan_floors.principal(scan)
        if n < _scan_floors._ADOPTION_MIN:
            # Too small to be real coverage, and small scans vary between CI
            # and local checkouts — demanding a pin here makes this test red in
            # one environment and green in the other. See _ADOPTION_MIN.
            continue
        if test_file not in floors:
            unpinned.append(f"{test_file} (principal scan: {_kind}={n})")

    assert not unpinned, (
        f"{len(unpinned)} test file(s) scan the repo with no pinned coverage "
        f"floor. Each reports GREEN when its scan finds nothing — the exact "
        f"shape that lets a stale glob delete a guard invisibly:\n  - "
        + "\n  - ".join(unpinned)
        + "\n\nFix:  python3 scripts/rescan_floors.py"
    )


def test_manifest_has_no_stale_entries():
    """A pinned file that no longer exists means the manifest is drifting."""
    floors = _scan_floors.load_floors()
    missing = [f for f in floors if not os.path.exists(os.path.join(_HERE, f))]
    assert not missing, (
        "These files are pinned in scan_floors.json but no longer exist:\n  - "
        + "\n  - ".join(sorted(missing))
        + "\n\nRun: python3 scripts/rescan_floors.py"
    )


def test_floors_are_positive():
    """A floor of 0 is an unarmed guard wearing a guard's uniform."""
    floors = _scan_floors.load_floors()
    dead = {f: e for f, e in floors.items()
            if not e or any(v < 1 for v in e.values())}
    assert not dead, (
        "These pinned floors are 0 or empty, so they can never fail:\n  - "
        + "\n  - ".join(sorted(dead))
    )


def test_this_guard_is_ordered_last_not_merely_named_late():
    """★REGRESSION. The ordering this file depends on must be ENFORCED.

    The original relied on `test_scan_floors_are_pinned.py` sorting late among
    the test filenames. It does not sort late — 108 files sort after it — so
    the guard read a partial observation table and called it a full sweep.
    That is the same "green means nothing" shape the floors mechanism exists
    to remove, reproduced inside the mechanism's own meta-guard.

    This asserts the conftest hook actually moves it, rather than trusting the
    filename to keep doing a job it was never doing.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_ck", os.path.join(_HERE, "conftest.py"))
    ck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ck)

    class _Item:
        def __init__(self, name):
            self.fspath = name

    # A name that sorts AFTER this guard is exactly the case that was blind.
    items = [_Item("test_substations_columns.py"),
             _Item(os.path.basename(__file__)),
             _Item("test_zzz_hypothetical.py")]
    ck.pytest_collection_modifyitems(None, None, items)
    names = [str(i.fspath) for i in items]
    assert names[-1] == os.path.basename(__file__), (
        "the pinning guard is not ordered last (%s) — files after it scan "
        "unobserved and the check silently covers less than it claims" % names)
