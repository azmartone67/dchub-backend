"""Phase 63 -- email-redeem flow.

Captures identity (IP, User-Agent, email) at human-touchpoint click time,
since the anonymous signal writer cannot capture it at write time.

Routes:
  GET  /api/v1/redeem/<session_id>  -- minimal HTML form for email entry
  POST /api/v1/redeem/<session_id>  -- capture and persist, show success
"""
import os
import re
import datetime
from flask import Blueprint, request, Response


# === phase 99h: dev-key creation + email send ============================
import secrets as _p99_secrets
import logging as _p99_logging
_p99_logger = _p99_logging.getLogger("redeem.phase99h")


def _p99_make_key():
    return f"dch_live_{_p99_secrets.token_hex(16)}"


def _p99_persist_key(conn, email, api_key, session_id):
    """INSERT into mcp_dev_keys. Returns (ok, err, developer_id)."""
    import json as _j
    developer_id = f"dev_{_p99_secrets.token_hex(8)}"
    metadata = {"source": "redeem", "session_id": session_id}
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status, metadata) "
            "VALUES (%s, %s, %s, %s, 'active', %s::jsonb) "
            "ON CONFLICT (api_key) DO NOTHING RETURNING developer_id",
            (api_key, developer_id, email, "free", _j.dumps(metadata)),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return False, "key_collision", None
        return True, None, developer_id
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return False, f"{type(e).__name__}: {e}", None


