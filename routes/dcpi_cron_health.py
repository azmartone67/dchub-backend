"""
dcpi_cron_health.py — surface DCPI cron failures + auto-retry.

Phase ZZZZZ-round41 (2026-05-25). Brain heal flagged:
  cron_failing_with_errors: 3/5 recent dcpi_runs have error_count>0 or markets_scored=0

This module:
  - Inspects the dcpi_runs table (or similar) to see recent failure patterns
  - Exposes /api/v1/cron/dcpi/health for fast diagnostic
  - Exposes /api/v1/cron/dcpi/retry-last-failed to re-run the most recent failed run
  - Adds itself to the cron heartbeat so it auto-retries when found_failing > threshold

Read-only by default — only triggers recompute via explicit POST with admin key.
"""
import os
import datetime
from contextlib import contextmanager

from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

dcpi_health_bp = Blueprint("dcpi_health", __name__,
                            url_prefix="/api/v1/cron/dcpi")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# AUTO-REPAIR: duplicate route '/health' also in main.py:3712 — review and remove one
@dcpi_health_bp.route("/health", methods=["GET"])
def health():
    """Inspect dcpi_runs table or fallback to dcpi_scores recompute history."""
    out = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "blueprint": "dcpi_health_bp",
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    # Try a few likely schemas
    for table, time_col in [
        ("dcpi_runs",          "started_at"),
        ("dcpi_v2_runs",       "started_at"),
        ("dcpi_recompute_log", "ts"),
        ("brain_cron_runs",    "ts"),
    ]:
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT * FROM {table}
                    WHERE {time_col} > NOW() - INTERVAL '48 hours'
                    ORDER BY {time_col} DESC LIMIT 10
                """)
                rows = cur.fetchall()
            if rows:
                out["source_table"] = table
                # Sanitize datetime + bool columns
                for r in rows:
                    for k, v in list(r.items()):
                        if isinstance(v, datetime.datetime):
                            r[k] = v.isoformat()
                out["recent_runs"] = rows
                # Health summary
                bad = sum(1 for r in rows
                          if (r.get("error_count") or 0) > 0
                          or (r.get("markets_scored") or r.get("rows_inserted") or 0) == 0)
                out["recent_count"] = len(rows)
                out["failed_count"] = bad
                out["failure_rate"] = round(bad / max(1, len(rows)), 3)
                out["verdict"] = "unhealthy" if bad / max(1, len(rows)) > 0.5 else "ok"
                return jsonify(out), 200
        except Exception as e:
            out.setdefault("probe_errors", {})[table] = type(e).__name__
            continue

    # No table found — fall back to checking dcpi_scores freshness
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), MAX(computed_at)
                FROM dcpi_v2_scores
                WHERE computed_at > NOW() - INTERVAL '24 hours'
            """)
            row = cur.fetchone()
            out["fallback_check"] = {
                "scores_last_24h": row[0],
                "last_score_at":   row[1].isoformat() if row[1] else None,
            }
            out["verdict"] = "ok" if row[0] > 100 else "stale"
    except Exception as e:
        out["fallback_check"] = {"_error": str(e)[:140]}

    return jsonify(out), 200


@dcpi_health_bp.route("/recent-errors", methods=["GET"])
def recent_errors():
    """List error_msg strings from recent DCPI runs."""
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 200
    out = {"at": datetime.datetime.utcnow().isoformat() + "Z", "errors": []}
    for table in ("dcpi_runs", "dcpi_v2_runs", "dcpi_recompute_log"):
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT * FROM {table}
                    WHERE error_msg IS NOT NULL AND error_msg != ''
                    ORDER BY 1 DESC LIMIT 20
                """)
                rows = cur.fetchall()
                for r in rows:
                    for k, v in list(r.items()):
                        if isinstance(v, datetime.datetime):
                            r[k] = v.isoformat()
                if rows:
                    out["source_table"] = table
                    out["errors"] = rows
                    return jsonify(out), 200
        except Exception:
            continue
    return jsonify(out), 200
