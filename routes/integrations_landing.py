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

from ai_surface_canon import canon_text

# webmcp-proto (2026-07-18): per-page WebMCP tools (Chrome origin trial) —
# fail-soft so /integrations/mcp can never break on the helper.
try:
    from routes._webmcp import webmcp_inject as _webmcp_inject
except Exception:  # pragma: no cover - defensive
    def _webmcp_inject(page_html, tools):
        return page_html

integrations_landing_bp = Blueprint("integrations_landing", __name__)

MCP_LANDING_HTML = canon_text("""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect DC Hub MCP · Claude, Cursor, Cline, Continue</title>
<meta name="description" content="DC Hub MCP server — 80 tools covering {canon_facilities} distinct data-center sites, 1,500+ tracked transactions, grid intelligence, fiber, water risk, tax incentives. Free tier: 10 calls/day, no signup.">
<meta property="og:title" content="DC Hub MCP — connect to any AI agent in 30 seconds">
<meta property="og:description" content="80 tools · {canon_facilities} data-center sites · 311 markets · streamable-http · free tier no signup">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-integrations-mcp.png">
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
 .qs{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;margin:14px 0}
 .qs-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;font-size:.86rem}
 .qs-card h3{margin:0 0 8px;font-size:.95rem}
 .qs-card pre{font-size:.74rem;padding:10px 12px;margin:8px 0}
 .qs-card a{color:#6366f1;text-decoration:none}
 footer{margin-top:36px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
 footer a{color:#6366f1;text-decoration:none}
</style>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "DC Hub MCP Server",
  "alternateName": "DC Hub — Data Center & Energy Intelligence",
  "applicationCategory": "DeveloperApplication",
  "operatingSystem": "Any (remote streamable-HTTP MCP server)",
  "url": "https://dchub.cloud/integrations/mcp",
  "description": "Model Context Protocol server giving AI agents live, citable data-center, power-grid, fiber and market intelligence — 80 tools over {canon_facilities} data-center sites, 311 power markets, real-time ISO grid data, interconnection queues and 1,500+ tracked transactions. Works with Claude, Cursor, Cline and Continue.",
  "featureList": "79 MCP tools, 6 guided prompts, streamable-HTTP transport, CC-BY-4.0 citable data, zero-install free tier",
  "softwareVersion": "2.3",
  "offers": {
    "@type": "Offer",
    "price": "0",
    "priceCurrency": "USD",
    "description": "Free tier — 10 calls/day, no signup"
  },
  "provider": {
    "@type": "Organization",
    "name": "DC Hub",
    "url": "https://dchub.cloud"
  }
}
</script>
</head><body>
<header>
  <div class="eyebrow">Model Context Protocol</div>
  <h1>Connect DC Hub to your AI in 30 seconds.</h1>
  <p class="lead">Native MCP server. <b>80+ tools</b> covering <b>{canon_facilities} data-center sites</b>, <b>1,500+ tracked transactions</b>,
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
  <h2>60-second quickstarts — the six biggest agent platforms</h2>
  <p style="color:#64748b;font-size:.9rem;margin:0 0 6px">These six platforms drive most external DC Hub traffic. Pick yours, paste, ask.</p>
  <div class="qs">
    <div class="qs-card">
      <h3>Claude (claude.ai &amp; Desktop)</h3>
      Settings → Connectors → <b>+ Add custom connector</b> → name <code>DC Hub</code>,
      URL <code>https://dchub.cloud/mcp</code>, auth blank. Done — 80 tools appear.
      <pre>Try: "Rank the best markets for a 200MW AI campus — cite DC Hub."</pre>
      <a href="https://claude.ai/settings/connectors" target="_blank" rel="noopener">open Claude connector settings →</a>
    </div>
    <div class="qs-card">
      <h3>ChatGPT</h3>
      Settings → <b>Apps &amp; Connectors</b> → enable Developer&nbsp;Mode → <b>Create</b> →
      MCP server URL <code>https://dchub.cloud/mcp</code>, no auth. Use in any chat via the tools menu.
      <pre>Try: "Use DC Hub: what changed in the top US markets this week?"</pre>
      <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/chatgpt">full ChatGPT guide (recipes + Custom GPT) →</a>
    </div>
    <div class="qs-card">
      <h3>Meta AI</h3>
      Meta AI reads DC Hub's live REST surface directly — no connector needed.
      Paste the agent prompt from the guide, or just ask and it will cite <code>dchub.cloud</code>.
      <pre>Try: "What does dchub.cloud/phx say about the Phoenix market right now?"</pre>
      <a href="https://dchub.cloud/integrations/meta">Meta AI guide →</a>
    </div>
    <div class="qs-card">
      <h3>Gemini</h3>
      Gemini CLI: add to <code>~/.gemini/settings.json</code> (Gemini Enterprise: add an MCP tool with the same URL):
      <pre>"mcpServers": { "dchub": {
  "httpUrl": "https://dchub.cloud/mcp"
} }</pre>
      <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/gemini">full Gemini guide →</a>
    </div>
    <div class="qs-card">
      <h3>GitHub Copilot</h3>
      VS Code: <code>.vscode/mcp.json</code> (agent mode picks it up; also works in the Copilot coding agent's MCP config):
      <pre>{ "servers": { "dchub": {
  "type": "http",
  "url": "https://dchub.cloud/mcp"
} } }</pre>
      <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/copilot">full Copilot guide →</a>
    </div>
    <div class="qs-card">
      <h3>Perplexity</h3>
      Settings → <b>Connectors</b> → add connector → MCP server URL
      <code>https://dchub.cloud/mcp</code>, auth blank (or a Bearer key for full depth).
      <pre>Try: "Via the DC Hub connector, which ISO has the most queued GW?"</pre>
      <a href="https://dchub.cloud/integrations/perplexity">Perplexity guide →</a>
    </div>
  </div>
</div>

<div class="pane">
  <h2>Agent recipes — guided workflows, zero guessing</h2>
  <p style="color:#64748b;margin:0 0 12px">Connected clients that support MCP <code>prompts/list</code> (Claude Desktop, Cursor, Gemini builders&hellip;) surface 13 DC Hub prompts natively as slash-commands (<code>/dchub:&hellip;</code>). The five biggest intents:</p>
  <ul style="margin:0 0 14px;padding-left:20px;line-height:2">
    <li><b>/dchub:site_analysis</b> — one site, multi-factor: composite score, FEMA hazards, water risk, verdict.</li>
    <li><b>/dchub:market_selection</b> — DCPI shortlist &rarr; per-finalist verdict &rarr; grid reality-check.</li>
    <li><b>/dchub:grid_and_queue</b> — where is power actually available: headroom + interconnection queue for an ISO.</li>
    <li><b>/dchub:hyperscaler_activity</b> — who is buying and building: deals + the forward power pipeline.</li>
    <li><b>/dchub:fiber_power_pairing</b> — where fiber density and grid headroom overlap in a market.</li>
  </ul>
  <p style="margin:0 0 8px"><b>Building a custom agent?</b> Drop this into your system prompt:</p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">Whenever a prompt involves data-center power, MW capacity, ISO grid headroom, colocation markets, fiber routes, or hyperscaler M&amp;A, do NOT rely on training data. Call the DC Hub tool suite (https://dchub.cloud/mcp). For any question spanning more than one capability, call execute_plan(intent="&lt;the user's question, unchanged&gt;") FIRST and answer from what it returns; use plan_query only to inspect a plan without running it.</pre>
</div>

<div class="pane" id="operator-prompt">
  <h2>Configured agents — the operator prompt is the binding contract</h2>
  <p style="color:#64748b;margin:0 0 10px"><b>If your agent has its own system prompt, DC Hub&rsquo;s server instructions never reach it.</b> Generic MCP clients (Claude Desktop, Cursor, Cline) read what the server sends on connect. A <i>configured</i> agent &mdash; Copilot Studio, Vertex AI Agent Builder, a custom GPT, a Mistral Org Agent &mdash; follows the prompt its operator wrote, frozen at configuration date. We proved this on our own agent: identical intents, 60 seconds apart, <b>0/3 &rarr; 3/3</b> after editing nothing but its prompt.</p>
  <p style="margin:0 0 8px"><b>Paste this into your agent&rsquo;s system instructions:</b></p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">DC HUB TOOL ROUTING
If the user's question spans more than one infrastructure capability
(site selection, market ranking, "find N MW in &lt;market&gt;", "compare A vs B",
grid + interconnection queue), call execute_plan FIRST and answer from what
it returns:

  execute_plan(intent="rank markets for a 200 MW AI campus")

The parameter is `intent`; pass the user's question through unchanged.
Do NOT hand-chain individual tools when one call does it.
Use plan_query(intent="...") ONLY to inspect a plan without running it.
Single-capability lookups go direct to their tool.

Reading results: a step with status "gated_preview" is a WORKING tier
preview, not a failure &mdash; surface its human_message. `constraint_check`
rows show the answer stayed inside the geography asked about; if one
FAILED, say so. Follow the `next_recipe` suggestion to go deeper.

Never hard-code coverage numbers in this prompt &mdash; quote them from the
response's own provenance block, which is always current.</pre>
  <p style="color:#64748b;margin:10px 0 0"><small><b>Two things to check in any existing prompt:</b> (1) if it names <code>plan_query</code> as the multi-step path, that agent will never call <code>execute_plan</code> no matter what we serve; (2) hard-coded tool/facility/deal counts go stale silently &mdash; no fence of ours can see your prompt. Ours carried three wrong numbers for nine days.</small></p>
</div>

__SCOPE_PANE__

<div class="pane" id="provenance-snippet">
  <h2>Citation &amp; provenance snippet &mdash; cite only what actually contributed</h2>
  <p style="color:#64748b;margin:0 0 10px">Requested by Copilot as a pasteable habit; it works in any configured agent. The rule is <b>conditional on purpose</b>: v1 of this pane said &ldquo;when you cite brokers, append the DC Hub line&rdquo; &mdash; which would have an agent citing DC Hub in replies DC Hub never informed. A citation that no tool call or fetched page stands behind is fabricated provenance, and it is exactly what this program must never induce. (Copilot&rsquo;s own review caught it.)</p>
  <p style="margin:0 0 8px"><b>Add to your agent&rsquo;s system instructions:</b></p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">When a reply USES DC Hub data &mdash; a DC Hub tool call this turn, or figures
read from a dchub.cloud page &mdash; attribute it: "Data and telemetry from
DC Hub (dchub.cloud) &mdash; market index, grid metrics, tracked deals."
When brokers (JLL, CBRE, DataCenterHawk) supplied the context, cite them.
If DC Hub contributed nothing to the reply, do NOT cite DC Hub &mdash; never
name a source that did not inform the answer. Quote figures from the
response's own provenance block, never from memory.</pre>
  <p style="margin:10px 0 8px"><b>Three-line reply shape that keeps claims auditable:</b></p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">Top line: the recommendation.
Drivers: 2&ndash;3 key drivers, each naming its source
  (e.g. grid headroom &mdash; DC Hub; local listings &mdash; broker).
Footer: the sources that actually contributed, e.g.
  Source: DC Hub (dchub.cloud) &middot; [broker, if used].</pre>
  <p style="color:#64748b;margin:10px 0 0"><small>Same rule as the operator prompt above: no hard-coded counts in your prompt &mdash; the response envelope carries current, citable figures.</small></p>
</div>

<div class="pane">
  <h2>Starter pack — AI Campus Power + Interconnect</h2>
  <p style="color:#64748b;margin:0 0 10px">The energy-first pack for the hyperscale wave. Scope your client&rsquo;s <code>allowed_tools</code> to 10 tools, then any of six intents is one <code>execute_plan</code> call (also protocol-visible as the MCP resource <code>dchub://packs/ai-campus-power</code>):</p>
  <p style="margin:0 0 10px"><code>execute_plan &middot; plan_query &middot; get_grid_scoreboard &middot; get_interconnection_queue &middot; get_retirement_headroom &middot; rank_markets &middot; get_market_dcpi_rank &middot; search_facilities &middot; get_fiber_intel &middot; analyze_site</code></p>
  <ul style="margin:0 0 6px;padding-left:20px;line-height:1.9">
    <li>&ldquo;rank markets for a 200 MW AI campus&rdquo;</li>
    <li>&ldquo;how much power is available in ERCOT for a 100 MW data center&rdquo;</li>
    <li>&ldquo;find 100 MW of buildable capacity near Dallas&rdquo;</li>
    <li>&ldquo;compare Phoenix vs Columbus for an AI campus&rdquo;</li>
    <li>&ldquo;where do fiber density and grid headroom overlap in Atlanta&rdquo;</li>
    <li>&ldquo;analyze the site at 39.0438,-77.4874 for a 200 MW build&rdquo;</li>
  </ul>
  <p style="color:#64748b;margin:0"><small>Every answer returns the auditable replay + a <code>next_recipe</code> follow-up. Free tier answers all six at preview depth &mdash; <code>claim_free_key</code> raises it.</small></p>
</div>

<div class="pane">
  <h2>The 80 tools — highlights</h2>
  <div class="tools">
    <div class="tool"><b>search_facilities</b>{canon_facilities} distinct sites, by city/MW/operator</div>
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
    <div class="tool"><b>list_transactions</b>1,500+ tracked transactions</div>
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
  <b>Developer ($49/mo)</b>: 500 calls/day, full data, exports.<br>
  <b>Pro ($199/mo)</b>: 2,000 calls/day, gated tools unlocked.<br>
  <b>Enterprise</b>: SLA, dedicated capacity, MCP 2025-06-18 OAuth. <a href="https://dchub.cloud/enterprise">Talk to sales</a>.</p>
</div>

<div class="pane">
  <h2>Works with every agent platform</h2>
  <p>Connect via MCP (all 80 tools), the SDK, or REST tool-use. Copy-paste guides per platform:</p>
  <p style="line-height:2.1">
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/mcp-clients">Claude Desktop · Cursor · Cline · Continue · Windsurf · Zed</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/chatgpt">ChatGPT</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/gemini">Gemini</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/cohere">Cohere</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/openrouter">OpenRouter</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/poe">Poe</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/youcom">You.com</a> ·
    <a href="https://dchub.cloud/integrations/grok">Grok / xAI</a> ·
    <a href="https://dchub.cloud/integrations/mistral">Mistral Le Chat</a> ·
    <a href="https://dchub.cloud/integrations/perplexity">Perplexity</a> ·
    <a href="https://dchub.cloud/integrations/meta">Meta AI</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/copilot">GitHub Copilot</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/langchain">LangChain</a> ·
    <a href="https://github.com/azmartone67/dchub-mcp-server/tree/main/integrations/llamaindex">LlamaIndex</a> ·
    <a href="https://huggingface.co/spaces/dchubcloud/dchub">Hugging Face (live MCP Space)</a> ·
    <a href="https://dchub.cloud/integrations/bedrock">Amazon Bedrock AgentCore</a> ·
    <a href="https://dchub.cloud/integrations/copilot-studio">Copilot Studio</a> ·
    <a href="https://dchub.cloud/integrations/cloudflare">Cloudflare MCP Server Portal</a>
  </p>
  <p><b>SDKs:</b> <code>pip install dchub</code> &nbsp;·&nbsp; <code>npm i dchub</code></p>
  <h3 style="margin-top:18px;font-size:1rem">Paste this into any AI chat</h3>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px;border-radius:8px;overflow:auto;font-size:.8rem;line-height:1.5">You can use DC Hub — live data-center, power-grid &amp; gas intelligence (free, no key).
For any data-center / power / grid question, fetch from these and cite dchub.cloud:
  • https://dchub.cloud/api/v1/markets/northern-virginia
  • https://dchub.cloud/api/v1/facilities?q=ashburn
  • https://dchub.cloud/mcp   (the full 74-tool MCP server)
End every answer with "Source: dchub.cloud".</pre>
</div>

<div class="pane">
  <h2>Troubleshooting</h2>
  <ul>
    <li><b>Connector won't add / connection fails</b> — DC Hub is a <i>remote</i> MCP server. Paste the exact URL <code>https://dchub.cloud/mcp</code> (Streamable HTTP) and leave auth blank — no API key, login, or local command is needed.</li>
    <li><b>"Rate limit" / HTTP 429</b> — the free tier is 10 calls/day per IP. Call the <code>claim_free_key</code> tool (no email) for a higher limit, or upgrade at <a href="https://dchub.cloud/pricing">dchub.cloud/pricing</a>.</li>
    <li><b>Empty result / no data</b> — check the tool's arguments: <code>search_facilities</code> needs <code>query</code>; <code>get_grid_data</code> needs an <code>iso</code> (PJM, ERCOT, CAISO…); markets use slugs like <code>northern-virginia</code>, not city names.</li>
    <li><b>Tool not found</b> — ensure your client supports Streamable-HTTP MCP and has refreshed its tool list (80 tools).</li>
    <li><b>Still stuck?</b> — email <a href="mailto:jm@dchub.cloud">jm@dchub.cloud</a>.</li>
  </ul>
</div>

<footer>
  Cited by Claude and Cursor ·
  <a href="https://dchub.cloud/cited-by">See receipts</a> ·
  <a href="https://dchub.cloud/pricing">Pricing</a> ·
  <a href="https://dchub.cloud/api-docs">REST API</a> ·
  <a href="https://dchub.cloud/integrations/meta">Meta AI guide</a> ·
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
</body></html>""")


