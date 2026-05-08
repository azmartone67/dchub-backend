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
import secrets
import logging as _redeem_logging
_logger = _redeem_logging.getLogger("redeem")
from flask import Blueprint, request, Response



# === phase 99b: actually create a dev key + send via SendGrid ============
def _generate_dev_key():
    """Returns a new dev key string like 'dch_live_<32hex>' — matches existing keys."""
    return f"dch_live_{secrets.token_hex(16)}"


def _persist_dev_key(conn, email, api_key, session_id, source="redeem"):
    """Insert into mcp_dev_keys (the actual table per gen_dev_key.py).

    Schema: api_key, developer_id, email, tier, status, metadata.
    Returns (success: bool, error: str_or_none, developer_id_or_none).
    """
    import json as _json
    developer_id = f"dev_{secrets.token_hex(8)}"
    metadata = {"source": source, "session_id": session_id}
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO mcp_dev_keys
                  (api_key, developer_id, email, tier, status, metadata)
               VALUES (%s, %s, %s, %s, 'active', %s::jsonb)
               ON CONFLICT (api_key) DO NOTHING
               RETURNING developer_id""",
            (api_key, developer_id, email, "free", _json.dumps(metadata)),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            # already existed (rare with random key) — generate a new one
            return False, "ON CONFLICT — key collision (extremely rare)", None
        return True, None, developer_id
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return False, f"{type(e).__name__}: {e}", None


def _send_dev_key_email(email, api_key, tools_tried):
    """Send via SendGrid HTTP API. Returns (ok: bool, error_str: Optional[str])."""
    import os, json
    import urllib.request
    import urllib.error

    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if not sendgrid_key:
        return False, "SENDGRID_API_KEY not configured"

    from_email = os.environ.get("DCHUB_FROM_EMAIL", "noreply@dchub.cloud")
    from_name = os.environ.get("DCHUB_FROM_NAME", "DC Hub")

    tools_display = ", ".join(tools_tried[:5]) if tools_tried else "paid MCP tools"

    subject = "🔓 Your DC Hub dev key is ready"

    install_block = (
        '{\n'
        '  "mcpServers": {\n'
        '    "dchub": {\n'
        '      "command": "npx",\n'
        '      "args": ["-y", "mcp-remote", "https://dchub.cloud/mcp"],\n'
        f'      "env": {{ "DCHUB_API_KEY": "{api_key}" }}\n'
        '    }\n'
        '  }\n'
        '}'
    )

    html = f"""<!DOCTYPE html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px;">
<h2 style="color: #0a6b22;">🔓 Your DC Hub dev key is ready</h2>
<p>Welcome! Here's your free developer API key — unlocks 50 facility lookups, real-time grid data for 7 US ISOs, fiber intel, M&A deals, and 650+ GW pipeline.</p>
<div style="background: #f5f5f5; padding: 16px; border-radius: 8px; font-family: monospace; font-size: 14px; word-break: break-all; margin: 16px 0;">
  <strong>API key:</strong><br><code>{api_key}</code>
</div>
<h3>How to use it</h3>
<p>You tried: <em>{tools_display}</em>. Now those tools will return full data when you include this key.</p>
<h4>Claude Desktop / Cursor / Cline (MCP)</h4>
<p>Edit your MCP config and add:</p>
<pre style="background: #1a1a1a; color: #f0f0f0; padding: 14px; border-radius: 6px; overflow-x: auto; font-size: 12px;">{install_block}</pre>
<h4>Direct API call</h4>
<pre style="background: #1a1a1a; color: #f0f0f0; padding: 14px; border-radius: 6px; font-size: 12px;">curl -H "Authorization: Bearer {api_key}" https://dchub.cloud/api/v1/grid-intelligence?iso=ERCOT</pre>
<h3>What you've unlocked</h3>
<ul>
<li>50 facility lookups across 12,500+ data centers</li>
<li>Real-time grid data: PJM, ERCOT, CAISO, NYISO, MISO, SPP, ISONE</li>
<li>Fiber connectivity intelligence + carrier maps</li>
<li>1,800+ M&A deals indexed</li>
<li>650+ GW construction pipeline</li>
<li>SEC EDGAR filings for 17 hyperscaler/REIT/power companies</li>
</ul>
<p style="color: #888; margin-top: 32px; font-size: 13px;">Need more? Upgrade to Pro at <a href="https://dchub.cloud/pricing">https://dchub.cloud/pricing</a> for $49/mo unlimited access. Reply to this email if you have questions.</p>
<p style="color: #888; font-size: 12px;">— The DC Hub team</p>
</body></html>"""

    text = f"""Your DC Hub dev key is ready.

API KEY: {api_key}

You tried: {tools_display}.

Add to Claude Desktop / Cursor / Cline:
{install_block}

