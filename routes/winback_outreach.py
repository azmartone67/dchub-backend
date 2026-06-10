"""Phase SSSS (2026-05-16) — winback outreach delivery loop.

RRRR shipped /api/v1/media/winback-pitches but pitches don't deliver
themselves. The contact field is usually a URL (anthropic.com/contact-
sales) so direct email-to-platform doesn't work. The pragmatic loop:

  1. Weekly cron fetches winback-pitches
  2. For each pitch NOT sent in last 7 days, email the OPERATOR
     (DCHUB_OPERATOR_EMAIL env) with the pitch ready to forward
  3. Track delivery in winback_outreach_sent table (7-day cooldown)
  4. Operator forwards manually (or with one-click email-client deeplink)
  5. Operator hits POST /api/v1/media/winback/mark-sent when done

Brain detector winback_pitch_unsent fires if pitches accumulate
without delivery — keeps the loop honest.

  POST /api/v1/media/winback/deliver        admin cron entry
  POST /api/v1/media/winback/mark-sent      admin (operator confirms send)
  GET  /api/v1/media/winback/log            public (sent history)

Cron: .github/workflows/winback-weekly.yml (added separately, fires
Monday 14:45 UTC, after the existing weekly digests).
"""

from __future__ import annotations

import os
import datetime
import urllib.parse
from flask import Blueprint, jsonify, request


winback_outreach_bp = Blueprint("winback_outreach", __name__)


_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_OPERATOR_EMAIL = (os.environ.get("DCHUB_OPERATOR_EMAIL")
                    or "api@dchub.cloud").strip()
_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME",  "DC Hub Brain")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS winback_outreach_sent (
    id           BIGSERIAL PRIMARY KEY,
    platform     TEXT NOT NULL,
    method       TEXT NOT NULL,          -- 'operator_briefing' | 'direct_send' | 'manual'
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_to TEXT,                    -- operator email OR platform contact
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|sent|bounced|opened|reply
    pitch_snapshot JSONB,                 -- copy of pitch at send time
    confirmed_by TEXT,                    -- operator name/email that confirmed manual delivery
    confirmed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_winback_platform_sent
    ON winback_outreach_sent(platform, sent_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _send_via_resend(to_email: str, subject: str, body_html: str) -> tuple[bool, str]:
    if not _RESEND_KEY:
        return False, "no_resend_key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            json={
                "from":    f"{_FROM_NAME} <{_FROM_EMAIL}>",
                "to":      [to_email],
                "subject": subject,
                "html":    body_html,
            },
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            timeout=10,
        )
        if r.status_code < 300: return True, f"sent_{r.status_code}"
        return False, f"status_{r.status_code}_{r.text[:80]}"
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}"


def _platform_was_recently_sent(cur, platform: str, days: int = 7) -> bool:
    try:
        cur.execute("""
            SELECT 1 FROM winback_outreach_sent
             WHERE platform = %s
               AND sent_at >= NOW() - INTERVAL '%s days'
             LIMIT 1
        """, (platform, days))
        return bool(cur.fetchone())
    except Exception:
        return False


def _render_operator_briefing_html(pitch: dict) -> str:
    """The email body that lands in the operator's inbox — ready to
    copy/forward to the platform's contact form."""
    p = pitch
    # Build mailto: deep-link so operator can one-click open their
    # email client with the pitch pre-filled. Falls back to plain
    # text if the contact is a URL not an email.
    contact = (p.get("contact") or "").strip()
    if "@" in contact and not contact.startswith("http"):
        mailto = (f"mailto:{contact}"
                  f"?subject={urllib.parse.quote(p.get('email_subject',''))}"
                  f"&body={urllib.parse.quote(p.get('email_body',''))}")
        contact_block = (f'<a href="{mailto}" style="display:inline-block;'
                         f'background:#065f46;color:white;padding:.5rem 1rem;'
                         f'border-radius:6px;text-decoration:none;font-weight:600">'
                         f'📧 Open in email client</a>')
    elif contact.startswith("http"):
        contact_block = (f'<a href="{contact}" style="display:inline-block;'
                         f'background:#1e40af;color:white;padding:.5rem 1rem;'
                         f'border-radius:6px;text-decoration:none;font-weight:600">'
                         f'🌐 Open contact form: {contact[:60]}…</a>')
    else:
        contact_block = f'<code style="background:#f3f4f6;padding:.2rem .4rem;border-radius:3px">{contact}</code>'

    return f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;max-width:680px;
margin:0 auto;padding:1.5rem;color:#1f2937;line-height:1.6">

<div style="background:linear-gradient(135deg,#0f172a,#1e3a8a);color:white;
padding:1rem 1.25rem;border-radius:8px;margin-bottom:1.5rem">
 <h2 style="margin:0;font-size:1.1rem">🤖 DC Hub Brain — Winback Pitch Ready</h2>
 <p style="margin:.25rem 0 0;color:#cbd5e1;font-size:.9rem">
   Platform: <strong>{p.get('platform','?')}</strong> ·
   {p.get('dormant_count','?')} dormant agents ·
   {p.get('total_prior_calls','?'):,} historical calls
 </p>
</div>

<p>The brain identified {p.get('platform')} as a winback target:</p>
<ul>
 <li><strong>{p.get('dormant_count','?')} dormant agents</strong> idle 14+ days</li>
 <li><strong>{p.get('total_prior_calls','?'):,} total historical calls</strong></li>
 <li>Pitch angle: {p.get('pitch_angle','?')}</li>
</ul>

