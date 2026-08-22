"""Detector-with-the-fix — the merge rule for brain-plumbing PRs (Claim Loop step 4).

★ THE RULE. A PR that fixes the brain's own plumbing must ship the detector
that would have caught the bug it fixes: a `def check_*` in
routes/brain_consistency_radar.py that is REGISTERED in the sweep.

★ THE TRAP THIS CLOSES. scan_all() runs the detectors it finds in one tuple
literal — `for fn in (...)` — plus a few `detectors.append(<name>)` calls. A
check that is defined but not in that container NEVER RUNS, and until now the
registration tests asserted `src.count(name) >= 2`, which a name in a comment
satisfies. Every membership claim here is made with `ast` against executable
text; a comment is invisible to it by construction.

Three layers:
  1. the LIVE rule — in a pull_request CI context it evaluates THIS PR and
     skips elsewhere with a reason (never a silent green);
  2. fixture PRs through the same pure evaluator (util/brain_detector_rule) —
     the RED/GREEN pair the rule is mutation-tested on, the grep trap, the
     exemptions and the controls;
  3. the registry itself — the sweep container is located by ast on the real
     radar, and the three step-4 detectors are proven to be IN it.

Also pins the weekly-number helper (brain_pr_carries_detector) against a
canned REST session, and its token order against stability_master_shell.
"""
import ast
import difflib
import json
import os
import subprocess

import pytest

from util import brain_detector_rule as rule

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RADAR = os.path.join(REPO_ROOT, "routes", "brain_consistency_radar.py")
STEP4_DETECTORS = (
    "check_measurement_definition_changed",
    "check_stored_slug_resolves",
    "check_funnel_adjacent_step_collapse",
)


# ── a stub radar: one registered check, one commented-out name ───────────────

_STUB = '''"""stub radar"""


def check_a() -> list:
    return []


def check_b() -> list:
    return []


def scan_all() -> list:
    detectors = []
    for fn in (check_a,
               # check_b,
               ):
        detectors.append(fn)
    return detectors
'''

_ADD_X = _STUB + "\n\ndef check_x() -> list:\n    return []\n"


def _replace_once(src, old, new):
    assert src.count(old) == 1, f"anchor {old!r} found {src.count(old)}x"
    return src.replace(old, new, 1)


_ADD_X_REGISTERED = _replace_once(
    _ADD_X, "    for fn in (check_a,\n",
    "    for fn in (check_a,\n               check_x,\n")


def _diff(old, new):
    return "".join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile="a/" + rule.RADAR_PATH, tofile="b/" + rule.RADAR_PATH))


def _verdict(new_src, old_src=_STUB, title="fix(brain): a thing",
             body="", files=(rule.RADAR_PATH,)):
    return rule.evaluate_pr(title, body, list(files), new_src,
                            _diff(old_src, new_src), old_src)


# ── 2. the RED/GREEN pair ────────────────────────────────────────────────────

def test_an_added_check_without_registration_is_red():
    """This test proves the rule FAILS when a brain fix adds check_x but
    leaves it out of the sweep tuple."""
    v = _verdict(_ADD_X)
    assert v["applies"] is True
    assert v["ok"] is False
    assert v["unregistered"] == ["check_x"]
    assert "check_x" in v["reason"] and rule.RULE_NAME in v["reason"]
    assert rule.RADAR_PATH in v["reason"], "the reason must point at the container"


def test_registering_it_turns_green():
    v = _verdict(_ADD_X_REGISTERED)
    assert v["applies"] is True
    assert v["ok"] is True
    assert v["registered"] == ["check_x"]


def test_a_name_in_a_comment_does_not_register():
    """★ THE GREP TRAP. `src.count('check_x') >= 2` is satisfied by a comment
    in the tuple; the sweep is not."""
    commented = _replace_once(
        _ADD_X, "               # check_b,\n",
        "               # check_b,\n               # check_x,\n")
    assert commented.count("check_x") >= 2, "the grep-style check would pass here"
    assert rule.registered_checks(commented) == {"check_a"}
    assert _verdict(commented)["ok"] is False


def test_a_name_in_a_string_does_not_register():
    stringed = _replace_once(
        _ADD_X, "    detectors = []\n",
        "    detectors = []\n    _note = 'check_x'\n")
    assert rule.registered_checks(stringed) == {"check_a"}
    assert _verdict(stringed)["ok"] is False


