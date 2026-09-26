"""
brain_pr_outcome_monitor.py — Brain ROUND 2 (2026-06-07).
=========================================================

PROBLEM
-------
Round 1 (commit dfabb3c4) shipped the Layer-5 PR writer + 5 draft PRs
open (#1031-1035). Round 1 STOPS at "open draft PR". The operator
clicks Merge but the brain never finds out:
  - Did the patch actually deploy to Railway?
  - Did sentinel grade on the touched endpoint go UP, DOWN, or stay
    flat after merge?
  - Should the brain learn "next time I see finding X, DON'T propose
    pattern P because it regressed last time"?

This module closes that loop. Twice a day (10/22 UTC via the SCHEDULE
harness) it:

  1.  Polls GitHub for PRs merged in the last 24h on
      azmartone67/dchub-backend (default repo for Layer-5 drafts).
  2.  For each PR, decides BRAIN-AUTHORED by scanning:
        - PR title contains "[brain-" or "brain-l5"
        - Branch name starts with "brain-v2/" or "brain/fix-"
        - PR body contains "Auto-proposed by Brain v2 Layer 5"
        - Commit message contains "Co-Authored-By: Claude"
  3.  For brain-authored PRs:
        - Pull the merged commit SHA + the files changed
        - Wait 3 min for Railway deploy (skip if deploy is fresh enough)
        - Re-probe sentinel page-integrity → compute before/after grade
        - Write the outcome row (success / regression / deploy_fail /
          unknown)
  4.  Also logs DRAFT-state PRs (never merged but opened by Layer-5)
      so the dashboard shows the full pipeline.
  5.  On a REGRESSION, opens a follow-up brain_finding so the next
      synthesis pass learns the patch was bad.

Endpoints:
  - POST /api/v1/admin/brain/pr-outcomes/monitor-now  (manual trigger)
  - GET  /api/v1/admin/brain/pr-outcomes              (table dump)
  - GET  /api/v1/admin/brain/pr-outcomes/summary      (success/regression %)

Safety:
  - BRAIN_PR_OUTCOME_MONITOR_DISABLE=1 kill switch (default OFF = enabled)
  - Idempotent: PR rows are UNIQUE on pr_number; re-runs UPDATE in place
  - Defensive: every GitHub call wrapped in try/except, never raises
  - Cost: zero — gh CLI is a local subprocess, no Claude calls

Companion: routes/brain_strategic_planner.py (consumes
brain_pr_outcomes in _gather_outcomes_context for the synthesis prompt
so the brain LEARNS from its track record).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shlex
import subprocess
from typing import Any, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_pr_outcome_monitor_bp = Blueprint(
    "brain_pr_outcome_monitor", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_GITHUB_REPO = os.environ.get(
    "GITHUB_REPO", "azmartone67/dchub-backend").strip()
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
_INTERNAL_BASE = (os.environ.get("INTERNAL_BASE_URL")
                  or "http://localhost:8080").rstrip("/")
_RAILWAY_BASE = "https://dchub-backend-production.up.railway.app"

# Markers we use to decide "the brain opened this PR"
_BRAIN_TITLE_MARKERS = (
    "[brain-l5", "[brain-l6", "brain-l5", "brain-l6",
    "[brain ", "brain auto", "brain auto-propose",
)
_BRAIN_BRANCH_MARKERS = (
    "brain-v2/", "brain/fix-", "brain-l5/", "brain-l6/",
)
_BRAIN_BODY_MARKERS = (
    "Auto-proposed by Brain v2 Layer 5",
    "Brain Layer-5 auto-proposed",
    "Brain Layer-6",
)
# ★★★ 2026-09-21 — TWO MARKERS REMOVED, EACH MEASURED MISFIRING.
#
#   "Co-Authored-By: Claude"  Every human-directed Claude Code session PR
#                             carries this trailer. Of 25 misattributed PRs
#                             checked, 24 were flagged by it alone.
#   "brain_pr_opener"         A module NAME. It matches any PR whose body
#                             merely mentions the module — e.g. a fix TO it.
#                             The 25th misattributed PR was flagged by this.
#
# Both matched prose ABOUT the brain rather than work BY it. Read live via
# GET /api/v1/admin/brain/pr-outcomes: of 88 merged brain_authored=TRUE rows,
# only 7 came from a brain pipeline. The other 81 were fix/, feat/, seo/ …
# sessions a human directed — so L6 (brain_strategic_planner) computed its
# success rate and read its "recent PRs" from work it did not do, and
# brain_self_perception believed the same.
#
# ★ VERIFIED SAFE BEFORE REMOVAL: all 7 real pipeline PRs (#4738 #4567 #4220
# #4221 #4222 #4202 #3421) are still detected by title or branch markers
# without these two. Zero real brain PRs lost.
#
# ★ DO NOT ADD BACK A TRAILER, A MODULE NAME, OR ANY SUBSTRING A HUMAN COULD
# WRITE IN PROSE. A body marker must be a signature only a brain writer emits.
# tests/test_brain_authored_markers.py pins this.


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("BRAIN_PR_OUTCOME_MONITOR_DISABLE"))


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ─── GitHub fetch ──────────────────────────────────────────────────

def _gh_api(path: str) -> dict | list | None:
    """Hit the GitHub REST API. Prefers `requests` + GITHUB_TOKEN
    because Railway containers don't have the gh CLI baked in. Falls
    back to gh CLI if it's available locally (dev only)."""
    try:
        import requests
    except Exception:
        requests = None  # type: ignore

    headers = {
        "Accept":              "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":          "dchub-brain-pr-outcome-monitor/1.0",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"

    url = f"https://api.github.com{path}"
    if requests is not None:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.json()
            logger.warning(
                "pr_outcome_monitor: GitHub %s → %s", path, r.status_code)
            return None
        except Exception as e:
            logger.warning(
                "pr_outcome_monitor: GitHub %s exc: %s", path, e)
            return None

    # Fallback: gh CLI (local dev only — Railway lacks it)
    try:
        proc = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, timeout=15)
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
    except Exception as e:
        logger.warning("pr_outcome_monitor: gh CLI fallback exc: %s", e)
    return None


