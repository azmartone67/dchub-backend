"""
sentinel_auto_merge.py — Round 2 (2026-06-07).

Sentinel Round 1 (commit 740e3b47) ships: detect persistent 5xx → open
brain Layer-5 finding `page_persistent_5xx:<path>` → brain L5 proposes
a DRAFT PR via brain_backlog_admin. Human currently has to click Merge.

Round 2 closes the loop. Sentinel sweeps the brain-authored draft PRs
in a separate cron and auto-merges only those that pass ALL six gates:

  (1) GATE_SENTINEL_DERIVED   — finding issue == 'page_persistent_5xx:<path>'
  (2) GATE_CONFIDENCE_MIN     — brain L5 confidence ≥ 0.95
  (3) GATE_AST_PARSE          — every .py file in the diff parses
                                 (already gated upstream; re-verified)
  (4) GATE_SANDBOX_PATHS      — every changed file matches the sandbox
                                 allow-list (routes/*.py OR
                                 dchub-frontend/*.html)
  (5) GATE_FORBIDDEN_PATHS    — none of the changed files match the
                                 forbidden surfaces (main.py, auth*,
                                 tier_*, stripe*, schema_repair,
                                 _routes.json, _worker.js)
  (6) GATE_CAP_NOT_HIT        — weekly auto-merge cap (default 10) not
                                 yet hit AND kill switch off

A 7th implicit gate: dry-run. When SENTINEL_AUTO_MERGE_DRY_RUN=1 the
runner LOGS the decision (allow/reject) but skips the actual
`gh pr merge` call. This is the SAFETY DEFAULT for the first deploy.

After a real merge, the runner schedules a 5-min follow-up probe of
the sentinel page to verify the regression actually healed. The
outcome (success / regression / inconclusive) lands in
sentinel_auto_merge_log so the brain can learn which patterns
auto-merge cleanly vs which need human review.

Safety architecture:
  • Kill switch SENTINEL_AUTO_MERGE_DISABLE=1 — sweep returns early.
  • Dry-run SENTINEL_AUTO_MERGE_DRY_RUN=1 — log only, no gh pr merge.
  • Weekly cap default 10 (env SENTINEL_AUTO_MERGE_WEEKLY_CAP).
  • Confidence threshold default 0.95 (env SENTINEL_AUTO_MERGE_REQUIRE_CONF).
  • Forbidden path regex env-extensible
    (SENTINEL_AUTO_MERGE_FORBIDDEN_PATHS).
  • Decisions ALL logged regardless of merge — no silent allows.
  • Roll-back: set SENTINEL_AUTO_MERGE_DISABLE=1, revert the bad PR
    with `git revert <merge-sha>`, push.
"""
from __future__ import annotations

import ast
import base64
import datetime
import json
import logging
import os
import re
from typing import Any

from flask import Blueprint, Response, jsonify, request


logger = logging.getLogger(__name__)
sentinel_auto_merge_bp = Blueprint("sentinel_auto_merge", __name__)


# ── Config ────────────────────────────────────────────────────────────

def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("SENTINEL_AUTO_MERGE_DISABLE"))


def _is_dry_run() -> bool:
    # Default OFF; ops sets SENTINEL_AUTO_MERGE_DRY_RUN=1 in Railway
    # for the first deploy.
    return _truthy(os.environ.get("SENTINEL_AUTO_MERGE_DRY_RUN"))


def _weekly_cap() -> int:
    try:
        return max(0, int(os.environ.get(
            "SENTINEL_AUTO_MERGE_WEEKLY_CAP", "10")))
    except Exception:
        return 10


def _min_conf() -> float:
    try:
        return float(os.environ.get(
            "SENTINEL_AUTO_MERGE_REQUIRE_CONF", "0.95"))
    except Exception:
        return 0.95


# Sandbox: ONLY these path patterns are eligible for auto-merge.
# Anything outside is rejected by GATE_SANDBOX_PATHS.
_SANDBOX_PATTERNS = (
    re.compile(r"^routes/[^/]+\.py$"),
    re.compile(r"^dchub-frontend/[^/]+\.html$"),
    re.compile(r"^dchub-frontend/[^/]+/[^/]+\.html$"),
)

