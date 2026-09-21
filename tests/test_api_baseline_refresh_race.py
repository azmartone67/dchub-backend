"""A refresh baseline generated against an outdated main must not pass as current.

2026-09-21: #5026 regenerated contracts/api_response_surface.json on 4ef4fc37e,
where GET /api/v1/markets/<market> still served `stats.mw_coverage`. #5022
removed that key at 02:44Z without touching the baseline — the baseline it was
checked against had never recorded the key, so its `contract` check passed.
#5026's checks had passed on its OLD merge ref, branch protection is
strict:false, and it squash-merged at 03:15Z: main's baseline now asserted a
key main did not serve, and the required `contract` check went red on main and
on every open PR.

Both PRs were green by the only question `check` asks ("does this tree still
serve every key the baseline names?"). The question a baseline-committing PR has
to answer is "is this baseline the surface of the tree it lands in?", asked
against main as it is NOW. That is `verify-against`.

This rebuilds the race in a throwaway git repo with the REAL extractor:

    c0  app serves {a}        baseline = {a}
    c1  app serves {a, k}     baseline not regenerated (the lag a refresh closes)
    R   c1 + `baseline`       the refresh PR; it records k
    X   c1 without k          another PR removes k; baseline untouched

and pins: today's `check` passes on BOTH PR trees (that is how both landed);
the landed tree fails `check` (the incident); `verify-against` says STALE for R
on X, CURRENT for R on c1, and CURRENT again once R is regenerated on X.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "api_response_contract.py")

_APP = '''from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/api/v1/probe")
def probe():
    return jsonify({BODY})
'''

CURRENT, STALE, UNMEASURED = 0, 1, 2


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                       cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _contract(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(repo, "scripts", "api_response_contract.py"),
         *args], cwd=repo, capture_output=True, text=True)


def _serve(repo: str, keys: list[str]) -> None:
    body = "{" + ", ".join(f'"{k}": {i}' for i, k in enumerate(keys)) + "}"
    with open(os.path.join(repo, "app.py"), "w", encoding="utf-8") as fh:
        fh.write(_APP.replace("{BODY}", body))


def _regenerate(repo: str, message: str) -> str:
    _git(repo, "add", "-A")          # the extractor scans `git ls-files`
    r = _contract(repo, "baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo: str, message: str) -> str:
    _git(repo, "commit", "-qam", message)
    return _git(repo, "rev-parse", "HEAD")


def _baseline_keys(repo: str, rev: str) -> list[str]:
    doc = json.loads(_git(repo, "show", f"{rev}:contracts/api_response_surface.json"))
    return doc["endpoints"]["GET /api/v1/probe"]["keys"]


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path / "repo")
    os.makedirs(os.path.join(r, "scripts"))
    os.makedirs(os.path.join(r, "contracts"))
    shutil.copy(_SCRIPT, os.path.join(r, "scripts", "api_response_contract.py"))
    with open(os.path.join(r, "contracts", "route_serving_map.json"), "w") as fh:
        json.dump({"serving": {"GET /api/v1/probe": {"modules": ["app"]}}}, fh)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "race")
    _git(r, "config", "user.email", "race@example.invalid")
    _serve(r, ["a"])
    _regenerate(r, "c0: serves a, baseline {a}")
    return r


def _verify(repo: str, main: str, candidate: str) -> subprocess.CompletedProcess:
    return _contract(repo, "verify-against", "--main", main, "--candidate", candidate)


def test_the_5026_race_is_caught_where_check_passes(repo):
    _serve(repo, ["a", "k"])
    c1 = _commit(repo, "c1: serve k (baseline lags)")

    _git(repo, "switch", "-qc", "refresh")
    R = _regenerate(repo, "R: refresh the baseline on c1")
    assert _baseline_keys(repo, R) == ["a", "k"]
    # TODAY, on R's merge ref (main == c1): green.
    assert _contract(repo, "check").returncode == 0

    _git(repo, "switch", "-q", "main")
    _serve(repo, ["a"])
    X = _commit(repo, "X: remove k")
    # TODAY, on X: green too — the baseline it is checked against has no k.
    assert _contract(repo, "check").returncode == 0

    # The landing: R squashed onto X. This is main after 03:15Z.
    _git(repo, "merge", "-q", "--squash", "refresh")
    _commit(repo, "land R on X")
    landed = _contract(repo, "check")
    assert landed.returncode == 1 and "k" in landed.stdout, landed.stdout

    # THE GUARD, asked before that landing: R on X is stale, and says why.
    v = _verify(repo, X, R)
    assert v.returncode == STALE, v.stdout + v.stderr
    assert "ASSERTED BUT NOT SERVED" in v.stdout
    assert "GET /api/v1/probe  k" in v.stdout

    # Control: on the main it was generated on, the same R is current.
    v = _verify(repo, c1, R)
    assert v.returncode == CURRENT, v.stdout + v.stderr

    # Restore, both ways. Regenerating on X changes nothing — X's baseline is
    # already X's surface, so the lane opens no refresh at all — and X itself
    # verifies current. Or serve k again, and the original R is current.
    _git(repo, "switch", "-q", "--detach", X)
    assert _contract(repo, "baseline").returncode == 0
    assert _git(repo, "status", "--porcelain") == ""
    assert _verify(repo, X, X).returncode == CURRENT
    _git(repo, "switch", "-qc", "restore", X)
    _serve(repo, ["a", "k"])
    X2 = _commit(repo, "X2: serve k again")
    assert _verify(repo, X2, R).returncode == CURRENT


def test_a_refresh_older_than_an_additive_change_is_stale(repo):
    """#5020's shape: main ADDED a key while the refresh was in CI. Harmless
    to `check`, but the baseline is not main's surface, so it is not current."""
    _serve(repo, ["a", "k"])
    c1 = _commit(repo, "c1: serve k (baseline lags)")
    _git(repo, "switch", "-qc", "refresh")
    R = _regenerate(repo, "R: refresh on c1")
    _git(repo, "switch", "-q", "main")
    _serve(repo, ["a", "k", "m"])
    Y = _commit(repo, "Y: add m")
    v = _verify(repo, Y, R)
    assert v.returncode == STALE, v.stdout
    assert "SERVED BUT NOT RECORDED" in v.stdout
    assert "GET /api/v1/probe  m" in v.stdout
    assert _verify(repo, c1, R).returncode == CURRENT


