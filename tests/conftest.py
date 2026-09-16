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
import threading

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


# ── Fire-and-forget cache refreshes do not fetch under the suite ──────────
#
# ai_surface_canon.resolve_public_floors_cached() and
# resolve_server_version_cached() answer from cache and, when it is cold or
# stale, start ONE daemon thread (public-floors-refresh, server-version-refresh)
# to refill it FOR LATER. On a request path that is the whole point — the
# docstrings there record resolve_public_floors() at 7.59s / 7.78s / 15.46s
# against a 15s edge timeout. In a test process there is no later: nothing
# consumes the refilled cache, and the thread outlives the test that started it.
#
# MEASURED 2026-09-16, 8 runs of test_agent_surfaces_withdrawn_dcgi.py followed
# by test_agent_tool_identifier_survives_markdown.py: the first file starts both
# threads through routes/agents_md_fallback.py, and their lookups landed while
# the SECOND file's tests were running, every run. That file renders a Jinja
# template and reads routes/*.py — run alone under the same hook it opens no
# socket at all — so the unit-tests step's no-network rule failed on a file that
# has not changed since 2026-09-05, and on a different one of its tests each
# time.
#
# Nine test modules start these threads in a full suite, 18 starts in all, so
# the file that takes the blame is whichever one is running when a lookup
# lands — not a property of the blamed file at all.
#
# ★ WHY THE FIX IS HERE AND NOT IN THE HOOK'S ATTRIBUTION. The obvious repair
# looks like "stop reading PYTEST_CURRENT_TEST off the main thread" — it names
# the test pytest runs in the MAIN thread. Measured over a full suite before
# writing that: of 822 refusals, 660 come off a worker thread, and only 21 of
# those 660 are these unbounded refreshers. The rest are pool threads inside a
# call the running test makes and joins (site_sentinel 410, radar 189,
# schema_org_saturation 12). For those the running test IS the owner and the
# label is right; the hook cannot tell a bounded worker from a fire-and-forget
# daemon, so that change would have mislabelled 639 correct attributions to
# fix 21. What is wrong is the UNBOUNDED refresh, so that is what is stopped.
#
# The guard is per-THREAD, not per-module: a test that calls a refresher
# DIRECTLY still gets the real function, because that call is on the main
# thread and is bounded by the test making it. test_recommend_serves_live_
# facility_floor.py drives _refresh_public_floors() synchronously with
# resolve_canon monkeypatched, and is untouched by this.
_BACKGROUND_REFRESHERS = {
    # module -> (function, in-flight flag, lock protecting the flag)
    "ai_surface_canon": (
        ("_refresh_public_floors", "_public_floors_refreshing", "_public_floors_lock"),
        ("_refresh_server_version", "_server_version_refreshing", "_server_version_lock"),
    ),
}
_guarded_refreshers: set = set()


def _main_thread_only(module, real, flag, lock):
    """`real` on the main thread; on any other thread, clear the in-flight flag
    and return without fetching.

    ★ Clearing the flag is not tidiness. The real function clears it in a
    finally, and the module only ever starts a refresh when it is unset — so a
    wrapper that left it set would silently stop every LATER spawn in the
    process. That hides the next background fetch instead of preventing it,
    which is exactly the failure this whole comment is about.
    """
    def refresh():
        if threading.current_thread() is threading.main_thread():
            return real()
        with getattr(module, lock):
            setattr(module, flag, False)

    refresh.__name__ = getattr(real, "__name__", "refresh")
    refresh.__doc__ = getattr(real, "__doc__", None)
    refresh.__wrapped__ = real
    return refresh


def _no_background_fetches():
    """Install the guard on every refresher module that has been imported.

    Lazy on purpose: this file must not import a network-dependent module just
    to patch it, and a module nothing imports needs no guard. Called from both
    collectstart and runtest_setup so an import-time spawn is covered too.
    """
    for name, entries in _BACKGROUND_REFRESHERS.items():
        module = sys.modules.get(name)
        if module is None or name in _guarded_refreshers:
            continue
        _guarded_refreshers.add(name)
        for function, flag, lock in entries:
            real = getattr(module, function, None)
            if real is not None:
                setattr(module, function, _main_thread_only(module, real, flag, lock))


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
    _no_background_fetches()


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
    _no_background_fetches()
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


# ── a test must put back the library it swapped (2026-09-13) ─────────────
# tests/test_envelope_migration.py faked requests with
# `sys.modules["requests"] = fake` and cleaned up with `del`. Deleting is not
# restoring: the next `import requests` built a second module object, while
# every file that imported requests at collection still held the first. A
# later `monkeypatch.setattr(requests, "get", fake)` in such a file patched an
# object that code importing requests inside a function never saw, so the
# stubbed call went to the real network — and only when both files shared a
# process, so each one passed alone.
#
# The session ledger (tests/_stub_sentinel.py) could not see it; its docstring
# says why. This compares the registered objects around every test instead.
#
# ★ Autouse in the ROOT conftest, so it is set up before every function-scoped
# fixture the test or its module requests — monkeypatch included — and torn
# down after them. The check runs once monkeypatch has already put things
# back, which is what lets monkeypatch.setitem(sys.modules, ...) pass.
@pytest.fixture(autouse=True)
def _watched_libraries_keep_their_identity(request):
    before = {name: sys.modules.get(name) for name in _stub_sentinel.WATCHED}
    yield
    after = {name: sys.modules.get(name) for name in _stub_sentinel.WATCHED}
    changes = _stub_sentinel.identity_changes(before, after)
    if changes:
        pytest.fail(_stub_sentinel.describe_identity_changes(
            changes, request.node.nodeid), pytrace=False)