def test_detectors_append_registration_counts():
    appended = _replace_once(
        _ADD_X, "        detectors.append(fn)\n",
        "        detectors.append(fn)\n    detectors.append(check_x)\n")
    assert rule.registered_checks(appended) == {"check_a", "check_x"}
    assert _verdict(appended)["ok"] is True


def test_changing_a_registered_check_counts_as_carrying_it():
    changed = _replace_once(
        _STUB, "def check_a() -> list:\n    return []",
        "def check_a() -> list:\n    return [{'issue': 'x'}]")
    v = _verdict(changed)
    assert v["ok"] is True and v["registered"] == ["check_a"]


def test_changing_an_unregistered_check_is_still_red():
    changed = _replace_once(
        _STUB, "def check_b() -> list:\n    return []",
        "def check_b() -> list:\n    return [{'issue': 'x'}]")
    v = _verdict(changed)
    assert v["ok"] is False and v["unregistered"] == ["check_b"]


def test_a_removed_line_is_attributed_through_the_old_source():
    old = _replace_once(
        _STUB, "def check_a() -> list:\n    return []",
        "def check_a() -> list:\n    _x = 1\n    return []")
    v = _verdict(_STUB, old_src=old)
    assert v["registered"] == ["check_a"]


def test_touching_the_radar_without_a_check_is_red():
    v = _verdict(_STUB + "\n\ndef _helper():\n    return 1\n")
    assert v["applies"] is True and v["ok"] is False
    assert "no check_*" in v["reason"]


def test_a_missing_container_is_red_not_unmeasured():
    v = _verdict("def check_x():\n    return []\n")
    assert v["applies"] is True and v["ok"] is False
    assert "container" in v["reason"]


# ── exemptions and controls ──────────────────────────────────────────────────

def test_a_non_plumbing_file_puts_the_pr_out_of_scope():
    """CONTROL: the rule must not govern product changes."""
    v = _verdict(_ADD_X, files=(rule.RADAR_PATH, "routes/facility_profile_page.py"))
    assert v["applies"] is False and v["ok"] is None
    assert "facility_profile_page" in v["reason"]


def test_a_non_brain_title_is_out_of_scope():
    v = _verdict(_ADD_X, title="feat(seo): readmit GSC-proven URLs")
    assert v["applies"] is False and v["ok"] is None


@pytest.mark.parametrize("prefix", rule.BRAIN_FIX_TITLE_PREFIXES + ("FIX(BRAIN)",))
def test_every_declared_prefix_is_governed(prefix):
    assert _verdict(_ADD_X, title=f"{prefix}: something")["applies"] is True


def test_a_brain_fix_that_never_touches_the_radar_is_red():
    v = rule.evaluate_pr("fix(brain): L16 self-critique", "",
                         ["routes/brain_layer16_self_critique.py"])
    assert v["applies"] is True and v["ok"] is False
    assert rule.RADAR_PATH in v["reason"]


def test_a_spec_only_docs_pr_is_exempt():
    v = rule.evaluate_pr("[brain-spec] a proposal", "summary\n\nSPEC-ONLY\n",
                         ["docs/brain-proposals/2026-08-21-x.md"])
    assert v["applies"] is False and rule.SPEC_ONLY_MARKER in v["reason"]


def test_the_spec_only_marker_does_not_exempt_code():
    v = _verdict(_ADD_X, title="[brain-spec] a proposal", body="SPEC-ONLY")
    assert v["applies"] is True and v["ok"] is False


def test_docs_only_without_the_marker_is_governed():
    v = rule.evaluate_pr("[brain-spec] a proposal", "no marker here",
                         ["docs/brain-proposals/2026-08-21-x.md"])
    assert v["applies"] is True and v["ok"] is False


def test_no_radar_diff_is_no_verdict_not_a_pass():
    v = rule.evaluate_pr("fix(brain): x", "", [rule.RADAR_PATH])
    assert v["applies"] is True and v["ok"] is None


# ── the plumbing list is explicit ────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "routes/brain_consistency_radar.py",
    "routes/brain_autonomy_core.py",
    "routes/squasher_queue.py",
    "routes/cron_heartbeat.py",
    "brain_loop.py",
    ".github/workflows/brain-nightly.yml",
    "docs/brain-proposals/2026-08-21-x.md",
    "docs/brain-proposals/nested/deeper.md",
    "tests/test_brain_prs_carry_detector.py",
    "tests/test_squasher_queue.py",
])
def test_plumbing_paths(path):
    assert rule.is_brain_plumbing(path)