# Forbidden: any of these means immediate reject. Caller can extend via
# SENTINEL_AUTO_MERGE_FORBIDDEN_PATHS (comma-separated regex fragments).
_DEFAULT_FORBIDDEN = (
    r"^main\.py$",
    r"^auth.*\.py$",
    r"^routes/auth.*\.py$",
    r"^tier_.*\.py$",
    r"^routes/tier_.*\.py$",
    r"^stripe.*\.py$",
    r"^routes/stripe.*\.py$",
    r"^routes/schema_repair\.py$",
    r"^schema_repair\.py$",
    r"^.*_routes\.json$",
    r"^.*_worker\.js$",
    r"^crawler_scheduler\.py$",
    r"^routes/internal_auth\.py$",
    r"^internal_auth\.py$",
)


def _forbidden_patterns() -> list[re.Pattern]:
    extra = (os.environ.get("SENTINEL_AUTO_MERGE_FORBIDDEN_PATHS") or "").strip()
    patterns = list(_DEFAULT_FORBIDDEN)
    if extra:
        patterns.extend(p.strip() for p in extra.split(",") if p.strip())
    out: list[re.Pattern] = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except Exception:
            continue
    return out


_GITHUB_REPO = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
_SITE_BASE = os.environ.get("DCHUB_SITE_BASE_URL", "https://dchub.cloud").rstrip("/")
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


# ── DB helpers ────────────────────────────────────────────────────────

def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentinel_auto_merge_log (
    id                          BIGSERIAL PRIMARY KEY,
    pr_number                   INT NOT NULL,
    decision                    TEXT NOT NULL,
    reason                      TEXT,
    issue                       TEXT,
    sentinel_path               TEXT,
    confidence                  REAL,
    files_changed               JSONB,
    merged_at                   TIMESTAMPTZ,
    merge_sha                   TEXT,
    dry_run                     BOOLEAN NOT NULL DEFAULT FALSE,
    follow_up_at                TIMESTAMPTZ,
    follow_up_sentinel_grade    TEXT,
    follow_up_status_code       INT,
    follow_up_outcome           TEXT,
    decided_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sam_pr           ON sentinel_auto_merge_log(pr_number);
CREATE INDEX IF NOT EXISTS ix_sam_decided      ON sentinel_auto_merge_log(decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_sam_decision     ON sentinel_auto_merge_log(decision);
CREATE INDEX IF NOT EXISTS ix_sam_merged       ON sentinel_auto_merge_log(merged_at DESC);
CREATE INDEX IF NOT EXISTS ix_sam_followup_due ON sentinel_auto_merge_log(follow_up_at) WHERE follow_up_at IS NOT NULL AND follow_up_outcome IS NULL;
"""


def _ensure_schema():
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(_LOG_SCHEMA)
        return True
    except Exception as e:
        logger.warning("sentinel_auto_merge_log ensure failed: %s", e)
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def _merges_this_week() -> int:
    """Real merges in the rolling 7-day window. Dry-run rows DON'T count
    so dry-run iterations don't burn the weekly cap."""
    c = _conn()
    if c is None:
        return 0
    try:
        with c.cursor() as cur:
            cur.execute(_LOG_SCHEMA)
            cur.execute("""
                SELECT COUNT(*) FROM sentinel_auto_merge_log
                 WHERE decision = 'allow'
                   AND merged_at IS NOT NULL
                   AND dry_run = FALSE
                   AND merged_at > NOW() - INTERVAL '7 days'
            """)
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── GitHub REST client (no gh CLI dependency) ─────────────────────────
# The Railway runtime doesn't ship the gh binary. Use the REST API
# directly with GITHUB_TOKEN. Mirrors brain_pr_opener._gh pattern.

_GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN")
                 or os.environ.get("PR_SUBMIT_TOKEN") or "").strip()


def _gh_rest(method: str, path: str, body: dict | None = None,
              timeout: int = 30):
    """Minimal GitHub REST client. Returns (status_code, parsed_json_or_text)."""
    import requests
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dchub-sentinel-auto-merge/1.0",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    try:
        r = requests.request(method, url, headers=headers, json=body,
                              timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except Exception as e:
        return 599, f"{type(e).__name__}: {str(e)[:200]}"


def _list_brain_draft_prs(days: int = 7) -> list[dict]:
    """Pull open brain-authored DRAFT PRs from the last `days` days via
    REST API. Brain L5 PR title prefix is `[brain-l5 draft]` (set by
    brain_backlog_admin.py:329).
    """
    if not _GITHUB_TOKEN:
        logger.warning("sentinel_auto_merge: GITHUB_TOKEN unset — sweep "
                       "returns empty")
        return []
    # Use the search/issues endpoint with type:pr (works for PRs since
    # GitHub treats PRs as issues with extra fields).
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)
             ).strftime("%Y-%m-%d")
    q = (f"repo:{_GITHUB_REPO} is:pr is:open draft:true "
         f"\"brain-l5\" in:title created:>={since}")
    import urllib.parse as _up
    code, j = _gh_rest("GET",
                        f"/search/issues?q={_up.quote(q)}&per_page=50")
    if code != 200 or not isinstance(j, dict):
        logger.warning("brain draft PR search failed: %s %s", code,
                       (j if isinstance(j, str) else "")[:200])
        return []
    items = j.get("items") or []
    out: list[dict] = []
    for it in items:
        out.append({
            "number":      int(it.get("number") or 0),
            "title":       it.get("title") or "",
            "body":        it.get("body") or "",
            "headRefName": "",  # populated lazily by _pr_head_ref
            "createdAt":   it.get("created_at"),
            "isDraft":     bool(it.get("draft")),
            "url":         it.get("html_url"),
        })
    return out