def _list_recent_prs(days: int = 1) -> list[dict]:
    """List PRs touched in the last `days`. Includes draft, open,
    closed, merged. We post-filter for brain authorship + the merged
    timestamp window in `_partition_prs`."""
    out: list[dict] = []
    # PRs sorted by updated DESC. We grab up to 50 (one page) — that
    # covers ~2 days of typical brain activity (5 PRs/day).
    page = _gh_api(
        f"/repos/{_GITHUB_REPO}/pulls?"
        f"state=all&sort=updated&direction=desc&per_page=50")
    if not isinstance(page, list):
        return out
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days * 7)
    for pr in page:
        try:
            updated_at = pr.get("updated_at")
            if not updated_at:
                continue
            dt = _dt.datetime.fromisoformat(
                updated_at.replace("Z", "+00:00"))
            if dt < cutoff:
                continue
            out.append(pr)
        except Exception:
            continue
    return out


def _is_brain_authored(pr: dict) -> bool:
    """Decide whether the PR was opened by one of the brain layers."""
    title = (pr.get("title") or "").lower()
    body = (pr.get("body") or "")
    branch = ((pr.get("head") or {}).get("ref") or "").lower()
    if any(m.lower() in title for m in _BRAIN_TITLE_MARKERS):
        return True
    if any(branch.startswith(m) for m in _BRAIN_BRANCH_MARKERS):
        return True
    if any(m in body for m in _BRAIN_BODY_MARKERS):
        return True
    return False


def _list_pr_files(pr_number: int) -> list[str]:
    """Return list of file paths changed in the PR."""
    data = _gh_api(f"/repos/{_GITHUB_REPO}/pulls/{pr_number}/files")
    if not isinstance(data, list):
        return []
    return [f.get("filename", "") for f in data if f.get("filename")]


# ─── Sentinel probe ────────────────────────────────────────────────

