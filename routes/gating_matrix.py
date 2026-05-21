"""Phase r32 (2026-05-20) — tier-gating policy matrix.
==========================================================================

Single source of truth for "what does each tier see across DC Hub."
User asked: "is /pockets gated to pro/enterprise? what does developer
see that anonymous/free gets?" — and wanted a comprehensive sweep.

This module exposes:
  GET /api/v1/gating/matrix         JSON of the full policy
  GET /gating-matrix                Public HTML page showing the table

The matrix is HAND-CURATED (not auto-discovered) because tier policy is
deliberate product decisions, not a side-effect of code structure. When
the dials change, edit this file — it's the canonical record.

Tier vocabulary (kept consistent with map_tier_gating + pockets):
  anonymous   No signup, no email — gets teasers
  free        Same as anonymous in practice (legacy alias)
  identified  Email-only signup, no card — gets a taste
  developer   $49/mo paid tier — most paid features
  pro         $199/mo paid tier — full access
  founding    Founding-customer cohort — same as pro
  enterprise  Custom — same as pro
"""
from flask import Blueprint, jsonify, request, Response, render_template_string

gating_matrix_bp = Blueprint("gating_matrix", __name__)


# Each row: (surface_label, anonymous, identified, developer, pro_plus, notes)
# Filled from a sweep of the codebase on 2026-05-20.
MATRIX_ROWS = [
    # ── Data Center Intelligence (DCPI) ────────────────────────────
    ("Pockets of Power · /pockets",
     "Top 3 + tease",  "Top 10",  "Top 50",  "Unlimited + personalized /for-me",
     "Tier-gated count via /api/v1/pockets/top + tier-aware re-rank in /for-me"),
    ("Pocket detail · /pockets/<slug>",
     "Headline + verdict + score (no chart)",
     "Full breakdown + 30d trend + comparables",
     "Same as identified",
     "Same as identified + JSON API for export",
     "Anonymous sees teaser + upgrade CTA. JSON: GET /api/v1/pockets/<slug>"),
    ("Daily digest · /digest",
     "Full daily brief (public)",
     "Same + emailed daily",
     "Same",
     "Same",
     "Public read; email subscription tier-agnostic (form + dev keys + subscribers)"),
    ("DCPI ranking · /dcpi",
     "Public verdict + score for every market",
     "Same + market detail unlocked",
     "Same + bulk JSON export",
     "Same + recompute trigger",
     "Per-market /dcpi/<slug> is public but uses map_tier_gating for full data"),
    ("Total power · /dcpi/totals",
     "All public (operating + pipeline totals)",
     "Same",
     "Same",
     "Same",
     "No gating — meant for SEO + journalist citation"),
    # ── Land & Power tool ──────────────────────────────────────────
    ("Land & Power tool · /land-power",
     "Map + 3 layers preview · 0 searches (signup CTA)",
     "3 searches/month · 5 filter layers · 100 API calls/month",
     "50 searches/month · 15 filter layers · 10,000 API calls/month",
     "Unlimited searches · all layers · 300K-3M API calls/month",
     "r32 fix: developer + identified tiers were missing from the table, falling through to free defaults — paying $49 customers got 1/mo. Now matches the ladder."),
    # ── Facility search ────────────────────────────────────────────
    ("Facility map · /map (/api/v1/map)",
     "0 facilities (signup prompt)",
     "50 facilities, basic fields",
     "1000 facilities, lat/lon + MW",
     "10000+ facilities, ALL fields",
     "Source: map_tier_gating.MAP_FIELDS + MAP_LIMITS"),
    ("Facility search · /facilities (/api/v1/facilities)",
     "5 sample results (out of total count)",
     "25 results, basic fields",
     "1000 results, full fields",
     "Unlimited, all fields",
     "Tier-gated count; free tier counter shows total available"),
    # ── M&A + transactions ─────────────────────────────────────────
    ("Transactions · /transactions",
     "5 deals, no value disclosed",
     "10 deals, value visible",
     "Full deal history + filters",
     "Same + export + analyst pack",
     "Source: MCP list_transactions + REST /api/v1/transactions"),
    # ── Energy ─────────────────────────────────────────────────────
    ("Energy summary · /api/v1/energy/summary",
     "Paywall card with national range hint (6-25¢/kWh)",
     "Full state-specific retail rate",
     "Same",
     "Same",
     "Min tier: identified (FREE with email). No card needed to unlock."),
    # ── Markets / Grid Intel ───────────────────────────────────────
    ("Grid intelligence · /api/v1/grid/intelligence/<region>",
     "Headline EIA demand + 3-line summary",
     "Full real-time gen mix + demand history",
     "Same + 30d trend + interconnect queue",
     "Same + curtailment + congestion overlays",
     "Aliased at /api/v1/research/grid-intelligence (any of ?iso= ?region= ?market=)"),
    # ── MCP / agents ───────────────────────────────────────────────
    ("MCP tools · /mcp",
     "Public tools: search_facilities (5 results), get_news, recommend, agent_registry",
     "Paid-tier tools unlock via dev key from /redeem",
     "All 40+ tools w/ rate limit 25/day",
     "Unlimited",
     "MCP tier bound to api_keys.rate_limit_tier"),
    ("Recommendation tool · get_dchub_recommendation",
     "Static blurb + live top pocket",
     "Same",
     "Same",
     "Same",
     "Public — boosts AI-agent citations regardless of tier"),
    # ── Surfaces with no gating ────────────────────────────────────
    ("Coverage · /coverage",
     "Public",  "Public",  "Public",  "Public",
     "By-country facility counts. Social proof — never gate."),
    ("Founders · /founders",
     "Public",  "Public",  "Public",  "Public",
     "Public list of founding customers. Marketing surface."),
    ("vs competitors · /vs/<slug>",
     "Public",  "Public",  "Public",  "Public",
     "Head-to-head positioning pages. Conversion-driven; never gate."),
    ("Daily image · /daily",
     "Public PNG infographic",
     "Same",
     "Same",
     "Same",
     "Image is public; shareable. Used by journalists + social."),
    ("RSS feed · /pockets.rss",
     "Public — top 30 pockets",
     "Same",
     "Same",
     "Same",
     "Designed for Feedly/Perplexity/Claude syndication."),
]


