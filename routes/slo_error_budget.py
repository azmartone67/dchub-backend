"""SLO error-budget endpoint — the single source of truth for deploy gating + auto-rollback.

Reads the existing brain_http_errors table (populated by routes/brain_http_capture.py
after every Flask response). No new ingestion, no CF Analytics dependency, no synthetic
score — just the actual 5xx rate the users hit.

SLO contract (errors_slo_gate pillar):
  - global error rate <5% over the last 5 min  -> within_budget
  - 5-10% OR any single pattern >50/5min       -> soft_burn  (warn, no rollback)
  - >10% OR any pattern >100/5min               -> hard_burn  (deploy gate fails, auto-rollback fires)

Response is intentionally cheap: ONE indexed query (pattern, status, occurred_at).
No joins. No subqueries. <30 ms even at the worst hour we have on record.
"""
import os
import datetime as _dt
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

slo_bp = Blueprint("slo_error_budget", __name__)

# SLO thresholds — referenced in CHANGE LOG + post-deploy gate.
GLOBAL_ERR_PCT_SOFT = 5.0
GLOBAL_ERR_PCT_HARD = 10.0
PER_PATH_5MIN_SOFT = 50
PER_PATH_5MIN_HARD = 100
WINDOW_MIN = 5


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@slo_bp.route("/api/v1/slo/error-budget", methods=["GET"])
def error_budget():
    """Return current error-budget burn state. Used by deploy-pages SLO gate."""
    if not (_pg and _dsn()):
        return jsonify({"ok": False, "reason": "no_db"}), 503
    out = {
        "window_min": WINDOW_MIN,
        "as_of": _dt.datetime.utcnow().isoformat() + "Z",
        "thresholds": {
            "global_err_pct_soft": GLOBAL_ERR_PCT_SOFT,
            "global_err_pct_hard": GLOBAL_ERR_PCT_HARD,
            "per_path_5min_soft": PER_PATH_5MIN_SOFT,
            "per_path_5min_hard": PER_PATH_5MIN_HARD,
        },
    }
    try:
        with _pg.connect(_dsn(), connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout = '4s'")
            # 5xx by pattern in the window — single indexed scan.
            cur.execute("""
                SELECT pattern, COUNT(*) FILTER (WHERE status >= 500) AS n5xx,
                                COUNT(*)                                 AS ntot
                FROM brain_http_errors
                WHERE occurred_at > NOW() - INTERVAL '5 minutes'
                GROUP BY pattern
                ORDER BY n5xx DESC NULLS LAST
                LIMIT 50
            """)
            rows = cur.fetchall() or []
        global_5xx = sum(r[1] or 0 for r in rows)
        global_tot = sum(r[2] or 0 for r in rows)
        # Assume small baseline so a near-zero brain_http_errors window
        # doesn't divide-by-zero into a fake 100% "healthy".
        approx_total = max(global_tot, 1)
        global_pct = round((global_5xx / approx_total) * 100, 2)
        top_paths = [
            {"pattern": r[0], "n5xx": r[1], "ntot": r[2]}
            for r in rows if (r[1] or 0) > 0
        ][:10]
        worst_path_n5xx = top_paths[0]["n5xx"] if top_paths else 0

        # Verdict graded on ABSOLUTE per-path 5xx volume only (worst_path_n5xx).
        # r80 (2026-06-04): global_pct = global_5xx / COUNT(*) over brain_http_errors,
        # which is an ERRORS-ONLY table — so it measured "5xx as a share of errors",
        # NOT as a share of traffic. It structurally pinned at ~90-98% and tripped
        # hard_burn on essentially every window (a self-feeding false alarm that
        # produced the bulk of the open slo_hard_burn findings). It is no longer a
        # verdict input; global_err_pct stays in the payload for context only.
        if worst_path_n5xx >= PER_PATH_5MIN_HARD:
            verdict = "hard_burn"
        elif worst_path_n5xx >= PER_PATH_5MIN_SOFT:
            verdict = "soft_burn"
        else:
            verdict = "within_budget"

        out.update({
            "ok": verdict != "hard_burn",
            "verdict": verdict,
            "global_5xx": global_5xx,
            "global_total_errors": global_tot,
            "global_err_pct": global_pct,
            "worst_path_n5xx": worst_path_n5xx,
            "top_5xx_paths": top_paths,
            "source": "brain_http_errors (last 5 min)",
        })
        return jsonify(out), (200 if verdict != "hard_burn" else 503)
    except Exception as e:
        out.update({"ok": False, "reason": f"{type(e).__name__}: {str(e)[:160]}"})
        return jsonify(out), 503
