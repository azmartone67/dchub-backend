"""
Media outreach pipeline (2026-05-19).

DC Hub has social-publishing (LinkedIn/Twitter/Bluesky) but no JOURNALIST
outreach pipeline. This module closes that:

  • Curated journalist contact list (industry-publication writers
    covering DC + AI + energy)
  • Pitch-template generator (auto-drafts a pitch around a current
    DC Hub story — press release, DCPI mover, M&A deal)
  • Send via Resend (existing DCHUB_RESEND_API_KEY infrastructure)
  • Track sent/replied/converted into a media_outreach_log table
  • Brain detector check_media_outreach_silent fires when no pitches
    sent in 14 days

Endpoints:
  GET  /api/v1/media/journalists       list of curated contacts
  POST /api/v1/media/pitch-draft       generate a pitch (admin)
  POST /api/v1/media/pitch-send        send via Resend (admin)
  GET  /api/v1/media/outreach-log      sent/replied tracking
  GET  /media/outreach                 admin HTML dashboard

This is the OUTBOUND extension of dchub_media_hub (which handles
inbound + the on-site media surface).
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
media_outreach_bp = Blueprint("media_outreach", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
_FROM_ADDR = "press@dchub.cloud"


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_outreach_log (
    id              BIGSERIAL PRIMARY KEY,
    recipient_email TEXT NOT NULL,
    recipient_name  TEXT,
    publication     TEXT,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    pitch_topic     TEXT,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    replied_at      TIMESTAMPTZ,
    converted       BOOLEAN DEFAULT FALSE,
    resend_id       TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS ix_mol_recipient ON media_outreach_log(recipient_email);
CREATE INDEX IF NOT EXISTS ix_mol_sent ON media_outreach_log(sent_at DESC);
"""

_SCHEMA_INIT = False

def _ensure_schema():
    global _SCHEMA_INIT
    if _SCHEMA_INIT: return
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(_SCHEMA)
            try: c.commit()
            except Exception: pass
            _SCHEMA_INIT = True
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"media_outreach schema init failed: {e}")


# Curated journalist + publication contacts. Names are illustrative —
# user should verify on each publication's masthead before sending.
# Conservative: only well-known industry-DC writers.
_JOURNALISTS = [
    {"name": "Sebastian Moss",       "publication": "Data Center Dynamics (DCD)",
     "beat":  "data centers, hyperscale, M&A", "email": "sebastian.moss@datacenterdynamics.com",
     "linkedin": "https://www.linkedin.com/in/sebastianmoss/",
     "notes": "Editor-in-Chief. Likes scoops + data."},
    {"name": "Yevgeniy Sverdlik",    "publication": "Data Center Knowledge (DCK)",
     "beat":  "data center infrastructure", "email": "yevgeniy.sverdlik@informa.com",
     "linkedin": "https://www.linkedin.com/in/yevgeniysverdlik/",
     "notes": "Veteran DC writer. Likes industry-trend pieces."},
    {"name": "Karl Freund",          "publication": "Forbes (AI Infrastructure)",
     "beat":  "AI compute, GPUs, data center power",
     "email": "kfreund@cambrian-ai.com",
     "linkedin": "https://www.linkedin.com/in/karlfreund/",
     "notes": "AI infra columnist. Likes hyperscale power stories."},
    {"name": "Aaron Tilley",          "publication": "Wall Street Journal",
     "beat":  "Big Tech infrastructure", "email": "aaron.tilley@wsj.com",
     "linkedin": "https://www.linkedin.com/in/aaron-tilley/",
     "notes": "WSJ tech reporter. Likes deal-flow + capex angles."},
    {"name": "Reed Albergotti",       "publication": "Semafor Technology",
     "beat":  "Tech business + infrastructure", "email": "reed@semafor.com",
     "linkedin": "https://www.linkedin.com/in/reedalbergotti/",
     "notes": "Semafor's tech reporter. Likes contrarian / data-driven angles."},
    {"name": "Asa Fitch",             "publication": "Wall Street Journal",
     "beat":  "Semiconductors + AI compute", "email": "asa.fitch@wsj.com",
     "linkedin": "https://www.linkedin.com/in/asafitch/",
     "notes": "Chips + DC angles. Strong on supply-side stories."},
    {"name": "Stephen Council",       "publication": "SFGate / Tech",
     "beat":  "Bay Area tech + data centers",
     "email": "stephen.council@sfgate.com",
     "notes": "West-coast DC + Silicon Valley angles."},
    {"name": "Rich Miller",           "publication": "Data Center Frontier",
     "beat":  "data centers — broadest DC industry coverage",
     "email": "rmiller@datacenterfrontier.com",
     "linkedin": "https://www.linkedin.com/in/rich-miller-dcf/",
     "notes": "Founder/Editor. The bible of US DC industry. Read by every operator + investor."},
    {"name": "Bill Kleyman",          "publication": "Data Center Frontier",
     "beat":  "infrastructure, AI workloads",
     "email": "bkleyman@gmail.com",
     "notes": "Frequent DCF contributor. Likes hands-on technical angles."},
    {"name": "Tom Krazit",            "publication": "Runtime (newsletter) + freelance",
     "beat":  "Cloud infrastructure + DCs",
     "email": "tom@runtime.news",
     "notes": "Cloud + DC analyst. Independent. Read by industry insiders."},
]


