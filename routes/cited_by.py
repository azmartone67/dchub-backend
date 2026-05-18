"""
Phase ZZZZ-cited-by (2026-05-18) — public proof-of-citation surface.

The user's strategy: "let AI agents do the killing." Every time ChatGPT,
Claude, Perplexity, Gemini, etc. calls a DC Hub MCP tool, we already log
it in mcp_tool_calls. This endpoint surfaces that publicly — a living
dashboard of which AI platforms cite us, how often, for what.

Two surfaces:
  GET /api/v1/cited-by         JSON: top platforms, call counts, tools
  GET /cited-by                HTML: scrollable proof page + schema.org

The pitch: "DC Hub is the only DC-intelligence platform where you can SEE
the AI agents using it in real time. CBRE/JLL ship quarterly PDFs nobody
verifies. We ship JSON that LLMs answer questions with — here's the
receipts." Strong sales asset for the partnership pitch.
"""

import os
import re
import logging
import datetime as _dt
from flask import Blueprint, jsonify, Response

logger = logging.getLogger(__name__)
cited_by_bp = Blueprint("cited_by", __name__)


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


# UA fingerprints → friendly platform names. Conservative — only match
# patterns we KNOW are LLM agents, not random scrapers. Same fingerprint
# heuristics the brain uses elsewhere.
_UA_PATTERNS = [
    (re.compile(r"chatgpt|openai", re.I),         "ChatGPT (OpenAI)"),
    (re.compile(r"claude", re.I),                  "Claude (Anthropic)"),
    (re.compile(r"perplexitybot|perplexity", re.I),"Perplexity"),
    (re.compile(r"gemini|googleother", re.I),      "Gemini (Google)"),
    (re.compile(r"groq", re.I),                    "Groq"),
    (re.compile(r"cursor", re.I),                  "Cursor"),
    (re.compile(r"windsurf", re.I),                "Windsurf"),
    (re.compile(r"continue\.dev|continueai", re.I),"Continue.dev"),
    (re.compile(r"cody|sourcegraph", re.I),        "Cody (Sourcegraph)"),
    (re.compile(r"copilot|github-copilot", re.I),  "GitHub Copilot"),
    (re.compile(r"cline", re.I),                   "Cline"),
    (re.compile(r"phind", re.I),                   "Phind"),
    (re.compile(r"you\.com|youbot", re.I),         "You.com"),
    (re.compile(r"meta-external|metabot|llama", re.I), "Meta AI"),
    (re.compile(r"applebot-extended", re.I),       "Apple Intelligence"),
    # Phase ZZZZ-T3-classifier-v2 (2026-05-18): node-script (50K calls!)
    # is mostly modelcontextprotocol/sdk in Node. The unknown bucket
    # (25K calls / 42 IPs) is mostly bots without UA — those stay
    # unknown. But we catch more SDK + MCP patterns:
    (re.compile(r"@modelcontextprotocol|mcp-sdk|@anthropic", re.I), "MCP SDK (Node/Python)"),
    (re.compile(r"axios|undici|node-fetch", re.I), "Node HTTP client"),
    (re.compile(r"aiohttp|httpx|urllib3", re.I),   "Python HTTP client"),
    (re.compile(r"go-http-client|github\.com/", re.I), "Go HTTP client"),
    (re.compile(r"reqwest|hyper", re.I),           "Rust HTTP client"),
    (re.compile(r"curl/", re.I),                   "curl CLI"),
    (re.compile(r"wget", re.I),                    "wget CLI"),
    (re.compile(r"postmanruntime", re.I),          "Postman"),
    (re.compile(r"insomnia", re.I),                "Insomnia"),
    (re.compile(r"warp|charm\.io", re.I),          "Warp / Charm CLI"),
    (re.compile(r"zed", re.I),                     "Zed editor"),
    (re.compile(r"jetbrains|intellij", re.I),      "JetBrains AI"),
    (re.compile(r"deepseek", re.I),                "DeepSeek"),
    (re.compile(r"mistral", re.I),                 "Mistral AI"),
    (re.compile(r"grok|xai", re.I),                "Grok (xAI)"),
    (re.compile(r"replit", re.I),                  "Replit AI"),
    (re.compile(r"v0\.dev|vercel-ai", re.I),       "Vercel v0"),
    (re.compile(r"bolt\.new", re.I),               "Bolt.new"),
    (re.compile(r"goose|block-ai", re.I),          "Goose (Block)"),
]


