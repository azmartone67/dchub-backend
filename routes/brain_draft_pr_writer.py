"""
brain_draft_pr_writer.py — PHASE 2 (2026-06-18).

The GENERIC "apply a mechanical proposal's search→replace as a DRAFT
GitHub PR" writer. This is the piece Phase 0 explicitly stubbed: L22's
real-PR path (routes/brain_layer22_pr_writer.py) is RECIPE-specific
(route_alias_404 inserts an @app.route line; cron_if_mismatched edits a
workflow cron) and has no way to apply an arbitrary single-hunk
search/replace. This module provides that generic writer.

SAFETY (this layer NEVER merges, NEVER pushes to main):
  · DRAFT PRs only (gh pr create --draft, base=main, head=the new branch).
  · Defense-in-depth: re-runs classify_mechanical(proposal) at APPLY time;
    a non-mechanical proposal is refused even when called directly.
  · The exactly-once check is re-run against LIVE main (fetched from
    GitHub), not the proposal's snapshot — a stale/drifted proposal aborts.
  · dry_run=True is the DEFAULT and has ZERO GitHub side effects.
  · Real opening additionally requires DCHUB_L22_REAL_PR==1 AND a token.
  · Idempotent: a proposal already carrying pr_url / status='pr_opened'
    is skipped.
  · Rate cap MAX_DRAFT_PRS_PER_RUN (env, default 5) bounds a single run.
  · Every GitHub call is wrapped so one proposal's failure aborts THAT
    proposal cleanly without touching the others.

REUSE (one GitHub client only — do NOT add a second):
  All token handling, repo/owner/fork/base config, and the git-CLI
  mechanics come from routes/brain_layer22_pr_writer:
    GH_TOKEN  = PR_SUBMIT_TOKEN or GITHUB_TOKEN
    UPSTREAM_REPO / FORK_OWNER / DEFAULT_BASE / _safe_run
    _BOT_AUTHOR_NAME / _BOT_AUTHOR_EMAIL
  The branch/commit/push/PR flow mirrors open_cron_stagger_pr (which
  already opens a `gh pr create --draft` PR via the fork). The one helper
  L22 doesn't expose — reading a file's CURRENT content off main — is a
  thin read-only GitHub Contents-API GET that uses the SAME token.

The classifier (routes/brain_mechanical_classifier) owns the gate logic
and the forbidden-path / allowlist constants; we import + reuse
classify_mechanical and _extract_single_change rather than re-deriving.

Heavy imports (flask, the classifier's flask-importing module, requests)
are LAZY so this module stays importable in a no-flask test environment
where every GitHub primitive is monkeypatched.
"""
from __future__ import annotations

import os


# ── Rate cap (env-driven) ────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


MAX_DRAFT_PRS_PER_RUN = _env_int("MAX_DRAFT_PRS_PER_RUN", 5)


# ── L22 config / token / git-CLI reuse (single GitHub client) ────────
# Imported lazily inside _l22() so the module imports cleanly without the
# flask chain (brain_layer22_pr_writer defines a Blueprint at import time).
# Tests monkeypatch the module-level wrappers below, so they never hit this.
def _l22():
    from routes import brain_layer22_pr_writer as l22
    return l22


def _gh_config() -> dict:
    """Resolve the SAME token / repo / fork / base config L22 uses. Reading
    them live (not at import) so env set after import is honored, and so a
    test can monkeypatch the L22 module attributes."""
    l22 = _l22()
    return {
        "token": getattr(l22, "GH_TOKEN", "") or "",
        "upstream": getattr(l22, "UPSTREAM_REPO", "azmartone67/dchub-backend"),
        "fork_owner": getattr(l22, "FORK_OWNER", "dchub-cloud-bot"),
        "base": getattr(l22, "DEFAULT_BASE", "main"),
        "author_name": getattr(l22, "_BOT_AUTHOR_NAME", "dchub-l22-bot"),
        "author_email": getattr(l22, "_BOT_AUTHOR_EMAIL", "l22-bot@dchub.cloud"),
    }


