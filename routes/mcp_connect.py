"""
mcp_connect.py — per-MCP-client landing pages.

Phase r80-mcp-connect (2026-06-06). The funnel deep-dive showed that the
conversion path that actually closes requires the MCP client to hold an
X-API-Key persistently. Claude.ai web (anon-only) can't. But Cursor, Cline,
Continue, and Claude Desktop ALL can — they store the key in their MCP
config file. Yet there was no per-client landing page that walked an
operator through (a) mint trial key, (b) paste install snippet, (c) try
prompt examples, (d) upgrade — in one place.

This blueprint ships four pages:

    GET  /connect/cursor
    GET  /connect/cline
    GET  /connect/continue
    GET  /connect/claude-desktop

Each is:
    1. Header + value prop ("DC Hub for <Client> — 38 tools, free tier, 30s")
    2. A "Mint your free trial key" button (POSTs /api/v1/keys/claim?platform=X,
       in-place swaps to the install snippet with the key embedded)
    3. A pre-rendered, copy-button install snippet tuned per client
    4. Three "Try it" example prompts tuned to the client
    5. Trial limits + Pro Monthly + Pro Annual upgrade CTAs (Stripe links
       carry client_reference_id={trial_key} for Fix-E-style attribution)

Telemetry: every page view INSERTs a row into connect_landing_views. The
mint button updates that row with the minted key for funnel attribution.
"""

from __future__ import annotations

import os
import json
import logging
from flask import Blueprint, request, jsonify, make_response, redirect

logger = logging.getLogger(__name__)

mcp_connect_bp = Blueprint("mcp_connect", __name__)


# ── Per-client config ────────────────────────────────────────────────────
# Each client gets: pretty name, install file path on each OS, snippet
# format, snippet content (a JSON or YAML body), and a deep-link if one
# exists. The TRIAL_KEY placeholder is replaced client-side after the
# mint button POST returns.

_CLIENTS = {
    "cursor": {
        "name":           "Cursor",
        "tagline":        "The AI code editor — get DC Hub MCP data in 30s",
        "install_path":   "~/Library/Application Support/Cursor/User/globalStorage/mcp.json",
        "install_path_win": "%APPDATA%\\Cursor\\User\\globalStorage\\mcp.json",
        "snippet_lang":   "json",
        # Cursor reads mcp.json with mcpServers map. Setting headers.X-API-Key
        # lets DC Hub recognise the operator as IDENTIFIED (50 calls/day,
        # 7 day trial). The URL path is the canonical MCP endpoint.
        "snippet":        """{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        # Deep-link: no documented `cursor://` MCP install protocol as of
        # 2026-06. Leaving blank — operator hand-pastes config file. If
        # Cursor ships a protocol later we wire it in here.
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "What's the gas pipeline capacity within 50 miles of Northern Virginia data centers?",
            "Rank the top 10 US power markets by DCPI for build readiness in Q4 2026.",
            "Find me 50MW+ powered shell sites available in PJM with substation co-location.",
        ],
    },
    "cline": {
        "name":           "Cline",
        "tagline":        "VSCode autonomous agent — bind DC Hub MCP in your settings",
        "install_path":   "~/.cline/mcp_settings.json",
        "install_path_win": "%USERPROFILE%\\.cline\\mcp_settings.json",
        "snippet_lang":   "json",
        # Cline (the VSCode extension) loads mcp_settings.json on
        # workspace open. Same mcpServers schema as Cursor.
        "snippet":        """{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Compare interconnect queue depth for ERCOT West vs PJM AEP this quarter.",
            "Pull DC Hub's latest hyperscaler deal autopsy for AWS in Ohio.",
            "What 100MW+ sites in MISO have substation capacity + fiber within 1 mile?",
        ],
    },
    "continue": {
        "name":           "Continue",
        "tagline":        "Open-source AI dev assistant — add DC Hub MCP to config.yaml",
        "install_path":   "~/.continue/config.yaml",
        "install_path_win": "%USERPROFILE%\\.continue\\config.yaml",
        "snippet_lang":   "yaml",
        # Continue uses YAML; the MCP block lives under `models:` with
        # provider: mcp. The schema matches Continue's docs.continue.dev
        # MCP support page.
        "snippet":        """models:
  - name: dchub
    provider: mcp
    url: https://dchub.cloud/mcp
    headers:
      X-API-Key: "{{TRIAL_KEY}}\"""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Run get_grid_intelligence on CAISO and summarize spare headroom.",
            "Score this 200MW Northern Virginia site for DCPI BUILD readiness.",
            "List the top 5 emerging DCPI markets outside the top 20 metros.",
        ],
    },
    "claude-desktop": {
        "name":           "Claude Desktop",
        "tagline":        "Anthropic's desktop app — wire DC Hub MCP into claude_desktop_config.json",
        "install_path":   "~/Library/Application Support/Claude/claude_desktop_config.json",
        "install_path_win": "%APPDATA%\\Claude\\claude_desktop_config.json",
        "snippet_lang":   "json",
        # Claude Desktop config schema mirrors Cursor's — same mcpServers
        # map. Restart Claude Desktop after editing for the server to
        # appear in the connector picker.
        "snippet":        """{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        # Claude Desktop doesn't expose an `claude://` install protocol
        # as of 2026-06. Operator restarts the app after pasting the JSON.
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "What's the latest grid intelligence for PJM — load + generation mix?",
            "Find me 50MW+ powered land sites in Texas with sub-12-month TTP.",
            "Rank the top 10 international markets by DCPI for Q4 build decisions.",
        ],
    },
}


