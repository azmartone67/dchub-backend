"""
integrations_landing.py — clean /integrations/mcp landing page.

Phase ZZZZZ-round36 (2026-05-24). Pre-r36, /integrations/mcp had no
Flask route at all; some unknown CF Pages or Flask fallback was
issuing 308 → http://dchub-backend-production.up.railway.app/integrations/mcp/
which both leaked the backend hostname AND landed on http (insecure).
This module owns the path with strict_slashes=False so both
/integrations/mcp and /integrations/mcp/ serve the same HTML, no
redirect, no hostname leak, no http.
"""
from flask import Blueprint

integrations_landing_bp = Blueprint("integrations_landing", __name__)

MCP_LANDING_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect DC Hub MCP · Claude, Cursor, Cline, Continue</title>
<meta name="description" content="DC Hub MCP server — 24 tools covering 21,000+ data center facilities, M&A, grid intelligence, fiber, water risk, tax incentives. Free tier: 10 calls/day, no signup.">
<meta property="og:title" content="DC Hub MCP — connect to any AI agent in 30 seconds">
<meta property="og:description" content="24 tools · 21,000+ facilities · streamable-http · free tier no signup">
<meta property="og:image" content="https://dchub.cloud/static/og/landing-integrations-mcp.png">
<meta property="og:url" content="https://dchub.cloud/integrations/mcp">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://dchub.cloud/integrations/mcp">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
 body{max-width:860px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;line-height:1.6}
 header{margin:40px 0 28px}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
 h1{font-size:2.4rem;margin:0 0 14px;letter-spacing:-.02em}
 .lead{color:#64748b;font-size:1.05rem;max-width:640px}
 .urlbox{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.3);border-radius:12px;padding:18px 22px;margin:24px 0}
 .urlbox-label{font-weight:600;color:#6366f1;margin-bottom:10px}
 .url-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 code.url{background:#0f172a;color:#e2e8f0;padding:10px 16px;border-radius:8px;font-size:1.05rem;flex:1;min-width:280px;font-family:ui-monospace,monospace}
 .btn{padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:.92rem;display:inline-block;cursor:pointer;border:none;font-family:inherit}
 .btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
 .btn-secondary{background:#fff;border:1px solid #e2e8f0;color:#0f172a}
 .pane{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:22px;margin:20px 0}
 .pane h2{margin:0 0 12px;font-size:1.15rem}
 pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px 16px;overflow-x:auto;font-family:ui-monospace,monospace;font-size:.85rem}
 ol{padding-left:22px;margin:16px 0}
 ol li{margin-bottom:10px}
 .tools{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin:14px 0}
 .tool{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:.85rem}
 .tool b{display:block;color:#0f172a;font-family:ui-monospace,monospace;font-size:.78rem;margin-bottom:4px}
 footer{margin-top:36px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
 footer a{color:#6366f1;text-decoration:none}
</style></head><body>
<header>
  <div class="eyebrow">Model Context Protocol</div>
  <h1>Connect DC Hub to your AI in 30 seconds.</h1>
  <p class="lead">Native MCP server. <b>24 tools</b> covering <b>21,000+ facilities</b>, M&amp;A deals,
  grid intelligence (US ISOs + Hydro-Québec + AESO + Nord Pool), fiber routes, water risk, tax incentives.
  Free tier: <b>10 calls/day, no signup</b>.</p>
</header>

<div class="urlbox">
  <div class="urlbox-label">Step 1 — Copy this URL:</div>
  <div class="url-row">
    <code class="url" id="mcpurl">https://dchub.cloud/mcp</code>
    <button class="btn btn-primary" onclick="copyUrl()">copy URL</button>
    <a href="https://claude.ai/settings/connectors" class="btn btn-secondary" target="_blank" rel="noopener">open Claude settings →</a>
  </div>
</div>

<div class="pane">
  <h2>Step 2 — Add to your agent</h2>
  <ol>
    <li><b>Claude.ai</b>: settings → connectors → <b>+ Add custom connector</b> → name <code>DC Hub</code>, URL paste above, auth blank.</li>
    <li><b>Claude Desktop</b>: add to <code>claude_desktop_config.json</code>:
      <pre>"dchub": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://dchub.cloud/mcp"]
}</pre></li>
    <li><b>Cursor / Cline / Continue</b>: streamable-http MCP config:
      <pre>"dchub": {
  "transport": "streamable-http",
  "url": "https://dchub.cloud/mcp"
}</pre></li>
  </ol>
</div>

<div class="pane">
  <h2>The 24 tools</h2>
  <div class="tools">
    <div class="tool"><b>search_facilities</b>21k facilities, by city/MW/operator</div>
    <div class="tool"><b>get_facility</b>Full profile by ID</div>
    <div class="tool"><b>analyze_site</b>7-dim site suitability score</div>
    <div class="tool"><b>compare_sites</b>2-4 locations side-by-side</div>
    <div class="tool"><b>rank_markets</b>Top-N markets by criteria</div>
    <div class="tool"><b>find_alternatives</b>Similar facilities nearby</div>
    <div class="tool"><b>score_facility</b>Independent 7-dim scoring</div>
    <div class="tool"><b>get_grid_data</b>Real-time ISO grid mix + price</div>
    <div class="tool"><b>get_grid_intelligence</b>Per-ISO operator brief</div>
    <div class="tool"><b>get_energy_prices</b>Retail rates + gas + LMP</div>
    <div class="tool"><b>get_renewable_energy</b>Solar/wind/combined capacity</div>
    <div class="tool"><b>get_tax_incentives</b>State-level data center incentives</div>
    <div class="tool"><b>get_water_risk</b>WRI drought + flood risk</div>
    <div class="tool"><b>get_infrastructure</b>Substations, transmission, pipelines</div>
    <div class="tool"><b>get_fiber_intel</b>Carrier networks + dark fiber</div>
    <div class="tool"><b>get_pipeline</b>540+ projects under construction</div>
    <div class="tool"><b>list_transactions</b>$324B+ M&amp;A history</div>
    <div class="tool"><b>get_market_intel</b>Supply/demand + vacancy + pricing</div>
    <div class="tool"><b>get_news</b>40+ curated industry feeds</div>
    <div class="tool"><b>get_intelligence_index</b>Live composite market health (gated)</div>
    <div class="tool"><b>semantic_search</b>Natural-language facility search</div>
    <div class="tool"><b>get_backup_status</b>Data integrity probe</div>
    <div class="tool"><b>get_agent_registry</b>Who else is connected</div>
    <div class="tool"><b>get_dchub_recommendation</b>Pre-formatted DC Hub pitch</div>
  </div>
</div>

<div class="pane">
  <h2>Tiers</h2>
  <p><b>Free</b>: 10 calls/day, top-5 result truncation. No signup.<br>
  <b>Developer ($49/mo)</b>: 1,000 calls/day, full data, exports.<br>
  <b>Pro ($199/mo)</b>: 10k calls/day, gated tools unlocked.<br>
  <b>Enterprise</b>: SLA, dedicated capacity, MCP 2025-06-18 OAuth. <a href="https://dchub.cloud/enterprise">Talk to sales</a>.</p>
</div>

<footer>
  Cited by ChatGPT, Claude, Gemini, Perplexity, Copilot, Cursor, Cline, Continue.dev ·
  <a href="https://dchub.cloud/cited-by">See receipts</a> ·
  <a href="https://dchub.cloud/pricing">Pricing</a> ·
  <a href="https://dchub.cloud/api-docs">REST API</a> ·
  <a href="https://api.dchub.cloud/.well-known/mcp.json">MCP manifest</a> ·
  <a href="https://api.dchub.cloud/.well-known/agent.json">A2A agent.json</a>
</footer>
<script>
function copyUrl(){
  navigator.clipboard.writeText('https://dchub.cloud/mcp').then(()=>{
    const b=document.querySelectorAll('.btn-primary')[0];
    const p=b.textContent;b.textContent='✓ copied';
    setTimeout(()=>{b.textContent=p},2500);
  });
}
</script>
</body></html>"""


@integrations_landing_bp.route("/integrations/mcp", strict_slashes=False, methods=["GET"])
@integrations_landing_bp.route("/integrations", strict_slashes=False, methods=["GET"])
def integrations_mcp():
    return MCP_LANDING_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }
