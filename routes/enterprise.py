"""Phase 272 — public /enterprise self-serve contact page.

Why this exists
---------------
The MCP funnel report shows:
  • 39,340 tool calls / 7d
  • 8,167 paywall hits / 7d
  • 1 paid conversion / 30d
  • 0 enterprise customers (in 30d, ever)

Enterprise has zero conversion because there is no enterprise contact path
on the site — the upgrade-prompt copy points everyone at /pricing ($49/mo
Pro). Enterprise buyers won't self-serve a credit card; they expect to
have a conversation. This module adds the missing surface.

What it does
------------
  GET  /enterprise                       — public HTML inquiry page
  POST /api/v1/enterprise/contact        — JSON form handler
       writes to enterprise_inquiries table; optionally relays to a Slack
       webhook (env DCHUB_SALES_WEBHOOK) and/or a sales email (env
       DCHUB_SALES_EMAIL via the existing Resend pipeline if present).

Defensive design
----------------
  • Honeypot field (hidden 'company_url') — bots fill it, humans don't.
    Submissions with a non-empty honeypot are silently accepted (return
    200) but discarded; this avoids tipping off the bot.
  • Per-IP soft rate limit: max 5 submissions / 10 min from the same IP.
  • All user input HTML-escaped before being written into the page or
    relayed; lengths capped (org/email 200, use_case 2000).
  • Email format check is intentionally loose (contains '@' and '.').
    Stricter validation belongs in a dedicated email validator, not here.
  • DB write is best-effort: if the table doesn't exist we create it
    on the fly; any DB error returns 503 with a friendly fallback email.
"""
from __future__ import annotations
import os
import re
import json
import logging
import time
from datetime import datetime, timezone
from html import escape as _h
from flask import Blueprint, Response, request, jsonify

logger = logging.getLogger(__name__)

enterprise_bp = Blueprint("enterprise", __name__)

SALES_EMAIL = os.environ.get("DCHUB_SALES_EMAIL", "enterprise@dchub.cloud")
SALES_WEBHOOK = os.environ.get("DCHUB_SALES_WEBHOOK")  # optional
CALENDLY_URL = os.environ.get("DCHUB_CALENDLY_URL", "")  # optional

