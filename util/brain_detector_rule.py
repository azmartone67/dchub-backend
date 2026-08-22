"""Detector-with-the-fix — the merge rule for brain-plumbing PRs (Claim Loop step 4).

THE RULE. A PR that fixes the brain's own plumbing must ship the detector that
would have caught the bug it fixes — a `def check_*` in
routes/brain_consistency_radar.py that is REGISTERED in the sweep. Not merely
defined: registered. `scan_all()` runs the detectors it finds in one tuple
literal (`for fn in (...)`) plus a handful of `detectors.append(<name>)` calls
behind guarded imports. A check that is not in that container never runs, and
nothing in the repo noticed that class of miss until this rule — the existing
registration tests assert `src.count(name) >= 2`, which a name in a comment
satisfies. This module locates the container with `ast` and refuses to count
anything that is not an executable `Name` inside it.

WHAT COUNTS AS A BRAIN-PLUMBING PR (both conditions):
  * the title starts with one of BRAIN_FIX_TITLE_PREFIXES, and
  * EVERY changed file matches BRAIN_PLUMBING_PATHS.
A brain fix that reaches outside that list is a product change by construction
and is judged by product tests, not by this rule. Docs-only `[brain-spec]` PRs
whose body carries the literal SPEC_ONLY_MARKER are exempt — the house
convention for spec-only proposals.

WHAT SATISFIES IT: the diff adds or changes at least one `check_*` function in
the radar whose name is in the sweep container of the PR's version of the file.

Pure by design: everything here takes strings and lists, so the same evaluation
runs in CI (tests/test_brain_prs_carry_detector.py, against git) and in the
weekly number (evaluate_pr_remote / brain_pr_carries_detector, against the
GitHub REST API). No Flask, no DB.
"""
from __future__ import annotations

import ast
import os
import re

RULE_NAME = "detector-with-the-fix"
RADAR_PATH = "routes/brain_consistency_radar.py"
SWEEP_FUNCTION = "scan_all"
SWEEP_REGISTRY = "detectors"          # the list scan_all fills and then runs
SPEC_ONLY_MARKER = "SPEC-ONLY"

# Title prefixes that mark a PR as a fix to the brain's own machinery.
# Matched case-insensitively against the start of the title.
BRAIN_FIX_TITLE_PREFIXES = (
    "fix(brain)",
    "brain-spec",
    "[brain-",
    "fix(squasher)",
    "fix(autonomy)",
)

# The brain's own modules. A PR whose changed files ALL match one of these is
# plumbing. Globs: `*` never crosses a `/`; `**` does. Keep this list explicit
# — it is the containment surface of the rule, and a path that is not here is
# a product path.
BRAIN_PLUMBING_PATHS = (
    "routes/brain_*.py",                 # every brain layer, the radar, autopilot
    "routes/brain_autonomy_*",           # autonomy core (already under the line above; listed for the reader)
    "routes/squasher_*.py",              # the squasher queue + portal
    "routes/cron_heartbeat.py",          # the brain's clock
    "brain_*.py",                        # root-level brain modules
    ".github/workflows/brain-*.yml",     # the brain's own workflows
    "docs/brain-proposals/**",           # spec proposals the brain drafts
    "tests/test_brain_*.py",             # the brain's tests ride along with its fixes
    "tests/test_squasher_*.py",
)

# A docs-only PR for the SPEC-ONLY exemption: markdown anywhere, or anything
# under docs/.
DOCS_ONLY_PATHS = (
    "docs/**",
    "**/*.md",
    "*.md",
)