def _compose_pitch(topic: str, story: dict, recipient: dict) -> tuple[str, str]:
    """Generate (subject, body) for a journalist pitch around a topic."""
    first_name = (recipient.get("name") or "").split()[0]
    if not first_name: first_name = "there"

    subject = f"DC Hub data: {story.get('headline', topic)}"
    body = f"""Hi {first_name},

I run dchub.cloud — a real-time data center intelligence platform tracking
21,000+ facilities, 280+ markets, $324B+ M&A history, and live grid data
across 11 ISOs. CC-BY-4.0 license — free to cite with attribution.

{story.get('story_paragraph', '')}

The data behind this: {story.get('data_url', 'https://dchub.cloud/industry/pulse')}
Full methodology: {story.get('methodology_url', 'https://dchub.cloud/dcpi')}

A few framings that might work for {recipient.get('publication', 'your publication')}:
- {story.get('angle_1', 'The headline number with context')}
- {story.get('angle_2', 'What it means for the industry')}
- {story.get('angle_3', 'Forward-looking implication')}

Happy to provide:
- Pre-built charts/graphics in PNG or SVG
- Raw JSON or CSV for any DC Hub data
- A 15-min call with our data + a quote you can use

No quarterly PDF release cycle, no $25K license — just live JSON if it
helps your reporting.

Best,
[your name]
DC Hub · https://dchub.cloud
press@dchub.cloud

P.S. — If this isn't your beat, would you point me to the right person on
your team? Happy to add you to a once-a-month DC industry data brief
(short, no marketing, just numbers) if that'd be useful — reply 'subscribe'."""
    return subject, body


@media_outreach_bp.route("/api/v1/media/journalists", methods=["GET"])
def journalists():
    """List curated journalist contacts. Public so user can see + edit."""
    return jsonify(
        ok=True,
        count=len(_JOURNALISTS),
        journalists=_JOURNALISTS,
        note=("Hand-curated list. Verify each contact on the publication's "
              "masthead before sending — names + emails change. To extend, "
              "edit _JOURNALISTS in routes/media_outreach.py"),
    ), 200


@media_outreach_bp.route("/api/v1/media/pitch-draft", methods=["POST"])
def pitch_draft():
    """Draft a pitch for a specific journalist around a topic. Admin-gated.

    POST body:
      { recipient_email: "...",
        topic: "industry_pulse" | "dcpi_mover" | "ma_deal" | <custom>,
        story: {headline, story_paragraph, data_url, methodology_url,
                angle_1, angle_2, angle_3} }
    """
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    email = body.get("recipient_email")
    if not email:
        return jsonify(ok=False, error="recipient_email required"), 400
    recipient = next((j for j in _JOURNALISTS if j["email"] == email), None)
    if not recipient:
        return jsonify(ok=False, error=f"unknown recipient {email}",
                       hint="GET /api/v1/media/journalists for the list"), 404

    topic = body.get("topic", "industry_pulse")
    story = body.get("story") or {
        "headline": "Weekly DC industry stat sheet",
        "story_paragraph": (
            "DC Hub publishes a weekly machine-readable stat sheet of "
            "US/global data center facility counts, M&A volume, pipeline "
            "MW, and AI-agent adoption metrics. CC-BY-4.0 (free to cite). "
            "Designed for analyst + journalist use — schema.org Dataset "
            "markup, every metric sourced + timestamped."),
        "data_url": "https://dchub.cloud/industry/pulse",
        "methodology_url": "https://dchub.cloud/dcpi/methodology",
        "angle_1": "DC industry data is mostly locked behind $25K/yr paywalls (DCHawk, dcByte). Our weekly stat sheet is free.",
        "angle_2": "AI agents (ChatGPT/Claude/Perplexity/Gemini) auto-cite our MCP server in real time — see /cited-by.",
        "angle_3": "Pipeline + DCPI rankings shift weekly. Static quarterly reports miss the inflection points.",
    }
    subject, txt = _compose_pitch(topic, story, recipient)
    return jsonify(
        ok=True,
        recipient=recipient,
        subject=subject,
        body=txt,
        note=("Pitch drafted. To send: POST /api/v1/media/pitch-send "
              "with the same recipient_email + the drafted subject/body."),
    ), 200