# ── GitHub primitive 1: read a file's CURRENT content off MAIN ───────
# This is the only GitHub primitive L22 doesn't already expose. It is a
# READ-ONLY Contents-API GET against the upstream repo's base branch, using
# the SAME token. Tests monkeypatch this whole function.
def get_file_on_main(file_path: str) -> dict:
    """Fetch {ok, content, sha, head_sha} for `file_path` from the upstream
    repo's base branch (main). Read-only, no fork, no write. Returns
    {ok:False, error} on any failure so the caller can abort cleanly."""
    cfg = _gh_config()
    token = cfg["token"]
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import base64
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps_unavailable: {e}"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = cfg["base"]
    try:
        # File content + blob sha (at ref=base).
        rc = requests.get(
            f"https://api.github.com/repos/{cfg['upstream']}/contents/{file_path}",
            headers=headers, params={"ref": base}, timeout=15,
        )
        if rc.status_code != 200:
            return {"ok": False, "error": f"contents_{rc.status_code}: {rc.text[:160]}"}
        j = rc.json() or {}
        raw = base64.b64decode(j.get("content", "") or "").decode("utf-8", "replace")
        blob_sha = j.get("sha")
        # HEAD commit sha of base (for the diff_preview / branch-off point).
        rb = requests.get(
            f"https://api.github.com/repos/{cfg['upstream']}/branches/{base}",
            headers=headers, timeout=15,
        )
        head_sha = None
        if rb.status_code == 200:
            head_sha = ((rb.json() or {}).get("commit") or {}).get("sha")
        return {"ok": True, "content": raw, "sha": blob_sha, "head_sha": head_sha}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ── GitHub primitive 2: open a DRAFT PR applying new file content ────
