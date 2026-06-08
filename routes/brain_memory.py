"""
Phase ZZZZ-brain-memory (2026-05-18) — Brain L3: remember what worked.

Closes the gap "brain sees the same finding 3 times and re-flags it like
it's new every time." This module persists every (issue → attempted_fix
→ outcome) triple, then exposes a lookup so future brain runs can say
"we've seen this 3 times — last time fix X worked / didn't work."

Table: brain_finding_outcomes
Endpoints:
  POST /api/v1/brain/memory/record    record a fix attempt + outcome
  GET  /api/v1/brain/memory/lookup    given an issue type, return past
                                       attempts + success rate
  GET  /api/v1/brain/memory/stats     overall: how many issues seen N+ times,
                                       which fix templates worked best
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_memory_bp = Blueprint("brain_memory", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_finding_outcomes (
    id                BIGSERIAL PRIMARY KEY,
    issue_type        TEXT NOT NULL,
    finding_url       TEXT,
    finding_detail    TEXT,
    fix_kind          TEXT NOT NULL,        -- 'manual', 'auto_pr', 'config_change', 'cron_added'
    fix_summary       TEXT,
    fix_pr_url        TEXT,
    outcome           TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'success', 'failed', 'partial', 'rolled_back'
    outcome_detail    TEXT,
    attempted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_bfo_issue       ON brain_finding_outcomes(issue_type);
CREATE INDEX IF NOT EXISTS ix_bfo_attempted   ON brain_finding_outcomes(attempted_at DESC);
CREATE INDEX IF NOT EXISTS ix_bfo_outcome     ON brain_finding_outcomes(outcome);
"""

_SCHEMA_INIT_DONE = False

def _ensure_schema():
    global _SCHEMA_INIT_DONE
    if _SCHEMA_INIT_DONE: return
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(_SCHEMA)
            try: c.commit()
            except Exception: pass
            _SCHEMA_INIT_DONE = True
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"brain_memory schema init failed: {e}")


@brain_memory_bp.route("/api/v1/brain/memory/record", methods=["POST"])
def record_outcome():
    """Record a fix attempt and (optionally) its outcome.

    POST body:
      { issue_type: "blueprint_registered_but_not_serving",
        finding_url: "main.py: register_blueprint(industry_pulse_bp)",
        finding_detail: "...",
        fix_kind: "auto_pr" | "manual" | "config_change" | "cron_added",
        fix_summary: "moved to safe zone",
        fix_pr_url: "https://github.com/.../pull/123",
        outcome: "success" | "failed" | "pending" | "rolled_back",
        outcome_detail: "endpoint now returns 200" }
    """
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    _ensure_schema()
    body = request.get_json(silent=True) or {}
    issue = body.get("issue_type")
    fix_kind = body.get("fix_kind", "manual")
    if not issue:
        return jsonify(ok=False, error="issue_type required"), 400

    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                INSERT INTO brain_finding_outcomes
                  (issue_type, finding_url, finding_detail, fix_kind,
                   fix_summary, fix_pr_url, outcome, outcome_detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                RETURNING id
            """, (issue, body.get("finding_url"), body.get("finding_detail"),
                  fix_kind, body.get("fix_summary"), body.get("fix_pr_url"),
                  body.get("outcome", "pending"), body.get("outcome_detail")))
            rid = cur.fetchone()[0]
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    return jsonify(ok=True, id=rid,
                    note="Recorded. Future brain runs will see this in /lookup."), 200


@brain_memory_bp.route("/api/v1/brain/memory/lookup", methods=["GET"])
def lookup():
    """Given ?issue=<issue_type>, return past attempts + success rate.
    Brain detectors can call this before logging a finding — if past
    attempts succeeded with fix X, recommend X immediately."""
    _ensure_schema()
    issue = (request.args.get("issue") or "").strip()
    if not issue:
        return jsonify(ok=False, error="?issue=<issue_type> required"), 400

    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT fix_kind, fix_summary, fix_pr_url, outcome,
                       outcome_detail, attempted_at
                  FROM brain_finding_outcomes
                 WHERE issue_type = %s
                 ORDER BY attempted_at DESC
                 LIMIT 20
            """, (issue,))
            rows = cur.fetchall() or []
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    attempts = []
    success_count = 0
    failed_count = 0
    for r in rows:
        a = {
            "fix_kind":      r[0],
            "fix_summary":   r[1],
            "fix_pr_url":    r[2],
            "outcome":       r[3],
            "outcome_detail": r[4],
            "attempted_at":   r[5].isoformat() if r[5] else None,
        }
        attempts.append(a)
        if r[3] == "success": success_count += 1
        elif r[3] == "failed": failed_count += 1

    success_rate = (success_count / (success_count + failed_count) * 100
                    if (success_count + failed_count) else None)

    # Pick the most-recently-successful fix as the recommendation
    recommended_fix = None
    for a in attempts:
        if a["outcome"] == "success":
            recommended_fix = {
                "kind":    a["fix_kind"],
                "summary": a["fix_summary"],
                "pr_url":  a["fix_pr_url"],
            }
            break

    return jsonify(
        ok=True,
        issue=issue,
        attempt_count=len(attempts),
        success_count=success_count,
        failed_count=failed_count,
        success_rate_pct=round(success_rate, 1) if success_rate is not None else None,
        recommended_fix=recommended_fix,
        attempts=attempts,
    ), 200


