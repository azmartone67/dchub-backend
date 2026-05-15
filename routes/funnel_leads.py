"""
funnel_leads.py — Phase MM (2026-05-15) Bundle 9: MCP funnel recovery.

The MCP upgrade funnel was converting 0.05% (4 paid out of 8,285 upgrade
signals in 30d). Diagnosis: 240+ free users keep hitting paid tools but
nobody has ever emailed them. This module surfaces the hot leads so we
can target them directly.

Endpoints:
    GET /api/v1/admin/funnel-leads      — top free users by cap hits
                                          (admin-gated, returns emails)
    GET /api/v1/admin/funnel-leads.csv  — same data as CSV download
    POST /api/v1/admin/funnel-leads/broadcast — fire a targeted email
                                          broadcast to the top N hot leads
                                          (admin-gated, requires confirm)

Cohort logic:
    Hot lead = a user whose API key triggered ≥3 upgrade_signal events
    in the last 30 days OR ≥10 cap-exceeded responses. The list is
    sorted by total signals descending so the most-engaged users
    surface first.
"""
import os
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request, Response

funnel_leads_bp = Blueprint("funnel_leads", __name__)

ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("ADMIN_KEY")


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key") or
                    request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return w


def _build_lead_query(days: int = 30, min_signals: int = 3, limit: int = 100):
    """Join mcp_upgrade_signals with mcp_dev_keys to surface emails."""
    return f"""
        WITH signals AS (
            SELECT api_key_id,
                   tool_name,
                   COUNT(*) AS signal_count,
                   MAX(created_at) AS last_signal_at,
                   array_agg(DISTINCT tool_name) AS tools
              FROM mcp_upgrade_signals
             WHERE created_at > NOW() - INTERVAL '{int(days)} days'
               AND api_key_id IS NOT NULL
          GROUP BY api_key_id, tool_name
        ),
        rolled AS (
            SELECT api_key_id,
                   SUM(signal_count) AS total_signals,
                   MAX(last_signal_at) AS last_at,
                   array_agg(DISTINCT tool_name ORDER BY tool_name) AS tools_hit
              FROM signals
          GROUP BY api_key_id
            HAVING SUM(signal_count) >= {int(min_signals)}
        )
        SELECT k.id, k.email, k.tier, k.name,
               r.total_signals, r.last_at, r.tools_hit,
               k.created_at AS key_created
          FROM rolled r
          JOIN mcp_dev_keys k ON k.id = r.api_key_id
         WHERE COALESCE(k.tier, 'free') IN ('free', 'identified')
           AND k.email IS NOT NULL AND k.email <> ''
      ORDER BY r.total_signals DESC, r.last_at DESC
         LIMIT {int(limit)};
    """


@funnel_leads_bp.route("/api/v1/admin/funnel-leads", methods=["GET"])
@_require_admin
def get_funnel_leads():
    """Top free users hitting paid-tool walls. Returns JSON for the dashboard.

    Query params:
        days          (default 30)  — lookback window
        min_signals   (default 3)   — minimum upgrade signals to qualify
        limit         (default 100) — max rows returned
    """
    try:
        days = min(int(request.args.get("days") or 30), 90)
        min_signals = max(int(request.args.get("min_signals") or 3), 1)
        limit = min(int(request.args.get("limit") or 100), 500)
    except Exception:
        days, min_signals, limit = 30, 3, 100

    out = {"ok": True, "days": days, "min_signals": min_signals,
           "leads": [], "as_of": datetime.now(timezone.utc).isoformat()}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_build_lead_query(days, min_signals, limit))
            for r in cur.fetchall():
                out["leads"].append({
                    "api_key_id": r[0],
                    "email": r[1],
                    "tier": r[2] or 'free',
                    "name": r[3],
                    "total_signals": int(r[4] or 0),
                    "last_signal_at": r[5].isoformat() if r[5] else None,
                    "tools_hit": list(r[6]) if r[6] else [],
                    "key_created_at": r[7].isoformat() if r[7] else None,
                })
            # Roll-up stats
            cur.execute(f"""SELECT COUNT(DISTINCT api_key_id),
                                    SUM(1)::int
                              FROM mcp_upgrade_signals
                             WHERE created_at > NOW() - INTERVAL '{int(days)} days'
                               AND api_key_id IS NOT NULL""")
            row = cur.fetchone()
            out["totals"] = {
                "distinct_users_signaled": int(row[0]) if row and row[0] else 0,
                "total_signals": int(row[1]) if row and row[1] else 0,
            }
            cur.execute("""SELECT tool_name, COUNT(*) AS n
                             FROM mcp_upgrade_signals
                            WHERE created_at > NOW() - INTERVAL '30 days'
                              AND tool_name IS NOT NULL
                         GROUP BY tool_name ORDER BY n DESC LIMIT 10""")
            out["top_tools_30d"] = [
                {"tool": r[0], "signals": int(r[1])} for r in cur.fetchall()
            ]
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    return jsonify(out), 200


