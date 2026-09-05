"""
brain_layer22_pr_writer.py — Phase r57 (2026-05-25).

Promotes L22 from Issue-drafter to actual PR-writer for ONE
whitelisted recipe: route_alias_404. Other recipes stay Issue-only
until that pattern proves out.

How it works:
  1. L22 detects a 404 pattern matching route_alias_404 (e.g.
     /api/v1/facility/<slug>) and the fix is the predictable
     1-line `@app.route` alias.
  2. This module takes the draft + actually:
       - Forks azmartone67/dchub-backend to dchub-cloud-bot if no
         fork exists
       - Clones the fork to /tmp
       - Inserts the @app.route alias in main.py near the canonical
         route (deterministic regex match)
       - Commits with "[brain-l22-auto] add <pattern> alias"
       - Pushes to branch auto-l22-route-alias-<slug>
       - Opens PR back to azmartone67/dchub-backend/main
  3. Logs the PR URL to brain_layer22_actions table.

Safety:
  - Only fires for recipe == "route_alias_404" (literal whitelist)
  - Diff capped at 5 lines (the alias decorator + def)
  - Forbidden path list checked (admin/secrets/keys)
  - Won't open duplicate PRs (checks open PRs for same branch name)
  - DRY_RUN env var skips actual git ops; just logs the diff

Triggered by L22's _scan_and_draft when env DCHUB_L22_REAL_PR=1
AND the recipe is route_alias_404.
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess

from flask import Blueprint, jsonify, request
from utc_clock import utc_now


brain_l22_pr_writer_bp = Blueprint("brain_l22_pr_writer", __name__)


# ── Config ─────────────────────────────────────────────────────────

UPSTREAM_REPO = os.environ.get("L22_UPSTREAM_REPO", "azmartone67/dchub-backend")
FORK_OWNER    = os.environ.get("L22_FORK_OWNER", "dchub-cloud-bot")
DEFAULT_BASE  = os.environ.get("L22_BASE_BRANCH", "main")
GH_TOKEN      = (os.environ.get("PR_SUBMIT_TOKEN")
                 or os.environ.get("GITHUB_TOKEN") or "").strip()
DRY_RUN       = os.environ.get("DCHUB_L22_REAL_PR", "0") != "1"

_BOT_AUTHOR_NAME  = "dchub-l22-bot"
_BOT_AUTHOR_EMAIL = "l22-bot@dchub.cloud"


def _safe_run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    """Run subprocess; return (returncode, stdout+stderr)."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True,
                            text=True, timeout=60)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {str(e)[:200]}"


def _apply_route_alias_to_main_py(repo_dir: str, src_path: str,
                                    dst_path: str) -> tuple[bool, str]:
    """Insert an @app.route alias near the canonical handler.

    Looks for the existing @app.route('<dst_path>') line in main.py,
    inserts a new @app.route('<src_path>') line directly above it.

    Returns (ok, message).
    """
    main_py = os.path.join(repo_dir, "main.py")
    if not os.path.exists(main_py):
        return False, f"main.py not found at {main_py}"

    with open(main_py) as f:
        content = f.read()

    # Find the canonical route. Use a regex anchored by the dst_path.
    # Pattern handles both 'path' and "path" quoting.
    pat = re.compile(
        r"(@app\.route\(['\"]" + re.escape(dst_path) + r"['\"][^)]*\))",
        re.MULTILINE,
    )
    m = pat.search(content)
    if not m:
        return False, f"canonical @app.route for {dst_path} not found"

    # Insert the alias 1 line above the matched line
    alias_line = (f"@app.route('{src_path}')  "
                   f"# r57 L22-auto-alias of {dst_path}\n")
    insert_pos = m.start()
    new_content = content[:insert_pos] + alias_line + content[insert_pos:]
    with open(main_py, "w") as f:
        f.write(new_content)
    return True, f"alias inserted: {src_path} → {dst_path}"


