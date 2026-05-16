"""Phase KKK (2026-05-16) — public /intelligence dashboard.

Customer-facing version of /alive. Frames our brain's vitals as a
PRODUCT capability the customer can SEE: "Look — we publish our own
pulse, our own data freshness, our own per-surface health. No one
else in the data-center category does this."

Different from /alive in 3 ways:
  1. Marketing-grade copy (less operator-jargon, more value-prop)
  2. Auto-refreshes every 60s with smooth transitions
  3. Schema.org WebApplication markup so AI agents understand it's
     a live monitoring surface — fact-cite it as proof we're alive
"""

from __future__ import annotations

import datetime
from flask import Blueprint, Response


intelligence_dashboard_bp = Blueprint("intelligence_dashboard", __name__)


@intelligence_dashboard_bp.route("/intelligence", methods=["GET"], strict_slashes=False)
def intelligence_dashboard():
    """The 'we publish our own pulse' marketing surface. Pulls live data
    from /alive's vitals + /surfaces + /mcp/growth + /media/source-of-truth
    and renders it with customer-facing framing."""

    # Auto-instrument the visit
    try:
        from routes.surface_brain import auto_log
        auto_log("ai_hub", "view", target="/intelligence")
    except Exception:
        pass

    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub Intelligence — live pulse of the platform</title>