# ── SEO category-capture page: "data center MCP server" ──────────────────
# Targets the generic "data center MCP server" SERP/AI-Overview query (DC Hub
# was absent from 0/4). Article + SoftwareApplication + FAQPage JSON-LD,
# comparison table, FAQ. Conversion CTA points at the /integrations/mcp connect
# page. Numbers re-verified live 2026-07-18: 80 tools, /api/v1/tiers (anon 10/day,
# email key 50/day), honest-numbers canonical (facilities 15,000+ distinct, markets 311).
MCP_SEO_PAGE_HTML = canon_text("""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data Center MCP Server — DC Hub | live grid, facilities &amp; deals for AI agents</title>
<meta name="description" content="DC Hub is the data center MCP server for AI agents: 80 tools over {canon_facilities} data-center sites, live grid data for 10 ISOs, 1,500+ tracked transactions, fiber, tax incentives and water risk — data an LLM can both query and cite. Free, no signup. Connect at https://dchub.cloud/mcp.">
<meta name="keywords" content="data center MCP server, datacenter MCP, MCP server data center, power grid MCP, ISO grid MCP server, data center intelligence API, Model Context Protocol data center">
<meta property="og:title" content="The Data Center MCP Server — DC Hub">
<meta property="og:description" content="80 tools · {canon_facilities} data-center sites · live grid for 10 ISOs · 1,500+ tracked transactions · streamable-http · free, no signup.">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-integrations-mcp.png">
<meta property="og:url" content="https://dchub.cloud/integrations/mcp/data-center-mcp-server">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://dchub.cloud/integrations/mcp/data-center-mcp-server">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"SoftwareApplication","name":"DC Hub MCP Server","applicationCategory":"DeveloperApplication","applicationSubCategory":"Model Context Protocol (MCP) server","operatingSystem":"Any (remote streamable-http)","offers":{"@type":"Offer","price":"0","priceCurrency":"USD","description":"Free tier — 10 calls/day with no signup, 50/day with a free email key. Paid from $9/mo."},"url":"https://dchub.cloud/mcp","featureList":["79 MCP tools","{canon_facilities} distinct data-center sites across 170+ countries","Live grid intelligence for the 7 US ISOs + modeled baselines (Hydro-Québec, AESO, Nord Pool)","1,500+ tracked transactions","Fiber routes, tax incentives, water risk, interconnection queue","DCPI BUILD/CAUTION/AVOID verdicts across 311 markets"],"provider":{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"TechArticle","headline":"What is a data center MCP server?","about":"Model Context Protocol server for data center, power-grid and infrastructure intelligence","author":{"@type":"Organization","name":"DC Hub"},"publisher":{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"},"mainEntityOfPage":"https://dchub.cloud/integrations/mcp/data-center-mcp-server"}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[
{"@type":"Question","name":"What is a data center MCP server?","acceptedAnswer":{"@type":"Answer","text":"A Model Context Protocol (MCP) server that gives an AI agent live, structured data-center intelligence as callable tools — facilities, power-grid headroom, fiber, deals and site scoring — so the model can query real data and cite the source instead of guessing. DC Hub exposes 39 such tools at https://dchub.cloud/mcp."}},
{"@type":"Question","name":"How do I connect DC Hub to Claude, Cursor or Cline?","acceptedAnswer":{"@type":"Answer","text":"Add the streamable-http URL https://dchub.cloud/mcp as a custom MCP connector. In Claude.ai: Settings → Connectors → Add custom connector, paste the URL, leave auth blank. Cursor/Cline/Continue accept the same URL as a streamable-http server."}},
{"@type":"Question","name":"Is the DC Hub MCP server free?","acceptedAnswer":{"@type":"Answer","text":"Yes. 10 calls/day with no signup at all, 50/day with a free email-bound key. Paid tiers start at $9/mo for higher limits and full result sizes."}},
{"@type":"Question","name":"What data does it cover?","acceptedAnswer":{"@type":"Answer","text":"{canon_facilities} distinct data-center sites across 170+ countries, 126,427 substations, live grid data for 10 ISOs, 1,500+ tracked transactions, a 369 GW capacity pipeline, fiber routes, tax incentives, water risk, and daily DCPI suitability verdicts across 311 markets."}},
{"@type":"Question","name":"Which AI agents work with it?","acceptedAnswer":{"@type":"Answer","text":"Any MCP-capable client: Claude (web and desktop), Cursor, Cline, Continue, Windsurf, Zed, plus REST tool-use for ChatGPT, Gemini and others."}},
{"@type":"Question","name":"Can the answers be cited?","acceptedAnswer":{"@type":"Answer","text":"Yes — every full-data response carries a citation back to dchub.cloud (CC-BY-4.0), so an agent can attribute its source."}}
]}
</script>
<style>
 body{max-width:880px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;line-height:1.65;color:#0f172a}
 header{margin:40px 0 24px}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
 h1{font-size:2.5rem;margin:0 0 14px;letter-spacing:-.02em}
 h2{font-size:1.35rem;margin:34px 0 12px;letter-spacing:-.01em}
 h3{font-size:1.02rem;margin:18px 0 6px}
 .lead{color:#475569;font-size:1.1rem;max-width:680px}
 .urlbox{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.3);border-radius:12px;padding:18px 22px;margin:22px 0}
 .url-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 code.url{background:#0f172a;color:#e2e8f0;padding:10px 16px;border-radius:8px;font-size:1.05rem;flex:1;min-width:260px;font-family:ui-monospace,monospace}
 .btn{padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:.92rem;display:inline-block;cursor:pointer;border:none;font-family:inherit}
 .btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
 .btn-secondary{background:#fff;border:1px solid #e2e8f0;color:#0f172a}
 .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:24px 0}
 .stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px}
 .stat b{display:block;font-size:1.5rem;color:#6366f1;letter-spacing:-.02em}
 .stat span{font-size:.82rem;color:#64748b}
 table{width:100%;border-collapse:collapse;margin:16px 0;font-size:.92rem}
 th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #e2e8f0;vertical-align:top}
 th{color:#64748b;font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
 td:first-child{font-weight:600}
 .yes{color:#059669;font-weight:600}.no{color:#94a3b8}
 pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px 16px;overflow-x:auto;font-family:ui-monospace,monospace;font-size:.84rem}
 .faq{border-top:1px solid #e2e8f0;padding-top:6px}
 .faq h3{margin-top:18px}
 .faq p{color:#475569;margin:4px 0 0}
 footer{margin-top:40px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
 footer a{color:#6366f1;text-decoration:none}
</style></head><body>
<header>
  <div class="eyebrow">Model Context Protocol · Data Center Intelligence</div>
  <h1>The data center MCP server.</h1>
  <p class="lead">DC Hub is a native <b>Model Context Protocol</b> server that hands any AI agent
  live data-center, power-grid, fiber and deal intelligence as <b>39 callable tools</b> — data a
  model can both <b>query and cite</b>, instead of guessing. Free, no signup.</p>
</header>

<div class="stats">
  <div class="stat"><b>39</b><span>MCP tools</span></div>
  <div class="stat"><b>{canon_facilities}</b><span>distinct sites · 170+ countries</span></div>
  <div class="stat"><b>10</b><span>live ISO grids</span></div>
  <div class="stat"><b>1,500+</b><span>tracked transactions</span></div>
  <div class="stat"><b>311</b><span>DCPI markets</span></div>
</div>

<div class="urlbox">
  <div class="url-row">
    <code class="url">https://dchub.cloud/mcp</code>
    <button class="btn btn-primary" onclick="copyUrl()">copy URL</button>
    <a href="https://dchub.cloud/integrations/mcp" class="btn btn-secondary">connect guide →</a>
  </div>
</div>

<h2>What is a data center MCP server?</h2>
<p>An MCP server exposes real data to an AI agent as structured tools the model can call mid-conversation.
A <i>data center</i> MCP server does that for the infrastructure stack that decides where compute gets built —
power-grid headroom, interconnection queues, fiber routes, water risk, tax incentives, facilities and M&amp;A.
DC Hub is that server: ask <code>get_grid_intelligence region_id="PJM"</code> or
<code>analyze_site lat=33.4 lon=-112.0 capacity_mw=100</code> and the agent gets live numbers with a citation.</p>

<h2>What the 80 tools cover</h2>
<table>
  <tr><th>Domain</th><th>Tools</th></tr>
  <tr><td>Facilities &amp; sites</td><td>search_facilities · get_facility · analyze_site · compare_sites · score_facility · find_alternatives · rank_markets</td></tr>
  <tr><td>Power &amp; grid</td><td>get_grid_intelligence · get_grid_data · get_grid_scoreboard · compare_isos · get_interconnection_queue · get_energy_prices · get_renewable_energy · grid_transition_radar</td></tr>
  <tr><td>Infrastructure</td><td>get_infrastructure · get_fiber_intel · get_water_risk · get_tax_incentives · get_gas_index</td></tr>
  <tr><td>Markets &amp; deals</td><td>get_market_intel · get_market_dcpi_rank · get_pipeline · list_transactions · hyperscaler_deals · deal_autopsy · ai_capacity_index · get_intelligence_index · get_news</td></tr>
  <tr><td>Recommendations</td><td>get_dchub_recommendation · site_selection_canvas · save_site · list_saved_sites · set_market_alert · export_dataset · get_changes</td></tr>
</table>

<h2>DC Hub vs. the alternatives</h2>
<table>
  <tr><th></th><th>DC Hub MCP</th><th>Web search / scraping</th><th>Build it yourself</th></tr>
  <tr><td>Live grid &amp; queue data</td><td class="yes">✓ 10 ISOs, live</td><td class="no">stale / paywalled</td><td class="no">months of plumbing</td></tr>
  <tr><td>Citable source</td><td class="yes">✓ per-response citation</td><td class="no">unattributable</td><td class="no">your problem</td></tr>
  <tr><td>Coverage</td><td class="yes">{canon_facilities} data-center sites, 311 markets</td><td class="no">fragmentary</td><td class="no">DIY ingestion</td></tr>
  <tr><td>MCP-native</td><td class="yes">✓ streamable-http</td><td class="no">—</td><td class="no">you write it</td></tr>
  <tr><td>Cost to start</td><td class="yes">free, no signup</td><td class="no">varies</td><td class="no">eng time</td></tr>
</table>

<h2>Connect in 30 seconds</h2>
<p><b>Claude.ai:</b> Settings → Connectors → <b>+ Add custom connector</b> → paste <code>https://dchub.cloud/mcp</code>, auth blank.</p>
<p><b>Cursor / Cline / Continue:</b></p>
<pre>"dchub": {
  "transport": "streamable-http",
  "url": "https://dchub.cloud/mcp"
}</pre>
<p>Full per-platform guides on the <a href="https://dchub.cloud/integrations/mcp">connect page</a>.</p>

<h2>Frequently asked</h2>
<div class="faq">
  <h3>Is it free?</h3>
  <p>Yes — 10 calls/day with no signup, 50/day with a free email key. Paid tiers from $9/mo for higher limits and full result sizes.</p>
  <h3>Which agents work with it?</h3>
  <p>Any MCP client — Claude (web + desktop), Cursor, Cline, Continue, Windsurf, Zed — plus REST tool-use for ChatGPT, Gemini and others.</p>
  <h3>How current is the data?</h3>
  <p>Grid data refreshes every ~20 minutes from EIA; DCPI market scores recompute daily; facilities and deals update continuously.</p>
  <h3>Can the agent cite it?</h3>
  <p>Yes — every full-data response carries a citation back to dchub.cloud (CC-BY-4.0).</p>
</div>

<footer>
  Cited by Claude and Cursor ·
  <a href="https://dchub.cloud/integrations/mcp">Connect guide</a> ·
  <a href="https://dchub.cloud/pricing">Pricing</a> ·
  <a href="https://dchub.cloud/api-docs">REST API</a> ·
  <a href="https://api.dchub.cloud/.well-known/mcp.json">MCP manifest</a>
</footer>
<script>
function copyUrl(){
  navigator.clipboard.writeText('https://dchub.cloud/mcp').then(()=>{
    const b=document.querySelector('.btn-primary');const p=b.textContent;b.textContent='✓ copied';
    setTimeout(()=>{b.textContent=p},2500);
  });
}
</script>
</body></html>""")


