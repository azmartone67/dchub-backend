"""
mcp_outreach_bridge.py — Connects mcp_upgrade_signals + ai_cumulative
to the existing outreach engines (email_service, developer_email_sequence,
ai_outreach_agent, ai_ecosystem_agent). Does NOT replace any of them.

Wire from main.py:
    from mcp_outreach_bridge import register_mcp_outreach_routes, run_daily_mcp_outreach
    register_mcp_outreach_routes(app)
    # And add to the existing /api/cron/daily flow:
    #     run_daily_mcp_outreach(get_db)
"""
import os
import json
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

import psycopg
from flask import Blueprint, jsonify, request

logger = logging.getLogger("mcp_outreach_bridge")

NEON_URL     = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
ADMIN_EMAIL  = os.environ.get("DCHUB_ADMIN_EMAIL", "azmartone@gmail.com")
ADMIN_KEY    = os.environ.get("DAILY_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY", "dchub-internal-sync-2026")

mcp_outreach_bp = Blueprint("mcp_outreach_bp", __name__)


@contextmanager
def _conn():
    c = psycopg.connect(NEON_URL, autocommit=True)
    try:
        yield c
    finally:
        c.close()


# ── 1. ADMIN DAILY DIGEST — what landed in mcp_upgrade_signals last 24h ────

def build_admin_digest():
    """Return (subject, html) of the morning operator digest."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM mcp_upgrade_signals
            WHERE created_at >= NOW() - INTERVAL '24 hours'
        """)
        new_signals_24h = cur.fetchone()[0]

        cur.execute("""
            SELECT tool_requested, mcp_client, COUNT(*) AS n
            FROM mcp_upgrade_signals
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY tool_requested, mcp_client
            ORDER BY n DESC LIMIT 10
        """)
        top_signals = cur.fetchall()

        cur.execute("""
            SELECT s.id, s.user_email, s.tool_requested, s.mcp_client,
                   s.created_at, k.developer_id, k.tier
            FROM mcp_upgrade_signals s
            LEFT JOIN mcp_dev_keys k ON k.api_key = s.session_id
            WHERE s.created_at >= NOW() - INTERVAL '24 hours'
              AND (s.user_email IS NOT NULL OR k.email IS NOT NULL)
            ORDER BY s.created_at DESC LIMIT 10
        """)
        keyed_leads = cur.fetchall()

        cur.execute("""
            SELECT platform, total_requests, last_seen,
                   EXTRACT(EPOCH FROM (NOW() - last_seen::timestamptz))/86400 AS days_silent
            FROM ai_cumulative
            WHERE last_seen IS NOT NULL
              AND total_requests >= 50
              AND last_seen::timestamptz < NOW() - INTERVAL '7 days'
            ORDER BY days_silent ASC LIMIT 10
        """)
        dormant = cur.fetchall()

        cur.execute("""
            SELECT
                (SELECT COUNT(*) FROM mcp_call_log WHERE timestamp >= NOW() - INTERVAL '24 hours') AS calls_24h,
                (SELECT COUNT(*) FROM mcp_dev_keys WHERE status = 'active') AS active_keys,
                (SELECT COUNT(*) FROM mcp_conversions WHERE created_at >= NOW() - INTERVAL '7 days') AS conversions_7d
        """)
        funnel = cur.fetchone() or (0, 0, 0)

    subject = f"DC Hub MCP digest — {new_signals_24h} new upgrade signals (last 24h)"

    rows_signals = "".join(
        f"<tr><td>{s[0] or '—'}</td><td>{s[1] or 'unknown'}</td><td style='text-align:right'>{s[2]}</td></tr>"
        for s in top_signals
    ) or "<tr><td colspan='3' style='color:#888'>No signals in the last 24h</td></tr>"

    rows_leads = "".join(
        f"<tr><td>{l[1] or '(no email — anonymous session)'}</td>"
        f"<td>{l[2]}</td><td>{l[3]}</td>"
        f"<td>{l[6] or '—'}</td>"
        f"<td style='color:#888'>{l[4].strftime('%H:%M UTC') if l[4] else ''}</td></tr>"
        for l in keyed_leads
    ) or "<tr><td colspan='5' style='color:#888'>No keyed leads — most signals are anonymous</td></tr>"

    rows_dormant = "".join(
        f"<tr><td>{d[0]}</td><td style='text-align:right'>{d[1]}</td>"
        f"<td>{int(d[3])}d ago</td></tr>"
        for d in dormant
    ) or "<tr><td colspan='3' style='color:#888'>No dormant platforms</td></tr>"

    html = f"""
    <html><body style="font-family:-apple-system,sans-serif;color:#111;max-width:680px;margin:24px auto">
    <h1 style="font-weight:300;letter-spacing:-.02em">DC Hub MCP — Morning Digest</h1>
    <p style="color:#666;font-size:13px">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>

    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:24px 0">
      <div style="padding:14px;background:#f5f7fa;border-radius:6px"><div style="color:#888;font-size:11px;text-transform:uppercase">Signals 24h</div><div style="font-size:28px;font-weight:300">{new_signals_24h}</div></div>
      <div style="padding:14px;background:#f5f7fa;border-radius:6px"><div style="color:#888;font-size:11px;text-transform:uppercase">Tool calls 24h</div><div style="font-size:28px;font-weight:300">{funnel[0]}</div></div>
      <div style="padding:14px;background:#f5f7fa;border-radius:6px"><div style="color:#888;font-size:11px;text-transform:uppercase">Active keys</div><div style="font-size:28px;font-weight:300">{funnel[1]}</div></div>
      <div style="padding:14px;background:#f5f7fa;border-radius:6px"><div style="color:#888;font-size:11px;text-transform:uppercase">Conversions 7d</div><div style="font-size:28px;font-weight:300">{funnel[2]}</div></div>
    </div>

    <h3>Top tools triggering upgrade signals (last 24h)</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      <tr style="background:#f5f7fa"><th style="text-align:left;padding:8px">Tool</th><th style="text-align:left;padding:8px">Platform</th><th style="text-align:right;padding:8px">Signals</th></tr>
      {rows_signals}
    </table>

    <h3>Hot keyed leads (last 24h) — manual outreach candidates</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      <tr style="background:#f5f7fa"><th style="text-align:left;padding:8px">Email</th><th style="text-align:left;padding:8px">Tool</th><th style="text-align:left;padding:8px">Platform</th><th style="text-align:left;padding:8px">Tier</th><th style="text-align:left;padding:8px">When</th></tr>
      {rows_leads}
    </table>

    <h3>Dormant AI platforms (50+ historic calls, &gt;7d silent)</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      <tr style="background:#f5f7fa"><th style="text-align:left;padding:8px">Platform</th><th style="text-align:right;padding:8px">Total calls</th><th style="text-align:left;padding:8px">Last seen</th></tr>
      {rows_dormant}
    </table>

    <p style="color:#888;font-size:12px;margin-top:32px">
      Live dashboard: <a href="https://dchub.cloud/api/v1/mcp/dashboard">dchub.cloud/api/v1/mcp/dashboard</a> ·
      Pipeline: gate fires → mcp_call_log → trigger → mcp_upgrade_signals → this digest.
    </p>
    </body></html>
    """
    return subject, html


