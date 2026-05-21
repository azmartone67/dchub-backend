"""
Phase ZZZZ-vs-generator (2026-05-18) — head-to-head landing-page generator.

The user's strategy concrete-move-3: "compare-us-to-X page generator —
drop in a competitor name + we generate a fact-sheet per the side-by-side
template." Means new competitive landing pages become a 5-min task.

  GET /vs/<slug>                HTML head-to-head page (SEO-indexed,
                                schema.org Comparison)
  GET /api/v1/competitive/vs/<slug>  JSON same data, AI-citable

Each competitor is positioned with FACTS, not adjectives. The page leads
with the 3 things DC Hub does that they structurally can't:
  • MCP / agent-callable
  • Live JSON (vs quarterly PDF)
  • CC-BY-4.0 free citation

Plus partnership hook: "we'd embed your data too — call us."
"""

import logging
import datetime as _dt
from flask import Blueprint, jsonify, Response

logger = logging.getLogger(__name__)
competitive_vs_bp = Blueprint("competitive_vs", __name__)


# Hand-curated facts per competitor. Stays explicitly NEUTRAL (no
# disparagement) — facts about their stated offerings vs ours. If we
# don't know a fact, we say "—" not "no".
_COMPETITORS = {
    "dchawk": {
        "name": "DC Hawk",
        "url":  "https://datacenterhawk.com",
        "category": "Data center research / brokerage intelligence",
        "their_strengths": [
            "Multi-decade analyst relationships in colo brokerage",
            "Deep quarterly market reports + investor briefings",
            "Strong North-American absorption tracking",
        ],
        "facts": {
            "data_format":        "PDF reports + slide decks",
            "update_cadence":     "Quarterly (some monthly digests)",
            "mcp_native":         "No (not exposed to LLMs)",
            "api_access":         "Enterprise contract required",
            "facility_coverage":  "Strong NA, lighter global",
            "pricing_model":      "$25K+ annual seat",
            "citation_license":   "Proprietary",
        },
    },
    "dcbyte": {
        "name": "DC Byte",
        "url":  "https://dcbyte.com",
        "category": "Global data center supply database",
        "their_strengths": [
            "First-mover global supply database",
            "Strong APAC + EMEA coverage",
            "Established analyst + investor distribution",
        ],
        "facts": {
            "data_format":        "Web dashboard + CSV export (paid)",
            "update_cadence":     "Quarterly with weekly news layer",
            "mcp_native":         "No",
            "api_access":         "Yes (paid tier)",
            "facility_coverage":  "Strong global",
            "pricing_model":      "Enterprise seat-based",
            "citation_license":   "Proprietary (subscriber-only)",
        },
    },
    "dcd": {
        "name": "DCD (Data Center Dynamics)",
        "url":  "https://datacenterdynamics.com",
        "category": "Data center industry news + events",
        "their_strengths": [
            "Largest industry news desk",
            "Conference + community network",
            "Strong vendor + sponsor relationships",
        ],
        "facts": {
            "data_format":        "Editorial articles + research reports",
            "update_cadence":     "Daily news, periodic research",
            "mcp_native":         "No",
            "api_access":         "—",
            "facility_coverage":  "News-driven (not structured DB)",
            "pricing_model":      "Free (ad-supported) + research subs",
            "citation_license":   "Editorial (per-article terms)",
        },
    },
    "dcknowledge": {
        "name": "Data Center Knowledge",
        "url":  "https://datacenterknowledge.com",
        "category": "Industry trade publication",
        "their_strengths": [
            "Long publishing history",
            "Aggregated industry analysis",
            "Vendor + analyst quotes",
        ],
        "facts": {
            "data_format":        "Articles",
            "update_cadence":     "Daily editorial",
            "mcp_native":         "No",
            "api_access":         "—",
            "facility_coverage":  "Editorial coverage, not structured DB",
            "pricing_model":      "Free (ad-supported)",
            "citation_license":   "Editorial (per-article)",
        },
    },
    "cbre": {
        "name": "CBRE Data Center Solutions",
        "url":  "https://cbre.com/insights/data-center",
        "category": "Commercial real estate research",
        "their_strengths": [
            "Global commercial real estate authority",
            "Trusted analyst relationships",
            "Owns CRE leasing + brokerage flow",
        ],
        "facts": {
            "data_format":        "Semi-annual PDF reports",
            "update_cadence":     "Twice yearly (H1/H2)",
            "mcp_native":         "No",
            "api_access":         "Enterprise only",
            "facility_coverage":  "Top global metros only",
            "pricing_model":      "Bundled with brokerage relationship",
            "citation_license":   "Proprietary (free with attribution)",
        },
        "partnership_hook": ("CBRE Research is a candidate partner. "
                             "DC Hub's live JSON can feed CBRE's quarterly "
                             "narratives — citation-ready, no NDA, CC-BY-4.0. "
                             "We've reached out via partnerships@dchub.cloud."),
    },
    "jll": {
        "name": "JLL Data Centers",
        "url":  "https://jll.com/en/industries/data-centers",
        "category": "Commercial real estate + advisory",
        "their_strengths": [
            "Top-3 global CRE advisor",
            "Cross-industry capital markets reach",
            "Trusted in enterprise procurement",
        ],
        "facts": {
            "data_format":        "Quarterly research reports",
            "update_cadence":     "Quarterly",
            "mcp_native":         "No",
            "api_access":         "—",
            "facility_coverage":  "Top global metros",
            "pricing_model":      "Bundled with advisory",
            "citation_license":   "Attribution-OK on excerpts",
        },
        "partnership_hook": ("JLL Data Centers — DC Hub data can power "
                             "your real-time client briefings. CC-BY-4.0 "
                             "citation, no licensing review. Reach: "
                             "partnerships@dchub.cloud"),
    },
}


