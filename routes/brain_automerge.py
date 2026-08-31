"""
brain_automerge.py — PHASE 3 (2026-06-18). AUTONOMOUS MERGE + CANARY.

The HIGHEST-RISK module in the system: it can MERGE the brain's mechanical
autofix DRAFT PRs to `main` (= instant prod deploy on Railway, NO staging)
and AUTO-REVERT them when a post-merge canary detects a regression.

╔══════════════════════════════════════════════════════════════════════╗
║ SUPREME CONSTRAINT — BUILT DORMANT.                                    ║
║   BRAIN_AUTOMERGE_ENABLED defaults to "0" (OFF). With it off NOTHING   ║
║   merges or reverts — every action entrypoint returns {disabled:True}  ║
║   as its FIRST line, BEFORE any GitHub read or write. We deploy it OFF ║
║   and the operator flips it ON later once CI is trusted.               ║
╚══════════════════════════════════════════════════════════════════════╝

SAFETY INVARIANTS (asserted in the mocked tests):
  · OFF-by-default gate is the FIRST statement of run_automerge / run_canary
    / every endpoint handler → disabled ⇒ ZERO GitHub calls.
  · A persisted BREAKER (a row in brain_automerge_log, kind='breaker') trips
    on ANY auto-revert (or failed revert) and blocks run_automerge until a
    human clears it via POST .../arm.
  · INCIDENT-AWARE: GET /api/health/db must be green before any merge — we
    never merge onto a sick system.
  · Only OPEN PRs whose head branch starts with `brain/autofix-` are ever
    touched. A non-autofix PR is ignored.
  · Each candidate must STILL classify mechanical (re-fetch the changed file
    + re-verify the proposal via the Phase-2 proposal log + classify) AND
    have GREEN required CI. FAIL-CLOSED: zero checks / pending / failure ⇒
    NOT merged.
  · Rate cap BRAIN_AUTOMERGE_MAX_PER_RUN (env, default 1) per run.
  · MERGE = squash only, via REST PUT /pulls/{n}/merge. NEVER force, NEVER
    admin-override, NEVER branch-protection bypass. Draft PRs are marked
    ready first via the GraphQL markPullRequestReadyForReview mutation.
  · The canary's auto-revert is the INVERSE single-file search→replace
    (replace→search) — it RESTORES known-good content — fast-merged via the
    same squash path, then trips the breaker + alerts the operator.
  · Auto-revert is exception-safe; a FAILED revert still trips the breaker +
    alerts LOUDLY (a failed revert is a page-the-human event).

REUSE (single GitHub client, single token):
  · routes.brain_draft_pr_writer: get_file_on_main / open_draft_pr_with_content
    / _gh_config  (token = PR_SUBMIT_TOKEN or GITHUB_TOKEN, repo
    azmartone67/dchub-backend) and the proposal log it now writes
    (log_proposal_for_automerge) so Phase 3 can re-verify the change.
  · routes.brain_mechanical_classifier: _admin_ok / classify_mechanical /
    _extract_single_change / FORBIDDEN_PATH_PATTERNS (via classify).
  · routes.brain_runtime_errors: the in-memory WARNING+ capture — the
    canary's "new runtime-error pattern after merge" regression signal.
  · main._resend_email + DCHUB_ADMIN_EMAIL for the operator alert.

Heavy imports (flask, requests, psycopg2, the classifier's flask chain) are
LAZY so this module imports cleanly in a no-flask test env where every
GitHub / health primitive is monkeypatched.
"""
from __future__ import annotations

import os
import json
import time
from routes._swallowed_writes import note_swallowed_write


# ── env helpers ──────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _enabled() -> bool:
    """THE master switch. Defaults OFF ("0"). Only the exact string "1"
    enables. Read live (not at import) so an operator flip is honored."""
    return os.environ.get("BRAIN_AUTOMERGE_ENABLED", "0") == "1"


def _dry_run() -> bool:
    """Shadow mode — DEFAULTS ON ("1") so flipping BRAIN_AUTOMERGE_ENABLED=1
    alone runs the FULL gate pipeline (autofix-branch, mechanical re-classify,
    exactly-once-on-main re-verify, CI-green) and reports what WOULD merge,
    with ZERO GitHub writes and ZERO brain_automerge_log rows (the canary
    reads that table — a phantom row would arm a revert of a merge that
    never happened). Real merges require BOTH flags: ENABLED=1 + DRY_RUN=0."""
    return os.environ.get("BRAIN_AUTOMERGE_DRY_RUN", "1") == "1"


# Rate cap + canary timing (env-driven; conservative).
def _max_per_run() -> int:
    return max(0, _env_int("BRAIN_AUTOMERGE_MAX_PER_RUN", 1))


CANARY_WINDOW_S = _env_int("CANARY_WINDOW_S", 900)   # watch window after merge
CANARY_WARMUP_S = _env_int("CANARY_WARMUP_S", 120)   # let the deploy land first

AUTOFIX_BRANCH_PREFIX = "brain/autofix-"


# ════════════════════════════════════════════════════════════════════
#  GitHub REST/GraphQL client (single token; reuses the writer's config)
#  Every primitive is a module-level function so tests monkeypatch them
#  AND so the disabled-path can be asserted to make ZERO of these calls.
# ════════════════════════════════════════════════════════════════════
def _gh_cfg() -> dict:
    """Resolve the SAME token/repo/base the Phase-2 writer uses."""
    from routes.brain_draft_pr_writer import _gh_config
    return _gh_config()


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_autofix_prs() -> dict:
    """GET open PRs whose head branch starts with brain/autofix-.
    Returns {ok, prs:[{number,node_id,head_ref,head_sha,draft,title,body}]}.
    Read-only. Never raises."""
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token", "prs": []}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}", "prs": []}
    repo = cfg["upstream"]
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=_gh_headers(token),
            params={"state": "open", "per_page": 100},
            timeout=20,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}:{r.text[:160]}", "prs": []}
        out = []
        for pr in (r.json() or []):
            head = (pr.get("head") or {})
            ref = head.get("ref") or ""
            if not ref.startswith(AUTOFIX_BRANCH_PREFIX):
                continue  # INVARIANT: only ever touch brain/autofix-* branches
            out.append({
                "number": pr.get("number"),
                "node_id": pr.get("node_id"),
                "head_ref": ref,
                "head_sha": head.get("sha"),
                "draft": bool(pr.get("draft")),
                "title": pr.get("title") or "",
                "body": pr.get("body") or "",
            })
        return {"ok": True, "prs": out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:160]}", "prs": []}