def _pr_head_ref(pr_number: int) -> str | None:
    code, j = _gh_rest("GET",
                        f"/repos/{_GITHUB_REPO}/pulls/{pr_number}",
                        timeout=15)
    if code != 200 or not isinstance(j, dict):
        return None
    head = j.get("head") or {}
    return head.get("ref")


def _pr_files(pr_number: int) -> list[dict]:
    """Returns [{'path': ..., 'additions': N, 'deletions': N}, ...] for
    every file in the PR diff. Paginates if there are >100 files."""
    out: list[dict] = []
    page = 1
    while True:
        code, j = _gh_rest("GET",
            f"/repos/{_GITHUB_REPO}/pulls/{pr_number}/files"
            f"?per_page=100&page={page}", timeout=30)
        if code != 200 or not isinstance(j, list):
            break
        if not j:
            break
        for f in j:
            out.append({"path": f.get("filename") or "",
                        "additions": int(f.get("additions") or 0),
                        "deletions": int(f.get("deletions") or 0)})
        if len(j) < 100:
            break
        page += 1
        if page > 5:
            break
    return out


def _pr_patch_for_file(pr_number: int, path: str) -> str | None:
    """Fetch raw file contents from the PR head ref so the ast.parse
    gate runs on the actual proposed content (not the upstream-stale
    pre-proposal content)."""
    ref = _pr_head_ref(pr_number)
    if not ref:
        return None
    code, j = _gh_rest("GET",
        f"/repos/{_GITHUB_REPO}/contents/{path}?ref={ref}", timeout=15)
    if code != 200 or not isinstance(j, dict):
        return None
    b64 = (j.get("content") or "").replace("\n", "")
    if not b64:
        return None
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        return None


# ── PR metadata parsing ───────────────────────────────────────────────

_BODY_CONFIDENCE_RE = re.compile(
    r"(?:Confidence|confidence)[:* ]+\s*\**\s*([0-9]+(?:\.[0-9]+)?)",
)


def _extract_confidence(pr_body: str) -> float | None:
    """Brain L5 PR body includes `**Confidence:** {confidence:.2f}`."""
    if not pr_body:
        return None
    m = _BODY_CONFIDENCE_RE.search(pr_body)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _find_linked_sentinel_finding(pr_body: str) -> tuple[str | None, str | None]:
    """Returns (issue_key, sentinel_path) when the PR body references a
    sentinel-derived finding. We look for the canonical
    `page_persistent_5xx:<path>` issue key the sentinel writes via
    routes/site_sentinel.py:track_consecutive_failures.

    Pattern variants observed:
      - "page_persistent_5xx:/some-path" in body
      - "loop_name: page_persistent_5xx:/some-path" in body
      - "Loop: `page_persistent_5xx:/some-path`" in body
    """
    if not pr_body:
        return None, None
    m = re.search(r"page_persistent_5xx:([/A-Za-z0-9_\-./?=]+)", pr_body)
    if not m:
        return None, None
    path = m.group(1).strip().rstrip(".,`'\"")
    # Trim trailing punctuation that might leak from sentence boundary.
    if path.endswith("`"):
        path = path[:-1]
    return f"page_persistent_5xx:{path}", path


# ── The 6 gates ───────────────────────────────────────────────────────