@funnel_leads_bp.route("/api/v1/admin/funnel-leads.csv", methods=["GET"])
@_require_admin
def get_funnel_leads_csv():
    """Same data as CSV for paste-into-spreadsheet workflows."""
    import csv, io
    try:
        days = min(int(request.args.get("days") or 30), 90)
        min_signals = max(int(request.args.get("min_signals") or 3), 1)
        limit = min(int(request.args.get("limit") or 100), 500)
    except Exception:
        days, min_signals, limit = 30, 3, 100

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["api_key_id", "email", "tier", "name",
                "total_signals", "last_signal_at", "tools_hit",
                "key_created_at"])
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_build_lead_query(days, min_signals, limit))
            for r in cur.fetchall():
                w.writerow([
                    r[0], r[1], r[2] or 'free', r[3], int(r[4] or 0),
                    r[5].isoformat() if r[5] else "",
                    ";".join(r[6]) if r[6] else "",
                    r[7].isoformat() if r[7] else "",
                ])
    except Exception as e:
        w.writerow([f"# ERROR: {str(e)[:200]}"])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="funnel-leads-{datetime.now(timezone.utc).strftime("%Y-%m-%d")}.csv"')
    return resp


@funnel_leads_bp.route("/api/v1/admin/funnel-leads/broadcast", methods=["POST"])
@_require_admin
def broadcast_to_leads():
    """Fire a targeted email broadcast to the top N hot leads using the
    existing broadcast infrastructure (routes/broadcast.py).

    Body JSON:
        subject       — required
        body_html     — required
        cta_link      — optional
        days          — lookback (default 30)
        min_signals   — minimum signals (default 5 for targeted; harder
                        threshold than the dashboard's 3)
        limit         — max recipients (default 50; protects against
                        accidental large send)
        confirm_send  — must be true to actually send (defaults to dry-run)
    """
    body = request.get_json(silent=True) or {}
    subject = (body.get("subject") or "").strip()
    body_html = body.get("body_html") or ""
    if not subject or not body_html:
        return jsonify(ok=False, error="subject + body_html required"), 400

    try:
        days = min(int(body.get("days") or 30), 90)
        min_signals = max(int(body.get("min_signals") or 5), 3)
        limit = min(int(body.get("limit") or 50), 200)
    except Exception:
        days, min_signals, limit = 30, 5, 50
    confirm = bool(body.get("confirm_send"))

    # Resolve emails
    emails = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_build_lead_query(days, min_signals, limit))
            for r in cur.fetchall():
                if r[1]:  # email
                    emails.append({
                        "email": r[1],
                        "signals": int(r[4] or 0),
                        "tools": list(r[6]) if r[6] else [],
                    })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    if not emails:
        return jsonify(ok=False, error="no_hot_leads",
                       hint="Lower min_signals or extend days."), 200

    if not confirm:
        return jsonify(ok=True, mode="dry_run",
                       eligible_count=len(emails),
                       sample_emails=[e["email"] for e in emails[:5]],
                       note=(f"Dry-run — would email {len(emails)} hot leads "
                             "with ≥" + str(min_signals) + " signals in last "
                             + str(days) + "d. Pass confirm_send: true to "
                             "actually deliver.")), 200

    # Real send — hand off to the broadcast endpoint via test_client
    import json as _json
    sent = 0; failed = 0; send_errors = []
    try:
        from routes.broadcast import _send_email
        for entry in emails:
            ok, err = _send_email(entry["email"], "", subject, body_html)
            if ok:
                sent += 1
            else:
                failed += 1
                if len(send_errors) < 5:
                    send_errors.append({"email": entry["email"], "error": err})
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    return jsonify(ok=True, mode="sent",
                   eligible_count=len(emails),
                   sent_count=sent,
                   failed_count=failed,
                   send_errors_sample=send_errors,
                   subject=subject), 200