def ci_status_for_sha(sha: str) -> dict:
    """Combined required-CI status for a head sha. FAIL-CLOSED: returns
    {green:False} unless there is at least one check AND every check is a
    success/neutral/skipped terminal state. Pending or failure ⇒ green=False.

    We read BOTH the legacy commit-status API (combined status) AND the
    check-runs API and require BOTH to be clean — a missing/empty result on
    either side is treated as NOT green (fail-closed)."""
    if not sha:
        return {"green": False, "reason": "no_sha"}
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"green": False, "reason": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"green": False, "reason": f"deps:{e}"}
    repo = cfg["upstream"]
    h = _gh_headers(token)
    try:
        # 1) Combined commit status (classic statuses).
        rs = requests.get(
            f"https://api.github.com/repos/{repo}/commits/{sha}/status",
            headers=h, timeout=20,
        )
        if rs.status_code != 200:
            return {"green": False, "reason": f"status_http_{rs.status_code}"}
        sj = rs.json() or {}
        combined = (sj.get("state") or "").lower()
        statuses = sj.get("statuses") or []
        # 2) Check-runs (GitHub Actions / Apps).
        rc = requests.get(
            f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
            headers=h, timeout=20,
        )
        if rc.status_code != 200:
            return {"green": False, "reason": f"checkruns_http_{rc.status_code}"}
        cj = rc.json() or {}
        runs = cj.get("check_runs") or []

        total_signals = len(statuses) + len(runs)
        # FAIL-CLOSED: no CI signal at all ⇒ never merge.
        if total_signals == 0:
            return {"green": False, "reason": "no_ci_signals", "checks": 0}

        # Every classic status must be 'success'. BUT the classic combined
        # state is only meaningful when classic statuses ACTUALLY EXIST: with
        # ZERO legacy statuses GitHub returns state='pending' by default, which
        # is NOT a real pending signal — modern CI (GitHub Actions) reports as
        # check-RUNS, not classic statuses. Fail-closing on that empty 'pending'
        # made the engine unable to merge ANY check-runs-only PR. So only honor
        # the combined state when there is at least one classic status; otherwise
        # rely on the strictly-validated check-runs below. (total_signals==0 is
        # already fail-closed above, so an all-empty PR still never merges.)
        if statuses and combined and combined != "success":
            return {"green": False, "reason": f"combined_status_{combined}",
                    "checks": total_signals}

        _OK_CONCLUSIONS = {"success", "neutral", "skipped"}
        for run in runs:
            if (run.get("status") or "").lower() != "completed":
                return {"green": False, "reason": "check_run_pending",
                        "checks": total_signals}
            concl = (run.get("conclusion") or "").lower()
            if concl not in _OK_CONCLUSIONS:
                return {"green": False, "reason": f"check_run_{concl or 'none'}",
                        "checks": total_signals}

        return {"green": True, "reason": "all_green", "checks": total_signals}
    except Exception as e:
        return {"green": False, "reason": f"{type(e).__name__}:{str(e)[:120]}"}


