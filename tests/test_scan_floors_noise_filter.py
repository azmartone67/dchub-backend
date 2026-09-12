#!/usr/bin/env python3
"""The coverage floor must measure OUR code, not the size of the checkout.

NO NETWORK, NO DB, NO APP BOOT.

Why this exists
---------------
_scan_floors wraps the scan primitives and counts what they yield. Callers that
skip vendored paths with `continue` (rather than pruning `dirnames`) have
already had those paths counted by the time they reject them — so directories
the guard never reads propped up its floor. Three incidents, one cause:

  #3868  docs/mcp-helper-pkg/.venv retired -> 10 floors re-pinned
  #3871  the dchub-frontend mirror retired ->  1 floor re-pinned, main went red
  test_honest_numbers.py:506 records a third (.claude/worktrees)

_counted() now skips noise segments when COUNTING. This file pins that it skips
the right things, counts the right things, and — the part that matters most —
that it still hands the caller every item unchanged.
"""
import os
import pathlib

from tests import _scan_floors


def test_noise_paths_are_not_counted():
    assert _scan_floors._is_noise("repo/.venv/lib/python3.9/site-packages/x.py", "glob")
    assert _scan_floors._is_noise("repo/node_modules/left-pad/index.js", "glob")
    assert _scan_floors._is_noise("repo/routes/__pycache__/x.cpython-313.pyc", "glob")
    assert _scan_floors._is_noise("repo/.git/objects/ab/cdef", "glob")
    assert _scan_floors._is_noise(".claude/worktrees/foo/main.py", "glob")


def test_real_code_is_counted():
    for good in ("routes/brain_capability_ledger.py", "main.py",
                 "docs/mcp-helper-pkg/pyproject.toml", "tests/conftest.py"):
        assert not _scan_floors._is_noise(good, "glob"), good


def test_substring_matches_do_not_false_positive():
    """`venv` must match a path SEGMENT, not any occurrence of the letters.

    Without the (^|/)…(/|$) anchors, a legitimate file like `my_venv_docs.md`
    or `routes/site-packages-report.py` would stop being counted — the guard
    would quietly under-measure real code, which is the same fail-open
    direction this whole module exists to close.
    """
    for good in ("docs/my_venv_docs.md", "routes/site-packages-report.py",
                 "scripts/build_node_modules_report.py", "a/venvish/b.py"):
        assert not _scan_floors._is_noise(good, "glob"), good


def test_walk_tuples_are_read_by_their_dirpath():
    assert _scan_floors._is_noise((".venv/lib", ["x"], ["y.py"]), "walk")
    assert not _scan_floors._is_noise(("routes", ["brain"], ["x.py"]), "walk")


def test_pathlib_paths_are_handled():
    assert _scan_floors._is_noise(pathlib.Path("repo/.venv/x.py"), "glob")
    assert not _scan_floors._is_noise(pathlib.Path("repo/routes/x.py"), "glob")


def test_an_unrecognised_item_still_counts():
    """Fail OPEN into being measured, never silently uncounted."""
    assert _scan_floors._is_noise(object(), "glob") is False


def test_the_wrapper_is_transparent(tmp_path):
    """★ The count is filtered. What the CALLER receives must not be.

    If the wrapper dropped noise items from the stream instead of just from the
    tally, every guard in the suite would silently stop scanning those paths —
    a far worse bug than the one being fixed, and invisible because the tests
    would still pass.
    """
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "real.py").write_text("x = 1")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "real.cpython-313.pyc").write_text("noise")

    with _scan_floors.temporarily_installed():
        _scan_floors.set_current_file("test_scan_floors_noise_filter.py")
        walked = list(os.walk(tmp_path))
        globbed = sorted(str(p) for p in pathlib.Path(tmp_path).rglob("*"))

    dirs = {os.path.basename(d) for d, _, _ in walked}
    assert "__pycache__" in dirs, "walk stopped yielding the noise dir to the caller"
    assert any("real.py" in g for g in globbed)
    assert any("__pycache__" in g for g in globbed), (
        "rglob stopped yielding noise to the caller — the wrapper is no longer "
        "transparent and every guard's scan just changed")


