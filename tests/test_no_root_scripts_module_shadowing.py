"""No top-level module name resolves to two different files (2026-09-01).

House rule: tests NEVER import main. This one reads the filesystem only.

THE CLASS. Five .py basenames existed BOTH at the repo root and under
scripts/, with different contents in every case:

    ai_wars_response_generator.py     68 root / 92 scripts
    eia_gas_bulk_loader.py           235 root / 115 scripts
    eia_generator_reseed.py          436 root /   4 scripts
    firstlight_fiber_seed.py         123 root / 328 scripts
    push_firstlight.py                 5 root /  25 scripts

Which file a bare `import <name>` gets is decided by sys.path ORDER, not by
anything in the source. tests/test_evidence_status_convention.py does

    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "scripts"))

so scripts/ lands AHEAD of the repo root, and sys.path is process-global for
the rest of the pytest session. Every module imported by bare name after that
point — in any later test file, in any collection order — resolves to the
scripts/ copy unless that file happens to re-insert ROOT in front first.

WHY IT WAS NOT THEORETICAL. scripts/eia_generator_reseed.py was a four-line
placeholder whose module scope did

    raise NotImplementedError("eia_generator_reseed.py: original corrupted")

standing in for a 14-byte "404: Not Found" download that had been saved over
the real file (kept alongside as scripts/eia_generator_reseed.py.corrupted).
The root copy is the real 436-line loader. main.py imports it BY BARE NAME in
two places — _run_standalone_loader() and phase12g_loader_async(), both
`__import__(mod_name)` — behind three admin endpoints:

    POST /api/admin/phase12c-rerun-loaders
    POST /api/admin/run-all-loaders
    POST /api/admin/load-power-plants-live

Both call sites catch broadly (Exception / BaseException), so the shadowed
import would not have crashed anything. It would have returned
{"ok": false, "error": "NotImplementedError: ... original corrupted"} from a
loader that reseeds the eia_generators table, which is exactly the shape of
failure this repo has been bitten by before: reported, structured, and unread.

★ THE EXISTING REGISTRY TEST CANNOT SEE THIS. tests/test_loader_entrypoint_
registry.py checks every _STANDALONE_LOADERS entry, but it AST-parses
os.path.join(REPO_ROOT, module_name + '.py') — it reads the ROOT file by
construction. Production does a bare __import__. So the registry test stays
green while the thing production actually imports is a different file. A test
that resolves a module by an explicit path can never guard an importer that
resolves it by name; that gap is what this file covers.

WHAT SAVED IT. Nothing structural — only that each of the two live importers
happened to have ROOT ahead of scripts/ on its own path (ai_wars_automation.py
runs with the repo root as sys.path[0]; tests/test_eia_reseed_zero_write.py
does its own sys.path.insert(0, ROOT) at module scope). That is a property of
each caller, re-established by hand every time, and silently lost the first
time someone writes `import eia_generator_reseed` in a file that does not.

THE RULE. A basename may live at the repo root or in scripts/, not both.
Collisions are rejected even when the two files are byte-identical: identical
today is the state every one of the five started in, and drift is invisible
until the wrong copy is the one that runs. There is deliberately no allowlist
— the fix for a genuine need is to RENAME the scripts/ copy to something that
cannot collide with a top-level module name, which costs one line and removes
the ambiguity instead of recording it.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _py_basenames(directory):
    """Top-level .py files in `directory` (no recursion, no packages).

    Only files directly in the directory can be reached by a bare-name import
    of that directory, so a subdirectory is a package question, not this one.
    """
    if not os.path.isdir(directory):
        return set()
    return {
        name
        for name in os.listdir(directory)
        if name.endswith(".py") and os.path.isfile(os.path.join(directory, name))
    }


def test_the_directories_this_guard_reads_are_really_there():
    """A guard that scans an empty set passes for the wrong reason.

    If scripts/ is renamed or the root layout changes, the collision test
    below would go quietly green over nothing. Floor it.
    """
    assert os.path.isdir(SCRIPTS), "scripts/ is missing — this guard scans nothing"
    root_py = _py_basenames(ROOT)
    scripts_py = _py_basenames(SCRIPTS)
    assert len(root_py) > 100, (
        "expected the repo root to hold many top-level modules, found %d — "
        "this guard is not looking where it thinks it is" % len(root_py))
    assert len(scripts_py) > 20, (
        "expected scripts/ to hold many scripts, found %d — this guard is not "
        "looking where it thinks it is" % len(scripts_py))


def test_no_module_basename_exists_at_both_root_and_scripts():
    """The whole rule, in one assertion. See this module's docstring."""
    collisions = sorted(_py_basenames(ROOT) & _py_basenames(SCRIPTS))
    assert not collisions, (
        "these basenames exist BOTH at the repo root and in scripts/, so which "
        "file `import <name>` returns depends on sys.path order rather than on "
        "anything in the source:\n  "
        + "\n  ".join(collisions)
        + "\n\nKeep ONE copy. If the scripts/ copy must stay, rename it to a "
          "name no top-level module uses. Do not add an allowlist here — an "
          "exemption records the ambiguity instead of removing it.")


def test_the_reseed_placeholder_stays_gone():
    """Named separately because it was the live one.

    A module-scope `raise` shadowing a real module is a trap regardless of
    whether any importer currently reaches it, and this particular file sat
    in front of a loader wired to three admin endpoints. The general rule
    above would catch it as a collision; this says why it matters.
    """
    assert not os.path.exists(os.path.join(SCRIPTS, "eia_generator_reseed.py")), (
        "scripts/eia_generator_reseed.py is back. Its module scope raises "
        "NotImplementedError, and it shadows the real 436-line "
        "eia_generator_reseed.py at the repo root — which main.py imports by "
        "bare name behind /api/admin/load-power-plants-live and two other "
        "loader endpoints.")


def test_the_corrupted_download_stays_gone():
    """The 14-byte "404: Not Found" body the placeholder stood in for.

    Not importable, so it shadows nothing on its own — but it is the evidence
    that a failed download was once committed over a live loader, and keeping
    it invites the placeholder back to explain it.
    """
    assert not os.path.exists(
        os.path.join(SCRIPTS, "eia_generator_reseed.py.corrupted")), (
        "scripts/eia_generator_reseed.py.corrupted is back — a saved HTTP 404 "
        "body committed as source. Delete it; the real module is at the repo "
        "root and its history is in git.")