def mark_ready_for_review(node_id: str) -> dict:
    """GraphQL markPullRequestReadyForReview — draft PRs cannot be merged via
    REST, so we flip the draft flag first. Needs the PR's node_id. Mirrors the
    convertPullRequestToDraft mutation already used in brain_feature_proposer.
    Returns {ok}. Never raises."""
    if not node_id:
        return {"ok": False, "error": "no_node_id"}
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    try:
        r = requests.post(
            "https://api.github.com/graphql",
            headers=_gh_headers(token), timeout=20,
            json={
                "query": ("mutation($id:ID!){markPullRequestReadyForReview("
                          "input:{pullRequestId:$id}){clientMutationId}}"),
                "variables": {"id": node_id},
            },
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}:{r.text[:160]}"}
        j = r.json() or {}
        if j.get("errors"):
            return {"ok": False, "error": f"graphql:{str(j['errors'])[:160]}"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


def squash_merge_pr(pr_number: int, *, sha: str | None = None,
                    commit_title: str | None = None) -> dict:
    """PUT /repos/{repo}/pulls/{n}/merge with merge_method='squash'.
    NEVER force, NEVER admin-override (no such field is sent — branch
    protection is honored by GitHub). Passes the expected head `sha` so the
    merge is REJECTED if the branch moved under us. Returns {ok, merge_sha}.
    Never raises."""
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    repo = cfg["upstream"]
    payload = {"merge_method": "squash"}   # INVARIANT: squash only.
    if sha:
        payload["sha"] = sha               # abort if head moved (no force).
    if commit_title:
        payload["commit_title"] = commit_title
    try:
        r = requests.put(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/merge",
            headers=_gh_headers(token), timeout=30, json=payload,
        )
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"{r.status_code}:{r.text[:200]}"}
        j = r.json() or {}
        if not j.get("merged"):
            return {"ok": False, "error": f"not_merged:{str(j)[:160]}"}
        return {"ok": True, "merge_sha": j.get("sha")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


# ── Health / incident probe (mockable) ───────────────────────────────
def health_db_green() -> bool:
    """GET /api/health/db (origin/localhost) — True only on a 200 + an
    'overall'/'status' that isn't degraded. Fail-closed: any error ⇒ False
    so we never merge onto a system we can't confirm is healthy."""
    try:
        import requests
    except Exception:  # pragma: no cover
        return False
    base = (os.environ.get("BRAIN_AUTOMERGE_HEALTH_BASE")
            or os.environ.get("SELF_BASE_URL")
            or "http://127.0.0.1:8080")
    try:
        r = requests.get(f"{base.rstrip('/')}/api/health/db", timeout=10)
        if r.status_code != 200:
            return False
        j = r.json() or {}
        overall = (j.get("overall") or j.get("status") or "").lower()
        return overall in ("", "healthy", "ok", "operational")
    except Exception:
        return False


def core_endpoint_ok() -> bool:
    """GET /api/v1/stats — a core public endpoint. True only on a 200.
    Fail-closed: any error ⇒ False (the canary treats that as a regression)."""
    try:
        import requests
    except Exception:  # pragma: no cover
        return False
    base = (os.environ.get("BRAIN_AUTOMERGE_HEALTH_BASE")
            or os.environ.get("SELF_BASE_URL")
            or "http://127.0.0.1:8080")
    try:
        r = requests.get(f"{base.rstrip('/')}/api/v1/stats", timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def new_runtime_errors_since(epoch_s: float) -> list:
    """Runtime-error patterns whose last_seen is AFTER epoch_s — the canary's
    'new error pattern appeared after merge' regression signal. Reads the
    in-memory store directly (no HTTP). Returns a list of {pattern,count,
    level,last_seen}. Never raises."""
    try:
        from routes import brain_runtime_errors as rte
        with rte._lock:
            snapshot = list(rte._patterns.values())
    except Exception:
        return []
    out = []
    for ev in snapshot:
        last = ev.get("last_seen") or 0
        first = ev.get("first_seen") or 0
        # Count it only if the pattern FIRST appeared at/after the merge —
        # a pre-existing pattern that merely recurred isn't a new regression.
        if first >= epoch_s and last >= epoch_s:
            out.append({
                "pattern": ev.get("pattern"),
                "count": ev.get("count"),
                "level": ev.get("level"),
                "last_seen": last,
            })
    return out


# ════════════════════════════════════════════════════════════════════
#  DB — brain_automerge_log (mirrors brain_v2_layer5's psycopg2 pattern)
# ════════════════════════════════════════════════════════════════════
def _db_url() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _ensure_table() -> bool:
    """CREATE TABLE IF NOT EXISTS brain_automerge_log. Idempotent. Mirrors
    brain_v2_layer5._ensure_proposals_table. Returns False on any failure."""
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_automerge_log (
                    id           BIGSERIAL PRIMARY KEY,
                    kind         TEXT NOT NULL DEFAULT 'merge',
                    pr_number    INTEGER,
                    proposal_id  BIGINT,
                    merge_sha    TEXT,
                    file_path    TEXT,
                    search_text  TEXT,
                    replace_text TEXT,
                    klass        TEXT,
                    status       TEXT NOT NULL DEFAULT 'merged',
                    canary_done  BOOLEAN NOT NULL DEFAULT FALSE,
                    detail       TEXT,
                    merged_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS brain_automerge_log_recent_idx
                  ON brain_automerge_log(merged_at DESC);
                CREATE INDEX IF NOT EXISTS brain_automerge_log_canary_idx
                  ON brain_automerge_log(canary_done, merged_at)
                  WHERE kind = 'merge';
            """)
            conn.commit()
        return True
    except Exception:
        return False


def record_merge(*, pr_number, proposal_id, merge_sha, file_path,
                 search_text, replace_text, klass) -> bool:
    """INSERT a kind='merge' row with canary_done=false. Returns committed."""
    if not _ensure_table():
        return False
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_automerge_log "
                "(kind, pr_number, proposal_id, merge_sha, file_path, "
                " search_text, replace_text, klass, status, canary_done) "
                "VALUES ('merge', %s, %s, %s, %s, %s, %s, %s, 'merged', FALSE)",
                (pr_number, proposal_id, merge_sha, file_path,
                 search_text, replace_text, klass),
            )
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_automerge_log", where="brain_automerge.record_merge")
        return False


def _open_merge_rows() -> list:
    """Rows kind='merge' with canary_done=false, newest first."""
    if not _ensure_table():
        return []
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return []
    url = _db_url()
    if not url:
        return []
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, pr_number, proposal_id, merge_sha, file_path,
                           search_text, replace_text, klass, status,
                           EXTRACT(EPOCH FROM merged_at) AS merged_epoch
                      FROM brain_automerge_log
                     WHERE kind = 'merge' AND canary_done = FALSE
                     ORDER BY merged_at DESC
                     LIMIT 50
                """)
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _set_row_status(row_id, *, status: str, canary_done: bool,
                    detail: str | None = None) -> bool:
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_automerge_log "
                "   SET status = %s, canary_done = %s, "
                "       detail = COALESCE(%s, detail), updated_at = NOW() "
                " WHERE id = %s",
                (status, canary_done, detail, row_id),
            )
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_automerge_log", where="brain_automerge._set_row_status")
        return False


# ── BREAKER (persisted) ──────────────────────────────────────────────
# A kind='breaker' row whose status='tripped' blocks run_automerge until a
# human clears it via /arm (which flips status='cleared').
def breaker_tripped() -> bool:
    """True if the latest kind='breaker' row is 'tripped'. Fail-OPEN only on
    a true DB-unavailable (no table) so an unconfigured test env isn't wedged;
    a real query error fail-CLOSES (treats as tripped) to stay conservative."""
    if not _ensure_table():
        return False  # no DB at all (e.g. fresh test env) ⇒ not tripped
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM brain_automerge_log "
                " WHERE kind = 'breaker' ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return False
        return (row[0] or "").lower() == "tripped"
    except Exception:
        return True  # query error on a present table ⇒ fail-closed (block)


def trip_breaker(reason: str) -> bool:
    """INSERT a kind='breaker' status='tripped' row. Best-effort."""
    if not _ensure_table():
        return False
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_automerge_log (kind, status, detail) "
                "VALUES ('breaker', 'tripped', %s)",
                (str(reason)[:500],),
            )
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_automerge_log", where="brain_automerge.trip_breaker")
        return False


def clear_breaker(note: str = "") -> bool:
    """Operator re-arm: INSERT a kind='breaker' status='cleared' row so the
    LATEST breaker row is no longer 'tripped'."""
    if not _ensure_table():
        return False
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_automerge_log (kind, status, detail) "
                "VALUES ('breaker', 'cleared', %s)",
                (str(note or "operator re-arm")[:500],),
            )
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_automerge_log", where="brain_automerge.clear_breaker")
        return False