def test_line_churn_alone_is_not_staleness(repo):
    """Every edit above a handler moves `source`; #5047 was 2 key lines and 360
    source lines. A refresh must not go stale because a file grew a comment."""
    _serve(repo, ["a", "k"])
    _commit(repo, "c1: serve k (baseline lags)")
    _git(repo, "switch", "-qc", "refresh")
    R = _regenerate(repo, "R: refresh on c1")
    _git(repo, "switch", "-q", "main")
    with open(os.path.join(repo, "app.py"), encoding="utf-8") as fh:
        src = fh.read()
    with open(os.path.join(repo, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("# a comment\n# and another\n" + src)
    Y = _commit(repo, "Y: shift every line")
    assert _verify(repo, Y, R).returncode == CURRENT


def test_a_candidate_that_does_not_merge_is_unmeasured_not_current(repo):
    _serve(repo, ["a", "k"])
    _commit(repo, "c1: serve k")
    _git(repo, "switch", "-qc", "refresh")
    R = _regenerate(repo, "R: records k")
    _git(repo, "switch", "-q", "main")
    _serve(repo, ["a", "m"])
    _regenerate(repo, "Y: serve m instead of k, regenerate")
    v = _verify(repo, "main", R)
    assert v.returncode == UNMEASURED, v.stdout + v.stderr
    assert "does not merge cleanly" in v.stdout


def test_unresolvable_refs_are_unmeasured(repo):
    v = _verify(repo, "no-such-ref", "HEAD")
    assert v.returncode == UNMEASURED and "cannot resolve" in v.stdout