<h3 style="margin-top:1.5rem">📤 One-click delivery</h3>
<p>{contact_block}</p>

<h3 style="margin-top:1.5rem">Suggested subject</h3>
<p><code style="background:#f3f4f6;padding:.4rem .6rem;border-radius:4px;display:block">
{p.get('email_subject','')}
</code></p>

<h3 style="margin-top:1.5rem">Suggested body</h3>
<pre style="background:#f9fafb;padding:1rem 1.25rem;border-radius:8px;
white-space:pre-wrap;font-family:Menlo,Monaco,monospace;font-size:.85rem;
border:1px solid #e5e7eb">{p.get('email_body','')}</pre>

<p style="color:#9ca3af;font-size:.85rem;margin-top:2rem;border-top:1px solid #e5e7eb;padding-top:1rem">
 Once you've sent (or decided not to), confirm with:<br>
 <code style="background:#f3f4f6;padding:.15rem .3rem;border-radius:3px">
 curl -X POST https://dchub.cloud/api/v1/media/winback/mark-sent
 -H "X-Admin-Key: $K"
 -d '{{"platform":"{p.get('platform')}", "status":"sent"}}'</code><br><br>
 Sample UAs in the dormant set: {', '.join((p.get('sample_uas') or [])[:3])[:200]}<br>
 7-day cooldown per platform; next pitch will email Mon at 14:45 UTC.
</p>
</body></html>"""


def deliver_pending(dry_run: bool = False) -> dict:
    """Cron entry. Fetches winback-pitches, emails operator one per
    platform not sent in last 7d, records to winback_outreach_sent."""
    out: dict = {"emailed": [], "skipped": [], "errors": [],
                 "dry_run": dry_run,
                 "operator_email": _OPERATOR_EMAIL,
                 "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}

    # Pull pitches
    try:
        from routes.dchub_media_revival import _classify_ua  # for UA fallback
        from routes.bot_outreach import _compute_dormant
        # Re-compute the same pitch shape the public endpoint serves
        import requests as _req
        # Easier: fetch our own endpoint (it's already memoized)
        try:
            r = _req.get("http://localhost:8080/api/v1/media/winback-pitches",
                          timeout=5)
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        pitches = data.get("pitches") or []
    except Exception as e:
        out["errors"].append(f"pitch_fetch_failed:{type(e).__name__}")
        return out

    if not pitches:
        out["errors"].append("no_pitches_found")
        return out

    c = _conn()
    if c is None:
        out["errors"].append("no_database")
        return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            for p in pitches:
                platform = p.get("platform") or "Unknown"
                # 7-day cooldown per platform
                if _platform_was_recently_sent(cur, platform, days=7):
                    out["skipped"].append({"platform": platform,
                                            "reason": "cooldown_7d"})
                    continue

                subject = (f"[DC Hub Brain] Winback pitch ready: "
                           f"{platform} ({p.get('dormant_count','?')} agents, "
                           f"{p.get('total_prior_calls','?'):,} calls)")
                html = _render_operator_briefing_html(p)

                if dry_run:
                    out["emailed"].append({"platform": platform,
                                            "to": _OPERATOR_EMAIL,
                                            "dry_run": True,
                                            "subject": subject})
                    continue

                ok, info = _send_via_resend(_OPERATOR_EMAIL, subject, html)
                # Persist regardless of send outcome — we want a record
                try:
                    import json as _json
                    cur.execute("""
                        INSERT INTO winback_outreach_sent
                          (platform, method, delivered_to, status,
                           pitch_snapshot)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT DO NOTHING
                    """, (platform, "operator_briefing", _OPERATOR_EMAIL,
                          "sent" if ok else "send_failed",
                          _json.dumps(p)))
                except Exception: pass

                if ok:
                    out["emailed"].append({"platform": platform,
                                            "to": _OPERATOR_EMAIL,
                                            "info": info})
                else:
                    out["errors"].append({"platform": platform,
                                           "info": info})
    finally:
        try: c.close()
        except Exception: pass
    return out


@winback_outreach_bp.route("/api/v1/media/winback/deliver", methods=["POST"])
def deliver():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    dry = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    return jsonify(deliver_pending(dry_run=dry)), 200


@winback_outreach_bp.route("/api/v1/media/winback/mark-sent", methods=["POST"])
def mark_sent():
    """Operator confirms manual delivery to a platform."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    platform = (d.get("platform") or "").strip()[:100]
    status   = (d.get("status") or "sent").strip().lower()[:30]
    who      = (d.get("by") or "operator").strip()[:80]
    if not platform:
        return jsonify(error="platform_required"), 400
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO winback_outreach_sent
                  (platform, method, status, confirmed_by, confirmed_at)
                VALUES (%s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (platform, "manual", status, who))
            r = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, recorded_id=int(r[0]) if r else None,
                   message=f"Recorded {status} for {platform} by {who}"), 200


@winback_outreach_bp.route("/api/v1/media/winback/log", methods=["GET"])
def log_endpoint():
    """Public log of winback sends — proof of activity for /transparency."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT platform, method, status, sent_at, confirmed_at, confirmed_by
                  FROM winback_outreach_sent
                 ORDER BY sent_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = []
    for r in rows:
        out.append({
            "platform":     r["platform"],
            "method":       r["method"],
            "status":       r["status"],
            "sent_at":      r["sent_at"].isoformat() if r["sent_at"] else None,
            "confirmed_at": r["confirmed_at"].isoformat() if r["confirmed_at"] else None,
            "confirmed_by": r["confirmed_by"],
        })
    resp = jsonify(log=out, count=len(out),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
