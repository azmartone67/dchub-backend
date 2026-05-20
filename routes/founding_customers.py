"""Phase FF+25-followup-r19 (2026-05-20) — founding customers.
==========================================================================

Kevin Serfass (kevin.d.serfass@gmail.com) is the first new paid
customer to come in via the website front-door (not the MCP funnel).
$9 → $49 within 60 seconds at 2026-05-20 20:03 UTC. Pure top-funnel
conversion driven by Switzerland positioning + the brand polish.

The first dozen paid customers matter disproportionately:
  · They're proof the value-prop lands
  · They become reference customers (with permission)
  · They tell us which use cases the product actually solves
  · They tolerate the rough edges that prevent customer #50 from
    converting

This module gives us a queryable founding-customer cohort + a brain
signal so the Inspector celebrates / tracks these specifically.

ENDPOINTS:
  POST /api/v1/admin/founding-customers/tag      add an email to the
                                                   founding cohort
  POST /api/v1/admin/founding-customers/untag    remove
  GET  /api/v1/admin/founding-customers           list (admin)
  GET  /api/v1/founding-customers/count           public count

Used by:
  · brain_inspector — adds founding_customers count to signal block
  · /status dashboard — surfaces the count as a positive metric
  · Inspector system prompt rule: when founding_customers > 0,
    name them as a positive Healthy item
"""
import os
import json
import logging
import datetime
from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
founding_customers_bp = Blueprint("founding_customers", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    c = _get_db()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS founding_customers (
                    email           TEXT PRIMARY KEY,
                    tagged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    plan_at_tag     TEXT,
                    first_payment_at TIMESTAMPTZ,
                    stripe_customer_id TEXT,
                    notes           TEXT,
                    contact_status  TEXT DEFAULT 'new',
                    contacted_at    TIMESTAMPTZ,
                    consented_to_cite BOOLEAN DEFAULT FALSE
                )
            """)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning(f"[founding-customers] table create failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/admin/founding-customers/tag",
                              methods=["POST"])
def tag_founding():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    p = request.get_json(silent=True) or {}
    email = (p.get("email") or "").lower().strip()
    if not email:
        return jsonify(ok=False, error="email_required"), 400
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO founding_customers
                  (email, plan_at_tag, first_payment_at,
                   stripe_customer_id, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                  notes = COALESCE(founding_customers.notes, '')
                          || E'\\n' || COALESCE(EXCLUDED.notes, '')
            """, (
                email, p.get("plan"),
                p.get("first_payment_at"),
                p.get("stripe_customer_id"),
                p.get("notes"),
            ))
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, email=email,
                       tagged_at=datetime.datetime.utcnow().isoformat() + "Z")
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/admin/founding-customers",
                              methods=["GET"])
def list_founding():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT email, tagged_at, plan_at_tag, first_payment_at,
                       stripe_customer_id, contact_status, contacted_at,
                       consented_to_cite, notes
                  FROM founding_customers
                 ORDER BY tagged_at DESC
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "email": r[0],
                    "tagged_at": str(r[1]) if r[1] else None,
                    "plan_at_tag": r[2],
                    "first_payment_at": str(r[3]) if r[3] else None,
                    "stripe_customer_id": r[4],
                    "contact_status": r[5],
                    "contacted_at": str(r[6]) if r[6] else None,
                    "consented_to_cite": r[7],
                    "notes": r[8],
                })
        return jsonify(ok=True, count=len(rows), founding=rows)
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/founding-customers/count",
                              methods=["GET"])
