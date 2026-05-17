"""Phase IIII (2026-05-16) — public ops transparency dashboard.

User vision: "i want the industry to use us as the source !!!! ...
becoming more critical for the industry as the truth for data center
and energy."

`/transparency` is the public-facing operations console that backs up
the "AI-powered, real-time, no BS" brand promise. Every metric is live,
every detector finding is visible, every autopilot action is auditable.

Differs from existing surfaces:
  - /alive: operator-jargon dashboard (internal-feeling)
  - /intelligence: marketing-grade pulse (customer-facing snapshot)
  - /transparency (THIS): the OPS console — finding-level detail
    with timestamps so any auditor can see exactly what the brain
    detected and what it did about it

Pulls live data from:
  /api/v1/brain/heartbeat            — verdict + 24h autopilot rollup
  /api/v1/brain/consistency-radar    — current findings by type
  /api/v1/sentinel/scan              — page-health for 52 surfaces
  /api/v1/facilities/delta           — discovery-pipeline freshness (HHHH)
  /api/v1/bots/dormant               — outreach worklist (AAAA)
  /api/v1/spare-capacity/listings    — marketplace activity (CCCC)
  /api/v1/developers/funnel          — acquisition conversion (BBBB)
"""

from __future__ import annotations

import datetime
from flask import Blueprint, Response


transparency_bp = Blueprint("transparency", __name__)


@transparency_bp.route("/transparency", methods=["GET"], strict_slashes=False)
def transparency_dashboard():
    """Public ops console. Auto-refreshes every 60s. Every number is
    live; every finding links to the JSON source for fact-citation."""
    try:
        from routes.surface_brain import auto_log
        auto_log("transparency", "view", target="/transparency")
    except Exception: pass

    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub · Transparency — live ops console</title>