@media_outreach_bp.route("/api/v1/media/pitch-send", methods=["POST"])
def pitch_send():
    """Send a pitch via Resend. Admin-gated.

    POST body: { recipient_email, subject, body, topic, story }
    """
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    _ensure_schema()
    if not _RESEND_KEY:
        return jsonify(ok=False, error="DCHUB_RESEND_API_KEY not set"), 503

    b = request.get_json(silent=True) or {}
    email = b.get("recipient_email"); subject = b.get("subject"); txt = b.get("body")
    if not (email and subject and txt):
        return jsonify(ok=False, error="recipient_email + subject + body required"), 400

    # Phase ZZZZ-cooldown (2026-05-19): prevent inbox storms (Rich got
    # 4 emails in 6 min during a test). Reject if same recipient was
    # already pitched in last 7 days. Override with ?force=true.
    force = (request.args.get("force") or b.get("force") or "").lower() in ("1","true","yes")
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT id, sent_at FROM media_outreach_log
                 WHERE recipient_email = %s
                   AND sent_at >= NOW() - INTERVAL '7 days'
                 ORDER BY sent_at DESC LIMIT 1
            """, (email,))
            prev = cur.fetchone()
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        prev = None
    if prev and not force:
        return jsonify(
            ok=False,
            error="cooldown",
            hint=(f"Recipient {email} was already pitched at {prev[1].isoformat()}. "
                  f"7-day cooldown to prevent spam. Add ?force=true if intentional. "
                  f"Most likely fix: pick a different recipient from "
                  f"GET /api/v1/media/journalists"),
            previous_send_id=prev[0],
            previous_sent_at=prev[1].isoformat(),
            cooldown_block=True,
        ), 429

    # Phase ZZZZ-pitch-guard (2026-05-19): prevent placeholder leaks like
    # subject:"<from draft>" or body:"<from draft>" from being sent to
    # real journalists. Either reject + return draft auto-filled, or
    # accept body=="auto" and inline-generate.
    placeholder_markers = ("<from draft>", "<draft>", "<TODO>", "<placeholder>",
                            "from draft", "FROM DRAFT")
    if any(m in subject for m in placeholder_markers) or any(m in txt for m in placeholder_markers):
        return jsonify(ok=False,
                       error="placeholder text detected in subject or body",
                       hint=("Looks like you sent '<from draft>' literally. "
                             "Either (a) re-run /pitch-draft and copy the full "
                             "subject + body strings into this call, OR "
                             "(b) re-run this call with body:'auto' to auto-"
                             "inline the draft."),
                       safety_block=True), 400

    # Auto-mode: caller can pass body:"auto" + topic to skip the
    # copy-paste step entirely.
    if txt.strip().lower() == "auto":
        topic = b.get("topic", "industry_pulse")
        story = b.get("story") or {}
        recipient_obj = next((j for j in _JOURNALISTS if j["email"] == email), None)
        if not recipient_obj:
            return jsonify(ok=False,
                           error=f"unknown recipient {email}",
                           hint="GET /api/v1/media/journalists for the list"), 404
        if not story:
            # Default story (same as the draft endpoint)
            story = {
                "headline": "Weekly DC industry stat sheet",
                "story_paragraph": (
                    "DC Hub publishes a weekly machine-readable stat sheet of "
                    "US/global data center facility counts, M&A volume, "
                    "pipeline MW, and AI-agent adoption metrics. CC-BY-4.0 "
                    "(free to cite). Designed for analyst + journalist use — "
                    "schema.org Dataset markup, every metric sourced + timestamped."),
                "data_url": "https://dchub.cloud/industry/pulse",
                "methodology_url": "https://dchub.cloud/dcpi/methodology",
                "angle_1": "DC industry data is mostly locked behind $25K/yr paywalls (DCHawk, dcByte). Our weekly stat sheet is free.",
                "angle_2": "AI agents (ChatGPT/Claude/Perplexity/Gemini) auto-cite our MCP server in real time — see /cited-by.",
                "angle_3": "Pipeline + DCPI rankings shift weekly. Static quarterly reports miss the inflection points.",
            }
        subject, txt = _compose_pitch(topic, story, recipient_obj)

    recipient = next((j for j in _JOURNALISTS if j["email"] == email), None)

    # Send via Resend
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_KEY}",
                     "Content-Type": "application/json"},
            json={"from": _FROM_ADDR,
                  "to": [email],
                  "subject": subject,
                  "text": txt},
            timeout=15,
        )
        rj = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text[:200]}
        if r.status_code not in (200, 201):
            return jsonify(ok=False, status=r.status_code, response=rj,
                           hint="Resend rejected. Check from-address verified domain."), 502
        resend_id = rj.get("id")
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 503

    # Log
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                INSERT INTO media_outreach_log
                  (recipient_email, recipient_name, publication, subject,
                   body, pitch_topic, resend_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, sent_at
            """, (email, (recipient or {}).get("name"),
                  (recipient or {}).get("publication"),
                  subject, txt, b.get("topic"), resend_id))
            row = cur.fetchone(); c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=f"sent but log-write failed: {e}"), 500

    return jsonify(
        ok=True,
        id=row[0],
        sent_at=row[1].isoformat() if row[1] else None,
        recipient_email=email,
        resend_id=resend_id,
    ), 200