_VOLUME_CHOICES = {"1k", "10k", "100k", "1M+"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RATE_BUCKET: dict = {}  # ip -> list[timestamps]
_RATE_WINDOW_S = 600
_RATE_MAX = 5


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


def _ensure_table():
    # r-inquiry-schema-fork (2026-09-05): this used to carry its OWN
    # `CREATE TABLE IF NOT EXISTS enterprise_inquiries` with org_name/
    # expected_volume NOT NULL and no `status` column — incompatible with
    # routes/enterprise_inquiry.py's definition of the same table. IF NOT
    # EXISTS made the loser a silent no-op, so one of the two lead-capture
    # endpoints has been failing to store since 2026-06-30 and which one
    # depended on June traffic order. Both now build the same union table.
    try:
        from util.enterprise_inquiries_schema import ensure_enterprise_inquiries
        with _conn() as c, c.cursor() as cur:
            ensure_enterprise_inquiries(cur)
            c.commit()
    except Exception as e:
        # surfaced by /api/v1/enterprise/contact's catch-all
        logger.warning(f"[enterprise] schema init failed: {e}")


# ── r-restore-helpers (2026-09-05) ────────────────────────────────────────
# RESTORED VERBATIM from 6ca3aaef6. #3894 replaced _ensure_table by splicing
# out everything between `def _ensure_table():` and the route decorator that
# follows it — and these two helpers lived in that gap. Nothing referenced
# them at import time, so the module imported clean, every test passed, and
# the endpoint 500'd with `name '_rate_limited' is not defined` the first
# time a VALID submission got past validation. Measured live at the origin:
#     valid POST -> 500 {"error":"name '_rate_limited' is not defined"}
# That is worse than the bug #3894 fixed: a 503 naming the sales address
# became an internal error naming nothing.
def _rate_limited(ip: str) -> bool:
    now = time.time()
    window = _RATE_BUCKET.setdefault(ip, [])
    # drop old entries
    window[:] = [t for t in window if now - t < _RATE_WINDOW_S]
    if len(window) >= _RATE_MAX:
        return True
    window.append(now)
    return False


def _relay_to_webhook(payload: dict) -> str:
    """Optional Slack-compatible webhook relay. Returns a short status."""
    if not SALES_WEBHOOK:
        return "no_webhook_configured"
    try:
        # r-restore-helpers (2026-09-05): was urllib.request.urlopen. Restoring
        # it verbatim reintroduced it as a NEW line, and regression_lint's
        # urllib-request-on-railway rule blocked the merge — correctly. Python's
        # default urllib User-Agent is what Cloudflare answers with error 1010
        # (see reference_dchub_urllib_ua_block_0810), so an outbound call from
        # here can be refused before it reaches the webhook. `requests` is
        # already a dependency and is what every other outbound call uses.
        # Behaviour preserved exactly: same JSON body, same 5s timeout, same
        # two return strings.
        import requests
        body = json.dumps({
            "text": f"New DC Hub enterprise inquiry from *{payload['org_name']}* <{payload['email']}>",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "New enterprise inquiry"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Org:* {payload['org_name']}"},
                    {"type": "mrkdwn", "text": f"*Email:* {payload['email']}"},
                    {"type": "mrkdwn", "text": f"*Volume:* {payload['expected_volume']}/mo"},
                    {"type": "mrkdwn", "text": f"*IP:* {payload.get('source_ip','?')}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                  "text": f"*Use case:*\n{payload['use_case']}"}},
            ],
        }).encode("utf-8")
        requests.post(SALES_WEBHOOK, data=body,
                      headers={"Content-Type": "application/json"},
                      timeout=5)
        return "webhook_ok"
    except Exception as e:
        return f"webhook_failed: {str(e)[:80]}"


# ── r-contact-notify (2026-09-05) ─────────────────────────────────────────
# The public lead form notified NOBODY. _relay_to_webhook is the only
# notification this endpoint had, and it opens `if not SALES_WEBHOOK: return
# "no_webhook_configured"` — DCHUB_SALES_WEBHOOK has never been set, so every
# submission returned that string and the lead sat in a table nobody watches.
#
# ★ THE SIBLING ENDPOINT ALREADY HAD THIS. routes/enterprise_inquiry.py's
# _notify_admin fires a Resend email on every inquiry, using RESEND_API_KEY and
# ADMIN_INBOX_EMAIL — both already configured in production. So the two
# enterprise capture paths diverged a SECOND way: one emails a human, the
# public one did nothing. (The first divergence was the schema fork, #3894.)
# Closing it needs no new credential, which is why this is code and not a
# Railway variable.
#
# The webhook stays exactly as it was: an optional Slack upgrade for whoever
# sets DCHUB_SALES_WEBHOOK. Email is the path that works today.
def _sales_email_html(payload: dict) -> str:
    """The admin-notification body. Pure, so a test can inspect it without a
    network call and without importing main.

    ★ EVERY FIELD IS ESCAPED. This is attacker-controlled free text from a
    PUBLIC form being interpolated into HTML that a human opens in a mail
    client. The sibling routes/enterprise_inquiry.py::_notify_admin
    interpolates the same class of input RAW — that is a live injection into
    the admin inbox and is not fixed here, but new code must not copy it.
    """
    org = _h(payload.get("org_name") or "?")
    return (
        f"<h2>New enterprise inquiry — {org}</h2>"
        f"<p><b>Org:</b> {org}<br>"
        f"<b>Email:</b> {_h(payload.get('email') or '?')}<br>"
        f"<b>Expected volume:</b> {_h(payload.get('expected_volume') or '?')}/mo<br>"
        f"<b>Source IP:</b> {_h(payload.get('source_ip') or '?')}</p>"
        f"<p><b>Use case:</b><br>{_h(payload.get('use_case') or '—')}</p>"
        f"<p style='color:#666;font-size:12px'>Reply to {_h(payload.get('email') or '')} "
        f"to reach them. Submitted via the /enterprise form.</p>"
    )


def _notify_sales(payload: dict) -> bool:
    """Email the inquiry to the admin inbox. True if the send was accepted.

    ★ ROUTED THROUGH main._resend_email, the SANCTIONED transactional sender,
    not a hand-rolled POST to api.resend.com. tests/test_marketing_chokepoint.py
    ratchets against new direct Resend callers — the backend already has ~45
    hand-rolled senders and no way to prove CAN-SPAM compliance across them, so
    every new one has to justify itself. The first version of this function was
    a direct POST and that guard caught it. This is an ops alert about an
    inbound lead, so it is transactional rather than marketing, but reusing the
    governed sender is still strictly better than adding a 46th bypass and an
    allow-list entry.

    Lazy import, matching routes/auth_routes.py and
    routes/agent_iteration_suite.py — main imports this blueprint, so a
    module-level import would be circular.
    """
    to_addr = os.environ.get("ADMIN_INBOX_EMAIL") or SALES_EMAIL
    try:
        from main import _resend_email
        return bool(_resend_email(
            to_addr,
            f"New enterprise inquiry — {payload.get('org_name') or '?'}",
            _sales_email_html(payload),
        ))
    except Exception as e:
        logger.warning(f"[enterprise] sales notify failed: {e}")
        return False


@enterprise_bp.route("/api/v1/enterprise/contact", methods=["POST"])
def api_enterprise_contact():
    """Accept an enterprise inquiry. JSON body or form-encoded.

    Required fields: org_name, email, use_case, expected_volume.
    Optional: company_url (honeypot — must be empty).
    """
    data = request.get_json(silent=True) or request.form.to_dict() or {}

    # Honeypot — silently accept but discard if filled
    if (data.get("company_url") or "").strip():
        return jsonify(ok=True), 200  # don't tip off the bot

    org = (data.get("org_name") or "").strip()[:200]
    email = (data.get("email") or "").strip()[:200]
    use_case = (data.get("use_case") or "").strip()[:2000]
    volume = (data.get("expected_volume") or "").strip()

    errors = {}
    if not org: errors["org_name"] = "required"
    if not _EMAIL_RE.match(email): errors["email"] = "must be a valid email"
    if not use_case: errors["use_case"] = "tell us what you want to build"
    if volume not in _VOLUME_CHOICES:
        errors["expected_volume"] = f"must be one of: {sorted(_VOLUME_CHOICES)}"
    if errors:
        return jsonify(ok=False, errors=errors), 400

    src_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _rate_limited(src_ip):
        return jsonify(ok=False, error="rate_limited",
                       message=f"Too many submissions. Email {SALES_EMAIL} directly."), 429

    payload = {
        "org_name": org, "email": email, "use_case": use_case,
        "expected_volume": volume, "source_ip": src_ip,
        "user_agent": (request.headers.get("User-Agent") or "")[:400],
    }

    relay_status = _relay_to_webhook(payload)

    try:
        _ensure_table()
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO enterprise_inquiries
                  (org_name, email, use_case, expected_volume, source_ip, user_agent, relay_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (org, email, use_case, volume, src_ip,
                  payload["user_agent"], relay_status))
            c.commit()
    except Exception as e:
        # Even if DB fails, the webhook relay may have succeeded — surface that
        return jsonify(
            ok=False, error="storage_failed",
            relay_status=relay_status,
            message=f"We couldn't save your request. Please email {SALES_EMAIL} directly. ({str(e)[:120]})",
        ), 503

    # r-contact-notify: the lead is STORED by here. Tell a human about it.
    notified = _notify_sales(payload)

    return jsonify(
        ok=True,
        message=(
            f"Thanks. We'll reply within 24 hours at {email}. "
            f"If urgent, email {SALES_EMAIL}."
        ),
        relay_status=relay_status,
        # Honest per-path result, matching what submit_inquiry reports: a lead
        # that stored but reached no inbox is a different state from one that
        # did, and "200 ok" alone hid that for the whole life of this endpoint.
        stored=True,
        notified=notified,
    ), 200


_ENTERPRISE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub for Enterprise · Talk to sales</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Custom enterprise tier for DC Hub: unlimited MCP calls, SLA-backed uptime, dedicated support, on-prem options. Built for teams scoring data center markets at scale.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/enterprise">
<meta property="og:title" content="DC Hub for Enterprise">
<meta property="og:description" content="Unlimited MCP calls. SLA-backed. Dedicated support. Talk to us.">
<meta property="og:url" content="https://dchub.cloud/enterprise">
<meta name="twitter:card" content="summary_large_image">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<style>
:root{--bg:#0a0a0f;--bg2:#0f1119;--card:#131319;--bd:rgba(255,255,255,.08);--tx:#fafafa;--tx2:#a1a1aa;--tx3:#71717a;--green:#10b981;--red:#ef4444;--acc:#6366f1;--acc-light:#818cf8;--acc-vivid:#a855f7;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}
*{box-sizing:border-box}
body{font-family:'Instrument Sans',-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem;}
.eyebrow{font-family:'JetBrains Mono',monospace;font-size:0.74rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.14em;margin-bottom:0.6rem;}
h1{font-size:clamp(2.4rem,5vw,3.4rem);margin:0 0 0.7rem;font-weight:800;letter-spacing:-0.025em;line-height:1.05;}
h1 .grad{background:var(--gradient);-webkit-background-clip:text;background-clip:text;color:transparent;}
.lede{color:var(--tx2);font-size:1.1rem;max-width:680px;margin:0 0 2.5rem;}
.cols{display:grid;grid-template-columns:1.05fr 0.95fr;gap:2.2rem;align-items:start;}
@media (max-width:820px){.cols{grid-template-columns:1fr;}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.8rem;}
.form-card h2{margin:0 0 1.1rem;font-size:1.25rem;font-weight:700;letter-spacing:-0.01em;}
label{display:block;font-size:0.78rem;color:var(--tx2);margin:0.95rem 0 0.35rem;text-transform:uppercase;letter-spacing:0.08em;font-weight:600;}
input[type=text],input[type=email],textarea,select{width:100%;background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:0.7rem 0.85rem;color:var(--tx);font-family:inherit;font-size:0.95rem;line-height:1.4;}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--acc);}
textarea{min-height:110px;resize:vertical;}
.radio-row{display:grid;grid-template-columns:repeat(2,1fr);gap:0.5rem;margin-top:0.45rem;}
.radio-row label{display:flex;align-items:center;justify-content:center;background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:0.65rem;font-size:0.9rem;text-transform:none;letter-spacing:0;cursor:pointer;font-weight:500;color:var(--tx);margin:0;}
.radio-row label.sel{background:rgba(99,102,241,0.13);border-color:var(--acc);color:var(--tx);}
.radio-row input{position:absolute;opacity:0;pointer-events:none;}
.hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0;}
.btn{display:inline-flex;align-items:center;justify-content:center;background:var(--gradient);color:white;border:0;border-radius:8px;padding:0.85rem 1.5rem;font-weight:700;font-size:0.95rem;cursor:pointer;font-family:inherit;margin-top:1.4rem;width:100%;letter-spacing:0.01em;}
.btn:hover{filter:brightness(1.08);}
.btn:disabled{opacity:0.6;cursor:not-allowed;}
.err{color:var(--red);font-size:0.82rem;margin-top:0.3rem;}
.success{background:rgba(16,185,129,0.10);border:1px solid var(--green);color:var(--green);padding:1rem 1.2rem;border-radius:8px;margin-top:1rem;font-size:0.95rem;}
.bullets{list-style:none;padding:0;margin:0;}
.bullets li{padding:0.85rem 0;border-bottom:1px solid var(--bd);display:flex;align-items:flex-start;gap:0.7rem;}
.bullets li:last-child{border-bottom:none;}
.bullets li::before{content:"";display:inline-block;width:7px;height:7px;background:var(--green);border-radius:50%;margin-top:0.55rem;flex:0 0 7px;}
.bullets li strong{color:var(--tx);font-weight:700;}
.bullets li span{color:var(--tx2);font-size:0.9rem;}
.divider{height:1px;background:var(--bd);margin:1.4rem 0;}
.alt{color:var(--tx2);font-size:0.88rem;margin:0;}
.alt code{background:var(--bg2);padding:2px 6px;border-radius:4px;color:var(--tx);font-family:'JetBrains Mono',monospace;font-size:0.84rem;}
.alt a{color:var(--acc-light);text-decoration:none;border-bottom:1px dotted rgba(129,140,248,0.5);}
.cal{display:inline-flex;align-items:center;gap:0.5rem;background:transparent;border:1px solid var(--bd);color:var(--tx);padding:0.65rem 1rem;border-radius:8px;text-decoration:none;font-weight:600;font-size:0.9rem;margin-top:1rem;}
.cal:hover{border-color:var(--acc);}
.foot{color:var(--tx3);font-size:0.8rem;margin-top:3rem;text-align:center;}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">DC Hub · Enterprise</div>
  <h1>Power your AI infrastructure team with <span class="grad">our entire data graph</span>.</h1>
  <p class="lede">Unlimited MCP calls. SLA-backed uptime. Dedicated support. Custom data feeds. On-prem options. Tell us what you want to build and we'll have you live in days, not weeks.</p>

  <div class="cols">
    <div class="card form-card">
      <h2>Tell us about your team</h2>
      <form id="ef" novalidate>
        <input type="text" name="company_url" class="hp" tabindex="-1" autocomplete="off" aria-hidden="true">
        <label for="org_name">Org name</label>
        <input type="text" id="org_name" name="org_name" maxlength="200" required>
        <div class="err" data-err-for="org_name"></div>

        <label for="email">Work email</label>
        <input type="email" id="email" name="email" maxlength="200" required>
        <div class="err" data-err-for="email"></div>

        <label for="use_case">What do you want to build?</label>
        <textarea id="use_case" name="use_case" maxlength="2000" required placeholder="e.g. We're scoring 200 candidate build-sites monthly for an AI hyperscaler client; need DCPI + grid_intelligence + fiber routes."></textarea>
        <div class="err" data-err-for="use_case"></div>

        <label>Expected MCP calls per month</label>
        <div class="radio-row" role="radiogroup" aria-label="Expected monthly MCP call volume">
          <label><input type="radio" name="expected_volume" value="1k">1k</label>
          <label><input type="radio" name="expected_volume" value="10k">10k</label>
          <label><input type="radio" name="expected_volume" value="100k">100k</label>
          <label><input type="radio" name="expected_volume" value="1M+">1M+</label>
        </div>
        <div class="err" data-err-for="expected_volume"></div>

        <button type="submit" class="btn" id="submit-btn">Request enterprise access</button>
        <div class="success" id="ok" style="display:none"></div>
      </form>

      <div class="divider"></div>
      <p class="alt">Prefer email? <a href="mailto:{{SALES_EMAIL}}">{{SALES_EMAIL}}</a></p>
      {{CALENDLY_BLOCK}}
    </div>

    <div class="card">
      <h2>What enterprise unlocks</h2>
      <ul class="bullets">
        <li><span><strong>Unlimited MCP calls</strong> across every paid tool — analyze_site, get_grid_intelligence, get_fiber_intel, compare_sites, the whole surface.</span></li>
        <li><span><strong>99.9% SLA</strong> with credits, dedicated status page, incident-response on the same hour.</span></li>
        <li><span><strong>Dedicated Slack / shared channel</strong> with our engineering team. No tickets, no queues.</span></li>
        <li><span><strong>Custom data feeds</strong> — your facility list, your watchlist, your DCPI markets get refreshed on your cadence.</span></li>
        <li><span><strong>On-prem / VPC option</strong> — for buyers who can't send addresses to a public API. We ship a container.</span></li>
        <li><span><strong>White-label DCPI</strong> — embed the Excess Power Score on your internal dashboards under your brand.</span></li>
      </ul>
    </div>
  </div>

  <p class="foot">DC Hub · the canonical data-center intelligence layer for AI agents.<br>Already on free tier? Your existing API key works here once you're upgraded — no migration.</p>