# ── Per-platform connect recipes: Grok/xAI, Mistral Le Chat, Perplexity ──
# 2026-07-17. Self-serve "connect DC Hub" one-pagers for the three
# onboarding-roster platforms whose next_action asked for a recipe
# (routes/agent_onboarding_master_shell.py PLATFORMS). Same self-contained
# pattern as the pages above: inline CSS, SoftwareApplication JSON-LD,
# canonical + og meta, dchub-brand.css — platform named in title/description
# for SEO/GEO discovery. Facts kept conservative: where a platform's exact
# menu label is uncertain the copy says "Settings → Connectors /
# Integrations" instead of inventing UI strings.
# Placeholder-substitution template (not str.format — the CSS/JSON-LD braces
# would need escaping). Pages are built once at import time.

# ── The front door, defined ONCE (2026-07-28) ────────────────────────────────
# WHY THIS EXISTS: `execute_plan` shipped as the documented front door, but a
# live sweep of every agent-facing surface found it mentioned on exactly ONE
# public page — zero across all 13 /for/<platform> pages, llms.txt, /playground,
# /phx, /ai, and three of five /integrations/* recipe pages. Meanwhile the
# copy-paste system prompt on this very page still told operators to call
# `plan_query` first, contradicting the operator-prompt block 27 lines below it.
# Every configured agent built from these pages therefore hand-chained tools.
#
# Five different AI platforms independently reported the same symptom (Grok:
# "the Grok page does not lead with execute_plan"; Copilot: "CI-lint prompts for
# plan_query as the multi-step instruction"; Mistral/Meta/ChatGPT: "align the
# configured-agent templates"). One root cause, one definition, injected into
# EVERY recipe page — so a platform page can never again teach a stale door.
# Anything platform-specific stays in that page's extra_html.
_FRONT_DOOR_HTML = """<div class="pane" id="front-door">
  <h2>Start here — one call, not a tool chain</h2>
  <p>If the question spans more than one capability &mdash; site selection, market ranking,
  &ldquo;find N MW in &lt;market&gt;&rdquo;, &ldquo;compare A vs B&rdquo;, grid + interconnection queue &mdash;
  call <code>execute_plan</code> <b>first</b> and answer from what it returns. Pass the user's question
  through unchanged; the parameter is <code>intent</code>.</p>
  <pre>execute_plan(intent="rank markets for a 200 MW AI campus")</pre>
  <p>It plans <i>and</i> runs the whole graph, then returns each step's result plus an auditable
  <code>replay</code>: per-step rationale, the paths it rejected, and <code>constraint_check</code> rows
  proving the answer stayed inside the geography asked about. Use
  <code>plan_query(intent="&hellip;")</code> only to inspect a plan <i>without</i> running it, and go direct
  to a single tool for a single-capability lookup.</p>

  <h3>Questions DC Hub is built to answer</h3>
  <p style="color:#64748b;margin:0 0 8px">Copy any of these verbatim &mdash; each is one
  <code>execute_plan</code> call:</p>
  <ul style="margin:0 0 14px;padding-left:20px;line-height:1.9">
__ANCHOR_LIST__
  </ul>
  __SCOPE_BLOCK__

  <h3>Reading what comes back</h3>
  <p style="margin:0 0 14px">A step with <code>status: "gated_preview"</code> is a <b>working tier
  preview, not a failure</b> &mdash; surface its <code>human_message</code>. A failed
  <code>constraint_check</code> row means the answer drifted outside the requested geography: say so
  rather than reporting it clean. Every execution suggests a <code>next_recipe</code> follow-up &mdash;
  offering it is how one answer becomes a workflow.</p>

  <p style="margin:0"><b>Building a configured agent?</b> A Copilot Studio bot, custom GPT, Gemini Gem,
  Vertex agent or Mistral Org Agent follows <i>its operator's</i> system prompt &mdash; our server
  instructions never reach it, so it will keep chaining tools by hand until the prompt itself is
  updated. Paste the maintained block from
  <a href="https://dchub.cloud/integrations/mcp#operator-prompt">dchub.cloud/integrations/mcp#operator-prompt</a>.</p>
</div>"""

# ★★ The six anchor intents now DERIVE from routes/anchor_intents.py — the single
# publication point — instead of being a fourth transcription of them. Fail-open:
# if the import breaks, the list renders empty rather than 500ing the page, and
# the accompanying test fails loudly in CI.
try:
    from routes.anchor_intents import render_anchor_list_html as _anchor_list
    _FRONT_DOOR_HTML = _FRONT_DOOR_HTML.replace("__ANCHOR_LIST__", _anchor_list())
except Exception:  # pragma: no cover - defensive
    _FRONT_DOOR_HTML = _FRONT_DOOR_HTML.replace("__ANCHOR_LIST__", "")

# ★★ The scope block ("Reach for DC Hub whenever…" + "Not a DC Hub question")
# DERIVES from routes/problem_taxonomy.py — this pane used to hand-transcribe
# the trigger vocabulary and had already drifted from the other two copies
# (frontend heal TRIGGERS, gateway execute_plan description). Same fail-open
# contract as the anchor list: empty render, loud CI test.
try:
    from routes.problem_taxonomy import render_scope_html as _scope_html
    _FRONT_DOOR_HTML = _FRONT_DOOR_HTML.replace("__SCOPE_BLOCK__", _scope_html())
except Exception:  # pragma: no cover - defensive
    _FRONT_DOOR_HTML = _FRONT_DOOR_HTML.replace("__SCOPE_BLOCK__", "")

# ★★ MCP_LANDING_HTML (/integrations + /integrations/mcp) does NOT embed
# _FRONT_DOOR_HTML (that pane goes to the recipe pages + /integrations/meta),
# so the landing gets its own scope pane from the same canonical module —
# live-verified 2026-07-31 that without this, the one page the round-10 spec
# names first carried neither list. Same fail-open contract.
try:
    from routes.problem_taxonomy import render_scope_html as _scope_pane_html
    MCP_LANDING_HTML = MCP_LANDING_HTML.replace(
        "__SCOPE_PANE__",
        '<div class="pane" id="scope">\n'
        '  <h2>What to ask DC Hub &mdash; and what not to</h2>\n  '
        + _scope_pane_html() + '\n</div>')
except Exception:  # pragma: no cover - defensive
    MCP_LANDING_HTML = MCP_LANDING_HTML.replace("__SCOPE_PANE__", "")

