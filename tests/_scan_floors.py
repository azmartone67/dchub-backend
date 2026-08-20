"""Runtime coverage floors — a guard must PROVE it observed something.

The bug class this closes
-------------------------
A guard that scans the repo and finds nothing reports GREEN. That result is
byte-identical to "scanned all 120 modules, every one clean". CI cannot tell
them apart, so any refactor that moves files out from under a glob silently
deletes the guard and leaves the green check standing.

Mutation-proved 2026-08-20 against tests/test_brain_loggers_defined.py: it
scans 118 modules today. Repointing its glob at a plausible refactor target
(routes/brain/*.py instead of routes/brain_*.py) dropped it to 2 modules — and
it still exited 0, still printed "1 passed". Coverage fell 98%, nothing went
red. 50 test files in this suite scan the repo. Every one of them fails open.

Why this is enforced at RUNTIME rather than by editing each test
---------------------------------------------------------------
Editing 50 call sites is a one-off patch fifty times over: it decays the moment
someone writes test 51 without a floor. Wrapping the scan primitives instead
means the floor applies to every test that scans, including ones not yet
written, and the pinned numbers live in ONE reviewable file
(tests/scan_floors.json) instead of scattered through the suite.

How it works
------------
During a test run the scan primitives (glob.glob, glob.iglob, os.walk,
Path.glob, Path.rglob) are wrapped to record the size of each scan, keyed by
the test file that made it. After each test file finishes, the LARGEST scan it
performed is compared against its pinned floor. A file whose principal scan
collapsed fails loudly, naming the drop.

Max is the right aggregator: a file that scans the repo (782 files) and also
walks a tmp_path (3 files) should be judged on its principal scan, not its
smallest one.

Floors are COLLAPSE detectors, not exact pins — set ~20% under the true count.
High enough that a stale glob trips it, loose enough that deleting a file or
two does not manufacture a red build. A fence that cries wolf gets deleted, and
a deleted fence protects nothing.

Maintenance
-----------
    python3 -m pytest tests/ -q            # a collapse fails the owning file
    python3 scripts/rescan_floors.py       # re-measure + rewrite the manifest

Only re-measure when the code legitimately changed shape. Re-measuring to make
a red build green is how you turn this into decoration.
"""
from __future__ import annotations

import glob as _glob_mod
import json
import os
import pathlib
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_MANIFEST = os.path.join(_HERE, "scan_floors.json")

# Scans smaller than this are never a file's principal scan — almost always a
# tmp_path fixture or a single-file lookup, not a coverage sweep.
_NOISE = 3

# ...and below this we do not DEMAND a pin. Separate from _NOISE on purpose:
# scan sizes at the low end vary by environment (test_media_card walks 2
# directories locally and 6 in CI, because the tree differs), so demanding a pin
# there produces a check that is red in CI and green locally. That flapping is
# how a fence gets deleted. A scan this small is not carrying real coverage, and
# a pinned file is still floor-checked at ANY size — this only governs whether
# an UNPINNED file is nagged into the manifest.
_ADOPTION_MIN = 8

_lock = threading.Lock()
# {test_file_basename: {kind: max_size}}
observations: dict[str, dict[str, int]] = {}
_current: dict[str, str] = {"file": ""}

_installed = False
_originals: dict = {}


def _record(kind: str, n: int) -> None:
    f = _current["file"]
    if not f:
        return
    with _lock:
        slot = observations.setdefault(f, {})
        if n > slot.get(kind, -1):
            slot[kind] = n


def set_current_file(basename: str) -> None:
    _current["file"] = basename