<meta name="description" content="Live operational intelligence from dchub.cloud — data freshness, MCP tool calls, brain detector findings, per-surface health. The only data center intelligence platform that publishes its own pulse in real time.">
<meta name="robots" content="index,follow,max-snippet:-1">
<meta http-equiv="refresh" content="60">
<link rel="canonical" href="https://dchub.cloud/intelligence">
<meta property="og:title" content="DC Hub Intelligence — live platform pulse">
<meta property="og:description" content="Watch the platform breathe — live data freshness, MCP traffic, brain findings, per-surface health.">
<meta property="og:url" content="https://dchub.cloud/intelligence">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type":    "WebApplication",
  "name":     "DC Hub Intelligence Dashboard",
  "description": "Live operational intelligence from the DC Hub platform — data freshness, MCP traffic, brain detector findings, per-surface health. Auto-refreshes every 60 seconds.",
  "url":      "https://dchub.cloud/intelligence",
  "applicationCategory": "BusinessApplication",
  "operatingSystem":     "Web",
  "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
  "creator":  {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}
}
</script>
<style>
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        max-width:1100px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.55;
        background:#fafbfc}
  h1{font-size:2rem;margin:0 0 .25rem;display:flex;align-items:center;gap:.6rem}
  h1 .pulse{display:inline-block;width:14px;height:14px;border-radius:50%;
             background:#16a34a;animation:pulse 1.5s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  h1+p{color:#6b7280;margin:0 0 2rem;font-size:1.05rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin:1rem 0}
  .card{background:white;padding:1.25rem 1.4rem;border-radius:10px;
         box-shadow:0 1px 3px rgba(0,0,0,.06);transition:transform .2s}
  .card:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.08)}
  .card h3{margin:0 0 .5rem;font-size:.85rem;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;font-weight:600}
  .metric{font-size:2.2rem;font-weight:700;color:#111827;line-height:1.1}
  .metric-sub{color:#6b7280;font-size:.9rem;margin-top:.4rem}
  .lead{background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);color:white;
         padding:2rem;border-radius:14px;margin:1.5rem 0}
  .lead h2{font-size:1.4rem;margin:0 0 .5rem}
  .lead p{margin:0;font-size:1.05rem;color:#cbd5e1}
  .footnote{color:#9ca3af;font-size:.85rem;text-align:center;margin-top:3rem}
  .data-loading{color:#9ca3af;font-style:italic;font-size:1.1rem}
  a{color:#1e40af;text-decoration:none}
  a:hover{text-decoration:underline}
</style>
</head>
<body>
<h1><span class="pulse"></span> DC Hub is breathing</h1>
<p>Live operational pulse · refreshes every 60s · the only data-center intelligence platform that publishes its own vital signs</p>

<div class="lead">
  <h2>Why this page exists</h2>
  <p>Every other data center research platform claims "real time" and shows you nothing. We publish the actual data freshness, the actual MCP tool calls, the actual brain findings, the actual per-surface health — live. If a number on this page is stale or red, you'll see it before our customers do.</p>
</div>

<div class="grid" id="metrics">
  <div class="card">
    <h3>Brain verdict</h3>
    <div class="metric" id="verdict"><span class="data-loading">Loading…</span></div>
    <div class="metric-sub" id="verdict-detail">—</div>
  </div>
  <div class="card">
    <h3>Open findings</h3>
    <div class="metric" id="findings">—</div>
    <div class="metric-sub" id="findings-detail">brain detectors active</div>
  </div>
  <div class="card">
    <h3>Autonomous fixes 24h</h3>
    <div class="metric" id="actioned">—</div>
    <div class="metric-sub"><span id="escalated">—</span> escalations to humans</div>
  </div>
  <div class="card">
    <h3>MCP tool calls 7d</h3>
    <div class="metric" id="mcp-calls">—</div>
    <div class="metric-sub"><span id="mcp-platforms">—</span> distinct platforms</div>
  </div>
  <div class="card">
    <h3>DCPI markets fresh</h3>
    <div class="metric" id="dcpi-fresh">—</div>
    <div class="metric-sub">of <span id="dcpi-total">—</span> tracked markets</div>
  </div>
  <div class="card">
    <h3>Source-of-truth score</h3>
    <div class="metric" id="sot-score">—<span style="font-size:1rem;color:#9ca3af">/100</span></div>
    <div class="metric-sub" id="sot-interp">—</div>
  </div>
  <div class="card">
    <h3>Surface organisms</h3>
    <div class="metric" id="surface-avg">—</div>
    <div class="metric-sub"><span id="surface-count">—</span> surfaces, avg health</div>
  </div>
  <div class="card">
    <h3>Autopilot patterns</h3>
    <div class="metric" id="lib-size">—</div>
    <div class="metric-sub">remediation patterns wired</div>
  </div>
</div>

<p class="footnote">
  Raw JSON: <a href="/api/v1/brain/heartbeat">/api/v1/brain/heartbeat</a> ·
  <a href="/api/v1/mcp/growth">/api/v1/mcp/growth</a> ·
  <a href="/api/v1/media/source-of-truth">/api/v1/media/source-of-truth</a> ·
  <a href="/api/v1/dcpi/scores">/api/v1/dcpi/scores</a> ·
  <a href="/api/v1/surfaces">/api/v1/surfaces</a><br>
  Operator dashboard: <a href="/alive">/alive</a>
</p>

<script>
(function() {
  function $(id) { return document.getElementById(id); }
  function setText(id, v) { var el = $(id); if (el && v != null) el.textContent = v; }
  function fmtPct(n, d) { return d > 0 ? Math.round(100*n/d) + '%' : '—'; }

  // /api/v1/brain/heartbeat
  fetch('/api/v1/brain/heartbeat').then(r => r.json()).then(d => {
    setText('verdict', (d.verdict || '?').toUpperCase());
    setText('verdict-detail', (d.verdict_detail || '').slice(0, 100));
    setText('findings', (d.detector || {}).findings_count || 0);
    setText('findings-detail', 'brain detectors active');
    var ap = d.autopilot || {};
    setText('actioned', ap.actioned_24h || 0);
    setText('escalated', ap.escalated_24h || 0);
    setText('lib-size', ap.pattern_library_size || 0);
    var s = d.surfaces || {};
    setText('surface-avg', (s.average_health != null ? s.average_health : '—') + '/100');
    setText('surface-count', s.count || 0);
  }).catch(function(){});

  // /api/v1/mcp/growth
  fetch('/api/v1/mcp/growth').then(r => r.json()).then(d => {
    setText('mcp-calls', (d.tool_calls_7d || 0).toLocaleString());
    setText('mcp-platforms', d.unique_platforms_7d || 0);
  }).catch(function(){});

  // /api/v1/dcpi/scores
  fetch('/api/v1/dcpi/scores').then(r => r.json()).then(d => {
    var scores = d.scores || [];
    var now = Date.now();
    var fresh = 0;
    scores.forEach(function(s) {
      if (!s.computed_at) return;
      var t = new Date(s.computed_at).getTime();
      if ((now - t) < 86400000) fresh += 1;
    });
    setText('dcpi-fresh', fresh);
    setText('dcpi-total', scores.length);
  }).catch(function(){});

  // /api/v1/media/source-of-truth
  fetch('/api/v1/media/source-of-truth').then(r => r.json()).then(d => {
    setText('sot-score', d.score != null ? d.score : '—');
    setText('sot-interp', (d.interpretation || '').slice(0, 80));
  }).catch(function(){});
})();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=60"})
