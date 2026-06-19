"""
routes/brain_innovation_email.py — DAILY EMAIL DIGEST of the brain INNOVATION
front, pushed to the operator inbox (2026-06-19).

THE PROBLEM THIS SOLVES: the brain now autonomously produces VERIFIED,
adversarially-refuted analysis — a self-chosen agenda, human + self-directed
investigations, and ranked propose-only enhancement proposals — surfaced on the
browser-openable innovation dashboard (routes/brain_innovation_dashboard.py). But
the operator still has to REMEMBER to open that page. This module pushes the SAME
consolidated digest to the inbox once a day so the innovation front is SEEN
without opening anything.

REUSE — DATA: this does NOT re-query the three tables differently. It calls the
EXACT SAME builder the dashboard uses —
routes.brain_innovation_dashboard.build_digest() — which reads brain_self_agenda
/ brain_investigations / brain_enhancement_proposals and flattens each item. One
source of truth; the email and the page can never disagree.

REUSE — SENDER: this is OPERATOR OPS MAIL (transactional), NOT marketing. It
ships via the SAME resilient transactional sender the ops alerters use —
email_fallback.send_email_resilient (Resend primary / SendGrid fallback, direct
HTTP + User-Agent, DB-independent, never raises) — exactly like
routes/health_alerter.py / routes/site_sentinel.py. It MUST NOT route through the
marketing consent choke-point (routes/marketing_send.py) and MUST NOT require
marketing_opt_in — operator ops mail is not subject to marketing consent.

SHIPS DARK: BRAIN_INNOVATION_EMAIL_ENABLED defaults OFF. With it off the send
endpoint returns {sent:false, skipped:"disabled"} and sends NO email (the FIRST
guard, before any compose / provider check). Recipient comes from env
(BRAIN_DIGEST_EMAIL → ADMIN_ALERT_EMAIL); never hardcoded. No provider key →
{skipped:"no_provider"} (never crashes). Admin-gated endpoint.

ENDPOINT (admin-gated, the cron hits this):
  POST /api/v1/brain/innovation/digest/email   -> runs send_innovation_digest();
                                                  returns the skip reason so the
                                                  cron logs are legible.
  POST .../digest/email?dry=1                   -> compose + RETURN the html
                                                  WITHOUT sending (preview).

HONEST + SAFE: the body renders the REAL digest (the underlying investigate()
data is already adversarially-fenced; the honest-numbers canon still applies). No
fabricated numbers — every value comes straight from build_digest().
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger("brain_innovation_email")

brain_innovation_email_bp = Blueprint("brain_innovation_email", __name__)

# How many items to render per section in the email — keep it skimmable. The
# dashboard shows everything; the email is a glance, so we cap.
_MAX_PER_SECTION = 6

# The deep link the operator opens for the full, interactive surface. The
# operator appends ?admin_key= (we never put a key in the email body).
_DASHBOARD_URL = "https://dchub.cloud/api/v1/brain/innovation/dashboard"


# ── env helpers ──────────────────────────────────────────────────────
def _enabled() -> bool:
    """Master kill switch — ships DARK. Default OFF: with it off NO email is
    composed or sent (the first guard in send_innovation_digest)."""
    return str(os.environ.get("BRAIN_INNOVATION_EMAIL_ENABLED", "")).lower() in (
        "1", "true", "yes",
    )


def _recipient() -> str:
    """Operator inbox from env — BRAIN_DIGEST_EMAIL, falling back to
    ADMIN_ALERT_EMAIL (the same address the ops alerters use). NEVER hardcode an
    address; empty -> the caller skips with no_recipient."""
    return (os.environ.get("BRAIN_DIGEST_EMAIL")
            or os.environ.get("ADMIN_ALERT_EMAIL") or "").strip()


def _has_provider_key() -> bool:
    """True if either transactional provider key is present. Mirrors what
    email_fallback.send_email_resilient checks (Resend primary / SendGrid
    fallback) so we can skip cleanly instead of attempting a doomed send."""
    return bool(
        (os.environ.get("RESEND_API_KEY") or "").strip()
        or (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
        or (os.environ.get("SENDGRID_API_KEY") or "").strip()
    )


def _admin_ok() -> bool:
    """Admin gate — same shape every brain admin endpoint accepts: an
    internal/admin key from a header OR the ?admin_key= query param. Mirrors
    brain_innovation_dashboard._admin_ok."""
    _keys = set()
    for _n in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "INTERNAL_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    _sent = (request.headers.get("X-Admin-Key")
             or request.headers.get("X-Internal-Key")
             or request.args.get("admin_key") or "").strip()
    # If no admin key is configured at all, fail closed (no open endpoint).
    return bool(_sent) and _sent in _keys


# ── render helpers (best-effort, never raise) ────────────────────────
def _esc(s) -> str:
    """Minimal HTML escape for body text pulled from the digest."""
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _conf_str(c) -> str:
    """Confidence as a fixed-2 string, or — when missing/unparseable."""
    if c is None:
        return "—"
    try:
        return f"{float(c):.2f}"
    except Exception:
        return "—"


def _refutation_verdict(it: dict) -> str:
    """Human-readable refutation verdict from the flattened item:
    survived / refuted / un-refuted / unparsed / inconclusive. Best-effort."""
    if not it.get("refutation_attempted"):
        return "un-refuted"
    if it.get("refutation_unparsed"):
        return "refutation unparsed"
    survived = it.get("refutation_survived")
    if survived is True:
        return "survived refutation"
    if survived is False:
        return "refuted"
    return "refutation inconclusive"


def _render_item_html(kind: str, it: dict) -> str:
    """One item card (heading + confidence + refutation verdict + decision)."""
    it = it if isinstance(it, dict) else {}
    heading = it.get("title") or it.get("question") or "(untitled)"
    conf = _conf_str(it.get("confidence"))
    verdict = _refutation_verdict(it)
    area = it.get("area")
    rows = [
        '<div style="background:#f8fafc;border:1px solid #e5e7eb;border-left:3px '
        'solid #6366f1;border-radius:8px;padding:.7rem .85rem;margin-bottom:.6rem">',
        f'<div style="font-weight:650;font-size:.95rem;color:#111827;'
        f'margin-bottom:.35rem">{_esc(heading)}</div>',
        '<div style="font-size:.8rem;color:#475569;margin-bottom:.3rem">'
        f'<strong>confidence</strong> {_esc(conf)} '
        f'&nbsp;·&nbsp; <strong>refutation:</strong> {_esc(verdict)}'
        + (f' &nbsp;·&nbsp; <strong>area:</strong> {_esc(area)}' if area else "")
        + "</div>",
    ]
    rec = it.get("recommendation")
    if rec:
        rows.append(
            '<div style="font-size:.85rem;color:#334155;margin:.25rem 0">'
            f"{_esc(rec)}</div>"
        )
    decision = it.get("decision_for_human")
    if decision:
        rows.append(
            '<div style="font-size:.66rem;text-transform:uppercase;'
            'letter-spacing:.08em;color:#6b7280;margin-top:.4rem">decision for '
            'human</div>'
            '<div style="font-size:.84rem;color:#1e293b;background:#fff;'
            'border:1px solid #e5e7eb;border-radius:6px;padding:.4rem .6rem;'
            f'margin-top:.15rem">{_esc(decision)}</div>'
        )
    item_id = it.get("id")
    if item_id is not None:
        rows.append(
            '<div style="font-family:monospace;font-size:.7rem;color:#9ca3af;'
            f'margin-top:.4rem">#{_esc(item_id)}</div>'
        )
    rows.append("</div>")
    return "".join(rows)


def _render_section_html(label: str, kind: str, items: list) -> str:
    """One section: header + up to _MAX_PER_SECTION item cards (or an empty note).
    """
    items = items if isinstance(items, list) else []
    shown = items[:_MAX_PER_SECTION]
    extra = len(items) - len(shown)
    head = (
        '<div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;'
        'text-transform:uppercase;font-weight:700;margin:1.1rem 0 .5rem">'
        f"▼ {_esc(label)} ({len(items)})</div>"
    )
    if not shown:
        return head + (
            '<div style="font-size:.85rem;color:#9ca3af;padding:.6rem .85rem;'
            'background:#f8fafc;border:1px dashed #e5e7eb;border-radius:8px">'
            "Nothing here yet.</div>"
        )
    body = "".join(_render_item_html(kind, it) for it in shown)
    if extra > 0:
        body += (
            '<div style="font-size:.78rem;color:#6b7280;margin-top:.2rem">'
            f"+{extra} more on the dashboard</div>"
        )
    return head + body


def compose_innovation_email() -> dict:
    """Compose {subject, html, text} from the SAME digest the dashboard uses.

    Pulls routes.brain_innovation_dashboard.build_digest() — the single source of
    truth (no re-query) — and renders a skimmable operator email: a short header
    (counts + date), then three sections (Self-directed agenda / Investigations /
    Proposals), each item showing the title/question, a confidence value, the
    refutation verdict, the decision-for-human, capped at ~6 per section, plus a
    deep link to the dashboard. Best-effort: NEVER raises — on any failure it
    still returns a valid envelope (a degraded one)."""
    try:
        from routes.brain_innovation_dashboard import build_digest
        digest = build_digest() or {}
    except Exception as e:
        logger.warning("compose_innovation_email: digest build failed: %s", e)
        digest = {}

    counts = digest.get("counts") if isinstance(digest.get("counts"), dict) else {}
    n_agenda = int(counts.get("agenda") or 0)
    n_inv = int(counts.get("investigations") or 0)
    n_prop = int(counts.get("proposals") or 0)
    total = n_agenda + n_inv + n_prop

    now = datetime.now(timezone.utc)
    date_pretty = now.strftime("%A, %B %d, %Y")
    subject = f"DC Hub · Brain Innovation Digest · {now.strftime('%Y-%m-%d')}"

    sections = (
        _render_section_html("Self-directed agenda", "agenda", digest.get("agenda"))
        + _render_section_html("Investigations", "inv", digest.get("investigations"))
        + _render_section_html("Proposals", "prop", digest.get("proposals"))
    )

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,'Inter',sans-serif;max-width:640px;
margin:0 auto;padding:1.25rem;color:#1f2937;line-height:1.5;background:#ffffff">

<div style="background:linear-gradient(135deg,#1e1b4b,#6366f1);color:white;
padding:1.25rem 1.5rem;border-radius:10px;margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#c7d2fe;
 text-transform:uppercase;margin-bottom:.4rem">DC Hub · Brain · Innovation</div>
 <h1 style="margin:0;font-size:1.45rem;font-weight:700;letter-spacing:-.02em">
 What the brain is thinking</h1>
 <div style="color:#e0e7ff;font-size:.95rem;margin-top:.25rem">{_esc(date_pretty)}
 &nbsp;·&nbsp; {total} verified item{'' if total == 1 else 's'}
 ({n_agenda} agenda · {n_inv} investigations · {n_prop} proposals)</div>
</div>

<div style="font-size:.88rem;color:#475569;margin-bottom:.5rem">
 The brain's verified, adversarially-refuted analysis — the agenda it chose for
 itself, the investigations it ran, and the ranked improvements it proposed.
 Nothing here is acted on; each item is for you to read and grade. Open the
 dashboard to grade or approve.
</div>

{sections}

<div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid #e5e7eb">
 <a href="{_DASHBOARD_URL}" style="display:inline-block;background:#6366f1;
 color:#fff;text-decoration:none;font-weight:600;font-size:.88rem;
 padding:.55rem 1rem;border-radius:8px">Open the innovation dashboard →</a>
 <div style="font-size:.75rem;color:#9ca3af;margin-top:.5rem">
 Append <code>?admin_key=…</code> to grade / approve. Read-only digest ·
 brain_self_agenda · brain_investigations · brain_enhancement_proposals</div>
</div>

<div style="margin-top:1rem;color:#9ca3af;font-size:.72rem">
 Operator innovation digest (transactional) · routes/brain_innovation_email.py ·
 ships dark behind <code>BRAIN_INNOVATION_EMAIL_ENABLED</code>
</div>

</body></html>"""

    # Plain-text fallback — skimmable, mirrors the html.
    def _txt_section(label, items):
        items = items if isinstance(items, list) else []
        lines = [f"== {label} ({len(items)}) =="]
        if not items:
            lines.append("  (nothing here yet)")
        for it in items[:_MAX_PER_SECTION]:
            it = it if isinstance(it, dict) else {}
            heading = it.get("title") or it.get("question") or "(untitled)"
            lines.append(
                f"  - {heading}  [conf {_conf_str(it.get('confidence'))} · "
                f"{_refutation_verdict(it)}]"
            )
            if it.get("decision_for_human"):
                lines.append(f"      decision: {it['decision_for_human']}")
        extra = len(items) - min(len(items), _MAX_PER_SECTION)
        if extra > 0:
            lines.append(f"  (+{extra} more on the dashboard)")
        return "\n".join(lines)

    text = "\n\n".join([
        f"DC Hub · Brain Innovation Digest · {date_pretty}",
        f"{total} verified items "
        f"({n_agenda} agenda · {n_inv} investigations · {n_prop} proposals)",
        _txt_section("Self-directed agenda", digest.get("agenda")),
        _txt_section("Investigations", digest.get("investigations")),
        _txt_section("Proposals", digest.get("proposals")),
        f"Dashboard: {_DASHBOARD_URL} (append ?admin_key=)",
    ])

    return {"subject": subject, "html": html, "text": text}


