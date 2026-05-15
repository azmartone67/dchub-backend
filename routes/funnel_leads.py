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

# Match the exact pattern brain_v2_layer4 uses (proven to work all session
# via the brain_learn workflow). No strip, no extra fallback. The .strip()
# I added earlier may have been silently changing the comparison.
ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        # Match brain_v2_layer4 _require_admin exactly — no .strip() on
        # either side. The strip was silently mismatching the value.
        provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return w


def _build_lead_query(days: int = 30, min_signals: int = 3, limit: int = 100):
    """Aggregate hot leads from mcp_upgrade_signals.

    Real schemas (verified 2026-05-15 via the workflow run that returned
    'column does not exist' twice):
      mcp_upgrade_signals: user_email, tool_requested, signal_type,
                            tier_current, tier_required, created_at
      mcp_dev_keys:        api_key (PK), developer_id, email, tier,
                            status, metadata, created_at, last_used_at
    No api_key_id, no k.id, no k.name. LEFT JOIN by email."""
    return f"""
        WITH rolled AS (
            SELECT user_email,
                   COUNT(*) AS total_signals,
                   MAX(created_at) AS last_signal_at,
                   array_agg(DISTINCT tool_requested ORDER BY tool_requested)
                     FILTER (WHERE tool_requested IS NOT NULL) AS tools_hit
              FROM mcp_upgrade_signals
             WHERE created_at > NOW() - INTERVAL '{int(days)} days'
               AND user_email IS NOT NULL
               AND user_email <> ''
          GROUP BY user_email
            HAVING COUNT(*) >= {int(min_signals)}
        )
        SELECT k.developer_id,
               r.user_email,
               COALESCE(k.tier, 'free') AS tier,
               NULL::text AS name,
               r.total_signals,
               r.last_signal_at,
               r.tools_hit,
               k.created_at AS key_created
          FROM rolled r
          LEFT JOIN mcp_dev_keys k ON k.email = r.user_email
                                  AND COALESCE(k.status, 'active') = 'active'
         WHERE COALESCE(k.tier, 'free') IN ('free', 'identified')
      ORDER BY r.total_signals DESC, r.last_signal_at DESC
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
                    "developer_id": r[0],
                    "email": r[1],
                    "tier": r[2] or 'free',
                    "name": r[3],
                    "total_signals": int(r[4] or 0),
                    "last_signal_at": r[5].isoformat() if r[5] else None,
                    "tools_hit": list(r[6]) if r[6] else [],
                    "key_created_at": r[7].isoformat() if r[7] else None,
                })
            # Roll-up stats
            cur.execute(f"""SELECT COUNT(DISTINCT user_email),
                                    COUNT(*)::int
                              FROM mcp_upgrade_signals
                             WHERE created_at > NOW() - INTERVAL '{int(days)} days'
                               AND user_email IS NOT NULL""")
            row = cur.fetchone()
            out["totals"] = {
                "distinct_users_signaled": int(row[0]) if row and row[0] else 0,
                "total_signals": int(row[1]) if row and row[1] else 0,
            }
            cur.execute("""SELECT tool_requested, COUNT(*) AS n
                             FROM mcp_upgrade_signals
                            WHERE created_at > NOW() - INTERVAL '30 days'
                              AND tool_requested IS NOT NULL
                         GROUP BY tool_requested ORDER BY n DESC LIMIT 10""")
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


# ─────────────────────────────────────────────────────────────────────
# Personalized template — uses per-lead tool affinity to write a
# targeted subject + body, instead of one-size-fits-all blast.
# ─────────────────────────────────────────────────────────────────────
def _tool_value_blurb(tool: str) -> str:
    """Per-tool one-liner used in the personalized email body."""
    BLURBS = {
        "get_market_intel": "supply/demand MW, pricing, vacancy for 60+ global DC markets",
        "get_grid_data": "real-time ISO demand, peak, reserve margin, fuel mix, queue",
        "get_grid_intelligence": "interconnection-queue MW, headroom %, renewable mix per ISO",
        "get_water_risk": "WRI Aqueduct water-stress + drought + flood per lat/lon",
        "get_energy_prices": "exact ¢/kWh by state with industrial/commercial/residential split",
        "get_renewable_energy": "solar + wind capacity with project-level MW, COD, PPA prices",
        "get_fiber_intel": "3,282 long-haul routes — carriers, lit/dark, latency, IX",
        "analyze_site": "composite site-score for any lat/lon — power, fiber, water, tax",
        "compare_sites": "side-by-side scoring across up to 5 candidate sites",
        "get_tax_incentives": "50-state sales-tax abatements + property exemptions",
        "get_pipeline": "540+ active DC projects — operator, capacity, status, ETA",
        "list_transactions": "$324B+ M&A history — buyer, seller, MW, $/kW, date",
        "get_intelligence_index": "DCPI index for 280+ markets — rank, weekly delta, top movers",
    }
    return BLURBS.get(tool, f"`{tool}` premium intelligence")


def _personalized_email(lead: dict, sender_name: str = "DC Hub") -> tuple[str, str]:
    """Returns (subject, body_html) personalized for this lead's tool affinity."""
    tools = lead.get("tools_hit") or []
    primary_tool = tools[0] if tools else None
    signals = lead.get("total_signals") or 0
    blurb = _tool_value_blurb(primary_tool) if primary_tool else "DC Hub intelligence tools"

    if primary_tool:
        subject = f"You tried {primary_tool} — it's free now"
    else:
        subject = "Your DC Hub upgrade signals — we made a change"

    other_tools = tools[1:5] if len(tools) > 1 else []
    other_list = ""
    if other_tools:
        other_list = "<ul style=\"padding-left:20px;color:#374151;font-size:14px\">" + \
            "".join(f"<li><code>{t}</code> — {_tool_value_blurb(t)}</li>" for t in other_tools) + \
            "</ul>"

    body_html = f"""
<html><body style="font-family:-apple-system,Helvetica,sans-serif;max-width:580px;margin:0 auto;padding:24px;color:#111;line-height:1.6">
<div style="border-bottom:2px solid #3b82f6;padding-bottom:12px;margin-bottom:20px">
  <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;font-weight:600">DC Hub</div>
  <h1 style="margin:6px 0 0;font-size:22px">Quick note — we lowered the wall</h1>
</div>

<p>Your MCP client (Claude / Cursor / Windsurf / etc.) called DC Hub's <strong><code>{primary_tool or 'paid tools'}</code></strong> {signals} time{'s' if signals != 1 else ''} in the last 30 days. Each call hit our paywall.</p>

<p><strong>That changes today.</strong> We just moved {primary_tool or 'this tool'} (and 6 others) to the free Identified tier. Your existing API key already has access:</p>

<ul style="padding-left:20px;color:#374151">
  <li><code>get_market_intel</code> · supply/demand, pricing, vacancy</li>
  <li><code>get_grid_data</code> · live ISO demand + queue + fuel mix</li>
  <li><code>get_grid_intelligence</code> · headroom % + renewable mix</li>
  <li><code>get_fiber_intel</code> · long-haul routes + carriers</li>
  <li><code>get_water_risk</code>, <code>get_energy_prices</code>, <code>get_renewable_energy</code></li>
</ul>

<p>200 calls/day. 20 rows per call. No card. No new key.</p>

<p>What I'd ask you to do, today: <strong>retry the question that hit the wall yesterday</strong>. If the data is what you needed, great. If 20 rows isn't enough or you want <code>analyze_site</code> + <code>compare_sites</code> for unlimited site-selection workflows, Developer is $49/mo at <a href="https://dchub.cloud/pricing?utm_source=outreach&amp;utm_campaign=hot_leads&amp;utm_term={primary_tool or 'lead'}" style="color:#3b82f6">dchub.cloud/pricing</a>.</p>

{f"<p>Bonus context — your client also tried:</p>{other_list}" if other_list else ""}

<p>Reply to this email if you want a manual walkthrough or have feedback on what to ship next.</p>

<div style="margin-top:32px;padding-top:18px;border-top:1px solid #e5e7eb;font-size:13px;color:#6b7280">
  — {sender_name} · <a href="https://dchub.cloud" style="color:#3b82f6;text-decoration:none">dchub.cloud</a> · <a href="https://dchub.cloud/by-the-numbers" style="color:#3b82f6;text-decoration:none">By the Numbers</a> · <a href="https://dchub.cloud/api/v1/subscribers/unsubscribe?email={lead.get('email','')}" style="color:#9ca3af;text-decoration:none">Unsubscribe</a>
</div>
</body></html>"""
    return subject, body_html


