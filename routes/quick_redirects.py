"""
Phase ZZZZ-redirects (2026-05-18) — index-page redirects + canonical
handlers for paths the second audit dashboard flagged as 404.

  /vs                 → /vs/dchawk
  /industry           → /industry/pulse
  /competitive        → /vs/dchawk
  /dcpi/methodology   → /dcpi (hash anchor #methodology)
  /AGENTS.md          → served Markdown agent manifest
  /iso/<iso>.json     → 301 to /api/v1/grid/<iso> (canonical ISO data)
"""

from flask import Blueprint, redirect, Response, jsonify

quick_redirects_bp = Blueprint("quick_redirects", __name__)


# AUTO-REPAIR: duplicate route '/vs' also in routes/bs_translator.py:276 — review and remove one
@quick_redirects_bp.route("/vs", methods=["GET"], strict_slashes=False)
def vs_index_redirect():
    return redirect("/vs/dchawk", code=301)


@quick_redirects_bp.route("/industry", methods=["GET"], strict_slashes=False)
def industry_index_redirect():
    return redirect("/industry/pulse", code=301)


@quick_redirects_bp.route("/competitive", methods=["GET"], strict_slashes=False)
def competitive_redirect():
    return redirect("/vs/dchawk", code=301)


@quick_redirects_bp.route("/dcpi/methodology", methods=["GET"],
                            strict_slashes=False)
def dcpi_methodology_redirect():
    """DCPI methodology lives as an anchor on the /dcpi page; this
    redirect resolves the bare /dcpi/methodology link the audit was 404'ing."""
    return redirect("/dcpi#methodology", code=301)


# AGENTS.md — agent-discovery manifest. Standard pattern for AI agents
# to find machine-readable instructions. The audit dashboard probes
# this URL because it's a known AI-coordination convention.
_AGENTS_MD = """# AGENTS.md — DC Hub

DC Hub is a real-time data center intelligence platform. AI agents can
integrate via MCP (Model Context Protocol) or direct REST.

## MCP Server

- Endpoint: `https://dchub.cloud/mcp` (streamable-http)
- Manifest: `https://dchub.cloud/.well-known/mcp.json`
- 40 tools across 4 tiers (FREE / IDENTIFIED / DEVELOPER / PRO)
- Pricing: free tier (1 row teaser), $9/mo (500 calls/day, full data),
  $199/mo (10k calls/day + multi-site comparator)

## REST API

- Base: `https://dchub.cloud/api/v1/`
- OpenAPI spec: `https://dchub.cloud/api/v1/openapi.json`
- Auth: `X-API-Key: <key>` header
- Claim a free dev key in one call:
  `POST https://dchub.cloud/api/v1/keys/claim` with body
  `{"client_name": "<your agent name>"}`

## Citation-clean weekly stat sheet

- HTML: `https://dchub.cloud/industry/pulse`
- JSON: `https://dchub.cloud/api/v1/industry/pulse`
- License: CC-BY-4.0 (free to cite with attribution)
- Schema.org Dataset markup embedded

## Live AI citation telemetry

- See which AI platforms call us live: `https://dchub.cloud/cited-by`

## What we track

- 21,000+ data center facilities, 280+ markets, 178 countries
- $324B+ M&A history (live + autopilot-curated)
- DCPI scores: BUILD/CAUTION/AVOID verdicts for 280 markets
- Live grid telemetry: 10 grid operators (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE, IESO, BPA, TVA) + 43 US utility balancing authorities
- 50,000+ fiber routes, 126,000 substations, 52,000 transmission lines
- 1,000+ active DC pipeline projects

## Competitive positioning

- vs DCHawk:  https://dchub.cloud/vs/dchawk
- vs DC Byte: https://dchub.cloud/vs/dcbyte
- vs CBRE:    https://dchub.cloud/vs/cbre
- vs JLL:     https://dchub.cloud/vs/jll

## Contact

- Partnerships: partnerships@dchub.cloud
- API support:  See /api-docs

DC Hub is the live, MCP-native alternative to static research (DCHawk,
dcByte, DC Knowledge). No quarterly PDFs, no $25K contracts, no NDAs —
just live JSON updated every 60 seconds.
"""


