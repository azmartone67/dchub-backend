"""Phase ZZZZZ-round40 — SEO page → JSON alternate link injection.

Item #4: every SEO page should advertise an MCP-shaped JSON alternate
so AI crawlers (ChatGPT, Claude, Perplexity, Gemini) can switch from
human-HTML to machine-readable JSON with one HTTP HEAD probe. Cheapest
distribution we have given DC Hub is already cited by all four.

Wiring (main.py, AFTER other blueprints):
    from routes.seo_agent_alternates import register_alternate_hook
    register_alternate_hook(app)

★★ 2026-09-05 — TWO OF THE FIVE PATTERNS MATCHED URLS THAT DO NOT EXIST, and
the whole affordance lived in <head>, where the agent channel cannot see it.

Measured live through the edge, by reading the X-DC-Alternates-Injected header
this module sets:

    /markets/dallas                  200  injected: market      <- worked
    /hyperscaler-deals               200  injected: deals       <- worked
    /ai-capacity-index               200  injected: capacity    <- worked
    /facilities/<slug>               200  injected: NONE
    /facility/<slug>                 301  (pattern targeted this redirect)
    /grid/ercot                      200  injected: NONE
    /grids/ercot                     404  (pattern targeted this 404)

`^/facility/([^/]+)` is singular; the ~20,500 live facility pages are
`/facilities/<slug>`. `^/grids/([^/]+)` is plural; live grid pages are
`/grid/<iso>`. Both patterns matched only a redirect and a 404 — neither of
which carries a `<head>` — so the two largest page families on the site have
never received an alternate link. A pattern aimed at a URL that does not serve
HTML fails silently forever: the hook returns early and sets no header, which
is indistinguishable from "this page is not an SEO page".

★ THE CAPACITY ALTERNATE WAS A 404 WE WERE ADVERTISING. `/ai-capacity-index`
DID inject, and pointed every AI crawler at `/ai-capacity-index/today.json`,
which returns 404 (18KB of HTML error page). Measured same day; the live
document is `/api/v1/ai-capacity-index` (200, 14,172 b). Advertising a dead
machine-readable alternate is worse than advertising none — the crawler spends
the fetch and learns the JSON surface is broken.

★★ HEAD-ONLY IS INVISIBLE TO THE AGENT CHANNEL. Cloudflare's "Markdown for
Agents" serves a converted representation on `Accept: text/markdown`, and
HTML→Markdown conversion discards `<head>` entirely. So `<link rel="alternate"
type="application/mcp+json">` and `<meta name="dchub:mcp-tools">` reach exactly
nobody in the representation a token-thrifty agent asks for — and
`dchub:mcp-tools` is a custom meta name no crawler consumes in any case. A
body-level line is now injected alongside them.

★ THE BODY LINE'S SHAPE IS NOT COSMETIC — see #3948, same day. An `<em>`
wrapping a `<code>` becomes an `_…_` emphasis run in markdown and every `_`
inside it is eaten as a delimiter, so `get_market_dcpi_rank` shipped to agents
as `getmarketdcpirank` — a tool that does not exist. The shapes measured to
survive intact are a plain `<p>`/`<div>` with a bare `<code>`: `/transactions`
(`list_transactions`) and `/facilities/<slug>` (`get_facility`) both come
through clean. NEVER wrap the tool name in `<em>`, and never style this line
with anything that converts to an underscore run.

★ TOOL NAMES ARE DRAWN FROM routes/tools_manifest.py `_TOOL_REST`, not typed
here from memory. A misspelled tool name is the same defect as the mangled one
above — it teaches an agent a call that errors — and
tests/test_agent_alternates_reach_live_pages.py fails if any name here is
absent from that catalog. `_TOOL_REST` is a partial map (61 of 83 tools), so
these lists are deliberately chosen from within it: binding to a real,
independently-maintained source is worth more than naming the single most apt
tool. `get_refined_queue` and `get_iso_context` are real tools but absent from
that catalog, so they are not used here.

★ ARGUMENT NAMES ARE THE SCHEMA'S, NOT THE PROSE'S. Verified against a live
`tools/list` on 2026-09-05: get_facility takes `slug`, get_market_intel takes
`market` (NOT `market_slug` — that is the name of the VALUE, and the server
silently strips undeclared arguments), get_grid_intelligence takes `iso`.
"""
import ast
import pathlib
import re

from flask import request

_SEO_PATTERNS = [
    # ★ The path that SERVES HTML, not the one that redirects to it. See the
    #   module docstring: /facility/<slug> is a 301 and /grids/<iso> a 404.
    (re.compile(r"^/facilities/([^/]+)/?$"), "facility", "/api/v1/facility/{0}"),
    (re.compile(r"^/markets/([^/]+)/?$"),    "market",   "/api/v1/markets/{0}"),
    (re.compile(r"^/grid/([^/]+)/?$"),       "grid",     "/api/v1/grid-intelligence?iso={0}"),
    (re.compile(r"^/hyperscaler-deals/?$"),  "deals",    "/api/v1/deals?limit=50"),
    # ★ was /ai-capacity-index/today.json — a 404 we were advertising.
    (re.compile(r"^/ai-capacity-index/?$"),  "capacity", "/api/v1/ai-capacity-index"),
]