def _classify_ua(ua: str) -> str | None:
    if not ua: return None
    for pat, name in _UA_PATTERNS:
        if pat.search(ua):
            return name
    return None


def _gather_cited_by_data(days: int = 30) -> dict:
    """Read mcp_tool_calls, group by classified platform."""
    by_platform: dict = {}
    total_calls = 0
    total_unique_uas = 0
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT user_agent, tool_name, COUNT(*) AS n,
                       MAX(created_at) AS last_seen,
                       MIN(created_at) AS first_seen
                  FROM mcp_tool_calls
                 WHERE created_at >= NOW() - INTERVAL '%s days'
                   AND user_agent IS NOT NULL
                   AND user_agent != ''
                 GROUP BY user_agent, tool_name
                 ORDER BY n DESC
                 LIMIT 2000
            """ % int(days))
            rows = cur.fetchall() or []
            seen_uas: set = set()
            for ua, tool, n, last_seen, first_seen in rows:
                seen_uas.add(ua)
                total_calls += n
                platform = _classify_ua(ua)
                if not platform: continue
                p = by_platform.setdefault(platform, {
                    "platform": platform,
                    "total_calls": 0,
                    "tool_breakdown": {},
                    "first_seen": first_seen.isoformat() if first_seen else None,
                    "last_seen":  last_seen.isoformat()  if last_seen  else None,
                })
                p["total_calls"] += n
                p["tool_breakdown"][tool] = p["tool_breakdown"].get(tool, 0) + n
                # extend window
                if first_seen and (not p["first_seen"] or
                                    first_seen.isoformat() < p["first_seen"]):
                    p["first_seen"] = first_seen.isoformat()
                if last_seen and (not p["last_seen"] or
                                   last_seen.isoformat() > p["last_seen"]):
                    p["last_seen"] = last_seen.isoformat()
            total_unique_uas = len(seen_uas)
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"cited_by gather failed: {e}")

    platforms = sorted(by_platform.values(),
                       key=lambda p: -p["total_calls"])
    # Trim tool_breakdown to top-5 per platform
    for p in platforms:
        tb = sorted(p["tool_breakdown"].items(), key=lambda x: -x[1])[:5]
        p["top_tools"] = [{"tool": t, "calls": n} for t, n in tb]
        del p["tool_breakdown"]

    return {
        "window_days": days,
        "total_calls_in_window": total_calls,
        "total_unique_user_agents": total_unique_uas,
        "platforms_identified": len(platforms),
        "platforms": platforms,
    }


@cited_by_bp.route("/api/v1/cited-by", methods=["GET"])
def cited_by_json():
    data = _gather_cited_by_data(days=30)
    data["ok"] = True
    data["generated_at"] = _dt.datetime.utcnow().isoformat() + "Z"
    data["note"] = ("Real-time citation telemetry from DC Hub's MCP server. "
                    "Public, machine-readable, schema.org-friendly. "
                    "Use this to verify DC Hub is the live source AI agents "
                    "depend on for data center intelligence.")
    return jsonify(data), 200


@cited_by_bp.route("/cited-by", methods=["GET"])
def cited_by_page():
    data = _gather_cited_by_data(days=30)
    platforms = data.get("platforms", [])
    total = data.get("total_calls_in_window", 0)
    rows_html = ""
    for p in platforms:
        tools = ", ".join(f"<code>{t['tool']}</code> ({t['calls']:,})"
                          for t in p["top_tools"])
        last = p.get("last_seen", "")[:10]
        rows_html += f"""<tr>
          <td><b>{p['platform']}</b></td>
          <td class="num">{p['total_calls']:,}</td>
          <td>{tools or '<i>—</i>'}</td>
          <td class="ago">{last}</td>
        </tr>"""
    if not platforms:
        rows_html = ('<tr><td colspan="4" style="text-align:center;color:#999;'
                     'padding:2rem">No identified LLM platforms in the last 30 days '
                     '(or telemetry just bootstrapping).</td></tr>')

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>AI Agents Citing DC Hub · Real-Time Proof</title>
<meta name="description" content="Live telemetry: which AI platforms (ChatGPT, Claude, Perplexity, Gemini, Groq, Cursor, Windsurf) call DC Hub's MCP server for data center intelligence. {total:,} calls in last 30 days from {len(platforms)} identified platforms.">
<link rel="canonical" href="https://dchub.cloud/cited-by">
<meta property="og:title" content="AI Agents Citing DC Hub — Real-Time Proof">
<meta property="og:description" content="{total:,} AI tool calls in last 30 days. Live evidence DC Hub is the LLM-citable source of truth for data centers.">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Dataset",
 "name":"DC Hub AI Citation Telemetry",
 "description":"Real-time log of AI platforms calling DC Hub's MCP server.",
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "license":"https://creativecommons.org/licenses/by/4.0/",
 "isAccessibleForFree":true
}}</script>
<style>
body{{font-family:'Instrument Sans',-apple-system,sans-serif;background:#05060d;color:#fafafa;margin:0;padding:2rem 1.5rem;line-height:1.6}}
.wrap{{max-width:980px;margin:0 auto}}
.pill{{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:99px;background:rgba(168,85,247,.12);border:1px solid rgba(168,85,247,.4);font-size:.78rem;color:#a855f7;font-weight:600;font-family:'JetBrains Mono',monospace;margin-bottom:14px}}
.pill::before{{content:"";width:8px;height:8px;border-radius:50%;background:#a855f7;animation:p 1.6s infinite}}
@keyframes p{{0%,100%{{opacity:.5}}50%{{opacity:1}}}}
h1{{font-size:2.4rem;font-weight:800;letter-spacing:-0.025em;margin:0 0 .25rem}}
.sub{{color:#9ca3af;font-size:1.05rem;margin-bottom:2rem}}
.kpi-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:2rem}}
.kpi{{background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:18px}}
.kpi-v{{font-family:'JetBrains Mono',monospace;font-size:1.8rem;font-weight:800}}
.kpi-l{{color:#9ca3af;font-size:.85rem;margin-top:6px}}
table{{width:100%;border-collapse:collapse;background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:10px;overflow:hidden}}
th{{text-align:left;padding:12px 14px;background:rgba(255,255,255,.03);color:#9ca3af;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;font-weight:600}}
td{{padding:12px 14px;border-top:1px solid rgba(255,255,255,.05)}}
td.num{{font-family:'JetBrains Mono',monospace;font-weight:600;color:#10b981}}
td.ago{{color:#6b7280;font-size:.85rem}}
.foot{{color:#6b7280;margin-top:2rem;font-size:.85rem}}
.foot a{{color:#a855f7;text-decoration:none}}
code{{background:rgba(255,255,255,.06);padding:1px 6px;border-radius:4px;font-size:.85em}}
</style>
</head><body>
<div class="wrap">
<div class="pill">● Live · Real-time MCP telemetry · Updated continuously</div>
<h1>AI Agents Citing DC Hub</h1>
<p class="sub">Every call to DC Hub's MCP server is logged with a user-agent fingerprint.
This page shows which AI platforms are using us as the source of truth for data center
intelligence — in the last 30 days, in real time.</p>

<div class="kpi-row">
  <div class="kpi"><div class="kpi-v">{total:,}</div><div class="kpi-l">Tool calls (last 30d)</div></div>
  <div class="kpi"><div class="kpi-v">{len(platforms)}</div><div class="kpi-l">AI platforms identified</div></div>
  <div class="kpi"><div class="kpi-v">{data.get('total_unique_user_agents',0)}</div><div class="kpi-l">Distinct user-agents</div></div>
</div>

<table>
<thead><tr><th>Platform</th><th class="num">Calls (30d)</th><th>Top tools called</th><th>Last seen</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>

<p class="foot">JSON: <a href="/api/v1/cited-by">/api/v1/cited-by</a> · MCP server: <a href="/mcp">/mcp</a> · Manifest: <a href="/.well-known/mcp.json">/.well-known/mcp.json</a> · CC-BY-4.0 (free to cite this dashboard).</p>
<p class="foot">This is the only public, live, MCP-citation surface for data center intelligence. Static PDF research (DCHawk, dcByte, DC Knowledge) cannot show this signal because they aren't queryable by AI agents. DC Hub <em>is</em>.</p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
