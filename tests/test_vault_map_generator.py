"""tests/test_vault_map_generator.py — the map cannot quietly go stale (2026-08-12).

An audit on 2026-08-11 re-proposed THREE already-shipped capabilities because
758 route modules were navigable only by grep. scripts/generate_vault_map.py is
the map. It is GENERATED rather than written, because a hand-maintained map is
accurate for one day and then lies — the same "failure rendered as a benign
value" this whole series of fixes has been removing.

Ways the map starts lying:
  (1) PHANTOM LOOP — the first cut scraped every `"name":` in system_loops.py
      and reported EIGHT loops, inventing `iso_metrics` out of a docstring that
      quotes heartbeat's registry as an example.
  (2) WRONG KEY — keying loops by function name gives `auto_press`, while the
      board reports `auto_press_daily`. A map keyed differently from the thing
      it maps sends you hunting for something that does not exist.
  (3) EATS HAND-WRITTEN NOTES — the generator overwrites a note a human owns.
  (4) SILENT DRIFT — --check stops detecting a stale vault, so CI can no longer
      prove the map matches the tree.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_vault_map_generator.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "generate_vault_map.py"


def _mod():
    spec = importlib.util.spec_from_file_location("vault_map", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _probed_loop_names() -> list:
    return _mod().collect_loops()["probes"]


def test_every_owned_note_is_produced():
    notes = _mod().build()
    assert set(notes) == set(_mod()._OWNED)
    for name, text in notes.items():
        assert text.strip(), "%s rendered empty" % name
        assert "generated: true" in text, \
            "%s lacks the generated marker that stops hand edits" % name


def test_no_phantom_loop_from_a_docstring():
    """★REGRESSION (1). `iso_metrics` is a heartbeat SURFACE quoted inside a
    docstring in system_loops.py — it is not a probed loop, and a file-wide
    `"name":` scrape reported it as one."""
    assert "iso_metrics" not in _probed_loop_names(), (
        "the map invented a loop out of a docstring example — read probe "
        "FUNCTION BODIES, not every quoted name in the file")


def test_loop_names_match_the_probe_functions_one_for_one():
    """Every probed loop, exactly once, no more and no fewer."""
    src = (_ROOT / "routes" / "system_loops.py").read_text(encoding="utf-8")
    n_funcs = len(re.findall(r"^def _probe_[a-z_]+\s*\(", src, re.M))
    names = _probed_loop_names()
    assert len(names) == n_funcs, \
        "%d probe functions but %d mapped loops" % (n_funcs, len(names))
    assert len(set(names)) == len(names), "duplicate loop names in the map"


def test_the_reported_name_is_used_not_the_function_name():
    """★REGRESSION (2). _probe_auto_press returns name 'auto_press_daily'; the
    map must carry what the board shows."""
    names = _probed_loop_names()
    assert "auto_press_daily" in names
    assert "auto_press" not in names


def test_source_nodes_and_edges_are_read_from_the_canonical_graph():
    g = _mod().collect_loops()
    assert g["sources"], "source nodes missing from the map"
    assert g["edges"], "declared edges missing from the map"
    typed = {s["loop"] for s in g["sources"]}
    assert typed <= set(g["probes"]), \
        "a typed source names a loop that is not probed: %s" % (
            typed - set(g["probes"]))


def test_hand_written_notes_are_never_owned():
    """★REGRESSION (3). These carry findings a generator cannot reconstruct."""
    owned = set(_mod()._OWNED)
    for hand in ("Context Integrity.md", "Admin Cache Leak.md", "Traps.md",
                 "Home.md", "DCHUB.md"):
        assert hand not in owned, \
            "the generator would overwrite the hand-written note %s" % hand


def test_check_mode_detects_a_stale_vault(tmp_path):
    """★REGRESSION (4). --check is how CI proves the map still matches."""
    m = _mod()
    notes = m.build()
    for name, text in notes.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    import sys
    argv = sys.argv[:]
    try:
        sys.argv = ["gen", "--check", "--check-target", str(tmp_path)]
        assert m.main() == 0, "--check called a fresh vault stale"
        (tmp_path / "Loop Graph.md").write_text("drifted", encoding="utf-8")
        assert m.main() == 1, "--check passed a vault that no longer matches"
    finally:
        sys.argv = argv


@pytest.mark.parametrize("note", ["Architecture Map.md", "Master Shells.md"])
def test_key_notes_carry_the_fix_history_pointer_or_links(note):
    """The map's whole job is to stop the next audit grepping its way to a
    wrong conclusion, so the entry points must point somewhere better."""
    text = _mod().build()[note]
    assert "[[" in text, "%s has no wikilinks — it is a dead end" % note


def test_the_in_repo_copy_is_current():
    """★THE ACTUAL CI GATE, and the reason an in-repo copy exists at all.

    The vault is a local Obsidian directory outside the repo, so a runner has no
    copy of it — `--check --vault ~/Documents/DCHUB` can never execute in CI.
    Shipping --check while saying "CI can prove the map matches" would have been
    a guard that reads as wired and enforces nothing, which is the failure this
    codebase keeps rediscovering. docs/architecture/ is committed precisely so
    THIS test can fail when someone adds a shell and does not regenerate."""
    import pathlib as _p
    m = _mod()
    target = _p.Path(m._REPO_DOCS)
    assert target.is_dir(), (
        "docs/architecture/ is missing — run "
        "`python3 scripts/generate_vault_map.py` and commit it")
    stale = []
    for name, text in m.build().items():
        cur = target / name
        if not cur.exists() or cur.read_text(encoding="utf-8").strip() != text.strip():
            stale.append(name)
    assert not stale, (
        "the committed architecture map no longer matches the tree: %s\n"
        "Re-run `python3 scripts/generate_vault_map.py` and commit "
        "docs/architecture/." % ", ".join(stale))
