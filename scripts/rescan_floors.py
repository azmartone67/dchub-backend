#!/usr/bin/env python3
"""Re-measure every test's repo scan and rewrite tests/scan_floors.json.

Run this when the code legitimately changed shape — files moved, a subsystem
was split, a scan was intentionally narrowed:

    python3 scripts/rescan_floors.py

★ Do NOT run it to turn a red build green. A COVERAGE COLLAPSE failure means a
guard stopped seeing the code it protects; re-pinning the floor to the
collapsed number silently deletes that guard and hands you back the exact
green-but-blind CI this mechanism exists to end. Fix the scan first.

The floor is set ~20% below the measured size: high enough that a stale glob
trips it, loose enough that deleting a file or two does not manufacture a red
build. Floors below 4 are pinned at 1 — at that size a scan is a lookup, not a
coverage sweep, and a tighter floor would just be noise.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import _scan_floors  # noqa: E402


def floor_for(n: int) -> int:
    """Collapse detector, not an exact pin."""
    if n <= 3:
        return 1
    if n <= 12:
        return max(2, int(n * 0.6))
    return max(10, int(n * 0.8) // 10 * 10)


def main() -> int:
    import pytest

    # Measure with floors DISABLED, so a currently-collapsed scan cannot abort
    # the very run that is supposed to re-measure it.
    os.environ["DCHUB_SCAN_FLOORS"] = "0"
    _scan_floors.install()

    class _Tracker:
        def pytest_runtest_setup(self, item):
            _scan_floors.set_current_file(os.path.basename(str(item.fspath)))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = pytest.main(
            ["-q", "-p", "no:randomly", "--tb=no", os.path.join(ROOT, "tests")],
            plugins=[_Tracker()],
        )
    _scan_floors.uninstall()

    if rc not in (0, 1):  # 1 == some tests failed; still a valid measurement
        print(f"pytest exited {rc} — measurement unreliable, refusing to write")
        print(buf.getvalue()[-3000:])
        return 2

    floors: dict[str, dict[str, int]] = {}
    for test_file, scan in sorted(_scan_floors.observations.items()):
        if not test_file or test_file == "?":
            continue
        kind, n = _scan_floors.principal(scan)
        if n < _scan_floors._NOISE:
            continue
        floors[test_file] = {kind: floor_for(n)}

    out = {
        "_comment": (
            "PINNED COVERAGE FLOORS — a test whose principal repo scan falls "
            "below its floor fails. Regenerate with scripts/rescan_floors.py "
            "ONLY when the code legitimately changed shape, never to make a "
            "red build green. See tests/_scan_floors.py."
        ),
        "floors": floors,
    }
    dest = os.path.join(ROOT, "tests", "scan_floors.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {dest}: {len(floors)} scanning test files pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
