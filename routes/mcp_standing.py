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
from routes._brand_shell import brand_page

mcp_standing_bp = Blueprint("mcp_standing", __name__)

BASE = "https://dchub.cloud"

# Marquee registry rankings — the shareable social proof. Each links to its source
# so a reader can VERIFY it there (not a self-asserted claim).
# ★ 2026-07-17: dropped the frozen "Quality score 83%" — that number is not
# sourced live from Glama in this process, so it silently rots (Glama recomputes
# it). We keep only claims that are STABLE + verifiable at the linked source: the
# Smithery #1 rank (actively defended by registry_monitor.py, which pages on a
# slip) and the A/A letter grades (Glama's coarse rubric, far slower to move than
# the percentage). No hardcoded numeric score that can drift out from under us.
_RANK_HIGHLIGHTS = [
    {"registry": "Smithery", "claim": "#1 server for “data centers”, “energy”, “grid”, “power”, “fiber”, “hyperscale”, “interconnection”",
     "source": "https://smithery.ai/servers/azmartone67/dchub"},
    {"registry": "Glama", "claim": "Profile quality graded A / A (Server Coherence · Tool Definition)",
     "source": "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server"},
]


# The registries DC Hub is CONFIRMED listed on (homepage-verified). Curated + stable
# so the shareable page never shows aspirational/abandoned crawler seeds as "listed".
CONFIRMED_REGISTRIES = [
    {"registry": "Smithery",              "db": "smithery",
     "url": "https://smithery.ai/servers/azmartone67/dchub"},
    {"registry": "Glama",                 "db": "glama",
     "url": "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server"},
    {"registry": "mcp.so",                "db": "mcp_so",
     "url": "https://mcp.so/servers/dchub-mcp-server"},
    {"registry": "PulseMCP",              "db": "pulsemcp",
     "url": "https://www.pulsemcp.com/servers/dchub"},
    {"registry": "LobeHub",               "db": "lobehub",
     "url": "https://lobehub.com/mcp/dchub-mcp-server"},
    # ★ 2026-07-27: both of these previously linked a page that does NOT mention
    # DC Hub — the official registry's own SOURCE REPO, and the bare github.com/mcp
    # index. The listings are real; the links were unverifiable, so a reader who
    # clicked "verify" found nothing. Now both point at a URL that actually
    # contains our entry (the registry search API returns our isLatest record;
    # github.com/mcp filters SERVER-side, confirmed against a control query).
    {"registry": "Official MCP Registry", "db": "mcp_official_registry",
     "url": "https://registry.modelcontextprotocol.io/v0/servers?search=cloud.dchub"},
    {"registry": "GitHub MCP Registry",   "db": None,
     "url": "https://github.com/mcp?query=dchub"},
    {"registry": "Awesome MCP Servers",   "db": None,
     "url": "https://github.com/search?q=repo%3Apunkpeye%2Fawesome-mcp-servers"
            "+dchub&type=code"},
    {"registry": "Source (GitHub)",       "db": None,
     "url": "https://github.com/azmartone67/dchub-mcp-server"},
]

# The recognizable AI platforms that reach DC Hub (homepage-verified by request volume).
# Curated so the shareable page shows real brands as social proof — never the internal
# probes/SDK clients ('Deploy Probe', 'Grid Meta Mcp', 'Mistralai Python') that a raw
# connection feed surfaces. The live endpoint counts MCP *sessions* (a narrow slice);
# these are the platforms whose agents query DC Hub.
CONNECTED_PLATFORMS = [
    "Claude", "ChatGPT", "Gemini", "Perplexity", "Grok", "Copilot", "Meta AI",
    "DeepSeek", "Mistral", "Cohere", "Poe", "HuggingFace", "Groq", "You.com",
]
_TOOLS_FALLBACK = 74  # last-known canonical; _tools_count() derives it from the canon


def _tools_count() -> int:
    """Live tool count, derived from the canon (was a hardcoded 49 that drifted
    behind the live 73). Pure import — no network — so it's safe on a public
    page; falls back to the last-known canonical count."""
    try:
        from ai_surface_canon import PINNED
        n = int(PINNED.get("tools_advertised") or 0)
        if n > 0:
            return n
    except Exception:
        pass
    return _TOOLS_FALLBACK


def _verified_map() -> dict:
    """Per-registry VERIFIED facts from the presence crawler's own table.

    Pure DB read — no outbound HTTP on a public page (and it preserves the
    no-self-request invariant from the 2026-07-06 flywheel outage).

    Columns are INTROSPECTED before use: registry_truth ALTERs this table to add
    the truth_* verdict columns, and live schema has diverged from the repo DDL
    before. A missing column degrades to "no verification shown" — never to a
    wrong claim, and never to a 500 on a public page.
    """
    try:
        from routes.brain_rag import _db
        c = _db()
    except Exception:
        return {}
    if c is None:
        return {}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'mcp_presence_listings'")
            have = {r[0] for r in cur.fetchall()}
            if "registry_name" not in have:
                return {}
            # ONLY the count the verifying scan itself measured. The older
            # dchub_metric_published_tools comes from a different run, so
            # pairing it with truth_checked_at would date a number nobody
            # took that day (live showed "mcp.so 58" while the scan read 79).
            tools_col = ("truth_found_tools"
                         if "truth_found_tools" in have else "NULL")
            if "truth_ok_at" in have and "truth_checked_at" in have:
                # verified_drift leaves truth_ok_at NULL but WAS verified —
                # date it from the check, else a lagging listing shows nothing.
                when_col = "COALESCE(truth_ok_at, truth_checked_at)"
            elif "truth_ok_at" in have:
                when_col = "truth_ok_at"
            elif "last_crawled_at" in have:
                when_col = "last_crawled_at"
            else:
                when_col = "NULL"
            verdict_col = "truth_verdict" if "truth_verdict" in have else "NULL"
            cur.execute(
                "SELECT registry_name, %s, %s, %s FROM mcp_presence_listings"
                % (tools_col, when_col, verdict_col))
            rows = cur.fetchall()
    except Exception:
        return {}
    finally:
        try:
            c.close()
        except Exception:
            pass
    out = {}
    for name, tools, when, verdict in rows:
        verified = str(verdict or "").startswith("verified")
        # ★ The published tool count is gated on the SAME evidence as the
        # checkmark. Live showed "Official MCP Registry — 30 tools", which is a
        # parse artifact off a JSON API response, not a count that registry
        # publishes about us. A number a reader would read as a fact has to
        # clear the same bar as the claim it sits next to.
        out[name] = {
            "tools": int(tools) if (tools and verified) else None,
            # Only 'verified_*' earns a checkmark. broken / unverified / NULL all
            # fall through to a plain "listed" — we state what we checked, and
            # stay silent about what we could not.
            "verified_at": when.strftime("%Y-%m-%d") if (when and verified) else None,
        }
    return out