# GitHub token order, mirroring routes/stability_master_shell._gh_token — a
# test pins the two tuples equal so they cannot drift apart.
_TOKEN_ENV_ORDER = ("PR_SUBMIT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
_REPO_DEFAULT = "azmartone67/dchub-backend"
_GH_API = "https://api.github.com"
_GH_UA = "dchub-detector-rule/1.0"


# ── path matching ────────────────────────────────────────────────────────────

def _glob_to_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_any(path: str, patterns) -> bool:
    p = (path or "").replace(os.sep, "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return any(_glob_to_regex(g).match(p) for g in patterns)


def is_brain_plumbing(path: str) -> bool:
    return matches_any(path, BRAIN_PLUMBING_PATHS)


def is_docs_only(path: str) -> bool:
    return matches_any(path, DOCS_ONLY_PATHS)


def has_brain_fix_prefix(title: str) -> bool:
    t = (title or "").strip().lower()
    return any(t.startswith(p.lower()) for p in BRAIN_FIX_TITLE_PREFIXES)


# ── the sweep container, by ast ──────────────────────────────────────────────

def _sweep_function(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == SWEEP_FUNCTION:
            return node
    raise LookupError(f"{SWEEP_FUNCTION}() not found in the radar")


def _appends_loop_var(for_node: ast.For) -> bool:
    """True when the loop body does `<SWEEP_REGISTRY>.append(<loop var>)`."""
    if not isinstance(for_node.target, ast.Name):
        return False
    var = for_node.target.id
    for node in ast.walk(for_node):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == SWEEP_REGISTRY
                and any(isinstance(a, ast.Name) and a.id == var
                        for a in node.args)):
            return True
    return False


def sweep_container(radar_src: str) -> ast.For:
    """The `for fn in (...)` whose body appends `fn` to the registry."""
    sweep = _sweep_function(ast.parse(radar_src))
    for node in ast.walk(sweep):
        if (isinstance(node, ast.For)
                and isinstance(node.iter, (ast.Tuple, ast.List))
                and _appends_loop_var(node)):
            return node
    raise LookupError(
        f"sweep container not found: no `for <fn> in (...)` inside "
        f"{SWEEP_FUNCTION}() appends its loop variable to `{SWEEP_REGISTRY}`")


def sweep_container_location(radar_src: str) -> tuple[int, int]:
    """(first line, last line) of the container tuple — for messages."""
    node = sweep_container(radar_src)
    return node.iter.lineno, node.iter.end_lineno or node.iter.lineno


def registered_checks(radar_src: str) -> set[str]:
    """Every name the sweep will actually run.

    Executable `Name` elements of the container tuple, plus every
    `detectors.append(<Name>)` inside scan_all (the guarded-import detectors).
    Comments, strings and docstrings are invisible to this by construction.
    Raises LookupError rather than returning an empty set when the container
    cannot be found — 'nothing registered' must never read as 'no rule'.
    """
    container = sweep_container(radar_src)
    names = {e.id for e in container.iter.elts if isinstance(e, ast.Name)}
    sweep = _sweep_function(ast.parse(radar_src))
    loop_vars = {n.target.id for n in ast.walk(sweep)
                 if isinstance(n, ast.For) and isinstance(n.target, ast.Name)}
    for node in ast.walk(sweep):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == SWEEP_REGISTRY):
            names.update(a.id for a in node.args
                         if isinstance(a, ast.Name) and a.id not in loop_vars)
    return names


def defined_checks(radar_src: str) -> set[str]:
    tree = ast.parse(radar_src)
    return {n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("check_")}


# ── the diff ─────────────────────────────────────────────────────────────────

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_line_numbers(diff_text: str, path: str = RADAR_PATH) -> tuple[set[int], set[int]]:
    """(added line numbers in the new file, removed line numbers in the old
    file) for `path` inside a unified diff that may span several files."""
    added, removed = set(), set()
    current = None
    old = new = 0
    in_hunk = False
    for raw in (diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            current, in_hunk = None, False
            continue
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            current = name
            in_hunk = False
            continue
        if raw.startswith("--- "):
            continue
        m = _HUNK_RE.match(raw)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk or current != path:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            added.add(new)
            new += 1
        elif raw.startswith("-"):
            removed.add(old)
            old += 1
        else:
            old += 1
            new += 1
    return added, removed


def _check_functions_covering(src: str, lines: set[int]) -> set[str]:
    if not lines:
        return set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("check_")):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        if any(start <= ln <= end for ln in lines):
            out.add(node.name)
    return out


def check_functions_touched(new_src: str, diff_text: str,
                            old_src: str | None = None) -> set[str]:
    """`check_*` functions the diff adds or changes: any function (in the new
    file) containing an added line, or (in the old file, when given)
    containing a removed line."""
    added, removed = diff_line_numbers(diff_text)
    touched = _check_functions_covering(new_src or "", added)
    if old_src is not None:
        touched |= _check_functions_covering(old_src, removed)
    return touched


# ── the verdict ──────────────────────────────────────────────────────────────

def evaluate_pr(title: str, body: str, changed_files, radar_new_src=None,
                radar_diff=None, radar_old_src=None) -> dict:
    """Apply the rule to one PR. Pure.

    Returns {"rule", "applies", "ok", "reason", "touched", "registered",
    "unregistered"}. `applies` False = the rule does not govern this PR
    (reason says why). `ok` None with `applies` True = the radar diff was not
    supplied, so the verdict could not be computed — callers decide whether
    that is a failure (CI: yes, for a brain fix) or unmeasured (weekly: None).
    """
    v = {"rule": RULE_NAME, "applies": False, "ok": None, "reason": "",
         "touched": [], "registered": [], "unregistered": []}
    if not has_brain_fix_prefix(title):
        v["reason"] = (f"title {title!r} does not start with a brain-fix prefix "
                       f"{BRAIN_FIX_TITLE_PREFIXES}")
        return v
    files = sorted({(f or "").replace(os.sep, "/") for f in (changed_files or []) if f})
    if not files:
        v["reason"] = "no changed files"
        return v
    if SPEC_ONLY_MARKER in (body or "") and all(is_docs_only(f) for f in files):
        v["reason"] = (f"docs-only PR whose body carries {SPEC_ONLY_MARKER} — "
                       f"exempt by house convention")
        return v
    outside = [f for f in files if not is_brain_plumbing(f)]
    if outside:
        v["reason"] = (f"touches non-plumbing file(s) {outside[:5]} — a product "
                       f"change, judged by product tests rather than this rule")
        return v
    v["applies"] = True
    if RADAR_PATH not in files:
        v["ok"] = False
        v["reason"] = (f"{RULE_NAME}: a brain-plumbing fix must add or change a "
                       f"registered check_* in {RADAR_PATH}; this PR does not "
                       f"touch it")
        return v
    if radar_new_src is None or radar_diff is None:
        v["reason"] = f"{RULE_NAME}: the radar diff was not available, verdict not computed"
        return v
    try:
        registered = registered_checks(radar_new_src)
    except (LookupError, SyntaxError) as e:
        v["ok"] = False
        v["reason"] = f"{RULE_NAME}: cannot locate the sweep container — {e}"
        return v
    touched = check_functions_touched(radar_new_src, radar_diff, radar_old_src)
    v["touched"] = sorted(touched)
    v["registered"] = sorted(touched & registered)
    v["unregistered"] = sorted(touched - registered)
    if not touched:
        v["ok"] = False
        v["reason"] = (f"{RULE_NAME}: the PR touches {RADAR_PATH} but adds or "
                       f"changes no check_* function")
        return v
    v["ok"] = bool(v["registered"])
    if v["ok"]:
        v["reason"] = f"carries registered detector(s) {v['registered']}"
    else:
        lo, hi = sweep_container_location(radar_new_src)
        v["reason"] = (f"{RULE_NAME}: {v['unregistered']} defined but NOT in the "
                       f"sweep container ({SWEEP_FUNCTION}() `for fn in (...)`, "
                       f"{RADAR_PATH}:{lo}-{hi}) — a check that is not registered "
                       f"never runs. Add the name to the tuple.")
    return v


# ── the weekly number: re-evaluate a merged PR over the GitHub REST API ─────

def gh_token() -> str:
    for n in _TOKEN_ENV_ORDER:
        val = (os.environ.get(n) or "").strip()
        if val:
            return val
    return ""


def _gh_headers(token: str, raw: bool = False) -> dict:
    h = {"Accept": "application/vnd.github.raw+json" if raw
         else "application/vnd.github+json",
         "User-Agent": _GH_UA}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def evaluate_pr_remote(pr_number: int, repo: str | None = None,
                       token: str | None = None, session=None,
                       timeout: int = 10) -> dict | None:
    """evaluate_pr() for a PR fetched over the REST API. None on ANY failure
    (no token, rate limit, >300 files, patch withheld) — fail-soft, never a
    verdict built on a partial read."""
    try:
        import requests
        s = session or requests.Session()
        repo = repo or _REPO_DEFAULT
        tok = token if token is not None else gh_token()
        base = f"{_GH_API}/repos/{repo}/pulls/{int(pr_number)}"
        r = s.get(base, headers=_gh_headers(tok), timeout=timeout)
        if r.status_code != 200:
            return None
        pr = r.json()
        title, body = pr.get("title") or "", pr.get("body") or ""
        ref = (pr.get("merge_commit_sha") if pr.get("merged")
               else (pr.get("head") or {}).get("sha"))
        base_sha = (pr.get("base") or {}).get("sha")
        files, radar_patch = [], None
        for page in (1, 2, 3):
            r = s.get(f"{base}/files", params={"per_page": 100, "page": page},
                      headers=_gh_headers(tok), timeout=timeout)
            if r.status_code != 200:
                return None
            chunk = r.json() or []
            for f in chunk:
                files.append(f.get("filename"))
                if f.get("filename") == RADAR_PATH:
                    radar_patch = f.get("patch")
            if len(chunk) < 100:
                break
        else:
            return None
        v = evaluate_pr(title, body, files)
        if not v["applies"] or RADAR_PATH not in files:
            return v
        if radar_patch is None or not ref:
            return None
        contents = f"{_GH_API}/repos/{repo}/contents/{RADAR_PATH}"
        r = s.get(contents, params={"ref": ref}, headers=_gh_headers(tok, raw=True),
                  timeout=timeout)
        if r.status_code != 200:
            return None
        new_src = r.text
        old_src = None
        if base_sha:
            r = s.get(contents, params={"ref": base_sha},
                      headers=_gh_headers(tok, raw=True), timeout=timeout)
            if r.status_code == 200:
                old_src = r.text
        diff_text = (f"diff --git a/{RADAR_PATH} b/{RADAR_PATH}\n"
                     f"--- a/{RADAR_PATH}\n+++ b/{RADAR_PATH}\n{radar_patch}\n")
        return evaluate_pr(title, body, files, new_src, diff_text, old_src)
    except Exception:
        return None


def brain_pr_carries_detector(pr_number: int, **kw) -> bool | None:
    """True/False = the rule applied and the PR did/did not carry a registered
    detector. None = the rule does not govern this PR OR it could not be
    evaluated; use evaluate_pr_remote() when the two must be told apart."""
    v = evaluate_pr_remote(pr_number, **kw)
    if not v or not v.get("applies"):
        return None
    return v.get("ok")