def test_pruning_idiom_still_works(tmp_path):
    """dirnames[:] pruning must keep working — the wrapper stays lazy.

    _scan_floors' own docstring records that an earlier version consumed the
    walk eagerly to count it, which broke this idiom and 11 real tests.
    """
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "a.py").write_text("")
    (tmp_path / "skipme").mkdir()
    (tmp_path / "skipme" / "b.py").write_text("")

    with _scan_floors.temporarily_installed():
        _scan_floors.set_current_file("test_scan_floors_noise_filter.py")
        seen = []
        for dirpath, dirnames, _files in os.walk(tmp_path):
            dirnames[:] = [d for d in dirnames if d != "skipme"]
            seen.append(os.path.basename(dirpath))
    assert "skipme" not in seen, "dirnames pruning stopped working — walk is eager"
    assert "keep" in seen


def test_temporary_install_restores_the_session_wide_wrappers():
    """★ A block that installs must not switch the meter off for everyone else.

    install() is idempotent-guarded, uninstall() is not, so the old
    `install(); try: ... finally: uninstall()` in the two tests above left the
    wrappers OFF for the rest of the session. Every file collected afterwards
    scanned unmeasured, which means:

      · a pinned file that scans inside a test body goes RED with "performed
        NO repo scan at all this run" — blaming the guard, not the meter;
      · test_scan_floors_are_pinned.py reads the same table, so an UNPINNED
        late scanner is invisible to the adoption check that exists to find it.

    The window was real but quiet: a module-scope scan is recorded during
    collection, before any test body runs, so the pinned files that sort after
    this one kept their observations. It surfaced the moment a pinned file
    scanned at run time and ran later (test_no_import_time_module_stubs.py,
    which conftest tail-orders).
    """
    _scan_floors.install()          # the state pytest_configure leaves behind
    assert _scan_floors._installed

    with _scan_floors.temporarily_installed():
        assert _scan_floors._installed
    assert _scan_floors._installed, (
        "temporarily_installed() left the wrappers OFF. Every scan after this "
        "point in the session goes unrecorded.")

    # ...and it must still be honest in the other direction: started off,
    # ends off.
    _scan_floors.uninstall()
    with _scan_floors.temporarily_installed():
        assert _scan_floors._installed
    assert not _scan_floors._installed, (
        "temporarily_installed() switched the wrappers ON for the rest of the "
        "session in a context that had them off.")

    _scan_floors.install()          # leave the session as we found it


def test_a_background_threads_scan_is_never_credited_to_a_test():
    """★ Attribution is per-TEST, and a worker thread is not in any test.

    `_current` is a single global and only `observations` is locked, so before
    this fix a scan on a background thread was credited to whichever test the
    main thread was running at that instant. Two failure modes, and the second
    is the bad one:

      · it invents a scan for a file that does not scan — the adoption guard
        then names an innocent file, a different one each run;
      · it can TOP UP a pinned file whose real scan collapsed, so a genuine
        collapse reads green. Fail-open, through the front door of the
        mechanism built to prevent it.

    Reproduced from routes/ddl_audit.py's `ddl-audit-boot` thread: the same
    stray walk=93 landed on test_async_admin_calls_are_polled.py on one tree
    and test_app_contract_gate.py on another, and on
    test_ausgrid_au_forecast_pin.py in a full run. None of the three scans
    anything.
    """
    import threading

    _scan_floors.install()
    try:
        _scan_floors.set_current_file("test_the_innocent_bystander.py")
        _scan_floors.observations.pop("test_the_innocent_bystander.py", None)
        _scan_floors.background_observations.clear()

        def _walker():
            # A real scan, on a thread that belongs to no test.
            list(os.walk(os.path.dirname(os.path.abspath(__file__))))

        t = threading.Thread(target=_walker, name="pretend-boot-audit")
        t.start()
        t.join()

        assert "test_the_innocent_bystander.py" not in _scan_floors.observations, (
            "a background thread's scan was credited to the test that happened "
            "to be running: "
            f"{_scan_floors.observations.get('test_the_innocent_bystander.py')}")
        assert "pretend-boot-audit" in _scan_floors.background_observations, (
            "the off-thread scan was dropped entirely rather than recorded "
            "separately — a guard that legitimately scans from a worker thread "
            "would vanish with no trace to diagnose")

        # ...and the main thread must still be credited normally.
        list(os.walk(os.path.dirname(os.path.abspath(__file__))))
        assert _scan_floors.observations.get("test_the_innocent_bystander.py"), (
            "the main thread stopped being recorded — the fix went too far and "
            "every floor is now unmeasured")
    finally:
        _scan_floors.observations.pop("test_the_innocent_bystander.py", None)
        _scan_floors.background_observations.clear()
        _scan_floors.uninstall()
        _scan_floors.install()