def _gate_sentinel_derived(pr_data: dict) -> tuple[bool, str, dict]:
    """(1) Finding issue must be page_persistent_5xx:<path>."""
    body = pr_data.get("body") or ""
    issue, path = _find_linked_sentinel_finding(body)
    if not issue:
        return (False,
                "GATE_SENTINEL_DERIVED: body does not reference a "
                "page_persistent_5xx:<path> sentinel finding",
                {"issue": None, "sentinel_path": None})
    return True, "sentinel-derived", {"issue": issue, "sentinel_path": path}


def _gate_confidence(pr_data: dict) -> tuple[bool, str, dict]:
    """(2) Brain L5 confidence ≥ SENTINEL_AUTO_MERGE_REQUIRE_CONF (default 0.95)."""
    body = pr_data.get("body") or ""
    conf = _extract_confidence(body)
    threshold = _min_conf()
    if conf is None:
        return (False,
                f"GATE_CONFIDENCE_MIN: no `Confidence: <n>` line in PR body "
                f"(threshold ≥ {threshold:.2f})",
                {"confidence": None})
    if conf < threshold:
        return (False,
                f"GATE_CONFIDENCE_MIN: confidence {conf:.2f} < "
                f"threshold {threshold:.2f}",
                {"confidence": conf})
    return True, f"confidence {conf:.2f} ≥ {threshold:.2f}", {"confidence": conf}


def _gate_sandbox_paths(files: list[dict]) -> tuple[bool, str, dict]:
    """(4) Every changed file must match the sandbox allow-list."""
    if not files:
        return False, "GATE_SANDBOX_PATHS: no files in PR", {"violators": []}
    violators = []
    for f in files:
        p = f.get("path") or ""
        if not any(rx.match(p) for rx in _SANDBOX_PATTERNS):
            violators.append(p)
    if violators:
        return (False,
                f"GATE_SANDBOX_PATHS: {len(violators)} file(s) outside "
                f"sandbox (routes/*.py | dchub-frontend/*.html): "
                f"{violators[:3]}",
                {"violators": violators})
    return True, f"{len(files)} file(s) within sandbox", {"violators": []}


def _gate_forbidden_paths(files: list[dict]) -> tuple[bool, str, dict]:
    """(5) None of the changed files match the forbidden surfaces."""
    patterns = _forbidden_patterns()
    forbidden_hits = []
    for f in files:
        p = f.get("path") or ""
        for rx in patterns:
            if rx.match(p):
                forbidden_hits.append({"path": p, "pattern": rx.pattern})
                break
    if forbidden_hits:
        return (False,
                f"GATE_FORBIDDEN_PATHS: {len(forbidden_hits)} forbidden "
                f"surface hit: {[h['path'] for h in forbidden_hits][:3]}",
                {"forbidden_hits": forbidden_hits})
    return True, "no forbidden surfaces touched", {"forbidden_hits": []}


def _gate_ast_parse(pr_number: int, files: list[dict]) -> tuple[bool, str, dict]:
    """(3) Every .py file in the diff parses cleanly at the PR head ref."""
    failures = []
    for f in files:
        p = f.get("path") or ""
        if not p.endswith(".py"):
            continue
        content = _pr_patch_for_file(pr_number, p)
        if content is None:
            # If we can't read it, fail closed. Better to reject than
            # auto-merge an un-verifiable patch.
            failures.append({"path": p, "error": "could_not_fetch"})
            continue
        try:
            ast.parse(content)
        except SyntaxError as se:
            failures.append({"path": p, "error": f"SyntaxError: {str(se)[:100]}"})
    if failures:
        return (False,
                f"GATE_AST_PARSE: {len(failures)} file(s) fail ast.parse "
                f"at PR head: {[f['path'] for f in failures][:3]}",
                {"failures": failures})
    return True, "all .py files pass ast.parse at PR head", {"failures": []}


def _gate_cap_and_kill_switch() -> tuple[bool, str, dict]:
    """(6) Weekly cap not hit AND kill switch off AND no DB-level block."""
    if _kill_switch_on():
        return (False,
                "GATE_CAP_NOT_HIT: kill switch SENTINEL_AUTO_MERGE_DISABLE=1",
                {"kill_switch": True})
    if is_db_blocked():
        return (False,
                "GATE_CAP_NOT_HIT: DB-level 24h block active "
                "(set by /admin/sentinel-inbox)",
                {"db_blocked": True})
    used = _merges_this_week()
    cap = _weekly_cap()
    if used >= cap:
        return (False,
                f"GATE_CAP_NOT_HIT: weekly cap {used}/{cap} reached",
                {"used_this_week": used, "weekly_cap": cap})
    return (True,
            f"under cap ({used}/{cap}) + kill switch off",
            {"used_this_week": used, "weekly_cap": cap})


