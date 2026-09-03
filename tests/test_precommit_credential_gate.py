"""The commit-boundary credential gate.

CI's syntax-check protects `main`, but this repo is PUBLIC and pushing a branch
publishes the object before any check runs. scripts/hooks/pre-commit is the half
that closes that window, so the properties below are the ones worth fencing:
they are each a way the gate could go quietly green.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(ROOT, "scripts", "check_no_leaked_credentials.py")
HOOK = os.path.join(ROOT, "scripts", "hooks", "pre-commit")
INSTALLER = os.path.join(ROOT, "scripts", "install-git-hooks.sh")

# Split ON PURPOSE, and interpolated rather than written out: a literal
# scheme://user:<real-looking>@host on one line would make THIS file a finding
# in the full scan. Same trick scripts/check_no_leaked_credentials.py:self_test
# uses on its own fixtures. `{...}` reads as a placeholder to PLACEHOLDER_PW,
# so the f-string line is silent too.
_FILLER = "Xk7dQ2vRm9pLzT4w"
REAL = f'DSN = "postgresql://u:{_FILLER}@db.example.com/x"\n'
FAKE = 'DSN = "postgresql://u:pw@db.example.com/x"\n'


def _repo(tmp_path):
    """A throwaway git repo carrying a copy of the scanner."""
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    os.makedirs(tmp_path / "scripts", exist_ok=True)
    shutil.copy(SCANNER, tmp_path / "scripts")
    return tmp_path


def _staged(tmp_path, *paths):
    subprocess.run(["git", "add", *paths], cwd=tmp_path, check=True)


def _scan(tmp_path):
    return subprocess.run(
        ["python3", "scripts/check_no_leaked_credentials.py", "--staged"],
        cwd=tmp_path, capture_output=True, text=True)


def test_staged_scan_blocks_a_real_password(tmp_path):
    r = _repo(tmp_path)
    (r / "cfg.py").write_text(REAL)
    _staged(r, "cfg.py")
    assert _scan(r).returncode == 1


def test_staged_scan_allows_a_placeholder(tmp_path):
    r = _repo(tmp_path)
    (r / "cfg.py").write_text(FAKE)
    _staged(r, "cfg.py")
    out = _scan(r)
    assert out.returncode == 0, out.stderr


def test_staged_scan_reads_the_index_not_the_worktree(tmp_path):
    """`git add` the secret, then clean the working copy — the COMMIT still
    carries it, so the gate must still refuse. Reading the worktree here is the
    silent-green failure: every check passes and the credential lands anyway."""
    r = _repo(tmp_path)
    (r / "cfg.py").write_text(REAL)
    _staged(r, "cfg.py")
    (r / "cfg.py").write_text(FAKE)          # worktree now looks innocent
    assert _scan(r).returncode == 1


def test_staged_mode_does_not_fire_the_stale_ledger_check(tmp_path):
    """main() fails on a KNOWN_EXPOSURES entry that matches nothing — an
    invariant that only holds over the FULL tracked set. Applied to one commit's
    files every entry looks stale, so --staged would block every commit. A gate
    that cries wolf gets uninstalled."""
    r = _repo(tmp_path)
    (r / "cfg.py").write_text(FAKE)
    _staged(r, "cfg.py")
    out = _scan(r)
    assert out.returncode == 0, out.stderr
    assert "stale KNOWN_EXPOSURES" not in out.stderr


def test_hook_breaks_a_self_chain_instead_of_hanging(tmp_path):
    """Installed over one of the scripts it calls, the hook recursed forever and
    every commit hung with no output (2026-09-03). The guard turns that into a
    no-op, so assert it from the OUTSIDE: re-entry must return, fast."""
    r = _repo(tmp_path)
    (r / "cfg.py").write_text(REAL)          # would block if it ran at all
    _staged(r, "cfg.py")
    env = dict(os.environ, DCHUB_PRECOMMIT_RUNNING="1")
    out = subprocess.run(["sh", HOOK], cwd=r, env=env,
                         capture_output=True, timeout=30)
    assert out.returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_installer_replaces_a_symlinked_hook_not_its_target(tmp_path):
    """`cp src dst` writes THROUGH a symlink. .git/hooks/pre-commit.chained-1 is
    an `ln -sf` into scripts/, so the naive copy overwrote the tracked source
    with the hook — which then called itself. Install must replace the LINK."""
    r = _repo(tmp_path)
    shutil.copytree(os.path.join(ROOT, "scripts", "hooks"), r / "scripts/hooks")
    shutil.copy(os.path.join(ROOT, "scripts",
                             "precommit-no-conflict-markers.sh"), r / "scripts")
    shutil.copy(INSTALLER, r / "scripts")
    target = r / "scripts/precommit-no-conflict-markers.sh"
    before = target.read_text()

    hooks = r / ".git/hooks"
    os.makedirs(hooks, exist_ok=True)
    # the real layout: a dispatcher that chains a SYMLINK into scripts/
    (hooks / "pre-commit").write_text(
        '#!/bin/sh\nexec "$(dirname "$0")/pre-commit.chained-1" "$@"\n')
    os.chmod(hooks / "pre-commit", 0o755)
    os.symlink("../../scripts/precommit-no-conflict-markers.sh",
               hooks / "pre-commit.chained-1")

    subprocess.run(["bash", "scripts/install-git-hooks.sh"], cwd=r,
                   capture_output=True, timeout=120)

    assert target.read_text() == before, \
        "installer wrote through the symlink and clobbered the tracked source"
    assert not os.path.islink(hooks / "pre-commit.chained-1")