def _http_get_json(path: str, timeout: int = 8) -> dict:
    import urllib.request
    headers = {"X-Internal-Probe": "1",
               "User-Agent": "dchub-brain-pr-outcome/1.0"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
    for base in (_RAILWAY_BASE, _INTERNAL_BASE):
        try:
            req = urllib.request.Request(f"{base}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, (dict, list)) else {}
        except Exception:
            continue
    return {}


def _fetch_sentinel_snapshot() -> dict:
    """Hit /api/v1/sentinel/page-integrity → returns {url: score}."""
    raw = _http_get_json("/api/v1/sentinel/page-integrity")
    out: dict[str, float] = {}
    if not raw:
        return out

    # The endpoint has shifted shape over time. Handle common shapes:
    # 1. {"pages": [{"url": "...", "score": N}, ...]}
    # 2. {"results": [...]}
    # 3. List of pages directly
    candidates = []
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        for k in ("pages", "results", "items", "details"):
            v = raw.get(k)
            if isinstance(v, list):
                candidates = v
                break

    for p in candidates:
        if not isinstance(p, dict):
            continue
        url = (p.get("url") or p.get("path") or "").strip()
        if not url:
            continue
        score = p.get("score") or p.get("grade") or p.get("integrity")
        try:
            out[url] = float(score)
        except (TypeError, ValueError):
            continue
    return out


def _extract_endpoint_from_files(files: list[str]) -> str | None:
    """Guess which sentinel-trackable endpoint a PR touches based on
    file paths. Best-effort — falls back to None which we record as
    'unknown' outcome."""
    # Map common file patterns → routes they likely affect
    for fp in files:
        f = (fp or "").lower()
        # routes/<name>.py → guess /api/v1/<name>/* or /<name>
        if f.startswith("routes/") and f.endswith(".py"):
            stem = f.split("/", 1)[1][:-3]
            # Strip brain_/admin_ prefixes — those aren't user-facing
            if stem.startswith(("brain_", "admin_", "schema_repair",
                                "_proposed_")):
                continue
            # Common 1:1 names
            return f"/{stem.replace('_', '-')}"
        # dchub-frontend/<page>.html → that page URL
        if f.startswith("dchub-frontend/") and f.endswith(".html"):
            page = f.split("/", 1)[1][:-5]
            if page in ("404", "500", "index"):
                continue
            return f"/{page}"
        # Templates likely belong to a backend route
        if f.startswith("templates/") and f.endswith(".html"):
            stem = f.split("/", 1)[1][:-5]
            return f"/{stem}"
    return None


# ─── PR outcome writer ─────────────────────────────────────────────

def _upsert_pr_outcome(row: dict) -> str:
    """Idempotent upsert by pr_number. Returns 'inserted'|'updated'|'skipped'."""
    c = _get_db()
    if c is None:
        return "skipped"
    pr_number = row.get("pr_number")
    if not pr_number:
        return "skipped"
    try:
        with c.cursor() as cur:
            # UPDATE first (idempotent on re-runs); INSERT if no row.
            cur.execute(
                """UPDATE brain_pr_outcomes SET
                    pr_url=COALESCE(%s, pr_url),
                    pr_title=COALESCE(%s, pr_title),
                    branch=COALESCE(%s, branch),
                    merged_at=COALESCE(%s, merged_at),
                    commit_sha=COALESCE(%s, commit_sha),
                    deploy_status=COALESCE(%s, deploy_status),
                    sentinel_endpoint=COALESCE(%s, sentinel_endpoint),
                    sentinel_before_grade=COALESCE(%s, sentinel_before_grade),
                    sentinel_after_grade=COALESCE(%s, sentinel_after_grade),
                    outcome=%s,
                    regression_details=COALESCE(%s, regression_details),
                    files_changed=COALESCE(%s, files_changed),
                    brain_authored=%s,
                    learned_at=NOW(),
                    updated_at=NOW()
                  WHERE pr_number=%s""",
                (row.get("pr_url"), row.get("pr_title"), row.get("branch"),
                 row.get("merged_at"), row.get("commit_sha"),
                 row.get("deploy_status"), row.get("sentinel_endpoint"),
                 row.get("sentinel_before_grade"),
                 row.get("sentinel_after_grade"),
                 row.get("outcome", "unknown"),
                 row.get("regression_details"), row.get("files_changed"),
                 bool(row.get("brain_authored")), pr_number))
            if cur.rowcount and cur.rowcount > 0:
                c.commit()
                return "updated"
            cur.execute(
                """INSERT INTO brain_pr_outcomes
                    (pr_number, pr_url, pr_title, branch, merged_at,
                     commit_sha, deploy_status, sentinel_endpoint,
                     sentinel_before_grade, sentinel_after_grade,
                     outcome, regression_details, files_changed,
                     brain_authored, learned_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT (pr_number) DO NOTHING""",
                (pr_number, row.get("pr_url"), row.get("pr_title"),
                 row.get("branch"), row.get("merged_at"),
                 row.get("commit_sha"), row.get("deploy_status"),
                 row.get("sentinel_endpoint"),
                 row.get("sentinel_before_grade"),
                 row.get("sentinel_after_grade"),
                 row.get("outcome", "unknown"),
                 row.get("regression_details"), row.get("files_changed"),
                 bool(row.get("brain_authored"))))
            c.commit()
            return "inserted"
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("pr_outcome_monitor: upsert failed pr=%s: %s",
                       pr_number, e)
        return "skipped"
    finally:
        try: c.close()
        except Exception: pass


def _file_regression_finding(pr_number: int, endpoint: str,
                              before: float, after: float) -> None:
    """When a brain PR regresses sentinel, write a finding so the next
    strategic synthesis sees it. Uses the canonical writer."""
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return
    c = _get_db()
    if c is None:
        return
    detail = (f"PR #{pr_number} merged but sentinel grade on {endpoint} "
              f"regressed {before:.1f} → {after:.1f}. The Layer-5 patch "
              "appears to have made things worse — review the diff and "
              "consider a revert.")
    try:
        with c.cursor() as cur:
            upsert_brain_finding(
                cur,
                issue="brain_pr_regression",
                url=f"PR#{pr_number}:{endpoint}",
                count=1,
                detail=detail,
                detector="brain_pr_outcome_monitor",
                status="open",
            )
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("pr_outcome_monitor: finding write failed: %s", e)
    finally:
        try: c.close()
        except Exception: pass


# ─── Main monitor ──────────────────────────────────────────────────

def monitor_recent_prs(days: int = 1,
                        wait_for_deploy: bool = False) -> dict:
    """The workhorse. Returns a summary dict for the JSON endpoint /
    cron logger.

    Args:
      days: look-back window in days (24h default)
      wait_for_deploy: if True, sleep ~3 min for Railway deploy. Set
                       to False for the cron path so we don't block
                       the scheduler thread (we'll catch it on the
                       next run when deploy is fresh).
    """
    if _kill_switch_on():
        return {"ok": False, "skipped": "kill_switch_on",
                "kill_switch": "BRAIN_PR_OUTCOME_MONITOR_DISABLE"}

    started = _dt.datetime.now(_dt.timezone.utc)
    prs = _list_recent_prs(days=days)
    sentinel_before = _fetch_sentinel_snapshot()

    # If wait_for_deploy=True and we have at least one merged brain PR,
    # sleep before re-probing sentinel.
    has_merged_brain = any(
        pr.get("merged_at") and _is_brain_authored(pr) for pr in prs)
    waited = False
    if wait_for_deploy and has_merged_brain:
        import time
        time.sleep(180)
        waited = True
    sentinel_after = _fetch_sentinel_snapshot() if waited else sentinel_before

    results = {"draft": 0, "success": 0, "regression": 0,
               "deploy_fail": 0, "unknown": 0, "non_brain": 0}
    detail_rows: list[dict] = []

    for pr in prs:
        try:
            pr_number = pr.get("number")
            if not pr_number:
                continue
            brain_auth = _is_brain_authored(pr)
            if not brain_auth:
                results["non_brain"] += 1
                continue

            merged_at = pr.get("merged_at")
            state = pr.get("state", "open")
            draft = bool(pr.get("draft"))
            files = _list_pr_files(pr_number)
            endpoint = _extract_endpoint_from_files(files)

            row = {
                "pr_number":     pr_number,
                "pr_url":        pr.get("html_url"),
                "pr_title":      (pr.get("title") or "")[:200],
                "branch":        ((pr.get("head") or {}).get("ref") or "")[:200],
                "files_changed": ",".join(files[:20])[:1500],
                "brain_authored": True,
            }

            if not merged_at:
                # Draft or open — log so dashboard shows the pipeline
                row["outcome"] = "draft" if draft else "open"
                _upsert_pr_outcome(row)
                results["draft"] += 1
                detail_rows.append(row)
                continue

            row["merged_at"] = merged_at
            row["commit_sha"] = (pr.get("merge_commit_sha") or "")[:64]

            # Sentinel before/after grade for the touched endpoint
            before_grade = sentinel_before.get(endpoint) if endpoint else None
            after_grade = sentinel_after.get(endpoint) if endpoint else None
            row["sentinel_endpoint"] = endpoint
            row["sentinel_before_grade"] = before_grade
            row["sentinel_after_grade"] = after_grade

            # ★★★ 2026-09-21 — THE SENTINEL NO LONGER GRADES. IT COULD NOT.
            #
            # `sentinel_before` is snapshotted when THIS MONITOR RUN starts,
            # which is up to 24h AFTER the PR merged and deployed — it is never
            # a pre-merge baseline. And unless wait_for_deploy is set,
            # `sentinel_after IS sentinel_before`, the same dict. So every
            # comparison below this line measured a page against itself.
            #
            # Measured on 2026-09-21: 88 of 88 merged rows were `unknown` /
            # `no_baseline`, but only because _extract_endpoint_from_files()
            # guesses a module name (`routes/claim_ledger.py` -> `/claim-ledger`)
            # and 0 of 54 such guesses exist as a sentinel page path. The
            # `unknown` was an accident of a broken join. Fix the join and every
            # PR would have graded `success` against itself — and L6
            # (brain_strategic_planner), which computes its success rate from
            # these rows, would have learned that everything it ships works.
            #
            # So the grades are still RECORDED (observability) but never turned
            # into an outcome. Real outcomes come from grade_recurrences(), which
            # has a genuine before/after: was the same finding targeted again
            # after this PR merged?
            row["outcome"] = "unknown"
            row["deploy_status"] = ("no_pre_merge_baseline" if endpoint
                                    else "no_page_touched")

            _upsert_pr_outcome(row)
            results[row["outcome"]] = results.get(row["outcome"], 0) + 1
            detail_rows.append(row)
        except Exception as e:
            logger.warning("pr_outcome_monitor: pr loop exc: %s", e)

    # Grade by recurrence on every run, so outcomes stay current without
    # anyone remembering to call an endpoint. It only ever upgrades `unknown`
    # to `recurred` on positive evidence, and it cannot break this run.
    try:
        _graded = grade_recurrences(apply=True)
    except Exception as _ge:  # noqa: BLE001
        _graded = {"ok": False, "error": str(_ge)[:200]}
    results["recurred"] = (_graded or {}).get("applied", 0)

    finished = _dt.datetime.now(_dt.timezone.utc)
    return {
        "ok": True,
        "started":  started.isoformat(),
        "finished": finished.isoformat(),
        "duration_s": (finished - started).total_seconds(),
        "scanned":   len(prs),
        "results":   results,
        "waited_for_deploy": waited,
        "details":   detail_rows[:50],
    }


# ─── Endpoints ─────────────────────────────────────────────────────

@brain_pr_outcome_monitor_bp.route(
    "/api/v1/admin/brain/pr-outcomes/monitor-now",
    methods=["POST", "GET"])
def monitor_now():
    """Trigger the monitor immediately. Returns the summary."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        days = int(request.args.get("days", 1))
    except Exception:
        days = 1
    wait = _truthy(request.args.get("wait", "0"))
    out = monitor_recent_prs(days=days, wait_for_deploy=wait)
    return jsonify(out)


# ─── Outcome grading by RECURRENCE (2026-09-21) ────────────────────
#
# The sentinel cannot grade (see the note in monitor_recent_prs). This is the
# signal that CAN: a brain PR names the finding it targets — brain_backlog_admin
# writes `**Finding:** `<issue_key>`` into the body — so if the SAME finding is
# targeted again by a PR opened AFTER this one merged, this fix did not hold.
#
# Measured over all 14 merged [brain-l5 draft] PRs: 4 name a finding, 1
# recurred — #4202 fixed cf_cache_rate_low, and #4567 re-targeted it five days
# later. Small, but it is a genuine before/after, and the first real outcome
# L6 has had.
#
# ★ WHAT THIS DELIBERATELY DOES NOT DO:
#   · It never grades `success`. "Not re-targeted" is absence of evidence —
#     true of every PR merged yesterday — and turning it into success is the
#     exact inflation this module just stopped. Only a positive signal grades.
#   · It never overwrites a real grade: only `unknown` becomes `recurred`.
#   · Reverts are NOT detected. Reverts in this repo are hand-written prose —
#     #4505 "Revert the /api/cron/daily gate" undoes #4432 and names it only in
#     a sentence — so a detector would report "not reverted" for PRs that were.

_FINDING_LINE_RE = re.compile(r"^\*\*Finding:\*\*\s*`([^`]+)`", re.M)
_GRADE_MAX = 150


def extract_finding(body) -> str:
    """The `**Finding:** `<key>`` a brain PR names, or "" if it names none."""
    m = _FINDING_LINE_RE.search(body or "")
    return m.group(1).strip() if m else ""


def recurrence_plan(prs) -> dict:
    """{pr_number: later_pr_number} for each MERGED PR whose finding was
    targeted again by a PR OPENED after it merged. Pure.

    prs: [{number, finding, created_at, merged_at}] — ISO-8601 UTC strings,
    which compare correctly as strings. The later PR may be open or merged:
    being opened at all is the finding coming back.
    """
    groups = {}
    for p in prs or []:
        f = (p.get("finding") or "").strip()
        if f:
            groups.setdefault(f, []).append(p)
    out = {}
    for group in groups.values():
        for p in group:
            merged = p.get("merged_at")
            if not merged:
                continue
            later = sorted((q for q in group
                            if q is not p and (q.get("created_at") or "") > merged),
                           key=lambda q: q.get("created_at") or "")
            if later:
                out[p["number"]] = later[0]["number"]
    return out


class _NoDatabase(Exception):
    """_get_db() returned None."""


# ★★★ 2026-09-21 — NEVER HOLD A DB CONNECTION ACROSS NETWORK I/O.
#
# main.get_pg_connection() tracks every checkout, and a reaper force-closes any
# connection held longer than ~60s. Measured on production: reclassify?apply=1
# took its connection at the top of the handler, spent 61s fetching 150 PRs from
# GitHub, and the log read
#     FORCED RECLAIM: Connection ... held 61s by thread 'ThreadPoolExecutor-0_15'
#        Checkout stack: ... line 779, in reclassify -> c = _get_db()
#     Connection ... forcibly reclaimed and closed (main pool)
# — then its UPDATE ran on the dead connection and the endpoint returned 500.
# Nothing was written (L6's track record was unchanged before/after). The dry
# run looked healthy only because it never touched the connection again after
# the fetches; the write path is the one that needs it afterwards.
#
# So every DB touch here is its own short checkout: read and release, do the
# slow GitHub work with NOTHING held, then check out a FRESH connection to write.

def _db_read(sql: str, params) -> list:
    c = _get_db()
    if c is None:
        raise _NoDatabase("no database")
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() or []
    finally:
        try:
            c.close()
        except Exception:
            pass


def _db_write(statements) -> int:
    """[(sql, params), ...] on ONE short checkout, ONE commit. Returns rowcount."""
    c = _get_db()
    if c is None:
        raise _NoDatabase("no database")
    try:
        n = 0
        with c.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params)
                n += cur.rowcount or 0
        c.commit()
        return n
    finally:
        try:
            c.close()
        except Exception:
            pass


def grade_recurrences(apply: bool = False, limit: int = _GRADE_MAX) -> dict:
    """Grade brain PRs by recurrence. Dry run unless apply=True. Never raises.

    No DB connection is held while GitHub is queried — see _db_read.
    """
    try:
        # ★ 2026-09-21 — FETCH ONLY THE PRs THAT CAN CARRY A FINDING.
        #
        # The first version selected every brain_authored row (150) and made
        # one GitHub call per row. Called at the origin it answered HTTP 200
        # after 17s — past the edge's ~15s, so the public endpoint 503'd —
        # with `naming_a_finding: 0` and nearly every PR in `unfetched`,
        # including the three real L5 PRs it exists to grade. It had graded
        # nothing in production, and because it also runs inside
        # monitor_recent_prs it spent the same GitHub budget the monitor
        # needs to list new PRs.
        #
        # Only ONE producer writes a `**Finding:**` line —
        # brain_backlog_admin.py, and every PR it opens is titled
        # `[brain-l5 draft] …` (verified by grepping origin/main for the
        # literal). A PR without that title cannot name a finding, so it
        # can neither be graded nor act as the later re-target. Narrowing
        # here loses nothing and cuts ~150 calls to ~15.
        #
        # `[` is literal in Postgres LIKE; the pattern has no `_` wildcard.
        rows = _db_read("""SELECT pr_number, outcome FROM brain_pr_outcomes
                            WHERE brain_authored = TRUE
                              AND pr_title LIKE '[brain-l5%%'
                            ORDER BY pr_number DESC LIMIT %s""",
                        (max(1, min(int(limit), _GRADE_MAX)),))
        stored = {int(r[0]): r[1] for r in rows}
        prs, unfetched = [], []
        for n in stored:                       # NOTHING is checked out here
            pr = _gh_api(f"/repos/{_GITHUB_REPO}/pulls/{n}")
            if not isinstance(pr, dict) or not pr:
                unfetched.append(n)
                continue
            prs.append({"number": n, "finding": extract_finding(pr.get("body")),
                        "created_at": pr.get("created_at"),
                        "merged_at": pr.get("merged_at")})
        plan = recurrence_plan(prs)
        # Only `unknown` is ever upgraded — a real grade is never overwritten.
        todo = {n: l for n, l in plan.items() if stored.get(n) == "unknown"}
        applied = 0
        if apply and todo:
            stmts = []
            for n, later in todo.items():
                f = next((p["finding"] for p in prs if p["number"] == n), "")
                stmts.append(("""UPDATE brain_pr_outcomes
                                    SET outcome = 'recurred',
                                        regression_details = %s
                                  WHERE pr_number = %s AND outcome = 'unknown'""",
                              (f"finding {f[:120]} re-targeted by #{later} "
                               f"after this PR merged", n)))
            applied = _db_write(stmts)         # fresh checkout, after the fetches
        return {"ok": True, "apply": bool(apply), "checked": len(stored),
                "naming_a_finding": sum(1 for p in prs if p["finding"]),
                "recurred": len(todo), "applied": applied,
                "recurred_prs": {str(k): v for k, v in list(todo.items())[:100]},
                "unfetched": unfetched[:50]}
    except _NoDatabase:
        return {"ok": False, "state": "UNMEASURED", "error": "no database"}
    except Exception as e:  # noqa: BLE001
        logger.warning("pr_outcome_monitor: grade_recurrences failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


@brain_pr_outcome_monitor_bp.route(
    "/api/v1/admin/brain/pr-outcomes/grade", methods=["POST"])
def grade_endpoint():
    """Inspect or run recurrence grading. Dry run by default; ?apply=1 writes."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(grade_recurrences(apply=_truthy(request.args.get("apply", "0"))))


_RECLASSIFY_MAX = 150   # GitHub fetch per row; keeps one call inside the
                        # admin POST budget. Idempotent — re-run to continue.


def reclassify_plan(rows_to_prs) -> dict:
    """Split stored brain_authored=TRUE rows by what _is_brain_authored says NOW.

    PURE over its input: `rows_to_prs` is [(pr_number, pr_dict_or_None)].
    ★ A PR we could not fetch is UNMEASURED and is NEVER flipped — failing to
    read it is not evidence that it is not the brain's. Only TRUE -> FALSE is
    ever proposed; this repairs one known error, it does not re-derive the flag
    from scratch.
    """
    keep, flip, unmeasured = [], [], []
    for n, pr in rows_to_prs:
        if not isinstance(pr, dict) or not pr:
            unmeasured.append(n)
        elif _is_brain_authored(pr):
            keep.append(n)
        else:
            flip.append(n)
    return {"keep": keep, "flip": flip, "unmeasured": unmeasured}


@brain_pr_outcome_monitor_bp.route(
    "/api/v1/admin/brain/pr-outcomes/reclassify", methods=["POST"])
def reclassify():
    """Re-run the CURRENT _is_brain_authored() over stored rows and clear the
    flag on the ones it now rejects.

    ★ WHY THIS EXISTS. Removing the two misfiring markers corrects only rows
    the monitor writes FROM NOW ON. The 81 historical rows it mislabelled stay
    wrong — and they are exactly what L6 reads as "my last 25 PRs". The table
    does not store the PR body, so the rule cannot be re-evaluated from stored
    columns; this re-fetches each PR and runs the SAME function the monitor
    uses — one definition, not a second SQL approximation of it.

    DRY RUN BY DEFAULT. ?apply=1 writes. Only TRUE -> FALSE, only for PRs that
    were actually fetched. ?limit= caps GitHub calls per request (max 150).
    """
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    apply_ = _truthy(request.args.get("apply", "0"))
    try:
        limit = max(1, min(int(request.args.get("limit", _RECLASSIFY_MAX)),
                           _RECLASSIFY_MAX))
    except Exception:
        limit = _RECLASSIFY_MAX
    try:
        rows = _db_read("""SELECT pr_number FROM brain_pr_outcomes
                            WHERE brain_authored = TRUE
                            ORDER BY pr_number DESC LIMIT %s""", (limit,))
        numbers = [int(r[0]) for r in rows]
        # NOTHING is checked out during these fetches — see _db_read.
        fetched = [(n, _gh_api(f"/repos/{_GITHUB_REPO}/pulls/{n}"))
                   for n in numbers]
        plan = reclassify_plan(fetched)
        applied = 0
        if apply_ and plan["flip"]:
            applied = _db_write([("""UPDATE brain_pr_outcomes
                                        SET brain_authored = FALSE
                                      WHERE pr_number = ANY(%s)
                                        AND brain_authored = TRUE""",
                                  (plan["flip"],))])
        return jsonify(ok=True, apply=apply_, checked=len(numbers),
                       keep=len(plan["keep"]), flip=len(plan["flip"]),
                       unmeasured=len(plan["unmeasured"]), applied=applied,
                       flip_prs=plan["flip"][:200],
                       unmeasured_prs=plan["unmeasured"][:50],
                       note=("dry run — pass ?apply=1 to write" if not apply_
                             else f"cleared brain_authored on {applied} row(s)"))
    except _NoDatabase:
        return jsonify(ok=False, state="UNMEASURED", error="no database"), 503
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)[:200]), 500