# ── Public API ────────────────────────────────────────────────────────

def should_auto_merge(pr_data: dict) -> tuple[bool, str]:
    """Run all six gates. Return (allowed, reason).

    pr_data is the dict returned by `gh pr list --json
    number,title,body,headRefName,author,createdAt,isDraft,labels,url`.
    """
    pr_number = int(pr_data.get("number") or 0)
    if pr_number <= 0:
        return False, "no pr number"

    # Gate 1: sentinel-derived
    ok, reason, _ = _gate_sentinel_derived(pr_data)
    if not ok:
        return False, reason

    # Gate 2: confidence
    ok, reason, _ = _gate_confidence(pr_data)
    if not ok:
        return False, reason

    # Gate 3-4-5 need the file list
    files = _pr_files(pr_number)
    if not files:
        return False, "GATE_SANDBOX_PATHS: gh pr view returned no files"

    ok, reason, _ = _gate_sandbox_paths(files)
    if not ok:
        return False, reason

    ok, reason, _ = _gate_forbidden_paths(files)
    if not ok:
        return False, reason

    # Gate 3: ast.parse (after sandbox + forbidden so we don't pay for
    # network fetches on rejected-by-path-pattern PRs)
    ok, reason, _ = _gate_ast_parse(pr_number, files)
    if not ok:
        return False, reason

    # Gate 6: cap + kill switch (last so we always see the file gates
    # in dry-run mode, even when the cap is exhausted)
    ok, reason, _ = _gate_cap_and_kill_switch()
    if not ok:
        return False, reason

    return True, "all gates passed"


