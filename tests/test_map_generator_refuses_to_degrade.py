"""tests/test_map_generator_refuses_to_degrade.py — a map that could not be
computed must not be written (2026-08-31).

WHAT HAPPENED. refresh-architecture-map.yml regenerates the committed map on
every merge to main. Its FIRST live run opened PR #3483 proposing:

    -| declared loop edges | 4 |      -| typed source nodes | 3 |
    +| declared loop edges | 0 |      +| typed source nodes | 0 |

plus deletion of the whole producer/consumer table. Nothing about the tree had
changed. generate_vault_map.py imports routes/graph_master_shell.py to read
LOOP_EDGES / LOOP_SOURCE_PRODUCERS, that job had no flask installed, and the
import failure was swallowed:

    except Exception as e:
        print("  warn: could not import the graph constants: %s" % e,
              file=sys.stderr)          # -> edges = [], sources = []

The generator then wrote a SMALLER map and exited 0. Measured against
origin/main in a bare venv: exit 0, and the checkout really was modified
(4 insertions, 13 deletions across both notes).

★ The generator's own docstring says it exists "to stop failures from being
rendered as benign values, and a stale map is exactly that". A map that quietly
drops a section it could not compute is that same failure wearing this script's
clothes — and it is worse than staleness, because the shrunken map passes its
own --check in the degraded environment that produced it.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_map_generator_refuses_to_degrade.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "generate_vault_map.py"


def _mod():
    spec = importlib.util.spec_from_file_location("vault_map_degrade", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_refusal_exists_and_is_its_own_error():
    m = _mod()
    assert issubclass(m.GraphConstantsUnavailable, Exception)


def test_a_healthy_checkout_reports_no_import_problem():
    m = _mod()
    g = m.collect_loops()
    assert "graph_error" in g, "collect_loops must surface whether the import worked"
    assert g["graph_error"] is None, (
        "the graph constants did not import in this environment (%s) — install "
        "flask/psycopg2 before running the suite" % g["graph_error"])


def test_a_failed_import_is_recorded_by_the_REAL_collector(monkeypatch):
    """★Drives the actual except branch — no stubbed collect_loops.

    Blocking the module in sys.modules makes `from routes.graph_master_shell
    import ...` raise exactly as it did on the runner with no flask. An earlier
    cut of this file stubbed collect_loops instead, so a mutation that stopped
    recording the error entirely SURVIVED: the test was exercising its own
    fake."""
    import sys as _sys

    m = _mod()
    monkeypatch.setitem(_sys.modules, "routes.graph_master_shell", None)
    g = m.collect_loops()
    assert g["graph_error"], (
        "collect_loops swallowed a real import failure — this is the exact "
        "silence that let a degraded map be written and proposed as PR #3483")
    assert g["edges"] == [] and g["sources"] == []
    # and the refusal must follow from it, through the real path
    with pytest.raises(m.GraphConstantsUnavailable):
        m.build()


def test_build_refuses_when_the_constants_did_not_import(monkeypatch):
    """★THE REGRESSION. Simulate the CI runner that had no flask: build() must
    raise instead of returning a map with the Loop Graph silently emptied."""
    m = _mod()
    real = m.collect_loops

    def degraded():
        g = real()
        g["graph_error"] = "ModuleNotFoundError: No module named 'flask'"
        g["edges"], g["sources"] = [], []
        return g

    monkeypatch.setattr(m, "collect_loops", degraded)
    with pytest.raises(m.GraphConstantsUnavailable) as e:
        m.build()
    assert "flask" in str(e.value), "the refusal must name what failed to import"
    assert "pip install" in str(e.value), "and must name the fix"


def test_the_refusal_names_the_section_that_would_have_vanished(monkeypatch):
    m = _mod()
    real = m.collect_loops

    def degraded():
        g = real()
        g["graph_error"] = "ModuleNotFoundError: No module named 'flask'"
        return g

    monkeypatch.setattr(m, "collect_loops", degraded)
    with pytest.raises(m.GraphConstantsUnavailable) as e:
        m.build()
    assert "Loop Graph" in str(e.value)


def test_an_absent_graph_module_is_honest_emptiness_not_a_refusal(monkeypatch):
    """★Precision, driven through the REAL guard. If routes/graph_master_shell.py
    genuinely does not exist, an empty loop graph is TRUE and must still render —
    refusing there would make the generator unusable for a repo that dropped the
    module. The refusal is only for 'the module is there and I could not read it'.

    Both conditions are made real: the source reads as absent AND the import
    would fail. Only the `if graph_src:` guard separates them, so stubbing
    collect_loops here would test nothing (a mutation removing that guard
    survived exactly that way)."""
    import sys as _sys

    m = _mod()
    real_read = m._read
    monkeypatch.setattr(
        m, "_read",
        lambda path: "" if path.endswith("graph_master_shell.py") else real_read(path))
    monkeypatch.setitem(_sys.modules, "routes.graph_master_shell", None)

    g = m.collect_loops()
    assert g["has_graph_src"] is False
    assert g["graph_error"] is None, (
        "an absent module is not a failed read — the generator must not refuse")
    notes = m.build()          # must NOT raise
    assert "Architecture Map.md" in notes


def test_the_healer_installs_what_the_generator_imports_through():
    """★The job that regenerates must run in the same environment as the gate
    that verifies. #3483 happened because it did not."""
    wf = _ROOT / ".github" / "workflows" / "refresh-architecture-map.yml"
    if not wf.is_file():
        pytest.skip("auto-heal workflow not present")
    body = wf.read_text(encoding="utf-8")
    install = [ln for ln in body.splitlines()
               if "pip install" in ln and not ln.lstrip().startswith("#")]
    assert install, "the healer must install the generator's import deps"
    assert any("flask" in ln for ln in install), (
        "flask is what routes/graph_master_shell.py needs; without it the "
        "generator refuses (or, before this fix, degraded silently)")
