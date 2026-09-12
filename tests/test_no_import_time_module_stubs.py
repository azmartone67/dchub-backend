"""A test module must not leave a fake library in sys.modules.

The incident
------------
``tests/test_brain_smoke_regression.py`` opened with::

    for _name in ("flask", "psycopg2", "requests"):
        if _name not in sys.modules:
            sys.modules[_name] = types.ModuleType(_name)

``_name not in sys.modules`` asks "has anything imported it YET?", not "is it
installed?" — so with flask installed and simply not yet imported, the empty
placeholder won, and there is no teardown hook during collection to put the
library back. Every module imported afterwards in that process got the fake.

Measured 2026-09-12 on origin/main @ 7f59c51c0, 57 files that import real
flask/psycopg2/requests:

    pytest <57 files>                            -> 940 collected,   0 errors
    pytest test_brain_smoke_regression.py <57>   -> 142 collected,  49 errors

A whole-directory CI run never saw it: an alphabetically earlier module imports
real flask first, the predicate goes False, and no placeholder is installed.
Only a SUBSET run trips it — "just the suites my change touches" — which is
exactly the run a developer or an agent trusts without a second opinion. The
files do not fail loudly, they fail to COLLECT, so the report is a short green.

Two guards, because one is not enough
-------------------------------------
STATIC (:func:`test_no_test_module_stubs_a_module_at_import_time`) reads the
SHAPE of every test file's AST, so it fires in a full run and a subset run
alike, whether or not the stub happened to be installed this time. This is the
detector.

RUNTIME (:func:`test_no_test_module_left_a_fake_library_in_sys_modules`) reads
the swap ledger that ``tests/_stub_sentinel.py`` fills during collection and
names the file that did it. In a full-directory run it is expected to stay
quiet forever — see the sentinel's docstring — so it is a backstop, not a
detector, and it is stated as such rather than counted as coverage.

Both are mutation-proved by the controls at the bottom of this file, which feed
the analyzer a known-bad module and assert it goes red. Without those, a scan
that stopped matching anything would report the same green as a clean repo.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import _stub_sentinel  # noqa: E402

TESTS_DIR = pathlib.Path(ROOT) / "tests"

# ── Floor ────────────────────────────────────────────────────────────
# A repo scan that matches nothing reports the same green as a repo scan that
# matched everything and found it clean. tests/_scan_floors.py pins the
# principal scan in tests/scan_floors.json; this second, explicit floor is here
# because that mechanism can be switched off with DCHUB_SCAN_FLOORS=0, and a
# guard whose only floor is optional has no floor.
# ~1191 test modules today; set well under, as a collapse detector.
_MIN_FILES_SCANNED = 900

# ── The exemption table ──────────────────────────────────────────────
# Keyed by (filename, stubbed name) — NOT by filename. A file-keyed register
# exempts every name added to that file later, which is half a ratchet: the
# next stub lands inside an existing exemption and nothing fires.
#
# `main` is the Flask app entrypoint. The pure-function suite deliberately
# never imports it (tests/conftest.py's docstring; tests/_market_canon_consts.py
# asserts "main" not in sys.modules), so a lightweight stand-in installed before
# `routes.*` is the house rule rather than a defect: there is no real module
# being shadowed. It is recorded here, name by name, so it stays visible.
_ALLOWED: dict[tuple[str, str], str] = {
    ("test_crossover_onramp.py", "main"): "house rule: renderers lazily do "
        "`from main import get_read_db`; importing real main drags in the app "
        "+ DB pools",
    ("test_facilities_hub_seo.py", "main"): "house rule: as above",
    ("test_facilities_hub_stored_slug.py", "main"): "house rule: as above",
    ("test_facility_site_code_titles.py", "main"): "house rule: as above",
}

# Names that may NEVER be exempted: real libraries, pinned in requirements.txt
# and pip-installed by the unit-tests job, so a fake standing in for one is
# always wrong. Guarded by test_the_exemption_table_cannot_cover_a_real_library.
_NEVER_EXEMPTABLE = frozenset(_stub_sentinel.WATCHED)


# ═════════════════════════════════════════════════════════════════════
# The analyzer
# ═════════════════════════════════════════════════════════════════════
def _is_sys_modules_subscript(node: ast.AST) -> bool:
    return (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "modules"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "sys")


def _keys_of(node: ast.Subscript, loop_vars: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Every module name this subscript can stand for.

    ``sys.modules[_name] = ...`` inside ``for _name in ("flask", "psycopg2",
    "requests")`` is the exact shape of the incident, and reporting it as the
    opaque key ``<_name>`` would be a real loss: the exemption table is keyed
    by (file, NAME), so an unresolved key could not be checked against
    _NEVER_EXEMPTABLE and the failure message could not say which library was
    replaced. Loop variables bound to a literal sequence of strings are
    therefore resolved to the names themselves, one finding each.
    """
    sl = node.slice
    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
        return (sl.value,)
    if isinstance(sl, ast.Name) and sl.id in loop_vars:
        return loop_vars[sl.id]
    if isinstance(sl, ast.Name):
        return (f"<{sl.id}>",)
    return ("<computed>",)