# Pro Monthly + Pro Annual Stripe Payment Links (canonical, from
# routes/_stripe_links.py). client_reference_id={trial_key} attributes
# the conversion back to the page that minted the key (Fix E pattern).
_STRIPE_MONTHLY = "https://buy.stripe.com/eVq5kE4oOfs13mleGuaZi0h"      # $199/mo
_STRIPE_ANNUAL  = "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07"      # $1,188/yr


# ── Telemetry: best-effort DB write ──────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _record_view(client_key: str) -> int | None:
    """Insert a row in connect_landing_views, return its id (best-effort).

    main.get_db() hands out a pooled psycopg2 connection that is NOT
    autocommit — we must commit() before close() or the row is rolled
    back on connection return. We close the connection ourselves at the
    end so it returns to the pool cleanly.
    """
    db = _get_db()
    if db is None:
        return None
    new_id = None
    try:
        ua = (request.headers.get("User-Agent") or "")[:300]
        ref = (request.headers.get("Referer") or "")[:500]
        ip = (request.headers.get("CF-Connecting-IP")
              or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "")[:64]
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO connect_landing_views
                     (client, viewed_at, user_agent, referer, ip)
                   VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s, %s, %s)
                   RETURNING id""",
                (client_key, ua, ref, ip),
            )
            row = cur.fetchone()
            new_id = int(row[0]) if row else None
        db.commit()
    except Exception as e:
        logger.warning("connect_landing_views insert failed: %s", e)
        try: db.rollback()
        except Exception: pass
    finally:
        try: db.close()
        except Exception: pass
    return new_id


# ── Page render ─────────────────────────────────────────────────────────
_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub for {NAME} — 38 MCP tools, free tier, 30s to install</title>
<meta name="description" content="Install DC Hub's MCP server in {NAME} in 30 seconds. 38 tools across 21,000+ data centers, 232 power markets, live ISO grids, $324B+ in tracked deals. Free trial — no credit card.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/connect/{KEY}">
<meta property="og:title" content="DC Hub MCP for {NAME}">
<meta property="og:description" content="38 tools, 30 seconds to install, free trial — for AI agents that need real data center, grid, and infrastructure intelligence.">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-architecture.png">
<style>
 :root{{--bg:#0a0a0f;--card:#15151c;--border:#2a2a35;--text:#e8e8f0;--muted:#9a9aa6;--accent:#7c5cff;--accent2:#22d3ee;--ok:#10b981;--warn:#f59e0b}}
 *{{box-sizing:border-box}}
 body{{background:var(--bg);color:var(--text);font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;margin:0;padding:32px 20px}}
 .container{{max-width:880px;margin:0 auto}}
 .topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:32px;font-size:.85rem}}
 .topbar a{{color:var(--muted);text-decoration:none}}
 .topbar a:hover{{color:var(--text)}}
 .eyebrow{{color:var(--accent);font-size:.78rem;letter-spacing:.18em;text-transform:uppercase;font-weight:600;margin-bottom:8px}}
 h1{{font-size:2.4rem;margin:.1em 0;letter-spacing:-.02em;line-height:1.15}}
 .tagline{{color:var(--muted);font-size:1.12rem;max-width:680px;margin:8px 0 28px}}
 .badges{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:28px}}
 .badge{{background:var(--card);border:1px solid var(--border);padding:6px 12px;border-radius:999px;font-size:.78rem;color:var(--muted)}}
 .badge b{{color:var(--text);font-weight:600}}
 .card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;margin:18px 0}}
 .step{{display:flex;align-items:center;gap:10px;font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.12em;font-weight:600;margin-bottom:14px}}
 .step-num{{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;background:var(--accent);color:#fff;font-size:.72rem;font-weight:700;letter-spacing:0}}
 .mint-btn{{display:inline-flex;align-items:center;gap:10px;background:var(--accent);color:#fff;border:none;padding:14px 24px;border-radius:10px;font-size:1.04rem;font-weight:600;cursor:pointer;transition:transform .1s,box-shadow .1s}}
 .mint-btn:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(124,92,255,.35)}}
 .mint-btn[disabled]{{opacity:.7;cursor:not-allowed;transform:none}}
 .key-box{{display:none;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:14px;margin-top:14px;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.88rem;color:var(--accent2);word-break:break-all}}
 .key-box.shown{{display:block}}
 .key-meta{{display:flex;gap:14px;margin-top:8px;font-size:.78rem;color:var(--muted)}}
 .snippet-wrap{{position:relative}}
 pre{{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:18px;overflow-x:auto;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.86rem;line-height:1.55;color:#e8e8f0;margin:0}}
 pre .comment{{color:#6e7681}}
 .copy-btn{{position:absolute;top:10px;right:10px;background:var(--border);color:var(--text);border:none;padding:6px 12px;border-radius:6px;font-size:.78rem;cursor:pointer}}
 .copy-btn:hover{{background:var(--accent);color:#fff}}
 .copy-btn.copied{{background:var(--ok);color:#fff}}
 .install-meta{{font-size:.85rem;color:var(--muted);margin-bottom:12px}}
 .install-meta code{{background:var(--border);color:var(--accent2);padding:1px 6px;border-radius:3px;font-family:ui-monospace,monospace;font-size:.88em}}
 .examples{{display:grid;grid-template-columns:1fr;gap:10px;margin-top:12px}}
 .example{{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-size:.94rem;color:#cdd0db;font-style:italic}}
 .example::before{{content:"> ";color:var(--accent2);font-style:normal;font-weight:600}}
 .upgrade-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}}
 @media (max-width:560px){{.upgrade-grid{{grid-template-columns:1fr}}}}
 .upgrade-tile{{background:#0d1117;border:1px solid var(--border);border-radius:10px;padding:18px;text-align:left;text-decoration:none;color:var(--text);transition:border-color .15s,transform .1s}}
 .upgrade-tile:hover{{border-color:var(--accent);transform:translateY(-1px)}}
 .upgrade-tile h3{{margin:0 0 6px;font-size:1.08rem}}
 .upgrade-tile .price{{font-size:1.4rem;font-weight:700;color:var(--accent2);letter-spacing:-.01em}}
 .upgrade-tile .save{{display:inline-block;background:rgba(16,185,129,.18);color:var(--ok);font-size:.74rem;padding:2px 8px;border-radius:999px;margin-left:8px;font-weight:600}}
 .upgrade-tile .desc{{font-size:.84rem;color:var(--muted);margin-top:4px}}
 .footer{{margin-top:48px;padding-top:24px;border-top:1px solid var(--border);font-size:.82rem;color:var(--muted);display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}}
 .footer a{{color:var(--muted);text-decoration:none}}
 .footer a:hover{{color:var(--text)}}
 .deep-link{{display:inline-block;margin-left:14px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:8px;font-size:.86rem;text-decoration:none}}
 .deep-link:hover{{border-color:var(--accent);color:var(--accent)}}
 .key-actions{{display:flex;gap:8px;margin-top:10px}}
 .key-action-btn{{background:var(--border);color:var(--text);border:none;padding:6px 12px;border-radius:6px;font-size:.78rem;cursor:pointer;font-family:inherit}}
 .key-action-btn:hover{{background:var(--accent);color:#fff}}
 .upgrade-note{{font-size:.84rem;color:var(--muted);margin-top:10px}}
</style></head><body>
<div class="container">

<div class="topbar">
  <a href="https://dchub.cloud/">&larr; DC Hub</a>
  <span><a href="https://dchub.cloud/mcp">MCP docs</a> &middot; <a href="https://dchub.cloud/pricing">Pricing</a></span>
</div>

<div class="eyebrow">DC Hub MCP &middot; {NAME}</div>
<h1>DC Hub for {NAME}</h1>
<p class="tagline">{TAGLINE} &middot; 38 tools, free tier, 30 seconds to install.</p>

<div class="badges">
  <span class="badge"><b>21,000+</b> facilities</span>
  <span class="badge"><b>232</b> DCPI markets</span>
  <span class="badge"><b>43</b> US ISO/BA grids live</span>
  <span class="badge"><b>2,000+</b> M&amp;A deals</span>
  <span class="badge"><b>Free tier</b> &middot; no credit card</span>
</div>

<!-- STEP 1: Mint a trial key ─────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">1</span> Mint your free trial key</div>
  <p style="margin:0 0 14px;color:var(--muted);font-size:.95rem">
    One click. No email required. Key works immediately in {NAME}'s MCP config.
    Trial gives you <b style="color:var(--text)">50 requests/day for 7 days</b>.
  </p>
  <button id="mint-btn" class="mint-btn" onclick="mintKey()">
    Mint trial key &rarr;
  </button>
  <div id="key-box" class="key-box"></div>
  <div id="key-meta" class="key-meta" style="display:none"></div>
  <div id="key-actions" class="key-actions" style="display:none">
    <button class="key-action-btn" onclick="copyKey()">Copy key</button>
    <button class="key-action-btn" onclick="mintKey(true)">Mint another</button>
  </div>
</div>

<!-- STEP 2: Install snippet ──────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">2</span> Add to your {NAME} config</div>
  <div class="install-meta">
    Paste into <code>{INSTALL_PATH}</code> (macOS/Linux) &middot;
    <code>{INSTALL_PATH_WIN}</code> (Windows){DEEP_LINK_HTML}
  </div>
  <div class="snippet-wrap">
    <button class="copy-btn" id="copy-snippet" onclick="copySnippet()">Copy</button>
    <pre id="snippet-body">{SNIPPET_RENDERED}</pre>
  </div>
  <p style="margin:14px 0 0;color:var(--muted);font-size:.85rem">
    After saving, restart {NAME} and DC Hub will appear in the MCP connector list.
  </p>
</div>

<!-- STEP 3: Try-it prompts ──────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">3</span> Try these prompts in {NAME}</div>
  <div class="examples">
{EXAMPLES_HTML}
  </div>
</div>

<!-- STEP 4: Upgrade ─────────────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">4</span> Trial limits + upgrade</div>
  <p style="margin:0 0 6px;color:var(--muted);font-size:.95rem">
    Your trial gives <b style="color:var(--text)">50 requests/day for 7 days</b>.
    Need more? Upgrade to Pro — gets you unlimited daily quota, all 38 tools,
    and removes the free-tier truncation on grid + fiber intel.
  </p>
  <div class="upgrade-grid">
    <!-- r68-funnel-stamping (2026-06-06): both hrefs now point at the
         /api/v1/connect/click proxy which stamps connect_landing_views
         .stripe_clicked_at before bouncing to Stripe.  The href is
         re-set by mintKey() once a trial key exists so we can carry
         it through as client_reference_id. -->
    <a id="upg-monthly" class="upgrade-tile" href="/api/v1/connect/click?platform={KEY}&plan=pro_monthly&view_id={VIEW_ID}">
      <h3>Pro Monthly</h3>
      <div class="price">$199<span style="font-size:.7em;color:var(--muted)">/mo</span></div>
      <div class="desc">Cancel anytime. Same unlimited access, monthly billing.</div>
    </a>
    <a id="upg-annual" class="upgrade-tile" href="/api/v1/connect/click?platform={KEY}&plan=pro_annual&view_id={VIEW_ID}">
      <h3>Pro Annual <span class="save">50% off</span></h3>
      <div class="price">$1,188<span style="font-size:.7em;color:var(--muted)">/yr</span></div>
      <div class="desc">One-time payment, 365 days of Pro. Best value.</div>
    </a>
  </div>
  <p class="upgrade-note" id="ref-note" style="display:none">
    Upgrade links carry your trial key so the conversion attributes back to this page.
  </p>
</div>

<div class="footer">
  <span>&copy; DC Hub &middot; <a href="https://dchub.cloud/architecture">how it works</a></span>
  <span>
    <a href="https://dchub.cloud/connect/cursor">Cursor</a> &middot;
    <a href="https://dchub.cloud/connect/cline">Cline</a> &middot;
    <a href="https://dchub.cloud/connect/continue">Continue</a> &middot;
    <a href="https://dchub.cloud/connect/claude-desktop">Claude Desktop</a>
  </span>
</div>

</div><!-- /container -->

<script>
const CLIENT_KEY  = {CLIENT_KEY_JSON};
const VIEW_ID     = {VIEW_ID_JSON};
const RAW_SNIPPET = {SNIPPET_JSON};
const STRIPE_M    = {STRIPE_M_JSON};
const STRIPE_A    = {STRIPE_A_JSON};
let mintedKey = "";

async function mintKey(again) {{
  const btn = document.getElementById("mint-btn");
  btn.disabled = true;
  btn.innerText = again ? "Minting another..." : "Minting...";
  try {{
    const r = await fetch("/api/v1/keys/auto-mint?tool=connect_landing&platform=" + encodeURIComponent(CLIENT_KEY), {{
      method: "POST",
      headers: {{ "Content-Type": "application/json", "X-DC-Connect-Client": CLIENT_KEY }},
      body: JSON.stringify({{ client_name: CLIENT_KEY, intended_use: "connect_landing_page" }}),
    }});
    const j = await r.json();
    if (j && j.ok && j.api_key) {{
      mintedKey = j.api_key;
      document.getElementById("key-box").innerText = mintedKey;
      document.getElementById("key-box").classList.add("shown");
      const meta = document.getElementById("key-meta");
      meta.style.display = "flex";
      meta.innerText = (j.tier || "IDENTIFIED") + " tier ·  "
                     + (j.daily_calls || 50) + " req/day · "
                     + (j.trial_days || 7) + "-day trial";
      document.getElementById("key-actions").style.display = "flex";

      // Swap {{TRIAL_KEY}} in the install snippet
      const body = document.getElementById("snippet-body");
      body.innerText = RAW_SNIPPET.replaceAll("{{TRIAL_KEY}}", mintedKey);

      // Bind Stripe links via the /api/v1/connect/click proxy: it stamps
      // connect_landing_views.stripe_clicked_at BEFORE 302ing to Stripe so
      // the conversion-funnel dashboard can read viewed→clicked drop-off.
      // Carry the minted key as `key` query param → proxy threads it into
      // Stripe as client_reference_id (Fix-E attribution chain preserved).
      const viewQS = VIEW_ID ? ("&view_id=" + encodeURIComponent(VIEW_ID)) : "";
      const keyQS = "&key=" + encodeURIComponent(mintedKey);
      document.getElementById("upg-monthly").href =
        "/api/v1/connect/click?platform=" + encodeURIComponent(CLIENT_KEY) +
        "&plan=pro_monthly" + viewQS + keyQS;
      document.getElementById("upg-annual").href =
        "/api/v1/connect/click?platform=" + encodeURIComponent(CLIENT_KEY) +
        "&plan=pro_annual" + viewQS + keyQS;
      document.getElementById("ref-note").style.display = "block";

      btn.innerText = again ? "Mint another" : "Key minted -- paste in step 2 ↑";

      // Best-effort tell the backend which key this view minted
      if (VIEW_ID) {{
        try {{
          await fetch("/api/v1/connect/mint-update", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ view_id: VIEW_ID, api_key: mintedKey }}),
          }});
        }} catch(_) {{}}
      }}
    }} else {{
      btn.disabled = false;
      btn.innerText = "Mint failed — try again";
      console.error("mint failed:", j);
    }}
  }} catch(e) {{
    btn.disabled = false;
    btn.innerText = "Mint failed — try again";
    console.error(e);
  }}
}}

function copyKey() {{
  if (!mintedKey) return;
  navigator.clipboard.writeText(mintedKey);
  const btns = document.querySelectorAll("#key-actions .key-action-btn");
  if (btns[0]) {{ btns[0].innerText = "Copied"; setTimeout(() => btns[0].innerText = "Copy key", 1500); }}
}}

function copySnippet() {{
  const body = document.getElementById("snippet-body").innerText;
  navigator.clipboard.writeText(body);
  const btn = document.getElementById("copy-snippet");
  btn.classList.add("copied");
  btn.innerText = "Copied";
  setTimeout(() => {{
    btn.classList.remove("copied");
    btn.innerText = "Copy";
  }}, 1500);
}}
</script>
</body></html>
"""


