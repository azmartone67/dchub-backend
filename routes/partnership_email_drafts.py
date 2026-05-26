"""
partnership_email_drafts.py — pre-staged emails for partner contacts.

Phase ZZZZZ-round47.16 (2026-05-25). After LinkedIn (Wed) + press
release (Tue) the natural escalation is a direct email to a known
contact at each partner with the /partners#anchor URL pre-filled.

Two modes:

  1. DRAFT (GET, no auth)  — returns subject + body + suggested-to
     for paste into Gmail/Outlook. Operator hits send themselves.

  2. SEND (POST, X-Admin-Key required) — actually fires via Resend
     SMTP to the specified recipient. Tracks sends in DB so we don't
     accidentally double-email the same contact for the same track.

Endpoints:
  GET  /api/v1/partnerships/email/<slug>            — draft, paste-ready
  POST /api/v1/partnerships/email/<slug>/send       — send now (admin)
       body: {"to": "recipient@dcd.com", "personal_note": "..."}
  GET  /api/v1/partnerships/email/log               — sent history
"""
import os
import datetime
import smtplib
from contextlib import contextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Blueprint, request, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

from routes.linkedin_partnership_weekly import _TRACKS as _LINKEDIN_TRACKS

partnership_email_bp = Blueprint("partnership_email", __name__,
                                  url_prefix="/api/v1/partnerships/email")