def _literal_str_seq(node: ast.AST) -> tuple[str, ...] | None:
    """The strings in a literal tuple/list/set, or None if it is not one."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    out = []
    for el in node.elts:
        if not (isinstance(el, ast.Constant) and isinstance(el.value, str)):
            return None
        out.append(el.value)
    return tuple(out)


def _real_module_bindings(tree: ast.Module) -> set[str]:
    """Names bound to a genuine module object rather than a fabricated one.

    ``importlib.util.module_from_spec(spec)`` REQUIRES its result to be placed
    in ``sys.modules`` before ``exec_module`` runs — that is the documented way
    to load a file by path, and the object is a real module compiled from real
    source, not a stand-in for a library that exists elsewhere. Three files use
    it (``_cwg``, ``railway_rollback``, ``_dmwatch``) and none of them shadow
    an importable name. Recognising the idiom keeps them out of the exemption
    table, where they would only dilute it.
    """
    real: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        fn = node.value.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name not in ("module_from_spec", "import_module", "reload"):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                real.add(t.id)
    return real


def _value_is_synthetic(value: ast.AST, real_bindings: set[str]) -> bool:
    """True when the right-hand side fabricates a stand-in.

    ``types.ModuleType(...)``, ``types.SimpleNamespace(...)``, a class
    instantiation, a lambda — anything that is not a module the interpreter
    actually loaded. A bare Name is synthetic unless it was bound from one of
    the real-module constructors above; that is the conservative direction,
    because an unrecognised binding should fail INTO the guard.
    """
    if isinstance(value, ast.Name):
        return value.id not in real_bindings
    if isinstance(value, ast.Attribute):
        # sys.modules["x"] = some.module  — an alias for something already
        # loaded; not a fabrication.
        return False
    if isinstance(value, ast.Call):
        fn = value.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        return name not in ("module_from_spec", "import_module", "reload")
    return True


def scan_source(source: str, filename: str) -> list[dict]:
    """Module-scope writes of a fabricated object into ``sys.modules``.

    Module scope only, on purpose. That is where the defect lives and where it
    is undecidable to fix: collection has no monkeypatch and no fixture
    finalizer, so an import-time assignment has no teardown available to it at
    all. Writes inside a function can be — and in this suite overwhelmingly
    are — wrapped in try/finally or handed to ``monkeypatch.setitem``.

    Reads the AST, so a comment that quotes the old idiom (this file and
    test_brain_smoke_regression.py both do) cannot re-trip the guard.
    """
    tree = ast.parse(source, filename=filename)
    real_bindings = _real_module_bindings(tree)
    found: list[dict] = []

    def walk(node: ast.AST, in_function: bool,
             loop_vars: dict[str, tuple[str, ...]]) -> None:
        for child in ast.iter_child_nodes(node):
            nested = in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            scoped = loop_vars
            if (not in_function and isinstance(child, (ast.For, ast.AsyncFor))
                    and isinstance(child.target, ast.Name)):
                values = _literal_str_seq(child.iter)
                if values is not None:
                    scoped = {**loop_vars, child.target.id: values}
            if not in_function and isinstance(child, ast.Assign):
                for target in child.targets:
                    if (_is_sys_modules_subscript(target)
                            and _value_is_synthetic(child.value, real_bindings)):
                        for module in _keys_of(target, scoped):
                            found.append({
                                "file": filename,
                                "line": child.lineno,
                                "module": module,
                                "source": ast.unparse(child),
                            })
            walk(child, nested, scoped)

    walk(tree, False, {})
    return found


def _scan_tests_dir() -> tuple[list[dict], int]:
    findings: list[dict] = []
    files = sorted(TESTS_DIR.glob("*.py"))
    for path in files:
        try:
            findings.extend(scan_source(
                path.read_text(encoding="utf-8"), path.name))
        except SyntaxError:
            continue  # tests/test_tests_are_collectable.py owns that failure
    return findings, len(files)


# ═════════════════════════════════════════════════════════════════════
# The guards
# ═════════════════════════════════════════════════════════════════════
def test_no_test_module_stubs_a_module_at_import_time():
    findings, n_files = _scan_tests_dir()

    assert n_files >= _MIN_FILES_SCANNED, (
        f"COVERAGE COLLAPSE: this guard scanned {n_files} test modules, floor "
        f"is {_MIN_FILES_SCANNED}. It found nothing because it looked nowhere, "
        f"and a scan that can find nothing reports the same green as a clean "
        f"repo. Repoint TESTS_DIR at tests/, or lower the floor in the PR that "
        f"genuinely shrinks the suite — never to turn a red build green."
    )

    offenders = [f for f in findings
                 if (f["file"], f["module"]) not in _ALLOWED]
    assert not offenders, (
        "A test module writes a fabricated object into sys.modules at IMPORT "
        "time. Collection has no monkeypatch and no fixture finalizer, so "
        "nothing puts the real module back — every file imported afterwards in "
        "the same process gets the fake, and in a subset run those files do not "
        "fail, they fail to COLLECT.\n\n"
        + "\n".join(f"  · {f['file']}:{f['line']}  sys.modules[{f['module']!r}]"
                    f"\n      {f['source']}" for f in offenders)
        + "\n\nFix: wrap the import in tests._import_shims.real_or_stub(...), "
          "which imports the real module when one exists and removes anything "
          "it installed; or move the fake into a fixture and install it with "
          "monkeypatch.setitem(sys.modules, ...), which pytest restores.\n"
          "See tests/test_brain_smoke_regression.py for the worked fix."
    )


def test_no_test_module_left_a_fake_library_in_sys_modules():
    """Backstop: nothing swapped a real library out during THIS session.

    Ordered last by tests/conftest.py::pytest_collection_modifyitems so it sees
    every file. Expected to stay quiet in a full run — the static guard above
    is the one that detects the shape. Its own can-it-fail control is
    test_the_sentinel_names_the_module_and_the_file_that_swapped_it.
    """
    _stub_sentinel.checkpoint("<session end>")
    assert not _stub_sentinel.violations, _stub_sentinel.describe()


def test_the_watched_libraries_are_the_real_thing_right_now():
    """Direct, order-independent read of the three named libraries.

    ``feedback_suite_stubs_a_module_into_sys_modules``: a stub has no
    ``__file__``. This holds in a single-file run too, where the ledger above
    has nothing to say.
    """
    for name in _stub_sentinel.WATCHED:
        mod = sys.modules.get(name)
        if mod is None:
            continue  # never imported in this run — nothing to shadow
        assert getattr(mod, "__file__", None), (
            f"sys.modules[{name!r}] is {mod!r}, which has no __file__ — it is a "
            f"stub, not the installed library. Some test module replaced it and "
            f"did not put it back; everything imported after that point in this "
            f"process saw the fake.\n"
            f"Ledger: {_stub_sentinel.describe() or '(no attribution recorded)'}"
        )
        assert not getattr(mod, "__dchub_placeholder__", False), (
            f"sys.modules[{name!r}] is a tests._import_shims placeholder that "
            f"escaped its context manager."
        )


# ═════════════════════════════════════════════════════════════════════
# Controls — these are what make the guards above non-vacuous
# ═════════════════════════════════════════════════════════════════════
_LEAKY = '''
import sys, types
for _name in ("flask", "psycopg2", "requests"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)
'''

_LEAKY_SIMPLE_NS = '''
import sys, types
sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)
'''

_CLEAN_IMPORTLIB = '''
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("_cwg", "routes/x.py")
cwg = importlib.util.module_from_spec(_spec)
sys.modules["_cwg"] = cwg
_spec.loader.exec_module(cwg)
'''

_CLEAN_IN_FIXTURE = '''
import sys, types, pytest

@pytest.fixture(autouse=True)
def _shim(monkeypatch):
    monkeypatch.setitem(sys.modules, "flask", types.ModuleType("flask"))
    yield

def _helper():
    saved = sys.modules.get("flask")
    sys.modules["flask"] = types.ModuleType("flask")
    try:
        yield
    finally:
        sys.modules["flask"] = saved
'''


@pytest.mark.parametrize("source,expect", [
    (_LEAKY, "flask"),
    (_LEAKY_SIMPLE_NS, "boto3"),
], ids=["the_original_loop_over_a_literal_tuple", "a_plain_simplenamespace"])
def test_the_static_scan_flags_a_module_scope_stub(source, expect):
    """MUTATION CONTROL. Reintroduce the exact leak; the scan must see it."""
    found = scan_source(source, "test_mutant.py")
    assert found, (
        "The static scan did not flag a module-scope sys.modules assignment. "
        "It has stopped being able to fail, so its green result on the real "
        "suite carries no information."
    )
    assert any(f["module"] == expect for f in found), \
        f"flagged {[f['module'] for f in found]}, expected {expect!r}"


@pytest.mark.parametrize("source", [_CLEAN_IMPORTLIB, _CLEAN_IN_FIXTURE],
                         ids=["importlib_module_from_spec", "monkeypatch_and_try_finally"])
def test_the_static_scan_does_not_flag_the_safe_idioms(source):
    """The other half of the control: it must not fire on everything.

    A guard that flags every file is as useless as one that flags none, and it
    gets deleted faster.
    """
    assert scan_source(source, "test_clean.py") == []


def test_the_exemption_table_cannot_cover_a_real_library():
    for (fname, module), _reason in _ALLOWED.items():
        assert module not in _NEVER_EXEMPTABLE, (
            f"{fname} is exempted for sys.modules[{module!r}], but {module!r} "
            f"is a real library installed by CI. Faking an installed library at "
            f"import time is the defect this guard exists to stop; it must be "
            f"fixed, not exempted."
        )


def test_every_exemption_still_matches_a_real_occurrence():
    """A stale exemption is a hole nobody is looking at."""
    findings, _ = _scan_tests_dir()
    live = {(f["file"], f["module"]) for f in findings}
    stale = sorted(set(_ALLOWED) - live)
    assert not stale, (
        f"{len(stale)} exemption(s) in _ALLOWED no longer match anything: "
        f"{stale}. The code was fixed; drop the entry so the table keeps "
        f"meaning what it says."
    )


def test_the_sentinel_names_the_module_and_the_file_that_swapped_it():
    """MUTATION CONTROL for the runtime half.

    Pure-function, so it proves the mechanism without needing a leak to occur
    in this session — which in a full-directory run it never does.
    """
    real = types.ModuleType("flask")
    real.__file__ = "/somewhere/flask/__init__.py"
    stub = types.ModuleType("flask")            # no __file__ — the tell

    baseline = {"flask": _stub_sentinel.fingerprint(real),
                "requests": _stub_sentinel.fingerprint(real)}
    now = {"flask": _stub_sentinel.fingerprint(stub),
           "requests": _stub_sentinel.fingerprint(real)}

    found = _stub_sentinel.compare(baseline, now, "test_the_offender.py")
    assert len(found) == 1, f"expected exactly one violation, got {found}"
    assert found[0]["module"] == "flask"
    assert found[0]["blamed_file"] == "test_the_offender.py"
    assert found[0]["looks_like_a_stub"] is True

    message = _stub_sentinel.describe(found)
    assert "flask" in message and "test_the_offender.py" in message, message
    # ...and it must NOT cry wolf over the module that did not change.
    assert "requests" not in message, message


def test_the_sentinel_does_not_flag_an_ordinary_first_import():
    real = types.ModuleType("psycopg2")
    real.__file__ = "/somewhere/psycopg2/__init__.py"
    baseline = {"psycopg2": _stub_sentinel.fingerprint(None)}
    now = {"psycopg2": _stub_sentinel.fingerprint(real)}
    assert _stub_sentinel.compare(baseline, now, "whoever.py") == []


def test_the_shim_helper_puts_everything_back():
    from tests import _import_shims

    absent = "__dchub_definitely_not_installed__"
    assert absent not in sys.modules

    with _import_shims.real_or_stub("flask", absent) as installed:
        # The real library wins; only the genuinely-missing name is faked.
        assert installed == (absent,), installed
        assert getattr(sys.modules["flask"], "__file__", None), \
            "real flask was shadowed by a stub"
        assert absent in sys.modules

    assert absent not in sys.modules, "the placeholder escaped the block"
    assert getattr(sys.modules["flask"], "__file__", None)


def test_the_placeholder_can_serve_a_from_import():
    """The old empty ModuleType could not, which is why the file never ran.

    `from flask import Blueprint` against types.ModuleType("flask") raises
    ImportError, so the shim failed in exactly the dependency-free environment
    it claimed to support.
    """
    from tests import _import_shims

    bare = types.ModuleType("__dchub_bare__")
    with pytest.raises(ImportError):
        exec("from __dchub_bare__ import Blueprint",
             {"__dchub_bare__": bare, **{"__builtins__": __builtins__}})

    absent = "__dchub_placeholder_probe__"
    with _import_shims.real_or_stub(absent):
        mod = sys.modules[absent]
        assert mod.Blueprint is not None
        assert callable(mod.jsonify)