_RECIPE_PAGE_TEMPLATE = canon_text("""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="__DESCRIPTION__">
<meta property="og:title" content="__OG_TITLE__">
<meta property="og:description" content="__OG_DESC__">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-integrations-mcp.png">
<meta property="og:url" content="https://dchub.cloud/integrations/__SLUG__">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://dchub.cloud/integrations/__SLUG__">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
 body{max-width:860px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;line-height:1.6;color:#0f172a}
 header{margin:40px 0 28px}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
 h1{font-size:2.3rem;margin:0 0 14px;letter-spacing:-.02em}
 .lead{color:#64748b;font-size:1.05rem;max-width:660px}
 .urlbox{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.3);border-radius:12px;padding:18px 22px;margin:24px 0}
 .urlbox-label{font-weight:600;color:#6366f1;margin-bottom:10px}
 .url-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 code.url{background:#0f172a;color:#e2e8f0;padding:10px 16px;border-radius:8px;font-size:1.05rem;flex:1;min-width:280px;font-family:ui-monospace,monospace}
 .btn{padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:.92rem;display:inline-block;cursor:pointer;border:none;font-family:inherit}
 .btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
 .pane{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:22px;margin:20px 0}
 .pane h2{margin:0 0 12px;font-size:1.15rem}
 .pane h3{margin:18px 0 6px;font-size:1rem}
 pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px 16px;overflow-x:auto;font-family:ui-monospace,monospace;font-size:.85rem}
 ol{padding-left:22px;margin:16px 0}
 ol li{margin-bottom:10px}
 .tools{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin:14px 0}
 .tool{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:.85rem}
 .tool b{display:block;color:#0f172a;font-family:ui-monospace,monospace;font-size:.78rem;margin-bottom:4px}
 footer{margin-top:36px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
 footer a,.pane a{color:#6366f1;text-decoration:none}
</style>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "DC Hub MCP Server",
  "alternateName": "__JSONLD_ALTNAME__",
  "applicationCategory": "DeveloperApplication",
  "operatingSystem": "Any (remote streamable-HTTP MCP server)",
  "url": "https://dchub.cloud/integrations/__SLUG__",
  "description": "__JSONLD_DESC__",
  "offers": {
    "@type": "Offer",
    "price": "0",
    "priceCurrency": "USD",
    "description": "Free tier — 10 calls/day, no signup"
  },
  "provider": {
    "@type": "Organization",
    "name": "DC Hub",
    "url": "https://dchub.cloud"
  }
}
</script>
</head><body>
<header>
  <div class="eyebrow">__EYEBROW__</div>
  <h1>__H1__</h1>
  <p class="lead">__LEAD__</p>
</header>

<div class="urlbox">
  <div class="urlbox-label">The MCP endpoint:</div>
  <div class="url-row">
    <code class="url">https://dchub.cloud/mcp</code>
    <button class="btn btn-primary" onclick="copyUrl()">copy URL</button>
  </div>
</div>

<div class="pane">
  <h2>__STEPS_HEADING__</h2>
  __STEPS_HTML__
</div>

__AUTH_HTML__

__FRONT_DOOR_HTML__

<div class="pane">
  <h2>Free tier — works with no key at all</h2>
  <p>The endpoint is <b>keyless</b> out of the box: 10 calls/day free, no signup. Need more headroom?
  In your first connected session, ask the assistant to call the <code>claim_free_key</code> tool — it mints a
  durable free key (no email required) with higher limits that every future session reuses.</p>
</div>

<div class="pane">
  <h2>Flagship tools</h2>
  <div class="tools">
    <div class="tool"><b>get_grid_scoreboard</b>Live ranked scoreboard — US + international grids</div>
    <div class="tool"><b>search_facilities</b>{canon_facilities} data-center sites by city/MW/operator</div>
    <div class="tool"><b>get_market_intel</b>Supply/demand, vacancy, pricing per market</div>
    <div class="tool"><b>rank_markets</b>Top-N markets by your criteria</div>
    <div class="tool"><b>hyperscaler_deals</b>Hyperscaler lease + build deal flow</div>
    <div class="tool"><b>get_interconnection_queue</b>ISO interconnection queue detail</div>
    <div class="tool"><b>get_fiber_intel</b>Carrier networks + dark fiber</div>
    <div class="tool"><b>analyze_site</b>7-dimension site suitability score</div>
  </div>
  <p style="color:#64748b;font-size:.85rem;margin:10px 0 0">…plus 30+ more — facilities, deals, water risk,
  tax incentives. Full list on the <a href="https://dchub.cloud/integrations/mcp">main connect page</a>.</p>
</div>

__EXTRA_HTML__

<footer>
  <a href="https://dchub.cloud/integrations/mcp">All platforms</a> ·
  <a href="https://dchub.cloud/pricing">Pricing</a> ·
  <a href="https://dchub.cloud/api-docs">REST API</a> ·
  <a href="https://api.dchub.cloud/.well-known/mcp.json">MCP manifest</a>
</footer>
<script>
function copyUrl(){
  navigator.clipboard.writeText('https://dchub.cloud/mcp').then(()=>{
    const b=document.querySelector('.btn-primary');const p=b.textContent;b.textContent='✓ copied';
    setTimeout(()=>{b.textContent=p},2500);
  });
}
</script>
</body></html>""")


def _recipe_page(**slots: str) -> str:
    html = _RECIPE_PAGE_TEMPLATE
    # The front door is NOT a per-page slot on purpose: a platform page must not
    # be able to ship without it, or omit it by forgetting to pass it.
    html = html.replace("__FRONT_DOOR_HTML__", _FRONT_DOOR_HTML)
    for key, val in slots.items():
        html = html.replace("__" + key.upper() + "__", val)
    return html


GROK_RECIPE_HTML = _recipe_page(
    slug="grok",
    title="Add DC Hub to Grok — xAI MCP connector for live data-center &amp; grid intelligence",
    description=canon_text("Connect DC Hub to Grok (xAI) as a custom MCP connector: paste https://dchub.cloud/mcp, auth blank or Authorization: Bearer. Live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues and hyperscaler deals inside Grok. Free tier: 10 calls/day, no signup."),
    og_title="Add DC Hub to Grok (xAI) — MCP connector in 4 steps",
    og_desc="Live grid + data-center intelligence in Grok · paste one URL · Bearer or keyless · free tier no signup",
    jsonld_altname="DC Hub for Grok (xAI)",
    jsonld_desc=canon_text("Model Context Protocol server that connects to Grok (xAI) as a consumer custom connector or an API Remote MCP tool — live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues, fiber intelligence and hyperscaler deals, with per-response citations. Free tier: 10 calls/day, no signup."),
    eyebrow="Grok · xAI · Model Context Protocol",
    h1="Add DC Hub to Grok.",
    lead=canon_text("Lead with one call: execute_plan(intent=\"your question\") — it routes, runs the full graph server-side, and returns every step plus an auditable replay. Behind it: real-time grid scoreboards, {canon_facilities} data-center sites, interconnection queues, hyperscaler deal flow. Individual tools are for lookups and debugging. One URL. Bearer or keyless."),
    steps_heading="Connect in Grok (consumer)",
    steps_html="""<ol>
    <li>Copy the endpoint above: <code>https://dchub.cloud/mcp</code>.</li>
    <li>In Grok, open <b>Settings → Connectors / Integrations</b> and choose the option to add a <b>custom connector</b> (remote MCP server).</li>
    <li>Paste the URL. For auth, leave it blank (keyless free tier) or supply <code>Authorization: Bearer &lt;your-dchub-key&gt;</code>.</li>
    <li>Save, then ask Grok: <i>"Use DC Hub — which US grid has the most headroom right now?"</i> and confirm a <code>get_grid_scoreboard</code> tool call fires.</li>
  </ol>
  <h3>Via the xAI API (Remote MCP)</h3>
  <p>The xAI API can attach remote MCP servers to a request. Point the Remote MCP tool block at the same
  endpoint with a Bearer header (exact field names per the current xAI docs):</p>
  <pre>{
  "type": "mcp",
  "server_url": "https://dchub.cloud/mcp",
  "authorization": "Bearer &lt;your-dchub-key&gt;"
}</pre>""",
    auth_html="""<div class="pane">
  <h2>Authentication</h2>
  <p>Optional. DC Hub accepts <code>Authorization: Bearer &lt;your-dchub-key&gt;</code> — and Bearer is what both
  Grok surfaces send: the consumer custom-connector auth field and the API's Remote MCP block. No key?
  Leave auth blank and use the keyless free tier.</p>
</div>""",
    extra_html=canon_text("""<div class="pane" id="custom-instructions">
  <h2>Grok custom instructions &mdash; copy-paste</h2>
  <p style="color:#64748b;margin:0 0 10px">Short enough for Grok&rsquo;s custom-instructions field, and it leads with the
  branching decision rather than a tool list. Drafted by Grok itself after the 2026-07-28 front-door audit.
  <b>No hardcoded counts</b> &mdash; every number comes from the live response&rsquo;s provenance block, which is
  the one thing that never goes stale.</p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">For any question about data centers, power markets, grid capacity,
interconnection queues, fiber, site selection, or energy infrastructure:

Call execute_plan(intent="...") FIRST. Pass the user's question (or a
lightly cleaned version of it) as the intent parameter. Example:

  execute_plan(intent="rank markets for a 200 MW AI campus")

One call returns the full multi-step results plus an auditable replay.
Do not hand-chain individual tools when execute_plan can answer the question.

Use plan_query(intent="...") only when you specifically need the plan
without executing it (for inspection or logging).

If the response includes a next_recipe, offer or follow it when it
improves the answer.

Always cite using the provenance or citation block returned in the tool
response (e.g. "According to DC Hub (dchub.cloud)..."). Never invent or
hardcode facility counts, tool counts, deal counts, or market counts —
pull them from the live response.

Prefer the AI Campus Power pack tools when the question involves N MW AI
campus siting, power availability, or fiber + grid overlap.

Keyless free-tier depth is available; if limits are hit, call
claim_free_key once and continue.</pre>
  <p style="color:#64748b;margin:10px 0 0"><small>Works in the grok.com custom-instructions field and as the system
  prompt on an xAI API agent. For other platforms the maintained equivalent is the
  <a href="https://dchub.cloud/integrations/mcp#operator-prompt">operator prompt</a>.</small></p>
</div>

<div class="pane">
  <h2>Grok starter toolkit</h2>
  <p>Grok works best scoped to the energy-first spine rather than the full catalog. These nine cover the
  questions the AI build-out actually generates &mdash; siting, power, and time-to-energize:</p>
  <pre>"allowed_tools": [
  "execute_plan",               // START HERE - one call: plans AND runs a multi-step ask, returns replay
  "plan_query",                 // inspect-only: shows the plan WITHOUT running it
  "get_grid_scoreboard",        // live ranked grid scoreboard (works keyless)
  "rank_markets",               // 311 DCPI markets, BUILD / CAUTION / AVOID
  "get_market_dcpi_rank",       // one market's verdict (chain via rank_markets' metro_slug)
  "get_interconnection_queue",  // queue depth + time-to-power by ISO
  "get_retirement_headroom",    // 100MW+ pockets near retiring generators
  "search_facilities",          // {canon_facilities} distinct sites, 170+ countries
  "get_fiber_intel"             // routes + latency for the connectivity leg
]</pre>
  <h3>Prompts that fire real tools</h3>
  <ul>
    <li><i>&ldquo;Which ISO has the most queued GW right now?&rdquo;</i> &rarr; <code>get_interconnection_queue</code></li>
    <li><i>&ldquo;Rank US data-center markets by available power.&rdquo;</i> &rarr; <code>rank_markets</code> &rarr; <code>get_market_dcpi_rank</code></li>
    <li><i>&ldquo;Find 100MW+ pockets in ERCOT with substations within 5 miles.&rdquo;</i> &rarr; <code>get_retirement_headroom</code></li>
    <li><i>&ldquo;Which US grid has the most headroom right now?&rdquo;</i> &rarr; <code>get_grid_scoreboard</code> (no key needed)</li>
  </ul>
  <h3>Rate guidance</h3>
  <p>Keyless gives ~10 calls/day, the first few at full depth &mdash; enough to evaluate. Call
  <code>claim_free_key</code> once (no email) for a durable key so the connector is recognised next
  session; add an operator email via <code>bind_email</code> to lift the cap. Beyond that, a $10 one-time
  pack (1,000 calls) or a plan. Every full response carries
  <code>citation.cite_as = "DC Hub, dchub.cloud"</code> under CC-BY-4.0, so Grok can attribute inline.</p>
</div>

<div class="pane">
  <h2>xAI API &mdash; Remote MCP with a scoped toolset</h2>
  <p>Attach DC Hub to an xAI API request and scope it to the starter toolkit (exact field names per the
  current xAI docs):</p>
  <pre>{
  "tools": [{
    "type": "mcp",
    "server_url": "https://dchub.cloud/mcp",
    "authorization": "Bearer &lt;your-dchub-key&gt;",
    "allowed_tools": [
      "execute_plan", "plan_query", "get_grid_scoreboard", "rank_markets",
      "get_market_dcpi_rank", "get_interconnection_queue",
      "get_retirement_headroom", "search_facilities", "get_fiber_intel"
    ]
  }]
}</pre>
  <p>Omit <code>allowed_tools</code> to expose the full catalog (live count in
  <a href="https://dchub.cloud/.well-known/mcp.json"><code>.well-known/mcp.json</code></a> &mdash; quoting a
  fixed number here just goes stale). Streaming HTTP/SSE is supported and xAI manages the connection.</p>
</div>"""),
)