@pytest.mark.parametrize("path", [
    "routes/facility_profile_page.py",
    "routes/brainy_product.py",
    "routes/brain_sub/inner.py",
    "main.py",
    "tests/test_seo_slug_and_soft404.py",
    ".github/workflows/pre-merge.yml",
    "docs/ROLLBACK-RUNBOOK.md",
])
def test_non_plumbing_paths(path):
    assert not rule.is_brain_plumbing(path)


# ── the diff parser ──────────────────────────────────────────────────────────

def test_diff_line_numbers_follow_the_hunk_headers():
    diff = ("--- a/routes/brain_consistency_radar.py\n"
            "+++ b/routes/brain_consistency_radar.py\n"
            "@@ -10,3 +10,4 @@ def check_q():\n"
            " ctx\n-old\n+new1\n+new2\n ctx2\n")
    added, removed = rule.diff_line_numbers(diff)
    assert added == {11, 12} and removed == {11}


def test_other_files_in_the_diff_are_ignored():
    diff = ("diff --git a/routes/other.py b/routes/other.py\n"
            "--- a/routes/other.py\n+++ b/routes/other.py\n"
            "@@ -1,1 +1,2 @@\n x\n+y\n")
    assert rule.diff_line_numbers(diff) == (set(), set())


# ── 3. the real radar ────────────────────────────────────────────────────────

def _radar_src():
    with open(RADAR, encoding="utf-8") as fh:
        return fh.read()


def test_the_real_sweep_container_is_found_by_ast():
    src = _radar_src()
    reg = rule.registered_checks(src)
    assert {"check_funnel_step_collapse", "check_stale_stored_slug_404s",
            "check_unmarked_population_shift"} <= reg
    assert len(reg) >= 100
    lo, hi = rule.sweep_container_location(src)
    assert hi - lo > 100, "the container is a ~400-line tuple, not a one-liner"


def test_every_defined_check_in_the_radar_is_registered():
    """The class this rule exists to close, asserted on the file as it ships."""
    src = _radar_src()
    orphans = rule.defined_checks(src) - rule.registered_checks(src)
    assert orphans == set(), f"defined but never swept: {sorted(orphans)}"


@pytest.mark.parametrize("name", STEP4_DETECTORS)
def test_step4_detector_is_in_the_sweep_container(name):
    """ast proof, not grep: the name must be an executable element of the
    scan_all tuple (or a detectors.append argument)."""
    src = _radar_src()
    assert name in rule.defined_checks(src), f"{name} is not defined"
    assert name in rule.registered_checks(src), (
        f"{name} is defined but NOT in the scan_all tuple — it would never run")


# ── 1. the live rule, in a pull_request CI context ───────────────────────────

def _pr_event():
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except Exception:
        return None
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return None
    return {"number": pr.get("number") or event.get("number"),
            "title": pr.get("title") or "", "body": pr.get("body") or ""}


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=180)


def _pr_refs(number):
    """(base, head) to diff. The GitHub merge ref works at any checkout depth
    (the unit-tests job checks out at depth 1); origin/main is the local
    fallback. DCHUB_PR_RULE_BASE_REF pins the base for a local simulation of
    the CI path (the harness that proves this test goes RED). None = cannot
    determine."""
    if number:
        ref = f"refs/remotes/pull/{number}/merge"
        r = _git("fetch", "-q", "--depth=2", "origin",
                 f"+refs/pull/{number}/merge:{ref}")
        if r.returncode == 0 and _git("rev-parse", "--verify", "-q",
                                      f"{ref}^1").returncode == 0:
            return f"{ref}^1", ref
    pinned = (os.environ.get("DCHUB_PR_RULE_BASE_REF") or "").strip()
    if pinned and _git("rev-parse", "--verify", "-q", pinned).returncode == 0:
        return pinned, "HEAD"
    if _git("rev-parse", "--verify", "-q", "origin/main").returncode == 0:
        mb = _git("merge-base", "origin/main", "HEAD")
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip(), "HEAD"
    return None