def public_count():
    """Public — just the count, no PII. Brain Inspector reads this and
    the Inspector brief celebrates each milestone (1, 5, 10, 25, 50)."""
    _ensure_table()
    c = _get_db()
    if c is None:
        return jsonify(count=0), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM founding_customers")
            n = int((cur.fetchone() or [0])[0] or 0)
        return jsonify(count=n,
                       milestone=("first" if n == 1
                                   else ("5+" if n >= 5
                                         else f"{n} of 5 to milestone")),
                       generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    finally:
        try: c.close()
        except Exception: pass


# ── Auto-tag hook ────────────────────────────────────────────────────
# Called from the Stripe webhook on checkout.session.completed +
# customer.subscription.created so every paid signup auto-tags into the
# founding cohort UNTIL the cap is hit. After cap, ordinary paid
# customers just become regular customers and no founding row is added.
#
# Cap is FOUNDING_CUSTOMERS_CAP env var (default 25). After hitting the
# cap the auto-tag stops permanently — those 25 become the canonical
# "founding 25" cohort for marketing / reference use.
FOUNDING_CAP = int(os.environ.get("FOUNDING_CUSTOMERS_CAP", "25"))


def auto_tag_if_under_cap(
    email: str,
    plan: str = "developer",
    stripe_customer_id: str | None = None,
    first_payment_at: str | None = None,
    notes: str | None = None,
) -> dict:
    """Idempotently tag a paid customer into founding_customers if the
    cohort is below FOUNDING_CAP. Safe to call from the Stripe webhook —
    no exception bubbles up if the table is missing or the connection
    fails.

    Returns {tagged: bool, position: int|None, cap: int}.
    """
    out: dict = {"tagged": False, "position": None,
                 "cap": FOUNDING_CAP, "reason": ""}
    if not email or "@" not in email:
        out["reason"] = "invalid_email"
        return out
    email = email.lower().strip()
    try:
        _ensure_table()
        c = _get_db()
        if c is None:
            out["reason"] = "no_db"
            return out
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM founding_customers")
                cohort_size = int((cur.fetchone() or [0])[0] or 0)
                if cohort_size >= FOUNDING_CAP:
                    out["reason"] = f"cap_reached ({cohort_size}/{FOUNDING_CAP})"
                    return out
                # Already tagged?
                cur.execute(
                    "SELECT 1 FROM founding_customers WHERE email = %s",
                    (email,),
                )
                if cur.fetchone():
                    out["reason"] = "already_tagged"
                    return out
                cur.execute("""
                    INSERT INTO founding_customers
                      (email, plan_at_tag, first_payment_at,
                       stripe_customer_id, notes, contact_status)
                    VALUES (%s, %s, %s, %s, %s, 'auto-tagged')
                """, (email, plan, first_payment_at,
                       stripe_customer_id, notes))
            try: c.commit()
            except Exception: pass
            out["tagged"] = True
            out["position"] = cohort_size + 1
            return out
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        out["reason"] = f"exception: {str(e)[:160]}"
        return out


def notify_admin_of_founding(email: str, position: int, plan: str,
                              stripe_customer_id: str | None) -> None:
    """Send Jonathan an admin alert email so he knows immediately when
    a new founding customer lands. Best-effort — never raises."""
    try:
        from main import send_admin_alert_email
    except Exception:
        return
    cap = FOUNDING_CAP
    subj = f"Founding customer #{position} of {cap} — {email}"
    body = (
        f"<h2>Founding customer #{position} of {cap} just signed up</h2>"
        f"<p><b>Email:</b> {email}</p>"
        f"<p><b>Plan:</b> {plan}</p>"
        f"<p><b>Stripe:</b> {stripe_customer_id or '(none)'}</p>"
        f"<p>The first {cap} paying customers matter disproportionately. "
        f"Reach out personally within the next hour — even a 60-second "
        f"welcome note converts a buyer into a reference customer.</p>"
        f"<p>"
        f"<a href='https://dchub.cloud/api/v1/admin/customer-lookup?"
        f"email={email}'>Customer record</a> · "
        f"<a href='https://dashboard.stripe.com/customers/"
        f"{stripe_customer_id or ''}'>Stripe</a> · "
        f"<a href='https://dchub.cloud/api/v1/admin/founding-customers'>"
        f"Cohort</a>"
        f"</p>"
    )
    try:
        send_admin_alert_email(subj, body)
    except Exception as e:
        logger.warning(f"[founding-customers] admin alert failed: {e}")


# ── Founding-customer welcome email ─────────────────────────────────
# Sent automatically by the Stripe webhook after auto_tag_if_under_cap
# succeeds. Different tone from the standard Pro welcome — acknowledges
# the founding-cohort status, asks for permission to cite (sets the
# consented_to_cite flag for /founders public page), invites a 15-min
# founder call. Sends via Resend (existing infra).

def send_founding_welcome_email(email: str, position: int,
                                  plan: str = "developer") -> bool:
    """Send the founding-customer welcome email. Returns True on
    success, False on any failure (never raises)."""
    resend_key = (os.environ.get("DCHUB_RESEND_API_KEY")
                  or "").strip()
    if not resend_key:
        logger.warning("[founding-customers] no Resend key; welcome "
                       "email skipped")
        return False

    cap = FOUNDING_CAP
    first_name = (email.split("@")[0] or "there").split(".")[0].title()
    consent_link = (f"https://dchub.cloud/api/v1/founding-customers/"
                    f"consent?email={email}")

    subject = f"You're #{position} of {cap} — welcome to DC Hub"
    body_text = f"""Hi {first_name},

Jonathan from DC Hub here. You just landed as founding customer #{position}
of {cap} — which means a lot more than the email signature suggests.

The first {cap} paying customers are the ones who proved this thing was
worth building. You showed up before the case studies, before the
reviews, before the analyst coverage. That carries weight.

Three things, none of which require a reply:

1. Your account is live. The plan you signed up for ({plan}) is active
   and your API key is ready in the dashboard at dchub.cloud/dashboard.

2. If you'd like 15 min on a Zoom this week — me, on you, no script —
   I'd love to hear what you're building and what's missing. Reply
   with a time that works and I'll send a link.

3. If we ever quote you on dchub.cloud/cited-by (with attribution and
   only the words you write back to me — no marketing-speak), would
   that be OK with you? Click here to opt in:
   {consent_link}
   Or just reply "yes" / "no" — I won't ask twice.

Real thanks for the bet.

— Jonathan
   dchub.cloud
   reply directly to this email
"""

    try:
        import urllib.request
        payload = json.dumps({
            "from": "DC Hub <jonathan@dchub.cloud>",
            "to": [email],
            "subject": subject,
            "text": body_text,
            "reply_to": "jonathan@dchub.cloud",
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        logger.info(f"[founding-customers] welcome sent to {email}")
        # Mark in DB
        try:
            c = _get_db()
            if c is not None:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE founding_customers SET "
                        "contact_status = 'welcomed', contacted_at = NOW() "
                        "WHERE email = %s",
                        (email,),
                    )
                try: c.commit()
                except Exception: pass
                c.close()
        except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"[founding-customers] welcome email failed: {e}")
        return False


# ── Admin send-welcome (for backfilling customers tagged before
#    the auto-email path was wired) ───────────────────────────────────
@founding_customers_bp.route(
    "/api/v1/admin/founding-customers/send-welcome",
    methods=["POST"],
)
def admin_send_welcome():
    """Fire send_founding_welcome_email for an already-tagged customer.

    Used for backfilling Kevin (tagged manually before the auto-email
    path went live) and any future case where a customer needs a
    resend. Idempotent at the customer level — they may get the email
    twice if called twice, so use sparingly."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    p = request.get_json(silent=True) or {}
    email = (p.get("email") or request.args.get("email") or "").lower().strip()
    if not email:
        return jsonify(ok=False, error="email_required"), 400
    _ensure_table()
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT email, plan_at_tag, contact_status
                  FROM founding_customers
                 WHERE email = %s
            """, (email,))
            r = cur.fetchone()
            if not r:
                return jsonify(
                    ok=False,
                    error="not_in_cohort",
                    hint=("Customer isn't tagged as founding. Call "
                           "POST /api/v1/admin/founding-customers/tag "
                           "first."),
                ), 404
            # Position is their rank in the cohort by tagged_at ASC
            cur.execute("""
                SELECT COUNT(*) FROM founding_customers
                 WHERE tagged_at <= (
                    SELECT tagged_at FROM founding_customers WHERE email = %s
                 )
            """, (email,))
            position = int((cur.fetchone() or [1])[0] or 1)
    finally:
        try: c.close()
        except Exception: pass

    sent = send_founding_welcome_email(
        email=email,
        position=position,
        plan=(r[1] or "developer"),
    )
    return jsonify(
        ok=sent, email=email, position=position,
        cap=FOUNDING_CAP,
        previous_status=r[2],
        note=("Welcome email " + ("sent" if sent
                                    else "FAILED — check Resend logs")),
    )


