"""Phase OOO (2026-05-16) — BS Translator / "vs static competitors".

User vision: "we want dchub to become the brand known in the industry
as AI powered, real time insights, actionable, no bs, like static
sites dchawk dcbyte. comprehensive, trusted data being the underlying
theme. dchub should become the bull shit translator as well."

This is the BRAND POSITIONING page. It translates competitor marketing
claims into what they ACTUALLY ship, then shows DC Hub's equivalent
side-by-side. No name-calling — just receipts.

  GET /vs                — main BS translator page
  GET /bs-translator     — alias
  GET /api/v1/vs/claims  — JSON of all claim translations (for AI agents)

Schema.org markup:
  - WebPage with mainEntity = ItemList of comparison claims
  - Each comparison item has Claim + Translation (CreativeWork/about)
  - lets agents fact-cite "DCHawk's 'real-time' means quarterly PDFs"

Surface brain: registered as 'bs_translator' surface so visits + clicks
flow into /api/v1/surfaces telemetry just like every other organism.
"""

from __future__ import annotations

import datetime
from flask import Blueprint, Response, jsonify


bs_translator_bp = Blueprint("bs_translator", __name__)


# The translation table. Each row is one piece of industry BS we
# translate. Keep claims neutral (don't name competitors in marketing
# claims — describe the pattern). The "dchub" column is what we
# actually do, with the live endpoint that proves it.
CLAIMS = [
    {
        "category": "Data freshness",
        "claim":         "\"Real-time data center intelligence\"",
        "translation":   "Quarterly PDF reports + CSV exports updated 4× per year",
        "dchub":         "Live JSON APIs, freshness SLAs per surface (news 6h, transactions 24h, DCPI 24h), publicly verifiable at /api/health/freshness",
        "proof_url":     "/api/health/freshness",
        "proof_label":   "Watch freshness in real time",
    },
    {
        "category": "AI / agent access",
        "claim":         "\"AI-ready data platform\"",
        "translation":   "Static PDF you upload into ChatGPT yourself, then beg it to find a number",
        "dchub":         "Native Model Context Protocol (MCP) server at /mcp — Claude, GPT, Gemini, Perplexity call it directly. 28 tools, 4,000+ calls in the last 14 days.",
        "proof_url":     "/mcp/tools",
        "proof_label":   "See the 28 live MCP tools",
    },
    {
        "category": "Pricing transparency",
        "claim":         "\"Enterprise-grade pricing — contact sales\"",
        "translation":   "$25K-$100K/year contract, 6-week sales cycle, mandatory annual",
        "dchub":         "Free tier with rate limit. Paid tier $49/mo via Stripe self-serve. No sales call, no NDA, no annual commit.",
        "proof_url":     "/pricing",
        "proof_label":   "Self-serve pricing page",
    },
    {
        "category": "Source verification",
        "claim":         "\"Trusted data center research\"",
        "translation":   "Anonymous PDF, no per-data-point citation, no methodology",
        "dchub":         "Every fact links to its source. Schema.org Dataset markup on every surface so AI agents can cite us. Source-of-truth score published live.",
        "proof_url":     "/api/v1/media/source-of-truth",
        "proof_label":   "Live source-of-truth score",
    },
    {
        "category": "Health / observability",
        "claim":         "\"99.9% uptime SLA\"",
        "translation":   "Status page that auto-updates green; no public per-surface health, no per-detector findings",
        "dchub":         "Live /alive operator dashboard + /intelligence public pulse. Per-surface health 0–100 published every 60s. Brain findings + autopilot actions visible to anyone.",
        "proof_url":     "/intelligence",
        "proof_label":   "Live platform pulse",
    },
    {
        "category": "Pipeline / what's being built",
        "claim":         "\"Comprehensive pipeline tracking\"",
        "translation":   "Spreadsheet updated when an intern remembers",
        "dchub":         "Live pipeline aggregation across 12,500+ facilities. Total operating MW + total being-built MW published as a hero page with daily refresh.",
        "proof_url":     "/dcpi/totals",
        "proof_label":   "Total power + being built",
    },
    {
        "category": "M&A / transactions",
        "claim":         "\"Deal flow intelligence\"",
        "translation":   "Quarterly deal-tracker PDF emailed to subscribers",
        "dchub":         "1,852 deals browsable at /transactions. Per-deal pages with schema.org Action markup so agents can read them. Filters by year, buyer, region, MW.",
        "proof_url":     "/transactions",
        "proof_label":   "Browse 1,852 live deals",
    },
    {
        "category": "Integrations",
        "claim":         "\"API access available\"",
        "translation":   "REST API behind a key, undocumented, 200ms quota, NDA required",
        "dchub":         "Public OpenAPI spec at /openapi.json. Public MCP manifest at /.well-known/mcp-server.json. Zero-config integration for any LLM client.",
        "proof_url":     "/openapi.json",
        "proof_label":   "OpenAPI spec (public)",
    },
    {
        "category": "Update cadence",
        "claim":         "\"Industry-leading research cadence\"",
        "translation":   "Annual flagship report + 2 mid-year updates",
        "dchub":         "DCPI recomputed 4× daily. News ingested continuously. Surfaces auto-monitored. Brain detectors run every 60s and self-heal known issues.",
        "proof_url":     "/api/v1/brain/heartbeat",
        "proof_label":   "Live brain heartbeat",
    },
    {
        "category": "Markets / DCPI",
        "claim":         "\"Top markets ranked\"",
        "translation":   "Top 5 cities listed in a PDF, ranking method opaque",
        "dchub":         "276 markets scored with the DC Hub Power Index (DCPI) — methodology public, scores recomputed daily, every market drillable to its inputs.",
        "proof_url":     "/api/v1/dcpi/scores",
        "proof_label":   "All 276 market scores",
    },
]