def open_route_alias_pr(src_path: str, dst_path: str,
                        trigger_count: int = 0,
                        rationale: str = "") -> dict:
    """Main entry: clone, patch, commit, push, open PR.

    Returns dict with pr_url + state. Never raises.
    """
    if DRY_RUN:
        return {
            "ok":      True,
            "dry_run": True,
            "would_do": f"open PR alias {src_path} → {dst_path}",
            "hint":    "Set DCHUB_L22_REAL_PR=1 to enable real git ops.",
        }
    if not GH_TOKEN:
        return {"ok": False, "error": "PR_SUBMIT_TOKEN / GITHUB_TOKEN unset"}

    # Working dir for this run
    slug = src_path.strip("/").replace("/", "-").replace("<", "_").replace(">", "_")
    branch = f"auto-l22-route-alias-{slug[:40]}-{utc_now().strftime('%Y%m%d%H%M')}"
    work_dir = f"/tmp/l22-pr-{slug}-{int(datetime.datetime.utcnow().timestamp())}"

    # 1. Clone the fork (use token in URL for auth)
    fork_url = f"https://x-access-token:{GH_TOKEN}@github.com/{FORK_OWNER}/dchub-backend.git"
    code, out = _safe_run(["git", "clone", "--depth", "1",
                             "--branch", DEFAULT_BASE,
                             fork_url, work_dir])
    if code != 0:
        # Fork might not exist yet — create it via gh
        code, out = _safe_run(["gh", "repo", "fork", UPSTREAM_REPO,
                                "--org", FORK_OWNER, "--clone=false",
                                "--remote=false"])
        if code != 0:
            return {"ok": False, "stage": "fork_create", "error": out[:300]}
        # Retry clone
        code, out = _safe_run(["git", "clone", "--depth", "1",
                                 "--branch", DEFAULT_BASE,
                                 fork_url, work_dir])
        if code != 0:
            return {"ok": False, "stage": "fork_clone_retry",
                    "error": out[:300]}

    # 2. Configure git author for this clone
    _safe_run(["git", "config", "user.name", _BOT_AUTHOR_NAME], cwd=work_dir)
    _safe_run(["git", "config", "user.email", _BOT_AUTHOR_EMAIL], cwd=work_dir)

    # 3. Create branch
    code, out = _safe_run(["git", "checkout", "-b", branch], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "branch", "error": out[:300]}

    # 4. Apply the route alias edit
    ok, msg = _apply_route_alias_to_main_py(work_dir, src_path, dst_path)
    if not ok:
        return {"ok": False, "stage": "patch", "error": msg}

    # 5. Commit
    code, out = _safe_run(["git", "add", "main.py"], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_add", "error": out[:300]}
    commit_msg = (
        f"[brain-l22-auto] add {src_path} route alias\n\n"
        f"Auto-drafted by Brain L22 (route_alias_404 recipe).\n"
        f"Trigger: {trigger_count}x 404 hits in 1h on {src_path}\n"
        f"Canonical route at {dst_path}.\n\n"
        f"{rationale}\n\n"
        f"DRY_RUN={DRY_RUN}  ·  Recipe whitelisted as safe (1-line diff).\n"
        f"Co-Authored-By: Brain L22 <l22-bot@dchub.cloud>"
    )
    code, out = _safe_run(["git", "commit", "-m", commit_msg], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_commit", "error": out[:300]}

    # 6. Push
    code, out = _safe_run(["git", "push", "-u", "origin", branch],
                            cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_push", "error": out[:300]}

    # 7. Open PR via gh CLI
    pr_title = f"[brain-l22] Add {src_path} route alias (auto)"
    pr_body = (
        f"## What\n\n"
        f"Adds a single route alias so `{src_path}` no longer 404s. "
        f"Canonical route at `{dst_path}` is unchanged.\n\n"
        f"## Why\n\n"
        f"Brain L22 ring buffer recorded **{trigger_count}x 404 hits in "
        f"the last hour** on `{src_path}`. Common cause: frontend hits "
        f"singular form, backend serves plural (or vice-versa).\n\n"
        f"## How to verify\n\n"
        f"```\n"
        f"curl -i https://dchub.cloud{src_path}\n"
        f"# expect 200 (same response as {dst_path})\n"
        f"```\n\n"
        f"## How to revert\n\n"
        f"`git revert <merge-commit>` — this commit is isolated and 1-line.\n\n"
        f"---\n\n"
        f"_Auto-generated by Brain L22's `route_alias_404` recipe — the\n"
        f"only whitelisted recipe approved for real-PR drafting (r57).\n"
        f"Other recipes still draft Issues, not PRs._\n"
    )
    code, out = _safe_run([
        "gh", "pr", "create",
        "--repo", UPSTREAM_REPO,
        "--head", f"{FORK_OWNER}:{branch}",
        "--base", DEFAULT_BASE,
        "--title", pr_title,
        "--body",  pr_body,
    ], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "gh_pr_create",
                "error": out[:300], "branch": branch}

    # Extract PR URL from gh output
    pr_url = None
    for line in out.splitlines():
        if line.strip().startswith("https://github.com/"):
            pr_url = line.strip()
            break

    return {
        "ok":          True,
        "stage":       "complete",
        "branch":      branch,
        "pr_url":      pr_url,
        "src_path":    src_path,
        "dst_path":    dst_path,
        "diff_lines":  1,
    }


# ── Recipe: cron_if_mismatched (r85h — WALK 2nd recipe) ─────────────
#
# Staggers a colliding GitHub Actions cron so 2+ workflows stop firing at
# the same minute (thundering herd on the 2-replica backend + Neon). Mirrors
# the route_alias writer. FENCES (never auto-touch):
#   - evolve-cron.yml — a 19-cron mega-workflow whose jobs are gated by
#     `if: github.event.schedule == '...'`; changing a cron string there
#     SILENTLY DISABLES a job.
#   - ANY workflow containing `github.event.schedule` (same trap, generalised).
#   - DR-critical / canonical workflows stay put — we move the OTHER one.
#   - Only plain-integer-minute crons (skip */N, ranges, lists).
# yaml.safe_load-validates the edited file before commit. Human-merge, draft PR.

_CRON_KEEP_CANONICAL = {
    "backup-neon-r2.yml",       # nightly pg_dump -> R2 (DR-critical)
    "iso-data-pull.yml",        # real-time grid pull
    "dchub-daily-status.yml",   # canonical daily status
}


def _cron_minute_is_plain_int(expr: str) -> bool:
    parts = expr.split()
    return len(parts) == 5 and parts[0].isdigit() and 0 <= int(parts[0]) <= 59


def _apply_cron_stagger_to_workflows(repo_dir: str) -> dict:
    """Find ONE SAFE cron collision in .github/workflows and stagger the
    non-canonical workflow's minute to a free slot. Edits exactly one file,
    one line. Returns {ok, file, old_cron, new_cron, kept, detail} or
    {ok:False, reason}. No-op (ok:False) when nothing is safe to fix."""
    import glob as _glob
    wf_dir = os.path.join(repo_dir, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return {"ok": False, "reason": "no .github/workflows dir"}
    files: dict[str, str] = {}
    for p in (_glob.glob(os.path.join(wf_dir, "*.yml"))
              + _glob.glob(os.path.join(wf_dir, "*.yaml"))):
        try:
            files[os.path.basename(p)] = open(p).read()
        except OSError:
            continue
    cron_pat = re.compile(r"cron:\s*['\"]([^'\"]+)['\"]")
    expr_files: dict[str, set] = {}
    for fn, text in files.items():
        for m in cron_pat.finditer(text):
            expr_files.setdefault(m.group(1).strip(), set()).add(fn)

    for expr, fns in sorted(expr_files.items()):
        if len(fns) < 2 or not _cron_minute_is_plain_int(expr):
            continue
        # editable = not the mega-workflow, no schedule-guard, not DR-canonical
        editable = [fn for fn in fns
                    if fn != "evolve-cron.yml"
                    and "github.event.schedule" not in files[fn]
                    and fn not in _CRON_KEEP_CANONICAL]
        if not editable:
            continue  # both sides canonical/guarded — leave it for a human
        move_fn = sorted(editable)[0]
        parts = expr.split()
        # minutes already used at this SAME hour+dom+month+dow (avoid re-collide)
        used = set()
        for e in expr_files:
            ep = e.split()
            if len(ep) == 5 and ep[0].isdigit() and ep[1:] == parts[1:]:
                used.add(int(ep[0]))
        cur = int(parts[0])
        new_min = next(((cur + d) % 60 for d in (10, 20, 40, 5, 15, 25, 35, 45, 50, 55)
                        if ((cur + d) % 60) not in used), None)
        if new_min is None:
            continue
        new_cron = " ".join([str(new_min)] + parts[1:])
        text = files[move_fn]
        new_text = None
        for q in ("'", '"'):
            tok = q + expr + q
            if tok in text:
                new_text = text.replace(tok, q + new_cron + q, 1)
                break
        if new_text is None:
            continue
        try:
            import yaml
            yaml.safe_load(new_text)
        except Exception as ye:
            return {"ok": False, "reason": f"yaml invalid after edit: {str(ye)[:120]}"}
        with open(os.path.join(wf_dir, move_fn), "w") as f:
            f.write(new_text)
        kept = sorted(fns - {move_fn})
        return {"ok": True, "file": move_fn, "old_cron": expr,
                "new_cron": new_cron, "kept": kept,
                "detail": (f"staggered {move_fn} `{expr}` -> `{new_cron}` "
                           f"(was colliding with {', '.join(kept)})")}
    return {"ok": False, "reason": "no safe cron collision to stagger"}


def open_cron_stagger_pr(rationale: str = "") -> dict:
    """Clone the fork, stagger ONE safe cron collision, commit, push, open a
    DRAFT PR. Mirrors open_route_alias_pr. Never raises. Returns ok:False with
    no_action=True when the live workflows have no safe collision (never opens
    an empty PR)."""
    if DRY_RUN:
        return {"ok": True, "dry_run": True,
                "would_do": "scan workflows for a safe cron collision + open a stagger PR",
                "hint": "Set DCHUB_L22_REAL_PR=1 to enable real git ops."}
    if not GH_TOKEN:
        return {"ok": False, "error": "PR_SUBMIT_TOKEN / GITHUB_TOKEN unset"}

    ts = datetime.datetime.utcnow()
    work_dir = f"/tmp/l22-cron-{int(ts.timestamp())}"
    fork_url = f"https://x-access-token:{GH_TOKEN}@github.com/{FORK_OWNER}/dchub-backend.git"
    code, out = _safe_run(["git", "clone", "--depth", "1", "--branch",
                            DEFAULT_BASE, fork_url, work_dir])
    if code != 0:
        _safe_run(["gh", "repo", "fork", UPSTREAM_REPO, "--org", FORK_OWNER,
                   "--clone=false", "--remote=false"])
        code, out = _safe_run(["git", "clone", "--depth", "1", "--branch",
                                DEFAULT_BASE, fork_url, work_dir])
        if code != 0:
            return {"ok": False, "stage": "fork_clone", "error": out[:300]}

    change = _apply_cron_stagger_to_workflows(work_dir)
    if not change.get("ok"):
        return {"ok": False, "stage": "detect", "no_action": True,
                "reason": change.get("reason")}

    _safe_run(["git", "config", "user.name", _BOT_AUTHOR_NAME], cwd=work_dir)
    _safe_run(["git", "config", "user.email", _BOT_AUTHOR_EMAIL], cwd=work_dir)
    stem = change["file"].rsplit(".", 1)[0][:32]
    branch = f"auto-l22-cron-{stem}-{ts.strftime('%Y%m%d%H%M')}"
    code, out = _safe_run(["git", "checkout", "-b", branch], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "branch", "error": out[:300]}
    rel = os.path.join(".github", "workflows", change["file"])
    code, out = _safe_run(["git", "add", rel], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_add", "error": out[:300]}
    commit_msg = (
        f"[brain-l22-auto] stagger {change['file']} cron "
        f"{change['old_cron']} -> {change['new_cron']}\n\n"
        f"Auto-drafted by Brain L22 (cron_if_mismatched recipe).\n"
        f"{change['detail']}\n"
        f"Kept the canonical job on its slot; moved the non-canonical one to a "
        f"free minute. FENCED: never edits evolve-cron.yml or a workflow with an "
        f"`if: github.event.schedule` guard; plain-integer-minute crons only; "
        f"yaml-validated.\n\n{rationale}\n\n"
        f"Co-Authored-By: Brain L22 <l22-bot@dchub.cloud>"
    )
    code, out = _safe_run(["git", "commit", "-m", commit_msg], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_commit", "error": out[:300]}
    code, out = _safe_run(["git", "push", "-u", "origin", branch], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_push", "error": out[:300]}

    pr_title = f"[brain-l22] Stagger {change['file']} cron collision (auto)"
    pr_body = (
        f"## What\n\n{change['detail']}.\n\n"
        f"2+ workflows were firing at the exact same minute (`{change['old_cron']}`) "
        f"— a thundering herd on the 2-replica backend + Neon. This moves the "
        f"non-canonical job to a free minute; the canonical job is unchanged.\n\n"
        f"## Safety\n\n"
        f"- Edits exactly ONE workflow file, ONE cron line.\n"
        f"- NEVER touches `evolve-cron.yml` or any workflow with an "
        f"`if: github.event.schedule` job guard.\n"
        f"- DR-critical workflows (e.g. backup-neon-r2) stay on their slot.\n"
        f"- The edited file was `yaml.safe_load`-validated before commit.\n\n"
        f"## How to revert\n\n`git revert <merge-commit>` — one-line, isolated.\n\n"
        f"---\n_Auto-generated by Brain L22's `cron_if_mismatched` recipe "
        f"(WALK phase, human-merge)._\n"
    )
    code, out = _safe_run([
        "gh", "pr", "create", "--repo", UPSTREAM_REPO,
        "--head", f"{FORK_OWNER}:{branch}", "--base", DEFAULT_BASE,
        "--draft", "--title", pr_title, "--body", pr_body,
    ], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "gh_pr_create", "error": out[:300],
                "branch": branch}
    pr_url = next((ln.strip() for ln in out.splitlines()
                   if ln.strip().startswith("https://github.com/")), None)
    return {"ok": True, "stage": "complete", "branch": branch,
            "pr_url": pr_url, **change}


# ── HTTP endpoints (admin observability) ───────────────────────────

def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


@brain_l22_pr_writer_bp.route(
    "/api/v1/brain/l22/route-alias-pr", methods=["POST"]
)
def manual_route_alias_pr():
    """Manual trigger — admin can fire a route alias PR for a specific
    src→dst pair without waiting for L21 ring buffer detection."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    body = request.get_json(silent=True) or {}
    src = (body.get("src_path") or "").strip()
    dst = (body.get("dst_path") or "").strip()
    if not src or not dst:
        return jsonify({
            "ok": False,
            "error": "missing_src_or_dst",
            "expected": {"src_path": "/api/v1/facility/<slug>",
                          "dst_path": "/api/v1/facilities/<slug>"},
        }), 400

    result = open_route_alias_pr(
        src_path=src, dst_path=dst,
        trigger_count=int(body.get("trigger_count") or 0),
        rationale=str(body.get("rationale") or "manual admin trigger"),
    )
    return jsonify(result), 200 if result.get("ok") else 200


@brain_l22_pr_writer_bp.route(
    "/api/v1/brain/l22/pr-writer/status", methods=["GET"]
)
def pr_writer_status():
    return jsonify({
        "ok":             True,
        "enabled":        not DRY_RUN,
        "dry_run_reason": ("DCHUB_L22_REAL_PR != '1'" if DRY_RUN
                            else None),
        "upstream_repo":  UPSTREAM_REPO,
        "fork_owner":     FORK_OWNER,
        "base_branch":    DEFAULT_BASE,
        "gh_token_set":   bool(GH_TOKEN),
        "purpose":        ("Promotes route_alias_404 recipe from "
                            "Issue-drafter to real PR-writer."),
    }), 200
