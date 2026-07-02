"""
drift_pr_writer.py — generalized drift-fix DRAFT-PR writer (machinery ON RAILS).

Modeled on routes/brain_layer22_pr_writer.py (open_route_alias_pr): clone the
bot fork via GH_TOKEN (auto-fork if missing), branch, apply edits, commit,
push, `gh pr create --draft`. Generalized to arbitrary EXACT-string edits so
the AI-surface sentinel (ai_surface_sentinel.py) can turn simple count-string
drift on frontend-owned surfaces (llms.txt, ai.json, AGENTS.md …) into a
reviewable draft PR instead of a brain finding that waits for a human.

    open_drift_fix_pr(repo, edits, rationale)
        edits = [{"path": ..., "find": ..., "replace": ...}, ...]

Edit semantics — EXACT-string only, never regex, never fuzzy:
  * `find` must occur EXACTLY ONCE in the file; 0 or 2+ occurrences → that
    edit is SKIPPED and reported (never a guess-y replace).

Safety fences (ALL mandatory):
  * DRY_RUN unless DCHUB_DRIFT_PR=1 (default OFF) — logs what WOULD happen.
  * DRAFT PRs only (gh pr create --draft), human merges.
  * Repo allowlist: azmartone67/dchub-frontend + azmartone67/dchub-backend.
  * Refuses any path under .github/ (workflow-edit trap) or named
    _routes.json (CF Pages routing — one bad edit takes the site down).
  * Refuses absolute paths and ".." traversal.
  * Max 5 edits per PR.
  * _safe_run subprocess wrapper with a hard timeout (mirrors L22).

Never raises — every entry point returns a dict with ok/stage/error.
"""
from __future__ import annotations

import datetime
import os
import subprocess

UPSTREAM_OWNER = "azmartone67"
ALLOWED_REPOS = {
    f"{UPSTREAM_OWNER}/dchub-frontend",
    f"{UPSTREAM_OWNER}/dchub-backend",
}
FORK_OWNER = os.environ.get("DRIFT_PR_FORK_OWNER", "dchub-cloud-bot")
DEFAULT_BASE = os.environ.get("DRIFT_PR_BASE_BRANCH", "main")
GH_TOKEN = (os.environ.get("PR_SUBMIT_TOKEN")
            or os.environ.get("GITHUB_TOKEN") or "").strip()
MAX_EDITS_PER_PR = 5

_BOT_AUTHOR_NAME = "dchub-drift-bot"
_BOT_AUTHOR_EMAIL = "drift-bot@dchub.cloud"


def _dry_run() -> bool:
    """Read the flag at call time (not import time) so flipping the Railway
    env var takes effect on the next run without a code change."""
    return os.environ.get("DCHUB_DRIFT_PR", "0") != "1"


