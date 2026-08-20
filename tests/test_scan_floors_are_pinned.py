"""META-GUARD: every repo-scanning test must be pinned in scan_floors.json.

Without this, the floors decay the way every convention in this suite has: the
next scanning guard gets written without a floor, fails open from birth, and
nobody notices because it is green.

The runtime mechanism (tests/_scan_floors.py) can only enforce a floor it has.
A scanning file absent from the manifest is silently unprotected — this test is
what makes that absence loud.

It runs LAST (the filename sorts late) so the observation table is populated by
the time it reads it. Under -k filtering or a single-file run it sees only what
actually ran, so it checks what it observed and says so rather than pretending
to a full sweep.
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