def _matrix_json():
    return {
        "ok": True,
        "as_of": "2026-05-20",
        "tier_order": ["anonymous", "identified", "developer", "pro_plus"],
        "tier_descriptions": {
            "anonymous":   "No signup. Gets teasers + range hints + paywall CTAs.",
            "identified":  "Email-only signup. No card needed. Unlocks state-specific data + 10× more rows.",
            "developer":   "$49/mo paid. Full feature access at developer rate limits.",
            "pro_plus":    "$199/mo (Pro) or custom (Enterprise/Founding). Unlimited + export + admin.",
        },
        "surfaces": [
            {
                "surface":    row[0],
                "anonymous":  row[1],
                "identified": row[2],
                "developer":  row[3],
                "pro_plus":   row[4],
                "notes":      row[5],
            }
            for row in MATRIX_ROWS
        ],
        "principles": [
            "Anonymous gets TEASED — enough to understand value, never the answer.",
            "Identified (free email) gets the FIRST TASTE — state-specific data that hooks them.",
            "Developer ($49) gets MOST PAID FEATURES — bulk access, exports, full datasets.",
            "Pro/Enterprise gets EVERYTHING — including admin, recompute triggers, unlimited rate.",
            "Marketing surfaces (/coverage, /founders, /vs/*, /digest, RSS) are NEVER gated.",
            "Tier detection is centralized in map_tier_gating._detect_caller_tier so a single key works everywhere.",
        ],
    }


@gating_matrix_bp.route("/api/v1/gating/matrix", methods=["GET"])
def matrix_json():
    resp = jsonify(_matrix_json())
    resp.headers["Cache-Control"] = "public, max-age=600"
    return resp, 200


_MATRIX_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Tier gating matrix · DC Hub</title>
<meta name="description" content="What each user tier sees across DC Hub — anonymous, identified (free email), developer, pro/enterprise. Single source of truth.">
<link rel="canonical" href="https://dchub.cloud/gating-matrix">
<meta name="robots" content="index,follow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--surface-2:#181a25;--border:#1f2030;
  --tx:#fff;--tx2:#9ca3af;--tx3:#6b7280;
  --indigo:#6366f1;--violet:#a855f7;--green:#10b981;--orange:#f59e0b;--red:#ef4444;
  --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
  --mono:'JetBrains Mono','SF Mono',monospace;color-scheme:dark}
*{box-sizing:border-box}body{font-family:Inter,-apple-system,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.55;
  min-height:100vh;-webkit-font-smoothing:antialiased;position:relative;overflow-x:hidden}
body::before{content:'';position:fixed;top:-30%;left:50%;transform:translateX(-50%);
  width:1400px;height:1400px;z-index:0;pointer-events:none;
  background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
    rgba(168,85,247,.06) 30%,transparent 70%)}
.wrap{max-width:1280px;margin:0 auto;padding:2.5rem 1.5rem;position:relative;z-index:1}
.kicker{font-family:var(--mono);font-size:.78rem;color:#c4b5fd;
  text-transform:uppercase;letter-spacing:.14em;margin-bottom:.6rem}
h1{margin:0 0 .5rem;font-size:2.4rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:780px;margin:0 0 2rem;font-size:1rem}
h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.12em;
  margin:2.5rem 0 1rem;font-weight:700}