<meta name="description" content="Live ops transparency: every brain finding, every autopilot action, every page health check. The source-of-truth dashboard for the AI-powered data-center intelligence platform.">
<meta name="robots" content="index,follow,max-snippet:-1">
<meta http-equiv="refresh" content="60">
<link rel="canonical" href="https://dchub.cloud/transparency">
<meta property="og:title" content="DC Hub Transparency — live ops console">
<meta property="og:description" content="Every brain finding, autopilot action, and page-health check — visible to anyone. No BS, no PDFs.">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type":    "WebApplication",
  "name":     "DC Hub Transparency Console",
  "description": "Live operations dashboard exposing brain findings, autopilot actions, Site Sentinel page health, facility-discovery delta, dormant-agent outreach worklist, marketplace activity, and developer-acquisition funnel for the DC Hub platform.",
  "url":      "https://dchub.cloud/transparency",
  "applicationCategory": "BusinessApplication",
  "operatingSystem":     "Web",
  "offers":   {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
  "creator":  {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}
}
</script>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       max-width:1300px;margin:0 auto;padding:1.5rem 1rem;color:#e5e7eb;line-height:1.55;
       background:#0a0a14}
 h1{font-size:1.9rem;margin:0 0 .25rem;display:flex;align-items:center;gap:.6rem;color:white}
 h1 .pulse{display:inline-block;width:12px;height:12px;border-radius:50%;
            background:#10b981;animation:pulse 1.5s ease-in-out infinite}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
 h1+p{color:#9ca3af;margin:0 0 1.5rem;font-size:.95rem}
 h2{font-size:.8rem;color:#9ca3af;text-transform:uppercase;letter-spacing:.1em;
    margin:1.5rem 0 .5rem;font-weight:600}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.75rem;margin:.5rem 0}
 .card{background:#11121a;border:1px solid #1f2030;border-radius:8px;
        padding:.9rem 1rem;transition:border-color .2s}
 .card:hover{border-color:#6366f1}
 .card-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:#9ca3af;font-weight:600;margin-bottom:.4rem}
 .card-metric{font-size:1.7rem;font-weight:800;color:white;line-height:1;font-family:"SF Mono",monospace}
 .card-metric.green{color:#10b981}
 .card-metric.amber{color:#f59e0b}
 .card-metric.red{color:#ef4444}
 .card-sub{color:#6b7280;font-size:.78rem;margin-top:.4rem}
 .findings{background:#11121a;border:1px solid #1f2030;border-radius:8px;padding:1rem 1.25rem;margin:1rem 0;max-height:400px;overflow-y:auto}
 .finding{display:grid;grid-template-columns:140px 1fr 60px;gap:.75rem;padding:.5rem 0;border-bottom:1px solid #1f2030;font-size:.85rem;align-items:center}
 .finding:last-child{border-bottom:0}
 .finding-issue{color:#a5b4fc;font-family:"SF Mono",monospace;font-size:.78rem;word-break:break-all}
 .finding-detail{color:#cdd6e8;font-size:.82rem;line-height:1.45}
 .finding-count{color:#9ca3af;text-align:right;font-family:"SF Mono",monospace;font-size:.85rem}
 .loading{color:#6b7280;text-align:center;padding:2rem;font-style:italic}
 .footer{color:#6b7280;font-size:.8rem;text-align:center;margin-top:2.5rem}
 a{color:#a5b4fc;text-decoration:none}
 a:hover{color:#c4b5fd;text-decoration:underline}
 .hero{background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);padding:1.5rem 1.75rem;border-radius:12px;margin-bottom:1.5rem}
 .hero h2{color:white;margin:0 0 .4rem;font-size:1.1rem;letter-spacing:0;text-transform:none}
 .hero p{margin:0;color:#cbd5e1;font-size:.92rem}
</style>
</head>
<body>
<h1><span class="pulse"></span>DC Hub · Transparency</h1>
<p>Live ops console · refreshes every 60s · every brain finding, autopilot action, page health check, and discovery delta — visible to anyone.</p>

<div class="hero">
  <h2>Why this page exists</h2>
  <p>The brand promise is "AI-powered, real-time, actionable, no BS." This is the receipt. Every detector finding has a JSON source; every autopilot action has a timestamp; every page-health check has a status code. If a number on this page disagrees with reality, the brain is wrong and we'll fix it.</p>
</div>

<h2>Brain verdict</h2>
<div class="grid">
  <div class="card">
    <div class="card-label">Verdict</div>
    <div class="card-metric" id="verdict">—</div>
    <div class="card-sub" id="verdict-sub">checking…</div>
  </div>
  <div class="card">
    <div class="card-label">Active findings</div>
    <div class="card-metric" id="findings">—</div>
    <div class="card-sub" id="findings-types">— distinct types</div>
  </div>
  <div class="card">
    <div class="card-label">Autopilot 24h</div>
    <div class="card-metric green" id="actioned">—</div>
    <div class="card-sub"><span id="escalated">—</span> escalations</div>
  </div>
  <div class="card">
    <div class="card-label">Pattern library</div>
    <div class="card-metric" id="patterns">—</div>
    <div class="card-sub">remediation patterns wired</div>
  </div>
</div>

<h2>Site Sentinel · page health</h2>
<div class="grid">
  <div class="card">
    <div class="card-label">Pages healthy</div>
    <div class="card-metric green" id="sent-healthy">—</div>
    <div class="card-sub">of <span id="sent-total">—</span> monitored</div>
  </div>
  <div class="card">
    <div class="card-label">Pages unhealthy</div>
    <div class="card-metric" id="sent-unhealthy">—</div>
    <div class="card-sub">flagged by Sentinel</div>
  </div>
  <div class="card">
    <div class="card-label">Manifest size</div>
    <div class="card-metric" id="sent-manifest">—</div>
    <div class="card-sub">URLs polled every 15 min</div>
  </div>
</div>

<h2>Discovery pipeline · HHHH</h2>
<div class="grid">
  <div class="card">
    <div class="card-label">Facilities tracked</div>
    <div class="card-metric" id="fac-total">—</div>
    <div class="card-sub"><span id="fac-delta-7d">—</span> in last 7d</div>
  </div>
  <div class="card">
    <div class="card-label">Operating</div>
    <div class="card-metric green" id="fac-operating">—</div>
    <div class="card-sub">live data centers</div>
  </div>
  <div class="card">
    <div class="card-label">Pipeline</div>
    <div class="card-metric amber" id="fac-pipeline">—</div>
    <div class="card-sub">under construction / planned</div>
  </div>
  <div class="card">
    <div class="card-label">Snapshots</div>
    <div class="card-metric" id="fac-snaps">—</div>
    <div class="card-sub">days of baseline</div>
  </div>
</div>

<h2>Outreach worklist</h2>
<div class="grid">
  <div class="card">
    <div class="card-label">Dormant MCP agents</div>
    <div class="card-metric amber" id="dormant-count">—</div>
    <div class="card-sub"><span id="dormant-high">—</span> high-priority winback</div>
  </div>
  <div class="card">
    <div class="card-label">Spare-capacity listings</div>
    <div class="card-metric" id="spare-count">—</div>
    <div class="card-sub">live MW available</div>
  </div>
  <div class="card">
    <div class="card-label">/developers funnel</div>
    <div class="card-metric" id="dev-visitors">—</div>
    <div class="card-sub"><span id="dev-claimed">—</span> keys claimed 30d</div>
  </div>
</div>

<h2>Findings trend · 30-day sparkline (Phase MMMM)</h2>
<div class="card" style="margin:.5rem 0">
  <div class="card-label">Brain findings per day</div>
  <svg id="spark" viewBox="0 0 600 100" style="width:100%;height:100px;margin-top:.5rem">
    <polyline id="spark-line" fill="none" stroke="#10b981" stroke-width="2" points=""/>
    <g id="spark-dots"></g>
  </svg>
  <div class="card-sub" id="spark-info">loading…</div>
</div>

<h2>Page-health grid · Site Sentinel (Phase MMMM)</h2>
<div class="findings" id="sentinel-grid" style="max-height:300px">
  <div class="loading">loading 52-page grid…</div>
</div>

<h2>Active findings (top 12 by count)</h2>
<div class="findings" id="findings-list">
  <div class="loading">Loading findings…</div>
</div>

<p class="footer">
  Raw JSON sources:
  <a href="/api/v1/brain/heartbeat">heartbeat</a> ·
  <a href="/api/v1/brain/consistency-radar">radar</a> ·
  <a href="/api/v1/sentinel/scan">sentinel</a> ·
  <a href="/api/v1/facilities/delta">delta</a> ·
  <a href="/api/v1/bots/dormant">dormant</a> ·
  <a href="/api/v1/spare-capacity/listings">spare-cap</a> ·
  <a href="/api/v1/developers/funnel">dev-funnel</a>
  <br>Operator view: <a href="/alive">/alive</a> · Customer view: <a href="/intelligence">/intelligence</a>
</p>

<script src="/js/dchub-nav.js" defer></script>
<script>
(function() {
  function $(id) { return document.getElementById(id); }
  function setText(id, v) { var el = $(id); if (el && v !== undefined && v !== null) el.textContent = v; }
  function fmtNum(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
  function fmtDelta(n) { if (n == null) return '—'; return (n > 0 ? '+' : '') + Number(n).toLocaleString(); }

  // Brain heartbeat
  fetch('/api/v1/brain/heartbeat').then(r => r.json()).then(d => {
    setText('verdict', (d.verdict || '?').toUpperCase());
    setText('verdict-sub', (d.verdict_detail || '').slice(0,100) || ('cache age ' + (d._cache_age_seconds||0).toFixed(0) + 's'));
    setText('findings', (d.detector || {}).findings_count || 0);
    var types = Object.keys((d.detector || {}).by_issue || {}).length;
    setText('findings-types', types + ' distinct types');
    var ap = d.autopilot || {};
    setText('actioned', ap.actioned_24h || 0);
    setText('escalated', ap.escalated_24h || 0);
    setText('patterns', ap.pattern_library_size || 0);
  }).catch(function(){});

  // Sentinel
  fetch('/api/v1/sentinel/scan').then(r => r.json()).then(d => {
    setText('sent-healthy', d.healthy || 0);
    setText('sent-unhealthy', d.unhealthy || 0);
    setText('sent-total', d.total || 0);
    setText('sent-manifest', d.manifest_size || 0);
    var um = $('sent-unhealthy');
    if (um && (d.unhealthy || 0) > 0) um.classList.add('amber');
    else if (um) um.classList.add('green');
  }).catch(function(){});

  // Facilities delta
  fetch('/api/v1/facilities/delta').then(r => r.json()).then(d => {
    var cur = d.current || {};
    setText('fac-total', fmtNum(cur.total));
    setText('fac-operating', fmtNum(cur.operating));
    setText('fac-pipeline', fmtNum(cur.pipeline));
    setText('fac-snaps', d.snapshots_available || 0);
    var delta7 = (d.deltas || {})['7d'];
    setText('fac-delta-7d', delta7 ? fmtDelta(delta7.total) : 'no baseline yet');
  }).catch(function(){});

  // Dormant MCP
  fetch('/api/v1/bots/dormant').then(r => r.json()).then(d => {
    setText('dormant-count', d.count || 0);
    var high = (d.dormant || []).filter(function(a){
      return a.suggested_action === 'high_priority_winback';
    }).length;
    setText('dormant-high', high);
  }).catch(function(){});

  // Spare capacity
  fetch('/api/v1/spare-capacity/listings').then(r => r.json()).then(d => {
    setText('spare-count', d.total || 0);
  }).catch(function(){});

  // Developers funnel
  fetch('/api/v1/developers/funnel?days=30').then(r => r.json()).then(d => {
    var s = d.stages || {};
    setText('dev-visitors', fmtNum(s['0_unique_visitors']));
    setText('dev-claimed', fmtNum(s['2_keys_claimed']));
  }).catch(function(){});

  // Phase MMMM — findings sparkline (last 30 days)
  fetch('/api/v1/radar/history?days=30').then(r => r.json()).then(d => {
    var rows = d.history || [];
    var info = $('spark-info');
    var line = $('spark-line');
    var dots = $('spark-dots');
    if (!rows.length) {
      if (info) info.textContent = 'no snapshots yet — daily cron starts populating after first run';
      return;
    }
    var counts = rows.map(function(r){ return r.finding_count; });
    var min = Math.min.apply(null, counts);
    var max = Math.max.apply(null, counts);
    var range = (max - min) || 1;
    var step = 600 / Math.max(1, rows.length - 1);
    var points = counts.map(function(c, i){
      var x = i * step;
      var y = 90 - ((c - min) / range) * 80;
      return x.toFixed(0) + ',' + y.toFixed(0);
    }).join(' ');
    if (line) line.setAttribute('points', points);
    // Dots
    if (dots) {
      var html = '';
      counts.forEach(function(c, i){
        var x = i * step;
        var y = 90 - ((c - min) / range) * 80;
        html += '<circle cx="'+x.toFixed(0)+'" cy="'+y.toFixed(0)+'" r="3" fill="#10b981"><title>'+rows[i].date+': '+c+' findings</title></circle>';
      });
      dots.innerHTML = html;
    }
    if (info) {
      var first = counts[0], last = counts[counts.length - 1];
      var trend = (last < first) ? '↓ improving' : (last > first) ? '↑ noisier' : '→ flat';
      info.textContent = rows.length + ' days · min ' + min + ' · max ' + max + ' · current ' + last + ' · ' + trend;
    }
  }).catch(function(){});

  // Phase MMMM — sentinel page-health grid
  fetch('/api/v1/sentinel/scan').then(r => r.json()).then(d => {
    var rows = d.results || [];
    rows.sort(function(a, b){
      // Unhealthy first, then by category critical>high>normal
      if (a.healthy !== b.healthy) return a.healthy ? 1 : -1;
      var rank = {critical: 0, high: 1, normal: 2};
      return (rank[a.category] || 9) - (rank[b.category] || 9);
    });
    var html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.4rem">';
    rows.forEach(function(r){
      var color = r.healthy ? '#10b981' : (r.status_code === 0 ? '#f59e0b' : '#ef4444');
      var bg = r.healthy ? '#0f1f1a' : (r.status_code === 0 ? '#1f1a0f' : '#1f0f0f');
      var label = r.label || r.path;
      var sub = r.healthy ? 'ok ' + (r.bytes||0) + 'b' : (r.reason||'?').slice(0, 24);
      html += '<a href="' + r.path + '" style="display:block;padding:.5rem .65rem;background:'+bg+';border:1px solid '+color+';border-radius:6px;text-decoration:none">' +
              '<div style="font-size:.72rem;font-weight:600;color:'+color+';white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+label+'</div>' +
              '<div style="font-size:.65rem;color:#6b7280;margin-top:.15rem">'+sub+'</div>' +
              '</a>';
    });
    html += '</div>';
    var el = $('sentinel-grid');
    if (el) el.innerHTML = html;
  }).catch(function(){});

  // Findings list (from radar)
  fetch('/api/v1/brain/consistency-radar').then(r => r.json()).then(d => {
    var byIssue = d.by_issue || {};
    var entries = Object.entries(byIssue).sort(function(a, b) { return b[1] - a[1]; }).slice(0, 12);
    var findingDetails = {};
    (d.findings || []).forEach(function(f) {
      var k = f.issue;
      if (!findingDetails[k]) findingDetails[k] = (f.detail || '').slice(0, 200);
    });
    var html = entries.map(function(e) {
      var issue = e[0], count = e[1];
      var detail = findingDetails[issue] || '';
      return '<div class="finding">' +
             '<div class="finding-issue">' + issue + '</div>' +
             '<div class="finding-detail">' + detail + '</div>' +
             '<div class="finding-count">' + count + '</div>' +
             '</div>';
    }).join('');
    var el = $('findings-list');
    if (el) el.innerHTML = html || '<div class="loading">No active findings — brain is quiet.</div>';
  }).catch(function(){});
})();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=60"})