_DCHUB_FACTS = {
    "data_format":        "Live JSON via REST + MCP (machine + LLM readable)",
    "update_cadence":     "Continuous (60s freshness SLA on key surfaces)",
    "mcp_native":         "YES — 40 tools, 96 platforms integrated",
    "api_access":         "Free tier 25 calls/day, $9/mo for 500, $49 for 1000",
    "facility_coverage":  "21,000+ facilities, 280+ markets, 178 countries",
    "pricing_model":      "Self-serve $9 → $499, no enterprise gate",
    "citation_license":   "CC-BY-4.0 — free to cite with attribution",
}


def _vs_data(slug: str) -> dict | None:
    c = _COMPETITORS.get(slug.lower())
    if not c: return None
    fact_rows = []
    for key, label in [
        ("data_format",       "Data format"),
        ("update_cadence",    "Update cadence"),
        ("mcp_native",        "MCP / LLM native"),
        ("api_access",        "API access"),
        ("facility_coverage", "Facility coverage"),
        ("pricing_model",     "Pricing model"),
        ("citation_license",  "Citation license"),
    ]:
        fact_rows.append({
            "label":      label,
            "dchub":      _DCHUB_FACTS.get(key, "—"),
            "competitor": c["facts"].get(key, "—"),
        })
    return {
        "slug":         slug.lower(),
        "competitor":   c["name"],
        "competitor_url": c["url"],
        "category":     c["category"],
        "their_strengths": c["their_strengths"],
        "comparison":   fact_rows,
        "partnership_hook": c.get("partnership_hook",
            f"We'd happily partner with {c['name']} — DC Hub's live data can "
            f"feed your reports + analyses. CC-BY-4.0, no licensing review. "
            f"Reach: partnerships@dchub.cloud"),
    }


@competitive_vs_bp.route("/api/v1/competitive/vs/<slug>", methods=["GET"])
def vs_json(slug):
    data = _vs_data(slug)
    if not data:
        return jsonify(ok=False, error=f"Unknown competitor '{slug}'",
                       available=sorted(_COMPETITORS.keys())), 404
    data["ok"] = True
    data["generated_at"] = _dt.datetime.utcnow().isoformat() + "Z"
    data["dchub_facts"] = _DCHUB_FACTS
    return jsonify(data), 200


@competitive_vs_bp.route("/vs/<slug>", methods=["GET"])
def vs_page(slug):
    data = _vs_data(slug)
    if not data:
        # Render an index page listing all available comparisons
        items = "".join(
            f'<li><a href="/vs/{k}">DC Hub vs {v["name"]}</a> — '
            f'{v["category"]}</li>'
            for k, v in sorted(_COMPETITORS.items()))
        html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>DC Hub Competitive Comparisons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:2rem auto;
