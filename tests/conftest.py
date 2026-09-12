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
from tests import _stub_sentinel  # noqa: E402

_FLOORS_ON = os.environ.get("DCHUB_SCAN_FLOORS", "1") != "0"
_floors = _scan_floors.load_floors() if _FLOORS_ON else {}
_checked: set = set()


def pytest_configure(config):
    if _FLOORS_ON:
        _scan_floors.install()
    # Pin the identity of the real flask/psycopg2/requests BEFORE any test
    # module is imported, so a module that swaps one for a fake can be named.
    # See tests/_stub_sentinel.py and tests/test_no_import_time_module_stubs.py.
    _stub_sentinel.snapshot()


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
        base = os.path.basename(str(collector.fspath))
        # Attribute BEFORE switching: anything that appeared in sys.modules
        # since the last checkpoint was done by the PREVIOUS file's import.
        _stub_sentinel.checkpoint()
        _scan_floors.set_current_file(base)
        _stub_sentinel.set_current_file(base)


# Guards that must observe EVERY other file before they run, ordered to the
# tail of the session. The comment on pytest_collection_modifyitems explains
# why alphabetical position is not an ordering guarantee.
#
# ★ ORDER WITHIN THIS TUPLE MATTERS, and it is the reverse of the intuitive one.
# test_scan_floors_are_pinned.py reads the scan-observation table, so it must
# run after every file that scans — the other tail guards INCLUDED. Listing it
# first would recreate, inside the very mechanism built to end it, the blind
# zone its docstring describes.
_TAIL_GUARDS = (
    # Reads the sys.modules swap ledger, which only fills as files are
    # imported. Sorts 400+ files early on its own name, so without this it
    # would report on a fraction of the session and call it clean.
    "test_no_import_time_module_stubs.py",
    # LAST. It judges everything above, this file's own scan included.
    "test_scan_floors_are_pinned.py",
)


def pytest_collection_modifyitems(session, config, items):
    """Order the pinning meta-guard LAST, so it sees every file's scans.

    ★ It used to rely on its filename sorting late. It does not. 108 of the 688
    test files sort AFTER test_scan_floors_are_pinned.py, and the guard reads
    the observation table at the moment it runs — so an unpinned scanner among
    those 108 was invisible to the very guard that exists to find it.

    That was not hypothetical: test_substations_columns.py scans 802 files via
    Path.glob and sat unpinned in the blind zone from #3149 until #3279, green
    the whole time. CI never named it, because collection order put it 100+
    files after the check. Two PRs described it as something that "would
    surface on the next run"; in natural order it never would have.

    Alphabetical position is not an ordering guarantee. Make it one.
    """
    def _rank(item):
        base = os.path.basename(str(item.fspath))
        return _TAIL_GUARDS.index(base) if base in _TAIL_GUARDS else -1

    tail = [i for i in items if _rank(i) >= 0]
    if tail:
        tail.sort(key=_rank)
        items[:] = [i for i in items if _rank(i) < 0] + tail


def pytest_runtest_setup(item):
    base = os.path.basename(str(item.fspath))
    _stub_sentinel.checkpoint()
    _scan_floors.set_current_file(base)
    _stub_sentinel.set_current_file(base)


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


# ── process-sticky DDL flags must not leak between tests (2026-08-30) ────
# founder_note._ensure_log_schema and routes.squasher_queue._ensure_table run
# their schema DDL ONCE PER PROCESS now, because `ALTER TABLE ... ADD COLUMN
# IF NOT EXISTS` takes ACCESS EXCLUSIVE **before** evaluating the condition —
# a no-op ALTER still takes the strongest lock Postgres has, and running it on
# every request is what produced the 2026-08-30 lock-timeout burst.
#
# The flag is module state, so without this fixture a DDL-CONTENT assertion
# (e.g. test_ensure_table_adds_the_bookkeeping_columns) passes or fails
# depending on whether some EARLIER test in the same process already tripped
# the flag via enqueue()/drain(). That is an order-dependent flake across 734
# test modules, and it is not hypothetical: three tests in
# test_squasher_open_identity.py went red the moment the guard landed.
#
# ★ Resets only modules ALREADY in sys.modules — it imports nothing. This
# file's whole premise is that the pure-function suite does not drag in Flask
# or the DB, and an autouse fixture that imported routes.squasher_queue for
# every test would break exactly that. If a test never loaded the module,
# there is no flag to clear.
import pytest  # noqa: E402


# ── the "tests never import main.py" house rule, ENFORCED ────────────────
# Stated in the docstrings of dozens of test files and implemented in none of
# them. What actually held it up was an ACCIDENT: four files parked a fake
# `main` in sys.modules at module scope and never removed it. Collection
# imports every module before any test runs, so that fake — installed while
# collecting a file beginning with "c" — was present for the whole execution
# phase, including files that sort earlier. The leak was load-bearing.
#
# Removing it (the point of this change) therefore let the real 45K-line
# entrypoint back in. Measured on the full suite:
#
#     merged main, leak intact      0 boot markers   20:15
#     leak removed, no replacement  138 markers      47:58
#     fake supplied for the session 0 markers        19:17
#
# The import RAISES here ("No database URL configured") after ~14.5s, so
# Python drops the partial module and the next lazy `from main import
# get_read_db` pays the cost again — which is why one unguarded import turns
# into 138 boot banners and half an hour.
#
# Session-scoped so it covers the execution phase the way the leak did, and
# undone at the end so it cannot escape the run. Files that install their own
# fake `main` (test_facility_fiber_connectivity, test_claim_ledger_followup,
# test_ai_platform_signals_source, ...) save and restore around this one, which
# is why they are unaffected.
@pytest.fixture(autouse=True, scope="session")
def _house_rule_main_is_never_imported():
    from _pytest.monkeypatch import MonkeyPatch

    from tests._import_shims import fake_main

    mp = MonkeyPatch()
    mp.setitem(sys.modules, "main", fake_main())
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _reset_process_sticky_ddl_flags():
    for _name in ("founder_note", "routes.squasher_queue"):
        _mod = sys.modules.get(_name)
        if _mod is None:
            continue
        _reset = getattr(_mod, "_reset_for_tests", None)
        if _reset is None:
            continue
        try:
            _reset()
        except Exception:
            pass
    yield
