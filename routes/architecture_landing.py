"""
architecture_landing.py — public Platform Architecture page.

Phase ZZZZZ-round47.2 (2026-05-25). /architecture was referenced by
nav_config_routes.py, site_sentinel.py, brain_consistency_radar.py
as a canonical public page, but no route ever served it — 404 every
visit. Pages worker even has its own OG metadata for the path:

  /architecture: 'Platform Architecture | DC Hub' — How DC Hub aggregates
                  intelligence from 50,000+ facilities across 140+ countries.

This blueprint fills the gap with a content-rich, SEO-indexed page
documenting how DC Hub works under the hood. Live counts are pulled
in-page so the numbers don't drift.
"""
import datetime
from flask import Blueprint

architecture_bp = Blueprint("architecture_landing", __name__)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Platform Architecture | DC Hub</title>
<meta name="description" content="How DC Hub aggregates real-time intelligence from 20,000+ data center facilities across 170+ countries — ingestion pipelines, ISO grid feeds, DCPI scoring, MCP surface, and AI-agent integration.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/architecture">
<meta property="og:title" content="Platform Architecture — DC Hub">
<meta property="og:description" content="The data pipelines, scoring engine, and AI-agent integration layer behind DC Hub's coverage of global data-center infrastructure.">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-architecture.png">
<style>
 body{max-width:960px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.6;color:#0f172a}
 h1{font-size:2.2rem;margin:.3em 0;letter-spacing:-.02em}
 h2{font-size:1.35rem;margin:1.6em 0 .5em;letter-spacing:-.01em;color:#1e293b}
 h3{font-size:1.05rem;margin:1.2em 0 .4em;color:#334155}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}
 .lead{color:#475569;font-size:1.08rem;max-width:780px;margin-bottom:24px}
 .pane{background:#f8fafc;border:1px solid #e2e8f0;padding:18px 22px;border-radius:10px;margin:20px 0}
 .pane.dark{background:#0f172a;color:#e2e8f0;border-color:#334155}
 .pane.dark h2,.pane.dark h3{color:#fff}
 .pane code{background:#fff;color:#3730a3}
 .pane.dark code{background:#1e293b;color:#a5b4fc}
 code{background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-family:ui-monospace,monospace;font-size:.88em}
 ul{padding-left:22px}
 li{margin:.3em 0}
 table{width:100%;border-collapse:collapse;margin:14px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04)}
 th{background:#0f172a;color:#fff;text-align:left;padding:8px 12px;font-size:.82rem}
 td{padding:10px 12px;border-top:1px solid #e2e8f0;font-size:.95rem;vertical-align:top}
 .pill{display:inline-block;background:#dcfce7;color:#15803d;padding:2px 8px;border-radius:3px;font-size:.78rem;margin-left:6px}
 .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:16px 0}
 .stat{background:#f8fafc;border:1px solid #e2e8f0;padding:14px;border-radius:8px;text-align:center}
 .stat-num{font-size:1.65rem;font-weight:700;color:#6366f1;letter-spacing:-.02em}
 .stat-label{color:#64748b;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
 .footer{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}
 .footer a{color:#6366f1;text-decoration:none}
 .footer a:hover{text-decoration:underline}
</style></head><body>
<div class="eyebrow">DC Hub · Platform Architecture</div>
<h1>How DC Hub Works</h1>
<p class="lead">DC Hub is a real-time intelligence platform for data center infrastructure: 20,000+ facilities,
170+ countries, 286 power markets scored daily, $324B+ in M&amp;A deals tracked, and an MCP server that lets
AI agents query all of it directly. Here's how it fits together.</p>

<div class="stat-grid" id="live-stats">
  <div class="stat"><div class="stat-num" id="s-facilities">21,000+</div><div class="stat-label">Facilities</div></div>
  <div class="stat"><div class="stat-num" id="s-countries">170+</div><div class="stat-label">Countries</div></div>
  <div class="stat"><div class="stat-num" id="s-markets">286</div><div class="stat-label">DCPI markets</div></div>
  <div class="stat"><div class="stat-num" id="s-tools">23+</div><div class="stat-label">MCP tools</div></div>
  <div class="stat"><div class="stat-num" id="s-gw">369 GW</div><div class="stat-label">Pipeline tracked</div></div>
</div>

<h2>1. Data ingestion — 18 ISO feeds + 11 federal sources</h2>
<p>DC Hub ingests grid-level data from <b>18 Independent System Operators</b> covering the US (PJM, ERCOT, CAISO,
MISO, SPP, NYISO, ISO-NE, TVA, SOCO, FRCC, BPA) plus international ISOs (AESO Alberta, Hydro-Québec, Nord Pool
across 15 Nordic + Baltic zones). Federal data flows in from EIA-860, HIFLD, FERC, FCC Form 477, and ArcGIS
FeatureServers. PeeringDB provides carrier facility presence. OSM Overpass supplements fiber routes where
state KMZ sources are thin.</p>

<table>
 <thead><tr><th>Source class</th><th>What we pull</th><th>Refresh</th></tr></thead>
 <tbody>
  <tr><td>ISO grid feeds</td><td>Hourly load, generation mix, interconnect queue</td><td>5-60 min</td></tr>
  <tr><td>EIA-860</td><td>Power plant inventory, capacity, fuel</td><td>Daily</td></tr>
  <tr><td>HIFLD</td><td>Substations, transmission lines, gas pipelines</td><td>Weekly</td></tr>
  <tr><td>PeeringDB + ArcGIS</td><td>Carrier facility presence, fiber waypoints</td><td>Daily</td></tr>
  <tr><td>News + filings</td><td>$1B+ hyperscaler deals, M&amp;A, expansion announcements</td><td>4× daily</td></tr>
  <tr><td>IPinfo + Clearbit</td><td>Visitor enrichment for company-level analytics</td><td>Real-time</td></tr>
 </tbody>
</table>

<h2>2. The DCPI scoring engine</h2>
<p>The <a href="/dcpi"><b>DC Hub Power Index</b></a> ranks 286 US + international markets on a 0–100 composite:
spare power capacity, ISO interconnect time-to-power, grid constraint, operator depth, fiber depth, and
demand pressure. Markets get one of four verdicts: <code>BUILD</code> (14 today), <code>CAUTION</code> (141),
<code>AVOID</code> (63), or <code>LOW_SIGNAL</code> (67). Today's top BUILD: Cheyenne, WY. Today's top AVOID:
Northern Virginia.</p>
<p>International coverage: <a href="/dcpi/intl">AESO + Hydro-Québec + Nord Pool</a>.</p>

<h2>3. MCP server — 23+ tools for AI agents</h2>
<p>The <a href="/mcp">DC Hub MCP server</a> exposes the full intelligence catalog to Claude, ChatGPT, Cursor,
Cline, and every other MCP-compatible agent. Tools include <code>search_facilities</code>,
<code>get_grid_intelligence</code>, <code>compare_sites</code>, <code>get_pipeline</code>,
<code>hyperscaler_deals</code>, <code>ai_capacity_index</code>, <code>get_renewable_energy</code>,
<code>get_water_risk</code>, <code>get_tax_incentives</code>, and more. Tier ladder: free 5 calls/day,
Starter $9/mo, Developer $49/mo, Pro $199/mo, Enterprise custom.</p>

<div class="pane dark">
<h3 style="margin-top:0">Live endpoints</h3>
<ul style="margin-bottom:0">
 <li><code>POST https://dchub.cloud/mcp</code> — JSON-RPC initialize / tools/list / tools/call</li>
 <li><code>GET  https://dchub.cloud/.well-known/mcp-server.json</code> — server descriptor</li>
 <li><code>GET  https://dchub.cloud/.well-known/mcp-tools.json</code> — auto-generated tool manifest</li>
 <li><code>GET  https://dchub.cloud/api/v1/ai-agents.json</code> — canonical AI-agent integration map</li>
</ul>
</div>

<h2>4. Stack — multi-cloud, fail-over by design</h2>
<table>
 <thead><tr><th>Layer</th><th>Tech</th><th>Notes</th></tr></thead>
 <tbody>
  <tr><td>Edge</td><td>Cloudflare Pages + Workers</td><td>KV cache + stale-while-error failover</td></tr>
  <tr><td>API</td><td>Flask + Gunicorn on Railway</td><td>Primary; sub-100ms WSGI fast-path for health probes</td></tr>
  <tr><td>Failover</td><td>Render</td><td>Cold-standby with strict circuit-breaker promotion</td></tr>
  <tr><td>Database</td><td>Neon PostgreSQL</td><td>Single source of truth; HTTP API for serverless reads</td></tr>
  <tr><td>MCP server</td><td>Node.js (separate Railway service)</td><td>Streamable-HTTP transport, 5-min auth keyCache</td></tr>
  <tr><td>Brain v2</td><td>Claude Opus 4.7</td><td>Layer 4 text replacement + Layer 22 auto-code; running 80+ learning cycles</td></tr>
 </tbody>
</table>

<h2>5. The Brain — autonomous improvement loop</h2>
<p>Brain v2 audits the platform on a 5-minute cycle: probes every public surface, detects regressions,
proposes fixes, and (within scoped safety constraints) ships them. Layers 1–11 cover detection + diagnosis;
layers 14, 20, 22, 23 cover causal analysis, durability, code-emission, and lifecycle curation. Layer 22
is the only writer; everything else is read-only.</p>

<p class="footer">
<a href="/">Home</a> · <a href="/dcpi">DCPI</a> · <a href="/mcp">MCP</a> · <a href="/transparency">Live ops</a>
· <a href="/coverage">Coverage map</a> · <a href="/pricing">Pricing</a> · <a href="/api-docs">API docs</a>
· Rendered __DATE__
</p>
</body></html>"""


@architecture_bp.route("/architecture", methods=["GET"], strict_slashes=False)
def architecture():
    html = _TEMPLATE.replace("__DATE__", datetime.datetime.utcnow().strftime("%B %d, %Y"))
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.2-architecture",
    }