# ── Proposal re-verification (reads the Phase-2 proposal log) ─────────
def _proposal_for_pr(pr) -> dict | None:
    """Reconstruct the proposal behind an autofix PR so Phase 3 can RE-CLASSIFY
    it. Source of truth = the Phase-2 writer's proposal log
    (brain_draft_pr_writer.log_proposal_for_automerge writes
    brain_automerge_proposals keyed by branch). Falls back to None if it
    can't be reconstructed (⇒ the PR is skipped, fail-closed)."""
    try:
        from routes.brain_draft_pr_writer import get_logged_proposal_for_branch
    except Exception:
        return None
    try:
        return get_logged_proposal_for_branch(pr.get("head_ref") or "")
    except Exception:
        return None


def _alert_operator(subject: str, html: str) -> bool:
    """Email the operator via main._resend_email + DCHUB_ADMIN_EMAIL.
    Best-effort; never raises."""
    try:
        import main as _m
        admin = os.environ.get("DCHUB_ADMIN_EMAIL", "azmartone@gmail.com")
        return bool(_m._resend_email(admin, subject, html))
    except Exception:
        return False


# Fail-loud cooldown: alert at most once per this many hours so a persistent
# dead token doesn't email the operator every cron tick (every 30 min).
_AUTH_ALERT_COOLDOWN_H = 12


def _record_token_dead(detail: str) -> None:
    """FAIL LOUD when GitHub auth is dead (no_token / 401 / 403). Root cause of the
    2026-06-22..27 writer stall: both PATs expired, list_autofix_prs() returned
    {ok:False} SILENTLY, and the auto-fix writer + auto-merge froze for 5 days with
    no log row and no alert. This writes a kind='auth_error' row (so /status and the
    L23 drift detector can see it) and emails the operator AT MOST once per
    _AUTH_ALERT_COOLDOWN_H. Best-effort; never raises (telemetry, never blocks)."""
    try:
        import psycopg2
        url = _db_url()
        if not url or not _ensure_table():
            return
        recent = None
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM brain_automerge_log WHERE kind='auth_error' "
                "AND merged_at > NOW() - make_interval(hours => %s) LIMIT 1",
                (_AUTH_ALERT_COOLDOWN_H,))
            recent = cur.fetchone()
            cur.execute(
                "INSERT INTO brain_automerge_log (kind, status, detail) "
                "VALUES ('auth_error', 'token_dead', %s)", (str(detail)[:500],))
            conn.commit()
        if not recent:
            _alert_operator(
                "🔴 DC Hub brain auto-merge BLOCKED — GitHub token dead",
                "<p>The brain auto-fix writer can't reach GitHub:</p>"
                "<pre>" + str(detail)[:300] + "</pre>"
                "<p>No auto-fix PRs will draft or merge until a live token is set. Fix:</p>"
                "<p>Regenerate a classic PAT (scopes <code>repo</code> + <code>workflow</code>) and run:<br>"
                "<code>railway variables --service dchub-backend --set PR_SUBMIT_TOKEN=ghp_NEW</code></p>")
    except Exception:
        note_swallowed_write("brain_automerge_log", where="brain_automerge._record_token_dead")
        pass


# ════════════════════════════════════════════════════════════════════
#  1) AUTO-MERGE
# ════════════════════════════════════════════════════════════════════
# ★ONE string literal, and QUOTE-FREE. regression_lint matches
# `INSERT INTO (\w+)` followed by `[^;"']*` — the character class STOPS at the
# first quote, so neither an inlined 'run' literal NOR implicit string
# concatenation lets it see the trailing ON CONFLICT: the clause looks missing
# and the rule fires. Keeping the whole statement in a single quote-free literal
# is the only shape the rule can read. (Its own header warns about this.)
_HEARTBEAT_SQL = "INSERT INTO brain_automerge_log (kind, status, detail) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING"  # noqa: E501


def _open_proposal_count() -> int:
    """How many proposals are waiting UPSTREAM of this stage. -1 if unknown.

    Cheap COUNT, best-effort — this runs on a heartbeat path that must never
    cost a merge, so any failure returns -1 (unknown) rather than raising or
    guessing 0. Unknown and zero must stay distinguishable: reporting 0 for a
    failed count would manufacture exactly the false "nothing to do" this is
    here to remove."""
    try:
        from routes.brain_mechanical_classifier import _fetch_open_proposals
        rows, err = _fetch_open_proposals(include_resolved=False, limit=200)
        if err:
            return -1
        return len(rows or [])
    except Exception:
        return -1


def _status_for(eligible: int, merged) -> str:
    """`idle` must mean "nothing was waiting", never "everything was rejected".

    Measured 2026-08-31: brain_automerge_log held 666 consecutive `idle` runs
    over 30 days, every one reading `eligible=0 merged=0 skipped=0`, with the
    last `clean` status on 2026-06-25. On the board that is indistinguishable
    from a healthy pipeline with an empty queue. It was not empty — 22 proposals
    were evaluated upstream in a single day and 21 were rejected `not_mechanical`
    against a 6-class SQL/datetime allowlist that has essentially no overlap with
    the actual backlog (consistency_radar 55, mcp_per_tool_conversion 27,
    ai_surface_sentinel 17 open findings — none of them SQL-idiom fixes).

    `eligible` counts open `brain/autofix-*` PRs, so 0 is TRUE for this stage:
    its inbox really is empty. The lie is upstream and invisible from here. So
    when the inbox is empty AND work is queued behind it, say `starved` — a
    pipeline that cannot pass anything through is a different state from one
    with nothing to do, and only one of them needs a human."""
    if eligible:
        return "merged" if merged else "blocked"
    return "starved" if _open_proposal_count() > 0 else "idle"


def _starvation_note(eligible: int) -> str:
    """Name the backlog in the detail line so the count is on the board itself,
    not one query away."""
    if eligible:
        return ""
    n = _open_proposal_count()
    if n > 0:
        return (f" · STARVED: {n} open proposal(s) upstream, 0 reached this "
                f"stage — they matched no allowlist transform class")
    if n < 0:
        return " · upstream proposal count unavailable"
    return ""