def _render_page(client_key: str, view_id: int | None) -> str:
    c = _CLIENTS[client_key]
    # Render examples as <div class="example"> lines
    examples_html = "\n".join(
        f'    <div class="example">{e}</div>' for e in c["examples"]
    )
    deep_link_html = ""
    if c.get("deep_link"):
        deep_link_html = (
            f'<a class="deep-link" href="{c["deep_link"]}">{c.get("deep_link_label","Open in app")}</a>'
        )
    # The snippet body still has the literal {{TRIAL_KEY}} sentinel — JS
    # swaps it after the mint POST. Server-side we just escape any HTML
    # characters that would break <pre> rendering.
    snippet_rendered = (
        c["snippet"]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    # Note: we use .format(...) on a heredoc but the template includes JS
    # with `${...}` template literals — wait, no, we deliberately wrote it
    # with no template literals. CSS uses doubled-braces `{{` / `}}` to
    # escape under .format(). The JSON placeholders are inserted via
    # repr-safe json.dumps so they're always valid JS literals.
    return _PAGE_TEMPLATE.format(
        NAME=c["name"],
        KEY=client_key,
        TAGLINE=c["tagline"],
        INSTALL_PATH=c["install_path"],
        INSTALL_PATH_WIN=c["install_path_win"],
        SNIPPET_RENDERED=snippet_rendered,
        EXAMPLES_HTML=examples_html,
        DEEP_LINK_HTML=deep_link_html,
        STRIPE_MONTHLY=_STRIPE_MONTHLY,
        STRIPE_ANNUAL=_STRIPE_ANNUAL,
        VIEW_ID=(str(view_id) if view_id else ""),
        CLIENT_KEY_JSON=json.dumps(client_key),
        VIEW_ID_JSON=json.dumps(view_id),
        SNIPPET_JSON=json.dumps(c["snippet"]),
        STRIPE_M_JSON=json.dumps(_STRIPE_MONTHLY),
        STRIPE_A_JSON=json.dumps(_STRIPE_ANNUAL),
    )


# ── Routes ──────────────────────────────────────────────────────────────
def _serve(client_key: str):
    if client_key not in _CLIENTS:
        return jsonify(ok=False, error="unknown_client",
                       known=list(_CLIENTS.keys())), 404
    view_id = _record_view(client_key)
    html = _render_page(client_key, view_id)
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    # 5-minute edge cache — pages are mostly static, the dynamic bit
    # (mint button) is fetched live from /api/v1/keys/auto-mint.
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@mcp_connect_bp.route("/connect/cursor", methods=["GET"])
def connect_cursor():
    return _serve("cursor")


@mcp_connect_bp.route("/connect/cline", methods=["GET"])
def connect_cline():
    return _serve("cline")


@mcp_connect_bp.route("/connect/continue", methods=["GET"])
def connect_continue():
    return _serve("continue")


@mcp_connect_bp.route("/connect/claude-desktop", methods=["GET"])
def connect_claude_desktop():
    return _serve("claude-desktop")


# Best-effort "this view's key" hook the JS calls after a successful mint.
# Updates the connect_landing_views row with key_minted_for so the funnel
# can attribute trial keys -> page that minted them.
@mcp_connect_bp.route("/api/v1/connect/mint-update", methods=["POST"])
def mint_update():
    body = request.get_json(silent=True) or {}
    view_id = body.get("view_id")
    api_key = (body.get("api_key") or "").strip()
    if not view_id or not api_key:
        return jsonify(ok=False, error="missing_view_id_or_key"), 200
    db = _get_db()
    if db is None:
        return jsonify(ok=False, error="no_db"), 200
    try:
        with db.cursor() as cur:
            cur.execute(
                """UPDATE connect_landing_views
                       SET key_minted_for = %s,
                           key_minted_at  = NOW()
                     WHERE id = %s""",
                (api_key[:120], int(view_id)),
            )
        db.commit()
        return jsonify(ok=True), 200
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        return jsonify(ok=False, error="update_failed",
                       detail=str(e)[:160]), 200
    finally:
        try: db.close()
        except Exception: pass


# ── r68-funnel-stamping (2026-06-06): stripe-click proxy ────────────────
# The Connect landing pages had direct buy.stripe.com links. Result: 0
# stripe_clicked_at stamps in connect_landing_views even though some
# operators DID click through. Same root cause as the redeem-page leak:
# in-page JS beacons race the cross-origin navigation and lose.
#
# This proxy stamps the per-view row first, THEN 302s to Stripe with
# client_reference_id wired. View_id comes from a query param so the
# stamp lands on the row that minted the trial key (= the row the
# funnel-attribution joins on).
@mcp_connect_bp.route("/api/v1/connect/click", methods=["GET"])
def connect_click_proxy():
    """Stamp connect_landing_views.stripe_clicked_at then 302 to Stripe.

    Query params:
      platform — cursor|cline|continue|claude-desktop (audit only)
      plan     — pro_monthly | pro_annual  (which Stripe link to bounce to)
      view_id  — connect_landing_views.id (set by the page after _record_view)
      key      — minted trial key, used as client_reference_id for Stripe
                 attribution (Fix-E pattern, mirrors redeem-page click).
    """
    platform = (request.args.get("platform") or "").strip().lower()
    plan = (request.args.get("plan") or "pro_monthly").strip().lower()
    view_id = (request.args.get("view_id") or "").strip()
    key = (request.args.get("key") or "").strip()

    stripe_base = _STRIPE_ANNUAL if plan == "pro_annual" else _STRIPE_MONTHLY

    # Stamp first.  Best-effort: a failure here must NEVER block the
    # redirect (the user is mid-click on the Upgrade button — making them
    # see an error page for a telemetry blip would be a bigger leak than
    # the missing stamp). Wrap tight, log, redirect.
    if view_id:
        db = _get_db()
        if db is not None:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        """UPDATE connect_landing_views
                               SET stripe_clicked_at = COALESCE(stripe_clicked_at, NOW()),
                                   stripe_clicked_plan = COALESCE(stripe_clicked_plan, %s)
                             WHERE id = %s""",
                        (plan[:32], int(view_id)) if view_id.isdigit() else (plan[:32], -1),
                    )
                db.commit()
            except Exception as e:
                try: db.rollback()
                except Exception: pass
                # Pre-schema-repair DBs lack stripe_clicked_at/_plan →
                # retry just stamping the timestamp column (added second
                # below).  If both fail, log + redirect anyway.
                try:
                    with db.cursor() as cur:
                        cur.execute(
                            """UPDATE connect_landing_views
                                   SET stripe_clicked_at = COALESCE(stripe_clicked_at, NOW())
                                 WHERE id = %s""",
                            (int(view_id),) if view_id.isdigit() else (-1,),
                        )
                    db.commit()
                except Exception as e2:
                    try: db.rollback()
                    except Exception: pass
                    logger.warning(
                        "connect_click_proxy stamp failed platform=%s view_id=%s: %s",
                        platform, view_id, e2,
                    )
            finally:
                try: db.close()
                except Exception: pass

    # Carry the minted trial key forward to Stripe so the webhook can
    # attribute the conversion back to this page via the existing
    # users.api_key match path.
    sep = "&" if "?" in stripe_base else "?"
    if key:
        redirect_url = f"{stripe_base}{sep}client_reference_id={key}"
    else:
        redirect_url = stripe_base
    return redirect(redirect_url, code=302)


