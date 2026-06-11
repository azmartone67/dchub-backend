"""Phase ZZZZZ-round40 — SEO page → JSON alternate link injection.

Item #4: every SEO page should advertise an MCP-shaped JSON alternate
so AI crawlers (ChatGPT, Claude, Perplexity, Gemini) can switch from
human-HTML to machine-readable JSON with one HTTP HEAD probe. Cheapest
distribution we have given DC Hub is already cited by all four.

Wiring (main.py, AFTER other blueprints):
    from routes.seo_agent_alternates import register_alternate_hook
    register_alternate_hook(app)
"""
import re
from flask import request

_SEO_PATTERNS = [
    (re.compile(r"^/facility/([^/]+)/?$"),  "facility",  "/api/v1/facility/{0}"),
    (re.compile(r"^/markets/([^/]+)/?$"),   "market",    "/api/v1/markets/{0}"),
    (re.compile(r"^/grids/([^/]+)/?$"),     "grid",      "/api/v1/grid/{0}"),
    (re.compile(r"^/hyperscaler-deals/?$"), "deals",     "/api/v1/deals?limit=50"),
    (re.compile(r"^/ai-capacity-index/?$"), "capacity",  "/ai-capacity-index/today.json"),
]


_BREADCRUMB_SECTION = {
    "facility": ("Facilities", "/sites"),
    "market":   ("Markets", "/markets"),
    "grid":     ("Grid Intelligence", "/grid-intel"),
    "deals":    ("Hyperscaler Deals", "/hyperscaler-deals"),
    "capacity": ("AI Capacity Index", "/ai-capacity-index"),
}


def _alternate_link_for(path):
    for pat, kind, tmpl in _SEO_PATTERNS:
        m = pat.match(path)
        if m:
            slug = (m.groups()[0] if m.groups() else "") or ""
            return tmpl.format(*m.groups()), kind, slug
    return None, None, None


def _breadcrumb_jsonld(path, kind, slug):
    """2026-06-11: BreadcrumbList JSON-LD for SEO pages — GSC showed
    Breadcrumbs 0/0. Adds SERP breadcrumb display across the market /
    facility / grid trees. Built from the matched path; never raises."""
    import json as _json
    base = "https://dchub.cloud"
    items = [{"@type": "ListItem", "position": 1, "name": "DC Hub", "item": base + "/"}]
    pos = 2
    sec = _BREADCRUMB_SECTION.get(kind)
    if sec:
        items.append({"@type": "ListItem", "position": pos, "name": sec[0], "item": base + sec[1]})
        pos += 1
    if slug:
        name = (slug.replace("-", " ").replace("_", " ").strip()[:80].title()) or kind.title()
        items.append({"@type": "ListItem", "position": pos, "name": name, "item": base + path})
    payload = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}
    return ('<script type="application/ld+json">'
            + _json.dumps(payload, separators=(",", ":")) + "</script>")


def register_alternate_hook(app):
    @app.after_request
    def _inject_alternates(resp):
        try:
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in ct:
                return resp
            alt_url, kind, slug = _alternate_link_for(request.path)
            if not alt_url:
                return resp
            body = resp.get_data(as_text=True)
            if "<head>" not in body or 'rel="alternate" type="application/json"' in body:
                return resp
            link_tags = (
                f'''<link rel="alternate" type="application/json" href="{alt_url}" title="{kind} JSON for AI agents">'''
                f'''<link rel="alternate" type="application/mcp+json" href="https://dchub.cloud/mcp" title="DC Hub MCP">'''
                f'''<meta name="dchub:resource-type" content="{kind}">'''
                f'''<meta name="dchub:mcp-tools" content="search_facilities,get_facility,analyze_site,compare_sites,get_market_intel">'''
                + _breadcrumb_jsonld(request.path, kind, slug)
            )
            body = body.replace("<head>", "<head>" + link_tags, 1)
            resp.set_data(body)
            resp.headers["X-DC-Alternates-Injected"] = kind
        except Exception:
            pass
        return resp
    return app