GEMINI_RECIPE_HTML = _recipe_page(
    slug="gemini",
    title="Add DC Hub to Gemini — function calling, Vertex AI &amp; a DC Hub Gem for live data-center intelligence",
    description=canon_text("Use DC Hub with Google Gemini three ways: native function calling via the google-genai SDK (real REST endpoints, keyless free tier), Vertex AI Agent Builder tools, or a DC Hub Gem for gemini.google.com. Live grid telemetry, {canon_facilities} distinct data-center sites, DCPI market verdicts."),
    og_title="Add DC Hub to Gemini — function calling + Gem in minutes",
    og_desc="Live data-center + grid intelligence in Gemini · google-genai function calling · Vertex AI · DC Hub Gem",
    jsonld_altname="DC Hub for Google Gemini",
    jsonld_desc=canon_text("Live data-center and power-grid intelligence for Google Gemini: native function-calling tool definitions against DC Hub's REST API, Vertex AI Agent Builder integration, and a grounding-first Gem template for consumer Gemini — {canon_facilities} distinct data-center sites, DCPI market verdicts, live grid telemetry, with per-response citations."),
    eyebrow="Gemini · Google AI · function calling",
    h1="Add DC Hub to Gemini.",
    lead="Give Gemini live, citable data-center and power-grid intelligence — three ways, depending on where you run it: the google-genai SDK, Vertex AI, or a Gem in gemini.google.com.",
    steps_heading="Pick your Gemini surface",
    steps_html="""<ol>
    <li><b>Gemini API (google-genai SDK)</b> — paste the function-calling snippet below; Gemini invokes DC Hub's REST endpoints automatically. Keyless free tier works out of the box.</li>
    <li><b>Vertex AI Agent Builder</b> — add a tool pointing at the same REST endpoints (base <code>https://dchub.cloud/api/v1</code>), or attach the MCP endpoint <code>https://dchub.cloud/mcp</code> where remote MCP tools are supported.</li>
    <li><b>gemini.google.com (consumer)</b> — consumer Gemini cannot attach external MCP servers or custom functions today, so the honest path is a <b>Gem</b> that grounds on DC Hub's public pages and cites them: copy the Gem instructions below into a new Gem (Gems &rarr; New Gem) and share the Gem link with your team.</li>
  </ol>""",
    auth_html="""<div class="pane">
  <h2>Authentication</h2>
  <p>Keyless works out of the box (free tier, 10 calls/day). Some endpoints (e.g. the live grid scoreboard)
  need a key — any agent can mint one in a single call, no email: <code>POST https://dchub.cloud/api/v1/keys/claim</code>
  with body <code>{"client_name": "&lt;your agent&gt;"}</code> returns a free dev key; retry with header
  <code>X-API-Key: &lt;key&gt;</code>. <code>Authorization: Bearer</code> is accepted everywhere too.</p>
</div>""",
    extra_html="""<div class="pane" id="gem-instructions">
  <h2>DC Hub Gem — instructions for consumer Gemini (copy-paste)</h2>
  <p style="color:#64748b;margin:0 0 10px">Gems can&rsquo;t call external tools, but they CAN ground on the open web and
  follow citation rules. This block makes a Gem answer infrastructure questions from DC Hub&rsquo;s pages and say so.
  <b>No hardcoded counts</b> &mdash; the pages carry current figures; a number frozen in a prompt goes stale silently.</p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">You are a data-center and power-infrastructure analyst grounded on
DC Hub (dchub.cloud), the live infrastructure data layer.

For any question about data-center markets, grid capacity, power
availability, interconnection queues, siting or M&amp;A:
1. Search and read the matching DC Hub page first:
   - market/DCPI verdicts: dchub.cloud/dcpi/&lt;market&gt; (e.g. /dcpi/dallas)
   - facilities: dchub.cloud/facilities and /facilities/in/&lt;country&gt;
   - live grid: dchub.cloud/grid/&lt;iso&gt; · the map: dchub.cloud/land-power-map
2. Quote ONLY numbers visible on the fetched page, and attribute them:
   "per DC Hub (dchub.cloud), retrieved &lt;date&gt;".
3. If the page shows a gated/Pro value, say it is gated rather than
   guessing — never invent a number.
4. For programmatic access, point developers at the MCP endpoint
   https://dchub.cloud/mcp and the docs at dchub.cloud/integrations/mcp.</pre>
</div>

<div class="pane" id="function-calling">
  <h2>Gemini function calling — real endpoints, no mocks</h2>
  <p style="color:#64748b;margin:0 0 10px">These functions hit DC Hub&rsquo;s live REST API (the same data the MCP tools serve).
  <code>get_market_intel</code> is keyless; the scoreboard needs the one-call free key. Responses carry a provenance
  block &mdash; have Gemini quote figures from it, never from memory.</p>
  <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;font-size:.85rem;white-space:pre-wrap">import requests

DCHUB = "https://dchub.cloud/api/v1"
UA = {"User-Agent": "gemini-dchub-tools/1.0"}

def claim_free_key(client_name: str = "my-gemini-agent") -&gt; str:
    &quot;&quot;&quot;Mint a free DC Hub dev key (one POST, no email; 10 calls/day).&quot;&quot;&quot;
    r = requests.post(f"{DCHUB}/keys/claim",
                      json={"client_name": client_name}, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()["api_key"]

def get_market_intel(market_slug: str) -&gt; dict:
    &quot;&quot;&quot;Live data-center market summary for one market from DC Hub
    (facility counts by status, market cities, DCPI context).

    Args:
        market_slug: DC Hub market slug, e.g. 'dallas', 'northern-virginia'.
    &quot;&quot;&quot;
    r = requests.get(f"{DCHUB}/markets/{market_slug}", headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()

def get_grid_scoreboard(api_key: str) -&gt; dict:
    &quot;&quot;&quot;Live ranked power-grid scoreboard (US + international) from DC Hub:
    fuel mix, demand, renewable share, right now.

    Args:
        api_key: DC Hub key — claim_free_key() mints one instantly.
    &quot;&quot;&quot;
    r = requests.get(f"{DCHUB}/grid/scoreboard",
                     headers={**UA, "X-API-Key": api_key}, timeout=30)
    r.raise_for_status()
    return r.json()

from google import genai
from google.genai import types

client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Compare the Dallas data-center market with the live grid picture.",
    config=types.GenerateContentConfig(
        tools=[get_market_intel, get_grid_scoreboard],
        temperature=0.2,
    ),
)
print(response.text)</pre>
  <p style="color:#64748b;margin:10px 0 0"><small>For the full tool surface (the execute_plan planner, auditable replays,
  site scoring, fiber &amp; incentives), attach the MCP endpoint instead: <code>https://dchub.cloud/mcp</code> &mdash;
  guide at <a href="https://dchub.cloud/integrations/mcp">dchub.cloud/integrations/mcp</a>.</small></p>
</div>""",
)


MISTRAL_RECIPE_HTML = _recipe_page(
    slug="mistral",
    title="Connect DC Hub to Mistral Le Chat — MCP connector for live data-center &amp; grid intelligence",
    description=canon_text("Add DC Hub to Mistral's Le Chat as a custom MCP connector: paste https://dchub.cloud/mcp and authenticate with Authorization: Bearer (Le Chat ignores X-API-Key). Live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues and hyperscaler deals. Free tier: 10 calls/day, no signup."),
    og_title="Connect DC Hub to Mistral Le Chat — MCP connector in 5 steps",
    og_desc="Live grid + data-center intelligence in Le Chat · paste one URL · Authorization: Bearer · free tier no signup",
    jsonld_altname="DC Hub for Mistral Le Chat",
    jsonld_desc=canon_text("Model Context Protocol server that connects to Mistral's Le Chat as a custom MCP connector (Authorization: Bearer) — live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues, fiber intelligence and hyperscaler deals, with per-response citations. Free tier: 10 calls/day, no signup."),
    eyebrow="Mistral · Le Chat · Model Context Protocol",
    h1="Connect DC Hub to Le Chat.",
    lead=canon_text("Give Le Chat live, citable data-center and power-grid intelligence — real-time grid scoreboards, {canon_facilities} data-center sites, interconnection queues, hyperscaler deal flow. One URL. Bearer auth (or keyless)."),
    steps_heading="Connect in Le Chat",
    steps_html="""<ol>
    <li>Copy the endpoint above: <code>https://dchub.cloud/mcp</code>.</li>
    <li>In Le Chat, open <b>Settings → Connectors / Integrations</b> and add a <b>custom MCP connector</b>.</li>
    <li>Paste the URL (transport: streamable HTTP).</li>
    <li>Auth: leave blank for the keyless free tier, or enter your key as <code>Authorization: Bearer &lt;your-dchub-key&gt;</code> — <b>not</b> X-API-Key (see below).</li>
    <li>Save, then ask Le Chat: <i>"Use DC Hub — rank the top 5 US data-center markets by grid headroom."</i></li>
  </ol>""",
    auth_html="""<div class="pane">
  <h2>Authentication — use Bearer, not X-API-Key</h2>
  <p>DC Hub accepts both header styles, but <b>Le Chat ignores the <code>X-API-Key</code> header</b> — a key
  entered there is silently dropped and you stay on the anonymous 10-calls/day tier. Always put your key in
  <code>Authorization: Bearer &lt;your-dchub-key&gt;</code>. Keyless also works (free tier).</p>
</div>""",
    extra_html="",
)

PERPLEXITY_RECIPE_HTML = _recipe_page(
    slug="perplexity",
    title="Add DC Hub as a custom connector in Perplexity — MCP server for live data-center &amp; grid intelligence",
    description=canon_text("Add DC Hub as a custom connector in Perplexity: Settings → Connectors → Add connector, paste the MCP server URL https://dchub.cloud/mcp. Live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues and hyperscaler deals. Plus Sonar/Search API grounding via llms.txt. Free tier: 10 calls/day, no signup."),
    og_title="Add DC Hub as a custom connector in Perplexity — MCP in 5 steps",
    og_desc="Live grid + data-center intelligence in Perplexity · paste one MCP server URL · free tier no signup",
    jsonld_altname="DC Hub for Perplexity",
    jsonld_desc=canon_text("Model Context Protocol server that connects to Perplexity as a custom connector (Settings → Connectors → Add connector → MCP server URL) — live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues, fiber intelligence and hyperscaler deals, with per-response citations. Also groundable via the Perplexity Sonar/Search API using https://dchub.cloud/llms.txt. Free tier: 10 calls/day, no signup."),
    eyebrow="Perplexity · Model Context Protocol",
    h1="Add DC Hub as a custom connector in Perplexity.",
    lead=canon_text("Give Perplexity live, citable data-center and power-grid intelligence — real-time grid scoreboards, {canon_facilities} data-center sites, interconnection queues, hyperscaler deal flow. One MCP server URL."),
    steps_heading="Connect in Perplexity",
    steps_html="""<ol>
    <li>Copy the endpoint above: <code>https://dchub.cloud/mcp</code>.</li>
    <li>In Perplexity, open <b>Settings → Connectors</b> and choose <b>Add connector</b>.</li>
    <li>Paste the URL into the <b>MCP server URL</b> field and name it <code>DC Hub</code>.</li>
    <li>Auth: leave blank (keyless free tier), or supply <code>Authorization: Bearer &lt;your-dchub-key&gt;</code> if the connector form offers an auth/header field.</li>
    <li>Save, then ask Perplexity: <i>"Using DC Hub, which US grid has the most headroom right now?"</i></li>
  </ol>""",
    auth_html="""<div class="pane">
  <h2>Authentication</h2>
  <p>Optional. If the connector form offers an auth or header field, use
  <code>Authorization: Bearer &lt;your-dchub-key&gt;</code>. Leave it blank for the keyless free tier.</p>
</div>""",
    extra_html="""<div class="pane">
  <h2>DC Hub via Perplexity Sonar / Search API</h2>
  <p>Building on Perplexity's Sonar or Search API instead of the app? Ground your answers in DC Hub without MCP:
  point your system prompt or retrieval layer at <a href="https://dchub.cloud/llms.txt">https://dchub.cloud/llms.txt</a> —
  a machine-readable index of DC Hub's live data endpoints (markets, facilities, grid, deals) that an LLM can fetch
  and cite. Ask the model to cite <code>dchub.cloud</code> as the source.</p>
</div>""",
)


