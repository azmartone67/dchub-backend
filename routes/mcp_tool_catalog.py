"""Phase UU (2026-05-16) — public MCP tool catalog.

GET /mcp/tools             public HTML catalog (human-readable)
GET /api/v1/mcp/tools.json  machine-readable manifest (LLM-friendly)

Until now, the only complete listing of our 27 MCP tools was the
OpenAPI spec — not a page that an indexing crawler or a "best MCP
servers" directory could link to. This blueprint exposes both an
HTML page and a JSON manifest with example invocations per tool,
so external indexes (Glama, Perplexity, Gemini's tool registry)
have something concrete to point at.

The manifest is sourced from a single TOOLS list below — keeping it
hand-curated rather than introspecting the FastMCP runtime means
this file is deployable independently and won't crash if the runtime
changes shape.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, Response


mcp_tool_catalog_bp = Blueprint("mcp_tool_catalog", __name__)


# Categories — drives the visual grouping on the HTML page and the
# `category` field in the JSON manifest.
_CATEGORIES = [
    ("decision",      "Decision tools",      "Tools that DECIDE — given criteria, which markets/sites are best."),
    ("intelligence",  "Market intelligence", "Tools that DESCRIBE — facts about specific markets, facilities, deals."),
    ("infrastructure","Infrastructure",      "Grid, fiber, water, tax, energy — the physical-layer signals."),
    ("portfolio",     "Portfolio + search",  "Facility-level search, portfolio analytics, semantic discovery."),
    ("diagnostic",    "Diagnostic",          "Tools that EXPLAIN — why a market moved, why a score shifted."),
]


# Tool catalog. Each entry: (name, category, tier, summary, example_invocation).
# tier values: "free", "identified" (email-verified), "developer", "pro"
TOOLS = [
    # ── DECISION ──
    ("recommend_market",      "decision",       "identified",
     "Rank DCPI markets against capacity/deadline/water/retail-rate criteria",
     'recommend_market(capacity_mw=200, deadline_months=18, water_stress_max=3, iso="PJM", top_n=3)'),
    ("compare_markets",       "decision",       "free",
     "Side-by-side multi-market diff with per-dimension winners",
     'compare_markets(slugs="northern-virginia,phoenix,atlanta")'),
    ("simulate_buildout",     "decision",       "identified",
     "10-yr TCO envelope: capex/opex/power/tax/risk with sensitivity",
     'simulate_buildout(lat=39.04, lon=-77.48, state="VA", capacity_mw=100, redundancy="N+1")'),
    ("get_dchub_recommendation","decision",     "pro",
     "AI-formatted location recommendation for a site brief",
     'get_dchub_recommendation()'),
    # ── INTELLIGENCE ──
    ("get_market_intel",      "intelligence",   "free",
     "Describe one DCPI market — verdict, scores, risks, opportunities",
     'get_market_intel(market="northern-virginia")'),
    ("get_intelligence_index","intelligence",   "identified",
     "Composite intelligence index across all markets",
     'get_intelligence_index()'),
    ("get_news",              "intelligence",   "free",
     "Recent industry news, optionally filtered by market or company",
     'get_news(limit=10)'),
    ("get_pipeline",          "intelligence",   "identified",
     "540+ construction-pipeline projects (369 GW tracked)",
     'get_pipeline(market="virginia")'),
    ("list_transactions",     "intelligence",   "free",
     "$324B+ tracked M&A transactions — search by buyer/seller/year",
     'list_transactions(year=2025)'),
    ("get_agent_registry",    "intelligence",   "identified",
     "Inter-agent citation events — who cited DC Hub data and when",
     'get_agent_registry()'),
    # ── INFRASTRUCTURE ──
    ("get_grid_data",         "infrastructure", "free",
     "Real-time ISO grid metrics — PJM/ERCOT/CAISO/MISO/SPP/NYISO/ISO-NE",
     'get_grid_data(iso="PJM")'),
    ("get_grid_intelligence", "infrastructure", "pro",
     "Curated grid intelligence — substations, voltage classes, queue depth",
     'get_grid_intelligence(region_id="virginia")'),
    ("get_grid_headroom",     "infrastructure", "identified",
     "Available interconnection MW at substations near a site",
     'get_grid_headroom(lat=39.04, lon=-77.48, state="VA", radius_km=25)'),
    ("get_fiber_intel",       "infrastructure", "pro",
     "Dark fiber routes and carrier networks for a geography",
     'get_fiber_intel(market="ashburn")'),
    ("get_water_risk",        "infrastructure", "free",
     "WRI Aqueduct water stress + drought + flood per coordinate",
     'get_water_risk(lat=33.45, lon=-112.07, state="AZ")'),
    ("get_energy_prices",     "infrastructure", "identified",
     "EIA retail rates per state/sector, ¢/kWh",
     'get_energy_prices(state="VA", data_type="industrial")'),
    ("get_renewable_energy",  "infrastructure", "identified",
     "Renewable generation/curtailment per state",
     'get_renewable_energy(state="TX", energy_type="solar")'),
    ("get_tax_incentives",    "infrastructure", "free",
     "Data-center tax incentives per US state",
     'get_tax_incentives(state="VA")'),
    ("get_geothermal_potential","infrastructure","identified",
     "NLR geothermal viability for a site",
     'get_geothermal_potential(lat=39.74, lon=-105.17, state="CO")'),
    ("get_microgrid_viability","infrastructure","identified",
     "On-site generation + storage feasibility scoring",
     'get_microgrid_viability(lat=39.04, lon=-77.48, state="VA", capacity_mw=50)'),
    ("get_colocation_score",  "infrastructure", "identified",
     "Site fit score for renewable-colocated data center",
     'get_colocation_score(lat=33.43, lon=-112.07, state="AZ")'),
    ("get_infrastructure",    "infrastructure", "free",
     "Generic infrastructure lookup at a coordinate",
     'get_infrastructure(lat=39, lon=-77)'),
    ("get_air_permitting",    "infrastructure", "identified",
     "EPA air permitting risk envelope for capacity",
     'get_air_permitting(lat=39, lon=-77, capacity_mw=100)'),
    # ── PORTFOLIO + SEARCH ──
    ("search_facilities",     "portfolio",      "free",
     "Search 21,319 data center facilities by name, market, capacity",
     'search_facilities(market="ashburn", min_mw=10)'),
    ("search_facilities_semantic","portfolio",  "free",
     "Vector similarity search over facility descriptions",
     'search_facilities_semantic(q="30 MW with PJM access", topK=5)'),
    ("get_facility",          "portfolio",      "free",
     "Full record for one facility by slug or ID",
     'get_facility(slug="qts-ashburn")'),
    ("analyze_site",          "portfolio",      "pro",
     "Composite analysis for any lat/lon — power, fiber, risk, climate",
     'analyze_site(lat=39.04, lon=-77.48)'),
    ("compare_sites",         "portfolio",      "pro",
     "Side-by-side site analysis across multiple coordinates",
     'compare_sites(locations="39.04,-77.48;33.45,-112.07")'),
    ("get_backup_status",     "portfolio",      "identified",
     "Backup status across operational facilities",
     'get_backup_status()'),
    # ── DIAGNOSTIC ──
    ("explain_market_move",   "diagnostic",     "free",
     "Why a DCPI market's verdict/scores shifted — ranked deltas + news",
     'explain_market_move(slug="phoenix", window_days=30)'),
]


def _build_manifest() -> dict:
    by_cat: dict[str, list] = {c[0]: [] for c in _CATEGORIES}
    for name, cat, tier, summary, example in TOOLS:
        by_cat.setdefault(cat, []).append({
            "name":     name,
            "category": cat,
            "tier":     tier,
            "summary":  summary,
            "example":  example,
            "docs":     f"https://dchub.cloud/mcp/tools#{name}",
        })
    return {
        "version":   "2026-05-16",
        "transport": "streamable-http",
        "endpoint":  "https://dchub.cloud/mcp",
        "auth":      "X-API-Key header OR Authorization: Bearer <key>",
        "claim_endpoint": {
            "method": "POST",
            "url":    "https://dchub.cloud/api/v1/keys/claim",
            "body":   {"client_name": "<your agent name>"},
            "returns": "{api_key, tier, daily_calls}",
        },
        "categories": [
            {"id": c[0], "label": c[1], "description": c[2]} for c in _CATEGORIES
        ],
        "tool_count": len(TOOLS),
        "tools": {cat: by_cat.get(cat, []) for cat in by_cat},
    }


@mcp_tool_catalog_bp.route("/api/v1/mcp/tools.json", methods=["GET", "OPTIONS"])
def api_tool_manifest():
    if "OPTIONS" == (__import__("flask").request.method):
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        return resp, 200
    resp = jsonify(_build_manifest())
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@mcp_tool_catalog_bp.route("/mcp/tools", methods=["GET"])
@mcp_tool_catalog_bp.route("/mcp/tools/", methods=["GET"])
def html_tool_catalog():
    """Human-readable catalog page. Designed to be linkable from "best
    MCP servers" directories and indexable by Google/Perplexity/Gemini."""
    manifest = _build_manifest()

    cat_blocks = []
    for cat_meta in manifest["categories"]:
        cat_id = cat_meta["id"]
        cat_tools = manifest["tools"].get(cat_id, [])
        if not cat_tools:
            continue
        rows = []
        for t in cat_tools:
            tier_badge = {
                "free":       '<span class="tier free">FREE</span>',
                "identified": '<span class="tier identified">IDENTIFIED</span>',
                "developer":  '<span class="tier developer">DEVELOPER</span>',
                "pro":        '<span class="tier pro">PRO</span>',
            }.get(t["tier"], f'<span class="tier">{t["tier"]}</span>')
            rows.append(
                f'<tr id="{t["name"]}">'
                f'<td><code>{t["name"]}</code> {tier_badge}</td>'
                f'<td>{t["summary"]}</td>'
                f'<td><code class="example">{t["example"]}</code></td>'
                f'</tr>'
            )
        cat_blocks.append(
            f'<section>'
            f'<h2>{cat_meta["label"]}</h2>'
            f'<p class="cat-desc">{cat_meta["description"]}</p>'
            f'<table><thead><tr><th>Tool</th><th>What it does</th><th>Example</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            f'</section>'
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub MCP Server — {manifest['tool_count']} tools for data-center intelligence</title>
<meta name="description" content="Complete catalog of {manifest['tool_count']} MCP tools exposed by dchub.cloud — decision tools (recommend_market, compare_markets, simulate_buildout, explain_market_move), market intelligence, grid/fiber/water infrastructure, portfolio search. Free dev key claimable in 30 seconds.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/mcp/tools">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1f2937;line-height:1.6}}
  h1{{margin:0 0 .25rem;font-size:2rem}}
  h1 + p{{color:#6b7280;margin:0 0 2rem}}
  h2{{margin-top:2.5rem;font-size:1.3rem;border-bottom:1px solid #e5e7eb;padding-bottom:.25rem}}
  .cat-desc{{color:#6b7280;margin:.25rem 0 1rem}}
  table{{width:100%;border-collapse:collapse;font-size:.92rem}}
  th,td{{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #f3f4f6;vertical-align:top}}
  th{{background:#f9fafb;font-weight:600;color:#374151}}
  code{{font-family:Menlo,Consolas,monospace;font-size:.88em;background:#f3f4f6;padding:1px 5px;border-radius:3px}}
  code.example{{display:block;background:#0f172a;color:#a7f3d0;padding:.4rem .6rem;border-radius:4px;white-space:pre-wrap}}
  .tier{{display:inline-block;font-size:.65rem;padding:1px 6px;border-radius:3px;
         margin-left:.4rem;vertical-align:middle;font-weight:600}}
  .tier.free{{background:#dcfce7;color:#166534}}
  .tier.identified{{background:#dbeafe;color:#1e40af}}
  .tier.developer{{background:#fef3c7;color:#92400e}}
  .tier.pro{{background:#fce7f3;color:#9d174d}}
  .quick-start{{background:#0f172a;color:#e2e8f0;padding:1rem 1.25rem;border-radius:6px;margin:1.5rem 0}}
  .quick-start code{{background:#1e293b;color:#a7f3d0}}
  .quick-start pre{{margin:.5rem 0 0;white-space:pre-wrap;font-size:.85rem}}
</style>
</head>
<body>
<h1>DC Hub MCP Server</h1>
<p><strong>{manifest['tool_count']} tools</strong> for data-center site selection, market intelligence, and infrastructure analysis · Endpoint: <code>{manifest['endpoint']}</code></p>

<div class="quick-start">
  <strong>Quick start — claim a free dev key in 30 seconds:</strong>
  <pre>curl -X POST https://dchub.cloud/api/v1/keys/claim \\
  -H 'Content-Type: application/json' \\
  -d '{{"client_name":"your-agent-name"}}'</pre>
  Returns: <code>{{"ok":true,"api_key":"dch_live_...","tier":"free","daily_calls":100}}</code>
</div>

{"".join(cat_blocks)}

<section>
  <h2>Machine-readable manifest</h2>
  <p>For programmatic consumption (LLM tool registries, MCP directories):</p>
  <p><code><a href="/api/v1/mcp/tools.json">GET /api/v1/mcp/tools.json</a></code> — full manifest with categories, tiers, examples.</p>
</section>

<p style="margin-top:3rem;color:#9ca3af;font-size:.85rem">
  DC Hub Nexus MCP Server v2.2 · Updated {manifest['version']} ·
  <a href="https://dchub.cloud/mcp">/mcp</a> ·
  <a href="https://dchub.cloud/llms.txt">/llms.txt</a> ·
  <a href="https://dchub.cloud/openapi.json">/openapi.json</a>
</p>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