@brain_pr_outcome_monitor_bp.route(
    "/api/v1/admin/brain/pr-outcomes", methods=["GET"])
def list_outcomes():
    """Table dump for the dashboard."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, pr_number, pr_url, pr_title, branch,
                          merged_at, commit_sha, deploy_status,
                          sentinel_endpoint, sentinel_before_grade,
                          sentinel_after_grade, outcome,
                          regression_details, files_changed,
                          brain_authored, created_at, updated_at
                     FROM brain_pr_outcomes
                    ORDER BY COALESCE(merged_at, created_at) DESC
                    LIMIT 100""")
            cols = ["id", "pr_number", "pr_url", "pr_title", "branch",
                    "merged_at", "commit_sha", "deploy_status",
                    "sentinel_endpoint", "sentinel_before_grade",
                    "sentinel_after_grade", "outcome",
                    "regression_details", "files_changed",
                    "brain_authored", "created_at", "updated_at"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # Coerce datetimes for JSON
        for r in rows:
            for k in ("merged_at", "created_at", "updated_at"):
                if r.get(k):
                    r[k] = str(r[k])
            for k in ("sentinel_before_grade", "sentinel_after_grade"):
                if r.get(k) is not None:
                    try: r[k] = float(r[k])
                    except Exception: pass
        return jsonify(ok=True, count=len(rows), outcomes=rows)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@brain_pr_outcome_monitor_bp.route(
    "/api/v1/admin/brain/pr-outcomes/summary", methods=["GET"])
def summary():
    """Aggregate success/regression rate for the dashboard headline."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT outcome, COUNT(*)
                     FROM brain_pr_outcomes
                    WHERE brain_authored = TRUE
                      AND created_at > NOW() - INTERVAL '30 days'
                    GROUP BY outcome""")
            by_outcome = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute(
                """SELECT COUNT(*) FROM brain_pr_outcomes
                    WHERE brain_authored = TRUE
                      AND outcome = 'regression'
                      AND created_at > NOW() - INTERVAL '7 days'""")
            recent_regressions = int((cur.fetchone() or [0])[0])
        total = sum(by_outcome.values())
        merged = sum(by_outcome.get(k, 0)
                     for k in ("success", "regression", "unknown",
                               "deploy_fail", "recurred"))
        success = by_outcome.get("success", 0)
        regression = by_outcome.get("regression", 0)
        return jsonify(
            ok=True,
            window_days=30,
            total=total,
            merged=merged,
            by_outcome=by_outcome,
            success_rate=(round(success / merged, 3) if merged else None),
            regression_rate=(round(regression / merged, 3) if merged else None),
            recent_regressions_7d=recent_regressions,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@brain_pr_outcome_monitor_bp.route(
    "/api/v1/admin/brain/pr-outcomes/health", methods=["GET"])
def health():
    """Quick health probe."""
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        github_token_set=bool(_GITHUB_TOKEN),
        repo=_GITHUB_REPO,
        kill_env="BRAIN_PR_OUTCOME_MONITOR_DISABLE",
    )