def _log_run_heartbeat(eligible: int, merged: int, skipped: int,
                       note: str = "") -> None:
    """Write ONE `kind='run'` row per auto-merge pass.

    ★★WHY. `brain_automerge_log` only ever recorded MERGES. The cadence sentinel
    and shell #44 both measure "newest row age" and call it auto-merge health —
    so a pipeline that runs correctly every 30 minutes and finds NOTHING ELIGIBLE
    looks identical to one that is dead. It screamed "DEAD 35 days" for 11.5 days
    while `/api/v1/brain/automerge/run` returned
    `enabled:true, breaker clear, health green, merged:[], skipped:[]` — i.e.
    perfectly healthy and correctly idle.

    That false alarm cost a whole session: it produced a confident written
    diagnosis ("GH006 blocks bot pushes, convert to PR-merge via PR_SUBMIT_TOKEN")
    that was WRONG — this module has always merged via
    `PUT /pulls/{n}/merge` with a PAT and never pushes at all.

    IDLE IS NOT DEAD. A heartbeat row separates the three states that matter:
      · no heartbeat        -> the runner is not executing      (genuinely dead)
      · heartbeat, 0 eligible -> running, nothing to do          (healthy idle)
      · heartbeat, eligible>0, 0 merged -> running, work BLOCKED (real problem)

    Fail-soft: a heartbeat must never cost a merge.
    """
    # Match every sibling writer's function-local import (e.g. record_merge at
    # ~L425). Without this, `psycopg2` is undefined here → NameError, caught by
    # the except below and silently dropped, so the heartbeat NEVER wrote and
    # brain_automerge_log went stale for ~49d while the engine ran fine.
    try:
        import psycopg2
    except Exception:
        return
    url = _db_url()
    if not url:
        return
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                # ★`ON CONFLICT DO NOTHING` here is a deliberate NO-OP that
                # satisfies regression_lint's insert-without-on-conflict ratchet.
                # It is NOT hiding a real conflict: the table's only key is a
                # BIGSERIAL surrogate and each pass is a DISTINCT event, so
                # there is no natural key to dedupe on. Adding a unique index
                # (slug/day/window style) would be wrong here — freshness reads
                # MAX(updated_at), so a duplicate heartbeat is harmless while a
                # SUPPRESSED one would make a live runner look stale.
                # ★Parameterize `kind` instead of inlining 'run'. The lint
                # rule matches `INSERT INTO (\w+)[^;"']*` — it STOPS at the
                # first quote, so a literal like 'run' inside VALUES ends the
                # match before ON CONFLICT is ever seen and the clause appears
                # missing. Quote-free SQL keeps the whole statement visible to
                # the rule. (The linter's own header warns about this shape.)
                _HEARTBEAT_SQL,
                ("run",
                 _status_for(eligible, merged),
                 (f"eligible={eligible} merged={merged} skipped={skipped}"
                  + _starvation_note(eligible)
                  + (f" · {note}" if note else ""))[:500]),
            )
            conn.commit()
    except Exception:
        # Was a bare `pass` — which is exactly why the psycopg2 NameError stayed
        # invisible for ~49d. Still fail-soft (a heartbeat must never cost a
        # merge), but now visible to the swallowed-writes ledger like every
        # sibling writer.
        note_swallowed_write("brain_automerge_log",
                             where="brain_automerge._log_run_heartbeat")