# ── Per-track contact suggestions ────────────────────────────────────
# These are PUBLIC role-emails / press contacts gathered from each
# org's public website. Always verify before sending.
_CONTACT_SUGGESTIONS = {
    "dchawk":   ["info@dchawk.com"],
    "dcbyte":   ["info@dcbyte.com"],
    "dcd":      ["editorial@datacenterdynamics.com", "info@datacenterdynamics.com"],
    "dcf":      ["editorial@datacenterfrontier.com"],
    "cbre":     ["datacenters@cbre.com", "research@cbre.com"],
    "jll":      ["data-centers@jll.com", "research@jll.com"],
    "partners": ["press@reuters.com", "techreporters@bloomberg.net",
                  "editorial@datacenterfrontier.com"],
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
FROM_EMAIL = os.environ.get("OUTREACH_FROM_EMAIL", "partnerships@dchub.cloud").strip()
FROM_NAME  = os.environ.get("OUTREACH_FROM_NAME", "Jonathan Martone, DC Hub").strip()


def _ensure_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS partnership_emails_sent (
                    id            SERIAL PRIMARY KEY,
                    track_slug    TEXT NOT NULL,
                    to_email      TEXT NOT NULL,
                    subject       TEXT,
                    personal_note TEXT,
                    sent_at       TIMESTAMPTZ DEFAULT NOW(),
                    smtp_status   INT,
                    smtp_detail   TEXT,
                    UNIQUE(track_slug, to_email)
                );
                CREATE INDEX IF NOT EXISTS ix_pes_sent ON partnership_emails_sent(sent_at DESC);
            """)
    except Exception:
        pass


_ensure_table()


def _is_admin(req):
    expected = os.environ.get("DCHUB_ADMIN_KEY", "").strip()
    if not expected:
        return False
    got = req.headers.get("X-Admin-Key", "").strip()
    return bool(got and got == expected)


_PARTNER_KEYWORDS = {
    "dchawk":  ["dchawk", "dc hawk"],
    "dcbyte":  ["dcbyte", "dc byte"],
    "dcd":     ["data center dynamics", "datacenterdynamics", " dcd "],
    "dcf":     ["data center frontier", "datacenterfrontier"],
    "cbre":    ["cbre", "data center solutions"],
    "jll":     ["jll", "jones lang lasalle"],
    "partners": ["partnership", "partnerships", "industry"],
}


def _recent_partner_mention(slug):
    """Query press_releases for a recent mention of this partner so the
    email opener can reference something specific. Returns dict with
    title + date + relative phrase, or None if nothing relevant in 60d."""
    keywords = _PARTNER_KEYWORDS.get(slug.lower(), [])
    if not keywords or not (_pg and _dsn()):
        return None
    try:
        with _conn() as c, c.cursor() as cur:
            # Find a press release where title or body mentions the partner
            conds = " OR ".join(["LOWER(title) LIKE %s OR LOWER(body) LIKE %s"] * len(keywords))
            params = []
            for kw in keywords:
                params.extend([f"%{kw.lower()}%", f"%{kw.lower()}%"])
            cur.execute(f"""
                SELECT title, slug, created_at
                  FROM press_releases
                 WHERE created_at > NOW() - INTERVAL '60 days'
                   AND ({conds})
                 ORDER BY created_at DESC LIMIT 1
            """, params)
            r = cur.fetchone()
            if r:
                days_ago = (datetime.datetime.utcnow() - r[2].replace(tzinfo=None)).days if r[2] else 0
                rel = "yesterday" if days_ago <= 1 else f"{days_ago} days ago" if days_ago < 30 else "last month"
                return {
                    "title":   r[0],
                    "slug":    r[1],
                    "rel":     rel,
                    "url":     f"https://dchub.cloud/press-release/{r[1]}",
                }
    except Exception:
        pass
    return None


def _build_email(track, personal_note=""):
    """Produce subject + html + text from the track. r47.20 auto-personalize +
    r47.22 Switzerland-model framing: every opener makes clear this is an
    open invitation, not an announcement of an executed partnership."""
    partner_name_map = {
        "dchawk":  "DCHawk",
        "dcbyte":  "DCByte",
        "dcd":     "Data Center Dynamics",
        "dcf":     "Data Center Frontier",
        "cbre":    "CBRE",
        "jll":     "JLL",
        "partners": "your team",
    }
    partner_label = partner_name_map.get(track["slug"], track["slug"].upper())

    subject = f"DC Hub → {partner_label}: open partnership invitation (Switzerland model)"

    if personal_note:
        intro = personal_note.strip()
    else:
        # Try to find a recent press mention to make the opener concrete
        mention = _recent_partner_mention(track["slug"])
        if mention:
            intro = (
                f"Hi —\n\n"
                f"We covered \"{mention['title']}\" {mention['rel']} on our "
                f"daily press cadence — figured you'd want to see it: {mention['url']}\n\n"
                f"While I had your address, I'm also reaching out about an open partnership "
                f"invitation DC Hub has published for {partner_label}. It's an "
                f"invitation, not an announcement — we'd love to hear if there's interest."
            )
        else:
            intro = (
                f"Hi —\n\n"
                f"I'm Jonathan, founder of DC Hub — the neutral, live data layer beneath the "
                f"data-center research industry (cited by 96+ AI platforms including ChatGPT, "
                f"Claude, Gemini, Cursor, Cline). We've just published an open partnership "
                f"invitation for {partner_label} as part of our 'Switzerland model'. "
                f"No partnership currently exists; we're publicly extending the offer to see "
                f"if there's interest."
            )

    # r47.17: emails use /go/partners/<slug> click-tracker URL so we
    # can attribute partnership clicks to the email channel separately
    # from organic / LinkedIn traffic.
    tracked_url = f"https://api.dchub.cloud/go/partners/{track['slug']}?src=email"
    tracked_root = f"https://api.dchub.cloud/go/partners?src=email"

    text = (
        f"{intro}\n\n"
        f"{track['headline']}\n"
        f"{'─' * len(track['headline'])}\n\n"
        f"{track['body']}\n\n"
        f"Full open invitation: {tracked_url}\n"
        f"All six Switzerland-model invitations: {tracked_root}\n\n"
        f"This isn't a bulk send — happy to keep it conversational. What's the easiest "
        f"first step on your side? Even a polite \"not now\" is useful for us so we know "
        f"we're not pestering.\n\n"
        f"Jonathan Martone\n"
        f"Founder, DC Hub\n"
        f"jm@dchub.cloud · https://www.linkedin.com/in/jonathanmartone/\n\n"
        f"P.S. To be crystal clear: this is an open invitation, not an announcement of "
        f"any executed partnership. If we ever publish about a partnership with you, "
        f"it'll be because we've actually signed something together.\n"
    )

    # Plain-text-to-HTML conversion that preserves paragraph structure
    paragraphs = "".join(
        f"<p>{p.replace(chr(10), '<br>')}</p>"
        for p in text.split("\n\n") if p.strip()
    )
    html = (
        f"<!DOCTYPE html><html><body style=\"font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;"
        f"max-width:620px;margin:0 auto;padding:24px;line-height:1.55;color:#0f172a\">"
        f"<p style=\"color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;"
        f"font-weight:600\">DC Hub · {track['slug'].upper()} Partnership</p>"
        f"{paragraphs}"
        f"<hr style=\"border:none;border-top:1px solid #e2e8f0;margin:20px 0\">"
        f"<p style=\"font-size:.82rem;color:#94a3b8\">"
        f"Sent because you're listed as the public partnership/editorial contact for {track['slug'].upper()}. "
        f"This isn't a bulk send — one human will read each reply. To opt out, "
        f"reply with \"remove\" and we won't email you again.</p></body></html>"
    )
    return {"subject": subject, "text": text, "html": html}


def _send_resend(to_email, subject, html, text):
    if not RESEND_KEY:
        return 503, "RESEND_API_KEY not set"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]      = to_email
    msg["Reply-To"] = "jm@dchub.cloud"
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP("smtp.resend.com", 587, timeout=20) as srv:
            srv.starttls()
            srv.login("resend", RESEND_KEY)
            srv.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        return 200, "smtp_ok"
    except Exception as e:
        return 500, f"{type(e).__name__}: {str(e)[:140]}"


def _record_send(slug, to_email, subject, personal_note, status, detail):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO partnership_emails_sent
                  (track_slug, to_email, subject, personal_note, smtp_status, smtp_detail)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (track_slug, to_email) DO UPDATE SET
                  subject=EXCLUDED.subject,
                  personal_note=EXCLUDED.personal_note,
                  sent_at=NOW(),
                  smtp_status=EXCLUDED.smtp_status,
                  smtp_detail=EXCLUDED.smtp_detail
            """, (slug, to_email, subject, personal_note or "", status, detail[:500]))
    except Exception:
        pass