padding:1rem;color:var(--dch-text);line-height:1.7;background:var(--dch-bg)}}h1{{font-size:2rem}}
li{{margin:.5rem 0}}a{{color:#6366f1;text-decoration:none}}a:hover{{text-decoration:underline}}</style>
</head><body><h1>DC Hub vs the field</h1>
<p>Head-to-head fact comparisons. No adjectives, just numbers.</p>
<ul>{items}</ul>
<script src="/js/dchub-nav.js" defer></script></body></html>"""
        return Response(html, mimetype="text/html", status=404)

    rows_html = "".join(f"""<tr>
      <td><b>{r['label']}</b></td>
      <td class="dchub">{r['dchub']}</td>
      <td class="comp">{r['competitor']}</td>
    </tr>""" for r in data["comparison"])
    strengths_html = "".join(f"<li>{s}</li>" for s in data["their_strengths"])

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>DC Hub vs {data['competitor']} — Side-by-Side · DC Hub</title>
<meta name="description" content="Head-to-head: DC Hub vs {data['competitor']}. Facts on data format, cadence, MCP-native, API access, coverage, pricing, citation license. No marketing fluff.">
<link rel="canonical" href="https://dchub.cloud/vs/{data['slug']}">
<meta property="og:title" content="DC Hub vs {data['competitor']}">
<meta property="og:description" content="Facts side-by-side. No adjectives. {data['category']}.">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"WebPage",
 "name":"DC Hub vs {data['competitor']}",
 "description":"Head-to-head comparison of DC Hub vs {data['competitor']}.",
 "url":"https://dchub.cloud/vs/{data['slug']}",
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}}
}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--dch-bg);color:var(--dch-text);margin:0;padding:2rem 1.5rem;line-height:1.6}}
.wrap{{max-width:980px;margin:0 auto}}
.pill{{display:inline-block;padding:6px 14px;border-radius:99px;background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.4);font-size:.78rem;color:#6366f1;font-weight:600;font-family:'JetBrains Mono',monospace;margin-bottom:1rem}}
h1{{font-size:2.4rem;font-weight:800;letter-spacing:-0.025em;margin:0 0 .25rem}}
h1 .vs{{color:#a855f7;margin:0 .5rem}}
.sub{{color:#9ca3af;margin-bottom:2rem}}
.strengths{{background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:18px;margin-bottom:2rem}}
.strengths h3{{margin:0 0 .5rem;font-size:1rem;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;font-weight:600}}
.strengths ul{{margin:.5rem 0 0;padding-left:1.25rem;color:#cbd5e1}}
table{{width:100%;border-collapse:collapse;background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:10px;overflow:hidden;margin-bottom:2rem}}
th{{text-align:left;padding:12px 14px;background:rgba(255,255,255,.03);color:#9ca3af;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;font-weight:600}}
th.dchub{{color:#10b981}}th.comp{{color:#a855f7}}
td{{padding:14px;border-top:1px solid rgba(255,255,255,.05);vertical-align:top}}
td b{{color:#9ca3af}}
td.dchub{{color:#10b981;font-weight:500}}
td.comp{{color:#cbd5e1}}
.hook{{background:linear-gradient(135deg,rgba(99,102,241,.10),rgba(168,85,247,.10));border:1px solid rgba(129,140,248,.40);border-radius:10px;padding:1.25rem 1.5rem;color:#cbd5e1;margin-bottom:2rem}}
.hook b{{color:#818cf8}}
.foot{{color:var(--dch-text-mute);font-size:.85rem;margin-top:2rem}}
.foot a{{color:#a855f7;text-decoration:none}}
</style>
</head><body>
<div class="wrap">
<div class="pill">FACTS ONLY · NO MARKETING FLUFF</div>
<h1>DC Hub <span class="vs">vs</span> {data['competitor']}</h1>
<p class="sub">{data['category']}. Last updated {_dt.datetime.utcnow().strftime('%Y-%m-%d')}.
JSON: <a href="/api/v1/competitive/vs/{data['slug']}" style="color:#a855f7">/api/v1/competitive/vs/{data['slug']}</a></p>

<div class="strengths">
  <h3>What {data['competitor']} is genuinely strong at</h3>
  <ul>{strengths_html}</ul>
</div>

<table>
<thead><tr>
  <th>Dimension</th>
  <th class="dchub">DC Hub</th>
  <th class="comp">{data['competitor']}</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<div class="hook">
  <b>Partnership pitch</b> — {data['partnership_hook']}
</div>

<p class="foot">Other comparisons: <a href="/vs/dchawk">DCHawk</a> · <a href="/vs/dcbyte">DC Byte</a> · <a href="/vs/dcd">DCD</a> · <a href="/vs/cbre">CBRE</a> · <a href="/vs/jll">JLL</a></p>
<p class="foot">Source of truth: <a href="/industry/pulse">/industry/pulse</a> · Live MCP citations: <a href="/cited-by">/cited-by</a></p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})


# /vs index handler moved to routes/quick_redirects.py — that file
# 301-redirects to /vs/dchawk (a real working competitor page) instead
# of returning the 404-styled index. Cleaner UX.