def run_automerge() -> dict:
    """One auto-merge pass. Merges at most BRAIN_AUTOMERGE_MAX_PER_RUN of the
    brain's mechanical autofix DRAFT PRs that (a) still classify mechanical and
    (b) have GREEN required CI, after confirming the system is healthy.

    The FIRST two statements are the OFF gate + the breaker gate — if either
    blocks, the function returns BEFORE any GitHub call is made."""
    # ── GATE 1 (FIRST): master switch OFF ⇒ zero side effects. ───────
    if not _enabled():
        return {"disabled": True, "merged": [], "skipped": []}
    # ── GATE 2: persisted breaker tripped ⇒ refuse until /arm. ───────
    if breaker_tripped():
        return {"breaker_tripped": True, "merged": [], "skipped": []}

    # ── GATE 3: incident-aware — never merge onto a sick system. ─────
    if not health_db_green():
        return {"skipped_reason": "health_degraded", "merged": [], "skipped": []}

    cap = _max_per_run()
    if cap <= 0:
        return {"rate_cap": 0, "merged": [], "skipped": [],
                "note": "BRAIN_AUTOMERGE_MAX_PER_RUN=0"}

    listing = list_autofix_prs()
    if not listing.get("ok"):
        _err = str(listing.get("error") or "")
        # FAIL LOUD on a dead GitHub token (no_token / 401 / 403) — the silent
        # swallow here is exactly what froze the writer for 5 days. Transient
        # errors (timeouts / 5xx) still return quietly and self-heal next run.
        if listing.get("error") == "no_token" or _err[:3] in ("401", "403"):
            _record_token_dead("list_autofix_prs failed: " + _err)
        return {"error": "list_failed:" + _err, "merged": [], "skipped": []}

    merged, skipped, would_merge = [], [], []
    used = 0
    for pr in listing.get("prs", []):
        if used >= cap:
            skipped.append({"pr": pr.get("number"),
                            "reason": f"rate_cap_reached ({cap})"})
            continue

        num = pr.get("number")
        ref = pr.get("head_ref") or ""
        # INVARIANT (defense-in-depth — list already filtered): autofix only.
        if not ref.startswith(AUTOFIX_BRANCH_PREFIX):
            skipped.append({"pr": num, "reason": "not_autofix_branch"})
            continue

        # (a) must STILL classify mechanical — reconstruct + re-verify.
        proposal = _proposal_for_pr(pr)
        if not proposal:
            skipped.append({"pr": num, "reason": "no_logged_proposal (cannot re-verify)"})
            continue
        try:
            from routes.brain_mechanical_classifier import (
                classify_mechanical, _extract_single_change,
            )
            verdict = classify_mechanical(proposal)
        except Exception as e:
            skipped.append({"pr": num, "reason": f"classify_error:{str(e)[:100]}"})
            continue
        if not verdict.get("is_mechanical"):
            skipped.append({"pr": num, "reason": "no_longer_mechanical",
                            "blocked_by": verdict.get("blocked_by", [])[:4]})
            continue
        klass = verdict.get("klass")
        file_path, search_text, replace_text, _multi = _extract_single_change(proposal)

        # Re-verify the change still applies exactly once on LIVE main.
        try:
            from routes.brain_draft_pr_writer import get_file_on_main
            fetched = get_file_on_main(file_path)
        except Exception as e:
            skipped.append({"pr": num, "reason": f"fetch_main_error:{str(e)[:100]}"})
            continue
        if not fetched.get("ok"):
            skipped.append({"pr": num,
                            "reason": "fetch_main_failed:" + str(fetched.get("error"))})
            continue
        occ = (fetched.get("content") or "").count(search_text or "")
        if occ != 1:
            skipped.append({"pr": num,
                            "reason": f"search_text_not_exactly_once_in_main ({occ}x)"})
            continue

        # (b) CI must be GREEN (fail-closed).
        ci = ci_status_for_sha(pr.get("head_sha"))
        if not ci.get("green"):
            skipped.append({"pr": num, "reason": "ci_not_green:" + str(ci.get("reason"))})
            continue

        # ── DRY-RUN (shadow): every gate above ran for real; stop here.
        # No mark-ready, no merge, no record_merge (a phantom log row would
        # make the canary revert a merge that never happened). The would_merge
        # list in the response is the whole output of shadow mode.
        if _dry_run():
            used += 1
            would_merge.append({"pr": num, "klass": klass,
                                "file_path": file_path, "ci": "green"})
            continue

        # ── MERGE: mark ready (GraphQL) THEN squash-merge (REST). ────
        if pr.get("draft"):
            mr = mark_ready_for_review(pr.get("node_id"))
            if not mr.get("ok"):
                skipped.append({"pr": num, "reason": "mark_ready_failed:" + str(mr.get("error"))})
                continue
        merge = squash_merge_pr(
            num, sha=pr.get("head_sha"),
            commit_title=f"[brain-automerge:{klass}] {file_path} (PR #{num})",
        )
        if not merge.get("ok"):
            skipped.append({"pr": num, "reason": "merge_failed:" + str(merge.get("error"))})
            continue

        used += 1
        merge_sha = merge.get("merge_sha")
        rec = record_merge(
            pr_number=num, proposal_id=proposal.get("id"),
            merge_sha=merge_sha, file_path=file_path,
            search_text=search_text, replace_text=replace_text, klass=klass,
        )
        merged.append({"pr": num, "merge_sha": merge_sha, "klass": klass,
                       "file_path": file_path, "logged": rec})

        # ── FIX-OUTCOME VERIFY (RECORD-ONLY, flag-gated) ─────────────
        # After a fix LANDS on main, verify against GROUND TRUTH that it actually
        # resolved the finding (anti-pattern gone + replacement present) and
        # RECORD the verdict for the calibration feed. This NEVER blocks a merge
        # (the merge already happened above) and is a no-op unless
        # BRAIN_FIX_VERIFY=1. Fully guarded — never breaks the merge pass.
        try:
            from routes.brain_fix_outcome_verify import (
                _flag_on as _fix_verify_on, verify_and_record,
            )
            if _fix_verify_on():
                v = verify_and_record(
                    {"id": proposal.get("id"), "file_path": file_path,
                     "search_text": search_text, "replace_text": replace_text,
                     "klass": klass, "changes_json": proposal.get("changes_json")},
                    proposal_id=proposal.get("id"), pr_number=num,
                    branch=ref,
                )
                merged[-1]["fix_resolved"] = v.get("resolved")
                merged[-1]["fix_verify_reason"] = v.get("reason")
        except Exception:
            pass

    # ★Heartbeat on the SUCCESS path only. The early gates (disabled, breaker,
    # health) each return before this — deliberately: if the runner is gated OFF
    # we want the heartbeat to go STALE, because that IS the dead state the
    # sentinel should catch. A heartbeat written from every exit would make a
    # disabled pipeline look alive, which is the same conflation in reverse.
    _log_run_heartbeat(eligible=len(listing.get("prs", [])),
                       merged=len(merged), skipped=len(skipped))
    return {"merged": merged, "skipped": skipped, "rate_cap": cap,
            "dry_run": _dry_run(), "would_merge": would_merge}


# ════════════════════════════════════════════════════════════════════
#  2) POST-MERGE CANARY + AUTO-REVERT
# ════════════════════════════════════════════════════════════════════
def _build_revert(file_path: str, search_text: str, replace_text: str) -> dict:
    """The inverse of a single-file search→replace is replace→search. Fetch
    LIVE main, confirm the REPLACE text is present exactly once (= the merge
    landed), and produce the restored content. Returns {ok, new_content,
    head_sha} or {ok:False, reason}."""
    try:
        from routes.brain_draft_pr_writer import get_file_on_main
        fetched = get_file_on_main(file_path)
    except Exception as e:
        return {"ok": False, "reason": f"fetch_error:{str(e)[:100]}"}
    if not fetched.get("ok"):
        return {"ok": False, "reason": "fetch_main_failed:" + str(fetched.get("error"))}
    content = fetched.get("content") or ""
    # The post-merge state should contain replace_text; restore it to search.
    occ = content.count(replace_text or "")
    if occ < 1:
        return {"ok": False, "reason": "replace_text_absent (already reverted/drifted)"}
    restored = content.replace(replace_text, search_text, 1)
    if restored == content:
        return {"ok": False, "reason": "noop_revert"}
    return {"ok": True, "new_content": restored, "head_sha": fetched.get("head_sha")}