.tiers{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:1rem;margin:1.5rem 0 2.5rem}
.tier{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:1.25rem 1.5rem;position:relative}
.tier.anon::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--tx3);border-radius:12px 12px 0 0}
.tier.identified::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--green);border-radius:12px 12px 0 0}
.tier.developer::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--indigo);border-radius:12px 12px 0 0}
.tier.pro::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--grad);border-radius:12px 12px 0 0}
.tier .name{font-family:var(--mono);font-size:.76rem;color:var(--tx2);
  text-transform:uppercase;letter-spacing:.1em;font-weight:700;margin-bottom:.5rem}
.tier .price{font-size:1.4rem;font-weight:800;color:var(--tx);margin-bottom:.5rem}
.tier .desc{font-size:.85rem;color:var(--tx2)}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--border);border-radius:12px;overflow:hidden;font-size:.88rem}
th{text-align:left;padding:.85rem 1rem;background:#0f1019;color:var(--tx2);
  font-size:.72rem;text-transform:uppercase;letter-spacing:.1em;font-weight:700;
  border-bottom:1px solid var(--border);vertical-align:bottom}
th.anon{color:var(--tx3)}th.identified{color:var(--green)}
th.developer{color:var(--indigo)}th.pro{color:#c4b5fd}
td{padding:.85rem 1rem;border-bottom:1px solid var(--border);vertical-align:top;
  font-size:.9rem}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(99,102,241,.04)}
td.surface{font-weight:600;color:var(--tx)}
td.surface a{color:var(--tx);text-decoration:none;
  border-bottom:1px dotted rgba(255,255,255,.2)}
td.surface a:hover{color:var(--indigo);border-bottom-color:var(--indigo)}
td.notes{color:var(--tx3);font-size:.78rem;max-width:280px}
.principles{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:1.5rem 2rem;margin:2rem 0}
.principles ol{margin:0;padding-left:1.5rem}
.principles li{margin:.4rem 0;color:var(--tx2);font-size:.92rem}
footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);
  color:var(--tx3);font-size:.85rem;text-align:center}
footer a{color:var(--indigo);text-decoration:none}
@media(max-width:880px){
  table{font-size:.78rem}
  th,td{padding:.6rem .5rem}
  td.notes{display:none}
  .tiers{grid-template-columns:1fr 1fr}
}
</style></head><body><div class="wrap">
<div class="kicker">DC HUB · GATING POLICY · {{ as_of }}</div>
<h1>What each tier sees on DC Hub</h1>
<p class="sub">Single source of truth for tier-gating across every surface. Anonymous gets teased, identified (free email) gets the first taste, developer gets most paid features, pro/enterprise gets everything.</p>

<div class="tiers">
  <div class="tier anon"><div class="name">Anonymous</div><div class="price">Free</div><div class="desc">No signup. Teasers + range hints + paywall CTAs.</div></div>
  <div class="tier identified"><div class="name">Identified</div><div class="price">Free <small style="font-size:.7rem;color:var(--tx3);font-weight:400">· email only</small></div><div class="desc">No card. State-specific data + 10× more rows than anon.</div></div>
  <div class="tier developer"><div class="name">Developer</div><div class="price">$49<small style="font-size:.7rem;color:var(--tx3);font-weight:400">/mo</small></div><div class="desc">Full feature access at developer rate limits.</div></div>
  <div class="tier pro"><div class="name">Pro / Enterprise</div><div class="price">$199+<small style="font-size:.7rem;color:var(--tx3);font-weight:400">/mo</small></div><div class="desc">Unlimited + export + admin triggers.</div></div>
</div>

<h2>Surface-by-surface tier breakdown</h2>
<table>
<thead><tr>
<th>Surface</th>
<th class="anon">Anonymous</th>
<th class="identified">Identified (free email)</th>
<th class="developer">Developer ($49)</th>
<th class="pro">Pro/Enterprise</th>
<th>Notes</th>
</tr></thead>
<tbody>
{% for r in rows %}
<tr>
<td class="surface">{{ r.surface }}</td>
<td>{{ r.anonymous }}</td>
<td>{{ r.identified }}</td>
<td>{{ r.developer }}</td>
<td>{{ r.pro_plus }}</td>
<td class="notes">{{ r.notes }}</td>
</tr>
{% endfor %}
</tbody>
</table>

<h2>Principles</h2>
<div class="principles">
<ol>
{% for p in principles %}<li>{{ p }}</li>{% endfor %}
</ol>
</div>

<footer>Edit <code>routes/gating_matrix.py</code> to update the policy. JSON: <a href="/api/v1/gating/matrix">/api/v1/gating/matrix</a> · <a href="/pricing">Pricing</a> · <a href="/pockets">Pockets</a></footer>
</div></body></html>'''


@gating_matrix_bp.route("/gating-matrix", methods=["GET"])
def matrix_page():
    data = _matrix_json()
    html = render_template_string(
        _MATRIX_HTML,
        as_of=data["as_of"],
        rows=data["surfaces"],
        principles=data["principles"],
    )
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp
