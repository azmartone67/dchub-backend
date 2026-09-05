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



def _pushes_to_another_service(cmd: str) -> bool:
    """True only for a push whose remote is an EXPLICIT non-GitHub URL.

    ★ The premise this file defends is about THIS repo's main — the
      enforce_admins argument in scripts/install-git-hooks.sh. A workflow that
      publishes to a different service entirely does not touch that premise,
      and the scan could not tell the two apart: it read `git push origin main`
      inside a job that had cloned a Hugging Face dataset and reported it as a
      push to our own main.

    ★ THE EXEMPTION IS DELIBERATELY NARROW, because the tempting version of it
      is unsafe. A bare remote NAME is never exempt — `origin` means whatever
      the last clone in that job set it to, so exempting it would let a real
      push to our main hide behind a rename. Only a spelled-out URL on a host
      that is not GitHub qualifies. That also forces the remote to be legible
      at the push site, which is the property a reviewer needs anyway.
    """
    return "https://" in cmd and "github.com" not in cmd


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
                if _pushes_to_another_service(s):
                    continue
                offenders.append(f"{fn}: {s[:90]}")
    assert not offenders, (
        "a workflow pushes to main again — the enforce_admins decision in "
        "scripts/install-git-hooks.sh rests on this being false:\n  "
        + "\n  ".join(offenders))


def test_the_external_push_exemption_cannot_launder_a_push_to_our_own_main():
    """★ A GUARD ON THE EXEMPTION. It must not become a way through.

    Every shape below still has to be an offender. Without this, widening
    _pushes_to_another_service to something permissive — `"origin" in cmd`, or
    dropping the github.com test — would fail nothing, because the only other
    assertion here is that the CURRENT workflows are clean.
    """
    must_be_offenders = [
        "git push origin main",
        "git push origin HEAD:main",
        "git push https://github.com/azmartone67/dchub-backend main",
        "git push https://x-access-token:${GITHUB_TOKEN}@github.com/azmartone67/dchub-backend HEAD:main",
        "git push --force origin main",
    ]
    for cmd in must_be_offenders:
        assert not _pushes_to_another_service(cmd), (
            f"{cmd!r} would be exempted from the CI push guard — a push to our "
            f"own main must never qualify as 'another service'"
        )


def test_the_exemption_actually_exempts_a_real_external_publish():
    """The control. An exemption that exempts nothing is a broken guard, and
    every other assertion here would still pass under it."""
    assert _pushes_to_another_service(
        'git push "https://user:${HF_TOKEN}@huggingface.co/datasets/dchubcloud/dcpi-market-verdicts" main')