# Phase ZZZZZ-round6 (2026-05-23): /AGENTS.md is handled canonically
# by ai_agent_discovery.py:288 (loads from the live AGENTS.md file
# with a fallback) — that's the version registered first in Flask, so
# /AGENTS.md probes route there. Removing this duplicate registration
# kills the "shadowed route" startup warning. The _AGENTS_MD constant
# above stays defined as data; serve it under a clearly-distinct path.
@quick_redirects_bp.route("/AGENTS-inline.md", methods=["GET"])
def agents_md():
    """Inline copy of AGENTS.md — fallback when the file-loader
    handler in ai_agent_discovery.py is unavailable."""
    return Response(_AGENTS_MD, mimetype="text/markdown",
                    headers={"Cache-Control": "public, max-age=3600"})


@quick_redirects_bp.route("/iso/<iso>.json", methods=["GET"])
def iso_legacy_json(iso):
    """Legacy path some external pollers (including our own audit
    dashboard) hit. Canonical home is /api/v1/grid/<iso>; redirect
    keeps both alive."""
    return redirect(f"/api/v1/grid/{iso.lower()}", code=301)


@quick_redirects_bp.route("/iso/<iso>", methods=["GET"])
def iso_legacy(iso):
    return redirect(f"/api/v1/grid/{iso.lower()}", code=301)


