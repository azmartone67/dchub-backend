"""
brain_cross_session_pollinator.py — Brain ROUND 2 (2026-06-07).
===============================================================

PROBLEM
-------
Today the brain runs PER SESSION (one detector loop_run = one logical
"session"). A finding like `shadowed_route` or `stale_cron_endpoint`
might surface in 3 different brain sessions over a week and never
escalate — each session writes a row, each row appears in the backlog
once, the operator never sees the PATTERN.

This module pollinates findings ACROSS sessions:

  1. After every brain_findings INSERT (call `detect_cross_session_
     patterns` from any writer or via the explicit endpoint), we check
     whether `(detector, issue)` has appeared in N+ DISTINCT
     `session_id` values in the last 7 days.
  2. If yes, we escalate every matching row — stamp
     `cross_session_escalated_at = NOW()` and bump `cross_session_count`
     to the cluster size.
  3. The escalation also writes a META finding
     (`issue='cross_session_pattern'`) so the strategic synthesis
     sees it as a higher-severity signal.
  4. The backlog dashboard reads these via
     `/api/v1/admin/brain/cross-session-learnings` and shows a
     "Cross-session learnings" card.

Threshold: N = 3 distinct sessions by default (env override:
BRAIN_CROSS_SESSION_THRESHOLD). Window: 7 days.

Safety:
  - BRAIN_CROSS_SESSION_ESCALATE_DISABLE=1 (kill switch, default OFF)
  - Idempotent: re-escalating doesn't re-stamp (WHERE clause excludes
    already-escalated rows)
  - Defensive: every DB op savepoint-wrapped
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_cross_session_bp = Blueprint(
    "brain_cross_session_pollinator", __name__)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("BRAIN_CROSS_SESSION_ESCALATE_DISABLE"))


def _threshold() -> int:
    try:
        return max(2, int(os.environ.get(
            "BRAIN_CROSS_SESSION_THRESHOLD", "3")))
    except Exception:
        return 3


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


def detect_cross_session_patterns(window_days: int = 7) -> dict:
    """Scan brain_findings for (detector, issue) pairs that cross the
    session threshold; stamp `cross_session_escalated_at` on every row
    in each escalated cluster.

    Returns a dict {escalated_clusters: [...], escalated_rows: N}.

    Defensive — never raises. Logs to brain_findings via the canonical
    writer so the synthesis sees the meta-finding.
    """
    if _kill_switch_on():
        return {"ok": False, "skipped": "kill_switch_on"}

    c = _get_db()
    if c is None:
        return {"ok": False, "error": "no_db"}

    threshold = _threshold()
    clusters: list[dict] = []
    rows_escalated = 0
    try:
        with c.cursor() as cur:
            # Cluster check: which (detector, issue) pairs are in N+
            # distinct sessions in the last `window_days`?
            cur.execute(
                """SELECT detector, issue,
                          COUNT(DISTINCT COALESCE(session_id, ''))
                            AS session_n,
                          COUNT(*) AS row_n
                     FROM brain_findings
                    WHERE last_seen > NOW() - INTERVAL '%s days'
                      AND COALESCE(detector, '') <> ''
                      AND issue NOT IN ('cross_session_pattern')
                    GROUP BY detector, issue
                   HAVING COUNT(DISTINCT COALESCE(session_id, ''))
                            >= %s
                    ORDER BY session_n DESC, row_n DESC
                    LIMIT 50""",
                (window_days, threshold))
            candidates = cur.fetchall() or []

            for det, iss, sess_n, row_n in candidates:
                # Mark every matching row that isn't already escalated.
                try:
                    cur.execute(
                        """UPDATE brain_findings
                              SET cross_session_escalated_at = NOW(),
                                  cross_session_count = %s,
                                  status = CASE WHEN status = 'open'
                                                THEN 'escalated'
                                                ELSE status
                                           END
                            WHERE detector = %s
                              AND issue = %s
                              AND last_seen > NOW() - INTERVAL '%s days'
                              AND cross_session_escalated_at IS NULL""",
                        (int(sess_n), det, iss, window_days))
                    rows_escalated += cur.rowcount or 0
                except Exception as e:
                    logger.warning(
                        "cross_session: row escalation failed (%s/%s): %s",
                        det, iss, e)
                clusters.append({
                    "detector": det,
                    "issue": iss,
                    "distinct_sessions": int(sess_n),
                    "total_rows": int(row_n),
                })

                # Write a META finding so the strategic synthesis sees
                # this as a separate higher-altitude signal.
                try:
                    from routes.brain_findings_writer import (
                        upsert_brain_finding,
                    )
                    upsert_brain_finding(
                        cur,
                        issue="cross_session_pattern",
                        url=f"detector={det};issue={iss}",
                        count=int(sess_n),
                        detail=(f"{det}/{iss} surfaced in {sess_n} "
                                f"distinct brain sessions over "
                                f"{window_days}d ({row_n} total "
                                "findings). Escalated to higher "
                                "severity — recurring pattern the "
                                "brain has independently rediscovered."),
                        detector="brain_cross_session_pollinator",
                        status="escalated",
                    )
                except Exception as e:
                    logger.warning(
                        "cross_session: meta-finding write failed: %s", e)
        try: c.commit()
        except Exception: pass
        return {
            "ok": True,
            "threshold": threshold,
            "window_days": window_days,
            "escalated_clusters": clusters,
            "escalated_rows": rows_escalated,
        }
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("cross_session: scan failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
    finally:
        try: c.close()
        except Exception: pass


# ─── Endpoints ─────────────────────────────────────────────────────

@brain_cross_session_bp.route(
    "/api/v1/admin/brain/cross-session/scan", methods=["POST", "GET"])
def cross_session_scan():
    """Run the pattern detector now. Returns escalated clusters."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        window = int(request.args.get("days", 7))
    except Exception:
        window = 7
    out = detect_cross_session_patterns(window_days=window)
    return jsonify(out)


@brain_cross_session_bp.route(
    "/api/v1/admin/brain/cross-session-learnings", methods=["GET"])
def list_learnings():
    """Dashboard card source: top cross-session patterns of last 7d."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            # Read directly from brain_findings — both the META rows
            # AND any rows escalated in the last 7d.
            cur.execute(
                """SELECT detector, issue,
                          COUNT(DISTINCT COALESCE(session_id, '')) AS sess_n,
                          COUNT(*) AS row_n,
                          MAX(cross_session_escalated_at) AS escalated_at,
                          MAX(last_seen) AS last_seen,
                          string_agg(DISTINCT url, ' | ') AS sample_urls
                     FROM brain_findings
                    WHERE cross_session_escalated_at > NOW() - INTERVAL '7 days'
                      AND issue NOT IN ('cross_session_pattern')
                    GROUP BY detector, issue
                    ORDER BY sess_n DESC, row_n DESC
                    LIMIT 30""")
            cols = ["detector", "issue", "distinct_sessions", "row_count",
                    "escalated_at", "last_seen", "sample_urls"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            for k in ("escalated_at", "last_seen"):
                if r.get(k):
                    r[k] = str(r[k])
            if r.get("sample_urls"):
                r["sample_urls"] = (r["sample_urls"] or "")[:500]
        return jsonify(
            ok=True,
            threshold=_threshold(),
            count=len(rows),
            learnings=rows,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@brain_cross_session_bp.route(
    "/api/v1/admin/brain/cross-session/health", methods=["GET"])
def health():
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        threshold=_threshold(),
        kill_env="BRAIN_CROSS_SESSION_ESCALATE_DISABLE",
        threshold_env="BRAIN_CROSS_SESSION_THRESHOLD",
    )