# ── Consent endpoint (CC-BY-style opt-in for /founders public page) ──
@founding_customers_bp.route("/api/v1/founding-customers/consent",
                              methods=["GET", "POST"])
def consent():
    """Public — a founding customer can opt in to be listed on the
    /founders public page. Token-less by design (the link is mailed
    directly to them; visiting it = consent). One-click UX."""
    email = (request.args.get("email") or "").lower().strip()
    if not email or "@" not in email:
        return Response(
            "<p>Invalid link. Reply to the welcome email and we'll fix.</p>",
            mimetype="text/html",
        )
    _ensure_table()
    c = _get_db()
    if c is None:
        return Response("<p>System unavailable. Try again shortly.</p>",
                        mimetype="text/html")
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE founding_customers SET "
                "consented_to_cite = TRUE WHERE email = %s "
                "RETURNING email", (email,),
            )
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        if not row:
            return Response(
                "<p>We don't have your email on file. Probably means "
                "you're not in the founding cohort yet — that's OK, just "
                "reply to Jonathan directly.</p>",
                mimetype="text/html",
            )
        return Response(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Thanks — DC Hub</title>"
            "<style>body{font-family:-apple-system,sans-serif;"
            "background:#0a0a0f;color:#f5f5f7;display:flex;"
            "align-items:center;justify-content:center;min-height:100vh;"
            "margin:0;padding:20px}"
            ".card{max-width:480px;text-align:center;"
            "background:#131319;border:1px solid rgba(255,255,255,.06);"
            "border-radius:14px;padding:40px}"
            "h1{font-size:1.5rem;margin:0 0 12px;"
            "background:linear-gradient(135deg,#6366f1,#a855f7);"
            "-webkit-background-clip:text;background-clip:text;"
            "color:transparent}"
            "p{color:#a1a1aa;line-height:1.5}"
            "a{color:#c7d2fe}</style></head><body>"
            "<div class='card'>"
            "<h1>Thanks — consent recorded</h1>"
            "<p>You're now eligible to appear on "
            "<a href='https://dchub.cloud/founders'>dchub.cloud/founders</a> "
            "once enough of the cohort opts in. We'll only quote the words "
            "you write back to us in email — no marketing-speak.</p>"
            "<p style='margin-top:20px;font-size:.85rem'>"
            "Change your mind? Just reply with 'opt out'.</p>"
            "</div></body></html>",
            mimetype="text/html",
        )
    finally:
        try: c.close()
        except Exception: pass