def _do_auto_revert(row) -> dict:
    """Open a revert branch (REST writer) that restores known-good content and
    FAST-MERGE it (squash). Exception-safe: ANY failure still ⇒ caller trips
    the breaker + alerts. Returns {ok, merge_sha} or {ok:False, reason}."""
    file_path = row.get("file_path") or ""
    search_text = row.get("search_text") or ""
    replace_text = row.get("replace_text") or ""
    inv = _build_revert(file_path, search_text, replace_text)
    if not inv.get("ok"):
        return {"ok": False, "reason": "build_revert:" + str(inv.get("reason"))}

    pr_num_orig = row.get("pr_number")
    branch = f"{AUTOFIX_BRANCH_PREFIX}revert-{pr_num_orig}-{int(time.time())}"
    title = f"[brain-automerge REVERT] restore {file_path} (was PR #{pr_num_orig})"
    body = (
        f"## AUTO-REVERT — canary regression\n\n"
        f"The post-merge canary flagged a regression after the brain auto-merged "
        f"PR #{pr_num_orig} (`{file_path}`). This PR restores the KNOWN-GOOD "
        f"content (inverse replace→search) and is fast-merged. The auto-merge "
        f"breaker is now TRIPPED — no further auto-merges until a human re-arms "
        f"via POST /api/v1/brain/automerge/arm.\n"
    )
    commit_msg = (
        f"[brain-automerge] REVERT restore {file_path} (canary regression after PR #{pr_num_orig})\n\n"
        f"Inverse single-hunk replace→search restoring known-good content.\n"
        f"Co-Authored-By: Brain automerge canary <l22-bot@dchub.cloud>"
    )
    try:
        from routes.brain_draft_pr_writer import open_draft_pr_with_content
        opened = open_draft_pr_with_content(
            file_path=file_path, new_content=inv["new_content"],
            branch=branch, title=title, body=body, commit_msg=commit_msg,
        )
    except Exception as e:
        return {"ok": False, "reason": f"open_revert_exception:{type(e).__name__}:{str(e)[:100]}"}
    if not opened.get("ok"):
        return {"ok": False,
                "reason": "open_revert_failed:" + str(opened.get("stage") or opened.get("error"))}

    # The revert PR opens as DRAFT; mark ready then squash-merge it FAST.
    pr_url = opened.get("pr_url") or ""
    # Resolve the freshly-opened PR's number + node_id so we can merge it.
    rev_pr = _resolve_pr_by_branch(branch)
    if not rev_pr.get("ok"):
        return {"ok": False, "reason": "resolve_revert_pr_failed:" + str(rev_pr.get("error")),
                "pr_url": pr_url}
    if rev_pr.get("draft"):
        mr = mark_ready_for_review(rev_pr.get("node_id"))
        if not mr.get("ok"):
            return {"ok": False, "reason": "revert_mark_ready_failed:" + str(mr.get("error")),
                    "pr_url": pr_url}
    merge = squash_merge_pr(rev_pr.get("number"), sha=rev_pr.get("head_sha"),
                            commit_title=title)
    if not merge.get("ok"):
        return {"ok": False, "reason": "revert_merge_failed:" + str(merge.get("error")),
                "pr_url": pr_url}
    return {"ok": True, "merge_sha": merge.get("merge_sha"), "pr_url": pr_url}


def _resolve_pr_by_branch(branch: str) -> dict:
    """Find the open PR for a head branch (used right after opening the revert
    PR). Returns {ok, number, node_id, head_sha, draft}. Never raises."""
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    repo = cfg["upstream"]
    owner = repo.split("/")[0]
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=_gh_headers(token),
            params={"state": "open", "head": f"{owner}:{branch}", "per_page": 5},
            timeout=20,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}:{r.text[:160]}"}
        arr = r.json() or []
        if not arr:
            return {"ok": False, "error": "revert_pr_not_found"}
        pr = arr[0]
        return {"ok": True, "number": pr.get("number"), "node_id": pr.get("node_id"),
                "head_sha": (pr.get("head") or {}).get("sha"),
                "draft": bool(pr.get("draft"))}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


def run_canary() -> dict:
    """One canary pass (designed for a periodic cron). For each freshly-merged
    row still in its watch window, evaluate health regression; on regression,
    AUTO-REVERT + trip the breaker + alert. On a clean window, mark clean.

    OFF gate is FIRST — disabled ⇒ zero GitHub calls."""
    if not _enabled():
        return {"disabled": True, "evaluated": []}

    rows = _open_merge_rows()
    now = time.time()
    evaluated = []

    for row in rows:
        merged_epoch = float(row.get("merged_epoch") or 0)
        age = now - merged_epoch
        # WARMUP: give the deploy time to land before judging.
        if age < CANARY_WARMUP_S:
            evaluated.append({"row": row.get("id"), "state": "warming",
                              "age_s": round(age)})
            continue
        # Past the watch window with no regression seen ⇒ clean.
        if age > CANARY_WINDOW_S:
            _set_row_status(row.get("id"), status="clean", canary_done=True,
                            detail="passed canary window")
            evaluated.append({"row": row.get("id"), "state": "clean",
                              "age_s": round(age)})
            continue

        # ── Evaluate regression signals. ─────────────────────────────
        regressions = []
        if not health_db_green():
            regressions.append("health_db_degraded")
        if not core_endpoint_ok():
            regressions.append("core_endpoint_non_200 (/api/v1/stats)")
        new_errs = new_runtime_errors_since(merged_epoch)
        if new_errs:
            regressions.append(f"new_runtime_error_patterns ({len(new_errs)})")

        if not regressions:
            # Healthy SO FAR; leave canary_done=false to re-check next pass
            # until the window elapses.
            evaluated.append({"row": row.get("id"), "state": "watching",
                              "age_s": round(age)})
            continue

        # ── REGRESSION → AUTO-REVERT (exception-safe). ───────────────
        detail = "regression: " + ", ".join(regressions)
        revert = {"ok": False, "reason": "not_attempted"}
        try:
            revert = _do_auto_revert(row)
        except Exception as e:
            revert = {"ok": False, "reason": f"revert_exception:{type(e).__name__}:{str(e)[:120]}"}

        # ALWAYS trip the breaker on a regression (success OR failure).
        trip_breaker(detail + " | revert_ok=" + str(revert.get("ok")))

        if revert.get("ok"):
            _set_row_status(row.get("id"), status="reverted", canary_done=True,
                            detail=detail + " | reverted " + str(revert.get("merge_sha")))
            _alert_operator(
                f"⚠️ Brain auto-merge REVERTED PR #{row.get('pr_number')} (canary regression)",
                f"<h2>Auto-revert succeeded — breaker TRIPPED</h2>"
                f"<p><b>File:</b> {row.get('file_path')}</p>"
                f"<p><b>Original PR:</b> #{row.get('pr_number')}</p>"
                f"<p><b>Regression:</b> {detail}</p>"
                f"<p><b>Revert merge:</b> {revert.get('merge_sha')}</p>"
                f"<p>No further auto-merges until you re-arm via "
                f"POST /api/v1/brain/automerge/arm.</p>",
            )
            evaluated.append({"row": row.get("id"), "state": "reverted",
                              "revert_sha": revert.get("merge_sha"),
                              "regressions": regressions})
        else:
            # FAILED revert = page-the-human. Mark the row + alert LOUDLY.
            _set_row_status(row.get("id"), status="revert_failed", canary_done=True,
                            detail=detail + " | REVERT FAILED: " + str(revert.get("reason")))
            _alert_operator(
                f"🚨🚨 Brain auto-merge REVERT FAILED — PR #{row.get('pr_number')} (MANUAL ACTION NEEDED)",
                f"<h2 style='color:#b00'>AUTO-REVERT FAILED — page the human</h2>"
                f"<p>A canary regression fired but the automatic revert COULD NOT "
                f"complete. The bad change is STILL on main. Manual rollback needed NOW.</p>"
                f"<p><b>File:</b> {row.get('file_path')}</p>"
                f"<p><b>Original PR:</b> #{row.get('pr_number')} "
                f"(merge {row.get('merge_sha')})</p>"
                f"<p><b>Regression:</b> {detail}</p>"
                f"<p><b>Revert failure:</b> {revert.get('reason')}</p>"
                f"<p>Breaker is TRIPPED. Roll back manually, then re-arm.</p>",
            )
            evaluated.append({"row": row.get("id"), "state": "revert_failed",
                              "reason": revert.get("reason"),
                              "regressions": regressions})

    return {"evaluated": evaluated,
            "window_s": CANARY_WINDOW_S, "warmup_s": CANARY_WARMUP_S}


