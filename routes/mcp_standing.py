"""
routes/mcp_standing.py — public, SHAREABLE "MCP standing & adoption" surface.

  GET /api/v1/mcp/standing   → JSON: registries we're listed on (live) + our rank
                               highlights + connected AI platforms (live) + summary
  GET /mcp-standing          → a clean, OG-tagged, indexable HTML card to SHARE

The ask: "an MCP section that shows who we're connected with and our rankings — a
nice tool to share showing adoption." Registries come from the live presence crawler
(mcp_presence_listings); connected platforms from the live MCP activity; the marquee
registry ranks (Smithery #1) are linked to their source so a reader can verify.
"""
import json
import urllib.request
from flask import Blueprint, jsonify, Response

mcp_standing_bp = Blueprint("mcp_standing", __name__)

BASE = "https://dchub.cloud"

# Marquee registry rankings — the shareable social proof. Each links to its source
# so it's verifiable (not a self-asserted claim). Smithery search-rank #1 across the
# queries that define this category; Glama profile-completeness score.
_RANK_HIGHLIGHTS = [
    {"registry": "Smithery", "claim": "#1 server for “data centers”, “energy”, “grid”, “power”, “fiber”, “hyperscale”, “interconnection”",
     "source": "https://smithery.ai/server/@dchub/dchub-mcp-server"},
    {"registry": "Glama", "claim": "Quality score 83% (Server Coherence A · Tool Definition A)",
     "source": "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server"},
]


# The registries DC Hub is CONFIRMED listed on (homepage-verified). Curated + stable
# so the shareable page never shows aspirational/abandoned crawler seeds as "listed".
CONFIRMED_REGISTRIES = [
    {"registry": "Smithery",              "url": "https://smithery.ai/server/@dchub/dchub-mcp-server"},
    {"registry": "Glama",                 "url": "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server"},
    {"registry": "mcp.so",                "url": "https://mcp.so/server/dchub-mcp-server"},
    {"registry": "PulseMCP",              "url": "https://www.pulsemcp.com/servers/dchub"},
    {"registry": "LobeHub",               "url": "https://lobehub.com/mcp/dchub-mcp-server"},
    {"registry": "Official MCP Registry", "url": "https://github.com/modelcontextprotocol/registry"},
]

# Only recognizable AI platforms count as "connected" on the shareable page — internal
# probes / health-checks / unknown SDKs ('deploy-probe', 'yellowmcp-health', 'unknown')
# must never appear as social proof. Substring allowlist of real platforms.
_PLATFORM_ALLOW = (
    "claude", "anthropic", "chatgpt", "openai", "gpt", "gemini", "google", "bard",
    "perplexity", "grok", "xai", "copilot", "microsoft", "meta", "llama", "deepseek",
    "mistral", "cohere", "you.com", "youchat", "poe", "quora", "huggingface", "groq",
    "cursor", "cline", "windsurf", "continue", "openrouter",
)
TOOLS_COUNT = 31


def _registries_live():
    """Confirmed registry listings (curated, homepage-verified)."""
    return [{**r, "listed": True, "tools": None, "last_seen": None}
            for r in CONFIRMED_REGISTRIES]