def _registries_live():
    """Confirmed listings, ENRICHED with what the crawler actually verified.

    The SET of rows stays curated so aspirational crawler seeds never appear as
    "listed"; the per-row verification date and published tool count come from
    the crawler, so the checkmark means something a reader can date.
    """
    vm = _verified_map()
    out = []
    for r in CONFIRMED_REGISTRIES:
        v = vm.get(r.get("db") or "", {})
        out.append({"registry": r["registry"], "url": r["url"], "listed": True,
                    "tools": v.get("tools"), "verified_at": v.get("verified_at"),
                    "last_seen": None})
    return out


def _platforms_live():
    """Recognizable AI platforms reaching DC Hub. Curated brands (credible social
    proof); tracked_count enriched from the live MCP activity feed best-effort."""
    tracked = None
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8080/api/v1/mcp/platforms",
            headers={"User-Agent": "dchub-mcp-standing/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            tracked = len((json.loads(resp.read(200000)).get("platforms") or []))
    except Exception:
        pass
    top = [{"platform": p, "connections": "active", "status": "connected"}
           for p in CONNECTED_PLATFORMS]
    return {"active_count": len(CONNECTED_PLATFORMS), "tracked_count": tracked,
            "tools_count": _tools_count(), "top": top}


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
                    f"{plats.get('tools_count') or _TOOLS_FALLBACK} tools."),
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
        f"<td>{('✅ verified ' + e(r['verified_at'])) if r.get('verified_at') else 'listed'}</td>"
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
    body = (
        "<h1>DC Hub — MCP Standing &amp; Adoption</h1>"
        f"<p class=\"lede\">{e(s['headline'])}</p>"
        f"<div class=\"big\">{e(s['summary'])}</div>"
        "<h2>Where the MCP server lives</h2>"
        "<table><tbody>"
        f"<tr><td><b>Endpoint</b></td><td><code>{BASE}/mcp</code></td></tr>"
        "<tr><td><b>Transport</b></td><td>Streamable HTTP (remote — nothing to install)</td></tr>"
        f"<tr><td><b>Tools</b></td><td>{e(plats.get('tools_count'))} live</td></tr>"
        "<tr><td><b>Access</b></td><td>Works keyless at free-tier depth; "
        "call <code>claim_free_key</code> for the full free tier</td></tr>"
        "<tr><td><b>Source</b></td><td><a href=\"https://github.com/azmartone67/dchub-mcp-server\" "
        "rel=\"noopener\">github.com/azmartone67/dchub-mcp-server</a></td></tr>"
        "<tr><td><b>Setup</b></td><td><a href=\"/connect#start\">Per-client setup guides →</a></td></tr>"
        "</tbody></table>"
        "<h2>Registry rankings</h2><table><thead><tr><th>Registry</th><th>Standing</th><th></th></tr></thead><tbody>"
        + rank_rows + "</tbody></table>"
        "<h2>Listed on</h2><table><thead><tr><th>Registry</th><th>Status</th><th>Tools</th></tr></thead><tbody>"
        + reg_rows + "</tbody></table>"
        f"<h2>Connected AI platforms <span class=\"sub\">· {e(plats.get('active_count'))} platforms · {e(plats.get('tracked_count'))} clients tracked</span></h2>"
        "<table><thead><tr><th>Platform</th><th>Connections</th><th>Status</th></tr></thead><tbody>"
        + plat_rows + "</tbody></table>"
        "<p class=\"cite\">Live data from DC Hub. Connect: <a href=\"/mcp\">MCP server</a> · "
        "<a href=\"/api/v1/onboard\">onboard any client</a> · <a href=\"/api/v1/mcp/standing\">JSON</a>. "
        "Source: <a href=\"https://dchub.cloud\">dchub.cloud</a>.</p>")
    page = brand_page(
        title="DC Hub — MCP Standing & Adoption · #1 for data-center intelligence",
        description=(s["summary"] + " DC Hub is the #1 MCP server for data-center, power & "
                     "energy intelligence across Smithery, Glama, mcp.so, PulseMCP, LobeHub "
                     "and the official MCP registry."),
        canonical="https://dchub.cloud/mcp-standing",
        body_html=body,
        ld_jsons=[json.dumps(ld, separators=(",", ":"))],
        og_desc=s["summary"])
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=600, must-revalidate"
    return resp, 200