# Public admin/stats endpoint for the funnel dashboard.
@mcp_connect_bp.route("/api/v1/connect/stats", methods=["GET"])
def connect_stats():
    db = _get_db()
    if db is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT client,
                       COUNT(*)                              AS views,
                       COUNT(key_minted_for)                 AS keys_minted,
                       COUNT(DISTINCT ip)                    AS unique_ips,
                       MIN(viewed_at)                        AS first_view,
                       MAX(viewed_at)                        AS last_view
                  FROM connect_landing_views
                 WHERE viewed_at > NOW() - INTERVAL '30 days'
              GROUP BY client
              ORDER BY views DESC
            """)
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "client":      r[0],
                "views_30d":   int(r[1] or 0),
                "keys_minted": int(r[2] or 0),
                "unique_ips":  int(r[3] or 0),
                "first_view":  r[4].isoformat() if r[4] else None,
                "last_view":   r[5].isoformat() if r[5] else None,
                "mint_rate":   round((r[2] / r[1]) * 100, 2) if r[1] else 0.0,
            })
        return jsonify(ok=True, by_client=out, window="30d"), 200
    except Exception as e:
        return jsonify(ok=False, error="query_failed",
                       detail=str(e)[:200]), 500
    finally:
        try: db.close()
        except Exception: pass
