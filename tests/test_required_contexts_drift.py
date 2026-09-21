"""tests/test_required_contexts_drift.py — main-branch-health compares main's
required checks with REQUIRED_CONTEXTS at RUNTIME (2026-09-21).

THE GAP. tests/test_main_red_is_watched.py derives which workflow carries each
declared context, and pins a measured floor so dropping one fails offline. But
nothing offline can see a context ADDED in GitHub's branch-protection settings,
and that is how the list went from six to seven: `contract` was required for a
while and nothing noticed. On 2026-09-21 a red `contract` on main blocked every
open PR while main-branch-health beat green, because GATING did not read
api-response-contract.yml.

THE MEASUREMENT the collector rests on (Actions run 35576294578, 2026-09-21,
GITHUB_TOKEN with main-branch-health's own permissions): GET /branches/main
answered 200 on contents=read with all seven contexts; every /protection
endpoint answered 403, wanting administration=read, which GITHUB_TOKEN cannot
be granted.

Every assertion CALLS the code. The module's prose says "unmeasured" and
"drift" in plenty of places, so a text match would hold with the logic gone.

House rules: no DB, never import main, nothing at module scope.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "tools" / "deadman" / "main_branch_verdict.py"
_LEDGER = _ROOT / "routes" / "ingest_runs.py"

REPO = "azmartone67/dchub-backend"
HEAD = "2e00f9aee62bd519b1d6bcb46e5e4f6acecaa2e2"
SEVEN = ("substance-gate", "syntax-check", "unit-tests", "regression-lint",
         "db-parity", "app-contract-gate", "contract")
SIX = SEVEN[:-1]   # the list as it stood before `contract` was noticed


def _mod():
    spec = importlib.util.spec_from_file_location("main_branch_verdict", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _measured(contexts=SEVEN):
    """`.protection.required_status_checks` in the shape GITHUB_TOKEN read it
    in Actions run 35576294578 (checks pinned to app 15368, enforcement
    `everyone`)."""
    return {"checks": [{"app_id": 15368, "context": c} for c in contexts],
            "contexts": list(contexts), "enforcement_level": "everyone"}


def _ok_status():
    """The ledger's _OK_STATUS literal, read off the source — importing routes/
    would pull in Flask for one set."""
    for node in ast.parse(_LEDGER.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_OK_STATUS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("routes/ingest_runs.py no longer defines _OK_STATUS")


# --------------------------------------------------------------------------
# required_drift(live, declared) — the pure comparison
# --------------------------------------------------------------------------

def test_a_context_added_in_github_is_drift():
    """★ The 2026-09-21 blind spot, replayed: branch protection requires seven,
    the module declares the old six."""
    status, note = _mod().required_drift(list(SEVEN), SIX)
    assert status == "drift", (status, note)
    assert "required in GitHub but missing from REQUIRED_CONTEXTS: contract" in note
    assert "no longer required" not in note, note


def test_a_declared_context_no_longer_required_is_drift():
    status, note = _mod().required_drift(list(SIX), SEVEN)
    assert status == "drift", (status, note)
    assert "in REQUIRED_CONTEXTS but no longer required: contract" in note
    assert "missing from" not in note, note


def test_drift_both_ways_names_each_side():
    live = [c for c in SEVEN if c != "db-parity"] + ["new-gate"]
    status, note = _mod().required_drift(live, SEVEN)
    assert status == "drift", (status, note)
    assert "missing from REQUIRED_CONTEXTS: new-gate" in note, note
    assert "no longer required: db-parity" in note, note


def test_an_exact_match_is_match_whatever_the_order():
    live = list(reversed(SEVEN)) + ["contract"]   # order and repeats do not matter
    status, note = _mod().required_drift(live, SEVEN)
    assert status == "match", (status, note)


def test_an_empty_list_is_unmeasured_never_match():
    """An empty answer from a repo that requires checks is a failed probe. It
    must not read as `match` even when nothing is declared either."""
    m = _mod()
    for declared in (SEVEN, ()):
        status, _ = m.required_drift([], declared)
        assert status == "unmeasured", (declared, status)


def test_a_missing_or_malformed_list_is_unmeasured():
    m = _mod()
    # None: no protection block. A bare string would iterate as characters.
    for live in (None, "contract", {"contexts": list(SEVEN)},
                 list(SEVEN) + [""], list(SEVEN) + [None]):
        status, note = m.required_drift(live, SEVEN)
        assert status == "unmeasured", (live, status, note)


def test_an_error_is_unmeasured_and_says_why():
    err = RuntimeError("gh: Resource not accessible by integration (HTTP 403)")
    status, note = _mod().required_drift(err, SEVEN)
    assert status == "unmeasured", (status, note)
    assert "HTTP 403" in note, note


# --------------------------------------------------------------------------
# combined(verdict, contexts) — what the board is told
# --------------------------------------------------------------------------

def test_a_green_verdict_does_not_hide_drift_or_an_unread_list():
    m = _mod()
    ok = _ok_status()
    green = ("success", "all 4 gating workflow(s) green on HEAD 2e00f9aee")
    for c_status in ("drift", "unmeasured"):
        status, note = m.combined(green, (c_status, "the contexts note"))
        assert status == "contexts_" + c_status, (c_status, status)
        assert status not in ok, (
            "%r is in the ledger's _OK_STATUS, so the main-ci feed would stay "
            "green on it" % status)
        assert "the contexts note" in note, note


def test_green_and_match_is_success():
    status, note = _mod().combined(("success", "green"), ("match", "all 7"))
    assert status == "success", status
    assert "all 7" in note, note


def test_a_red_unmeasured_or_pending_verdict_is_not_replaced():
    """main_red stays the actionable news; pending still beats nothing."""
    m = _mod()
    for v in ("main_red", "unmeasured", "pending"):
        for c in ("match", "drift", "unmeasured"):
            status, note = m.combined((v, "verdict note"), (c, "contexts note"))
            assert status == v, (v, c, status)
            assert "contexts note" in note, (v, c, note)


# --------------------------------------------------------------------------
# collect_required(repo) — the read, against a recording stub
# --------------------------------------------------------------------------

def _stub(m, answer):
    calls = []

    def fake_gh(args):
        calls.append(list(args))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    m._gh = fake_gh
    return calls


def test_the_read_is_get_a_branch_not_the_admin_endpoint():
    """/protection answers 403 to GITHUB_TOKEN (it wants administration=read).
    It works with the owner's token, so a "cleanup" onto it would pass every
    local check and go unmeasured in Actions."""
    m = _mod()
    calls = _stub(m, json.dumps(_measured()))
    live = m.collect_required(REPO)
    assert calls and calls[0][:2] == ["api", "repos/%s/branches/main" % REPO], calls
    assert sorted(set(live)) == sorted(SEVEN), live
    assert m.required_drift(live, SEVEN)[0] == "match"


def test_a_403_comes_back_as_an_error_not_an_empty_list():
    m = _mod()
    _stub(m, subprocess.CalledProcessError(
        1, ["gh", "api"], output='{"message":"Resource not accessible by integration"}',
        stderr="gh: Resource not accessible by integration (HTTP 403)\n"))
    live = m.collect_required(REPO)
    assert isinstance(live, BaseException), live
    status, note = m.required_drift(live, SEVEN)
    assert status == "unmeasured" and "HTTP 403" in note, (status, note)


def test_no_protection_block_is_unmeasured():
    m = _mod()
    for answer in ("null\n", "{}", "", '{"enforcement_level":"everyone"}',
                   '{"checks":[{"app_id":1}]}', "not json"):
        _stub(m, answer)
        status, _ = m.required_drift(m.collect_required(REPO), SEVEN)
        assert status == "unmeasured", (answer, status)


def test_a_context_in_only_one_of_contexts_or_checks_still_counts():
    """`contexts` is deprecated in favour of `checks`; either may carry one the
    other lacks, or stop being sent."""
    m = _mod()
    rsc = _measured(SIX)
    rsc["checks"].append({"app_id": 15368, "context": "contract"})
    _stub(m, json.dumps(rsc))
    assert "contract" in m.collect_required(REPO)
    _stub(m, json.dumps({"checks": _measured()["checks"]}))
    assert set(m.collect_required(REPO)) == set(SEVEN)


# --------------------------------------------------------------------------
# main() — the comparison has to reach the step outputs, not just exist
# --------------------------------------------------------------------------

def _run_main(m, monkeypatch, tmp_path, branch_answer):
    green = json.dumps([{"headSha": HEAD, "status": "completed",
                         "conclusion": "success"}])

    def fake_gh(args):
        if args[:2] == ["run", "list"]:
            return green
        if args[:2] == ["api", "repos/%s/commits/main" % REPO]:
            return HEAD + "\n"
        if args[:2] == ["api", "repos/%s/branches/main" % REPO]:
            if isinstance(branch_answer, BaseException):
                raise branch_answer
            return json.dumps(branch_answer)
        raise AssertionError("unexpected gh call %r" % (args,))

    m._gh = fake_gh
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("MAIN_HEAD_SHA", raising=False)
    assert m.main() == 0
    return dict(line.split("=", 1)
                for line in out.read_text(encoding="utf-8").splitlines())


def test_main_beats_drift_even_when_every_gating_workflow_is_green(
        monkeypatch, tmp_path):
    m = _mod()
    got = _run_main(m, monkeypatch, tmp_path,
                    _measured(tuple(m.REQUIRED_CONTEXTS) + ("new-gate",)))
    assert got["contexts_status"] == "drift", got
    assert got["status"] == "contexts_drift", got
    assert "new-gate" in got["note"] and "new-gate" in got["contexts_note"], got


def test_main_reports_an_unreadable_list_as_unmeasured(monkeypatch, tmp_path):
    m = _mod()
    got = _run_main(m, monkeypatch, tmp_path, subprocess.CalledProcessError(
        1, ["gh"], stderr="gh: Resource not accessible by integration (HTTP 403)"))
    assert got["contexts_status"] == "unmeasured", got
    assert got["status"] == "contexts_unmeasured", got


def test_main_is_success_when_the_lists_match(monkeypatch, tmp_path):
    m = _mod()
    got = _run_main(m, monkeypatch, tmp_path, _measured(tuple(m.REQUIRED_CONTEXTS)))
    assert got["contexts_status"] == "match", got
    assert got["status"] == "success", got