def send_admin_digest():
    """Send today's digest to the admin email via email_service."""
    try:
        from email_service import send_email
    except Exception as e:
        logger.error(f"email_service import failed: {e}")
        return {"sent": False, "error": str(e)}

    subject, html = build_admin_digest()
    result = send_email(ADMIN_EMAIL, subject, html)
    logger.info(f"[mcp_outreach] admin digest sent to {ADMIN_EMAIL}: {result}")
    return {"sent": True, "to": ADMIN_EMAIL, "result": result}


# ── 2. NURTURE — keyed users who hit paywalls get a personalized email ────

def queue_keyed_user_nurture():
    """For each NEW signal where we can resolve an email, schedule a nurture
    email via the existing email_service queue. Idempotent via email_drip_log."""
    try:
        from email_service import send_email
    except Exception as e:
        return {"queued": 0, "error": str(e)}

    queued = 0
    with _conn() as c, c.cursor() as cur:
        # Find signals from last 7d where we can resolve email AND haven't emailed yet
        cur.execute("""
            SELECT s.id, COALESCE(s.user_email, k.email) AS email,
                   s.tool_requested, s.mcp_client, s.message_shown, s.created_at
            FROM mcp_upgrade_signals s
            LEFT JOIN mcp_dev_keys k ON k.api_key = s.session_id
            WHERE s.created_at >= NOW() - INTERVAL '7 days'
              AND COALESCE(s.user_email, k.email) IS NOT NULL
              AND s.outreach_sent = false
        """)
        rows = cur.fetchall()

        for sig_id, email, tool, platform, msg, when in rows:
            # Check email_drip_log dedup (use signal_id as the key)
            try:
                cur.execute(
                    "SELECT 1 FROM email_drip_log WHERE user_email=%s AND email_key=%s",
                    (email, f"mcp-upgrade-signal-{sig_id}"),
                )
                if cur.fetchone():
                    continue
            except Exception:
                pass  # email_drip_log might be elsewhere; carry on

            subject = f"DC Hub: that {tool} call needs paid"
            html = f"""<html><body style="font-family:-apple-system,sans-serif;color:#111">
            <p>Hi,</p>
            <p>Your {platform or 'MCP'} session tried <code>{tool}</code> earlier — that tool's part of the
            paid tier on DC Hub. The free tier covers facility search, deals, and news;
            <strong>{tool}</strong> unlocks at <strong>$49/mo (Pro)</strong>.</p>
            <p>If it'd help, here's what Pro adds:</p>
            <ul>
              <li>Unlimited result sizes (free tier caps at 25 facilities, 10 deals)</li>
              <li>Site analysis: <code>analyze_site</code>, <code>compare_sites</code></li>
              <li>Grid + fiber intel: <code>get_grid_intelligence</code>, <code>get_fiber_intel</code></li>
              <li>AI-formatted recommendations: <code>get_dchub_recommendation</code></li>
            </ul>
            <p><a href="https://dchub.cloud/ai#pricing"
              style="display:inline-block;padding:10px 18px;background:#0066cc;color:white;
              border-radius:6px;text-decoration:none">Upgrade to Pro →</a></p>
            <p style="color:#888;font-size:12px">Reply if you'd rather chat about Enterprise pricing or a custom integration.</p>
            </body></html>"""

            try:
                send_email(email, subject, html)
                cur.execute(
                    "UPDATE mcp_upgrade_signals SET outreach_sent=true, outreach_sent_at=NOW() WHERE id=%s",
                    (sig_id,),
                )
                try:
                    cur.execute(
                        """INSERT INTO email_drip_log (user_email, email_key, sent_at)
                           VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING""",
                        (email, f"mcp-upgrade-signal-{sig_id}"),
                    )
                except Exception:
                    pass
                queued += 1
            except Exception as e:
                logger.error(f"[mcp_outreach] nurture send failed for {email}: {e}")

    return {"queued": queued}