# r43-SEO (2026-05-30): /iso (bare) and /iso/ used to return CF Error 1000
# because the path was in _routes.json but no Flask route matched. That
# wiped the entire "ISO" SEO surface. This landing page returns proper
# HTML with Dataset + BreadcrumbList JSON-LD and direct links into the
# 7 major US ISOs plus DCPI for cross-traffic.
_ISO_INDEX_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub · ISO Index — PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, SPP</title>
<meta name="description" content="ISO landing page: live grid data, interconnection queue, and data-center-suitable headroom for every major US grid operator. PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, SPP.">
<link rel="canonical" href="https://dchub.cloud/iso">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Dataset","name":"DC Hub ISO Index — Per-Region Grid Snapshot",
  "alternateName":["ISO Index","US ISO Grid Data"],
  "description":"Live snapshots of every major US ISO and balancing authority: PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, SPP. Covers interconnection queue depth, current load vs. firm capacity, fuel mix, and data-center-suitable headroom per market.",
  "url":"https://dchub.cloud/iso","sameAs":"https://dchub.cloud/iso",
  "creator":{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"},
  "publisher":{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"},
  "keywords":"ISO, PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, SPP, grid intelligence, data center power, energy intelligence, interconnection queue",
  "isAccessibleForFree":true,
  "spatialCoverage":{"@type":"Place","name":"United States"},
  "temporalCoverage":"2024-01-01/..",
  "distribution":[
   {"@type":"DataDownload","encodingFormat":"application/json","contentUrl":"https://dchub.cloud/api/v1/grid-intelligence","name":"All ISO regions"},
   {"@type":"DataDownload","encodingFormat":"application/json","contentUrl":"https://dchub.cloud/api/v1/iso/pjm/snapshot","name":"PJM snapshot"},
   {"@type":"DataDownload","encodingFormat":"application/json","contentUrl":"https://dchub.cloud/api/v1/iso/ercot/snapshot","name":"ERCOT snapshot"}]},
 {"@type":"BreadcrumbList","itemListElement":[
  {"@type":"ListItem","position":1,"name":"DC Hub","item":"https://dchub.cloud/"},
  {"@type":"ListItem","position":2,"name":"Intelligence","item":"https://dchub.cloud/intelligence"},
  {"@type":"ListItem","position":3,"name":"ISO Index","item":"https://dchub.cloud/iso"}]}
]}
</script>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;background:#0a0a12;color:#e6e9f0;margin:0;line-height:1.65}
.container{max-width:1100px;margin:0 auto;padding:32px 24px}
header{margin:24px 0 32px}
.eyebrow{color:#818cf8;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:8px}
h1{font-size:2.4rem;margin:0 0 12px;letter-spacing:-.02em;color:#fafafa}
.lede{color:#a1a1aa;font-size:1.05rem;max-width:720px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:28px 0}
.card{background:#11121a;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:18px 20px;text-decoration:none;color:inherit;transition:.15s}
.card:hover{border-color:#818cf8;transform:translateY(-2px)}
.card .iso{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:.78rem;color:#818cf8;letter-spacing:.06em}
.card h3{font-size:1.05rem;margin:6px 0 4px;color:#fafafa}
.card p{font-size:.85rem;color:#a1a1aa;margin:0}
.context{background:#11121a;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:24px;margin:32px 0}
.context h2{font-size:1.2rem;margin:0 0 12px;color:#fafafa}
.context p{color:#a1a1aa;margin:0 0 12px}
.context a{color:#818cf8}
.foot{margin-top:24px;padding-top:18px;border-top:1px solid rgba(255,255,255,.08);color:#71717a;font-size:.82rem}
.foot a{color:#818cf8;text-decoration:none}
</style></head><body>
<div class="container">
<header>
  <div class="eyebrow">Energy intelligence · ISO index</div>
  <h1>The seven US ISOs, one screen</h1>
  <p class="lede">Live grid snapshots for every major US ISO and balancing authority — interconnection queue depth, current load, firm capacity, fuel mix, and the headroom that determines whether a 200 MW data center can land in this study cycle.</p>
</header>

<div class="grid">
  <a class="card" href="/grid/pjm"><div class="iso">PJM</div><h3>PJM Interconnection</h3><p>13 states + DC · largest US grid operator</p></a>
  <a class="card" href="/grid/ercot"><div class="iso">ERCOT</div><h3>Electric Reliability Council of Texas</h3><p>Texas-only · 90% of state load</p></a>
  <a class="card" href="/grid/caiso"><div class="iso">CAISO</div><h3>California ISO</h3><p>California + parts of NV · highest renewables share</p></a>
  <a class="card" href="/grid/miso"><div class="iso">MISO</div><h3>Midcontinent ISO</h3><p>15 states · Manitoba · largest geographic footprint</p></a>
  <a class="card" href="/grid/nyiso"><div class="iso">NYISO</div><h3>New York ISO</h3><p>NY State · constrained zones drive premium pricing</p></a>
  <a class="card" href="/grid/isone"><div class="iso">ISO-NE</div><h3>ISO New England</h3><p>6 New England states · winter-peaking gas exposure</p></a>
  <a class="card" href="/grid/spp"><div class="iso">SPP</div><h3>Southwest Power Pool</h3><p>14 states · highest wind penetration in US</p></a>
</div>

<section class="context">
  <h2>How DC Hub uses ISO data</h2>
  <p>Every data-center site-selection question eventually narrows to one ISO. The ISO sets the rules for interconnection studies, the queue you wait in, the capacity-payment regime you'll be exposed to, and — through fuel mix and headroom — the marginal cost of every megawatt-hour you consume. DC Hub ingests live operator data from all seven major US ISOs (PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, SPP) plus three international peers (AESO, Hydro-Québec, Nord Pool) and recomputes per-region capacity, queue depth, and headroom on a continuous cycle.</p>
  <p>The per-ISO signals feed two derived surfaces: the <a href="/dcpi">Data Center Power Index (DCPI)</a> — a 0–100 score that ranks 200+ US markets as BUILD, CAUTION, AVOID, or LOW_SIGNAL — and the <a href="/grid-intelligence">grid intelligence dashboard</a>, which exposes the underlying capacity-factor and queue-position data for direct download. For market-level analysis, every <a href="/markets">DC Hub market page</a> links back to its serving ISO so the site-selection and energy-intelligence views stay coupled.</p>
  <p>Machine-readable JSON is at <code>/api/v1/grid-intelligence</code> for the full list, <code>/api/v1/iso/&lt;code&gt;/snapshot</code> for a single ISO, or via the <a href="/mcp">DC Hub MCP server</a> for agents.</p>
</section>

<div class="foot">
  Canonical: <a href="https://dchub.cloud/iso">/iso</a> ·
  Full grid dashboard: <a href="/grid-intelligence">/grid-intelligence</a> ·
  Per-market scores: <a href="/dcpi">/dcpi</a> ·
  Markets index: <a href="/markets">/markets</a>
</div>
</div>
</body></html>'''

@quick_redirects_bp.route("/iso", methods=["GET"], strict_slashes=False)
def iso_index_landing():
    """Public landing page for the ISO surface. Renders HTML with
    JSON-LD Dataset + BreadcrumbList so Google can index 'ISO' and
    'energy intelligence' queries against a real page rather than the
    JSON redirect that used to live here."""
    return Response(
        _ISO_INDEX_HTML,
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=600"},
    )