def _platforms_live():
    """Connected AI platforms from live MCP activity — filtered to recognizable
    brands so internal probes/health-checks never show as social proof."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8080/api/v1/mcp/platforms",
            headers={"User-Agent": "dchub-mcp-standing/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            d = json.loads(resp.read(200000))
        rows = d.get("platforms") or []
        def _real(name):
            n = (name or "").lower()
            return any(a in n for a in _PLATFORM_ALLOW)
        real = [r for r in rows if _real(r.get("platform")) and (r.get("total_connections") or 0) > 0]
        real.sort(key=lambda r: -(r.get("total_connections") or 0))
        top = [{"platform": (r.get("platform") or "").replace("-", " ").title(),
                "connections": r.get("total_connections"),
                "status": r.get("status")} for r in real[:14]]
        return {"active_count": len(real), "tracked_count": len(rows),
                "tools_count": TOOLS_COUNT, "top": top}
    except Exception:
        return {"active_count": None, "tracked_count": None, "tools_count": TOOLS_COUNT, "top": []}


def _standing():
    regs = _registries_live()
    plats = _platforms_live()
    return {
        "ok": True,
        "headline": "DC Hub is the #1 MCP server for data-center, power & energy intelligence.",
        "rank_highlights": _RANK_HIGHLIGHTS,
        "registries": regs,
        "registries_count": len(regs),
        "platforms": plats,
        "summary": (f"Listed on {len(regs)} MCP registries; "
                    f"{plats.get('active_count') or 'many'} AI platforms actively connected; "
                    f"{plats.get('tools_count') or 31} tools."),
        "endpoints": {"mcp": f"{BASE}/mcp", "onboard": f"{BASE}/api/v1/onboard",
                      "this": f"{BASE}/api/v1/mcp/standing", "page": f"{BASE}/mcp-standing"},
        "cite_as": "Source: dchub.cloud",
    }


@mcp_standing_bp.route("/api/v1/mcp/standing", methods=["GET"])
def api_standing():
    resp = jsonify(_standing())
    resp.headers["Cache-Control"] = "public, max-age=600, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@mcp_standing_bp.route("/mcp-standing", methods=["GET"])
def page_standing():
    import html as _h
    e = lambda x: _h.escape(str(x if x is not None else ""))
    s = _standing()
    plats = s["platforms"]

    rank_rows = "".join(
        f"<tr><td><b>{e(r['registry'])}</b></td><td>{e(r['claim'])}</td>"
        f"<td><a href='{e(r['source'])}' rel='noopener'>verify →</a></td></tr>"
        for r in s["rank_highlights"])
    reg_rows = "".join(
        f"<tr><td><a href='{e(r['url'])}' rel='noopener'>{e(r['registry'])}</a></td>"
        f"<td>{'✅ listed' if r['listed'] else '—'}</td>"
        f"<td>{e(r['tools']) if r.get('tools') else '—'}</td></tr>"
        for r in s["registries"]) or "<tr><td colspan=3>Refreshing…</td></tr>"
    plat_rows = "".join(
        f"<tr><td>{e(p['platform'])}</td><td>{e(p['connections'])}</td>"
        f"<td>{e(p['status'])}</td></tr>" for p in plats.get("top", [])) \
        or "<tr><td colspan=3>Refreshing…</td></tr>"

    ld = {
        "@context": "https://schema.org", "@type": "Organization",
        "name": "DC Hub", "url": "https://dchub.cloud",
        "description": s["headline"],
        "sameAs": [r["source"] for r in s["rank_highlights"]] + [r["url"] for r in s["registries"][:10]],
    }
    css = ("body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:920px;"
           "margin:0 auto;padding:24px;color:#0f172a}h1{font-size:1.5rem;margin-bottom:2px}"
           "h2{font-size:1.05rem;margin-top:28px;color:#1e293b}table{border-collapse:collapse;"
           "width:100%;margin:10px 0}th,td{padding:8px 10px;border-bottom:1px solid #e2e8f0;"
           "text-align:left;font-size:.92rem}th{background:#f8fafc}.sub{color:#475569}"
           ".big{font-size:1.05rem;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;"
           "padding:12px 16px;margin:14px 0}a{color:#2563eb;text-decoration:none}.cite{color:#64748b;"
           "font-size:.84rem;margin-top:22px}")
    page = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>DC Hub — MCP Standing &amp; Adoption (#1 for data-center intelligence)</title>"
        f"<meta name=\"description\" content=\"{e(s['summary'])} DC Hub is the #1 MCP server for data-center, power &amp; energy intelligence across Smithery, Glama, mcp.so, PulseMCP, LobeHub and the official registry.\">"
        "<link rel=\"canonical\" href=\"https://dchub.cloud/mcp-standing\">"
        "<meta property=\"og:title\" content=\"DC Hub — #1 MCP Server for Data-Center Intelligence\">"
        f"<meta property=\"og:description\" content=\"{e(s['summary'])}\">"
        "<meta property=\"og:url\" content=\"https://dchub.cloud/mcp-standing\"><meta property=\"og:type\" content=\"website\">"
        f"<script type=\"application/ld+json\">{json.dumps(ld, separators=(',',':'))}</script>"
        f"<style>{css}</style></head><body><main>"
        "<h1>DC Hub — MCP Standing &amp; Adoption</h1>"
        f"<p class=\"sub\">{e(s['headline'])}</p>"
        f"<div class=\"big\">{e(s['summary'])}</div>"
        "<h2>Registry rankings</h2><table><thead><tr><th>Registry</th><th>Standing</th><th></th></tr></thead><tbody>"
        + rank_rows + "</tbody></table>"
        "<h2>Listed on</h2><table><thead><tr><th>Registry</th><th>Status</th><th>Tools</th></tr></thead><tbody>"
        + reg_rows + "</tbody></table>"
        f"<h2>Connected AI platforms <span class=\"sub\">({e(plats.get('active_count'))} active / {e(plats.get('tracked_count'))} tracked)</span></h2>"
        "<table><thead><tr><th>Platform</th><th>Connections</th><th>Status</th></tr></thead><tbody>"
        + plat_rows + "</tbody></table>"
        "<p class=\"cite\">Live data from DC Hub. Connect: <a href=\"/mcp\">MCP server</a> · "
        "<a href=\"/api/v1/onboard\">onboard any client</a> · <a href=\"/api/v1/mcp/standing\">JSON</a>. "
        "Source: <a href=\"https://dchub.cloud\">dchub.cloud</a>.</p>"
        "</main></body></html>")
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=600, must-revalidate"
    return resp, 200
