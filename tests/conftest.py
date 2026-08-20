"""Pure-function test harness — Phase PP (2026-05-13).

These tests deliberately avoid importing the Flask app, the DB, or
any network-dependent module. They cover the pure functions on the
hot paths that have already shipped regressions this week:

  - dchub_media._pick_col              (schema-aware feed-v3)
  - routes.brain_v2_layer4._validate_proposal  (brain safety gate)
  - routes.brain_v2_layer4._auto_expand_find   (leaf-only context)
  - routes.marketing_engine._pick_daily_topic  (daily-press fallback)
  - mcp_gatekeeper._safe_echo_args     (upgrade CTA arg sanitizer)

Run with:  python3 -m pytest tests/ -v

Also installs the runtime COVERAGE FLOORS (tests/_scan_floors.py): every
repo scan a test makes is sized, and a file whose principal scan collapses
below its pinned floor fails. That closes the class where a stale glob finds
nothing and the guard reports green anyway. Set DCHUB_SCAN_FLOORS=0 to
disable locally while debugging — CI runs with it on.
"""
import os
import sys

# Make the project root importable for the test files. Avoids needing
# a setup.py / pyproject just to land minimal smoke tests.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import _scan_floors  # noqa: E402

_FLOORS_ON = os.environ.get("DCHUB_SCAN_FLOORS", "1") != "0"
_floors = _scan_floors.load_floors() if _FLOORS_ON else {}
_checked: set = set()


def pytest_configure(config):
    if _FLOORS_ON:
        _scan_floors.install()


def pytest_unconfigure(config):
    if _FLOORS_ON:
        _scan_floors.uninstall()


def pytest_collectstart(collector):
    """Attribute IMPORT-TIME scans to the module being imported.

    Some guards scan at module scope, or import a source module that scans at
    ITS module scope. Those run during collection, before any test starts, so
    with only a runtest_setup hook they were credited to whichever file
    happened to be current — usually the wrong one, and inconsistently between
    a full-suite run and a single-file run.

    ★ Attribution is deliberately NOT gated on _FLOORS_ON. scripts/rescan_floors.py
    measures with enforcement off, and when these hooks went quiet in that mode
    the measured attribution differed from the enforced attribution — so a
    module-scope scan got pinned under one filename and checked under another,
    and the suite went red immediately after a clean re-measure. Recording is
    harmless; only the CHECK is conditional.
    """
    if hasattr(collector, "fspath"):
        _scan_floors.set_current_file(os.path.basename(str(collector.fspath)))


def pytest_runtest_setup(item):
    _scan_floors.set_current_file(os.path.basename(str(item.fspath)))


def pytest_runtest_teardown(item, nextitem):
    """Check a file's floor once its last test has run.

    Deferred to the file boundary because a file's principal scan may happen in
    any of its tests — judging after the first would compare against a partial
    observation.
    """
    if not _FLOORS_ON:
        return
    this = os.path.basename(str(item.fspath))
    nxt = os.path.basename(str(nextitem.fspath)) if nextitem is not None else None
    if this == nxt or this in _checked:
        return
    _checked.add(this)
    # ★ Pass the scan through even when EMPTY. A pinned file that scanned
    # nothing is the most complete collapse possible, and returning early on
    # a falsy scan would silently exempt it — the same fail-open shape this
    # whole mechanism exists to end.
    scan = _scan_floors.observations.get(this) or {}
    err = _scan_floors.check(this, scan, _floors)
    if err:
        raise AssertionError(err)