@brain_memory_bp.route("/api/v1/brain/memory/backfill-from-commits", methods=["POST", "GET"])
def backfill_from_commits():
    """Phase ZZZZ-T2-memory-bootstrap (2026-05-18): brain memory was
    empty (0 records) because nothing's been writing to it. Bootstrap
    by walking the last N days of git commits and creating one record
    per fix-shaped commit. After this runs once, the auto-narrative
    cron writes new ones going forward.

    Walks GitHub API (no git binary on Railway), filters commits whose
    message starts with 'fix(' or 'feat(' (typical brain-fixable shape),
    creates `outcome='success'` records (assumes the fix shipped + the
    issue resolved — brain can downgrade later if it re-fires)."""
    _ensure_schema()
    days = int(request.args.get("days") or "7")
    try:
        import requests
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat() + "Z"
        h = {"Accept": "application/vnd.github+json"}
        if token: h["Authorization"] = f"Bearer {token}"
        r = requests.get(f"https://api.github.com/repos/{repo}/commits",
                         params={"since": since, "per_page": 100},
                         headers=h, timeout=15)
        if r.status_code != 200:
            return jsonify(ok=False, error=f"GitHub API {r.status_code}: {r.text[:200]}"), 503
        commits = r.json() or []
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    recorded = 0
    skipped = 0
    try:
        c = _conn()
        try:
            cur = c.cursor()
            for cm in commits:
                msg = (cm.get("commit", {}).get("message") or "").split("\n")[0]
                sha = cm.get("sha", "")[:12]
                # Only commits that look like fixes/features
                if not msg.startswith(("fix(", "feat(", "perf(")):
                    skipped += 1
                    continue
                # Extract the parenthesized scope as issue_type
                import re
                m = re.match(r"^(fix|feat|perf)\(([^)]+)\):\s*(.+)$", msg)
                if not m:
                    skipped += 1
                    continue
                kind, scope, summary = m.group(1), m.group(2), m.group(3)
                # Skip if already recorded for this sha
                cur.execute("SELECT 1 FROM brain_finding_outcomes WHERE fix_pr_url LIKE %s LIMIT 1",
                            (f"%{sha}%",))
                if cur.fetchone():
                    skipped += 1
                    continue
                cur.execute("""
                    INSERT INTO brain_finding_outcomes
                      (issue_type, finding_url, fix_kind, fix_summary,
                       fix_pr_url, outcome, outcome_detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (
                    f"commit_scope:{scope}",
                    f"git:{repo}",
                    f"{kind}_commit",
                    summary[:300],
                    f"https://github.com/{repo}/commit/{sha}",
                    "success",  # assumed; can downgrade later
                    f"Auto-recorded from commit {sha}",
                ))
                recorded += 1
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    return jsonify(
        ok=True,
        commits_seen=len(commits),
        recorded=recorded,
        skipped=skipped,
        days_window=days,
        note=("Brain memory bootstrapped from git history. Future fixes "
              "should be recorded via /api/v1/brain/memory/record."),
    ), 200


@brain_memory_bp.route("/api/v1/brain/memory/stats", methods=["GET"])
def stats():
    """Overall view: top recurring issues, top-working fix templates."""
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            # Top recurring issues
            cur.execute("""
                SELECT issue_type, COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE outcome = 'success') AS wins
                  FROM brain_finding_outcomes
                 GROUP BY issue_type
                 ORDER BY n DESC LIMIT 20
            """)
            issue_rows = cur.fetchall() or []
            # Top-working fix kinds
            cur.execute("""
                SELECT fix_kind,
                       COUNT(*) AS attempts,
                       COUNT(*) FILTER (WHERE outcome = 'success') AS wins,
                       ROUND(100.0 * COUNT(*) FILTER (WHERE outcome = 'success')
                                    / NULLIF(COUNT(*), 0), 1) AS win_rate
                  FROM brain_finding_outcomes
                 GROUP BY fix_kind
                 ORDER BY attempts DESC
            """)
            fix_rows = cur.fetchall() or []
            # Headline counts
            cur.execute("SELECT COUNT(*) FROM brain_finding_outcomes")
            total = (cur.fetchone() or [0])[0]
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    return jsonify(
        ok=True,
        total_records=total,
        top_recurring_issues=[
            {"issue_type": r[0], "occurrences": r[1], "wins": r[2]}
            for r in issue_rows
        ],
        fix_kind_performance=[
            {"kind": r[0], "attempts": r[1], "wins": r[2],
             "win_rate_pct": float(r[3]) if r[3] is not None else None}
            for r in fix_rows
        ],
        note=("Brain now has memory. Future detectors should check "
              "/lookup before flagging — if past fix X worked, recommend X."),
    ), 200
