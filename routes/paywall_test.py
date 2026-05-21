"""Phase r32-paywall (2026-05-20) — paywall response diagnostic.
==========================================================================

User wanted to know: when DC Hub fires a paywall response back through
MCP to Claude / ChatGPT / Perplexity / Gemini, does the LLM ACTUALLY
render the upgrade URL as a clickable link, or does it strip / mangle it?

15,837 paywall signals / 30d but 9 conversions = 0.05%. The structural
hypothesis: LLM clients aren't rendering our paywall payload usefully
to the human, so the conversion path is broken before it starts.

This module ships:

  GET /api/v1/admin/paywall-test?tool=get_grid_intelligence&format=raw
    Returns the EXACT paywall response shape DC Hub would send back
    for that tool. Side-by-side in three formats so the operator can:
      1. paste into Claude desktop / chat
      2. paste into ChatGPT
      3. paste into Perplexity
    and watch how each one renders the redeem URL.

  GET /paywall-test
    HTML page that runs the same probe with a "copy" button per
    format, plus per-LLM "open prompt" deep-links.

  POST /api/v1/admin/paywall-test/log-render
    Operator-fed observation: "Claude renders the URL as a link",
    "ChatGPT strips it", etc. Logs to brain_findings so we can
    measure the funnel input experimentally.

This is the diagnostic that tells us whether to fix the paywall
payload, or pivot entirely to client-side DevRel outreach.
"""
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, render_template_string

logger = logging.getLogger(__name__)
paywall_test_bp = Blueprint("paywall_test", __name__)