# Mirrors open_cron_stagger_pr's fork flow (clone fork → branch → write
# file → commit → push → `gh pr create --draft`). Uses L22's _safe_run +
# token + fork config exclusively. Tests monkeypatch this whole function.
def open_draft_pr_with_content(*, file_path: str, new_content: str,
                               branch: str, title: str, body: str,
                               commit_msg: str) -> dict:
    """Open a DRAFT PR that replaces `file_path` with `new_content` on a new
    branch off the fork, PR'd into upstream/main. NEVER commits to main.
    Returns {ok, pr_url, branch} or {ok:False, stage, error}. Never raises."""
    cfg = _gh_config()
    token = cfg["token"]
    if not token:
        return {"ok": False, "error": "PR_SUBMIT_TOKEN / GITHUB_TOKEN unset"}

    l22 = _l22()
    run = l22._safe_run  # the SAME subprocess runner L22 uses
    import datetime as _dt

    work_dir = f"/tmp/l22-mech-{int(_dt.datetime.utcnow().timestamp())}"
    upstream_url = (f"https://x-access-token:{token}@github.com/"
                    f"{cfg['upstream']}.git")

    # 1. Clone the UPSTREAM base branch directly. L22's WORKING PRs (#1179+) push
    #    branches straight to upstream as azmartone67:<branch>; the dchub-cloud-bot
    #    FORK does not exist (404), so the fork path always aborted at fork_clone.
    #    We branch off upstream + open the PR from the upstream branch. SAFE: only
    #    a feature branch is ever created/pushed here — NEVER main.
    code, out = run(["git", "clone", "--depth", "1", "--branch", cfg["base"],
                     upstream_url, work_dir])
    if code != 0:
        return {"ok": False, "stage": "upstream_clone", "error": out[:300]}

    # 2. Author + branch (NEVER main — base stays main, head is THIS branch).
    run(["git", "config", "user.name", cfg["author_name"]], cwd=work_dir)
    run(["git", "config", "user.email", cfg["author_email"]], cwd=work_dir)
    code, out = run(["git", "checkout", "-b", branch], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "branch", "error": out[:300]}

    # 3. Write the new file content (full-file replace; new_content already
    #    has the single search→replace applied by the caller).
    try:
        target = os.path.join(work_dir, file_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return {"ok": False, "stage": "write_file", "error": str(e)[:200]}

    # 4. Stage + commit + push.
    code, out = run(["git", "add", file_path], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_add", "error": out[:300]}
    code, out = run(["git", "commit", "-m", commit_msg], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_commit", "error": out[:300]}
    code, out = run(["git", "push", "-u", "origin", branch], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "git_push", "error": out[:300]}

    # 5. Open the DRAFT PR. base=main (cfg base), head=the upstream branch.
    #    --draft is ALWAYS passed — invariant enforced here AND asserted by tests.
    code, out = run([
        "gh", "pr", "create", "--repo", cfg["upstream"],
        "--head", branch, "--base", cfg["base"],
        "--draft", "--title", title, "--body", body,
    ], cwd=work_dir)
    if code != 0:
        return {"ok": False, "stage": "gh_pr_create", "error": out[:300],
                "branch": branch}
    pr_url = next((ln.strip() for ln in (out or "").splitlines()
                   if ln.strip().startswith("https://github.com/")), None)
    return {"ok": True, "branch": branch, "pr_url": pr_url}


# ── DB: mark a proposal pr_opened (mirrors layer5's pr_url update) ───
def _mark_pr_opened(proposal_id, pr_url: str) -> bool:
    """UPDATE brain_proposed_code_fixes SET status='pr_opened', pr_url=...
    Mirrors the reconcile/pr_url pattern in brain_v2_layer5. Best-effort;
    returns True on a committed update. Tests monkeypatch this."""
    try:
        import psycopg2
    except Exception:
        return False
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("ALTER TABLE brain_proposed_code_fixes "
                                "ADD COLUMN IF NOT EXISTS pr_url TEXT;")
                    conn.commit()
                except Exception:
                    conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE brain_proposed_code_fixes "
                    "SET status = 'pr_opened', pr_url = %s, reviewed_at = NOW() "
                    "WHERE id = %s",
                    (pr_url, proposal_id),
                )
            conn.commit()
        return True
    except Exception:
        return False


# ── Helpers ──────────────────────────────────────────────────────────
def _short_hash(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:8]


def _diff_preview(content: str, search_text: str, replace_text: str,
                  ctx: int = 2) -> list[str]:
    """A few context lines around the single replacement site, as a
    unified-ish preview (no GitHub side effects). Pure / read-only."""
    idx = content.find(search_text)
    if idx < 0:
        return []
    pre = content[:idx]
    start_line = pre.count("\n")
    lines = content.split("\n")
    s_first = start_line
    s_last = start_line + search_text.count("\n")
    lo = max(0, s_first - ctx)
    hi = min(len(lines), s_last + 1 + ctx)
    out = []
    for i in range(lo, hi):
        if s_first <= i <= s_last:
            out.append(f"- {lines[i]}")
        else:
            out.append(f"  {lines[i]}")
    # Show the replacement side as added lines (search→replace applied).
    for rl in (replace_text or "").split("\n"):
        out.append(f"+ {rl}")
    return out[:40]


def _already_opened(proposal: dict) -> bool:
    """Idempotency gate: skip if the proposal already has a pr_url or its
    status is already pr_opened."""
    if (proposal.get("pr_url") or "").strip():
        return True
    if (proposal.get("status") or "").strip() == "pr_opened":
        return True
    return False


# ── The generic writer ───────────────────────────────────────────────
def open_mechanical_draft_pr(proposal: dict, dry_run: bool = True) -> dict:
    """Apply ONE mechanical proposal's single search→replace as a DRAFT PR.

    dry_run=True (DEFAULT): zero GitHub side effects — returns
      {ok, would_open, branch, file_path, klass, diff_preview, head_sha}.
    dry_run=False: requires DCHUB_L22_REAL_PR==1 AND a token; opens a DRAFT
      PR (base=main, head=new branch), then marks the row pr_opened.

    Conservative — every gate that fails returns {ok:False, ...reason} and
    touches nothing. Never raises through to the caller for a GitHub error."""
    # ── Gate 0: idempotency — already has a PR. ──────────────────────
    if _already_opened(proposal):
        return {"ok": True, "skipped": True,
                "reason": "already_pr_opened",
                "id": proposal.get("id"),
                "pr_url": (proposal.get("pr_url") or None)}

    # ── Gate 1: DEFENSE-IN-DEPTH — re-classify at apply time. ────────
    # Import lazily so this module loads without flask in tests.
    from routes.brain_mechanical_classifier import (
        classify_mechanical, _extract_single_change,
    )
    verdict = classify_mechanical(proposal)
    if not verdict.get("is_mechanical"):
        return {"ok": False, "aborted": True,
                "reason": "not_mechanical",
                "blocked_by": verdict.get("blocked_by", []),
                "id": proposal.get("id")}
    klass = verdict.get("klass")

    file_path, search_text, replace_text, _multi = _extract_single_change(proposal)
    file_path = (file_path or "").strip()
    search_text = search_text or ""
    replace_text = replace_text or ""
    if not file_path or not search_text:
        return {"ok": False, "aborted": True, "reason": "missing_file_or_search",
                "id": proposal.get("id")}

    # ── Gate 2: re-verify search occurs EXACTLY ONCE in LIVE main. ───
    # The proposal may be stale; classify against REAL main, not the snapshot.
    fetched = get_file_on_main(file_path)
    if not fetched.get("ok"):
        return {"ok": False, "aborted": True,
                "reason": "fetch_main_failed:" + str(fetched.get("error")),
                "id": proposal.get("id")}
    content = fetched.get("content") or ""
    occ = content.count(search_text)
    if occ == 0:
        return {"ok": False, "aborted": True,
                "reason": "search_text_absent_in_main",
                "id": proposal.get("id")}
    if occ > 1:
        return {"ok": False, "aborted": True,
                "reason": f"search_text_ambiguous_in_main ({occ}x)",
                "id": proposal.get("id")}

    # ── Gate 3: compute the new content (single occurrence). ─────────
    new_content = content.replace(search_text, replace_text, 1)
    if new_content == content:
        return {"ok": False, "aborted": True, "reason": "noop_replace",
                "id": proposal.get("id")}

    head_sha = fetched.get("head_sha")
    branch = (f"brain/autofix-{klass}-{proposal.get('id')}-"
              f"{_short_hash(search_text + replace_text)}")
    preview = _diff_preview(content, search_text, replace_text)

    # ── dry_run (DEFAULT): no side effects. ──────────────────────────
    if dry_run:
        return {
            "ok": True, "would_open": True,
            "id": proposal.get("id"),
            "klass": klass,
            "branch": branch,
            "file_path": file_path,
            "head_sha": head_sha,
            "diff_preview": preview,
        }

    # ── Real open: require the live flag + a token. ──────────────────
    real_pr = os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"
    token = _gh_config()["token"]
    if not real_pr or not token:
        return {"ok": True, "skipped": True,
                "reason": ("DCHUB_L22_REAL_PR != 1" if not real_pr
                           else "no_token"),
                "id": proposal.get("id"),
                "branch": branch, "would_open": True,
                "diff_preview": preview}

    rationale = (proposal.get("rationale") or "").strip()
    title = f"[brain-autofix:{klass}] {file_path} (proposal #{proposal.get('id')})"
    commit_msg = (
        f"[brain-autofix] {klass}: {file_path}\n\n"
        f"Mechanical single-hunk search→replace from brain proposal "
        f"#{proposal.get('id')} (loop {proposal.get('loop_name') or '?'}).\n\n"
        f"Rationale: {rationale or '(none provided)'}\n\n"
        f"🤖 mechanical autofix — DRAFT, human review required\n"
        f"Co-Authored-By: Brain mechanical autofix <l22-bot@dchub.cloud>"
    )
    body = (
        f"## What\n\n"
        f"Applies a single mechanical search→replace to `{file_path}` "
        f"(transform class `{klass}`).\n\n"
        f"## Why\n\n{rationale or '(no rationale recorded on the proposal)'}\n\n"
        f"## Provenance\n\n"
        f"Auto-drafted from brain proposal **#{proposal.get('id')}** "
        f"(loop `{proposal.get('loop_name') or '?'}`, "
        f"confidence `{proposal.get('confidence')}`). Re-classified as "
        f"mechanical at apply-time and re-verified to occur exactly once on "
        f"`main` (HEAD `{head_sha}`).\n\n"
        f"## Diff preview\n\n```diff\n" + "\n".join(preview) + "\n```\n\n"
        f"## How to revert\n\n`git revert <merge-commit>` — single-hunk, isolated.\n\n"
        f"---\n"
        f"> [!IMPORTANT]\n"
        f"> 🤖 mechanical autofix — **DRAFT, human review required**. This PR "
        f"is opened in DRAFT against `main`; it is NEVER auto-merged.\n"
    )

    # ── Wrap the GitHub write so a failure aborts THIS proposal only. ─
    try:
        res = open_draft_pr_with_content(
            file_path=file_path, new_content=new_content, branch=branch,
            title=title, body=body, commit_msg=commit_msg,
        )
    except Exception as e:
        return {"ok": False, "aborted": True,
                "reason": f"pr_open_exception: {type(e).__name__}: {str(e)[:160]}",
                "id": proposal.get("id"), "branch": branch}
    if not res.get("ok"):
        return {"ok": False, "aborted": True,
                "reason": "pr_open_failed:" + str(res.get("stage") or res.get("error")),
                "id": proposal.get("id"), "branch": branch}

    pr_url = res.get("pr_url")
    db_ok = False
    try:
        db_ok = _mark_pr_opened(proposal.get("id"), pr_url or "")
    except Exception:
        db_ok = False

    return {"ok": True, "pr_url": pr_url, "branch": res.get("branch") or branch,
            "id": proposal.get("id"), "klass": klass, "status_updated": db_ok}


# ── Batch driver (used by the endpoint) ──────────────────────────────
def open_mechanical_draft_prs(rows, *, apply: bool,
                              max_prs: int | None = None) -> dict:
    """Classify open proposals, take the mechanical ones (respecting the rate
    cap), and run open_mechanical_draft_pr on each. apply=False => dry_run
    everywhere (no side effects). Returns
      {dry_run, opened:[...], previewed:[...], skipped:[...]}.
    One bad proposal never breaks the others."""
    from routes.brain_mechanical_classifier import classify_mechanical

    cap = MAX_DRAFT_PRS_PER_RUN if max_prs is None else int(max_prs)
    opened, previewed, skipped = [], [], []
    used = 0
    for row in (rows or []):
        try:
            verdict = classify_mechanical(row)
        except Exception as e:
            skipped.append({"id": row.get("id"),
                            "reason": f"classify_error: {str(e)[:120]}"})
            continue
        if not verdict.get("is_mechanical"):
            skipped.append({"id": row.get("id"), "reason": "not_mechanical",
                            "blocked_by": verdict.get("blocked_by", [])[:4]})
            continue
        if _already_opened(row):
            skipped.append({"id": row.get("id"), "reason": "already_pr_opened"})
            continue
        if used >= cap:
            skipped.append({"id": row.get("id"),
                            "reason": f"rate_cap_reached ({cap})"})
            continue
        used += 1
        try:
            res = open_mechanical_draft_pr(row, dry_run=(not apply))
        except Exception as e:
            skipped.append({"id": row.get("id"),
                            "reason": f"writer_exception: {str(e)[:120]}"})
            continue
        if not res.get("ok"):
            skipped.append({"id": row.get("id"),
                            "reason": res.get("reason", "aborted")})
        elif res.get("skipped"):
            skipped.append({"id": row.get("id"),
                            "reason": res.get("reason", "skipped")})
        elif apply and res.get("pr_url"):
            opened.append({"id": row.get("id"), "pr_url": res.get("pr_url"),
                           "branch": res.get("branch")})
        else:
            previewed.append({"id": row.get("id"), "branch": res.get("branch"),
                              "file": res.get("file_path"),
                              "klass": res.get("klass"),
                              "diff_preview": res.get("diff_preview", [])})
    return {"dry_run": not apply, "rate_cap": cap,
            "opened": opened, "previewed": previewed, "skipped": skipped}
