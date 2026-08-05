"""The pre-push main guard must not depend on someone remembering to install it.

House rule: tests NEVER import main. This one reads files. Nothing at module
scope.

WHY THIS EXISTS
===============
`main` is push-to-deploy and `enforce_admins` is false, so 137 of 223 commits
(61%) in the week to 2026-07-28 reached main as ungated admin pushes.
`scripts/hooks/pre-push-main-guard.sh` is the compensating control.

★ But it lives in `.git/hooks`, which is NOT version-controlled. A fresh clone
has NO guard until `scripts/install-git-hooks.sh` is run by hand. Verified
2026-08-05: the Claude Code remote container — a session that can and does push
— came up with the guard absent. The sessions most likely to push were the ones
without it, which is exactly the wrong way round.

A guard with an install step is a guard someone will be missing. So the setup
path installs it.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_session_start_installs_the_guard():
    """★ The whole point. A remote session that can push must not start
    unguarded."""
    src = _src(".claude", "hooks", "session-start.sh")
    assert "scripts/install-git-hooks.sh" in src


def test_a_failed_install_is_reported_not_swallowed():
    """★ A guard that quietly did not install is the same silent-success
    failure this repo keeps paying for. It must not be fatal to session start
    either — a broken installer should not stop work."""
    src = _src(".claude", "hooks", "session-start.sh")
    block = src[src.index("install-git-hooks.sh"):]
    assert "WARNING" in block, "a failed install must say so"
    assert "unguarded" in block, "and must say what that means"
    assert "||" in block or "if bash" in block, "must not abort the session"


def test_the_installer_no_longer_argues_against_enforce_admins():
    """★ The header used to say enforce_admins:true 'would also block
    auto-rollback.yml, which must write to main precisely when CI is red'.
    That stopped being true on 2026-07-28 — rollback is a Railway operation now
    and nothing in CI writes to main. A stale justification in the document
    someone reads while deciding is worse than no document."""
    src = _src("scripts", "install-git-hooks.sh")
    head = src[:4000]
    assert "would also block auto-rollback" not in head
    assert "railway_rollback.py" in head, "name what replaced it"
    assert "NOTHING in CI" in head or "nothing in CI" in head


def test_the_installer_admits_its_own_structural_weakness():
    """The reason to prefer enforce_admins over this hook, stated where the
    decision gets made rather than in a chat log."""
    head = _src("scripts", "install-git-hooks.sh")[:4000]
    assert "not version-controlled" in head.lower()
    assert "fresh clone" in head.lower()


def test_nothing_in_ci_writes_to_main():
    """The premise the two tests above rest on. If a workflow ever starts
    pushing to main again, this fails and the enforce_admins argument has to be
    re-read rather than silently inherited.

    ★ Comment lines are stripped first: four workflows still DESCRIBE the old
    `git push origin main` behaviour in prose, and matching raw text reports
    them as offenders — which is how this premise looked false on first check.
    """
    wf_dir = os.path.join(ROOT, ".github", "workflows")
    offenders = []
    for fn in sorted(os.listdir(wf_dir)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(wf_dir, fn), encoding="utf-8") as fh:
            code = "\n".join(l for l in fh.read().splitlines()
                             if not l.lstrip().startswith("#"))
        for line in code.splitlines():
            s = line.strip()
            if s.startswith("git push") and (
                    " main" in s or "HEAD:main" in s or ":main" in s):
                offenders.append(f"{fn}: {s[:90]}")
    assert not offenders, (
        "a workflow pushes to main again — the enforce_admins decision in "
        "scripts/install-git-hooks.sh rests on this being false:\n  "
        + "\n  ".join(offenders))