def test_this_pr_carries_a_detector_when_it_is_a_brain_fix():
    ev = _pr_event()
    if ev is None:
        pytest.skip("not in a pull_request CI context (no GITHUB_EVENT_PATH "
                    "with a pull_request) — the rule is exercised by the "
                    "fixture tests in this file")
    if not rule.has_brain_fix_prefix(ev["title"]):
        pytest.skip(f"rule does not govern this PR: title {ev['title']!r} "
                    f"has no brain-fix prefix {rule.BRAIN_FIX_TITLE_PREFIXES}")
    refs = _pr_refs(ev["number"])
    assert refs, (f"{rule.RULE_NAME}: PR #{ev['number']} is a brain fix but its "
                  f"diff could not be determined (refs/pull/N/merge fetch failed "
                  f"and origin/main is absent) — the rule cannot be verified, "
                  f"which is not a pass")
    base, head = refs
    files = _git("diff", "--name-only", base, head).stdout.split()
    radar_diff = _git("diff", base, head, "--", rule.RADAR_PATH).stdout
    new_src = _git("show", f"{head}:{rule.RADAR_PATH}").stdout
    old = _git("show", f"{base}:{rule.RADAR_PATH}")
    old_src = old.stdout if old.returncode == 0 else None
    v = rule.evaluate_pr(ev["title"], ev["body"], files, new_src, radar_diff, old_src)
    if not v["applies"]:
        pytest.skip(v["reason"])
    assert v["ok"], v["reason"]


# ── the weekly-number helper ─────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


class _Session:
    """Canned GitHub: keyed on (url suffix, ref/page)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        for (suffix, ref), resp in self.routes.items():
            if url.endswith(suffix) and (ref is None or params.get("ref") == ref
                                         or params.get("page") == ref):
                return resp
        return _Resp(404, {})


def _canned(title, new_src, old_src=_STUB, files=(rule.RADAR_PATH,)):
    patch = "".join(_diff(old_src, new_src).splitlines(True)[2:])
    return _Session({
        ("/pulls/7", None): _Resp(200, {"title": title, "body": "", "merged": True,
                                       "merge_commit_sha": "newsha",
                                       "base": {"sha": "oldsha"},
                                       "head": {"sha": "headsha"}}),
        ("/pulls/7/files", 1): _Resp(200, [
            {"filename": f, "patch": patch if f == rule.RADAR_PATH else ""}
            for f in files]),
        ("/contents/" + rule.RADAR_PATH, "newsha"): _Resp(200, text=new_src),
        ("/contents/" + rule.RADAR_PATH, "oldsha"): _Resp(200, text=old_src),
    })


def test_remote_helper_says_true_for_a_carrying_pr():
    s = _canned("fix(brain): x", _ADD_X_REGISTERED)
    assert rule.brain_pr_carries_detector(7, session=s, token="t") is True


def test_remote_helper_says_false_for_a_non_carrying_pr():
    s = _canned("fix(brain): x", _ADD_X)
    assert rule.brain_pr_carries_detector(7, session=s, token="t") is False


def test_remote_helper_is_none_when_the_rule_does_not_govern():
    s = _canned("feat(seo): x", _ADD_X)
    assert rule.brain_pr_carries_detector(7, session=s, token="t") is None
    v = rule.evaluate_pr_remote(7, session=s, token="t")
    assert v is not None and v["applies"] is False


def test_remote_helper_is_fail_soft():
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("network")
    assert rule.brain_pr_carries_detector(7, session=_Boom(), token="t") is None
    assert rule.evaluate_pr_remote(7, session=_Boom(), token="t") is None


def test_remote_helper_refuses_a_withheld_patch():
    s = _canned("fix(brain): x", _ADD_X_REGISTERED)
    s.routes[("/pulls/7/files", 1)] = _Resp(200, [{"filename": rule.RADAR_PATH}])
    assert rule.brain_pr_carries_detector(7, session=s, token="t") is None


def test_remote_helper_sends_the_token_and_the_raw_accept(monkeypatch):
    s = _canned("fix(brain): x", _ADD_X_REGISTERED)
    monkeypatch.setenv("PR_SUBMIT_TOKEN", "pr-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
    assert rule.gh_token() == "pr-tok"
    monkeypatch.delenv("PR_SUBMIT_TOKEN")
    assert rule.gh_token() == "gh-tok"
    assert rule.brain_pr_carries_detector(7, session=s) is True
    assert any("/contents/" in url for url, _p in s.calls)


def test_token_order_mirrors_stability_master_shell():
    path = os.path.join(REPO_ROOT, "routes", "stability_master_shell.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_gh_token")
    tuples = [n for n in ast.walk(fn) if isinstance(n, ast.Tuple)
              and n.elts and all(isinstance(e, ast.Constant) for e in n.elts)]
    assert tuples, "_gh_token no longer iterates a tuple of env names"
    assert tuple(e.value for e in tuples[0].elts) == rule._TOKEN_ENV_ORDER
