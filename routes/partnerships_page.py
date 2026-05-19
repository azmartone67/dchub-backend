"""
/partnerships — the "Switzerland" landing page.

Strategy: DC Hub is the LIVE INTELLIGENCE LAYER beneath everyone's
static research. We compete with no one's PRIMARY product:

  • Brokers (CBRE, JLL, Cushman): we feed their reports with live data
  • Research vendors (DCHawk, dcByte, DC Knowledge, Synergy Research,
    451 Research, Omdia): we make their quarterly cadence current
  • Analysts (Gartner, IDC, Forrester): we provide cite-clean primary data
  • Trade press (DCD, DCK, Data Center Frontier): we provide story-quality data
  • AI platforms (ChatGPT, Claude, Perplexity, Gemini, Groq): we are their MCP

No channel conflict. Complimentary by design. CC-BY-4.0 license.

This page positions that explicitly and provides one-click partnership
intake.
"""

from flask import Blueprint, Response

partnerships_bp = Blueprint("partnerships", __name__)


_HTML = """<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>Partnerships · DC Hub — The Live Data Layer Behind Everyone's Research</title>
<meta name="description" content="DC Hub is the live data layer beneath the data-center research industry. We don't compete — we compliment. Free CC-BY-4.0 data feeds for analysts (CBRE, JLL, Gartner, IDC), research vendors (DCHawk, dcByte, DC Knowledge), trade press (DCD, Data Center Frontier), and AI platforms (ChatGPT, Claude, Perplexity).">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/partnerships">
<meta property="og:title" content="DC Hub Partnerships — The Live Data Layer">
<meta property="og:description" content="No channel conflict. Complimentary by design. The live JSON underneath everyone's quarterly PDFs.">
<style>
:root{--bg:#05060d;--card:#0f1119;--bd:rgba(255,255,255,0.08);--tx:#fafafa;--tx2:#9ca3af;--green:#10b981;--purple:#a855f7;--blue:#3b82f6;--gold:#f59e0b}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Instrument Sans',-apple-system,sans-serif;background:var(--bg);color:var(--tx);line-height:1.6}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem}
.hero{text-align:center;padding:2rem 0 4rem}
.pill{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:99px;background:rgba(168,85,247,0.12);border:1px solid rgba(168,85,247,0.4);font-size:.78rem;color:var(--purple);font-weight:600;font-family:'JetBrains Mono',monospace;margin-bottom:1rem}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--purple);box-shadow:0 0 8px var(--purple);animation:p 1.6s infinite}
@keyframes p{0%,100%{opacity:.5}50%{opacity:1}}
h1{font-size:clamp(2.4rem,5vw,3.6rem);font-weight:800;letter-spacing:-0.025em;margin-bottom:1rem}
.sub{color:var(--tx2);font-size:1.2rem;max-width:720px;margin:0 auto 2rem}
.thesis{background:linear-gradient(135deg,rgba(16,185,129,0.06),rgba(99,102,241,0.06));border:1px solid rgba(16,185,129,0.3);border-radius:14px;padding:2rem;margin:2rem 0;font-size:1.1rem;line-height:1.8;color:#cbd5e1}
.thesis b{color:var(--green)}
h2{font-size:1.6rem;font-weight:800;margin:3rem 0 1rem;letter-spacing:-0.02em}
.partners{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin:1rem 0 2rem}
.partner{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.25rem 1.5rem;transition:border-color .15s}
.partner:hover{border-color:rgba(99,102,241,.4)}
.partner h3{font-size:.85rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.5rem;font-weight:600}
.partner .who{font-size:1rem;font-weight:700;color:var(--tx);margin-bottom:.4rem}
.partner .we{color:#cbd5e1;font-size:.95rem;line-height:1.55}
.partner .we b{color:var(--green)}
.cta{background:linear-gradient(135deg,rgba(245,158,11,0.06),rgba(245,158,11,0.1));border:1px solid rgba(245,158,11,0.4);border-radius:14px;padding:2rem;margin:3rem 0;text-align:center}
.cta h2{margin-top:0}
.cta a{display:inline-block;background:var(--gold);color:#1a1305;padding:.85rem 1.6rem;border-radius:8px;text-decoration:none;font-weight:700;margin:.25rem .5rem;transition:transform .1s}
.cta a:hover{transform:translateY(-1px)}
.cta a.alt{background:transparent;color:var(--gold);border:1px solid var(--gold)}
.principles{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:2rem;margin:2rem 0}
.principles li{margin:.8rem 0;color:#cbd5e1}
.principles li b{color:var(--tx)}
.foot{color:var(--tx2);font-size:.85rem;margin-top:3rem;text-align:center}
.foot a{color:var(--purple);text-decoration:none}
</style></head><body>
<div class="wrap">
<div class="hero">
<div class="pill">SWITZERLAND OF DATA CENTER DATA</div>
<h1>We Don't Compete.<br>We Compliment.</h1>
<p class="sub">DC Hub is the live data layer beneath the data-center research industry.
Brokers, analysts, research vendors, trade press, and AI platforms — all run on our JSON. Free. CC-BY-4.0. No channel conflict.</p>
</div>

<div class="thesis">
<b>The thesis</b> — Data center research is fragmented across paid quarterly PDFs (DCHawk, dcByte, DC Knowledge), CRE-bundled briefs (CBRE, JLL, Cushman), and editorial coverage (DCD, Data Center Frontier). None of them is LIVE. None of them is FREE. None of them is MACHINE-READABLE. DC Hub is all three. We power what everyone else already does — better.
</div>

<h2>Who we partner with (everyone, intentionally)</h2>
<div class="partners">

<div class="partner">
<h3>Commercial Real Estate</h3>
<div class="who">CBRE · JLL · Cushman & Wakefield</div>
<div class="we">Your <b>quarterly reports</b> become the synthesis layer; DC Hub's live JSON becomes the always-current evidence base. Citation-clean, no NDA, CC-BY-4.0. You cite us, you stay ahead of the cycle.</div>
</div>

<div class="partner">
<h3>Research Vendors</h3>
<div class="who">DCHawk · dcByte · DC Knowledge · Synergy Research · 451 Research · Omdia</div>
<div class="we">We don't compete with your <b>research products</b> — we make them current. Embed our live MW/pipeline/M&A feeds in your subscriber portals. We're additive: $9/mo developer plan gives you 500 calls/day to enrich your own dashboards.</div>
</div>

<div class="partner">
<h3>Analysts</h3>
<div class="who">Gartner · IDC · Forrester · McKinsey · Bain</div>
<div class="we">Your <b>analyst clients</b> ask quarterly questions; we answer them in real-time. Free CC-BY-4.0 data for slide decks, white papers, and client briefings. We give you primary-source citations you don't have to license.</div>
</div>

<div class="partner">
<h3>Trade Press</h3>
<div class="who">Data Center Dynamics · Data Center Frontier · Data Center Knowledge · The Register</div>
<div class="we">Pre-built <b>charts + raw data</b> for any DC story you cover. We monitor M&A, DCPI movers, pipeline projects — happy to send you the next 24h's likely story before competitors see it. Free.</div>
</div>

<div class="partner">
<h3>AI Platforms</h3>
<div class="who">ChatGPT · Claude · Perplexity · Gemini · Groq · Cursor · Windsurf</div>
<div class="we">Native <b>MCP server</b> at /mcp — your agents auto-discover our 40 tools without manual integration. Already 100+ teams using DC Hub via MCP. Live citation telemetry: <a href="/cited-by" style="color:var(--blue)">/cited-by</a>.</div>
</div>

<div class="partner">
<h3>Operators · Investors · Brokers</h3>
<div class="who">Hyperscalers · Colo operators · PE firms · Site-selection brokers</div>
<div class="we">Use our <b>API to enrich your CRMs</b>, your due-diligence workflows, your site-selection pipelines. $9/mo dev plan for daily-use teams; $199/mo PRO for broker shops that need multi-site comparators.</div>
</div>

</div>

<h2>Our partnership principles</h2>
<div class="principles">
<ol style="padding-left:1.25rem">
<li><b>No channel conflict.</b> We don't sell research, brokerage, advisory, or consulting. We only sell access to live data.</li>
<li><b>CC-BY-4.0 by default.</b> Every public surface is citation-clean — no license review needed. Just attribution.</li>
<li><b>White-label friendly.</b> Embed our data in your client portals. Brand it however you like. The data is the data.</li>
<li><b>API-first, MCP-native.</b> Our /.well-known/mcp.json is the universal manifest; our REST API is OpenAPI-spec'd. Self-serve from day one.</li>
<li><b>Reasonable pricing.</b> $0 free tier (teaser). $9/mo developer (500/day, full data). $199/mo PRO (multi-site comparator). Enterprise = SLA + 10K/day + custom. No $25K seats.</li>
<li><b>Open about everything.</b> Live audit dashboard, live citation telemetry, methodology pages — total transparency.</li>
</ol>
</div>

<div class="cta">
<h2>Want to partner? Three paths.</h2>
<p style="color:#cbd5e1;margin:.5rem 0 1.5rem">Email <a href="mailto:partnerships@dchub.cloud" style="color:var(--gold)">partnerships@dchub.cloud</a> · we reply same-day.</p>
<a href="mailto:partnerships@dchub.cloud?subject=Partnership%20discussion">Email partnerships</a>
<a href="/.well-known/mcp.json" class="alt">View MCP manifest</a>
<a href="/industry/pulse" class="alt">See live stat sheet</a>
</div>

<p class="foot">
The live data layer beneath the data-center research industry.<br>
<a href="/">DC Hub</a> · <a href="/industry/pulse">Weekly stat sheet</a> · <a href="/cited-by">Live citations</a> · <a href="/vs/dchawk">Head-to-head: DCHawk</a> · <a href="/vs/cbre">CBRE</a> · <a href="/vs/dcbyte">DCByte</a> · <a href="/vs/jll">JLL</a> · <a href="/AGENTS.md">AGENTS.md</a>
</p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""


@partnerships_bp.route("/partnerships", methods=["GET"], strict_slashes=False)
def partnerships_page():
    return Response(_HTML, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})