def _safe_run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    """Run subprocess; return (returncode, stdout+stderr). Mirrors L22.
    Output is TOKEN-REDACTED before it leaves this function: git prints the
    remote URL (with the embedded x-access-token) on clone/auth failures,
    and these strings flow into logs and API responses — this repo has a
    leaked-secret incident history, so redact at the single choke point."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True,
                           text=True, timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        if GH_TOKEN:
            out = out.replace(GH_TOKEN, "***REDACTED***")
        return r.returncode, out
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        if GH_TOKEN:
            msg = msg.replace(GH_TOKEN, "***REDACTED***")
        return 1, msg


def _path_refused(path: str) -> str | None:
    """Return a refusal reason for a forbidden/unsafe repo path, else None."""
    if not path or not isinstance(path, str):
        return "empty path"
    norm = os.path.normpath(path.strip()).replace("\\", "/")
    if norm.startswith("/") or norm.startswith(".."):
        return "absolute or traversal path refused"
    if norm == ".github" or norm.startswith(".github/"):
        return ".github/ is fenced (workflow-edit trap)"
    if os.path.basename(norm) == "_routes.json":
        return "_routes.json is fenced (CF Pages routing)"
    return None


def _validate(repo: str, edits: list) -> str | None:
    """Return an error string if the request violates a fence, else None."""
    if repo not in ALLOWED_REPOS:
        return f"repo {repo!r} not in allowlist {sorted(ALLOWED_REPOS)}"
    if not isinstance(edits, list) or not edits:
        return "edits must be a non-empty list of {path, find, replace}"
    if len(edits) > MAX_EDITS_PER_PR:
        return f"{len(edits)} edits > max {MAX_EDITS_PER_PR} per PR"
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return f"edits[{i}] is not a dict"
        for k in ("path", "find", "replace"):
            if not isinstance(e.get(k), str) or not e.get(k):
                return f"edits[{i}].{k} missing or empty"
        reason = _path_refused(e["path"])
        if reason:
            return f"edits[{i}].path {e['path']!r}: {reason}"
        if e["find"] == e["replace"]:
            return f"edits[{i}] find == replace (no-op)"
    return None


def _apply_edits(repo_dir: str, edits: list) -> tuple[list, list]:
    """Apply exact-string edits inside the clone. Returns (applied, skipped).

    An edit is applied ONLY when `find` occurs EXACTLY ONCE in the file —
    0 or 2+ occurrences means the surface changed under us, so we skip and
    report rather than guess."""
    applied, skipped = [], []
    for e in edits:
        rel = os.path.normpath(e["path"]).replace("\\", "/")
        full = os.path.join(repo_dir, rel)
        if not os.path.isfile(full):
            skipped.append({"path": rel, "reason": "file not found in repo"})
            continue
        try:
            with open(full, encoding="utf-8") as f:
                content = f.read()
        except Exception as ex:
            skipped.append({"path": rel, "reason": f"read: {str(ex)[:80]}"})
            continue
        n = content.count(e["find"])
        if n != 1:
            skipped.append({"path": rel,
                            "reason": f"find occurs {n}x (need exactly 1)",
                            "find": e["find"][:80]})
            continue
        try:
            with open(full, "w", encoding="utf-8") as f:
                f.write(content.replace(e["find"], e["replace"], 1))
        except Exception as ex:
            skipped.append({"path": rel, "reason": f"write: {str(ex)[:80]}"})
            continue
        applied.append({"path": rel, "find": e["find"][:80],
                        "replace": e["replace"][:80]})
    return applied, skipped


def open_drift_fix_pr(repo: str, edits: list, rationale: str = "") -> dict:
    """Main entry: validate fences, clone the fork, apply exact-string edits,
    commit, push, open a DRAFT PR. Never raises.

    DRY_RUN (default, DCHUB_DRIFT_PR unset/!=1) validates + reports what WOULD
    happen without any git/network side effects."""
    err = _validate(repo, edits)
    if err:
        return {"ok": False, "stage": "validate", "error": err}

    if _dry_run():
        return {
            "ok": True,
            "dry_run": True,
            "would_do": (f"open DRAFT PR against {repo} with "
                         f"{len(edits)} exact-string edit(s): "
                         + "; ".join(f"{e['path']}: {e['find'][:40]!r} -> "
                                     f"{e['replace'][:40]!r}" for e in edits)),
            "hint": "Set DCHUB_DRIFT_PR=1 to enable real git ops.",
        }
    if not GH_TOKEN:
        return {"ok": False, "stage": "auth",
                "error": "PR_SUBMIT_TOKEN / GITHUB_TOKEN unset"}

    repo_name = repo.split("/", 1)[1]
    ts = datetime.datetime.utcnow()
    branch = f"auto-drift-fix-{ts.strftime('%Y%m%d%H%M%S')}"
    work_dir = f"/tmp/drift-pr-{repo_name}-{int(ts.timestamp())}"
    fork_url = (f"https://x-access-token:{GH_TOKEN}@github.com/"
                f"{FORK_OWNER}/{repo_name}.git")

    # 1. Clone the fork (auto-fork on first failure, like L22)
    code, out = _safe_run(["git", "clone", "--depth", "1", "--branch",
                           DEFAULT_BASE, fork_url, work_dir])
    if code != 0:
        _safe_run(["gh", "repo", "fork", repo, "--org", FORK_OWNER,
                   "--clone=false", "--remote=false"])
        code, out = _safe_run(["git", "clone", "--depth", "1", "--branch",
                               DEFAULT_BASE, fork_url, work_dir])
        if code != 0:
            return {"ok": False, "stage": "fork_clone", "error": out[:300]}

    # 2. Author + branch
    _safe_run(["git", "config", "user.name", _BOT_AUTHOR_NAME], cwd=work_dir)
    _safe_run(["git", "config", "user.email", _BOT_AUTHOR_EMAIL], cwd=work_dir)
    code, out = _safe_run(["git", "checkout", "-b", branch], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "branch", "error": out[:300]}

    # 3. Apply exact-string edits
    applied, skipped = _apply_edits(work_dir, edits)
    if not applied:
        return {"ok": False, "stage": "patch", "no_action": True,
                "reason": "no edit matched exactly once", "skipped": skipped}

    # 4. Commit
    for a in applied:
        code, out = _safe_run(["git", "add", a["path"]], cwd=work_dir)
        if code != 0:
            return {"ok": False, "stage": "git_add", "error": out[:300]}
    commit_msg = (
        f"[brain-drift-auto] fix {len(applied)} stale value(s) from canon\n\n"
        f"Auto-drafted by the AI-surface drift PR writer "
        f"(routes/drift_pr_writer.py).\n"
        + "\n".join(f"- {a['path']}: {a['find']!r} -> {a['replace']!r}"
                    for a in applied)
        + (f"\n\n{rationale}" if rationale else "")
        + "\n\nExact-string edits only (each `find` matched exactly once); "
          "draft PR, human merges.\n"
          "Co-Authored-By: DC Hub Drift Bot <drift-bot@dchub.cloud>"
    )
    code, out = _safe_run(["git", "commit", "-m", commit_msg], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_commit", "error": out[:300]}

    # 5. Push
    code, out = _safe_run(["git", "push", "-u", "origin", branch],
                          cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_push", "error": out[:300]}

    # 6. DRAFT PR (never a ready-for-review PR)
    pr_title = f"[drift-fix] {len(applied)} stale value(s) vs ai_surface_canon (auto, draft)"
    pr_body = (
        "## What\n\n"
        "The AI-surface sentinel found served value(s) drifted from "
        "`ai_surface_canon` and drafted the mechanical fix:\n\n"
        + "\n".join(f"- `{a['path']}`: `{a['find']}` → `{a['replace']}`"
                    for a in applied)
        + ("\n\nSkipped (find not matched exactly once):\n"
           + "\n".join(f"- `{s['path']}`: {s['reason']}" for s in skipped)
           if skipped else "")
        + f"\n\n## Why\n\n{rationale or 'Canon drift detected by ai_surface_sentinel.'}\n\n"
        "## Safety\n\n"
        "- EXACT-string edits only; each `find` matched exactly once.\n"
        "- Fenced: never `.github/`, never `_routes.json`, max "
        f"{MAX_EDITS_PER_PR} edits, allowlisted repos only.\n"
        "- DRAFT PR — a human reviews + merges.\n\n"
        "## How to revert\n\n"
        "`git revert <merge-commit>` — isolated string swaps.\n\n"
        "---\n_Auto-generated by routes/drift_pr_writer.py "
        "(gated by DCHUB_DRIFT_PR)._\n"
    )
    code, out = _safe_run([
        "gh", "pr", "create", "--repo", repo,
        "--head", f"{FORK_OWNER}:{branch}", "--base", DEFAULT_BASE,
        "--draft", "--title", pr_title, "--body", pr_body,
    ], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "gh_pr_create", "error": out[:300],
                "branch": branch, "applied": applied, "skipped": skipped}

    pr_url = next((ln.strip() for ln in out.splitlines()
                   if ln.strip().startswith("https://github.com/")), None)
    return {"ok": True, "stage": "complete", "branch": branch,
            "pr_url": pr_url, "applied": applied, "skipped": skipped}
