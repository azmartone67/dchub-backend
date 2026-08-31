"""tests/test_architecture_map_autoheal.py — the map heals itself (2026-08-31).

docs/architecture/Architecture Map.md is GENERATED, and
tests/test_vault_map_generator.py::test_the_in_repo_copy_is_current reds main
when the committed copy stops matching the tree. Adding one route module does
it: #3473 was a ONE LINE refresh, "route modules 787 -> 788", opened by hand
because main was red.

It is a RACE, not carelessness — main's protection has strict:false, so PR A can
regenerate against its own tree while PR B lands a new route module first, and
A merges a map that was true when written. Two red mains in four days (#3275,
#3473) plus conflicts on #3268 and #3294.

These pin the properties that would let the healer silently stop healing.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_architecture_map_autoheal.py -v
"""
from __future__ import annotations

import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WF = _ROOT / ".github" / "workflows" / "refresh-architecture-map.yml"


def _text():
    return _WF.read_text(encoding="utf-8")


def _yaml():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_text())


def test_the_healer_exists():
    assert _WF.is_file(), "the auto-heal workflow is gone — the map goes back to " \
                          "reddening main until a human notices"


def test_it_runs_on_every_push_to_main():
    """★Drift only appears once a merge lands. A healer that runs on a schedule
    or on PRs would leave main red in the window that actually matters."""
    d = _yaml()
    on = d.get(True, d.get("on"))       # PyYAML reads bare `on:` as True
    push = on.get("push") or {}
    assert push.get("branches") == ["main"], \
        "the healer must trigger on push to main, got %r" % (push,)


def test_it_never_pushes_main_directly():
    """★Repo rule: CI never pushes main. The refresh must land as a PR through
    the same required checks as any other change."""
    body = _text()
    for forbidden in ("git push origin main", "git push -u origin main",
                      "git push --force"):
        assert forbidden not in body, \
            "the healer must open a PR, never push main (%r)" % forbidden
    assert "gh pr create" in body and "gh pr merge --auto" in body


def test_the_duplicate_pr_guard_cannot_swallow_its_own_failure():
    """★`gh pr list` has --head (EXACT) and NO prefix filter, so an earlier cut
    of this used a flag that does not exist, wrapped in `|| echo "0"`. That guard
    would have reported 'none open' forever and opened a duplicate PR per merge
    — wired, and enforcing nothing."""
    body = _text()
    assert "--head-prefix" not in body, \
        "--head-prefix is not a real gh flag; the prefix match belongs in jq"
    assert 'startswith("chore/arch-map-")' in body, \
        "the open-PR guard must match the healing branch prefix in jq"
    # ★ Join backslash continuations FIRST. An earlier cut of this test scanned
    # only the physical line holding "gh pr list", so a `|| echo "0"` appended to
    # the continuation line sailed past it — the assertion could not fail, which
    # a mutation caught and a green run never would have.
    logical, buf = [], ""
    for ln in body.splitlines():
        buf += ln.rstrip("\\") if ln.rstrip().endswith("\\") else ln
        if not ln.rstrip().endswith("\\"):
            logical.append(buf)
            buf = ""
    if buf:
        logical.append(buf)
    guard = [ln for ln in logical if "gh pr list --state open" in ln]
    assert guard, "the duplicate-PR guard is gone"
    assert not any("|| echo" in ln for ln in guard), \
        "the guard must fail loudly, not default to 'none open'"


def test_it_has_a_kill_switch():
    body = _text()
    assert "ARCH_MAP_AUTOHEAL_DISABLE" in body


def test_the_generator_it_calls_is_the_real_one():
    """A healer pointed at a script that no longer exists is a no-op that looks
    wired."""
    body = _text()
    assert "scripts/generate_vault_map.py" in body
    assert (_ROOT / "scripts" / "generate_vault_map.py").is_file()


def test_it_only_commits_the_generated_directory():
    """★The healer runs with contents:write on every merge. It must stage the
    generated docs and nothing else — a bare `git add -A` would sweep up
    whatever else a step happened to leave in the tree."""
    body = _text()
    assert "git add docs/architecture/" in body
    for greedy in ("git add -A", "git add .", "git commit -a"):
        assert greedy not in body, "the healer must stage only the generated map (%r)" % greedy