</div>

<script>
(function(){
  // Radio button "selected" styling
  document.querySelectorAll('.radio-row input[type=radio]').forEach(r => {
    r.addEventListener('change', () => {
      document.querySelectorAll('.radio-row label').forEach(l => l.classList.remove('sel'));
      r.closest('label').classList.add('sel');
    });
  });
  const form = document.getElementById('ef');
  const btn = document.getElementById('submit-btn');
  const ok = document.getElementById('ok');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    document.querySelectorAll('.err').forEach(el => el.textContent = '');
    btn.disabled = true; btn.textContent = 'Sending…';
    const payload = Object.fromEntries(new FormData(form));
    try {
      const res = await fetch('/api/v1/enterprise/contact', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        ok.textContent = data.message || 'Thanks. We will be in touch within 24 hours.';
        ok.style.display = 'block';
        form.querySelectorAll('input,textarea,button').forEach(el => el.disabled = true);
      } else if (data.errors) {
        Object.entries(data.errors).forEach(([k,v]) => {
          const el = document.querySelector(`[data-err-for="${k}"]`);
          if (el) el.textContent = v;
        });
        btn.disabled = false; btn.textContent = 'Request enterprise access';
      } else {
        ok.textContent = data.message || 'Something went wrong. Please email {{SALES_EMAIL}}.';
        ok.style.display = 'block';
        ok.style.background = 'rgba(239,68,68,0.10)';
        ok.style.borderColor = 'var(--red)';
        ok.style.color = 'var(--red)';
        btn.disabled = false; btn.textContent = 'Request enterprise access';
      }
    } catch (err) {
      ok.textContent = 'Network error. Please email {{SALES_EMAIL}}.';
      ok.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Request enterprise access';
    }
  });
})();
</script>
</body>
</html>"""


@enterprise_bp.route("/enterprise", methods=["GET"])
def enterprise_page():
    cal_block = ""
    if CALENDLY_URL:
        # CALENDLY_URL must be a valid URL — escape it before emitting
        cal_block = (
            f'<a class="cal" href="{_h(CALENDLY_URL)}" target="_blank" rel="noopener">'
            f'📅 Book a 30-min call</a>'
        )
    html = (_ENTERPRISE_PAGE_TEMPLATE
            .replace("{{SALES_EMAIL}}", _h(SALES_EMAIL))
            .replace("{{CALENDLY_BLOCK}}", cal_block))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return resp