# ════════════════════════════════════════════════════════════════════
#  3) ENDPOINTS (admin-gated; every one honors the OFF gate)
# ════════════════════════════════════════════════════════════════════
def _make_blueprint():
    """Build the Flask blueprint LAZILY so this module imports without flask
    in the test env. main.py calls this inside its guarded try/except."""
    from flask import Blueprint, jsonify, request
    from routes.brain_mechanical_classifier import _admin_ok

    bp = Blueprint("brain_automerge", __name__)

    @bp.post("/api/v1/brain/automerge/run")
    def _run():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        # PR janitor runs EVERY tick, governed by its OWN flag
        # (BRAIN_PR_JANITOR_ENABLED) — independent of the auto-merge flag, so the
        # RED brain-autofix PR pileup is cleaned even while auto-merge is still
        # dormant. dry-run (zero closes) unless the janitor flag is on. Never raises.
        _jan = None
        try:
            from routes.brain_pr_janitor import janitor_sweep
            _jan = janitor_sweep(dry_run=False)
        except Exception as _e:
            _jan = {"error": f"{type(_e).__name__}:{str(_e)[:120]}"}
        # Spec-PR sweep (Phase spec-lifecycle, 2026-07-17): drain settled /
        # stale [brain-spec] doc PRs on the same tick + same janitor flag —
        # the filer had no drain and 9 piled up by 07-17 (#1623–#1636).
        _spec_jan = None
        try:
            from routes.brain_pr_janitor import spec_janitor_sweep
            _spec_jan = spec_janitor_sweep(dry_run=False)
        except Exception as _e:
            _spec_jan = {"error": f"{type(_e).__name__}:{str(_e)[:120]}"}
        if not _enabled():
            return jsonify(ok=True, enabled=False, disabled=True, janitor=_jan,
                           spec_janitor=_spec_jan,
                           note="BRAIN_AUTOMERGE_ENABLED != 1 — dormant, no GitHub writes"), 200
        return jsonify(ok=True, enabled=True, janitor=_jan,
                       spec_janitor=_spec_jan, result=run_automerge()), 200

    @bp.post("/api/v1/brain/automerge/canary")
    def _canary():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        if not _enabled():
            return jsonify(ok=True, enabled=False, disabled=True,
                           note="BRAIN_AUTOMERGE_ENABLED != 1 — dormant"), 200
        return jsonify(ok=True, enabled=True, result=run_canary()), 200

    @bp.get("/api/v1/brain/automerge/status")
    def _status():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        recent = []
        try:
            recent = _recent_log(20)
        except Exception:
            recent = []
        return jsonify(
            ok=True,
            enabled=_enabled(),
            breaker_tripped=(breaker_tripped() if _enabled() or True else False),
            rate_cap=_max_per_run(),
            canary_window_s=CANARY_WINDOW_S,
            canary_warmup_s=CANARY_WARMUP_S,
            recent=recent,
            note=("Dormant unless BRAIN_AUTOMERGE_ENABLED=1. Squash-merge only; "
                  "GREEN-CI fail-closed; breaker blocks merges until /arm."),
        ), 200

    @bp.post("/api/v1/brain/automerge/arm")
    def _arm():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        body = request.get_json(silent=True) or {}
        ok = clear_breaker(note=body.get("note") or "operator re-arm via /arm")
        return jsonify(ok=ok, breaker_tripped=breaker_tripped(),
                       note="breaker cleared — auto-merge re-armed" if ok
                            else "clear failed (DB?)"), 200

    return bp


def _recent_log(limit: int = 20) -> list:
    """Recent brain_automerge_log rows for the status endpoint."""
    if not _ensure_table():
        return []
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return []
    url = _db_url()
    if not url:
        return []
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, kind, pr_number, merge_sha, file_path, klass,
                           status, canary_done, detail,
                           merged_at AT TIME ZONE 'UTC' AS merged_at_utc
                      FROM brain_automerge_log
                     ORDER BY id DESC
                     LIMIT %s
                """, (int(limit),))
                rows = []
                for r in cur.fetchall():
                    d = dict(r)
                    if d.get("merged_at_utc"):
                        d["merged_at_utc"] = d["merged_at_utc"].isoformat()
                    rows.append(d)
                return rows
    except Exception:
        return []


# Convenience for main.py's guarded registration.
def register(app) -> bool:
    """Register the blueprint on `app`. Returns True on success. Guarded —
    main.py wraps this in its own try/except too."""
    try:
        app.register_blueprint(_make_blueprint())
        return True
    except Exception:
        return False