def _log_decision(pr_data: dict, decision: str, reason: str,
                   extra: dict | None = None) -> None:
    """Best-effort log to sentinel_auto_merge_log. Never raises."""
    extra = extra or {}
    issue = extra.get("issue")
    sentinel_path = extra.get("sentinel_path")
    confidence = extra.get("confidence")
    files_changed = extra.get("files_changed")
    merge_sha = extra.get("merge_sha")
    merged_at = extra.get("merged_at")
    dry_run = bool(extra.get("dry_run"))
    follow_up_at = extra.get("follow_up_at")

    pr_number = int(pr_data.get("number") or 0)
    c = _conn()
    if c is None:
        logger.warning("sentinel_auto_merge: no DB, decision NOT logged "
                       "PR#%s %s: %s", pr_number, decision, reason[:80])
        return
    try:
        with c.cursor() as cur:
            cur.execute(_LOG_SCHEMA)
            cur.execute("""
                INSERT INTO sentinel_auto_merge_log
                  (pr_number, decision, reason, issue, sentinel_path,
                   confidence, files_changed, merge_sha, merged_at,
                   dry_run, follow_up_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (pr_number, decision, reason[:500],
                  issue, sentinel_path,
                  confidence,
                  json.dumps(files_changed) if files_changed is not None else None,
                  merge_sha, merged_at, dry_run, follow_up_at))
    except Exception as e:
        logger.warning("sentinel_auto_merge: decision log failed PR#%s: %s",
                       pr_number, e)
    finally:
        try:
            c.close()
        except Exception:
            pass


def _gather_decision_extras(pr_data: dict) -> dict:
    """Pre-parse fields we want to log alongside every decision so the
    dashboard can show why a PR was accepted or why a near-miss got
    blocked at gate N."""
    body = pr_data.get("body") or ""
    issue, sentinel_path = _find_linked_sentinel_finding(body)
    confidence = _extract_confidence(body)
    files = _pr_files(int(pr_data.get("number") or 0))
    return {
        "issue": issue,
        "sentinel_path": sentinel_path,
        "confidence": confidence,
        "files_changed": [f.get("path") for f in files],
    }


def _merge_pr(pr_number: int) -> tuple[bool, str, str | None]:
    """Real merge via REST API PUT /repos/{repo}/pulls/{n}/merge with
    method=squash. Also marks the PR as ready-for-review first (PRs that
    are draft cannot be merged). Returns (ok, message, merge_sha).
    """
    # 1. Mark ready-for-review (un-draft) via PATCH. Drafts can't merge.
    code, _ = _gh_rest("PATCH",
        f"/repos/{_GITHUB_REPO}/pulls/{pr_number}",
        body={"draft": False}, timeout=20)
    if code not in (200, 201):
        # Try the GraphQL fallback for marking ready (sometimes the REST
        # PATCH draft:false is undocumented). Continue anyway — merge
        # will fail loudly if still in draft.
        pass

    # 2. Squash-merge.
    body = {
        "commit_title": f"[sentinel-auto-merge] PR #{pr_number}",
        "commit_message": (
            "All 6 auto-merge gates passed.\n\n"
            "- GATE_SENTINEL_DERIVED  ok (page_persistent_5xx finding)\n"
            "- GATE_CONFIDENCE_MIN    ok (brain L5 conf >= threshold)\n"
            "- GATE_AST_PARSE         ok (at PR head ref)\n"
            "- GATE_SANDBOX_PATHS     ok (routes/*.py | dchub-frontend/*.html)\n"
            "- GATE_FORBIDDEN_PATHS   ok (no main.py/auth/tier/stripe/etc)\n"
            "- GATE_CAP_NOT_HIT       ok (under weekly cap, kill switch off)\n\n"
            "Follow-up sentinel probe scheduled in 5 min to verify the "
            "page actually heals."),
        "merge_method": "squash",
    }
    code, j = _gh_rest("PUT",
        f"/repos/{_GITHUB_REPO}/pulls/{pr_number}/merge",
        body=body, timeout=60)
    if code not in (200, 201):
        msg = j if isinstance(j, str) else json.dumps(j)[:300]
        return False, msg[:300], None
    sha = None
    if isinstance(j, dict):
        sha = j.get("sha")
    return True, "merged", sha


def run_auto_merge_sweep(force_log_all: bool = True) -> dict:
    """The runner — called from cron + /api/v1/admin/sentinel/auto-merge-eligible.

    Walks open brain-authored draft PRs in the last 7 days. For each:
      - runs should_auto_merge → (allowed, reason)
      - if allowed AND NOT dry-run AND under cap → gh pr merge
      - logs the decision regardless
    """
    _ensure_schema()
    out: dict = {
        "kill_switch":     _kill_switch_on(),
        "dry_run":         _is_dry_run(),
        "weekly_cap":      _weekly_cap(),
        "min_confidence":  _min_conf(),
        "used_this_week":  _merges_this_week(),
        "scanned":         0,
        "allowed":         0,
        "rejected":        0,
        "merged":          0,
        "decisions":       [],
        "ran_at":          datetime.datetime.utcnow().isoformat() + "Z",
    }

    if out["kill_switch"]:
        out["skipped_reason"] = ("kill switch on "
                                  "(SENTINEL_AUTO_MERGE_DISABLE=1)")
        return out

    prs = _list_brain_draft_prs(days=7)
    out["scanned"] = len(prs)

    for pr in prs:
        pr_number = int(pr.get("number") or 0)
        if pr_number <= 0:
            continue
        extras = _gather_decision_extras(pr)
        allowed, reason = should_auto_merge(pr)
        if allowed:
            out["allowed"] += 1
            if _is_dry_run():
                _log_decision(pr, "allow",
                              f"DRY_RUN: would merge — {reason}",
                              {**extras, "dry_run": True})
                out["decisions"].append({
                    "pr": pr_number, "decision": "allow_dry_run",
                    "reason": reason,
                    **{k: extras.get(k) for k in
                       ("issue", "sentinel_path", "confidence")},
                })
            else:
                ok, msg, sha = _merge_pr(pr_number)
                if ok:
                    follow_at = (datetime.datetime.utcnow()
                                 + datetime.timedelta(minutes=5))
                    _log_decision(pr, "allow",
                                  f"AUTO-MERGED — {reason}",
                                  {**extras,
                                   "merge_sha": sha,
                                   "merged_at": datetime.datetime.utcnow().isoformat() + "Z",
                                   "follow_up_at": follow_at.isoformat() + "Z",
                                   "dry_run": False})
                    out["merged"] += 1
                    out["decisions"].append({
                        "pr": pr_number, "decision": "merged",
                        "merge_sha": sha,
                        "follow_up_at": follow_at.isoformat() + "Z",
                        **{k: extras.get(k) for k in
                           ("issue", "sentinel_path", "confidence")},
                    })
                else:
                    _log_decision(pr, "allow_merge_failed",
                                  f"GATE PASS but merge failed: {msg}",
                                  {**extras, "dry_run": False})
                    out["decisions"].append({
                        "pr": pr_number, "decision": "merge_failed",
                        "reason": msg,
                    })
        else:
            out["rejected"] += 1
            if force_log_all:
                _log_decision(pr, "reject", reason, extras)
            out["decisions"].append({
                "pr": pr_number, "decision": "reject",
                "reason": reason,
                **{k: extras.get(k) for k in
                   ("issue", "sentinel_path", "confidence")},
            })

    return out


# ── Follow-up: did the sentinel page actually heal? ───────────────────

def run_follow_up_probes(max_age_min: int = 60) -> dict:
    """Sweep auto-merge rows with follow_up_at <= NOW + few minutes and
    follow_up_outcome IS NULL. Probe the sentinel_path; record grade.

    Outcome buckets:
      success      — page now 2xx → finding resolved → auto-merge worked
      regression   — page still 5xx → auto-merge did NOT heal
      inconclusive — page no longer in sentinel manifest, or fetch failed
    """
    import requests as _rq
    _ensure_schema()
    out: dict = {"checked": 0, "success": 0, "regression": 0,
                 "inconclusive": 0, "errors": []}
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    rows: list[dict] = []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_LOG_SCHEMA)
            cur.execute("""
                SELECT id, pr_number, sentinel_path, merge_sha, follow_up_at
                  FROM sentinel_auto_merge_log
                 WHERE follow_up_at IS NOT NULL
                   AND follow_up_outcome IS NULL
                   AND follow_up_at <= NOW() + INTERVAL '%s minutes'
                   AND merged_at IS NOT NULL
                 ORDER BY follow_up_at ASC
                 LIMIT 50
            """ % int(max_age_min))
            rows = list(cur.fetchall() or [])
    except Exception as e:
        out["error"] = f"select_failed: {e}"
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass

    for row in rows:
        out["checked"] += 1
        path = row.get("sentinel_path") or ""
        if not path:
            _finalize_followup(row["id"], "inconclusive", None, None)
            out["inconclusive"] += 1
            continue
        url = f"{_SITE_BASE}{path}"
        try:
            r = _rq.get(url, timeout=10,
                        headers={"User-Agent": "dchub-sentinel-followup/1.0"},
                        allow_redirects=True)
            sc = r.status_code
        except Exception as e:
            out["errors"].append(f"{path}: {type(e).__name__}: {str(e)[:80]}")
            _finalize_followup(row["id"], "inconclusive", None, None)
            out["inconclusive"] += 1
            continue
        if 200 <= sc < 400:
            _finalize_followup(row["id"], "success", "A", sc)
            out["success"] += 1
        elif 500 <= sc < 600:
            _finalize_followup(row["id"], "regression", "F", sc)
            out["regression"] += 1
        else:
            _finalize_followup(row["id"], "inconclusive",
                                 "C" if 400 <= sc < 500 else "D", sc)
            out["inconclusive"] += 1
    return out


def _finalize_followup(row_id: int, outcome: str, grade: str | None,
                        status_code: int | None) -> None:
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE sentinel_auto_merge_log
                   SET follow_up_outcome = %s,
                       follow_up_sentinel_grade = %s,
                       follow_up_status_code = %s
                 WHERE id = %s
            """, (outcome, grade, status_code, row_id))
    except Exception:
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── Admin auth ────────────────────────────────────────────────────────

