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


def test_unregistered_brain_drafts_are_not_counted_as_route_modules():
    """★ 2026-08-24. `routes/_proposed_*.py` are draft proposals the brain
    attaches to a strategic-draft PR. Not one is registered as a blueprint, yet
    all 27 counted toward `route modules`.

    Wrong on this map's own terms — it exists because "758 route modules were
    navigable only by grep" let an audit re-propose three shipped capabilities,
    so padding that number with the very drafts that caused the confusion makes
    it less navigable. And it made every brain-l6 draft PR born red: the PR adds
    one _proposed_ file, the count moves, the committed map goes stale, and
    test_the_in_repo_copy_is_current fails on a file the bot never knew to
    regenerate."""
    m = _mod()
    live = m.live_route_modules()
    assert live, "route module list came back empty"
    assert not [f for f in live if f.startswith("_proposed_")], \
        "an unregistered brain draft is being counted as a route module"
    # and the count actually rendered in the map is that list, not len(listdir)
    import os as _os
    on_disk = [f for f in _os.listdir(m._ROUTES) if f.endswith(".py")]
    drafts = [f for f in on_disk if f.startswith("_proposed_")]
    if drafts:                       # guard is only meaningful while any exist
        assert len(live) == len(on_disk) - len(drafts)
        assert f"| route modules | {len(live)} |" in \
            m.build()["Architecture Map.md"], \
            "the map still renders the padded count"


def test_a_promoted_draft_starts_counting(tmp_path, monkeypatch):
    """★ THE PAIRED CONTROL. Excluding a PREFIX must not exclude a real module.
    Promotion drops the `_proposed_` prefix, and the moment it does the module
    must appear — otherwise this fix hides live routes instead of drafts."""
    m = _mod()
    fake = tmp_path / "routes"
    fake.mkdir()
    for n in ("_proposed_thing.py", "thing.py", "notes.md"):
        (fake / n).write_text("", encoding="utf-8")
    monkeypatch.setattr(m, "_ROUTES", str(fake))
    live = m.live_route_modules()
    assert "thing.py" in live, "a real route module was excluded"
    assert "_proposed_thing.py" not in live
    assert "notes.md" not in live, "a non-.py file was counted"


# ── why this gate is advisory on PRs, and only on PRs (2026-09-04) ──────
#
# It stays the gate on main. On a PULL REQUEST it reports drift without failing,
# because requiring every PR to COMMIT the regenerated map is what turns one
# generated file into a repo-wide merge-conflict generator.
#
# The race is already documented, in .github/workflows/refresh-architecture-map.yml:
#
#     "IT IS A RACE, NOT CARELESSNESS. main's protection has strict:false, so a
#      PR need not be up to date to merge. PR A regenerates the map against its
#      own tree, PR B lands a new route module first, and A merges a map that
#      was true when written and is stale by the time it arrives."
#
# That workflow heals MAIN after every merge and demonstrably works. What it
# cannot heal is PR-vs-PR: two open PRs that each add a route module both commit
# a different version of the SAME generated file and conflict with each other,
# every time, by construction. Measured 2026-09-04: main takes ~47 commits/24h
# and the unit suite runs 25 minutes, so every PR is open long enough for this
# to be a certainty rather than bad luck. Three of four PRs open that evening
# hit it; two needed a hand-resolved rebase whose entire content was "re-run the
# generator".
#
# So the file no longer has to travel in the PR. Drift is reported on PRs and
# healed on main by the workflow above. The assertion is UNCHANGED for push,
# schedule and local runs — a developer still sees it, and main is still gated.
#
# ★ The skip is deliberately narrow: exactly GITHUB_EVENT_NAME == "pull_request".
# test_the_pr_exemption_cannot_widen below pins that, because a guard that
# learns to skip everywhere is the failure this file's own docstring is about.
# The healer's own branch is the ONE pull request that must still be gated.
# refresh-architecture-map.yml mints it at :110 as "chore/arch-map-<sha>" and
# matches on that prefix at :104; tests/test_architecture_map_autoheal.py:73
# already pins the string, so this reuses a constant rather than inventing one.
_HEALER_BRANCH_PREFIX = "chore/arch-map-"


