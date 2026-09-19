"""A test must put back the library it swapped out of sys.modules.

The incident
------------
``tests/test_envelope_migration.py`` faked requests for one call::

    sys.modules["requests"] = _FakeRequests
    try:
        env = m.probe("/x", (1, 3))
    finally:
        del sys.modules["requests"]

``del`` does not restore. The next ``import requests`` anywhere in the process
built a SECOND module object, while every file that imported requests at
collection kept the first. A file that later ran
``monkeypatch.setattr(requests, "get", fake)`` patched the first; code that
imports requests inside a function — site_planner.screen_environmental is one
— got the second and called the real client. Each file passed alone. Run
together (2026-09-13), a socket-level recorder on the patching side caught
real connections to hazards.fema.gov, services.arcgis.com and
fwsprimary.wim.usgs.gov: 16 teardown errors.

Why the existing guards missed it
---------------------------------
tests/test_no_import_time_module_stubs.py scans for MODULE-scope writes, and
this one is inside a test. Its session ledger, tests/_stub_sentinel.py, pins
``absent`` for a library nothing has imported by pytest_configure and does not
re-baseline on a first import, so real -> deleted reads as absent -> absent.

The guard
---------
tests/conftest.py::_watched_libraries_keep_their_identity reads sys.modules for
flask, psycopg2 and requests around every test and fails that test's teardown
when an entry was deleted, replaced, or left holding a stub. The decision is
tests._stub_sentinel.identity_changes, a pure function, so the controls below
prove it can fail without a leak having to happen in this process. The last
control runs the real fixture in a child pytest: the only way to prove the
wiring — that it is autouse, and that it runs after monkeypatch has put things
back.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import _stub_sentinel  # noqa: E402
from tests._import_shims import make_placeholder  # noqa: E402


def _library(name="requests"):
    """A module that looks installed: it carries a __file__."""
    mod = types.ModuleType(name)
    mod.__file__ = f"/site-packages/{name}/__init__.py"
    return mod


def _verdicts(before, after):
    return [(c["module"], c["change"])
            for c in _stub_sentinel.identity_changes(before, after)]


# ── shapes that must fail ─────────────────────────────────────────────
def test_a_deleted_library_is_flagged():
    """MUTATION CONTROL: the incident, reduced to its two readings."""
    assert _verdicts({"requests": _library()}, {"requests": None}) == [
        ("requests", "deleted")]


def test_a_second_copy_of_the_same_library_is_flagged():
    """★What a delete becomes one import later: the same __file__, a different
    object. A check comparing file paths, or only absent against present,
    passes it."""
    first, second = _library(), _library()
    assert first.__file__ == second.__file__
    assert _verdicts({"requests": first}, {"requests": second}) == [
        ("requests", "replaced")]


def test_a_fake_left_in_place_is_flagged():
    class _FakeRequests:
        pass

    assert _verdicts({"requests": _library()}, {"requests": _FakeRequests}) == [
        ("requests", "replaced")]


def test_a_stub_left_where_nothing_was_is_flagged():
    assert _verdicts({"flask": None}, {"flask": types.ModuleType("flask")}) == [
        ("flask", "stub left behind")]


def test_a_module_that_answers_any_attribute_is_not_mistaken_for_a_real_one():
    """A PEP 562 module answers `__file__` like any other name, so getattr
    cannot tell a placeholder from a library. tests._import_shims placeholders
    happen to answer with a FALSY dummy, which hides the difference from a
    truthiness check — MUT-found: reading __file__ with getattr passed an
    earlier version of this control. One answering with a string does not."""
    answers_anything = types.ModuleType("flask")
    answers_anything.__getattr__ = lambda name: f"/fake/{name}"
    assert getattr(answers_anything, "__file__", None) == "/fake/__file__"
    assert _verdicts({"flask": None}, {"flask": answers_anything}) == [
        ("flask", "stub left behind")]
    # ...and the repo's own placeholder, the one that actually gets installed.
    assert _verdicts({"flask": None}, {"flask": make_placeholder("flask")}) == [
        ("flask", "stub left behind")]


# ── shapes that must pass ─────────────────────────────────────────────
def test_an_ordinary_first_import_is_not_flagged():
    assert _verdicts({"psycopg2": None},
                     {"psycopg2": _library("psycopg2")}) == []


def test_a_library_left_alone_or_put_back_is_not_flagged():
    lib = _library()
    assert _verdicts({"requests": lib, "flask": None},
                     {"requests": lib, "flask": None}) == []


def test_the_failure_names_the_test_the_module_and_the_fix():
    flask = _library("flask")
    changes = _stub_sentinel.identity_changes(
        {"requests": _library(), "flask": flask},
        {"requests": None, "flask": flask})
    message = _stub_sentinel.describe_identity_changes(
        changes, "tests/test_offender.py::test_it")
    assert "tests/test_offender.py::test_it" in message, message
    assert "sys.modules['requests'] DELETED" in message, message
    assert "monkeypatch.setitem(sys.modules" in message, message
    # ...and it must not cry wolf over the library that was left alone.
    assert "'flask'" not in message, message


# ── the wiring ────────────────────────────────────────────────────────
def test_the_fixture_reaches_this_test(request):
    assert "_watched_libraries_keep_their_identity" in request.fixturenames


def test_the_wider_fixture_reaches_this_test_too(request):
    """Both fences are autouse in the root conftest, and both must be wired.

    They are complementary, not redundant: the narrow one baselines its three
    names as `absent` when nothing has imported them, so it can still say
    "stub left behind" for a name that was not there at the start. A snapshot
    of sys.modules has no entry for a name that is not in it, so the wide one
    is blind to that and catches the other 2,800-odd names instead.
    """
    assert "_no_module_is_left_swapped_or_deleted" in request.fixturenames


_CHILD_CONFTEST = (
    "from tests.conftest import _watched_libraries_keep_their_identity"
    "  # noqa: F401\n"
)

_CHILD_TESTS = '''
import sys

import requests


def test_a_setitem_is_put_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", object())


def test_b_del_is_not():
    sys.modules["requests"] = object()
    del sys.modules["requests"]


def test_c_an_untouched_test_after_it():
    pass
'''


def test_the_real_fixture_fails_the_teardown_of_the_test_that_deleted_requests(tmp_path):
    """END-TO-END CONTROL, in a child pytest.

    A child process, because reproducing the leak here would do the damage
    this guard exists to prevent to every file that runs after this one.
    test_a proves the fixture runs AFTER monkeypatch's undo (were it earlier,
    test_a would error too); test_b is the incident; test_c proves the leak is
    charged once, to the test that caused it, not to its neighbour.
    """
    (tmp_path / "conftest.py").write_text(_CHILD_CONFTEST, encoding="utf-8")
    (tmp_path / "test_child.py").write_text(_CHILD_TESTS, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=ROOT)
    for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         "test_child.py"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "3 passed, 1 error" in out, out
    assert "ERROR at teardown of test_b_del_is_not" in out, out
    assert "sys.modules['requests'] DELETED" in out, out
    assert "test_a_setitem_is_put_back" not in out, out
    assert "test_c_an_untouched_test_after_it" not in out, out


# ── the same end-to-end control, for the fence over every other name ───
_WIDE_CHILD_CONFTEST = (
    "from tests.conftest import _no_module_is_left_swapped_or_deleted"
    "  # noqa: F401\n"
)

# `colorsys` on purpose: a real module, present in sys.modules because this
# file imports it, outside _stub_sentinel.WATCHED, and needed by nothing that
# runs during teardown or reporting — so a failure here can only come from the
# wide fence, and deleting it cannot take pytest down with it.
_WIDE_CHILD_TESTS = '''
import sys

import colorsys  # noqa: F401


def test_a_setitem_is_put_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "colorsys", object())


def test_b_del_is_not():
    sys.modules["colorsys"] = object()
    del sys.modules["colorsys"]


def test_c_an_untouched_test_after_it():
    pass
'''


def test_the_wide_fixture_fails_the_teardown_of_a_test_that_dropped_any_module(tmp_path):
    """END-TO-END CONTROL for the widened fence, in a child pytest.

    Same shape as the control above, one name over: `colorsys` is not one of
    the three watched libraries, so before this fence existed the child ran
    three green tests and left the module gone. A child process for the same
    reason as above — reproducing the leak here would do the damage to every
    file that runs after this one.
    """
    (tmp_path / "conftest.py").write_text(_WIDE_CHILD_CONFTEST, encoding="utf-8")
    (tmp_path / "test_child.py").write_text(_WIDE_CHILD_TESTS, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=ROOT)
    for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         "test_child.py"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "3 passed, 1 error" in out, out
    assert "ERROR at teardown of test_b_del_is_not" in out, out
    assert "sys.modules['colorsys'] DELETED" in out, out
    # monkeypatch.setitem is put back, so the fence must not charge test_a...
    assert "test_a_setitem_is_put_back" not in out, out
    # ...nor the neighbour that merely ran after the leak.
    assert "test_c_an_untouched_test_after_it" not in out, out
