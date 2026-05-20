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
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script defer src="/js/dchub-brand.js"></script>
<style>
  /* Phase FF+25-followup-r14 (2026-05-20) — canonical brand on the
     backend-served intelligence dashboard. Was a white page with
     #fafbfc bg and Segoe UI; now matches refined.html dark canvas
     with indigo→violet glow, Instrument Sans, gradient KPIs. The
     LIVE data fetches below are unchanged — only the chrome. */
  :root{
    --bg:#0a0a0f; --surface:#131319;
    --border:rgba(255,255,255,.06); --border-strong:rgba(255,255,255,.1);
    --text:#f5f5f7; --text-dim:#a1a1aa; --text-faint:#71717a;
    --indigo:#6366f1; --violet:#a855f7;
    --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
    --font:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;
    --mono:'JetBrains Mono','SF Mono',monospace;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:var(--font);background:var(--bg);color:var(--text);
       max-width:1100px;margin:0 auto;padding:48px 24px 80px;
       line-height:1.55;-webkit-font-smoothing:antialiased;
       position:relative;min-height:100vh}
  body::before{content:'';position:fixed;top:-30%;left:50%;
    transform:translateX(-50%);width:1200px;height:1200px;z-index:0;
    pointer-events:none;
    background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
                                rgba(168,85,247,.06) 30%,transparent 60%)}
  body > *{position:relative;z-index:1}
  ::selection{background:var(--indigo);color:#fff}

  header.top{display:flex;align-items:center;justify-content:space-between;
    margin-bottom:32px;flex-wrap:wrap;gap:12px}
  header.top a.brand{display:inline-flex;align-items:center;gap:10px;
    text-decoration:none;color:var(--text)}

  h1{font-size:clamp(1.8rem,3.4vw,2.4rem);font-weight:700;letter-spacing:-.025em;
    line-height:1.05;margin:0 0 10px;display:flex;align-items:center;gap:12px}
  h1 .pulse{display:inline-block;width:10px;height:10px;border-radius:50%;
    background:var(--violet);box-shadow:0 0 12px var(--violet);
    animation:pulse 1.8s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  h1+p{color:var(--text-dim);margin:0 0 28px;font-size:1rem;
    max-width:720px;line-height:1.55}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
    gap:12px;margin:16px 0}
  .card{background:var(--surface);border:1px solid var(--border);
    padding:22px;border-radius:14px;transition:border-color .2s,transform .2s ease}
  .card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
  .card h3{margin:0 0 10px;font-family:var(--mono);font-size:10px;
    text-transform:uppercase;letter-spacing:.12em;color:var(--text-faint);
    font-weight:600}
  .metric{font-size:1.9rem;font-weight:700;letter-spacing:-.02em;
    background:var(--grad);-webkit-background-clip:text;background-clip:text;
    color:transparent;line-height:1.1;display:block;font-family:var(--mono)}
  .metric-sub{color:var(--text-dim);font-size:.85rem;margin-top:6px}

  .lead{background:var(--grad-soft);border:1px solid rgba(168,85,247,.22);
    color:var(--text);padding:28px;border-radius:14px;margin:20px 0;
    position:relative;overflow:hidden}
  .lead::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
    background:var(--grad)}
  .lead h2{font-size:1.25rem;font-weight:700;letter-spacing:-.015em;
    margin:0 0 8px;color:var(--text)}
  .lead p{margin:0;font-size:.98rem;color:var(--text-dim);line-height:1.55}

  .footnote{color:var(--text-faint);font-family:var(--mono);font-size:.7rem;
    text-align:center;margin-top:48px;letter-spacing:.04em;text-transform:uppercase}
  .data-loading{color:var(--text-faint);font-style:italic;font-size:.95rem}
  a{color:#c7d2fe;text-decoration:none;transition:color .15s}
  a:hover{color:#fff}
</style>
</head>
<body>
<header class="top">
  <a href="/" class="brand" data-dchub-brand></a>
  <span style="font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint)">Auto-refresh · 60s</span>
</header>
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
