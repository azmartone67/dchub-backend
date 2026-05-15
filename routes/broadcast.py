"""
broadcast.py — Phase GG (2026-05-15): Bundle 5C — free-tier broadcast email +
DC Hub Media announcements.

The audit found 72 free users with zero scheduled touchpoints. Today's
outreach is REACTIVE (limit-hit → email) and MANUAL (admin digest).
This module adds the missing capability: scheduled broadcasts to free
and identified users, plus a weekly auto-digest cron pulling from DC
Hub Media + market movement.

Tables (CREATE TABLE IF NOT EXISTS):
  • email_subscribers     — newsletter-only signups (no full user account)
  • broadcast_log         — every broadcast send + delivery counts

Endpoints:
  • POST /api/v1/admin/broadcast        — send a broadcast (admin only)
  • GET  /api/v1/admin/broadcasts       — list recent broadcasts
  • POST /api/v1/subscribers/subscribe  — public signup for newsletter
  • POST /api/v1/subscribers/unsubscribe — remove from newsletter
  • GET  /api/v1/subscribers/count      — public-friendly counts by tier
  • POST /api/v1/digest/weekly-auto     — admin/cron: auto-build + send
                                          the weekly DC Hub Media digest

All sending goes via Resend (RESEND_API_KEY env). If unset, runs in
dry-run mode and reports what WOULD be sent without delivering — same
pattern as digest_weekly + market_alerts.
"""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request

try:
    from util.provenance import src, attach_sources, now_iso
