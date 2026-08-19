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
from routes._swallowed_writes import note_swallowed_write


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

    # REST-API flow (NO git/gh CLI). The Railway runtime's git-CLI path fails
    # (clone aborts) and the real auto-PRs come from GitHub Actions, not the app —
    # but the GitHub REST API works here (get_file_on_main already reads via it).
    # Steps: base HEAD sha → create branch ref → PUT new content on the branch
    # (Contents API) → open a DRAFT PR (base=main, head=branch). NEVER commits to
    # main, NEVER merges. draft=True is hardcoded.
    try:
        import base64 as _b64
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "stage": "deps", "error": f"deps_unavailable: {e}"}
    repo, base = cfg["upstream"], cfg["base"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{repo}"
    try:
        # 1. base (main) HEAD sha to branch off.
        r = requests.get(f"{api}/git/ref/heads/{base}", headers=headers, timeout=15)
        if r.status_code != 200:
            return {"ok": False, "stage": "get_base_ref",
                    "error": f"{r.status_code}: {r.text[:160]}"}
        head_sha = ((r.json() or {}).get("object") or {}).get("sha")
        if not head_sha:
            return {"ok": False, "stage": "get_base_ref", "error": "no_sha"}
        # 2. current blob sha of the file (Contents PUT requires it to update).
        rf = requests.get(f"{api}/contents/{file_path}", headers=headers,
                          params={"ref": base}, timeout=15)
        if rf.status_code != 200:
            return {"ok": False, "stage": "get_file_sha",
                    "error": f"{rf.status_code}: {rf.text[:160]}"}
        file_sha = (rf.json() or {}).get("sha")
        # 3. create the branch ref (refs/heads/<branch> — NEVER main).
        rc = requests.post(f"{api}/git/refs", headers=headers, timeout=15,
                           json={"ref": f"refs/heads/{branch}", "sha": head_sha})
        if rc.status_code not in (200, 201):
            return {"ok": False, "stage": "create_branch",
                    "error": f"{rc.status_code}: {rc.text[:200]}", "branch": branch}
        # 4. commit new content onto the branch (Contents API).
        rp = requests.put(
            f"{api}/contents/{file_path}", headers=headers, timeout=20,
            json={"message": commit_msg,
                  "content": _b64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                  "branch": branch, "sha": file_sha})
        if rp.status_code not in (200, 201):
            return {"ok": False, "stage": "commit_content",
                    "error": f"{rp.status_code}: {rp.text[:200]}", "branch": branch}
        # 5. open the DRAFT PR. base=main, head=branch, draft=True (invariant).
        rpr = requests.post(
            f"{api}/pulls", headers=headers, timeout=20,
            json={"title": title, "head": branch, "base": base,
                  "body": body, "draft": True})
        if rpr.status_code not in (200, 201):
            return {"ok": False, "stage": "create_pr",
                    "error": f"{rpr.status_code}: {rpr.text[:200]}", "branch": branch}
        return {"ok": True, "branch": branch,
                "pr_url": (rpr.json() or {}).get("html_url")}
    except Exception as e:
        return {"ok": False, "stage": "api_exception",
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


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
        note_swallowed_write("brain_proposed_code_fixes", where="brain_draft_pr_writer._mark_pr_opened")
        return False


# ── Phase-3 hook: log the proposal behind a PR so auto-merge can re-verify ──
# Phase 3 (routes/brain_automerge.run_automerge) must, before merging an
# autofix PR, RE-CONFIRM the change is still a single-file mechanical
# search→replace in an allowlisted class. The PR diff alone doesn't carry the
# proposal's confidence / changes_json, so we persist the proposal metadata
# keyed by the PR's head BRANCH (which is deterministic:
# brain/autofix-<klass>-<id>-<hash>). Phase 3 reads it back via
# get_logged_proposal_for_branch(). Best-effort; a failure here never blocks
# opening the DRAFT PR (Phase 3 simply fail-closes and skips that PR).
def log_proposal_for_automerge(*, branch: str, file_path: str,
                               search_text: str, replace_text: str,
                               klass: str, proposal: dict) -> bool:
    """Upsert the proposal behind an autofix PR into brain_automerge_proposals
    keyed by branch. Returns committed. Tests monkeypatch this."""
    try:
        import psycopg2
    except Exception:
        return False
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_automerge_proposals (
                    branch        TEXT PRIMARY KEY,
                    proposal_id   BIGINT,
                    file_path     TEXT,
                    search_text   TEXT,
                    replace_text  TEXT,
                    klass         TEXT,
                    confidence    REAL,
                    changes_json  JSONB,
                    logged_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            try:
                conf = float(proposal.get("confidence") or 0.0)
            except Exception:
                conf = 0.0
            cj = proposal.get("changes_json")
            if not isinstance(cj, str):
                try:
                    cj = json.dumps(cj) if cj is not None else None
                except Exception:
                    cj = None
            cur.execute(
                "INSERT INTO brain_automerge_proposals "
                "(branch, proposal_id, file_path, search_text, replace_text, "
                " klass, confidence, changes_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (branch) DO UPDATE SET "
                "  proposal_id=EXCLUDED.proposal_id, file_path=EXCLUDED.file_path, "
                "  search_text=EXCLUDED.search_text, replace_text=EXCLUDED.replace_text, "
                "  klass=EXCLUDED.klass, confidence=EXCLUDED.confidence, "
                "  changes_json=EXCLUDED.changes_json, logged_at=NOW()",
                (branch, proposal.get("id"), file_path, search_text, replace_text,
                 klass, conf, cj),
            )
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_automerge_proposals", where="brain_draft_pr_writer.log_proposal_for_automerge")
        return False


def get_logged_proposal_for_branch(branch: str) -> dict | None:
    """Read back the proposal logged for an autofix PR branch (the Phase-3
    re-verify source of truth). Returns a proposal-shaped dict
    (id/file_path/search_text/replace_text/confidence/changes_json/status) or
    None if absent. Tests monkeypatch this."""
    if not branch:
        return None
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return None
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT proposal_id AS id, file_path, search_text, "
                    "       replace_text, klass, confidence, changes_json "
                    "  FROM brain_automerge_proposals WHERE branch = %s",
                    (branch,),
                )
                row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["status"] = "pr_opened"   # classify treats this as the live proposal
        return d
    except Exception:
        return None


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
    """Idempotency gate (PER-PROPOSAL): skip if THIS proposal already has a
    pr_url or its status is already pr_opened."""
    if (proposal.get("pr_url") or "").strip():
        return True
    if (proposal.get("status") or "").strip() == "pr_opened":
        return True
    return False


# ── CROSS-PROPOSAL dedup: another open autofix PR already owns this file+class ─
# _already_opened only catches THE SAME proposal twice. The L15-flood pattern is
# DIFFERENT proposals (#183 + #184) for the SAME (file_path, klass) BOTH opening
# a PR. brain_proposed_code_fixes has no klass column (the transform class is
# derived at classify-time), so we fetch the small set of OTHER rows that are
# already status='pr_opened' with a non-null pr_url on the SAME file_path, then
# re-derive each one's klass in-process and compare. Pure DB + local classify —
# NO GitHub API. Guarded/best-effort: any failure returns None (treated as "no
# existing PR" so we never wrongly block a legitimate distinct fix). Crucially
# this only matches SAME file AND SAME class — a different file OR a different
# class yields no match and still opens.
def _pr_is_open(pr_url: str):
    """True if the PR at pr_url is still OPEN, False if closed or merged, None if
    indeterminate (no token / network error / unparseable). Guarded; never raises.
    Lets dedup ignore a STALE 'pr_opened' DB row whose PR has since closed or merged,
    so a resolved PR can't permanently block a legit new fix to that file+class."""
    try:
        import re as _re
        import requests as _rq
        m = _re.search(r"/pull/(\d+)", pr_url or "")
        if not m:
            return None
        cfg = _gh_config()
        token, repo = cfg.get("token") or "", cfg.get("upstream") or ""
        if not token or not repo:
            return None
        resp = _rq.get(
            f"https://api.github.com/repos/{repo}/pulls/{m.group(1)}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("state") == "open"
    except Exception:
        return None


def find_open_pr_for_file_class(*, file_path: str, klass: str,
                                exclude_id=None) -> dict | None:
    """Return {id, pr_url} of ANOTHER proposal that already has an OPEN draft PR
    for the SAME (file_path, klass), or None. Best-effort; never raises."""
    file_path = (file_path or "").strip()
    klass = (klass or "").strip()
    if not file_path or not klass:
        return None
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return None
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        rows = []
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Cheap candidate set: SAME file, already PR-opened, real pr_url.
                cur.execute(
                    "SELECT id, loop_name, file_path, search_text, replace_text, "
                    "       changes_json, pr_url "
                    "  FROM brain_proposed_code_fixes "
                    " WHERE file_path = %s "
                    "   AND status = 'pr_opened' "
                    "   AND pr_url IS NOT NULL AND pr_url <> '' "
                    " LIMIT 200",
                    (file_path,),
                )
                rows = cur.fetchall() or []
    except Exception:
        return None
    # Re-derive each candidate's transform class in-process (no API) and match.
    try:
        from routes.brain_mechanical_classifier import classify_mechanical
    except Exception:
        return None
    for r in rows:
        try:
            rid = r.get("id")
            if exclude_id is not None and rid == exclude_id:
                continue
            verdict = classify_mechanical(dict(r))
            if verdict.get("klass") == klass:
                # Verify the blocking PR is ACTUALLY still open. A stale
                # 'pr_opened' row from a closed or merged PR must NOT permanently
                # block a legit new fix. Indeterminate (None) stays conservative
                # (treat as an open dup so we never double-open under flood).
                if _pr_is_open(r.get("pr_url")) is False:
                    continue
                return {"id": rid, "pr_url": r.get("pr_url")}
        except Exception:
            continue
    return None


def _mark_dup_skipped(proposal_id, dup_of_pr_url: str = "",
                      dup_of_id=None) -> bool:
    """UPDATE brain_proposed_code_fixes SET status='dup_skipped' so a proposal
    whose (file_path, klass) is already owned by another OPEN draft PR is not
    retried every tick. Best-effort; returns True on a committed update. Never
    raises. Tests monkeypatch this."""
    try:
        import psycopg2
    except Exception:
        return False
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return False
    note = "dup of open autofix PR for same file+class"
    if dup_of_id is not None:
        note += f" (proposal #{dup_of_id})"
    if (dup_of_pr_url or "").strip():
        note += f" {dup_of_pr_url}"
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE brain_proposed_code_fixes "
                    "SET status = 'dup_skipped', reviewed_at = NOW(), "
                    "    reviewer_note = %s "
                    "WHERE id = %s",
                    (note, proposal_id),
                )
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_proposed_code_fixes", where="brain_draft_pr_writer._mark_dup_skipped")
        return False


# ── The generic writer ───────────────────────────────────────────────
def parse_gate(file_path: str, old_content: str, new_content: str) -> str | None:
    """Return a refusal reason if the REPLACEMENT breaks the file, else None.

    ★ WHY THIS EXISTS — 2026-08-19. Three brain-authored PRs (#2915/#2916/#2917)
    merged and left `main` UN-PARSEABLE:

        ai_wars_automation.py       SyntaxError: unterminated string literal
        routes/mcp_funnel.py        IndentationError: unindent does not match…
        routes/monthly_outreach.py  IndentationError: unexpected indent

    Every existing gate passed, because every existing gate describes the
    SEARCH side or the SHAPE of the change: single hunk, <= N lines, search
    occurs exactly once in live main, no new control-flow token, allowlisted
    transform class, confidence. ★ NOTHING described the OUTPUT. We computed
    `content.replace(...)` and opened a PR without ever compiling the result.

    A replacement can satisfy all of those and still splice a string literal in
    half or land at the wrong indent — which is exactly what happened. So this
    gate asserts the one property that actually matters: **the file still
    parses**.

    ★ Fails CLOSED on a broken result, but NOT on a file that was already
    broken before we touched it — otherwise a pre-existing syntax error
    anywhere would permanently block every fix to that file, including the one
    that repairs it. Non-Python files are passed through (we have no parser for
    them); the caller's other gates still apply."""
    if not (file_path or "").endswith(".py"):
        return None
    import ast
    try:
        ast.parse(old_content)
    except SyntaxError:
        # The file did not parse BEFORE our edit — not our regression to judge.
        return None
    except Exception:
        return None
    try:
        ast.parse(new_content)
    except SyntaxError as e:
        return (f"replacement_breaks_syntax: {type(e).__name__}: "
                f"{str(e.msg)[:80]} at line {e.lineno}")
    except Exception as e:  # noqa: BLE001 — never crash a tick on a weird file
        return f"replacement_unparseable: {type(e).__name__}"
    return None


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

    # ── Gate 1a2: QUARANTINE-CONSUME — the janitor (brain_pr_janitor) quarantines
    # a (file, klass) recipe whose autofix PR went RED so it is NOT re-proposed.
    # Until now NOTHING read that status, so the recipe re-opened a PR every tick.
    # Consume it here: if this (file_path, klass) is quarantined, skip BEFORE any
    # GitHub call (runs in dry_run too). Best-effort — is_recipe_quarantined()
    # returns False on any DB error, so a transient DB problem can never wrongly
    # block a legitimate fix. A DIFFERENT class on the same file is unaffected.
    try:
        from routes.brain_fix_outcome_verify import is_recipe_quarantined
        if is_recipe_quarantined(file_path, klass):
            return {"ok": True, "skipped": True, "reason": "quarantined",
                    "id": proposal.get("id"),
                    "klass": klass, "file_path": file_path}
    except Exception:
        pass

    # ── Gate 1b: CROSS-PROPOSAL dedup — another OPEN autofix PR already owns
    # this (file_path, klass). Without this, two DIFFERENT proposals for the
    # same file+class (e.g. #183 + #184 on db_utils.py) BOTH open a draft PR and
    # the queue piles up (the L15-flood pattern). We mark THIS proposal
    # 'dup_skipped' (so it is not retried) and open nothing. Best-effort: a DB
    # error returns None => no dup => we fall through and open normally, and a
    # DIFFERENT file OR class never matches, so legitimately-distinct fixes still
    # open. (No GitHub side effects here — runs in dry_run too.)
    try:
        existing = find_open_pr_for_file_class(
            file_path=file_path, klass=klass, exclude_id=proposal.get("id"))
    except Exception:
        existing = None
    if existing:
        try:
            _mark_dup_skipped(proposal.get("id"),
                              dup_of_pr_url=existing.get("pr_url") or "",
                              dup_of_id=existing.get("id"))
        except Exception:
            pass
        return {"ok": True, "skipped": True,
                "reason": "dup_open_pr_same_file_class",
                "id": proposal.get("id"),
                "klass": klass, "file_path": file_path,
                "dup_of_id": existing.get("id"),
                "dup_of_pr_url": existing.get("pr_url")}

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

    # ── Gate 3b: the result must still PARSE. See parse_gate's docstring —
    # every other gate describes the search side or the shape of the change;
    # this is the only one that looks at the output. Runs in dry_run too, so
    # the shadow surface never previews a change that cannot compile.
    _broken = parse_gate(file_path, content, new_content)
    if _broken:
        return {"ok": False, "aborted": True, "reason": _broken,
                "id": proposal.get("id"), "file_path": file_path}

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

    # PHASE-3 HOOK: persist the proposal metadata keyed by branch so the
    # autonomous-merge pass can RE-VERIFY (re-classify) this change before it
    # ever merges. Best-effort — failure here never affects the opened PR.
    try:
        log_proposal_for_automerge(
            branch=res.get("branch") or branch, file_path=file_path,
            search_text=search_text, replace_text=replace_text,
            klass=klass, proposal=proposal,
        )
    except Exception:
        pass

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
    # CROSS-PROPOSAL dedup WITHIN this run: once we open/preview a PR for a
    # (file_path, klass) pair, a LATER row for the SAME pair is skipped. The DB
    # check in open_mechanical_draft_pr() only catches PRIOR-run open PRs; two
    # fresh same-file+class proposals in ONE batch (neither yet 'pr_opened')
    # would both slip through without this. Keyed on (file, klass) so a
    # different file OR class never collides.
    seen_pairs: dict = {}
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
        _pair = ((row.get("file_path") or "").strip(), verdict.get("klass"))
        if _pair[0] and _pair[1] and _pair in seen_pairs:
            # Another row THIS run already owns this file+class — mark it so it
            # is not retried next tick, exactly like the cross-run DB path.
            try:
                _mark_dup_skipped(row.get("id"),
                                  dup_of_id=seen_pairs[_pair])
            except Exception:
                pass
            skipped.append({"id": row.get("id"),
                            "reason": "dup_open_pr_same_file_class",
                            "dup_of_id": seen_pairs[_pair]})
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
            if _pair[0] and _pair[1]:
                seen_pairs.setdefault(_pair, row.get("id"))
            opened.append({"id": row.get("id"), "pr_url": res.get("pr_url"),
                           "branch": res.get("branch")})
        else:
            if _pair[0] and _pair[1]:
                seen_pairs.setdefault(_pair, row.get("id"))
            previewed.append({"id": row.get("id"), "branch": res.get("branch"),
                              "file": res.get("file_path"),
                              "klass": res.get("klass"),
                              "diff_preview": res.get("diff_preview", [])})
    return {"dry_run": not apply, "rate_cap": cap,
            "opened": opened, "previewed": previewed, "skipped": skipped}
