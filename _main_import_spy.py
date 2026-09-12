"""Diagnostic: name every test file that triggers an import of the REAL main.

Installs a meta_path finder ahead of everything else. When `main` is imported
and is not already in sys.modules, record the current test file and substitute
the lightweight fake — so the run stays fast and behaves as it did before the
leak was removed, while producing the complete list of free-riders.
"""
import os, sys, types, importlib.abc, importlib.machinery

OFFENDERS = {}
_cur = {"f": "<collection>"}


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "main":
            return None
        OFFENDERS.setdefault(_cur["f"], 0)
        OFFENDERS[_cur["f"]] += 1
        return importlib.machinery.ModuleSpec(fullname, _Loader())


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        m = types.ModuleType("main")
        m.get_read_db = lambda: None
        m.get_db = lambda: None
        return m

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _Finder())


def pytest_collectstart(collector):
    if hasattr(collector, "fspath"):
        _cur["f"] = os.path.basename(str(collector.fspath))


def pytest_runtest_setup(item):
    _cur["f"] = os.path.basename(str(item.fspath))


def pytest_sessionfinish(session, exitstatus):
    with open("/private/tmp/scratch/main_offenders.txt", "w") as fh:
        for k, v in sorted(OFFENDERS.items(), key=lambda kv: -kv[1]):
            fh.write(f"{v:6d}  {k}\n")
