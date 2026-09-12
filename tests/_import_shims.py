"""Import-time module shims that CANNOT escape the module that installs them.

The bug class this closes
-------------------------
A test module that needs to import application code without dragging in a heavy
dependency writes a placeholder into ``sys.modules`` and imports on top of it.
Two things then go wrong, and they compound:

1. **The wrong question.** The idiom was::

       if "flask" not in sys.modules:
           sys.modules["flask"] = types.ModuleType("flask")

   ``name not in sys.modules`` means *"has anything imported it YET?"*, not
   *"is it installed?"*. Flask is in requirements.txt and CI installs it, so the
   real module is always available — but at the moment the FIRST test module
   runs, nothing has imported it, so the placeholder wins and the real library
   is shadowed for the rest of the process.

2. **It never comes back.** A module-scope assignment runs during COLLECTION,
   where neither ``monkeypatch`` nor a fixture finalizer exists. Nothing
   restores it, so every later module in the same process imports the fake.

Measured on 2026-09-12 at 2fdebb9bf: ``tests/test_brain_smoke_regression.py``
did exactly this for flask/psycopg2/requests. Alone it collected 0 tests (its
own import chain needs ``from flask import Blueprint``, which an empty
placeholder cannot serve). Paired with any module that imports real flask, BOTH
files errored — 7 tests collected alone became 0 collected together. CI never
saw it: a whole-directory run imports real flask from an alphabetically earlier
file first, so the predicate is False and the placeholder is never installed.
Only a SUBSET run — "just the suites my change touches" — trips it, which is
the one run a developer trusts without a second opinion.

What this module does instead
-----------------------------
:func:`real_or_stub` asks the right question (can the module be IMPORTED?) and
guarantees the answer is restored:

* the real module always wins — it is imported, so a later ``import flask``
  gets the library rather than a placeholder;
* a placeholder is installed only for a name that genuinely cannot be imported;
* every placeholder this helper installed is removed on the way out, whether
  the block exits normally or by exception, so nothing survives into the next
  test module.

The placeholder is permissive on purpose. The empty ``types.ModuleType`` it
replaces could not satisfy ``from flask import Blueprint`` — so the old shim
failed in precisely the dependency-free environment it claimed to support.
:class:`_Placeholder` answers any attribute with a callable/subscriptable dummy,
which is enough for the ``Blueprint``/``jsonify``/``request`` surface these
route modules touch at import time.

``tests/test_no_import_time_module_stubs.py`` is the ratchet that keeps the old
idiom from coming back.
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
import types

import pytest

__all__ = ["real_or_stub", "importable", "make_placeholder",
           "fake_main", "stub_main_module"]


def importable(name: str) -> bool:
    """True when ``name`` can be imported — installed, not merely imported.

    ``find_spec`` raises (rather than returning None) for a name whose PARENT
    package is missing, so the exception is part of the answer.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


class _Dummy:
    """Answers whatever an import-time route module asks of it.

    Callable, subscriptable, iterable and attribute-open, so ``Blueprint("x",
    __name__)``, ``@bp.route(...)``, ``request.headers.get(...)`` and
    ``jsonify(...)`` all survive module import without a real library.
    """

    def __init__(self, *_a, **_k):
        pass

    def __call__(self, *_a, **_k):
        return _Dummy()

    def __getattr__(self, _name):
        return _Dummy()

    def __getitem__(self, _key):
        return _Dummy()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __repr__(self):
        return "<dchub test placeholder>"


def make_placeholder(name: str) -> types.ModuleType:
    """A module object that yields a :class:`_Dummy` for any attribute.

    PEP 562 module ``__getattr__`` is what makes ``from <name> import Anything``
    work, which a bare ``types.ModuleType(name)`` cannot do.
    """
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda _attr: _Dummy()  # type: ignore[attr-defined]
    mod.__dchub_placeholder__ = True          # type: ignore[attr-defined]
    mod.__path__ = []                         # allow `import name.sub`
    return mod


@contextlib.contextmanager
def real_or_stub(*names: str):
    """Make ``names`` importable for the duration of the block, then clean up.

    For each name: import the real module when one exists; otherwise install a
    placeholder. Placeholders installed here are removed on exit; anything that
    was already in ``sys.modules`` is left exactly as found.

    Yields the tuple of names that got a placeholder, so a caller can assert on
    it (the tests do).
    """
    installed: list[str] = []
    try:
        for name in names:
            if name in sys.modules:
                continue          # already imported — real or not, not ours
            if importable(name):
                # Import it FOR REAL. This is the whole fix: the library, not a
                # fake, is what every later module in this process now sees.
                importlib.import_module(name)
                continue
            sys.modules[name] = make_placeholder(name)
            installed.append(name)
        yield tuple(installed)
    finally:
        for name in installed:
            # Only ever remove what THIS block put there. A nested or
            # concurrent user's entry is not ours to drop.
            mod = sys.modules.get(name)
            if getattr(mod, "__dchub_placeholder__", False):
                del sys.modules[name]


# ═════════════════════════════════════════════════════════════════════
# `main`: the app entrypoint, faked for the pure-function suite
# ═════════════════════════════════════════════════════════════════════
def fake_main() -> types.SimpleNamespace:
    """The lightweight stand-in for main.py.

    The pure-function suite deliberately never imports the real entrypoint —
    tests/conftest.py says so, and tests/_market_canon_consts.py asserts
    ``"main" not in sys.modules``. Route modules reach for it LAZILY, inside
    functions (``from main import get_read_db`` in a try/except, 14 such call
    sites across facility_profile_page, dcpi, mcp_connect and
    facility_slug_freeze), so the stand-in has to be present while a test RUNS,
    not merely while the test module imports.
    """
    return types.SimpleNamespace(get_read_db=lambda: None, get_db=lambda: None)


@pytest.fixture(autouse=True)
def stub_main_module(monkeypatch):
    """Autouse: keep a fake ``main`` in sys.modules for one test, then remove it.

    ★ 2026-09-12. Four test modules used to park the fake at MODULE scope:

        if "main" not in sys.modules:
            sys.modules["main"] = types.SimpleNamespace(
                get_read_db=lambda: None, get_db=lambda: None)

    which never came back — collection has no teardown hook — so a fake app
    entrypoint outlived the file that wanted it and was visible to every module
    collected afterwards. Import it into a test module by name and pytest
    applies it automatically; monkeypatch removes the key on teardown (it was
    absent before, so `setitem` deletes rather than restores).

    Module scope was never required for these four: nothing in their import
    chains reads ``main`` at import time. Measured by deleting the stub
    outright — every file still PASSED, but by importing the real 45K-line
    app: test_crossover_onramp 0.6s -> 52.6s, test_facilities_hub_seo -> 65.6s,
    test_facilities_hub_stored_slug -> 39.5s. Passing while silently exercising
    the real entrypoint is the failure mode
    ``feedback_suite_stubs_a_module_into_sys_modules`` records in its
    reverse-direction section: the lazy import fails open, so nothing is
    visible. The stand-in earns its place; the module-scope assignment did not.
    """
    monkeypatch.setitem(sys.modules, "main", fake_main())
    yield