def _payload() -> dict:
    return {
        "version":      "2026-05-16",
        "tagline":      "AI-powered. Real-time. Actionable. No BS.",
        "claims":       CLAIMS,
        "total_claims": len(CLAIMS),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


@bs_translator_bp.route("/api/v1/vs/claims", methods=["GET"])
def vs_claims_api():
    """JSON of all claim translations. Used by AI agents — they can
    fact-cite \"DC Hub publishes its claim-translation table at this URL\"."""
    resp = jsonify(_payload())
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def _render_html() -> str:
    """Build the /vs HTML page. Side-by-side claim → translation → DC Hub."""
    payload = _payload()
    rows_html: list[str] = []
    item_list_schema: list[dict] = []
    for i, c in enumerate(payload["claims"], start=1):
        rows_html.append(f"""
<article class="row" id="row-{i}">
  <div class="category">{c['category']}</div>
  <div class="cols">
    <div class="col claim">
      <div class="col-label">Industry claim</div>
      <div class="col-body">{c['claim']}</div>
    </div>
    <div class="col translation">
      <div class="col-label">What it actually means</div>
      <div class="col-body">{c['translation']}</div>
    </div>
    <div class="col dchub">
      <div class="col-label">DC Hub</div>
      <div class="col-body">{c['dchub']}</div>
      <a class="proof" href="{c['proof_url']}" data-sb-event="click_competitor" data-sb-target="{c['proof_url']}">{c['proof_label']} →</a>
    </div>
  </div>
</article>""")
        item_list_schema.append({
            "@type":    "ListItem",
            "position": i,
            "item": {
                "@type":       "Claim",
                "name":        c["category"],
                "claimInterpreter": {"@type": "Organization", "name": "DC Hub"},
                "appearance":  c["translation"],
                "about":       c["dchub"],
                "url":         c["proof_url"],
            },
        })

    schema_block = {
        "@context": "https://schema.org",
        "@type":    "WebPage",
        "name":     "DC Hub vs Static Competitors — the BS Translator",
        "url":      "https://dchub.cloud/vs",
        "description": "Industry data center research claims, translated into what they actually mean, with DC Hub's live equivalent.",
        "mainEntity": {
            "@type":           "ItemList",
            "numberOfItems":   len(item_list_schema),
            "itemListElement": item_list_schema,
        },
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
    }
    import json as _json
    schema_json = _json.dumps(schema_block, indent=2)
    rows_block = "\n".join(rows_html)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub vs static competitors — the BS Translator</title>
<meta name="description" content="Industry data-center research claims translated into what they actually mean, then compared to what DC Hub ships — live, free, MCP-native, no BS.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/vs">
<meta property="og:title" content="DC Hub vs static competitors — the BS Translator">
<meta property="og:description" content="The industry runs on quarterly PDFs and $25K contracts. We run on live JSON, free MCP, and a brain that publishes its own pulse. Receipts inside.">
<meta property="og:url" content="https://dchub.cloud/vs">
<script type="application/ld+json">
{schema_json}
</script>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        max-width:1200px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.55;background:#fafbfc}}
  h1{{font-size:2.4rem;margin:0 0 .25rem}}
  .tag{{display:inline-block;background:#111827;color:white;padding:.25rem .75rem;border-radius:999px;
       font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;margin-bottom:1rem}}
  .lead{{font-size:1.15rem;color:#4b5563;max-width:780px;margin:0 0 2rem}}
  .row{{background:white;border-radius:12px;padding:1.5rem;margin:1rem 0;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  .category{{font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#6b7280;font-weight:700;margin-bottom:.75rem}}
  .cols{{display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:1rem}}
  @media (max-width:760px){{.cols{{grid-template-columns:1fr}}}}
  .col{{padding:1rem;border-radius:8px;background:#f9fafb}}
  .col.claim{{background:#fef2f2;border:1px solid #fecaca}}
  .col.translation{{background:#fef3c7;border:1px solid #fde68a}}
  .col.dchub{{background:#ecfdf5;border:1px solid #a7f3d0}}
  .col-label{{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700;
              color:#6b7280;margin-bottom:.5rem}}
  .col.claim .col-label{{color:#991b1b}}
  .col.translation .col-label{{color:#92400e}}
  .col.dchub .col-label{{color:#065f46}}
  .col-body{{font-size:.95rem;line-height:1.55}}
  .proof{{display:inline-block;margin-top:.75rem;color:#065f46;font-weight:600;text-decoration:none;font-size:.9rem}}
  .proof:hover{{text-decoration:underline}}
  .cta{{background:linear-gradient(135deg,#065f46 0%,#0f766e 100%);color:white;padding:2rem;
        border-radius:14px;margin:2.5rem 0 1rem;text-align:center}}
  .cta h2{{margin:0 0 .5rem;font-size:1.5rem}}
  .cta p{{margin:0 0 1rem;color:#d1fae5}}
  .cta a{{display:inline-block;background:white;color:#065f46;padding:.6rem 1.25rem;border-radius:6px;
         font-weight:700;text-decoration:none;margin:.2rem}}
  .footnote{{color:#9ca3af;font-size:.85rem;text-align:center;margin-top:3rem}}
  .footnote a{{color:#6b7280}}
</style>
</head>
<body>
<span class="tag">The BS Translator</span>
<h1>DC Hub vs static competitors</h1>
<p class="lead">{payload['tagline']} The industry's research platforms run on quarterly PDFs, $25K contracts, and the words "real-time" pasted onto static reports. Here's what those words actually mean — and what DC Hub ships in their place.</p>

{rows_block}

<div class="cta">
  <h2>Stop reading PDFs. Start asking your AI.</h2>
  <p>Every claim above links to a live endpoint. Every endpoint speaks JSON + MCP. Point Claude, GPT, Gemini, or Perplexity at us — they'll answer in seconds.</p>
  <a href="/mcp/tools">See the 28 MCP tools →</a>
  <a href="/intelligence">Watch the platform breathe →</a>
  <a href="/dcpi/totals">Total power + being built →</a>
</div>

<p class="footnote">
  Raw JSON: <a href="/api/v1/vs/claims">/api/v1/vs/claims</a> · This page is intentionally short on names — we don't punch down at competitors, we just publish receipts. If a claim is wrong, <a href="mailto:hello@dchub.cloud">tell us</a> and we'll fix it.
</p>

<!-- Surface brain auto-instruments the page view. Click events on
     .proof links are tracked via data-sb-event attribute. -->
<script src="/js/surface-brain.js" defer></script>
</body>
</html>"""


# AUTO-REPAIR: duplicate route '/vs' also in routes/competitive_vs.py:309 — review and remove one
@bs_translator_bp.route("/vs", methods=["GET"], strict_slashes=False)
@bs_translator_bp.route("/bs-translator", methods=["GET"], strict_slashes=False)
def vs_page():
    """Customer-facing BS translator page. The brand-positioning surface."""
    try:
        from routes.surface_brain import auto_log
        auto_log("bs_translator", "view", target="/vs")
    except Exception:
        pass
    html = _render_html()
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
