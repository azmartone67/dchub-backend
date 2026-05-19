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


# AUTO-REPAIR: duplicate route '/vs' also in routes/bs_translator.py:272 — review and remove one
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
- Live grid telemetry: 11 ISOs (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE, AESO, IESO, BPA, TVA)
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

# AUTO-REPAIR: duplicate route '/AGENTS.md' also in ai_discovery_routes.py:290 — review and remove one

@quick_redirects_bp.route("/AGENTS.md", methods=["GET"])
def agents_md():
    """Agent-discovery manifest. Standard convention for AI agents
    finding integration docs at /AGENTS.md."""
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