def send_innovation_digest() -> dict:
    """Compose + send the daily innovation digest to the operator inbox.

    GUARD ORDER (so the cron logs are legible and nothing leaks):
      1. flag OFF  -> {sent:false, skipped:"disabled"}  (NO email composed/sent)
      2. no recipient (BRAIN_DIGEST_EMAIL / ADMIN_ALERT_EMAIL unset)
                   -> {sent:false, skipped:"no_recipient"}
      3. no provider key (Resend / SendGrid)
                   -> {sent:false, skipped:"no_provider"}
      4. else compose + send via the REUSED transactional sender
         (email_fallback.send_email_resilient — Resend primary / SendGrid
         fallback) and return {sent:true, to}.

    TRANSACTIONAL — operator ops mail. Does NOT touch marketing_send / the consent
    choke-point and does NOT require marketing_opt_in. Best-effort: a sender error
    degrades to {sent:false, error:...}; never raises."""
    if not _enabled():
        return {"sent": False, "skipped": "disabled"}

    to = _recipient()
    if not to:
        return {"sent": False, "skipped": "no_recipient"}

    if not _has_provider_key():
        return {"sent": False, "skipped": "no_provider", "to": to}

    msg = compose_innovation_email()
    try:
        from email_fallback import send_email_resilient
        ok = send_email_resilient(
            to,
            msg["subject"],
            html_content=msg["html"],
            text_content=msg.get("text"),
            from_email=os.environ.get("SENDGRID_FROM_EMAIL", "alerts@dchub.cloud"),
            from_name="DC Hub Brain",
        )
    except Exception as e:
        logger.warning("send_innovation_digest: sender raised: %s", e)
        return {"sent": False, "error": str(e)[:160], "to": to}

    if ok:
        return {"sent": True, "to": to, "subject": msg["subject"]}
    return {"sent": False, "error": "send_failed", "to": to}