Direct API:
curl -H "Authorization: Bearer {api_key}" https://dchub.cloud/api/v1/grid-intelligence?iso=ERCOT

What's unlocked:
- 50 facility lookups across 12,500+ data centers
- Real-time grid data for 7 US ISOs
- Fiber connectivity intelligence
- 1,800+ M&A deals
- 650+ GW construction pipeline
- SEC EDGAR filings for 17 hyperscaler/REIT companies

Need more? Upgrade to Pro at https://dchub.cloud/pricing — $49/mo unlimited.

— The DC Hub team
"""

    payload = {
        "personalizations": [{"to": [{"email": email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html", "value": html},
        ],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {sendgrid_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, f"SendGrid HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8")[:500]
        except Exception: pass
        return False, f"SendGrid HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

# === end phase 99b helpers ============================================

redeem_bp = Blueprint('redeem', __name__)

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Get your free DC Hub dev key</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 480px; margin: 0 auto; padding: 4rem 1.5rem;
         color: #111; background: #fafafa; line-height: 1.55; }
  h1 { font-size: 1.5rem; margin: 0 0 0.5rem; font-weight: 600; }
  .sub { color: #555; margin: 0 0 1.5rem; font-size: 0.95rem; }
  .badge { display: inline-block; background: #e8f5e9; color: #1b5e20;
           padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.8rem;
           font-weight: 600; margin-bottom: 1rem; }
  input[type="email"] { width: 100%; padding: 0.85rem 1rem; border: 1.5px solid #d0d0d0;
          border-radius: 8px; font-size: 1rem; background: #fff;
          margin: 1rem 0 0.75rem; transition: border-color 0.15s; }
  input[type="email"]:focus { outline: 0; border-color: #1976d2; }
  button { background: #1976d2; color: #fff; border: 0; padding: 0.85rem 1.5rem;
           border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer;
           width: 100%; transition: background 0.15s; }
  button:hover { background: #1565c0; }
  ul.bullets { padding-left: 1.2rem; margin: 1rem 0; color: #555; font-size: 0.92rem; }
  ul.bullets li { margin: 0.3rem 0; }
  .note { font-size: 0.8rem; color: #888; margin-top: 2rem; padding-top: 1rem;
          border-top: 1px solid #eee; }
  code { font-family: "SF Mono", Monaco, Consolas, monospace; font-size: 0.78rem;
         background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }
</style>
</head>
<body>
  <span class="badge">DC Hub Free Dev Tier</span>
  <h1>Get your free dev key</h1>
  <p class="sub">60 seconds, just your email. No password. No spam. We'll send your key within an hour.</p>
  <form method="post">
    <input type="email" name="email" placeholder="you@company.com" required autofocus>
    <button type="submit">Send me my dev key</button>
  </form>
  <ul class="bullets">
    <li>25 free MCP tool calls/day across 14 paid tools</li>
    <li>Real-time grid intelligence, market intel, infrastructure data</li>
    <li>Upgrade to Pro ($49/mo) for unlimited any time</li>
  </ul>
  <p class="note">Session: <code>__SESSION_SHORT__</code></p>
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

  <p style="font-size: 0.8rem; color: #888; margin-top: 2rem;">
    Need it now? Upgrade to Pro for $49/mo unlimited at <a href="https://dchub.cloud/pricing">dchub.cloud/pricing</a>.
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

        # === phase 99d: actually create dev key + send email ===
        api_key = _generate_dev_key()
        key_ok, key_err, developer_id = False, None, None
        email_ok, email_err = False, None
        try:
            conn2, _ci = _connect()
            if conn2:
                key_ok, key_err, developer_id = _persist_dev_key(conn2, email, api_key, session_id)
                try: conn2.close()
                except Exception: pass
            if key_ok:
                email_ok, email_err = _send_dev_key_email(email, api_key, tools_tried)
        except Exception as _e_phase99d:
            key_err = key_err or f'unexpected: {type(_e_phase99d).__name__}: {_e_phase99d}'

        try:
            from routes.redeem_diagnostic import record_redeem_attempt
            record_redeem_attempt(
                session_id=session_id, email=email,
                email_send_ok=email_ok, email_send_error=email_err,
                api_key_created=key_ok,
                api_key_id=api_key if key_ok else None,
                extra={'key_err': key_err, 'tools_tried': (tools_tried or [])[:5], 'developer_id': developer_id},
            )
        except Exception:
            pass
        _logger.info(f'redeem session={session_id} email={email} key_ok={key_ok} email_ok={email_ok}')

        return Response(
            SUCCESS_HTML.replace('__EMAIL__', email)
                        .replace('__TOOLS__', tools_display),
            mimetype='text/html'
        )

    # GET: serve form
    short = session_id[:8]
    return Response(
        FORM_HTML.replace('__SESSION_SHORT__', short),
        mimetype='text/html'
    )