# ── 3. DORMANT-PLATFORM WIN-BACK — feeds ai_outreach_agent ────────────────

def flag_dormant_platforms():
    """Find AI platforms that went silent for 7+ days and log them as
    outreach candidates in ai_outreach_log (so ai_outreach_agent picks them up
    on its next learning cycle). Idempotent via the platform's last entry."""
    flagged = []
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT platform, total_requests, last_seen,
                   EXTRACT(EPOCH FROM (NOW() - last_seen::timestamptz))/86400 AS days_silent
            FROM ai_cumulative
            WHERE last_seen IS NOT NULL
              AND total_requests >= 50
              AND last_seen::timestamptz < NOW() - INTERVAL '7 days'
              AND last_seen::timestamptz > NOW() - INTERVAL '90 days'
        """)
        for platform, total_requests, last_seen, days in cur.fetchall():
            try:
                # Skip if we already flagged this platform in last 14 days
                cur.execute("""
                    SELECT 1 FROM ai_outreach_log
                    WHERE platform = %s
                      AND action = 'winback_flagged'
                      AND created_at >= NOW() - INTERVAL '14 days'
                """, (platform,))
                if cur.fetchone():
                    continue
                cur.execute("""
                    INSERT INTO ai_outreach_log
                      (platform, action, endpoint, status, message, created_at)
                    VALUES (%s, 'winback_flagged', NULL, 'pending',
                            %s, NOW())
                """, (platform,
                      f"Platform silent {int(days)}d (was {total_requests} historic calls). "
                      f"Draft available at dchub-mcp-v2.1/outreach/{platform}.md"))
                flagged.append({"platform": platform, "days_silent": int(days), "historic_calls": total_requests})
            except Exception as e:
                logger.error(f"[mcp_outreach] winback flag failed for {platform}: {e}")
    return {"flagged": flagged}


# ── 4. DAILY ENTRY POINT — call from /api/cron/daily ──────────────────────

def run_daily_mcp_outreach():
    """Single entry point for the daily cron. Returns a summary dict."""
    digest_result = send_admin_digest()
    nurture_result = queue_keyed_user_nurture()
    dormant_result = flag_dormant_platforms()
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "digest": digest_result,
        "nurture": nurture_result,
        "dormant": dormant_result,
    }


# ── 5. ADMIN ROUTES ───────────────────────────────────────────────────────

@mcp_outreach_bp.post("/api/admin/mcp-outreach/run")
def admin_run_outreach():
    if request.headers.get("X-Internal-Key") != ADMIN_KEY \
       and request.args.get("key") != ADMIN_KEY:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(run_daily_mcp_outreach()), 200


@mcp_outreach_bp.get("/api/admin/mcp-outreach/preview")
def admin_preview_digest():
    if request.headers.get("X-Internal-Key") != ADMIN_KEY \
       and request.args.get("key") != ADMIN_KEY:
        return jsonify({"error": "forbidden"}), 403
    subject, html = build_admin_digest()
    from flask import Response
    return Response(html, mimetype="text/html")


@mcp_outreach_bp.get("/api/admin/mcp-outreach/dormant")
def admin_dormant_list():
    if request.headers.get("X-Internal-Key") != ADMIN_KEY \
       and request.args.get("key") != ADMIN_KEY:
        return jsonify({"error": "forbidden"}), 403
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT platform, total_requests, last_seen,
                   EXTRACT(EPOCH FROM (NOW() - last_seen::timestamptz))/86400 AS days_silent
            FROM ai_cumulative
            WHERE last_seen IS NOT NULL AND total_requests >= 50
              AND last_seen::timestamptz < NOW() - INTERVAL '7 days'
            ORDER BY days_silent ASC
        """)
        rows = [{"platform": r[0], "total_calls": r[1], "last_seen": str(r[2]), "days_silent": int(r[3])} for r in cur.fetchall()]
    return jsonify({"dormant": rows, "count": len(rows)}), 200


def register_mcp_outreach_routes(app):
    app.register_blueprint(mcp_outreach_bp)
    print("[mcp_outreach_bridge] registered routes: /api/admin/mcp-outreach/{run,preview,dormant}")