def install() -> None:
    """Wrap the scan primitives. Idempotent."""
    global _installed
    if _installed:
        return
    _originals["glob"] = _glob_mod.glob
    _originals["iglob"] = _glob_mod.iglob
    _originals["walk"] = os.walk
    _originals["pglob"] = pathlib.Path.glob
    _originals["prglob"] = pathlib.Path.rglob

    def glob_(pathname, *a, **k):
        r = _originals["glob"](pathname, *a, **k)
        _record("glob", len(r))
        return r

    # ★ These MUST stay lazy. The first version of this module did
    # `list(os.walk(top))` to count in one shot — which consumes the whole walk
    # before the caller sees a single tuple, so the standard
    # `dirnames[:] = [d for d in dirnames if ...]` pruning idiom silently stops
    # working and the test descends into node_modules/.git/venv. That broke 11
    # real tests. Count incrementally while yielding instead: _record keeps a
    # max, so partial consumption still registers an honest lower bound and
    # full consumption converges on the true total.

    def _counted(it, kind):
        n = 0
        for item in it:
            n += 1
            _record(kind, n)
            yield item

    def iglob_(pathname, *a, **k):
        return _counted(_originals["iglob"](pathname, *a, **k), "glob")

    def walk_(top, *a, **k):
        # counts DIRECTORIES (tuples yielded); a stale walk root yields 0-1
        return _counted(_originals["walk"](top, *a, **k), "walk")

    def pglob_(self, pattern, *a, **k):
        return _counted(_originals["pglob"](self, pattern, *a, **k), "pglob")

    def prglob_(self, pattern, *a, **k):
        return _counted(_originals["prglob"](self, pattern, *a, **k), "prglob")

    _glob_mod.glob = glob_
    _glob_mod.iglob = iglob_
    os.walk = walk_
    pathlib.Path.glob = pglob_
    pathlib.Path.rglob = prglob_
    _installed = True


def uninstall() -> None:
    global _installed
    if not _installed:
        return
    _glob_mod.glob = _originals["glob"]
    _glob_mod.iglob = _originals["iglob"]
    os.walk = _originals["walk"]
    pathlib.Path.glob = _originals["pglob"]
    pathlib.Path.rglob = _originals["prglob"]
    _installed = False


def load_floors() -> dict:
    if not os.path.exists(_MANIFEST):
        return {}
    with open(_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh).get("floors", {})


def principal(scan: dict[str, int]) -> tuple[str, int]:
    """The file's biggest scan — the one that carries its coverage."""
    if not scan:
        return ("", 0)
    kind, n = max(scan.items(), key=lambda kv: kv[1])
    return (kind, n)


def check(test_file: str, scan: dict[str, int], floors: dict) -> str | None:
    """Return an error string if this file's principal scan collapsed.

    ★ The _NOISE cutoff deliberately applies ONLY to unpinned files. A total
    collapse produces a TINY number — the mutation that repointed
    test_brain_loggers_defined.py at a stale path left it scanning 1 file — so
    skipping small scans for a PINNED file would ignore exactly the failure
    this mechanism exists to catch. That is the "treat unobserved as success"
    bug, and it survived the first mutation test of this module until fixed.

    Once a file is pinned, ANY drop below its floor fails, including to 0.
    """
    kind, n = principal(scan)
    entry = floors.get(test_file)
    if entry is None:
        # Unpinned: only the adoption test cares, and only for real sweeps.
        return None
    if not scan:
        # Pinned file that scanned NOTHING — the most total collapse there is,
        # and the easiest to miss, because an absent observation looks like an
        # absent test rather than a broken one.
        return (
            f"COVERAGE COLLAPSE in {test_file}: it is pinned to scan "
            f"{entry}, but performed NO repo scan at all this run.\n\n"
            f"Its scan was removed, renamed, or short-circuited before running. "
            f"A guard that scans nothing cannot fail, so its green result "
            f"carries no information.\n\n"
            f"Restore the scan. If the guard genuinely no longer scans, drop "
            f"its entry from tests/scan_floors.json in the same PR."
        )
    floor = entry.get(kind)
    if floor is None:
        # File scans by a kind it did not before — treat the pinned principal
        # as the bar so a switched primitive cannot dodge the floor.
        floor = max(entry.values()) if entry else None
        if floor is None:
            return None
    if n >= floor:
        return None
    return (
        f"COVERAGE COLLAPSE in {test_file}: its principal scan ({kind}) found "
        f"{n} items, floor is {floor}.\n\n"
        f"This guard did not find the code it exists to protect, so green here "
        f"would mean nothing. Almost always a stale glob after files moved or "
        f"were renamed — the guard stopped covering them while CI kept passing.\n\n"
        f"Repoint the scan at the real code. Only lower the floor if the code "
        f"genuinely shrank, and do it in the PR that shrinks it — never to turn "
        f"this red build green."
    )