_BREADCRUMB_SECTION = {
    "facility": ("Facilities", "/sites"),
    "market":   ("Markets", "/markets"),
    "grid":     ("Grid Intelligence", "/grid-intel"),
    "deals":    ("Hyperscaler Deals", "/hyperscaler-deals"),
    "capacity": ("AI Capacity Index", "/ai-capacity-index"),
}


# kind -> (primary tool, argument template, the rest of the tools for this page)
# ★ Was ONE hardcoded list ("search_facilities,get_facility,analyze_site,
#   compare_sites,get_market_intel") emitted for every kind, so the deals page
#   and the capacity page both advertised the facility/market tools and named
#   none of their own.
_MCP_TOOLS = {
    "facility": ("get_facility", 'slug="{slug}"',
                 ("score_facility", "analyze_site", "get_fiber_intel")),
    "market":   ("get_market_intel", 'market="{slug}"',
                 ("get_market_dcpi_rank", "rank_markets", "search_facilities",
                  "analyze_site")),
    "grid":     ("get_grid_intelligence", 'iso="{slug}"',
                 ("get_interconnection_queue", "get_grid_data", "compare_isos")),
    "deals":    ("hyperscaler_deals", "",
                 ("list_transactions", "deal_autopsy")),
    "capacity": ("ai_capacity_index", "",
                 ("get_power_pipeline", "rank_markets")),
}

# Marker so the body line is injected once, and so a page that already carries
# its own handoff (/facilities/<slug> has one in its footer) is left alone.
_BODY_MARKER = "dc-agent-handoff"


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


def _tool_names(kind):
    """Every tool this page kind advertises, primary first."""
    primary, _, rest = _MCP_TOOLS[kind]
    return (primary,) + tuple(rest)


def _agent_handoff_html(kind, slug):
    """A body-level line naming the exact call that reproduces this page.

    ★ Plain <p>, bare <code>, no <em> — see the module docstring. The italics
    are carried in CSS so nothing here converts to an underscore emphasis run.
    """
    primary, arg_tmpl, rest = _MCP_TOOLS[kind]
    call = primary
    if arg_tmpl and slug:
        call = primary + " " + arg_tmpl.format(slug=slug)
    also = ", ".join(f"<code>{t}</code>" for t in rest)
    return (
        f'<p class="{_BODY_MARKER}" style="margin:2.5rem auto 1rem;max-width:760px;'
        f'font-size:.85rem;line-height:1.6;color:#9ca3af;text-align:center;'
        f'font-style:italic">'
        f'AI agents: this page live via the DC Hub MCP server at '
        f'<a href="https://dchub.cloud/mcp">https://dchub.cloud/mcp</a> — '
        f'<code>{call}</code>. Also for this page: {also}.</p>'
    )


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
            tools = ",".join(_tool_names(kind))
            link_tags = (
                f'''<link rel="alternate" type="application/json" href="{alt_url}" title="{kind} JSON for AI agents">'''
                f'''<link rel="alternate" type="application/mcp+json" href="https://dchub.cloud/mcp" title="DC Hub MCP">'''
                f'''<meta name="dchub:resource-type" content="{kind}">'''
                f'''<meta name="dchub:mcp-tools" content="{tools}">'''
                + _breadcrumb_jsonld(request.path, kind, slug)
            )
            body = body.replace("<head>", "<head>" + link_tags, 1)

            # ★ The head tags above are discarded by HTML->Markdown conversion,
            #   so the agent channel needs a line in the BODY. Skipped when the
            #   page already ships its own handoff — /facilities/<slug> names
            #   get_facility in its footer and that one survives markdown
            #   intact, so a second line would only be noise.
            if ("</body>" in body
                    and _BODY_MARKER not in body
                    and "dchub.cloud/mcp" not in body.split("</head>", 1)[-1]):
                body = body.replace(
                    "</body>", _agent_handoff_html(kind, slug) + "</body>", 1)
                resp.headers["X-DC-Agent-Handoff"] = kind

            resp.set_data(body)
            resp.headers["X-DC-Alternates-Injected"] = kind
        except Exception:
            pass
        return resp
    return app


def catalog_tool_names():
    """Tool names from routes/tools_manifest.py `_TOOL_REST`, read with ast.

    Exposed for the guard test so it checks this module against an
    independently-maintained catalog rather than against a list it also owns.
    """
    src = (pathlib.Path(__file__).resolve().parent / "tools_manifest.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "_TOOL_REST"):
            return set(ast.literal_eval(node.value).keys())
    raise AssertionError("_TOOL_REST not found in routes/tools_manifest.py")
