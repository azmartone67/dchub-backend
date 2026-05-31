"""
brain_proposed_debug.py — read-only admin diagnostic (2026-05-31).

WHY THIS EXISTS
---------------
GET /api/v1/brain/proposed-code/pending-pr (in routes/brain_v2_layer5.py,
proposed_code_pending_pr() ~line 743) returns 0 items even though
high-confidence proposals (confidence 0.85–0.95) persist in
brain_proposed_code_fixes — re-proposing the same fix yields outcome
"duplicate", so the rows ARE there. Auth is valid (other admin endpoints
200) and the base per-source threshold is 0.85, which 0.85–0.95 proposals
clear. The remaining suspect is the pending-pr query filter:

    WHERE pr_url IS NULL AND COALESCE(status,'proposed')='proposed'

plus the in-Python per-source threshold loop. This endpoint surfaces the
RAW shape of the table so a human can see, in one JSON blob, which of those
two gates is silently excluding everything (e.g. status is not 'proposed',
or pr_url is already set, or the confidence column is null/below bar).

It is intentionally a SEPARATE module from brain_v2_layer5.py so it can ship
without touching the live handler. It mirrors brain_v2_layer5's _admin_guard
byte-for-byte and imports ADMIN_KEY the same way. Fully guarded — every path
returns 200 with a diagnostic payload; it must NEVER 500.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

# Import ADMIN_KEY the SAME way brain_v2_layer5 does (from Layer 4), so the
# secret that authenticates the pending-pr endpoint authenticates this one too.
from routes.brain_v2_layer4 import ADMIN_KEY

brain_proposed_debug_bp = Blueprint("brain_proposed_debug", __name__)


def _admin_guard():
    """No-arg admin check → returns an error Response tuple, or None if OK.

    Mirrors brain_v2_layer5._admin_guard byte-for-byte so this diagnostic
    accepts the exact same X-Admin-Key the pending-pr endpoint accepts."""
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key header required"), 401
    return None


@brain_proposed_debug_bp.get("/api/v1/brain/proposed-code/debug-summary")
def proposed_code_debug_summary():
    """Read-only census of brain_proposed_code_fixes. Admin-gated because it
    echoes file paths / proposal metadata. Never 500s: any failure (no DB
    URL, missing table, missing column) degrades to a 200 with an `error`
    field and whatever partial stats were gathered.

    Returns:
      total_rows
      by_status            — COUNT(*) GROUP BY COALESCE(status,'proposed')
      pr_url_null / pr_url_set
      confidence_buckets   — {gte_085, between_05_085, lt_05} (null → lt_05)
      pending_pr_match     — COUNT matching the EXACT pending-pr WHERE
      recent_sample        — 5 newest rows, file_path truncated to 60 chars
    """
    auth_err = _admin_guard()
    if auth_err:
        return auth_err

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "table": "brain_proposed_code_fixes",
        "pending_pr_where": "pr_url IS NULL AND COALESCE(status,'proposed')='proposed'",
        "total_rows": None,
        "by_status": {},
        "pr_url_null": None,
        "pr_url_set": None,
        "confidence_buckets": {"gte_085": None, "between_05_085": None, "lt_05": None},
        "pending_pr_match": None,
        "recent_sample": [],
        "error": None,
    }

    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            out["error"] = "NEON_DATABASE_URL/DATABASE_URL not set"
            return jsonify(out), 200

        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            # --- total_rows ---
            try:
                cur.execute("SELECT COUNT(*) FROM brain_proposed_code_fixes")
                out["total_rows"] = cur.fetchone()[0]
            except Exception as e:
                conn.rollback()
                out["error"] = f"count: {str(e)[:160]}"
                # Table likely doesn't exist — nothing else will work.
                return jsonify(out), 200

            # --- by_status: COUNT(*) GROUP BY COALESCE(status,'proposed') ---
            try:
                cur.execute("""
                    SELECT COALESCE(status, 'proposed') AS s, COUNT(*)
                    FROM brain_proposed_code_fixes
                    GROUP BY COALESCE(status, 'proposed')
                    ORDER BY 2 DESC
                """)
                out["by_status"] = {str(s): int(c) for s, c in cur.fetchall()}
            except Exception as e:
                conn.rollback()
                out["by_status"] = {"_error": str(e)[:160]}

            # --- pr_url null vs set ---
            try:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE pr_url IS NULL)     AS nulls,
                        COUNT(*) FILTER (WHERE pr_url IS NOT NULL)  AS sets
                    FROM brain_proposed_code_fixes
                """)
                nulls, sets = cur.fetchone()
                out["pr_url_null"] = int(nulls or 0)
                out["pr_url_set"] = int(sets or 0)
            except Exception as e:
                conn.rollback()
                # pr_url column may not exist yet → all rows are effectively NULL.
                out["pr_url_null"] = out.get("total_rows")
                out["pr_url_set"] = 0
                out["confidence_buckets"]["_pr_url_note"] = (
                    f"pr_url column missing ({str(e)[:80]}) → treated as all-NULL"
                )

            # --- confidence buckets: >=0.85, 0.5–0.85, <0.5 (null → <0.5) ---
            try:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE confidence >= 0.85)                       AS hi,
                        COUNT(*) FILTER (WHERE confidence >= 0.5 AND confidence < 0.85)   AS mid,
                        COUNT(*) FILTER (WHERE confidence < 0.5 OR confidence IS NULL)    AS lo
                    FROM brain_proposed_code_fixes
                """)
                hi, mid, lo = cur.fetchone()
                out["confidence_buckets"]["gte_085"] = int(hi or 0)
                out["confidence_buckets"]["between_05_085"] = int(mid or 0)
                out["confidence_buckets"]["lt_05"] = int(lo or 0)
            except Exception as e:
                conn.rollback()
                out["confidence_buckets"]["_error"] = str(e)[:160]

            # --- EXACT pending-pr WHERE match count ---
            try:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM brain_proposed_code_fixes
                    WHERE pr_url IS NULL
                      AND COALESCE(status, 'proposed') = 'proposed'
                """)
                out["pending_pr_match"] = int(cur.fetchone()[0])
            except Exception as e:
                conn.rollback()
                # pr_url missing → equivalent to (status proposed) since pr_url IS NULL holds for all.
                try:
                    cur.execute("""
                        SELECT COUNT(*)
                        FROM brain_proposed_code_fixes
                        WHERE COALESCE(status, 'proposed') = 'proposed'
                    """)
                    out["pending_pr_match"] = int(cur.fetchone()[0])
                except Exception as e2:
                    conn.rollback()
                    out["pending_pr_match"] = f"error: {str(e)[:80]} / {str(e2)[:80]}"

            # --- recent_sample: 5 newest rows ---
            # pr_url may not exist; select it defensively via a fallback query.
            sample_sql_with_prurl = """
                SELECT id, loop_name, file_path,
                       COALESCE(status, 'proposed') AS status,
                       (pr_url IS NULL) AS pr_url_is_null,
                       confidence, proposed_at
                FROM brain_proposed_code_fixes
                ORDER BY proposed_at DESC NULLS LAST, id DESC
                LIMIT 5
            """
            sample_sql_no_prurl = """
                SELECT id, loop_name, file_path,
                       COALESCE(status, 'proposed') AS status,
                       TRUE AS pr_url_is_null,
                       confidence, proposed_at
                FROM brain_proposed_code_fixes
                ORDER BY proposed_at DESC NULLS LAST, id DESC
                LIMIT 5
            """
            sample_rows = None
            try:
                cur.execute(sample_sql_with_prurl)
                sample_rows = cur.fetchall()
            except Exception:
                conn.rollback()
                try:
                    cur.execute(sample_sql_no_prurl)
                    sample_rows = cur.fetchall()
                except Exception as e:
                    conn.rollback()
                    out["recent_sample"] = [{"_error": str(e)[:160]}]
                    sample_rows = None

            if sample_rows is not None:
                sample = []
                for r in sample_rows:
                    fp = r[2]
                    if isinstance(fp, str) and len(fp) > 60:
                        fp = fp[:60] + "…"
                    conf = r[5]
                    proposed_at = r[6]
                    sample.append({
                        "id": r[0],
                        "loop_name": r[1],
                        "file_path": fp,
                        "status": r[3],
                        "pr_url_is_null": bool(r[4]),
                        "confidence": float(conf) if conf is not None else None,
                        "proposed_at": proposed_at.isoformat() if proposed_at else None,
                    })
                out["recent_sample"] = sample

        return jsonify(out), 200

    except Exception as e:
        print(f"[brain_proposed_debug] debug-summary: {e}", file=sys.stderr)
        out["error"] = str(e)[:200]
        return jsonify(out), 200