@partnership_email_bp.route("/<slug>", methods=["GET"], strict_slashes=False)
def draft(slug):
    track = next((t for t in _LINKEDIN_TRACKS if t["slug"] == slug.lower()), None)
    if not track:
        return jsonify({"error": "unknown_slug",
                        "available": [t["slug"] for t in _LINKEDIN_TRACKS]}), 400
    email = _build_email(track)
    return jsonify({
        "track":          slug,
        "anchor":         track["anchor"],
        "url":            track["url"],
        "subject":        email["subject"],
        "text_body":      email["text"],
        "html_body":      email["html"],
        "suggested_to":   _CONTACT_SUGGESTIONS.get(slug.lower(), []),
        "hint":           f"GET-with-?personal_note=... to inject a personal opener. "
                          f"POST to /send (admin) to actually fire.",
    }), 200


@partnership_email_bp.route("/<slug>/send", methods=["POST"], strict_slashes=False)
def send(slug):
    if not _is_admin(request):
        return jsonify({"error": "unauthorized",
                        "hint": "X-Admin-Key required"}), 401
    track = next((t for t in _LINKEDIN_TRACKS if t["slug"] == slug.lower()), None)
    if not track:
        return jsonify({"error": "unknown_slug"}), 400
    body = request.get_json(silent=True) or {}
    to_email = (body.get("to") or "").strip()
    personal_note = (body.get("personal_note") or "").strip()
    if not to_email or "@" not in to_email:
        return jsonify({"error": "invalid_to",
                        "hint": "POST {\"to\":\"editor@dcd.com\",\"personal_note\":\"opt\"}"}), 400

    email = _build_email(track, personal_note)
    status, detail = _send_resend(to_email, email["subject"], email["html"], email["text"])
    _record_send(slug, to_email, email["subject"], personal_note, status, detail)
    return jsonify({
        "ok":            status == 200,
        "track":         slug,
        "to":            to_email,
        "subject":       email["subject"],
        "smtp_status":   status,
        "smtp_detail":   detail,
        "at":            datetime.datetime.utcnow().isoformat() + "Z",
    }), 200 if status == 200 else 502


@partnership_email_bp.route("/log", methods=["GET"])
def log():
    if not (_pg and _dsn()):
        return jsonify({"recent": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT track_slug, to_email, subject, sent_at, smtp_status
                  FROM partnership_emails_sent
                 ORDER BY sent_at DESC LIMIT 30
            """)
            recent = [{
                "track": r[0], "to": r[1], "subject": r[2],
                "sent_at": r[3].isoformat() if r[3] else None,
                "smtp_status": r[4],
            } for r in cur.fetchall()]
        return jsonify({"recent": recent, "count": len(recent)}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:140], "recent": []}), 200