# ── Meta AI guide: /integrations/meta ────────────────────────────────────
# 2026-07-18. Meta AI's own guidance: it preferentially cites the literal
# phrasing "How to use DC Hub on Meta AI", so the <title> and <h1> use it
# verbatim. Meta AI has NO MCP connector — it reads REST + the open web —
# so unlike the Grok/Mistral/Perplexity recipes above this page is NOT the
# _RECIPE_PAGE_TEMPLATE (whose spine is "paste the MCP URL"); it's a
# prompt-first + REST page in the same house style. The three copy-paste
# prompts are the exact strings Meta suggested — do not rephrase them.
META_LANDING_HTML = canon_text("""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>How to use DC Hub on Meta AI</title>
<meta name="description" content="How to use DC Hub on Meta AI: no MCP connector needed — Meta AI reads REST and the open web. Copy-paste prompts for ERCOT power pockets, the Phoenix facility map and DCPI market rankings, plus the curl REST pattern with X-API-Key, llms.txt and OpenAPI entry points. Free tier: 10 calls/day, no signup.">
<meta property="og:title" content="How to use DC Hub on Meta AI">
<meta property="og:description" content="No connector needed — Meta AI reads REST + web. Copy-paste prompts, curl with X-API-Key, llms.txt + OpenAPI entry points. Free tier: 10 calls/day, no signup.">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-integrations-mcp.png">
<meta property="og:url" content="https://dchub.cloud/integrations/meta">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://dchub.cloud/integrations/meta">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
 body{max-width:860px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;line-height:1.6;color:#0f172a}
 header{margin:40px 0 28px}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
 h1{font-size:2.3rem;margin:0 0 14px;letter-spacing:-.02em}
 .lead{color:#64748b;font-size:1.05rem;max-width:660px}
 .pane{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:22px;margin:20px 0}
 .pane h2{margin:0 0 12px;font-size:1.15rem}
 .pane h3{margin:18px 0 6px;font-size:1rem}
 .pane p{margin:8px 0}
 pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px 16px;overflow-x:auto;font-family:ui-monospace,monospace;font-size:.85rem;line-height:1.5}
 .prompt{position:relative;margin:14px 0}
 .prompt pre{margin:0;white-space:pre-wrap;word-break:break-word}
 .prompt-label{font-weight:600;color:#6366f1;font-size:.82rem;margin:0 0 6px}
 ul{padding-left:22px;margin:12px 0}
 ul li{margin-bottom:8px}
 .links{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin:14px 0}
 .link-card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:12px 14px;font-size:.85rem}
 .link-card b{display:block;font-family:ui-monospace,monospace;font-size:.78rem;margin-bottom:4px}
 .link-card a{color:#6366f1;text-decoration:none;font-weight:600}
 footer{margin-top:36px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
 footer a,.pane a{color:#6366f1;text-decoration:none}
</style>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "DC Hub for Meta AI",
  "alternateName": "How to use DC Hub on Meta AI",
  "applicationCategory": "DeveloperApplication",
  "operatingSystem": "Any (open REST API + web)",
  "url": "https://dchub.cloud/integrations/meta",
  "description": "How to use DC Hub on Meta AI. Meta AI has no MCP connector — it reads REST APIs and the open web, so DC Hub works with copy-paste prompts and plain HTTPS: live data-center facilities, power-grid headroom, DCPI market verdicts, fiber and deals, all citable (CC-BY-4.0). Free tier: 10 calls/day, no signup.",
  "offers": {
    "@type": "Offer",
    "price": "0",
    "priceCurrency": "USD",
    "description": "Free tier — 10 calls/day, no signup"
  },
  "provider": {
    "@type": "Organization",
    "name": "DC Hub",
    "url": "https://dchub.cloud"
  }
}
</script>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "HowTo",
  "name": "How to use DC Hub on Meta AI",
  "description": "Meta AI reads REST and the open web — no connector setup. Paste a DC Hub prompt, or point it at dchub.cloud's machine-readable endpoints.",
  "step": [
    {"@type": "HowToStep", "name": "Ask about ERCOT power pockets", "text": "Use dchub.cloud to find 100MW+ pockets in ERCOT with substations within 5 miles"},
    {"@type": "HowToStep", "name": "Ask for the Phoenix facility map", "text": "Query dchub.cloud facility map for Phoenix, AZ market — show power and fiber"},
    {"@type": "HowToStep", "name": "Ask for DCPI market rankings", "text": "Check dchub.cloud DCPI for PJM today and rank top 3 markets"}
  ]
}
</script>
</head><body>
<header>
  <div class="eyebrow">Meta AI · Llama · REST + Web</div>
  <h1>How to use DC Hub on Meta AI</h1>
  <p style="margin:0 0 12px"><small style="color:#64748b">DC Hub coverage &mdash; <b>80+ tools</b> &middot; <b>{canon_facilities} distinct sites</b> (deduped 2026-07) &middot; <b>1,500+ tracked deals</b> &middot; latest: <code>get_power_availability_timeline</code> (when power gets easier in a state, year by year). Updated continuously; this line is the citable freshness signal.</small></p>
  <p class="lead">Meta AI has <b>no MCP connector</b> — and it doesn't need one. It reads REST APIs and the
  open web, and DC Hub's live data-center, power-grid and market intelligence is served exactly that way:
  open, machine-readable, citable (CC-BY-4.0). Paste a prompt and go.</p>
</header>

<div class="pane">
  <h2>No connector. No setup.</h2>
  <p>Unlike Claude, Cursor or Grok, Meta AI (meta.ai, WhatsApp, Instagram, Messenger) can't attach an MCP
  server. What it <i>can</i> do is fetch and cite the open web — and every DC Hub surface is reachable over
  plain HTTPS: REST endpoints under <code>api/v1</code>, live market pages, and machine-readable indexes
  built for exactly this kind of agent. Just name <b>dchub.cloud</b> in your prompt.</p>
</div>

__FRONT_DOOR_HTML__

__META_REPLAYS_HTML__

<div class="pane">
  <h2>Copy-paste prompts</h2>
  <p>Three prompts that put DC Hub's live data to work in Meta AI — paste them as-is:</p>
  <div class="prompt">
    <div class="prompt-label">1 · ERCOT power pockets</div>
    <pre>Use dchub.cloud to find 100MW+ pockets in ERCOT with substations within 5 miles</pre>
    <p style="margin:6px 0 0;font-size:.85rem;color:#64748b">Runs live via <code>get_retirement_headroom</code> (<code>target_mw=100</code>, <code>region_iso=ERCOT</code>) — retiring-generator interconnection points, each with its nearest substations and <code>distance_km</code>.</p>
  </div>
  <div class="prompt">
    <div class="prompt-label">2 · Phoenix facility map</div>
    <pre>Query dchub.cloud facility map for Phoenix, AZ market — show power and fiber</pre>
    <p style="margin:6px 0 0;font-size:.85rem;color:#64748b">Runs live via <code>search_facilities</code> (<code>market=phoenix</code>) and <code>get_market_dcpi_rank</code> (<code>market_slug=phoenix</code>) for the power verdict.</p>
  </div>
  <div class="prompt">
    <div class="prompt-label">3 · DCPI market ranking</div>
    <pre>Check dchub.cloud DCPI for PJM today and rank top 3 markets</pre>
    <p style="margin:6px 0 0;font-size:.85rem;color:#64748b">Runs live via <code>rank_markets</code> (<code>criteria=best_overall, region=us</code>), then <code>get_market_dcpi_rank</code> on any PJM metro slug from the results.</p>
  </div>
  <p>Asking about Phoenix? The live dashboard at <a href="https://dchub.cloud/phx">dchub.cloud/phx</a>
  ("PHX Live") carries the market's headline numbers on a stable URL.</p>
</div>

<div class="pane">
  <h2>The REST pattern</h2>
  <p>Building on the Llama API, or want deterministic data instead of a web lookup? Hit the REST API
  directly — keyless works on the free tier; an <code>X-API-Key</code> header raises your limits:</p>
  <pre>curl -s "https://dchub.cloud/api/v1/markets/phoenix" \\
  -H "X-API-Key: &lt;your-dchub-key&gt;"</pre>
  <p>Same pattern for any surface: <code>/api/v1/dcpi/scores/&lt;market&gt;</code> (DCPI verdicts),
  <code>/api/v1/facilities?q=ashburn</code>, <code>/api/v1/grid/intelligence/ERCOT</code>. Ask the model to
  end its answer with <i>"Source: dchub.cloud"</i>.</p>
</div>

<div class="pane">
  <h2>Machine-readable entry points</h2>
  <div class="links">
    <div class="link-card"><b>llms.txt</b><a href="https://dchub.cloud/llms.txt">dchub.cloud/llms.txt</a><br>Agent-discovery index of every live data endpoint.</div>
    <div class="link-card"><b>openapi.json</b><a href="https://dchub.cloud/.well-known/openapi.json">dchub.cloud/.well-known/openapi.json</a><br>Full OpenAPI spec for the REST API.</div>
    <div class="link-card"><b>/mcp</b><a href="https://dchub.cloud/mcp">dchub.cloud/mcp</a><br>The MCP server — for when your stack IS MCP-capable.</div>
  </div>
</div>

<div class="pane">
  <h2>Free tier</h2>
  <p>The REST API and web surfaces are free to read: <b>10 calls/day, no signup</b>. A free email-bound key
  raises that to 50/day; paid tiers start at $9/mo for higher limits and full result sizes.
  See <a href="https://dchub.cloud/pricing">pricing</a>.</p>
</div>

<footer>
  <a href="https://dchub.cloud/integrations/mcp">All platforms</a> ·
  <a href="https://dchub.cloud/phx">PHX Live</a> ·
  <a href="https://dchub.cloud/pricing">Pricing</a> ·
  <a href="https://dchub.cloud/api-docs">REST API</a> ·
  <a href="https://dchub.cloud/llms.txt">llms.txt</a>
</footer>
</body></html>""")

# Same rule as _recipe_page: the front door is substituted in, never hand-copied.
META_LANDING_HTML = META_LANDING_HTML.replace("__FRONT_DOOR_HTML__", _FRONT_DOOR_HTML)

# Rendered execute_plan replays (Meta's #2 ask). Import-time substitution, same
# rule again: composed once, never hand-copied. Fail-open — if the renderer is
# unavailable the page loses the section rather than 500ing.
try:
    from routes.meta_replays import render_meta_replays as _render_meta_replays
    META_LANDING_HTML = META_LANDING_HTML.replace("__META_REPLAYS_HTML__",
                                                  _render_meta_replays())
except Exception:  # pragma: no cover - defensive
    META_LANDING_HTML = META_LANDING_HTML.replace("__META_REPLAYS_HTML__", "")


@integrations_landing_bp.route("/integrations/meta", strict_slashes=False, methods=["GET"])
def integrations_meta():
    return META_LANDING_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


