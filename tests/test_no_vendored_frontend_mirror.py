"""One copy of the frontend, in the repo that owns it.

★ WHAT WAS RETIRED (2026-09-05). This repo carried dchub-frontend/ — a vendored
2,426-file, 33 MB snapshot of the standalone frontend. Nothing served from it:
Flask serves static/, and all three workflows that need the real frontend check
it out as a SIBLING (path: dchub-frontend, alongside dchub-backend/), never
from in here.

★ IT WAS NOT INERT. deploy-frontend.yml pushed that subdirectory to Cloudflare
Pages. On 2026-05-28 it redeployed the stale worker over the canonical one on
every backend push — /facilities 404'd and /transactions + /markets timed out
through Cloudflare while the backend itself was healthy. The push trigger was
disabled that day and the workflow left in place behind a manual dispatch and a
force_stale_deploy flag. It has not run since. It is now deleted, because with
the mirror gone it could only ever deploy an empty directory.

★ AND IT MISLED READERS, which is the quieter cost. Four separate files carry
bespoke "skip dchub-frontend/" entries so their scans would not read it, each
one a thing someone had to know. Anything that did NOT know — a fresh script, a
person, an agent — measured the site against a snapshot last touched
2026-09-02, whose dchub-nav.js hashes differently from the file on dchub.cloud
and which has no wiki.html at all.

The exclusion entries are deliberately LEFT IN PLACE. They cost nothing, and
they are the second line if a mirror is ever restored by hand.
"""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIRROR = "dchub-frontend"


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, timeout=60)


@pytest.fixture(scope="module")
def tracked_files():
    """Everything git tracks in THIS repo. Tracked-only is the point: CI checks
    the real frontend out as a sibling, and a developer may have one locally —
    neither is tracked here, so neither can trip this guard."""
    r = _git("ls-files", "-z")
    if r.returncode != 0:
        pytest.skip(f"not a git checkout, or git unavailable: {r.stderr.strip()[:120]}")
    return [p for p in r.stdout.split("\0") if p]


def test_git_actually_answered(tracked_files):
    """★ NON-VACUITY. Every assertion below is 'no tracked file matches X'. An
    empty file list satisfies all of them. Prove git returned the real tree
    before trusting a negative."""
    assert len(tracked_files) > 500, (
        f"git ls-files returned only {len(tracked_files)} paths — this guard is "
        f"asserting absence against a list it never really got")
    assert "main.py" in tracked_files


def test_the_frontend_mirror_is_not_committed_back(tracked_files):
    """THE RULE. The frontend lives in its own repo."""
    inside = [p for p in tracked_files if p.split("/")[0] == MIRROR]
    assert not inside, (
        f"{len(inside)} file(s) of a frontend mirror are committed under "
        f"{MIRROR}/ again (e.g. {inside[:3]}). Nothing here serves them and CI "
        f"reads the sibling checkout, so a copy in this repo can only go stale "
        f"and mislead — it already caused a production outage once. Keep the "
        f"frontend in the dchub-frontend repo.")


def test_the_path_is_ignored(tracked_files):
    """A sibling clone or a stray checkout must not be able to re-add it by
    accident — `git add -A` has to refuse."""
    with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as fh:
        lines = {ln.strip() for ln in fh}
    assert {"/dchub-frontend/", "dchub-frontend/"} & lines, (
        ".gitignore no longer ignores the mirror path — `git add -A` after a "
        "local frontend clone would silently vendor 2,426 files back in")
    r = _git("check-ignore", "-q", "dchub-frontend/index.html")
    assert r.returncode == 0, "git does not actually ignore dchub-frontend/"


def test_the_workflow_that_deployed_the_mirror_is_gone(tracked_files):
    """★ The loaded gun. deploy-frontend.yml pushed the mirror to CF Pages and
    took production down doing it. With the mirror deleted it could only deploy
    an empty directory over the live site."""
    assert ".github/workflows/deploy-frontend.yml" not in tracked_files, (
        "deploy-frontend.yml is back — it deploys the (now absent) "
        "dchub-frontend/ subdirectory to Cloudflare Pages, i.e. it would "
        "publish an empty directory over the canonical frontend")


def test_no_workflow_deploys_a_frontend_subdirectory(tracked_files):
    """Renaming the file must not bring the behaviour back."""
    offenders = []
    for p in tracked_files:
        if not p.startswith(".github/workflows/") or not p.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(REPO, p), encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        if "pages deploy" not in body.lower():
            continue
        if MIRROR in body:
            offenders.append(p)
    assert not offenders, (
        f"{offenders} run a Cloudflare Pages deploy that references "
        f"{MIRROR}/ — the frontend is deployed from its own repo")