# ── Public /founders page ───────────────────────────────────────────
@founding_customers_bp.route("/founders", methods=["GET"])
def founders_html():
    """Public HTML page listing the consented founding customers as
    social proof. Hides email PII for non-consented rows (just shows
    count). Eyeball-card brand."""
    _ensure_table()
    c = _get_db()
    consented: list = []
    total = 0
    cap = FOUNDING_CAP
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT email, plan_at_tag, tagged_at,
                           consented_to_cite, notes
                      FROM founding_customers
                     ORDER BY tagged_at ASC
                """)
                for r in cur.fetchall():
                    total += 1
                    if r[3]:  # consented_to_cite
                        consented.append({
                            "email": r[0], "plan": r[1],
                            "tagged_at": r[2],
                        })
        finally:
            try: c.close()
            except Exception: pass

    consented_html = ""
    for i, c_row in enumerate(consented[:50], 1):
        em = c_row.get("email") or ""
        # Show first 2 chars + asterisks + domain — light privacy even
        # when consented (operator can swap to full email if customer
        # explicitly OKs)
        masked = (em[:2] + "***@" + em.split("@", 1)[1]) if "@" in em else em
        plan = (c_row.get("plan") or "").title()
        when = str(c_row.get("tagged_at"))[:10] if c_row.get("tagged_at") else ""
        consented_html += (
            f'<div class="founder">'
            f'<div class="founder-num">#{i:02d}</div>'
            f'<div class="founder-info">'
            f'<div class="founder-email">{masked}</div>'
            f'<div class="founder-meta">{plan} · joined {when}</div>'
            f'</div></div>'
        )

    if not consented_html:
        consented_html = (
            '<div style="padding:32px;text-align:center;'
            'color:#71717a;background:#131319;border:1px dashed '
            'rgba(255,255,255,.06);border-radius:14px">'
            'Cohort is building. Once founding customers opt in to be '
            'cited, they appear here.'
            '</div>'
        )

    return Response(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub · Founding customers</title>
<meta name="description" content="The first {cap} paid customers of DC Hub. The people who showed up before the case studies, the reviews, the analyst coverage.">
<meta property="og:title" content="DC Hub · Founding customers">
<meta property="og:description" content="The {cap}-customer cohort that proved DC Hub.">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script defer src="/js/dchub-brand.js"></script>
<style>
  :root{{--bg:#0a0a0f;--surface:#131319;--border:rgba(255,255,255,.06);
    --border-strong:rgba(255,255,255,.1);--text:#f5f5f7;
    --text-dim:#a1a1aa;--text-faint:#71717a;--indigo:#6366f1;
    --violet:#a855f7;
    --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
    --font:'Instrument Sans',-apple-system,sans-serif;
    --mono:'JetBrains Mono','SF Mono',monospace;}}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:var(--font);background:var(--bg);color:var(--text);
    line-height:1.55;-webkit-font-smoothing:antialiased;min-height:100vh;
    position:relative}}
  body::before{{content:'';position:fixed;top:-30%;left:50%;
    transform:translateX(-50%);width:1400px;height:1400px;z-index:0;
    pointer-events:none;
    background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
                                rgba(168,85,247,.06) 30%,transparent 60%)}}
  .wrap{{position:relative;z-index:1;max-width:780px;margin:0 auto;
    padding:64px 24px 80px}}
  header.top{{display:flex;align-items:center;justify-content:space-between;
    margin-bottom:36px;flex-wrap:wrap;gap:12px}}
  a.brand{{display:inline-flex;align-items:center;gap:10px;
    text-decoration:none;color:var(--text)}}
  .progress{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
    letter-spacing:.1em;color:var(--text-faint);
    padding:6px 14px;border-radius:999px;
    background:var(--grad-soft);
    border:1px solid rgba(168,85,247,.22)}}
  .eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
    letter-spacing:.16em;color:var(--violet);font-weight:600;margin-bottom:14px}}
  h1{{font-size:clamp(2rem,4.2vw,2.8rem);font-weight:700;
    letter-spacing:-.03em;line-height:1.05;margin-bottom:16px}}
  h1 .grad{{background:var(--grad);-webkit-background-clip:text;
    background-clip:text;color:transparent}}
  .lede{{color:var(--text-dim);font-size:1.02rem;line-height:1.55;
    max-width:640px;margin-bottom:36px}}
  .cohort{{display:flex;flex-direction:column;gap:8px;margin-bottom:48px}}
  .founder{{display:flex;align-items:center;gap:18px;padding:16px 22px;
    background:var(--surface);border:1px solid var(--border);
    border-radius:14px;transition:border-color .2s ease}}
  .founder:hover{{border-color:var(--border-strong)}}
  .founder-num{{font-family:var(--mono);font-size:1.05rem;font-weight:700;
    color:var(--violet);min-width:44px}}
  .founder-info{{flex:1;min-width:0}}
  .founder-email{{font-weight:600;font-size:.95rem;color:var(--text)}}
  .founder-meta{{font-family:var(--mono);font-size:10px;
    text-transform:uppercase;letter-spacing:.08em;color:var(--text-faint);
    margin-top:4px}}
  .cta{{background:var(--grad-soft);border:1px solid rgba(168,85,247,.22);
    border-radius:14px;padding:28px;text-align:center;margin-top:32px}}
  .cta h3{{font-size:1.1rem;font-weight:700;letter-spacing:-.02em;
    margin-bottom:8px}}
  .cta p{{color:var(--text-dim);font-size:.92rem;margin-bottom:18px}}
  .btn{{display:inline-flex;align-items:center;padding:11px 22px;
    background:var(--grad);color:#fff;text-decoration:none;
    border-radius:999px;font-weight:600;font-size:14px;
    transition:transform .15s ease,box-shadow .15s ease}}
  .btn:hover{{transform:translateY(-1px);
    box-shadow:0 8px 24px rgba(168,85,247,.32)}}
  .foot{{font-family:var(--mono);font-size:10.5px;color:var(--text-faint);
    text-align:center;margin-top:48px;letter-spacing:.06em}}
  .foot a{{color:var(--text-dim);margin:0 8px;text-decoration:none}}
  .foot a:hover{{color:var(--text)}}
</style>
</head><body>
<div class="wrap">
  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="progress">{total} of {cap} founding seats taken</span>
  </header>

  <div class="eyebrow">Founding customers</div>
  <h1>The first {cap}. <span class="grad">They showed up early.</span></h1>
  <p class="lede">Before the case studies, before the reviews, before the analyst coverage — these are the operators, investors, and AI agents who paid for DC Hub when it was still proving the value-prop. We don't forget that. Listed here with their permission.</p>

  <div class="cohort">
    {consented_html}
  </div>

  <div class="cta">
    <h3>{cap - total} founding seats still open</h3>
    <p>The first {cap} paid customers become the founding cohort — listed here forever (with permission), with founder-touch onboarding and direct access. Once we hit {cap}, the cohort closes.</p>
    <a href="/pricing" class="btn">See plans</a>
  </div>

  <div class="foot">
    <a href="/">dchub.cloud</a> · <a href="/cited-by">cited by</a> · <a href="/reports/monthly">monthly trend</a> · <a href="/pricing">pricing</a>
  </div>
</div>
</body></html>""",
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=300"})


def _smoke():
    logger.info(f"[founding-customers] ready · cap={FOUNDING_CAP} · "
                 f"POST /tag · GET /api/v1/admin/founding-customers · "
                 f"GET /founders (public) · "
                 f"auto_tag_if_under_cap() importable · "
                 f"send_founding_welcome_email() importable")

_smoke()