def _is_pull_request() -> bool:
    """True on a PR that may defer regeneration to the healer.

    ★ The healer's OWN PR is excluded, and that carve-out is the whole point.
    Its entire purpose is to make the committed map match the tree, so it is the
    one PR that must not be allowed to merge a stale one. main's protection has
    strict:false, so a healing PR is never required to be up to date: during its
    ~25-minute check cycle more merges can land and move the count again, and
    because the checkout is the MERGE ref its tree already contains those new
    modules while its committed map does not. Exempting it would disarm the only
    check that ever validated the healer's output — the healer would merge a map
    that was already stale, and the next push would just open another one.
    """
    import os
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return False
    head = os.environ.get("GITHUB_HEAD_REF") or ""
    return not head.startswith(_HEALER_BRANCH_PREFIX)


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
    if stale and _is_pull_request():
        # Reported, not enforced — refresh-architecture-map.yml regenerates this
        # on main after the merge. Failing here would force the PR to carry the
        # file, which is the conflict generator described above.
        import pytest as _pt
        _pt.skip(
            "architecture map drifted (%s) — NOT failing the PR: "
            "refresh-architecture-map.yml regenerates it on main after merge. "
            "Requiring every PR to commit this generated file is what makes two "
            "PRs conflict on it. Run `python3 scripts/generate_vault_map.py` "
            "locally if you want the diff in your branch." % ", ".join(stale))
    assert not stale, (
        "the committed architecture map no longer matches the tree: %s\n"
        "Re-run `python3 scripts/generate_vault_map.py` and commit "
        "docs/architecture/." % ", ".join(stale))


def test_the_pr_exemption_cannot_widen(monkeypatch):
    """The exemption must be EXACTLY pull_request. A guard that learns to skip
    on push, schedule or a bare local run stops gating main — which is the only
    thing this gate was ever for."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    for ev, expected in (("pull_request", True), ("push", False),
                         ("schedule", False), ("workflow_dispatch", False),
                         ("", False)):
        monkeypatch.setenv("GITHUB_EVENT_NAME", ev)
        assert _is_pull_request() is expected, f"event {ev!r} misclassified"
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    assert _is_pull_request() is False, "unset env (local run) must still enforce"


def test_the_healers_own_pr_is_never_exempt(monkeypatch):
    """The healer exists to make the committed map match the tree, so it is the
    one PR that must not merge a stale one.

    main's protection has strict:false, so a healing PR is never required to be
    up to date. During its ~25-minute check cycle more merges land, and its
    checkout is the MERGE ref — tree already updated, committed map not. Before
    the PR exemption, this check failing there is exactly what blocked auto-merge
    and forced a fresh regeneration. Exempting it would let the healer merge a
    map that is already stale again."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    for branch, exempt in (
        ("chore/arch-map-abc1234", False),   # the healer itself — MUST enforce
        ("chore/arch-map-", False),
        ("feat/some-feature", True),         # an ordinary PR — may defer
        ("fix/whatever", True),
        ("", True),
    ):
        monkeypatch.setenv("GITHUB_HEAD_REF", branch)
        assert _is_pull_request() is exempt, (
            f"branch {branch!r}: expected exempt={exempt}")


def test_the_healer_branch_prefix_matches_the_workflow_that_mints_it():
    """If the workflow renames its branch, the carve-out above silently stops
    matching and the healer becomes exempt again — the failure this whole file
    is about."""
    wf = (_ROOT / ".github" / "workflows" / "refresh-architecture-map.yml"
          ).read_text(encoding="utf-8")
    assert _HEALER_BRANCH_PREFIX in wf, (
        f"{_HEALER_BRANCH_PREFIX!r} no longer appears in "
        "refresh-architecture-map.yml — the healer branch was renamed and the "
        "carve-out in _is_pull_request() now matches nothing.")


def test_the_healer_that_justifies_the_exemption_exists():
    """The PR exemption is only safe because main is healed after merge. If that
    workflow is ever deleted, drift would ship silently — so pin it."""
    import pathlib as _p
    wf = _ROOT / ".github" / "workflows" / "refresh-architecture-map.yml"
    assert wf.is_file(), (
        "refresh-architecture-map.yml is gone — the PR exemption in "
        "test_the_in_repo_copy_is_current assumes it heals main after merge. "
        "Restore it, or make that assertion fail on PRs again.")
    text = wf.read_text(encoding="utf-8")
    assert "branches: [main]" in text, "the healer no longer runs on main pushes"
    assert "generate_vault_map" in text, "the healer no longer runs the generator"