@media_outreach_bp.route("/api/v1/media/outreach-log", methods=["GET"])
def outreach_log():
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT id, recipient_email, recipient_name, publication,
                       subject, pitch_topic, sent_at, replied_at, converted
                FROM media_outreach_log
                ORDER BY sent_at DESC LIMIT 50
            """)
            rows = cur.fetchall() or []
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503
    log = [{
        "id":      r[0], "to": r[1], "name": r[2], "pub": r[3],
        "subject": r[4], "topic": r[5],
        "sent_at": r[6].isoformat() if r[6] else None,
        "replied_at": r[7].isoformat() if r[7] else None,
        "converted": bool(r[8]),
    } for r in rows]
    total = len(log)
    replied = sum(1 for x in log if x["replied_at"])
    converted = sum(1 for x in log if x["converted"])
    return jsonify(
        ok=True,
        total=total, replied=replied, converted=converted,
        reply_rate_pct=round(replied / max(total, 1) * 100, 1),
        conversion_rate_pct=round(converted / max(total, 1) * 100, 1),
        log=log,
    ), 200


@media_outreach_bp.route("/media/outreach", methods=["GET"])
def outreach_page():
    """Admin HTML dashboard for the outreach pipeline."""
    return Response(f"""<!doctype html><html><head><meta charset=utf-8>
<title>Media Outreach · DC Hub</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:980px;margin:0 auto;padding:2rem 1rem;color:#1f2937}}
.kpi{{display:inline-block;margin:1rem 1.5rem 1rem 0}}.kpi-v{{font-size:2rem;font-weight:800;font-family:monospace}}
.kpi-l{{color:#6b7280;font-size:.85rem}}
table{{width:100%;border-collapse:collapse;margin-top:1rem}}
th,td{{padding:.5rem;border-bottom:1px solid #e5e7eb;text-align:left;font-size:.85rem}}
.muted{{color:#6b7280}}
</style></head><body>
<h1>Media Outreach Pipeline</h1>
<p class="muted">Outbound journalist pitches via Resend. Tracks sent/replied/converted.</p>

<div id="kpis">Loading...</div>
<h2>Journalists ({len(_JOURNALISTS)})</h2>
<p class="muted">JSON: <a href="/api/v1/media/journalists">/api/v1/media/journalists</a> · log: <a href="/api/v1/media/outreach-log">/api/v1/media/outreach-log</a></p>
<table id="journalists"></table>

<script>
fetch('/api/v1/media/outreach-log').then(r=>r.json()).then(d => {{
  document.getElementById('kpis').innerHTML =
    `<div class="kpi"><div class="kpi-v">${{d.total}}</div><div class="kpi-l">Total sent</div></div>`
    + `<div class="kpi"><div class="kpi-v">${{d.replied}}</div><div class="kpi-l">Replied (${{d.reply_rate_pct}}%)</div></div>`
    + `<div class="kpi"><div class="kpi-v">${{d.converted}}</div><div class="kpi-l">Converted (${{d.conversion_rate_pct}}%)</div></div>`;
}});
fetch('/api/v1/media/journalists').then(r=>r.json()).then(d => {{
  document.getElementById('journalists').innerHTML =
    '<tr><th>Name</th><th>Publication</th><th>Beat</th><th>Email</th></tr>' +
    d.journalists.map(j =>
      `<tr><td>${{j.name}}</td><td>${{j.publication}}</td><td class="muted">${{j.beat}}</td>` +
      `<td><a href="mailto:${{j.email}}">${{j.email}}</a></td></tr>`).join('');
}});
</script>
</body></html>""", mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
