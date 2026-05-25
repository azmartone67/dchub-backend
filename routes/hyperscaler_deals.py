"""
hyperscaler_deals.py — Hyperscaler AI Deal Tracker.

Phase ZZZZZ-round36 (2026-05-24). The hyperscaler/sovereign-AI deal
flow (Stargate, Oracle, CoreWeave, AMD-Taiwan, Equinix-Cramer, etc.)
is happening at $1B+/week. Nobody publishes a typed feed with $-figure
+ MW + region. DC Hub already pulls Bloomberg + DCD into the news
table; this endpoint surfaces the AI-capex subset as a structured feed.

Endpoint:
  GET /api/v1/hyperscaler-deals?limit=20 → JSON list
  GET /hyperscaler-deals → public ticker landing page
"""
import os
import re
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

hyperscaler_deals_bp = Blueprint("hyperscaler_deals", __name__)

# Keywords that mark AI-capex stories (case-insensitive)
HYPERSCALER_KEYWORDS = [
    "stargate", "openai", "anthropic", "coreweave", "lambda", "crusoe",
    "oracle ai", "oracle cloud", "amd taiwan", "nvidia gpu", "blackwell",
    "h100", "h200", "gpu cluster", "ai training", "ai data center",
    "hyperscale", "sovereign ai", "uae ai", "saudi ai",
    "microsoft ai", "google ai", "aws ai", "meta ai", "tesla cortex",
    "elon ai", "xai", "grok", "musk ai",
]

# Patterns to extract dollar figures + MW
RE_DOLLAR = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s?(billion|B|million|M|trillion|T)\b", re.I)
RE_MW = re.compile(r"([\d,]+(?:\.\d+)?)\s?(MW|GW|gigawatts?|megawatts?)\b", re.I)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _extract_dollars(text):
    if not text: return None
    m = RE_DOLLAR.search(text)
    if not m: return None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    if unit in ("b", "billion"): return {"value": num * 1e9, "display": f"${num}B"}
    if unit in ("m", "million"): return {"value": num * 1e6, "display": f"${num}M"}
    if unit in ("t", "trillion"): return {"value": num * 1e12, "display": f"${num}T"}
    return None


def _extract_mw(text):
    if not text: return None
    m = RE_MW.search(text)
    if not m: return None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    if "g" in unit: return {"value": num * 1000, "display": f"{num} GW"}
    return {"value": num, "display": f"{num} MW"}


def _classify_actor(text):
    t = (text or "").lower()
    actors = []
    for actor, keys in [
        ("OpenAI",     ["openai", "stargate"]),
        ("Anthropic",  ["anthropic", "claude"]),
        ("Microsoft",  ["microsoft", "azure"]),
        ("Google",     ["google", "gemini"]),
        ("AWS",        ["aws", "amazon web services"]),
        ("Meta",       ["meta ai", "facebook ai"]),
        ("Oracle",     ["oracle"]),
        ("CoreWeave",  ["coreweave"]),
        ("Lambda",     ["lambda labs"]),
        ("Crusoe",     ["crusoe"]),
        ("xAI",        ["xai", "grok", "musk ai"]),
        ("NVIDIA",     ["nvidia", "h100", "h200", "blackwell"]),
        ("AMD",        ["amd taiwan", "amd $10b", "amd $"]),
        ("Tesla",      ["tesla cortex", "tesla ai"]),
    ]:
        if any(k in t for k in keys): actors.append(actor)
    return actors


def _fetch_deals(limit=20):
    if not (_pg and _dsn()):
        return [], "database_unavailable"
    sql_keywords = " OR ".join([f"LOWER(title) LIKE '%%{k}%%'" for k in HYPERSCALER_KEYWORDS])
    # Schema: news table — id, title, source, url, published_date (DATE).
    # Try both `summary` and `description` columns; whichever exists.
    query_summary = f"""
        SELECT id, title, source, url, published_date, summary
        FROM news
        WHERE ({sql_keywords})
          AND published_date > CURRENT_DATE - INTERVAL '60 days'
        ORDER BY published_date DESC LIMIT %s
    """
    query_desc = f"""
        SELECT id, title, source, url, published_date, description AS summary
        FROM news
        WHERE ({sql_keywords})
          AND published_date > CURRENT_DATE - INTERVAL '60 days'
        ORDER BY published_date DESC LIMIT %s
    """
    query_no_summary = f"""
        SELECT id, title, source, url, published_date, '' AS summary
        FROM news
        WHERE ({sql_keywords})
          AND published_date > CURRENT_DATE - INTERVAL '60 days'
        ORDER BY published_date DESC LIMIT %s
    """
    rows = None
    last_err = None
    for q in (query_summary, query_desc, query_no_summary):
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(q, (limit * 3,))
                rows = cur.fetchall()
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    if rows is None:
        return [], last_err or "all_queries_failed"

    out = []
    for r in rows:
        full = (r.get("title") or "") + " " + (r.get("summary") or "")
        actors = _classify_actor(full)
        if not actors: continue
        pub = r.get("published_date")
        out.append({
            "id":         r.get("id"),
            "title":      r.get("title"),
            "source":     r.get("source"),
            "url":        r.get("url"),
            "published":  pub.isoformat() if pub else None,
            "actors":     actors,
            "value_usd":  _extract_dollars(full),
            "capacity":   _extract_mw(full),
            "summary":    (r.get("summary") or "")[:280],
        })
        if len(out) >= limit: break
    return out, None