def _admin_ok() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if not _ADMIN_KEY:
        return False
    return provided == _ADMIN_KEY


# ── HTTP endpoints ────────────────────────────────────────────────────

@sentinel_auto_merge_bp.route(
    "/api/v1/admin/sentinel/auto-merge-eligible", methods=["POST", "GET"])
def auto_merge_eligible():
    """Run the auto-merge sweep. Logs every decision. Returns the full
    decision list so the operator can sanity-check rejection reasons.
    """
    if not _admin_ok():
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key required"), 401
    result = run_auto_merge_sweep(force_log_all=True)
    return jsonify(result), 200


@sentinel_auto_merge_bp.route(
    "/api/v1/admin/sentinel/auto-merge-followup", methods=["POST", "GET"])
def auto_merge_followup():
    """Run the 5-min-post-merge sentinel follow-up probe."""
    if not _admin_ok():
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key required"), 401
    return jsonify(run_follow_up_probes()), 200


@sentinel_auto_merge_bp.route(
    "/api/v1/admin/sentinel/auto-merge-log", methods=["GET"])
def auto_merge_log_json():
    """Last 30d of decisions for the dashboard tab."""
    if not _admin_ok():
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key required"), 401
    days = max(1, min(90, int(request.args.get("days") or "30")))
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 500
    rows: list[dict] = []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_LOG_SCHEMA)
            cur.execute("""
                SELECT id, pr_number, decision, reason, issue, sentinel_path,
                       confidence, files_changed, merge_sha, merged_at,
                       dry_run, follow_up_at, follow_up_sentinel_grade,
                       follow_up_status_code, follow_up_outcome, decided_at
                  FROM sentinel_auto_merge_log
                 WHERE decided_at > NOW() - INTERVAL '%s days'
                 ORDER BY decided_at DESC
                 LIMIT 500
            """ % days)
            for r in cur.fetchall() or []:
                d = dict(r)
                for k in ("decided_at", "merged_at", "follow_up_at"):
                    if d.get(k) is not None:
                        try:
                            d[k] = d[k].isoformat()
                        except Exception:
                            d[k] = str(d[k])
                rows.append(d)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}:{str(e)[:120]}"), 500
    finally:
        try:
            c.close()
        except Exception:
            pass

    used = _merges_this_week()
    cap = _weekly_cap()
    # Outcome distribution among merged rows in the window
    outcomes = {"success": 0, "regression": 0, "inconclusive": 0, "pending": 0}
    for r in rows:
        if (r.get("decision") == "allow" and r.get("merged_at")
                and not r.get("dry_run")):
            o = r.get("follow_up_outcome")
            if o in outcomes:
                outcomes[o] += 1
            else:
                outcomes["pending"] += 1
    return jsonify(
        rows=rows,
        kill_switch=_kill_switch_on(),
        dry_run=_is_dry_run(),
        weekly_cap=cap,
        used_this_week=used,
        min_confidence=_min_conf(),
        outcomes=outcomes,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    ), 200


