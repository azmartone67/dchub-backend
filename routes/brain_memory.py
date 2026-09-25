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
from routes._swallowed_writes import note_swallowed_write

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

# Phase ZZZZ-feat3 (2026-06-28) — honest detector-keyed fix memory.
# (b) re-key: episodes were keyed on the git COMMIT scope ('commit_scope:phase-rrr'),
#     but live detectors call /memory/lookup?issue=<detector_issue> (e.g.
#     'paywall_anon_leak'), so lookups MISS. We DUAL-WRITE a new detector_issue
#     column (keeping issue_type untouched so existing consumers/stats don't
#     break) and have /lookup match on EITHER column. Additive, fail-safe.
_SCHEMA_DETECTOR_ISSUE = """
ALTER TABLE brain_finding_outcomes ADD COLUMN IF NOT EXISTS detector_issue TEXT;
CREATE INDEX IF NOT EXISTS ix_bfo_detector_issue ON brain_finding_outcomes(detector_issue);
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
            # Additive dual-write column for the re-key (own try so a failure
            # here can't block the base schema / degrades to legacy behavior).
            try:
                cur.execute(_SCHEMA_DETECTOR_ISSUE)
            except Exception as _e:
                logger.warning(f"brain_memory detector_issue migration skipped: {_e}")
                try: c.rollback()
                except Exception: pass
                cur.execute(_SCHEMA)  # re-run base after rollback so commit below sticks
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

    # (b) re-key: dual-write detector_issue. Callers that know the live
    # detector issue string can pass it explicitly; otherwise default to the
    # issue_type (which, for detector-originated records, already IS the
    # detector issue — only the commit-backfill path differs).
    detector_issue = (body.get("detector_issue") or issue)

    try:
        c = _conn()
        try:
            cur = c.cursor()
            try:
                cur.execute("""
                    INSERT INTO brain_finding_outcomes
                      (issue_type, finding_url, finding_detail, fix_kind,
                       fix_summary, fix_pr_url, outcome, outcome_detail, detector_issue)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    RETURNING id
                """, (issue, body.get("finding_url"), body.get("finding_detail"),
                      fix_kind, body.get("fix_summary"), body.get("fix_pr_url"),
                      body.get("outcome", "pending"), body.get("outcome_detail"),
                      detector_issue))
            except Exception:
                # Fail-safe: if the detector_issue column is somehow absent,
                # degrade to the legacy INSERT so recording never breaks.
                try: c.rollback()
                except Exception: pass
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


def record_verified_resolved(issue, url="", evidence="", *, fix_kind="auto_verified"):
    """Fail-safe, deduped writer for a VERIFIED-RESOLVED finding.

    Writes a brain_finding_outcomes row with outcome='success' carrying the
    recall-gate signature token `sig:<issue>|<url>`. This does two jobs:
      1. DEEPENS the fix-memory (the self-assessment 'memory depth' dimension
         reads verified-resolved outcomes — without these the store is mostly
         'pending' commit-backfill rows).
      2. ARMS recall_gate() — the gate matches a 'success' row whose
         outcome_detail contains `sig:<sig>`; nothing wrote one before, which
         is why #3 was inert.

    Idempotent: at most one success row per (issue, url) so repeated verify
    passes don't pile up (which would also skew fix_kind_performance). Never
    raises — mirrors the module's fail-safe contract. Returns
    'inserted' | 'exists' | 'skipped'. Keep the signature formula identical to
    the autopilot fire side (brain_autopilot.py INV2): sig = f"{issue}|{url}".
    """
    try:
        issue = (issue or "").strip()[:200]   # parity with autopilot fire side + _record_action[:200]
        if not issue:
            return "skipped"
        url = (url or "")[:500]
        detail = f"verified-resolved by outcome verifier; sig:{issue}|{url}"
        ev = (evidence or "")[:300]
        _ensure_schema()
        c = _conn()
        if c is None:
            return "skipped"
        try:
            cur = c.cursor()
            # Dedupe: one success row per (issue,url) is all the gate needs.
            try:
                cur.execute(
                    "SELECT 1 FROM brain_finding_outcomes "
                    "WHERE outcome='success' AND COALESCE(finding_url,'')=%s "
                    "AND (issue_type=%s OR detector_issue=%s) LIMIT 1",
                    (url, issue, issue))
                if cur.fetchone():
                    return "exists"
            except Exception:
                try: c.rollback()
                except Exception: pass
            try:
                cur.execute("""
                    INSERT INTO brain_finding_outcomes
                      (issue_type, finding_url, finding_detail, fix_kind,
                       fix_summary, fix_pr_url, outcome, outcome_detail, detector_issue)
                    VALUES (%s,%s,%s,%s,%s,%s,'success',%s,%s) ON CONFLICT DO NOTHING
                """, (issue, url, ev, fix_kind, "verified_resolved", None, detail, issue))
            except Exception:
                # detector_issue column absent → legacy INSERT (mirror record_outcome).
                try: c.rollback()
                except Exception: pass
                cur.execute("""
                    INSERT INTO brain_finding_outcomes
                      (issue_type, finding_url, finding_detail, fix_kind,
                       fix_summary, fix_pr_url, outcome, outcome_detail)
                    VALUES (%s,%s,%s,%s,%s,%s,'success',%s) ON CONFLICT DO NOTHING
                """, (issue, url, ev, fix_kind, "verified_resolved", None, detail))
            c.commit()
            return "inserted"
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        note_swallowed_write("brain_finding_outcomes", where="brain_memory.record_verified_resolved")
        return "skipped"


@brain_memory_bp.route("/api/v1/brain/memory/lookup", methods=["GET"])
def lookup():
    """Given ?issue=<issue_type>, return past attempts + success rate.
    Brain detectors can call this before logging a finding — if past
    attempts succeeded with fix X, recommend X immediately."""
    _ensure_schema()
    issue = (request.args.get("issue") or "").strip()
    if not issue:
        return jsonify(ok=False, error="?issue=<issue_type> required"), 400

    # (b) re-key: match on EITHER the git scope (issue_type, incl. the
    # 'commit_scope:' prefixed form) OR the live detector issue string
    # (detector_issue). Detectors call ?issue=<detector_issue>; backfilled
    # commit rows store the bare scope in detector_issue, so both resolve.
    try:
        c = _conn()
        try:
            cur = c.cursor()
            try:
                cur.execute("""
                    SELECT fix_kind, fix_summary, fix_pr_url, outcome,
                           outcome_detail, attempted_at
                      FROM brain_finding_outcomes
                     WHERE issue_type = %s
                        OR detector_issue = %s
                        OR issue_type = %s
                     ORDER BY attempted_at DESC
                     LIMIT 20
                """, (issue, issue, f"commit_scope:{issue}"))
            except Exception:
                # Fail-safe: column missing → legacy issue_type-only lookup.
                try: c.rollback()
                except Exception: pass
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


def _real_outcome_for_scope(cur, scope: str):
    """(a) Source a HONEST tri-state outcome for a commit scope from the
    verified outcome feeds instead of assuming 'success'.

    Returns one of: 'success' (a verifier confirmed resolution), 'failed'
    (a verifier confirmed it did NOT resolve / regressed), or 'pending'
    (no verified record exists — a shipped commit is not proof of resolution).

    Best-effort + fail-safe: any error or missing table degrades to 'pending'
    (the honest default), NEVER to a fabricated 'success'.

    Match strategy: the verified feeds key on pattern_name (autopilot_outcomes)
    or file_path/klass (brain_fix_outcomes), neither of which is the git scope.
    The most-reliable available correlation is pattern_name ILIKE the scope, so
    we look for the most recent verified verdict whose pattern mentions the scope.
    Absent a match we stay honest with 'pending'.
    """
    # autopilot_outcomes.succeeded is the tri-state verified signal (NULL =
    # cannot_verify). Take the most recent NON-NULL verdict for a matching
    # pattern. NULL/cannot_verify is treated as "no verdict" → keep looking.
    try:
        cur.execute("""
            SELECT succeeded
              FROM autopilot_outcomes
             WHERE succeeded IS NOT NULL
               AND pattern_name ILIKE %s
             ORDER BY verified_at DESC
             LIMIT 1
        """, (f"%{scope}%",))
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return "success" if row[0] else "failed"
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass

    # brain_fix_outcomes.resolved is the fix-verifier tri-state (NULL =
    # indeterminate). Correlate via file_path/klass mentioning the scope.
    try:
        cur.execute("""
            SELECT resolved
              FROM brain_fix_outcomes
             WHERE resolved IS NOT NULL
               AND (COALESCE(file_path,'') ILIKE %s OR COALESCE(klass,'') ILIKE %s)
             ORDER BY verified_at DESC
             LIMIT 1
        """, (f"%{scope}%", f"%{scope}%"))
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return "success" if row[0] else "failed"
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass

    # No verified record → the honest answer is 'pending', not 'success'.
    return "pending"


@brain_memory_bp.route("/api/v1/brain/memory/backfill-from-commits", methods=["POST", "GET"])
def backfill_from_commits():
    """Phase ZZZZ-T2-memory-bootstrap (2026-05-18): brain memory was
    empty (0 records) because nothing's been writing to it. Bootstrap
    by walking the last N days of git commits and creating one record
    per fix-shaped commit. After this runs once, the auto-narrative
    cron writes new ones going forward.

    Walks GitHub API (no git binary on Railway), filters commits whose
    message starts with 'fix(' or 'feat(' (typical brain-fixable shape).

    (a) HONESTY: outcome is NO LONGER hardcoded 'success'. We source the
    real tri-state outcome from the verified outcome feeds
    (autopilot_outcomes.succeeded / brain_fix_outcomes.resolved):
      · a verified-true  row  → 'success'
      · a verified-false row  → 'failed'
      · no verified record    → 'pending' (a shipped commit is NOT proof the
        finding resolved; only a verifier is).
    """
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
                # (a) HONEST outcome — source the real verified verdict instead
                # of assuming 'success'. (b) re-key — store the bare scope in
                # detector_issue so /lookup?issue=<scope> resolves these rows.
                real_outcome = _real_outcome_for_scope(cur, scope)
                try:
                    cur.execute("""
                        INSERT INTO brain_finding_outcomes
                          (issue_type, finding_url, fix_kind, fix_summary,
                           fix_pr_url, outcome, outcome_detail, detector_issue)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """, (
                        f"commit_scope:{scope}",
                        f"git:{repo}",
                        f"{kind}_commit",
                        summary[:300],
                        f"https://github.com/{repo}/commit/{sha}",
                        real_outcome,
                        f"Auto-recorded from commit {sha}; outcome={real_outcome} "
                        f"(sourced from verified feeds, not assumed)",
                        scope,
                    ))
                except Exception:
                    # Fail-safe: detector_issue column absent → legacy INSERT,
                    # but STILL use the honest outcome (not a fabricated success).
                    try: c.rollback()
                    except Exception: pass
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
                        real_outcome,
                        f"Auto-recorded from commit {sha}; outcome={real_outcome}",
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


# ─────────────────────────────────────────────────────────────────────────
# (c) RECALL GATE — DARK behind BRAIN_MEMORY_RECALL_GATE_ENABLED (default OFF).
#
# Before the run-loop FIRES a finding, it can consult this gate. If memory
# shows a VERIFIED-RESOLVED record for the same issue AND the finding
# signature is unchanged, the gate returns action='escalate_once' so the
# loop escalates ONCE (to a human) instead of re-firing the same finding
# again — the brain learns to STOP, not re-steer.
#
# Fail-safe contract: the flag defaults OFF, and on the flag being OFF OR
# ANY error the gate returns action='fire' (today's behavior). It NEVER
# suppresses a finding unless the flag is explicitly on AND it positively
# confirmed a verified-resolved record with an unchanged signature.
# ─────────────────────────────────────────────────────────────────────────

def _recall_gate_enabled() -> bool:
    return (os.environ.get("BRAIN_MEMORY_RECALL_GATE_ENABLED", "")
            .strip().lower() in ("1", "true", "yes", "on"))


def recall_gate(issue: str, signature: str | None = None) -> dict:
    """Decide whether to fire, or escalate-once, for `issue`.

    Args:
      issue:     the live detector issue string (matches issue_type OR
                 detector_issue OR commit_scope:<issue>).
      signature: an opaque stable hash/string of the CURRENT finding (e.g.
                 url+detail). If a verified-resolved record stored the same
                 signature, the finding is "unchanged" and we escalate-once.
                 If None/empty, signature is treated as unknown → we still
                 only gate on a verified-resolved record (conservative).

    Returns a dict:
      { "action": "fire" | "escalate_once",
        "reason": str,
        "enabled": bool,
        "verified_resolved": bool,
        "signature_unchanged": bool }

    DARK + fail-safe: returns action='fire' whenever the flag is off or
    anything goes wrong. Importable from the run-loop without side effects.
    """
    out = {"action": "fire", "reason": "gate_off_or_default",
           "enabled": False, "verified_resolved": False,
           "signature_unchanged": False}
    try:
        if not _recall_gate_enabled():
            return out
        out["enabled"] = True
        if not issue:
            out["reason"] = "no_issue"
            return out

        _ensure_schema()
        c = _conn()
        try:
            cur = c.cursor()
            # Pull the most recent records for this issue across both keys.
            try:
                cur.execute("""
                    SELECT outcome, outcome_detail, finding_detail, verified_at,
                           attempted_at
                      FROM brain_finding_outcomes
                     WHERE issue_type = %s
                        OR detector_issue = %s
                        OR issue_type = %s
                     ORDER BY COALESCE(verified_at, attempted_at) DESC
                     LIMIT 20
                """, (issue, issue, f"commit_scope:{issue}"))
            except Exception:
                try: c.rollback()
                except Exception: pass
                cur.execute("""
                    SELECT outcome, outcome_detail, finding_detail, NULL, attempted_at
                      FROM brain_finding_outcomes
                     WHERE issue_type = %s
                     ORDER BY attempted_at DESC
                     LIMIT 20
                """, (issue,))
            rows = cur.fetchall() or []
        finally:
            try: c.close()
            except Exception: pass

        # A verified-resolved record = outcome 'success'. (Honest: a 'pending'
        # commit-backfill row is NOT verified, so it never gates.)
        resolved_rows = [r for r in rows if (r[0] or "") == "success"]
        if not resolved_rows:
            out["reason"] = "no_verified_resolved_record"
            return out
        out["verified_resolved"] = True

        # Signature-unchanged check: if a caller-supplied signature matches a
        # signature we stored (we look in outcome_detail / finding_detail for a
        # 'sig:<...>' token, falling back to substring match), the finding is
        # unchanged → escalate-once. With no signature provided, treat as
        # unchanged=True (conservative recall) ONLY when a verified-resolved
        # record exists — the loop still gets a chance to pass a signature to
        # be stricter.
        sig = (signature or "").strip()
        if not sig:
            out["signature_unchanged"] = True
            out["action"] = "escalate_once"
            out["reason"] = "verified_resolved_no_signature_supplied"
            return out

        for r in resolved_rows:
            haystack = " ".join(str(x or "") for x in (r[1], r[2]))
            if (f"sig:{sig}" in haystack) or (sig in haystack):
                out["signature_unchanged"] = True
                out["action"] = "escalate_once"
                out["reason"] = "verified_resolved_and_signature_unchanged"
                return out

        out["reason"] = "verified_resolved_but_signature_changed"
        return out
    except Exception as e:
        # Fail-safe: any error → fire (today's behavior).
        return {"action": "fire", "reason": f"gate_error:{type(e).__name__}",
                "enabled": out.get("enabled", False),
                "verified_resolved": out.get("verified_resolved", False),
                "signature_unchanged": False}


@brain_memory_bp.route("/api/v1/brain/memory/recall-gate", methods=["GET"])
def recall_gate_endpoint():
    """Thin HTTP wrapper over recall_gate() so an HTTP-only run-loop can
    consult the gate: ?issue=<issue>&signature=<sig>. DARK + fail-safe —
    returns action='fire' unless BRAIN_MEMORY_RECALL_GATE_ENABLED is on AND
    a verified-resolved record with an unchanged signature is found."""
    issue = (request.args.get("issue") or "").strip()
    signature = (request.args.get("signature") or "").strip() or None
    if not issue:
        return jsonify(ok=False, error="?issue=<issue> required"), 400
    res = recall_gate(issue, signature)
    return jsonify(ok=True, issue=issue, **res), 200