@funnel_leads_bp.route("/api/v1/admin/funnel-leads/preview", methods=["GET"])
@_require_admin
def preview_personalized_emails():
    """Preview the personalized email for top N hot leads without sending.

    Query params:
        days, min_signals, limit — same as funnel-leads endpoint.
    Returns the rendered subject + first 500 chars of body per lead so
    you can sanity-check before firing.
    """
    try:
        days = min(int(request.args.get("days") or 30), 90)
        min_signals = max(int(request.args.get("min_signals") or 5), 1)
        limit = min(int(request.args.get("limit") or 10), 50)
    except Exception:
        days, min_signals, limit = 30, 5, 10

    previews = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_build_lead_query(days, min_signals, limit))
            for r in cur.fetchall():
                lead = {
                    "developer_id": r[0], "email": r[1], "tier": r[2] or 'free',
                    "name": r[3], "total_signals": int(r[4] or 0),
                    "tools_hit": list(r[6]) if r[6] else [],
                }
                subject, body = _personalized_email(lead)
                previews.append({
                    "email": lead["email"],
                    "signals": lead["total_signals"],
                    "primary_tool": (lead["tools_hit"][:1] or [None])[0],
                    "subject": subject,
                    "body_preview": body[:600],
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, days=days, min_signals=min_signals,
                   previews=previews, count=len(previews)), 200


@funnel_leads_bp.route("/api/v1/admin/funnel-leads/broadcast", methods=["POST"])
@_require_admin
def broadcast_to_leads():
    """Fire a targeted email blast to the top N hot leads.

    Body JSON:
        mode               — 'personalized' (default) or 'static'
        subject, body_html — required if mode='static'; ignored if personalized
        days               — lookback (default 30)
        min_signals        — min upgrade signals (default 5)
        limit              — max recipients (default 50, ceiling 200)
        confirm_send       — must be true; otherwise dry-run

    Personalized mode generates a per-lead subject + body based on which
    tools they kept hitting. Static mode uses the passed-in subject/body
    for all recipients.
    """
    body = request.get_json(silent=True) or {}
    mode = (body.get("mode") or "personalized").lower()
    if mode not in ("personalized", "static"):
        return jsonify(ok=False, error="mode must be 'personalized' or 'static'"), 400

    static_subject = (body.get("subject") or "").strip()
    static_body = body.get("body_html") or ""
    if mode == "static" and (not static_subject or not static_body):
        return jsonify(ok=False, error="static mode requires subject + body_html"), 400

    try:
        days = min(int(body.get("days") or 30), 90)
        min_signals = max(int(body.get("min_signals") or 5), 3)
        limit = min(int(body.get("limit") or 50), 200)
    except Exception:
        days, min_signals, limit = 30, 5, 50
    confirm = bool(body.get("confirm_send"))

    leads = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_build_lead_query(days, min_signals, limit))
            for r in cur.fetchall():
                if r[1]:  # email
                    leads.append({
                        "developer_id": r[0], "email": r[1], "tier": r[2] or 'free',
                        "name": r[3], "total_signals": int(r[4] or 0),
                        "tools_hit": list(r[6]) if r[6] else [],
                    })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    if not leads:
        return jsonify(ok=False, error="no_hot_leads",
                       hint="Lower min_signals or extend days."), 200

    if not confirm:
        sample = []
        for lead in leads[:5]:
            if mode == "personalized":
                subj, _ = _personalized_email(lead)
            else:
                subj = static_subject
            sample.append({"email": lead["email"], "subject": subj,
                           "tools": lead["tools_hit"][:3]})
        return jsonify(ok=True, mode="dry_run", send_mode=mode,
                       eligible_count=len(leads), sample=sample,
                       note=(f"Dry-run — would email {len(leads)} hot leads. "
                             "Pass confirm_send: true to actually deliver. "
                             f"GET /api/v1/admin/funnel-leads/preview to inspect "
                             "personalized bodies first.")), 200

    # Real send
    try:
        from routes.broadcast import _send_email
    except Exception as e:
        return jsonify(ok=False, error=f"send_helper_unavailable: {e}"), 500

    sent = 0; failed = 0; errors = []
    for lead in leads:
        if mode == "personalized":
            subj, html = _personalized_email(lead)
        else:
            subj, html = static_subject, static_body
        ok, err = _send_email(lead["email"], lead.get("name") or "", subj, html)
        if ok:
            sent += 1
        else:
            failed += 1
            if len(errors) < 5:
                errors.append({"email": lead["email"], "error": err})

    return jsonify(ok=True, mode="sent", send_mode=mode,
                   eligible_count=len(leads),
                   sent_count=sent, failed_count=failed,
                   errors_sample=errors), 200