# ════════════════════════════════════════════════════════════════════
#  Endpoint (admin-gated) — the cron hits this
# ════════════════════════════════════════════════════════════════════
@brain_innovation_email_bp.route(
    "/api/v1/brain/innovation/digest/email", methods=["POST"])
def innovation_digest_email():
    """Send the daily brain-innovation digest to the operator inbox (the cron
    target). Admin-gated. Returns the skip reason ({skipped:"disabled"|
    "no_recipient"|"no_provider"}) so the cron logs are legible. ?dry=1 composes +
    returns the html WITHOUT sending (preview)."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only — internal brain digest",
                       hint="send X-Admin-Key or append ?admin_key="), 403

    dry = str(request.args.get("dry", "")).lower() in ("1", "true", "yes")
    if dry:
        msg = compose_innovation_email()
        return jsonify(ok=True, dry=True, sent=False,
                       subject=msg["subject"], html=msg["html"]), 200

    result = send_innovation_digest()
    return jsonify(ok=True, **result), 200


def register_brain_innovation_email(app) -> None:
    """Idempotent registration helper for main.py. Mirrors
    register_brain_innovation_dashboard — registers the blueprint; the send path
    ships DARK behind BRAIN_INNOVATION_EMAIL_ENABLED (default OFF)."""
    try:
        app.register_blueprint(brain_innovation_email_bp)
    except Exception as e:
        logger.warning("brain_innovation_email already registered: %s", e)