@hyperscaler_deals_bp.route("/api/v1/hyperscaler-deals", methods=["GET"])
def api_hyperscaler_deals():
    limit = max(5, min(50, int(request.args.get("limit", 20))))
    deals, err = _fetch_deals(limit)
    return jsonify({
        "feed_name":   "Hyperscaler AI Deal Tracker",
        "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "result_count": len(deals),
        "deals":       deals,
        "error":       err,
        "methodology": ("Filters dc_news for 30+ hyperscaler/AI-capex keywords. "
                        "Extracts $-figures + MW via regex. Actors detected by name match."),
        "live_feed":   "https://api.dchub.cloud/api/v1/hyperscaler-deals",
        "landing":     "https://dchub.cloud/hyperscaler-deals",
    }), 200, {"Cache-Control": "public, max-age=600, s-maxage=1800"}


_LANDING_HD = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hyperscaler AI Deal Tracker — DC Hub</title>
<meta name="description" content="Live ticker of hyperscaler AI capex: Stargate, Oracle, CoreWeave, AMD, NVIDIA. $-figures and MW extracted. Updated every 10 min.">
<meta property="og:title" content="Hyperscaler AI Deal Tracker">
<meta property="og:description" content="Live $1B+/week AI capex deals — typed feed with dollars + megawatts.">
<meta property="og:image" content="https://dchub.cloud/static/og/landing-hyperscaler-deals.png">
<link rel="canonical" href="https://dchub.cloud/hyperscaler-deals">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
 body{max-width:1100px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.5}
 h1{font-size:2.2rem;margin:.4em 0;letter-spacing:-.02em}
 .lead{color:#475569;font-size:1.05rem;max-width:760px}
 .deal{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;margin:12px 0;display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .deal-main{flex:1;min-width:300px}
 .deal h3{margin:0 0 6px;font-size:1.05rem;color:#0f172a}
 .deal h3 a{color:inherit;text-decoration:none}
 .deal h3 a:hover{color:#6366f1}
 .meta{font-size:.82rem;color:#64748b;margin-bottom:6px}
 .actors{display:inline-flex;gap:6px;flex-wrap:wrap}
 .actor{background:#e0e7ff;color:#3730a3;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600}
 .summary{color:#475569;font-size:.9rem;margin-top:8px}
 .deal-figures{min-width:140px;text-align:right}
 .dollar{font-size:1.6rem;font-weight:700;color:#15803d;font-family:ui-monospace,monospace;line-height:1}
 .mw{font-size:1.1rem;font-weight:600;color:#6366f1;font-family:ui-monospace,monospace;margin-top:4px}
 .pane{background:#f8fafc;border:1px solid #e2e8f0;padding:18px 22px;border-radius:10px;margin:20px 0}
 .pane h2{margin-top:0;font-size:1.1rem}
 #status{color:#64748b;font-size:.85rem;margin:8px 0}
 .api{font-family:ui-monospace,monospace;background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-size:.85em}
</style></head><body>
<h1>Hyperscaler AI Deal Tracker</h1>
<p class="lead">Live ticker of AI capex deals: Stargate, Oracle, CoreWeave, AMD, NVIDIA, sovereign AI, hyperscale GPU clusters.
$-figures and MW extracted automatically. Refreshed every 10 minutes.</p>
<div id="status">loading...</div>
<div id="deals"></div>

<div class="pane">
  <h2>How to use this</h2>
  <p><b>API:</b> <span class="api">GET https://api.dchub.cloud/api/v1/hyperscaler-deals?limit=20</span></p>
  <p><b>MCP tool:</b> <span class="api">hyperscaler_deals({"limit": 20})</span> on <a href="/mcp">/mcp</a></p>
  <p>Underlying source: dc_news table (40+ industry feeds — Bloomberg, DCD, Reuters, Google News).
  Actor detection by name match; $/MW extracted via regex from title + summary.</p>
</div>

<p style="color:#64748b;font-size:.85rem;margin-top:24px"><a href="/">DC Hub</a> · <a href="/ai-capacity-index">AI Capacity Index</a> · <a href="/news">All news</a></p>

<script>
fetch('/api/v1/hyperscaler-deals?limit=25').then(r=>r.json()).then(d=>{
  const root=document.getElementById('deals');
  d.deals.forEach(deal=>{
    const div=document.createElement('div');div.className='deal';
    const actors=deal.actors.map(a=>'<span class="actor">'+a+'</span>').join('');
    const date=deal.published?new Date(deal.published).toLocaleDateString():'';
    div.innerHTML='<div class="deal-main">'
      +'<h3><a href="'+(deal.url||'#')+'" target="_blank" rel="noopener">'+deal.title+'</a></h3>'
      +'<div class="meta">'+date+' · '+(deal.source||'?')+' · <span class="actors">'+actors+'</span></div>'
      +'<div class="summary">'+(deal.summary||'')+'</div></div>'
      +'<div class="deal-figures">'
      +(deal.value_usd?'<div class="dollar">'+deal.value_usd.display+'</div>':'')
      +(deal.capacity?'<div class="mw">'+deal.capacity.display+'</div>':'')
      +'</div>';
    root.appendChild(div);
  });
  document.getElementById('status').textContent=d.result_count+' deals · refreshed '+new Date(d.computed_at).toLocaleString();
}).catch(e=>document.getElementById('status').textContent='Failed: '+e.message);
</script>
</body></html>"""


@hyperscaler_deals_bp.route("/hyperscaler-deals", strict_slashes=False, methods=["GET"])
def landing_hd():
    return _LANDING_HD, 200, {"Content-Type": "text/html; charset=utf-8",
                                "Cache-Control": "public, max-age=600, s-maxage=1800"}
