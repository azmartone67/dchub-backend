"""Phase ZZZZZ-round40 (2026-05-25) — Post-redeem onboarding page.

Funnel diagnosis: 9,885 paywall hits → 0 active dev keys. Free signups
get JSON {api_key:...} from /api/v1/redeem/<code> and bounce because
they don't know what to do next. This route serves a full HTML setup
guide with the API key pre-filled into each runtime config snippet
plus a one-click "test it" button.

Wiring (main.py):
    from routes.onboarding_page import onboarding_bp
    app.register_blueprint(onboarding_bp)

Also update the Stripe webhook + email-capture flow to send users to
https://dchub.cloud/onboard/<code> instead of /api/v1/redeem/<code>.
"""
import os, html as _html
from flask import Blueprint, request, jsonify, Response
import psycopg

onboarding_bp = Blueprint("onboarding_page", __name__)
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")

HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>You're in! Set up DC Hub MCP</title>
<link rel="icon" href="/favicon.ico">
<style>
body{font:16px/1.55 -apple-system,system-ui,sans-serif;max-width:680px;margin:48px auto;padding:0 24px;color:#0f172a;background:#fafbfc}
h1{font-size:1.7rem;margin:0 0 14px;letter-spacing:-.01em}
.eyebrow{color:#16a34a;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:700;margin-bottom:10px}
.lead{color:#475569;font-size:1.05rem;margin-bottom:32px}
.keybox{background:#0f172a;color:#fff;padding:18px 22px;border-radius:10px;font-family:ui-monospace,monospace;font-size:.95rem;word-break:break-all;margin:16px 0;position:relative}
.keybox button,pre button{position:absolute;top:8px;right:8px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#fff;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.75rem;font-family:inherit}
.step{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 22px;margin:14px 0}
.step h2{margin:0 0 10px;font-size:1.1rem;display:flex;align-items:center;gap:10px}
.step-num{background:#6366f1;color:#fff;border-radius:50%;width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;font-size:.9rem;font-weight:700}
pre{background:#0f172a;color:#fff;padding:14px 18px;border-radius:8px;overflow-x:auto;font-size:.85rem;line-height:1.5;font-family:ui-monospace,monospace;position:relative;margin:8px 0}
.btn{display:inline-block;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;border:none;font-size:.95rem;cursor:pointer;font-family:inherit}
.btn:disabled{opacity:.6;cursor:wait}
#tr{margin-top:12px;font-size:.9rem}
#tr.ok{color:#16a34a}#tr.err{color:#dc2626}
.footer{margin-top:36px;padding-top:20px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
.footer a{color:#6366f1}
</style></head><body>
<div class="eyebrow">✓ Welcome to DC Hub</div>
<h1>You're in. Three steps to first call.</h1>
<p class="lead">Your API key is ready. Plug it into any AI agent runtime in under 60 seconds.</p>

<div class="step"><h2><span class="step-num">1</span> Your API key</h2>
<p>Copy this — treat it like a password.</p>
<div class="keybox"><button onclick="copyKey()">copy</button><span id="apikey">__API_KEY__</span></div></div>

<div class="step"><h2><span class="step-num">2</span> Add to your AI runtime</h2>
<p><strong>Claude Desktop</strong> — <code>~/Library/Application Support/Claude/claude_desktop_config.json</code>:</p>
<pre><button onclick="copyPre(this)">copy</button><code>{
  "mcpServers": {
    "dchub": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://dchub.cloud/mcp",
               "--header", "X-API-Key:__API_KEY__"]
    }
  }
}</code></pre>
<p style="margin-top:14px"><strong>Cursor / Cline / Continue.dev</strong>:</p>
<pre><button onclick="copyPre(this)">copy</button><code>{
  "dchub": {
    "transport": "streamable-http",
    "url": "https://dchub.cloud/mcp",
    "headers": { "X-API-Key": "__API_KEY__" }
  }
}</code></pre>
<p style="margin-top:14px"><strong>Claude.ai (web)</strong> — Settings → Connectors → + Add custom — URL <code>https://dchub.cloud/mcp</code> — Header <code>X-API-Key: __API_KEY__</code></p></div>

<div class="step"><h2><span class="step-num">3</span> Test it works</h2>
<p>One click — pings /mcp with your key right now. Zero install.</p>
<button class="btn" id="tb" onclick="testCall()">Run test call →</button>
<div id="tr"></div></div>

<div class="footer">Need help? <a href="mailto:api@dchub.cloud">api@dchub.cloud</a> · <a href="https://dchub.cloud/integrations/mcp">Docs</a> · <a href="https://dchub.cloud/pricing">Upgrade: Starter $9 / Developer $49 / Pro $199</a></div>

<script>
const KEY="__API_KEY__";
function copyKey(){navigator.clipboard.writeText(KEY)}
function copyPre(b){const c=b.parentElement.querySelector("code");if(!c)return;navigator.clipboard.writeText(c.textContent.replace(/__API_KEY__/g,KEY));const t=b.textContent;b.textContent="copied!";setTimeout(()=>b.textContent=t,1500)}
async function testCall(){
  const b=document.getElementById("tb"),o=document.getElementById("tr");
  b.disabled=true;b.textContent="Calling…";o.textContent="";o.className="";
  try{
    const i=await fetch("https://dchub.cloud/mcp",{method:"POST",headers:{"X-API-Key":KEY,"Content-Type":"application/json","Accept":"application/json, text/event-stream"},body:JSON.stringify({jsonrpc:"2.0",id:1,method:"initialize",params:{protocolVersion:"2024-11-05",capabilities:{},clientInfo:{name:"onboarding",version:"1"}}})});
    const sid=i.headers.get("mcp-session-id");if(!sid)throw new Error("no session id");
    const t=await fetch("https://dchub.cloud/mcp",{method:"POST",headers:{"X-API-Key":KEY,"Mcp-Session-Id":sid,"Content-Type":"application/json","Accept":"application/json, text/event-stream"},body:JSON.stringify({jsonrpc:"2.0",id:2,method:"tools/list",params:{}})});
    const tx=await t.text();const dl=tx.split("\n").find(l=>l.startsWith("data: "));
    const d=JSON.parse(dl?dl.slice(6):tx);const n=(d.result&&d.result.tools||[]).length;
    o.className="ok";o.innerHTML="✓ <strong>Connected.</strong> "+n+" tools available. Your key is active.";
    b.textContent="✓ Tested";
    // Track key_first_use
    fetch("/api/v1/onboard/activated?source=test_button",{method:"POST",headers:{"X-API-Key":KEY}}).catch(()=>{});
  }catch(e){o.className="err";o.textContent="✗ "+e.message;b.disabled=false;b.textContent="Retry →"}
}
</script></body></html>"""

@onboarding_bp.route("/onboard/<code>", methods=["GET"])
def onboarding_page(code):
    """Serve full HTML onboarding page for a redeemed dev-key code.
    Bot/programmatic clients should still use /api/v1/redeem/<code> for JSON."""
    if not NEON_URL:
        return jsonify({"error": "NEON_DATABASE_URL not configured"}), 500
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT api_key, email FROM mcp_dev_keys "
                "WHERE metadata->>'redeem_code' = %s AND status='active' LIMIT 1",
                (code,)
            )
            row = cur.fetchone()
        if not row:
            return Response(
                "<html><body style=\"font:16px/1.5 sans-serif;max-width:480px;margin:60px auto;padding:0 24px;text-align:center\">"
                "<h1>That link is invalid or expired</h1>"
                "<p>Redeem links work once and expire after 24h.</p>"
                "<p><a href='/signup'>Get a new dev key →</a></p></body></html>",
                status=404, mimetype="text/html; charset=utf-8")
        api_key, email = row
    except Exception as e:
        return jsonify({"error": "lookup_failed", "detail": str(e)}), 500

    # Track key_issued event (idempotent; multiple opens fine)
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mcp_call_log (api_key, tool, status, event_type, referrer, user_agent, timestamp) "
                "VALUES (%s, 'onboarding_page_view', 'ok', 'key_issued', %s, %s, NOW())",
                (api_key, request.headers.get("Referer", ""), (request.headers.get("User-Agent", "") or "")[:500])
            )
    except Exception:
        pass

    page = HTML.replace("__API_KEY__", _html.escape(api_key))
    return Response(page, mimetype="text/html; charset=utf-8",
                    headers={"Cache-Control": "private, no-store",
                             "X-DC-Phase": "ZZZZZ-round40-onboarding"})


@onboarding_bp.route("/api/v1/onboard/activated", methods=["POST"])
def mark_activated():
    """Tracked by the in-page test button. Logs key_first_use for funnel analytics."""
    api_key = request.headers.get("X-API-Key", "").strip()
    if not api_key or not NEON_URL:
        return jsonify({"ok": False}), 400
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mcp_call_log (api_key, tool, status, event_type, referrer, user_agent, timestamp) "
                "VALUES (%s, 'onboard_test_button', 'ok', 'key_first_use', %s, %s, NOW())",
                (api_key, request.headers.get("Referer", ""), (request.headers.get("User-Agent", "") or "")[:500])
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 500


@onboarding_bp.route("/onboard/_health")
def _health():
    return jsonify({"ok": True, "phase": "ZZZZZ-round40-onboarding",
                    "template_bytes": len(HTML)})