except Exception:
    def src(claim, source, observed_at=None, url=None):
        return {"claim": claim, "source": source,
                "observed_at": observed_at.isoformat() if hasattr(observed_at, 'isoformat') else observed_at,
                "url": url}
    def attach_sources(p, s, generated_at=None):
        out = dict(p) if isinstance(p, dict) else {"result": p}
        out["sources"] = [x for x in (s or []) if x]
        out["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
        return out
    def now_iso():
        return datetime.now(timezone.utc).isoformat()

broadcast_bp = Blueprint("broadcast", __name__)

ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("ADMIN_KEY")
RESEND_API_KEY = os.environ.get("DCHUB_RESEND_API_KEY") or os.environ.get("RESEND_API_KEY")
FROM_EMAIL = os.environ.get("BROADCAST_FROM_EMAIL", "hello@dchub.cloud")
FROM_NAME = os.environ.get("BROADCAST_FROM_NAME", "DC Hub")


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS email_subscribers (
        id              BIGSERIAL PRIMARY KEY,
        email           TEXT NOT NULL UNIQUE,
        confirmed       BOOLEAN NOT NULL DEFAULT FALSE,
        source          TEXT,
        signup_url      TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        unsubscribed_at TIMESTAMPTZ,
        metadata        JSONB
    )""",
    "CREATE INDEX IF NOT EXISTS ix_es_status ON email_subscribers (unsubscribed_at, confirmed)",

    """CREATE TABLE IF NOT EXISTS broadcast_log (
        id              BIGSERIAL PRIMARY KEY,
        subject         TEXT NOT NULL,
        subject_hash    TEXT NOT NULL,
        target_tiers    TEXT NOT NULL,
        body_html       TEXT,
        cta_link        TEXT,
        triggered_by    TEXT,
        eligible_count  INT NOT NULL DEFAULT 0,
        sent_count      INT NOT NULL DEFAULT 0,
        failed_count    INT NOT NULL DEFAULT 0,
        mode            TEXT NOT NULL DEFAULT 'sent',
        sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at     TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS ix_bl_sent ON broadcast_log (sent_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_bl_hash ON broadcast_log (subject_hash, sent_at DESC)",
]


def _ensure_schema():
    try:
        with _conn() as c, c.cursor() as cur:
            for ddl in _SCHEMA:
                try:
                    cur.execute(ddl)
                except Exception:
                    pass
        return True
    except Exception:
        return False


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key") or
                    request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return w


def _build_audience(cur, target_tiers):
    """Resolve the target audience into (email, name, source) tuples.

    target_tiers is a list like ['free', 'identified', 'newsletter'].
    Pulls from users (filtered by plan) + email_subscribers (newsletter).
    Dedupes by email; subscribers can opt out via unsubscribed_at.
    """
    addrs = {}  # email → (name, source)
    plans = [t for t in target_tiers if t in
             ('free', 'identified', 'developer', 'pro', 'enterprise', 'founding', 'all_users')]
    if plans:
        if 'all_users' in plans:
            sql = """SELECT email, COALESCE(name, ''), plan
                       FROM users
                      WHERE email IS NOT NULL AND email <> ''"""
            params = []
        else:
            placeholders = ','.join(['%s'] * len(plans))
            sql = f"""SELECT email, COALESCE(name, ''), plan
                        FROM users
                       WHERE email IS NOT NULL AND email <> ''
                         AND plan IN ({placeholders})"""
            params = plans
        try:
            cur.execute(sql, params)
            for email, name, plan in cur.fetchall():
                if email:
                    addrs[email.lower().strip()] = (name, f"users:{plan}")
        except Exception:
            pass
    if 'newsletter' in target_tiers:
        try:
            cur.execute("""SELECT email FROM email_subscribers
                            WHERE unsubscribed_at IS NULL""")
            for (email,) in cur.fetchall():
                if email and email.lower().strip() not in addrs:
                    addrs[email.lower().strip()] = ("", "newsletter")
        except Exception:
            pass
    return list(addrs.items())


def _send_email(to_email, to_name, subject, body_html):
    """Send via Resend. Returns (ok, error_str)."""
    if not RESEND_API_KEY:
        return False, "no_provider"
    try:
        import urllib.request
        import urllib.error
        data = json.dumps({
            "from": f"{FROM_NAME} <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": subject,
            "html": body_html,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=data,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status < 300:
                return True, None
            return False, f"status_{r.status}"
    except urllib.error.HTTPError as e:
        return False, f"http_{e.code}"
    except Exception as e:
        return False, str(e)[:80]


# ─────────────────────────────────────────────────────────────────────
# Public subscription endpoints
# ─────────────────────────────────────────────────────────────────────
@broadcast_bp.route("/api/v1/subscribers/subscribe", methods=["POST"])
def subscribe():
    """Public newsletter signup. Body: {email, source?}. Idempotent."""
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 200:
        return jsonify(ok=False, error="invalid email"), 400
    source = (body.get("source") or "web")[:50]
    signup_url = (request.headers.get("Referer") or "")[:300]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO email_subscribers
                       (email, confirmed, source, signup_url)
                   VALUES (%s, TRUE, %s, %s)
                   ON CONFLICT (email) DO UPDATE
                   SET unsubscribed_at = NULL,
                       confirmed = TRUE
                   RETURNING id""",
                (email, source, signup_url))
            row = cur.fetchone()
        return jsonify(ok=True, id=row[0] if row else None,
                       email=email,
                       note=("You're subscribed to the DC Hub Weekly Digest. "
                             "Watch your inbox Monday 14:00 UTC.")), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@broadcast_bp.route("/api/v1/subscribers/unsubscribe", methods=["POST", "GET"])
def unsubscribe():
    """Public unsubscribe. POST body {email} or GET ?email=..."""
    _ensure_schema()
    email = ((request.get_json(silent=True) or {}).get("email")
             or request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify(ok=False, error="email required"), 400
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE email_subscribers
                              SET unsubscribed_at = NOW()
                            WHERE email = %s""", (email,))
            n = cur.rowcount
        return jsonify(ok=True, removed=n, email=email,
                       note="You're unsubscribed. Sorry to see you go."), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@broadcast_bp.route("/api/v1/subscribers/count", methods=["GET"])
def subscriber_count():
    """Public-friendly counts by tier. No PII exposed."""
    _ensure_schema()
    out = {"by_plan": {}, "newsletter": 0, "total_addressable": 0}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT plan, COUNT(*) FROM users
                            WHERE email IS NOT NULL AND email <> ''
                            GROUP BY plan""")
            for plan, n in cur.fetchall():
                out["by_plan"][plan or 'unknown'] = int(n)
            cur.execute("""SELECT COUNT(*) FROM email_subscribers
                            WHERE unsubscribed_at IS NULL""")
            row = cur.fetchone()
            out["newsletter"] = int(row[0]) if row else 0
            out["total_addressable"] = sum(out["by_plan"].values()) + out["newsletter"]
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    sources = [src("User plan counts", "users", now_iso()),
               src("Newsletter subscriber count", "email_subscribers", now_iso())]
    return jsonify(attach_sources(out, sources)), 200


# ─────────────────────────────────────────────────────────────────────
# Admin broadcast endpoints
# ─────────────────────────────────────────────────────────────────────
@broadcast_bp.route("/api/v1/admin/broadcast", methods=["POST"])
@_require_admin
def admin_broadcast():
    """Send a broadcast.

    Body JSON:
        subject       — required, max 200 chars
        body_html     — required
        target_tiers  — list like ['free','identified','newsletter']
                        ('all_users' targets every user with an email)
        cta_link      — optional, the primary CTA URL embedded in the email
        dedup_window_hours — optional (default 24); refuse if same subject
                              broadcast within this window
        dry_run       — bool; if true, return audience size without sending
        triggered_by  — string e.g. 'manual' / 'cron-weekly-digest'
    """
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    subject = (body.get("subject") or "").strip()
    body_html = body.get("body_html") or ""
    target_tiers = body.get("target_tiers") or ["free", "identified", "newsletter"]
    cta_link = body.get("cta_link") or ""
    triggered_by = (body.get("triggered_by") or "manual")[:60]
    dry_run = bool(body.get("dry_run"))
    dedup_hours = int(body.get("dedup_window_hours") or 24)

    if not subject or len(subject) > 200:
        return jsonify(ok=False, error="subject required (max 200 chars)"), 400
    if not body_html:
        return jsonify(ok=False, error="body_html required"), 400

    subject_hash = hashlib.sha1(subject.encode("utf-8")).hexdigest()[:24]
    try:
        with _conn() as c, c.cursor() as cur:
            # Dedup
            cur.execute(
                """SELECT id, sent_at FROM broadcast_log
                    WHERE subject_hash = %s
                      AND sent_at > NOW() - (%s * INTERVAL '1 hour')
                    ORDER BY sent_at DESC LIMIT 1""",
                (subject_hash, dedup_hours))
            dup = cur.fetchone()
            if dup and not body.get("force"):
                return jsonify(ok=False, error="duplicate_subject",
                               last_sent_at=dup[1].isoformat() if dup[1] else None,
                               note=f"Same subject broadcast within {dedup_hours}h. Set force=true to bypass."), 409

            audience = _build_audience(cur, target_tiers)
            eligible = len(audience)

            if dry_run:
                return jsonify(ok=True, mode="dry_run",
                               target_tiers=target_tiers,
                               eligible_count=eligible,
                               note=("Dry run — no emails sent. Audience "
                                     "would have included " + str(eligible) +
                                     " recipients.")), 200

            if not RESEND_API_KEY:
                cur.execute(
                    """INSERT INTO broadcast_log
                           (subject, subject_hash, target_tiers, body_html,
                            cta_link, triggered_by, eligible_count,
                            sent_count, failed_count, mode, finished_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0, 'no_provider', NOW())
                       RETURNING id""",
                    (subject, subject_hash, ','.join(target_tiers),
                     body_html[:65536], cta_link[:500],
                     triggered_by, eligible))
                log_id = cur.fetchone()[0]
                return jsonify(ok=True, mode="no_provider",
                               log_id=log_id,
                               eligible_count=eligible,
                               note="RESEND_API_KEY not configured. Set it in Railway to enable delivery."), 200

            # Insert the log row first; we'll update counts as we send.
            cur.execute(
                """INSERT INTO broadcast_log
                       (subject, subject_hash, target_tiers, body_html,
                        cta_link, triggered_by, eligible_count, mode)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'sending')
                   RETURNING id""",
                (subject, subject_hash, ','.join(target_tiers),
                 body_html[:65536], cta_link[:500],
                 triggered_by, eligible))
            log_id = cur.fetchone()[0]

        # Send loop — close txn before per-email HTTP work.
        sent = failed = 0
        send_errors = []
        for email, (name, _src) in audience:
            ok, err = _send_email(email, name, subject, body_html)
            if ok:
                sent += 1
            else:
                failed += 1
                if err and len(send_errors) < 5:
                    send_errors.append({"email": email, "error": err})

        # Update log row with final counts.
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """UPDATE broadcast_log
                      SET sent_count = %s, failed_count = %s,
                          mode = 'sent', finished_at = NOW()
                    WHERE id = %s""",
                (sent, failed, log_id))

        return jsonify(ok=True, mode="sent", log_id=log_id,
                       eligible_count=eligible, sent_count=sent,
                       failed_count=failed,
                       send_errors_sample=send_errors), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200


@broadcast_bp.route("/api/v1/admin/broadcasts", methods=["GET"])
@_require_admin
def admin_broadcasts_list():
    """Recent broadcast history."""
    _ensure_schema()
    try:
        limit = min(int(request.args.get("limit") or 30), 200)
    except Exception:
        limit = 30
    out = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, subject, target_tiers, eligible_count,
                          sent_count, failed_count, mode, triggered_by,
                          sent_at, finished_at
                     FROM broadcast_log
                    ORDER BY sent_at DESC LIMIT %s""", (limit,))
            for r in cur.fetchall():
                out.append({
                    "id": r[0], "subject": r[1],
                    "target_tiers": r[2],
                    "eligible_count": r[3], "sent_count": r[4],
                    "failed_count": r[5], "mode": r[6],
                    "triggered_by": r[7],
                    "sent_at": r[8].isoformat() if r[8] else None,
                    "finished_at": r[9].isoformat() if r[9] else None,
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    return jsonify(ok=True, broadcasts=out, count=len(out),
                   generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# Weekly DC Hub Media digest — admin / cron callable
# ─────────────────────────────────────────────────────────────────────
@broadcast_bp.route("/api/v1/digest/weekly-auto", methods=["POST"])
@_require_admin
def weekly_auto_digest():
    """Auto-builds the weekly digest from DC Hub Media + market movement
    + listings + brain status, then broadcasts to free + identified +
    newsletter subscribers. Idempotent via 6-day dedup.
    """
    _ensure_schema()
    # Pull source data via in-process Flask test_client (no network).
    try:
        from flask import current_app
        client = current_app.test_client()
    except Exception:
        client = None

    def _safe_get(path):
        if not client: return {}
        try:
            r = client.get(path)
            if r.status_code == 200:
                return r.get_json() or {}
        except Exception:
            pass
        return {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week = datetime.now(timezone.utc).strftime("Week of %b %d")
    subject = f"DC Hub Weekly — {week}"

    # Pull DCPI movers (last 7d)
    movers = _safe_get("/api/v1/dcpi/movers?window=7d&limit=5") or {}
    iso_comp = _safe_get("/api/v1/iso/comparison") or {}
    changes = _safe_get("/api/v1/changes/since?since=7d&limit=10") or {}
    brain = _safe_get("/api/v1/brain/self-assessment") or {}

    # Build HTML body
    parts = []
    parts.append(f"""
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#111;line-height:1.6">
<div style="border-bottom:2px solid #3b82f6;padding-bottom:16px;margin-bottom:24px">
  <div style="font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;font-weight:600">DC Hub Weekly</div>
  <h1 style="margin:8px 0 0;font-size:24px">{week}</h1>
</div>
""")

    # Section 1: DCPI movers
    top_isos = iso_comp.get("isos", [])[:3] if iso_comp else []
    if top_isos:
        parts.append('<h2 style="font-size:18px;margin:24px 0 12px">📊 Top ISOs by Excess Power</h2><ul style="padding-left:20px">')
        for i in top_isos:
            score = i.get("avg_excess_power_score", "—")
            verdict_counts = (f'{i.get("build_count",0)} BUILD / '
                              f'{i.get("caution_count",0)} CAUTION / '
                              f'{i.get("avoid_count",0)} AVOID')
            parts.append(f'<li style="margin-bottom:8px"><b>{i.get("iso")}</b> · excess {score} · {verdict_counts}</li>')
        parts.append('</ul>')

    # Section 2: Changes this week
    counts = (changes or {}).get("counts", {})
    if counts and (changes or {}).get("total_changes", 0):
        parts.append('<h2 style="font-size:18px;margin:24px 0 12px">📡 This Week\'s Changes</h2><ul style="padding-left:20px">')
        for k, v in counts.items():
            if v:
                label = k.replace('_', ' ').replace(' new', '').title()
                parts.append(f'<li>{v} new {label}</li>')
        parts.append('</ul>')

    # Section 3: Brain grade (if Pro tier — but visible to all as a transparency signal)
    grade = brain.get("grade")
    if grade:
        parts.append(f'<div style="background:#f3f4f6;padding:16px;border-radius:8px;margin:24px 0">'
                     f'<div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.08em">Brain self-assessment</div>'
                     f'<div style="font-size:32px;font-weight:800;color:#3b82f6;margin:4px 0">{grade}</div>'
                     f'<div style="color:#374151;font-size:14px">{brain.get("rationale","")}</div>'
                     f'</div>')

    parts.append("""
<div style="margin-top:32px;padding-top:24px;border-top:1px solid #e5e7eb;font-size:13px;color:#6b7280">
  <a href="https://dchub.cloud/dc-hub-media" style="color:#3b82f6;text-decoration:none">→ Read more on DC Hub Media</a>
  ·
  <a href="https://dchub.cloud/pricing" style="color:#3b82f6;text-decoration:none">Upgrade for full intelligence</a>
  ·
  <a href="{unsubscribe_url}" style="color:#9ca3af;text-decoration:none">Unsubscribe</a>
</div>
</body></html>""".replace("{unsubscribe_url}",
                          "https://dchub.cloud/api/v1/subscribers/unsubscribe?email={{recipient}}"))

    body_html = "\n".join(parts)

    # Hand off to admin_broadcast logic, internally — same dedup + send path.
    bcast_body = {
        "subject": subject,
        "body_html": body_html,
        "target_tiers": ["free", "identified", "newsletter"],
        "triggered_by": "cron-weekly-digest",
        "cta_link": "https://dchub.cloud/dc-hub-media",
        "dedup_window_hours": 144,  # 6 days
    }
    # Just forward to admin_broadcast by re-entering via test_client.
    try:
        if client:
            admin_key = request.headers.get("X-Admin-Key") or ""
            r = client.post(
                "/api/v1/admin/broadcast",
                headers={"X-Admin-Key": admin_key, "Content-Type": "application/json"},
                data=json.dumps(bcast_body))
            return r.get_json() or {"ok": False, "error": "no_json"}, r.status_code
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    return jsonify(ok=False, error="no_client"), 200


@broadcast_bp.route("/api/v1/broadcast/health", methods=["GET"])
def broadcast_health():
    ok = _ensure_schema()
    state = {"schema_ready": ok, "resend_configured": bool(RESEND_API_KEY),
             "from_email": FROM_EMAIL}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM email_subscribers WHERE unsubscribed_at IS NULL")
            state["active_subscribers"] = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM broadcast_log")
            state["total_broadcasts"] = int(cur.fetchone()[0])
            cur.execute("""SELECT plan, COUNT(*) FROM users
                            WHERE email IS NOT NULL AND email <> ''
                            GROUP BY plan""")
            state["users_with_email_by_plan"] = {r[0] or 'unknown': int(r[1])
                                                  for r in cur.fetchall()}
    except Exception as e:
        state["error_partial"] = str(e)[:200]
    return jsonify(ok=True, **state, generated_at=now_iso()), 200