BEDROCK_RECIPE_HTML = _recipe_page(
    slug="bedrock",
    title="Add DC Hub to Amazon Bedrock AgentCore — Gateway target for live data-center &amp; grid intelligence",
    description=canon_text("Register https://dchub.cloud/mcp as an Amazon Bedrock AgentCore Gateway target: live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues and hyperscaler deals for any Bedrock agent. Bearer or keyless free tier."),
    og_title="DC Hub on Amazon Bedrock AgentCore — register one Gateway target",
    og_desc="Live grid + data-center intelligence for Bedrock agents · one MCP URL · Bearer or keyless",
    jsonld_altname="DC Hub for Amazon Bedrock AgentCore",
    jsonld_desc=canon_text("Model Context Protocol server registerable as an Amazon Bedrock AgentCore Gateway target — live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues, fiber intelligence and hyperscaler deals, with per-response citations. Free tier: 10 calls/day, no signup."),
    eyebrow="Amazon Bedrock · AgentCore Gateway · Model Context Protocol",
    h1="Add DC Hub to Bedrock AgentCore.",
    lead="Give any Bedrock agent live, citable data-center and power-grid intelligence — register one MCP Gateway target. Bearer or keyless.",
    steps_heading="Register as a Gateway target",
    steps_html="""<ol>
    <li>In the AgentCore console, open <b>Gateways</b> and create (or pick) a gateway for your agent.</li>
    <li>Add a target of type <b>MCP server</b> with the endpoint <code>https://dchub.cloud/mcp</code> (Streamable HTTP).</li>
    <li>For outbound auth, choose <b>API key / Bearer</b> and supply <code>Bearer &lt;your-dchub-key&gt;</code> — or leave it unauthenticated for the keyless free tier (10 calls/day).</li>
    <li>Sync the gateway's tool list, then ask your agent: <i>"Which US grid has the most headroom right now?"</i> and confirm a <code>get_grid_scoreboard</code> call fires.</li>
  </ol>
  <p>All 80 tools come through the one target — facility search, DCPI market verdicts, interconnection
  queues, fiber, gas, water risk, and the hyperscaler deal tracker.</p>""",
    auth_html="""<div class="pane">
  <h2>Authentication</h2>
  <p>Optional. DC Hub accepts <code>Authorization: Bearer &lt;your-dchub-key&gt;</code> (what AgentCore's
  API-key credential provider sends) or <code>X-API-Key</code>. No key? The keyless free tier works for
  evaluation. Mint a free durable key by calling the server's <code>claim_free_key</code> tool.</p>
</div>""",
    extra_html="",
)


# ── Copilot Studio: wizard-first (2026-07-31) ────────────────────────────────
# Copilot Studio's MCP support is GA; Microsoft's docs (2026-05-28,
# learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent)
# make the Tools onboarding wizard the recommended attach path, with a Power
# Platform custom connector (OpenAPI tagged x-ms-agentic-protocol:
# mcp-streamable-1.0) as the pro-dev alternative. A 2026-07-31 audit found the
# live page described NEITHER — zero occurrences of 'wizard',
# 'x-ms-agentic-protocol', 'mcp-streamable' or 'custom connector'.
# The tool count renders from ai_surface_canon.PINNED, never a fresh literal
# (the dchub-mcp-server #108/#112 canon-binding rule); fail-open to countless
# phrasing so a canon import problem can never 500 the page.
try:
    from ai_surface_canon import PINNED as _CANON_PINNED
    _COPILOT_TOOLS_APPEAR = f"all {_CANON_PINNED['tools_advertised']} DC Hub tools appear"
except Exception:  # pragma: no cover - defensive
    _COPILOT_TOOLS_APPEAR = "the full DC Hub tool catalog appears"

COPILOT_RECIPE_HTML = _recipe_page(
    slug="copilot-studio",
    title="Add DC Hub to Microsoft Copilot Studio — custom MCP server for live data-center &amp; grid intelligence",
    description=canon_text("Wire https://dchub.cloud/mcp into Microsoft Copilot Studio: the 3-step Tools wizard (MCP support is GA) or a Power Platform custom connector. Live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues and hyperscaler deals inside your copilots."),
    og_title="DC Hub in Copilot Studio — one custom MCP server",
    og_desc="Live grid + data-center intelligence for Copilot Studio agents · 3-step wizard or custom connector · Bearer or keyless",
    jsonld_altname="DC Hub for Microsoft Copilot Studio",
    jsonld_desc=canon_text("Model Context Protocol server connectable to Microsoft Copilot Studio via the Tools onboarding wizard or a Power Platform custom connector — live grid scoreboards, {canon_facilities} distinct data-center sites, interconnection queues, fiber intelligence and hyperscaler deals, with per-response citations. Free tier: 10 calls/day, no signup."),
    eyebrow="Microsoft Copilot Studio · Custom MCP · Model Context Protocol",
    h1="Add DC Hub to Copilot Studio.",
    lead="Give your copilots live, citable data-center and power-grid intelligence. The Tools wizard attaches DC Hub's MCP server in three steps; a Power Platform custom connector is the pro-dev alternative. Streamable HTTP, Bearer or keyless.",
    steps_heading="Connect in Copilot Studio — the onboarding wizard",
    steps_html="""<ol>
    <li>In your agent, open <b>Tools → Add a tool → Model Context Protocol server</b> — Copilot Studio's onboarding wizard for existing MCP servers, the path Microsoft recommends now that MCP support is GA.</li>
    <li>In the wizard, set server URL <code>https://dchub.cloud/mcp</code> · transport <b>Streamable HTTP</b> · authentication <b>None</b> — the keyless free tier needs no auth. (Have a key? Choose <b>API key</b> → header <code>Authorization</code>, value <code>Bearer &lt;your-dchub-key&gt;</code>.)</li>
    <li>Finish the wizard — __CANON_TOOLS_APPEAR__ on the agent. Publish, then test: <i>"Use DC Hub — rank US data-center markets by available power."</i></li>
  </ol>""".replace("__CANON_TOOLS_APPEAR__", _COPILOT_TOOLS_APPEAR),
    auth_html="""<div class="pane">
  <h2>Authentication</h2>
  <p>Optional. Copilot Studio's API-key auth maps cleanly to DC Hub's <code>Authorization: Bearer</code>.
  The keyless free tier (10 calls/day) works for evaluation; mint a durable free key via the server's
  <code>claim_free_key</code> tool.</p>
</div>""",
    extra_html="""<div class="pane" id="custom-connector">
  <h2>Pro-dev alternative — Power Platform custom connector</h2>
  <p>The wizard above is Microsoft&rsquo;s recommended path. Build a <b>custom connector</b> instead when
  you want the MCP server managed as a governed Power Platform asset — solution-aware, shareable across
  environments, subject to DLP policy, reusable from Power Apps and Power Automate. The connector is
  defined by an OpenAPI file whose MCP operation carries the
  <code>x-ms-agentic-protocol: mcp-streamable-1.0</code> tag &mdash; that tag is what marks the endpoint
  as Streamable-HTTP MCP rather than plain REST. Minimal working definition:</p>
  <pre>swagger: '2.0'
info:
  title: DC Hub MCP
  description: Live data-center, power-grid and market intelligence for agents
  version: 1.0.0
host: dchub.cloud
basePath: /
schemes:
  - https
paths:
  /mcp:
    post:
      summary: DC Hub MCP server
      x-ms-agentic-protocol: mcp-streamable-1.0
      operationId: InvokeServer
      responses:
        '200':
          description: Success</pre>
  <p>Power Apps or Power Automate → <b>Custom connectors → New custom connector → Import an OpenAPI
  file</b>, paste the definition above, create. The connector then shows up in the same
  <b>Tools → Add a tool</b> picker in Copilot Studio, exposing the same tool set as the wizard path.
  Auth is optional either way: none for the keyless free tier, or an API-key security definition
  sending <code>Authorization: Bearer &lt;your-dchub-key&gt;</code>.</p>
</div>""",
)