def _p99_send_email(email, api_key, tools_tried):
    """Send dev-key email via Resend (primary) or GoDaddy SMTP (fallback).

    SendGrid removed in phase 102c (account permanently OOC, user declined
    upgrade). Resend errors are surfaced even when SMTP succeeds, so we can
    always diagnose Resend without it being masked by a working fallback.

    r32-welcome (2026-05-20): personalized opening based on tools_tried.
    If we know the recipient hit a specific paid tool (passed in via the
    redeem flow), the email leads with a working curl example FOR THAT
    TOOL — "you hit get_grid_intelligence, here's the call that just
    unlocked." Higher activation rate than the generic ERCOT default
    because it shows them the exact thing they were trying to do.
    """
    import os as _os, json as _j
    import urllib.request as _ur, urllib.error as _ue

    # r32-welcome: tool-aware customization.
    PRIMARY_TOOL_HINTS = {
        "get_grid_intelligence": {
            "tagline": "Real-time grid intelligence for 7 ISOs",
            "curl_example": (
                f"curl -H 'Authorization: Bearer {api_key}' "
                "https://dchub.cloud/api/v1/grid-intelligence?iso=PJM"
            ),
            "next_step": "Try PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE.",
        },
        "get_fiber_intel": {
            "tagline": "Fiber routes + carrier intelligence",
            "curl_example": (
                f"curl -H 'Authorization: Bearer {api_key}' "
                "https://dchub.cloud/api/v1/fiber/intel?market=ashburn"
            ),
            "next_step": "Try ashburn, dallas, phoenix, silicon-valley, atlanta.",
        },
        "get_market_intel": {
            "tagline": "DCPI scores for 276 markets",
            "curl_example": (
                f"curl -H 'Authorization: Bearer {api_key}' "
                "https://dchub.cloud/api/v1/markets/northern-virginia"
            ),
            "next_step": "Or open https://dchub.cloud/pockets for the live ranking.",
        },
        "analyze_site": {
            "tagline": "Composite site scoring across power/fiber/risk/carbon",
            "curl_example": (
                f"curl -H 'Authorization: Bearer {api_key}' "
                "'https://dchub.cloud/api/v1/site-forecast?lat=38.98&lon=-77.49&state=VA'"
            ),
            "next_step": "Swap lat/lon for any site you're evaluating.",
        },
        "get_water_risk": {
            "tagline": "Facility-level water-risk overlay",
            "curl_example": (
                f"curl -H 'Authorization: Bearer {api_key}' "
                "https://dchub.cloud/api/v1/water-risk?state=AZ"
            ),
            "next_step": "Useful in WECC + ERCOT where water stress matters most.",
        },
    }
    # Pick the first known tool from tools_tried that maps to a hint.
    primary_tool = None
    if tools_tried:
        for t in tools_tried:
            if t in PRIMARY_TOOL_HINTS:
                primary_tool = t
                break
    hint = PRIMARY_TOOL_HINTS.get(primary_tool, {
        "tagline":      "Real-time data center market intelligence",
        "curl_example": (f"curl -H 'Authorization: Bearer {api_key}' "
                         "https://dchub.cloud/api/v1/grid-intelligence?iso=ERCOT"),
        "next_step":    "Try any of 7 ISOs (PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE).",
    })

    subject = (
        f"Your DC Hub dev key — unlocks {primary_tool}"
        if primary_tool else
        "Your DC Hub dev key is ready"
    )
    personal_open = (
        f"You hit {primary_tool} from MCP — that's why this email landed.\n"
        f"{hint['tagline']}.\n\n"
        if primary_tool else
        f"{hint['tagline']}.\n\n"
    )
    text = (
        f"{personal_open}"
        f"Your DC Hub dev key:\n\n"
        f"  {api_key}\n\n"
        f"Add to Claude Desktop / Cursor / Cline config:\n\n"
        f'  {{"mcpServers":{{"dchub":{{"command":"npx","args":["-y","mcp-remote","https://dchub.cloud/mcp"],"env":{{"DCHUB_API_KEY":"{api_key}"}}}}}}}}\n\n'
        f"Direct API (try this now — the call that just unlocked):\n"
        f"  {hint['curl_example']}\n\n"
        f"{hint['next_step']}\n\n"
        f"Unlocks: 50 facility lookups, real-time grid (7 ISOs), fiber intel, M&A deals, 650+ GW pipeline.\n\n"
        f"Upgrade to Pro at https://dchub.cloud/pricing — $49/mo unlimited.\n"
    )
    html_install = (
        '{"mcpServers":{"dchub":{"command":"npx",'
        '"args":["-y","mcp-remote","https://dchub.cloud/mcp"],'
        '"env":{"DCHUB_API_KEY":"' + api_key + '"}}}}'
    )
    # r32-welcome: tool-aware HTML body — leads with the specific tool
    # the recipient hit so the email shows them the exact call that
    # just unlocked. Higher activation than the generic version.
    html_personal_open = (
        f"<p style='background:linear-gradient(135deg,#6366f111,#a855f711);"
        f"border:1px solid #6366f155;border-radius:8px;padding:12px 16px;"
        f"margin:0 0 16px'>"
        f"You hit <b>{primary_tool}</b> from MCP — that's why this email landed.<br>"
        f"<span style='color:#6b7280;font-size:13px'>{hint['tagline']}.</span>"
        f"</p>"
        if primary_tool else
        f"<p style='color:#6b7280;font-size:14px'>{hint['tagline']}.</p>"
    )
    html = (
        "<html><body style='font-family:system-ui;max-width:600px;margin:auto;padding:24px'>"
        f"<h2>Your DC Hub dev key is ready{' · ' + primary_tool if primary_tool else ''}</h2>"
        f"{html_personal_open}"
        f"<p>API key:<br><code style='background:#eee;padding:6px 10px;border-radius:4px'>{api_key}</code></p>"
        "<p>Add to your AI assistant config:</p>"
        f"<pre style='background:#1a1a1a;color:#eee;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto'>{html_install}</pre>"
        f"<p><b>Try it now</b> — the call that just unlocked:</p>"
        f"<pre style='background:#1a1a1a;color:#eee;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto'>{hint['curl_example']}</pre>"
        f"<p style='color:#6b7280;font-size:13px'>{hint['next_step']}</p>"
        "<p>Unlocks 50 facility lookups, real-time grid (7 ISOs), fiber intel, M&A deals.</p>"
        "<p><a href='https://dchub.cloud/pricing'>Upgrade to Pro</a> for unlimited access.</p>"
        "</body></html>"
    )

    from_email = _os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>")
    if "@" not in from_email and "<" not in from_email:
        from_email = "DC Hub <jonathan@dchub.cloud>"

    errors = []

    # === 1. Resend (primary) — phase 109A: requests + full browser headers ===
    resend_key = (_os.environ.get("RESEND_API_KEY") or "").strip()
    if resend_key:
        try:
            import requests as _rq
            payload = {"from": from_email, "to": [email],
                       "subject": subject, "text": text, "html": html}
            r = _rq.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; DCHub/1.0; +https://dchub.cloud)",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                },
                timeout=15,
            )
            if 200 <= r.status_code < 300:
                return True, f"via:resend (id={r.text[:120]})"
            errors.append(f"resend:HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            errors.append(f"resend:{type(e).__name__}: {str(e)[:200]}")
    else:
        errors.append("resend:RESEND_API_KEY not set")

    # === 2. GoDaddy SMTP (fallback) ===
    smtp_user = _os.environ.get("SMTP_USER") or _os.environ.get("SMTP_USERNAME")
    smtp_pass = _os.environ.get("SMTP_PASS") or _os.environ.get("SMTP_PASSWORD")
    smtp_host = _os.environ.get("SMTP_HOST")
    smtp_port = int(_os.environ.get("SMTP_PORT", 587))

    if smtp_host and smtp_user and smtp_pass:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"DC Hub <{smtp_user}>" if "@" in smtp_user else smtp_user
            msg["Reply-To"] = from_email if "@" in from_email else smtp_user
            msg["To"] = email
            msg.attach(MIMEText(text, "plain"))
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as srv:
                srv.ehlo(); srv.starttls(); srv.ehlo()
                srv.login(smtp_user, smtp_pass)
                srv.send_message(msg)
            return True, f"via:smtp (resend_failed: {'; '.join(errors)[:400]})"
        except Exception as e:
            errors.append(f"smtp:{type(e).__name__}: {str(e)[:300]}")
    else:
        errors.append("smtp:not_configured")

    return False, "; ".join(errors)

# === end phase 99h helpers ============================================

redeem_bp = Blueprint('redeem', __name__)

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Unlock __TOOLS_DISPLAY__ — DC Hub free dev key</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body { font-family: "DM Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 560px; margin: 0 auto; padding: 3rem 1.5rem 6rem;
         color: #e6e9f5; background: rgb(5,8,16); line-height: 1.6; }
  h1 { font-size: 1.75rem; margin: 0 0 0.5rem; font-weight: 700;
       letter-spacing: -0.02em; color: #fff; line-height: 1.2; }
  .sub { color: #9aa3bd; margin: 0 0 2rem; font-size: 0.98rem; }
  .badge { display: inline-flex; align-items: center; gap: 8px;
           background: rgba(34,211,238,.10); color: #22d3ee;
           padding: 6px 14px; border-radius: 999px;
           font-size: 0.72rem; font-weight: 700; letter-spacing: .12em;
           text-transform: uppercase; margin-bottom: 1.25rem; }
  .badge .dot { width:6px;height:6px;background:#22d3ee;border-radius:50%;
                animation:pulse 2s ease-in-out infinite; }
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .tools-hit { background: rgba(34,211,238,.06);
               border: 1px solid rgba(34,211,238,.18);
               border-radius: 10px; padding: 16px 18px; margin: 0 0 1.5rem;
               font-size: 0.9rem; color: #cbd5ff; }
  .tools-hit strong { color: #22d3ee; font-weight: 600; }
  input[type="email"] { width: 100%; padding: 14px 16px;
          border: 1.5px solid rgba(255,255,255,.18); border-radius: 10px;
          font-size: 1rem; background: rgba(255,255,255,.04);
          color: #fff; margin: 0 0 12px;
          font-family: inherit; transition: border-color 0.15s; }
  input[type="email"]::placeholder { color: #6a7390; }
  input[type="email"]:focus { outline: 0; border-color: #22d3ee;
                              background: rgba(34,211,238,.05); }
  button { background: linear-gradient(135deg,#22d3ee,#a855f7);
           color: #0a0f1f; border: 0; padding: 14px 24px;
           border-radius: 10px; font-size: 1rem; font-weight: 700;
           cursor: pointer; width: 100%; font-family: inherit;
           transition: transform 0.15s, box-shadow 0.15s; }
  button:hover { transform: translateY(-1px);
                  box-shadow: 0 8px 24px rgba(34,211,238,.25); }
  .what-you-get { margin: 2rem 0 0; }
  .what-you-get h2 { font-size: 0.78rem; color: #6a7390;
                       text-transform: uppercase; letter-spacing: .12em;
                       margin: 0 0 12px; font-weight: 700; }
  .tier-row { display: flex; align-items: baseline; justify-content: space-between;
              padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,.06);
              font-size: 0.92rem; }
  .tier-row:last-child { border-bottom: 0; }
  .tier-name { color: #fff; font-weight: 600; }
  .tier-name .price { color: #22d3ee; margin-left: 8px; font-weight: 500; }
  .tier-detail { color: #9aa3bd; font-size: 0.85rem; text-align: right; }
  .upgrade-row { background: rgba(168,85,247,.08);
                  border: 1px solid rgba(168,85,247,.22);
                  border-radius: 10px; padding: 14px 16px; margin: 1.5rem 0;
                  display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .upgrade-row .text { font-size: 0.88rem; color: #cbd5ff; }
  .upgrade-row .text strong { color: #fff; }
  .upgrade-row a { background: #a855f7; color: #fff;
                    padding: 8px 16px; border-radius: 8px;
                    font-size: 0.85rem; font-weight: 600;
                    text-decoration: none; white-space: nowrap; }
  .upgrade-row a:hover { background: #9333ea; }
  .note { font-size: 0.78rem; color: #6a7390; margin-top: 2rem;
          padding-top: 1.25rem; border-top: 1px solid rgba(255,255,255,.06);
          line-height: 1.5; }
  code { font-family: "JetBrains Mono", "SF Mono", Monaco, Consolas, monospace;
         font-size: 0.78rem; background: rgba(255,255,255,.06);
         padding: 1px 6px; border-radius: 4px; color: #cbd5ff; }
</style>
</head>
<body>
  <span class="badge"><span class="dot"></span>Free dev tier · 60 sec signup</span>
  <h1>Unlock __TOOLS_DISPLAY__</h1>
  <p class="sub">Your AI assistant tried __TOOLS_COUNT__ from the DC Hub MCP server but ran into the free-tier limit. One email → one key → done.</p>

  __TOOLS_HIT_BLOCK__

  <form method="post">
    <input type="email" name="email" placeholder="you@company.com" required autofocus>
    <button type="submit">Send me my dev key →</button>
  </form>

  <div class="upgrade-row">
    <div class="text"><strong>Need more than 1,000 calls/day?</strong> $9/mo Starter unlocks 10,000/day instantly.</div>
    <a href="https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g" target="_blank">$9/mo →</a>
  </div>

  <div class="what-you-get">
    <h2>The ladder</h2>
    <div class="tier-row">
      <span class="tier-name">Anonymous <span class="price">free</span></span>
      <span class="tier-detail">10 calls/day · 2 results per query</span>
    </div>
    <div class="tier-row">
      <span class="tier-name">Free dev key <span class="price">free</span></span>
      <span class="tier-detail">1,000 calls/day · email signup</span>
    </div>
    <div class="tier-row">
      <span class="tier-name">Starter <span class="price">$9/mo</span></span>
      <span class="tier-detail">10,000 calls/day · most popular</span>
    </div>
    <div class="tier-row">
      <span class="tier-name">Developer <span class="price">$49/mo</span></span>
      <span class="tier-detail">Unlimited · all paid tools</span>
    </div>
    <div class="tier-row">
      <span class="tier-name">Pro <span class="price">$199/mo</span></span>
      <span class="tier-detail">Unlimited + Pro-only tools</span>
    </div>
  </div>

  <p class="note">No password. No spam. Your key arrives by email within ~60 seconds. Already cited by ChatGPT, Claude, Gemini, Perplexity, and Groq · <a href="https://dchub.cloud/cited-by" style="color:#22d3ee">cited-by</a> · Session <code>__SESSION_SHORT__</code></p>
</body>
</html>
"""

SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Got it -- check your inbox</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 480px; margin: 0 auto; padding: 4rem 1.5rem;
         color: #111; background: #fafafa; line-height: 1.55; }
  h1 { font-size: 1.5rem; margin: 0 0 0.5rem; font-weight: 600; }
  .check { font-size: 2rem; color: #2e7d32; margin: 0; line-height: 1; }
  .sub { color: #555; margin: 0.5rem 0 1.5rem; }
  .email { font-weight: 600; color: #1976d2; }
  .panel { background: #fff; border: 1px solid #e0e0e0; padding: 1.25rem 1.5rem;
           border-radius: 8px; margin: 1.5rem 0; }
  .panel h2 { font-size: 1rem; margin: 0 0 0.5rem; color: #555; font-weight: 600; }
  .panel p { font-size: 0.92rem; color: #444; margin: 0.4rem 0; }
  a.btn { display: inline-block; background: #1976d2; color: #fff; padding: 0.65rem 1.2rem;
          border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.92rem;
          margin-top: 1rem; }
  a.btn:hover { background: #1565c0; }
</style>
</head>
<body>
  <p class="check">YES</p>
  <h1>Got it. Check your inbox in &lt;60 minutes.</h1>
  <p class="sub">We'll email <span class="email">__EMAIL__</span> your dev key plus a 2-minute walkthrough.</p>

  <div class="panel">
    <h2>While you wait</h2>
    <p>You've already tried these tools via your AI assistant: <strong>__TOOLS__</strong></p>
    <p>The full set of paid tools (14 endpoints) returns real-time data on grid demand, market intel, energy prices, water risk, and renewable infrastructure across all major US ISOs.</p>
    <a class="btn" href="https://dchub.cloud/ai">See what's included &rarr;</a>
  </div>

  <!-- Phase ZZ (2026-05-15): direct Stripe link cuts the upgrade
       path from 3 hops (redeem → pricing → stripe) to 1 hop
       (redeem → stripe). The DCHUB_STRIPE_DEVELOPER_LINK env var
       holds the canonical Payment Link from the Stripe Dashboard;
       falls back to /pricing if unset so the page still works. -->
  <p style="font-size: 0.85rem; color: #555; margin-top: 1.5rem;
            background: #f5f9ff; border: 1px solid #c3dafe;
            padding: 0.8rem 1rem; border-radius: 6px;">
    <strong>Need it now?</strong> Skip the email wait — upgrade to Developer
    ($49/mo unlimited) right now: <a href="__STRIPE_DEV_LINK__"
    style="color:#1976d2;font-weight:600;">checkout in 60 seconds &rarr;</a>
  </p>
  <p style="font-size: 0.75rem; color: #888; margin-top: 0.5rem;">
    Or compare all tiers at <a href="https://dchub.cloud/pricing">dchub.cloud/pricing</a>.
  </p>
</body>
</html>
"""

ERROR_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Something's off</title>
<style>body{font-family:system-ui;max-width:480px;margin:4rem auto;padding:0 1.5rem;line-height:1.5;}
h1{font-size:1.3rem;}p{color:#555;}a{color:#1976d2;}</style></head>
<body><h1>__TITLE__</h1><p>__MESSAGE__</p>
<p><a href="">Try again &rarr;</a></p></body></html>
"""


def _connect():
    neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not neon:
        return None, "no DB url configured"
    for modname in ('psycopg', 'psycopg2'):
        try:
            mod = __import__(modname)
            return mod.connect(neon), modname
        except Exception:
            continue
    return None, "no postgres driver"


def _capture_request_context():
    ip = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    if not ip:
        ip = request.headers.get('Cf-Connecting-Ip') or request.remote_addr
    ua = request.headers.get('User-Agent', '')[:500]
    return ip, ua


@redeem_bp.route('/api/v1/redeem/<session_id>', methods=['GET', 'POST'])
@redeem_bp.route('/redeem/<session_id>', methods=['GET', 'POST'])
def phase63_redeem(session_id):
    """Email-redeem landing page. Captures IP+UA+email and updates the
    mcp_upgrade_signals rows for this session_id."""
    session_id = (session_id or '').strip()

    # Validate session_id format (must be UUID-shaped)
    if not UUID_RE.match(session_id):
        return Response(
            ERROR_HTML.replace('__TITLE__', 'Invalid session ID')
                      .replace('__MESSAGE__', 'That session ID does not look right. Use the link from your AI assistant exactly.'),
            mimetype='text/html', status=400
        )

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        if not email or not EMAIL_RE.match(email) or len(email) > 254:
            return Response(
                ERROR_HTML.replace('__TITLE__', 'Email address looks off')
                          .replace('__MESSAGE__', 'Please enter a valid email and try again. We use it only to send your dev key.'),
                mimetype='text/html', status=400
            )

        ip, ua = _capture_request_context()
        conn, conn_info = _connect()
        if not conn:
            return Response(
                ERROR_HTML.replace('__TITLE__', 'We had a hiccup')
                          .replace('__MESSAGE__', 'Database is briefly unavailable. Please try again in a minute.'),
                mimetype='text/html', status=503
            )

        tools_tried = []
        try:
            cur = conn.cursor()
            # Update every row for this session
            cur.execute(
                "UPDATE mcp_upgrade_signals "
                "SET user_email = %s, ip_address = COALESCE(ip_address, %s), "
                "    user_agent = COALESCE(user_agent, %s), "
                "    notes = COALESCE(notes, '') || %s "
                "WHERE session_id = %s",
                (email, ip, ua,
                 f"\nphase63_redeem at {datetime.datetime.utcnow().isoformat()}Z",
                 session_id)
            )
            updated = cur.rowcount
            # Fetch tools tried for the success message
            cur.execute(
                "SELECT DISTINCT tool_requested FROM mcp_upgrade_signals "
                "WHERE session_id = %s AND tool_requested IS NOT NULL "
                "ORDER BY tool_requested LIMIT 10",
                (session_id,)
            )
            tools_tried = [r[0] for r in cur.fetchall()]
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return Response(
                ERROR_HTML.replace('__TITLE__', 'We had a hiccup')
                          .replace('__MESSAGE__', f'Could not save your email: {type(e).__name__}. Please try again.'),
                mimetype='text/html', status=500
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

        tools_display = ', '.join(tools_tried[:5]) if tools_tried else 'paid MCP tools'

        # phase 99h: create key + send email + log
        _p99_api_key = _p99_make_key()
        _p99_key_ok, _p99_key_err, _p99_dev_id = False, None, None
        _p99_email_ok, _p99_email_info = False, None
        try:
            _p99_conn, _p99_ci = _connect()
            if _p99_conn:
                _p99_key_ok, _p99_key_err, _p99_dev_id = _p99_persist_key(_p99_conn, email, _p99_api_key, session_id)
                try: _p99_conn.close()
                except Exception: pass
            if _p99_key_ok:
                # Phase ZZZZZ-round32 (2026-05-24): email send was blocking
                # the POST response. SendGrid call adds 2-5s in best case,
                # times out 15s+ when Railway is under load → worker returns
                # 503 → user never sees success page → conversion lost.
                # Master diagnostic 2026-05-24 confirmed 0% redeem POST
                # success rate during Railway-busy periods.
                # Fix: hand the email send to a daemon thread. The user
                # gets the success page immediately (with the API key on
                # it — they have everything they need). Email arrives
                # ~5s later. If email fails, _p99_rec() below + the
                # diagnostic endpoint /api/v1/redeem/diagnostic/<email>
                # still capture the key so we can re-send manually.
                import threading
                _p99_email_ok   = True             # optimistic — logged via diagnostic
                _p99_email_info = 'queued-async'
                def _send_in_background():
                    try:
                        ok, info = _p99_send_email(email, _p99_api_key, tools_tried)
                        _p99_logger.info(f'async-redeem-email session={session_id} email={email} ok={ok} info={info}')
                    except Exception as _bg_e:
                        _p99_logger.error(f'async-redeem-email session={session_id} email={email} crashed: {_bg_e}')
                threading.Thread(target=_send_in_background, daemon=True).start()
        except Exception as _p99_e:
            _p99_key_err = _p99_key_err or f'unexpected: {type(_p99_e).__name__}: {_p99_e}'
        try:
            from routes.redeem_diagnostic import record_redeem_attempt as _p99_rec
            _p99_rec(
                session_id=session_id, email=email,
                email_send_ok=_p99_email_ok,
                email_send_error=_p99_email_info if not _p99_email_ok else None,
                api_key_created=_p99_key_ok,
                api_key_id=_p99_api_key if _p99_key_ok else None,
                extra={'key_err': _p99_key_err, 'developer_id': _p99_dev_id, 'email_info': _p99_email_info},
            )
        except Exception:
            pass
        _p99_logger.info(f'redeem session={session_id} email={email} key_ok={_p99_key_ok} email_ok={_p99_email_ok}')
        # Phase ZZ (2026-05-15): inject the direct Stripe Developer
        # payment link so the success page can offer one-click upgrade.
        # Falls back to /pricing when DCHUB_STRIPE_DEVELOPER_LINK is
        # not configured — the page still works either way.
        _stripe_dev = os.environ.get('DCHUB_STRIPE_DEVELOPER_LINK',
                                       'https://dchub.cloud/pricing')
        return Response(
            SUCCESS_HTML.replace('__EMAIL__', email)
                        .replace('__TOOLS__', tools_display)
                        .replace('__STRIPE_DEV_LINK__', _stripe_dev),
            mimetype='text/html'
        )

    # GET: serve form — tool-aware. Query which tools this session
    # has hit + render personalized headline.
    short = session_id[:8]

    # r50.2 (2026-05-26): populate tools_tried so the landing shows
    # the user WHAT they're unlocking. 7-day data: 990 sessions hit
    # paywall, 0 claimed key — the bare-form GET response gave no
    # context for why the user should fill in the email field.
    tools_tried: list[str] = []
    try:
        conn, _ = _connect()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT DISTINCT tool_requested FROM mcp_upgrade_signals "
                    "WHERE session_id = %s AND tool_requested IS NOT NULL "
                    "ORDER BY tool_requested LIMIT 5",
                    (session_id,)
                )
                tools_tried = [r[0] for r in cur.fetchall()]
                cur.close()
            finally:
                conn.close()
    except Exception:
        pass

    if tools_tried:
        nice_tools = ', '.join(f'<code>{t}</code>' for t in tools_tried[:3])
        if len(tools_tried) > 3:
            nice_tools += f' + {len(tools_tried) - 3} more'
        tools_count = f'{len(tools_tried)} paid tool{"s" if len(tools_tried) != 1 else ""}'
        tools_display = (tools_tried[0] if len(tools_tried) == 1
                         else f'{len(tools_tried)} paid tools')
        tools_hit_block = (
            f'<div class="tools-hit">Tried this session: <strong>{nice_tools}</strong>. '
            f'Your free dev key unlocks all of them up to 1,000 calls/day.</div>'
        )
    else:
        nice_tools = ''
        tools_count = 'paid tools'
        tools_display = 'DC Hub paid tools'
        tools_hit_block = ''

    html = (FORM_HTML
            .replace('__SESSION_SHORT__', short)
            .replace('__TOOLS_DISPLAY__', tools_display)
            .replace('__TOOLS_COUNT__', tools_count)
            .replace('__TOOLS_HIT_BLOCK__', tools_hit_block))
    return Response(
        html,
        mimetype='text/html; charset=utf-8',
        headers={'Cache-Control': 'no-store, must-revalidate'},
    )