_INTERNAL_KEYS: set = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY",
           "ADMIN_API_KEY", "ADMIN_SECRET"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.cookies.get("dchub_admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# Mirror of the exact paywall payload from mcp_upgrade_gate.gate_tool_call.
# Keeping these in sync with the live gate logic is critical — if we
# probe a different shape than what we actually send, the diagnostic
# lies. If you change the message format there, update here too.
SIGNUP_URL = "https://dchub.cloud/signup?utm_source=mcp_paywall"
UPGRADE_URL = "https://dchub.cloud/pricing?utm_source=mcp_paywall"


def _generate_redeem_url(session_id: str = "TEST-DEMO") -> dict:
    """Mirror routes.pair_code.get_or_create_code — but in test mode
    without actually minting a real code. Returns the URL shape the
    live paywall would emit."""
    return {
        "code":       f"DCM-{session_id[-4:].upper() if session_id else 'TEST'}",
        "redeem_url": f"https://dchub.cloud/redeem/DCM-{session_id[-4:].upper() if session_id else 'TEST'}",
        "expires_in_hours": 24,
    }


def _build_paywall_payload(tool: str, session_id: str = "") -> dict:
    """Return the exact response shape gate_tool_call returns to MCP
    clients. The `message` field is what an LLM agent receives back
    when it tries to call a paid tool from free tier."""
    pair = _generate_redeem_url(session_id)
    msg = (
        f"🔓 **The {tool} tool requires a paid plan.**"
        f"\n\n"
        f"👉 **Human handoff:** get a free dev key here:\n"
        f"{pair['redeem_url']}\n\n"
        f"No credit card. Unlocks 50 facility lookups, real-time grid for 7 ISOs, fiber intel, M&A deals.\n\n"
        f"_Or upgrade to Pro at {UPGRADE_URL} for $49/mo unlimited access._"
    )
    return {
        "allowed":     False,
        "tier":        "free",
        "tool":        tool,
        "platform":    "test",
        "message":     msg,
        "redeem_url":  pair["redeem_url"],
        "upgrade_url": UPGRADE_URL,
    }


# Per-LLM "open chat with prompt" deep-links so the operator can
# 1-click test each surface.
LLM_OPEN_URLS = {
    "claude": "https://claude.ai/new?q={prompt}",
    "chatgpt": "https://chatgpt.com/?q={prompt}",
    "perplexity": "https://www.perplexity.ai/search?q={prompt}",
    "gemini": "https://gemini.google.com/app?q={prompt}",
}


def _llm_open_link(client: str, paywall_msg: str) -> str:
    """Build a deep-link URL that opens the LLM with a prompt that
    SHOULD trigger it to render the embedded URL. The prompt is
    designed to be neutral — just paste the message and observe."""
    import urllib.parse as _up
    prompt = (
        "I called a DC Hub MCP tool and got this response. "
        "Please tell me exactly what URLs you see and whether you'd render "
        "any of them as clickable links to a human user:\n\n" + paywall_msg
    )
    tpl = LLM_OPEN_URLS.get(client, "")
    if not tpl: return ""
    return tpl.format(prompt=_up.quote(prompt))


@paywall_test_bp.route("/api/v1/admin/paywall-test", methods=["GET"])
def paywall_test_json():
    """Return the paywall payload in multiple format snapshots so the
    operator can paste each into a different LLM and observe rendering."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401

    tool       = (request.args.get("tool") or "get_grid_intelligence").strip()
    session_id = (request.args.get("session_id") or "TEST-DEMO").strip()
    payload    = _build_paywall_payload(tool, session_id)
    msg        = payload["message"]

    # Three format snapshots that each surface differently in LLMs.
    formats = {
        "raw_message": msg,                       # Markdown — what we actually send
        "stripped_md": (msg
                        .replace("**", "")
                        .replace("_", "")),       # Plain text (what some LLMs see post-strip)
        "json_body":   payload,                   # If LLM parses the whole JSON
    }

    open_links = {
        client: _llm_open_link(client, msg)
        for client in LLM_OPEN_URLS
    }

    return jsonify(
        ok=True,
        as_of=datetime.utcnow().isoformat() + "Z",
        tool=tool,
        session_id=session_id,
        payload=payload,
        formats=formats,
        llm_open_links=open_links,
        observations_endpoint="/api/v1/admin/paywall-test/log-render",
        diagnostic_question=(
            "Open each LLM via the open_links, paste in the raw_message, "
            "and observe: (a) does the LLM show the redeem_url as a "
            "clickable link? (b) does it show the upgrade_url? (c) does "
            "it strip or rewrite either URL? Log observations via the "
            "log-render endpoint."
        ),
    ), 200


@paywall_test_bp.route("/api/v1/admin/paywall-test/log-render",
                       methods=["POST"])
def paywall_test_log_render():
    """Operator logs an LLM-render observation. Body:
       {client: "claude"|"chatgpt"|..., renders_redeem: bool,
        renders_upgrade: bool, notes: "..."}
    Writes to brain_findings for later aggregate analysis."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    client = (body.get("client") or "").lower().strip()
    if client not in LLM_OPEN_URLS:
        return jsonify(ok=False, error="bad_client",
                       valid=list(LLM_OPEN_URLS.keys())), 400

    renders_redeem  = bool(body.get("renders_redeem", False))
    renders_upgrade = bool(body.get("renders_upgrade", False))
    notes           = (body.get("notes") or "").strip()[:500]

    try:
        import psycopg2
        db = os.environ.get("DATABASE_URL")
        with psycopg2.connect(db, sslmode="require", connect_timeout=8) as c, \
             c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_findings
                    (issue, url, count, detail, detector, created_at)
                   VALUES ('paywall_render_observation', %s, %s, %s,
                           'paywall_test', NOW())""",
                (f"/paywall-test?client={client}",
                 1 if (renders_redeem or renders_upgrade) else 0,
                 f"client={client} redeem={renders_redeem} "
                 f"upgrade={renders_upgrade} notes={notes}"),
            )
            c.commit()
    except Exception as e:
        logger.warning(f"paywall_test log failed: {e}")
        return jsonify(ok=False, error="db_log_failed",
                       detail=str(e)[:200]), 500

    return jsonify(ok=True, logged=True, client=client,
                   renders_redeem=renders_redeem,
                   renders_upgrade=renders_upgrade), 200


_PAYWALL_TEST_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Paywall Diagnostic · DC Hub</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;
  --indigo:#6366f1;--violet:#a855f7;--green:#10b981;--orange:#f59e0b;--red:#ef4444;
  --mono:'JetBrains Mono','SF Mono',monospace;color-scheme:dark}
*{box-sizing:border-box}body{font-family:'Instrument Sans',-apple-system,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:2.5rem 1.5rem}
.kicker{font-family:var(--mono);font-size:.78rem;color:#c4b5fd;text-transform:uppercase;letter-spacing:.14em;margin-bottom:.6rem}
h1{margin:0 0 .5rem;font-size:2.2rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:760px;margin:0 0 2rem}
h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.12em;margin:2.5rem 0 1rem;font-weight:700}
.section{background:var(--surface);border:1px solid var(--bd);border-radius:12px;padding:1.5rem 1.75rem;margin-bottom:1rem}
.section h3{margin:0 0 .75rem;font-size:1rem;color:var(--tx)}
.section h3 small{font-family:var(--mono);font-size:.74rem;color:var(--tx2);font-weight:500;text-transform:uppercase;letter-spacing:.1em;margin-left:.5rem}
pre{background:#0a0a12;border:1px solid var(--bd);border-radius:8px;padding:1rem 1.25rem;font-family:var(--mono);font-size:.82rem;color:#cbd5e1;overflow-x:auto;line-height:1.5;margin:0 0 .75rem;white-space:pre-wrap;word-break:break-word}
button.copy{background:var(--indigo);color:#fff;border:0;padding:.4rem 1rem;border-radius:6px;font-size:.78rem;font-weight:600;cursor:pointer;font-family:var(--mono);text-transform:uppercase;letter-spacing:.06em}
button.copy:hover{background:var(--violet)}
.llm-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.75rem;margin-top:.75rem}
.llm-row a{display:block;padding:.85rem 1.25rem;background:linear-gradient(135deg,#6366f122,#a855f722);border:1px solid #a855f744;border-radius:8px;color:#fff;text-decoration:none;text-align:center;font-weight:600;font-size:.88rem;transition:all .15s}
.llm-row a:hover{background:linear-gradient(135deg,#6366f1,#a855f7);transform:translateY(-1px)}
.input-row{display:flex;gap:.5rem;margin-bottom:1.5rem}
.input-row input,.input-row select{background:#11121a;border:1px solid var(--bd);border-radius:8px;padding:.55rem 1rem;color:#fff;font-family:var(--mono);font-size:.85rem;flex:1}
.observ{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.75rem;margin-top:1rem}
.observ-card{background:#0a0a12;border:1px solid var(--bd);border-radius:8px;padding:1rem 1.25rem}
.observ-card label{font-size:.74rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:.5rem;font-weight:600}
.observ-card .opts{display:flex;gap:.5rem;margin-bottom:.5rem}
.observ-card button{background:#1f2030;color:#cbd5e1;border:1px solid #2a2d40;padding:.4rem .8rem;border-radius:6px;font-size:.8rem;cursor:pointer}
.observ-card button.yes{background:rgba(16,185,129,.18);color:#10b981;border-color:#10b98144}
.observ-card button.no{background:rgba(239,68,68,.18);color:#ef4444;border-color:#ef444444}
.observ-card button.active{outline:2px solid currentColor}
</style></head><body><div class="wrap">
<div class="kicker">DC HUB · PAYWALL DIAGNOSTIC</div>
<h1>Does our paywall actually convert?</h1>
<p class="sub">15,837 paywall signals / 30d but only 9 conversions = 0.05%. This page lets you see the exact MCP response we send back when an LLM hits a paid tool, then open Claude / ChatGPT / Perplexity / Gemini with that response pasted, so you can watch how each one renders the redeem URL.</p>

<div class="input-row">
  <select id="tool" onchange="reload()">
    <option value="get_grid_intelligence">get_grid_intelligence (4,167 signals)</option>
    <option value="get_fiber_intel">get_fiber_intel (3,884 signals)</option>
    <option value="get_market_intel">get_market_intel (3,709 signals)</option>
    <option value="get_grid_data">get_grid_data (3,109 signals)</option>
    <option value="get_water_risk">get_water_risk (3,022 signals)</option>
    <option value="analyze_site">analyze_site (paid premium)</option>
    <option value="compare_sites">compare_sites (paid premium)</option>
  </select>
  <button class="copy" onclick="reload()">Reload</button>
</div>

<div class="section">
  <h3>1 · Raw Markdown <small>what we actually send</small></h3>
  <pre id="raw">Loading…</pre>
  <button class="copy" onclick="cp('raw')">Copy raw</button>
</div>

<div class="section">
  <h3>2 · Stripped <small>some LLMs flatten markdown before display</small></h3>
  <pre id="stripped">Loading…</pre>
  <button class="copy" onclick="cp('stripped')">Copy stripped</button>
</div>

<div class="section">
  <h3>3 · JSON body <small>full response shape, if LLM parses as data</small></h3>
  <pre id="json">Loading…</pre>
  <button class="copy" onclick="cp('json')">Copy JSON</button>
</div>

<h2>Test in each LLM</h2>
<div class="llm-row" id="llms"></div>

<h2>Log what each LLM did</h2>
<p style="color:var(--tx2);font-size:.9rem;margin:0 0 1rem">
After pasting the raw markdown into each LLM, mark whether it rendered the redeem URL as a clickable link. Logs to brain_findings so we can correlate render-success with conversion rate over time.
</p>
<div class="observ" id="observ"></div>

<script>
let DATA = null;
async function reload(){
  const tool = document.getElementById('tool').value;
  const r = await fetch('/api/v1/admin/paywall-test?tool=' + tool);
  const d = await r.json();
  if (!d.ok) { alert('Auth needed: ' + d.error); return; }
  DATA = d;
  document.getElementById('raw').textContent      = d.formats.raw_message;
  document.getElementById('stripped').textContent = d.formats.stripped_md;
  document.getElementById('json').textContent     = JSON.stringify(d.formats.json_body, null, 2);
  // LLM open buttons
  const llms = document.getElementById('llms');
  llms.innerHTML = '';
  for (const [client, url] of Object.entries(d.llm_open_links || {})) {
    if (!url) continue;
    const a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Open ' + client.charAt(0).toUpperCase() + client.slice(1) + ' →';
    llms.appendChild(a);
  }
  // Observation cards
  const observ = document.getElementById('observ');
  observ.innerHTML = '';
  for (const client of ['claude','chatgpt','perplexity','gemini']) {
    const card = document.createElement('div');
    card.className = 'observ-card';
    card.innerHTML = `
      <label>${client}</label>
      <div class="opts" data-q="redeem">
        <span style="font-size:.74rem;color:#9ca3af;margin-right:.5rem">Redeem URL?</span>
        <button class="yes" onclick="mark(this, '${client}', 'redeem', true)">Yes</button>
        <button class="no"  onclick="mark(this, '${client}', 'redeem', false)">No</button>
      </div>
      <div class="opts" data-q="upgrade">
        <span style="font-size:.74rem;color:#9ca3af;margin-right:.5rem">Upgrade URL?</span>
        <button class="yes" onclick="mark(this, '${client}', 'upgrade', true)">Yes</button>
        <button class="no"  onclick="mark(this, '${client}', 'upgrade', false)">No</button>
      </div>`;
    observ.appendChild(card);
  }
}
function cp(id){
  const t = document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(()=>{
    event.target.textContent = 'Copied ✓';
    setTimeout(()=>{ event.target.textContent = 'Copy ' + id; }, 1200);
  });
}
const _state = {};
function mark(btn, client, q, v){
  // toggle visual
  btn.parentElement.querySelectorAll('button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _state[client] = _state[client] || {};
  _state[client][q] = v;
  // POST as a partial update — debounce 500ms
  clearTimeout(_state[client]._t);
  _state[client]._t = setTimeout(() => {
    fetch('/api/v1/admin/paywall-test/log-render', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        client: client,
        renders_redeem:  !!_state[client].redeem,
        renders_upgrade: !!_state[client].upgrade,
        notes: 'logged via paywall-test page'
      })
    });
  }, 500);
}
reload();
</script>
</div></body></html>'''


@paywall_test_bp.route("/paywall-test", methods=["GET"])
def paywall_test_page():
    if not _admin_ok():
        # Reuse the visitor-intelligence login flow for consistency.
        return Response(
            "<!DOCTYPE html><html><body style='font-family:system-ui;"
            "background:#0a0a12;color:#fff;padding:3rem;text-align:center'>"
            "<h1>Login first</h1><p>Visit "
            "<a href='/visitor-intelligence/auth?key=YOUR_KEY' "
            "style='color:#6366f1'>/visitor-intelligence/auth</a> "
            "to set the admin cookie, then come back to /paywall-test."
            "</p></body></html>",
            status=401, mimetype="text/html",
        )
    return Response(render_template_string(_PAYWALL_TEST_HTML),
                    mimetype="text/html")