@sentinel_auto_merge_bp.route(
    "/api/v1/admin/sentinel/auto-merge-block-24h", methods=["POST"])
def auto_merge_block_24h():
    """One-click 'block auto-merge for 24h'. Writes a sentinel_block row
    so the next sweep (which is at-most twice daily) sees the kill switch
    AS-IF it were set, even without env-var access. Idempotent (latest
    row wins). To clear early, set blocked_until to NULL via SQL."""
    if not _admin_ok():
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key required"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 500
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentinel_auto_merge_block (
                    id              SERIAL PRIMARY KEY,
                    blocked_until   TIMESTAMPTZ NOT NULL,
                    blocked_by      TEXT,
                    note            TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            until = (datetime.datetime.utcnow()
                     + datetime.timedelta(hours=24))
            cur.execute("""
                INSERT INTO sentinel_auto_merge_block
                  (blocked_until, blocked_by, note)
                VALUES (%s, %s, %s)
            """, (until, "admin_one_click",
                  "/admin/sentinel-inbox 24h block button"))
        return jsonify(ok=True,
                       blocked_until=until.isoformat() + "Z"), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}:{str(e)[:120]}"), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


def is_db_blocked() -> bool:
    """Read the soft DB-level block table. Lets the dashboard kill the
    runner without bouncing Railway. Returns True iff there is a row
    with blocked_until > NOW()."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentinel_auto_merge_block (
                    id              SERIAL PRIMARY KEY,
                    blocked_until   TIMESTAMPTZ NOT NULL,
                    blocked_by      TEXT,
                    note            TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                SELECT COUNT(*) FROM sentinel_auto_merge_block
                 WHERE blocked_until > NOW()
            """)
            row = cur.fetchone()
            return bool(row and int(row[0]) > 0)
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


@sentinel_auto_merge_bp.route(
    "/api/v1/admin/sentinel/auto-merge-status", methods=["GET"])
def auto_merge_status():
    if not _admin_ok():
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key required"), 401
    return jsonify(
        kill_switch=_kill_switch_on(),
        db_blocked=is_db_blocked(),
        dry_run=_is_dry_run(),
        weekly_cap=_weekly_cap(),
        min_confidence=_min_conf(),
        used_this_week=_merges_this_week(),
        github_repo=_GITHUB_REPO,
        sandbox_patterns=[p.pattern for p in _SANDBOX_PATTERNS],
        forbidden_patterns=[p.pattern for p in _forbidden_patterns()][:30],
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    ), 200