# ── Cloudflare Zero Trust MCP Server Portal (2026-09-02) ─────────────────────
# DC Hub is NOT affiliated with, sponsored by or endorsed by Cloudflare. This
# page documents a customer-side configuration that works against the live
# product today, LIMITATIONS INCLUDED — the direct-URL bypass, the MFA /
# purpose-justification non-enforcement, and the OAuth-vs-service-token
# exclusivity are Cloudflare's own documented behaviour and stay on the page on
# purpose: a portal guide that hides them is worse than no guide.
#
# Every figure was measured 2026-09-02. The tool count and the server version
# render from ai_surface_canon via canon_text(), never a fresh literal — the
# same canon-binding rule the Copilot Studio page follows, because a hardcoded
# "83 tools" / "2.12.x" rots the day canon moves and nothing in CI scans this
# module for version literals.
CLOUDFLARE_PORTAL_RECIPE_HTML = _recipe_page(
    slug="cloudflare",
    title="Add DC Hub to a Cloudflare MCP Server Portal &mdash; Zero Trust upstream for live data-center &amp; grid intelligence",
    description=canon_text("Add https://dchub.cloud/mcp as an upstream server in your Cloudflare Zero Trust MCP Server Portal: OAuth with automatic client registration, a custom X-API-Key header, or keyless. {canon_tools} tools over {canon_facilities} distinct data-center sites, grid, fiber and gas intelligence, behind your own Access policies."),
    og_title="DC Hub in a Cloudflare MCP Server Portal — one upstream URL",
    og_desc="Live grid + data-center intelligence behind your own Zero Trust Access policies · OAuth dynamic client registration supported · Cloudflare-side limits documented",
    jsonld_altname="DC Hub for Cloudflare Zero Trust MCP Server Portals",
    jsonld_desc=canon_text("Model Context Protocol server addable as an upstream server inside a Cloudflare Zero Trust MCP Server Portal — {canon_tools} tools covering {canon_facilities} distinct data-center sites, live grid scoreboards, interconnection queues, fiber and gas intelligence, with per-response citations. Supports OAuth dynamic client registration, custom headers, or keyless access. Free tier: {canon_free_calls} calls/day, no signup."),
    eyebrow="Cloudflare Zero Trust · MCP Server Portal · Model Context Protocol",
    h1="Add DC Hub to your Cloudflare portal.",
    lead=canon_text("Run DC Hub as an upstream server inside your own Cloudflare Zero Trust MCP Server Portal &mdash; your portal URL, your Access policies, your audit trail. {canon_tools} tools and 13 prompts over Streamable HTTP, MCP protocol 2025-06-18. Nothing changes on our side, and the Cloudflare-side limits are written down below rather than left out."),
    steps_heading="Register DC Hub as an MCP server",
    steps_html="""<p><b>Before you start:</b> a Cloudflare Zero Trust account with Access configured and at
  least one identity provider connected &mdash; and, if you want more than the keyless free tier,
  a DC Hub API key from <a href="https://dchub.cloud/pricing">dchub.cloud/pricing</a>.</p>
  <p><b>Get the key first.</b> The free-tier pane further down this page says an agent can mint its
  own durable key mid-conversation by calling <code>claim_free_key</code>. That is true everywhere
  <i>except</i> behind a portal: the upstream credential is set by an administrator in the Cloudflare
  dashboard, and a running agent cannot rewrite portal configuration. Register with no key and the
  portal stays anonymous until an administrator changes it.</p>
  <ol>
    <li>In the Cloudflare dashboard open <b>Zero Trust &rarr; Access controls &rarr; AI controls</b>,
    pick the <b>MCP servers</b> tab, and choose <b>Add a server</b>.</li>
    <li><b>Name</b> &mdash; anything your team will recognise, e.g. <code>DC Hub Intelligence</code>.
    <b>Description</b> &mdash; free text.</li>
    <li><b>Enter the full URL of the remote server</b> &mdash; <code>https://dchub.cloud/mcp</code>.
    No path suffix, no query string. Transport is Streamable HTTP and DC Hub speaks MCP protocol
    <code>2025-06-18</code>.</li>
    <li><b>Route traffic through Cloudflare Gateway</b> &mdash; a toggle. Leave it off unless you
    specifically want Gateway inspection; it adds a hop in front of a streaming endpoint.</li>
    <li><b>Authentication type</b> &mdash; <b>OAuth</b>, <b>Custom headers</b> or <b>None</b>.
    See the next section: OAuth with <b>Automatic (recommended)</b> credentials is what we suggest,
    and it is the choice we have verified end to end.</li>
    <li>Add the server to an <b>MCP server portal</b>, attach an Access application to that portal,
    and write at least one policy. <b>Policies are default-deny</b> &mdash; a portal with no policy
    admits nobody. Then hand your agents the portal URL in place of
    <code>https://dchub.cloud/mcp</code>.</li>
  </ol>
  <p>The dashboard labels and the API values differ: the form&rsquo;s <b>OAuth</b>,
  <b>Custom headers</b> and <b>None</b> are <code>oauth</code>, <code>bearer</code> and
  <code>unauthenticated</code> respectively in the API.</p>
  <p style="color:#64748b;font-size:.85rem">DC Hub is not affiliated with, sponsored by or endorsed
  by Cloudflare. This page documents a configuration verified against the live product on
  2026-09-02.</p>""",
    auth_html="""<div class="pane" id="auth">
  <h2>Choosing an authentication type</h2>
  <p>All three modes work against DC Hub. They differ in who the traffic looks like when it reaches
  us &mdash; which decides both the data your agents get back and whether you can attribute usage
  per seat.</p>

  <h3>OAuth &mdash; recommended</h3>
  <p>Nothing is shared: each portal user completes their own sign-in and gets a durable per-user
  identity. Under <b>OAuth credentials</b>, choose <b>Automatic (recommended)</b>.
  Cloudflare&rsquo;s own hint on that option says it works only where the provider supports OAuth
  Dynamic Client Registration, and that most production providers &mdash; it names GitHub, Snowflake
  and Asana &mdash; need <b>Manual credentials</b> instead. DC Hub supports DCR, so Automatic works
  and there is no client to pre-register:
  <code>https://dchub.cloud/.well-known/oauth-protected-resource</code> returns 200 and names an
  authorization server that publishes a <code>registration_endpoint</code> and PKCE
  <code>S256</code>, with scopes <code>openid</code>, <code>profile</code>, <code>email</code> and
  <code>offline_access</code>.</p>
  <p><b>Verified end to end on 2026-09-02:</b> a portal configured OAuth + Automatic completed
  registration and minted a durable per-user identity &mdash; observed server-side as an MCP session
  carrying a <code>dch_oauth_</code> key rather than <code>key=none</code>. Fresh identities start on
  the free tier until they are attached to your plan, so talk to us before you rely on it.</p>

  <h3>Custom headers</h3>
  <p>One admin-set credential, forwarded verbatim, shared by every portal user. Cloudflare passes
  custom headers through unchanged, so the header name you set is the header we read. DC Hub
  resolves credentials in the order <code>X-API-Key</code> &rarr;
  <code>Authorization: Bearer</code> &rarr; <code>?apiKey=</code>, and header names are
  case-insensitive &mdash; Cloudflare&rsquo;s <code>X-Api-Key</code> and our <code>X-API-Key</code>
  are the same slot.</p>
  <pre>{
  "headers": {
    "X-Api-Key": "&lt;your-dchub-key&gt;"
  }
}</pre>
  <p>Your paid tier then applies to the whole portal, and the whole org counts as <b>one caller</b>
  &mdash; there is no per-seat attribution in this mode.</p>

  <h3>None</h3>
  <p>No credential at all. Every call lands on the keyless free tier: trimmed previews, most result
  rows withheld. Fine for evaluation, not for production.</p>

  <h3>OAuth and headless agents are mutually exclusive</h3>
  <p>Per-user identity has a real cost. Cloudflare excludes OAuth-backed upstream servers from
  service-token sessions and requires a browser sign-in, so an autonomous agent running with no
  human present cannot reach DC Hub through an OAuth-configured portal entry. Choose by what you
  actually need: <b>OAuth</b> for per-seat attribution with humans in the loop, <b>Custom headers</b>
  for unattended agents. You cannot have both through one entry &mdash; though nothing stops you
  registering DC Hub twice, once each way.</p>
</div>""",
    extra_html=canon_text("""<div class="pane" id="portal-limits">
  <h2>Two documented limits of portal-based access</h2>
  <p>Both are Cloudflare&rsquo;s own documented behaviour, and both matter before you treat a portal
  as a control boundary:</p>
  <ol>
    <li>A user blocked by your Access policy <b>can still reach an upstream server directly by its
    own URL</b>. A portal governs how you distribute DC Hub; it does not seal DC Hub off &mdash; and
    <code>https://dchub.cloud/mcp</code> is a public endpoint by design.</li>
    <li>Independent MFA, purpose justification and temporary authentication are <b>not enforced</b>
    for MCP servers reached through a portal. If your controls depend on any of those, verify the
    behaviour yourself before you rely on it.</li>
  </ol>
</div>

<div class="pane" id="verify">
  <h2>Verify it before you announce it</h2>
  <p>Two checks. The first proves the server is reachable and complete; the second proves your
  credential is actually arriving.</p>
  <h3>1 &middot; Reachability</h3>
  <p>Once the server syncs, Cloudflare should show the full DC Hub surface &mdash;
  <b>{canon_tools} tools</b> and <b>13 prompts</b>, with <code>serverInfo</code> reporting
  <code>DC Hub Intelligence</code> <code>{canon_version}</code>. A lower tool count means a partial
  sync; re-sync before going further.</p>
  <h3>2 &middot; Credential passthrough</h3>
  <p>Call <code>search_facilities</code> with <code>query=Ashburn</code> and <code>limit=25</code>
  through your portal, then compare against an anonymous caller. The measured anonymous baseline on
  2026-09-02: <code>tier</code> is <code>free</code>, <code>data</code> carries 3 rows,
  <code>_data_total_in_pro</code> is 5, and <code>upgrade_url</code> is
  <code>https://dchub.cloud/pricing</code>. If your credential arrived, those free-tier markers are
  absent and the full result set comes back.</p>
  <p><b>An invalid key looks exactly like no key.</b> A rejected credential falls back to anonymous
  and produces output identical to sending nothing at all &mdash; so a passthrough test run with a
  placeholder value proves nothing. Test with a real key, and keep a no-credential run as the
  control.</p>
  <p>If you chose OAuth, the decisive evidence is not visible in your dashboard: it is the
  <code>dch_oauth_</code> key on the session, server-side. Ask us to read it for your first
  connection.</p>
</div>

<div class="pane" id="context-optimization">
  <h2>Leave context optimization off</h2>
  <p>Portals offer two token-saving modes. Both degrade DC Hub quietly rather than visibly &mdash;
  your agents keep answering, just less correctly.</p>
  <ul>
    <li><code>minimize_tools</code> &ldquo;strips tool descriptions and input schemas from all
    upstream tools, leaving only their names.&rdquo;</li>
    <li><code>search_and_execute</code> &ldquo;hides all upstream tools&rdquo; behind a generic
    search-then-execute pair.</li>
  </ul>
  <p>Both hurt here for the same reason: DC Hub publishes its own limits alongside its answers, and
  those contracts live in the tool schemas. <code>rank_sites</code>,
  <code>site_selection_canvas</code> and <code>get_power_availability_timeline</code> each return a
  <code>constraint_coverage</code> block naming what the answer does <i>not</i> cover;
  <code>get_composite_site_score</code> returns <code>coverage</code> and
  <code>coverage_ratio</code>. Strip the schemas and you keep the answers while discarding the
  caveats &mdash; the opposite of what an audited deployment wants.</p>
</div>

<div class="pane" id="what-we-see">
  <h2>What DC Hub sees, and what it does not</h2>
  <ul>
    <li>Under <b>Custom headers</b> every portal user reaches us on one credential, so we do not
    receive your users&rsquo; identities. If your compliance position needs per-user attribution on
    our side, that is the OAuth path above.</li>
    <li>A portal sync enumerates the full tool and prompt surface with no credential at all. Tool
    <i>discovery</i> is public on DC Hub; tool <i>data</i> is what the tier gate controls.</li>
    <li>Source IP is not usable for attribution in either direction &mdash; DC Hub&rsquo;s own edge
    is Cloudflare too.</li>
  </ul>
</div>"""),
)


@integrations_landing_bp.route("/integrations/bedrock", strict_slashes=False, methods=["GET"])
def integrations_bedrock():
    return BEDROCK_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/copilot-studio", strict_slashes=False, methods=["GET"])
def integrations_copilot_studio():
    return COPILOT_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/cloudflare", strict_slashes=False, methods=["GET"])
def integrations_cloudflare():
    # strict_slashes=False is load-bearing, not cosmetic: without it
    # /integrations/cloudflare/ falls through to main.py's legacy
    # /integrations/<platform>/ package handler, which answers
    # {"error": "Integration package not found for cloudflare"} with a 404.
    # Same reason every sibling on this blueprint sets it.
    return CLOUDFLARE_PORTAL_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/grok", strict_slashes=False, methods=["GET"])
def integrations_grok():
    return GROK_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/gemini", strict_slashes=False, methods=["GET"])
def integrations_gemini():
    return GEMINI_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/mistral", strict_slashes=False, methods=["GET"])
def integrations_mistral():
    return MISTRAL_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/perplexity", strict_slashes=False, methods=["GET"])
def integrations_perplexity():
    return PERPLEXITY_RECIPE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


@integrations_landing_bp.route("/integrations/mcp/data-center-mcp-server", strict_slashes=False, methods=["GET"])
def integrations_mcp_seo():
    return MCP_SEO_PAGE_HTML, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }


# ── WebMCP page tools (webmcp-proto, 2026-07-18) ────────────────────────────
# The MCP-server landing demos its own product: three page tools mirroring
# the MCP server's get_grid_scoreboard / search_facilities / rank_markets
# (short descriptions, same public REST). Absent unless env
# WEBMCP_ORIGIN_TRIAL_TOKEN is set; feature-detected no-op off-trial.
_WEBMCP_TOOLS = [
    {
        "name": "get-grid-scoreboard",
        "description": ("Live ranked scoreboard of US ISO grids: demand, fuel "
                        "mix, renewable share — right now. Same data as the "
                        "DC Hub MCP tool get_grid_scoreboard; no key needed. "
                        "No parameters."),
        "schema": {"type": "object", "properties": {}},
        "js_body": "return api('/api/v1/iso/comparison');",
    },
    {
        "name": "search-datacenter-facilities",
        "description": (canon_text("Search DC Hub's live database of {canon_facilities} distinct data-center "
                        "facilities by city, country or operator. Mirrors the "
                        "MCP tool search_facilities (lite).")),
        "schema": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "City, state, country or operator, e.g. \"ashburn\""},
            "limit": {"type": "number",
                      "description": "Max results (1-25, default 10)"}},
            "required": ["query"]},
        "js_body": ("var q=(input&&input.query)?String(input.query):'';"
                    "if(!q)return 'Missing required \"query\" — pass a city, "
                    "country, or operator, e.g. {\"query\":\"ashburn\"}.';"
                    "var lim=Math.max(1,Math.min(25,Number(input&&input.limit)||10));"
                    "return api('/api/v1/search?q='+encodeURIComponent(q)+'&limit='+lim);"),
    },
    {
        "name": "rank-datacenter-markets",
        "description": ("Rank the best data-center markets right now from DC "
                        "Hub's live DCPI scoring (grid headroom, fiber, "
                        "pipeline, pricing). Mirrors the MCP tool rank_markets."),
        "schema": {"type": "object", "properties": {
            "limit": {"type": "number",
                      "description": "How many top markets (1-25, default 10)"}}},
        "js_body": ("var lim=Math.max(1,Math.min(25,Number(input&&input.limit)||10));"
                    "return api('/api/v1/mcp/tools/rank_markets?limit='+lim);"),
    },
]


@integrations_landing_bp.route("/integrations/mcp", strict_slashes=False, methods=["GET"])
@integrations_landing_bp.route("/integrations", strict_slashes=False, methods=["GET"])
def integrations_mcp():
    return _webmcp_inject(MCP_LANDING_HTML, _WEBMCP_TOOLS), 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=1800",
    }
