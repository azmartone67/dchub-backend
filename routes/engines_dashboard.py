"""
engines_dashboard.py — visual dashboard for the autonomous optimization engines.

Renders the MCP Leadership Engine (/api/v1/mcp/leadership) + the AI Agent
Utilization Engine (/api/v1/agents/utilization) as one ops dashboard: index
gauges, status-colored dimension/track bars, the lifecycle funnel, and the
SHADOW optimize-loop (the actuators each engine WOULD fire). Self-contained
HTML; fetches the engine JSON client-side (same origin).

Served at /admin/engines (the CF zone worker passes /admin/* to the backend;
also reachable at the backend origin). Read-only view of read-only engines.
"""
from flask import Blueprint, Response

engines_dashboard_bp = Blueprint("engines_dashboard", __name__)

_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub · Optimization Engines</title>
<style>
:root{--bg:#0a0e1a;--panel:#121629;--ink:#E8ECF7;--dim:#8B92A8;--bd:#1F2540;--violet:#7c5cff;--accent:#4F8FFF}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:24px;font-weight:600;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.banner{background:linear-gradient(90deg,rgba(124,92,255,.14),rgba(79,143,255,.05));border:1px solid var(--bd);border-left:3px solid var(--violet);border-radius:10px;padding:14px 18px;margin-bottom:24px;font-size:14px}
.banner b{color:#fff}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--bd);border-radius:14px;padding:22px}
.phead{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}
.ptitle{font-size:15px;font-weight:600}
.ptag{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);border:1px solid var(--bd);border-radius:20px;padding:3px 9px}
.score{font-size:52px;font-weight:700;line-height:1;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.score small{font-size:18px;color:var(--dim);font-weight:500}
.verdict{font-size:12px;text-transform:uppercase;letter-spacing:.08em;margin:2px 0 18px}
.row{display:grid;grid-template-columns:120px 1fr 42px;align-items:center;gap:10px;margin-bottom:9px;font-size:12px}
.rname{color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar{height:7px;border-radius:4px;background:rgba(255,255,255,.06);overflow:hidden;position:relative}
.fill{position:absolute;inset:0 auto 0 0;border-radius:4px;transition:width .6s cubic-bezier(.2,.8,.2,1)}
.rval{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
.callout{margin:16px 0 6px;padding:11px 13px;border-radius:9px;background:rgba(255,90,90,.08);border:1px solid rgba(255,90,90,.22);font-size:12.5px}
.callout b{color:#ffb4b4}
.sec{margin-top:18px;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);border-top:1px solid var(--bd);padding-top:14px}
.act{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--dim)}
.act .arm{font-size:9.5px;font-weight:700;letter-spacing:.05em;padding:2px 7px;border-radius:5px;background:rgba(79,143,255,.16);color:#9cc2ff;white-space:nowrap}
.act .arm.no{background:rgba(255,255,255,.05);color:var(--dim)}
.act code{color:var(--ink);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.funnel{display:flex;gap:6px;margin:14px 0 4px;flex-wrap:wrap}
.fstep{flex:1;min-width:84px;background:rgba(255,255,255,.03);border:1px solid var(--bd);border-radius:8px;padding:8px 9px}
.fstep .fl{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.fstep .fv{font-size:12px;font-weight:600;margin-top:2px}
.foot{color:var(--dim);font-size:11.5px;margin-top:26px;text-align:center}
.err{color:#ffb4b4;font-size:12px}
</style></head><body><div class="wrap">
<h1>DC Hub · Optimization Engines</h1>
<div class="sub">self-measuring · self-optimizing · <b id="phase">shadow</b> phase — measures &amp; proposes, executes nothing until <code>fix_success_rate &ge; 50%</code></div>
<div class="banner" id="banner">Loading engines…</div>
<div class="grid">
  <div class="panel" id="lead"><div class="phead"><div class="ptitle">⚡ MCP Leadership</div><div class="ptag">/api/v1/mcp/leadership</div></div><div id="lead-body" class="err">loading…</div></div>
  <div class="panel" id="util"><div class="phead"><div class="ptitle">🤖 Agent Utilization</div><div class="ptag">/api/v1/agents/utilization</div></div><div id="util-body" class="err">loading…</div></div>
</div>
<div class="foot">SHADOW — both engines execute nothing. The auto-optimize loop arms per-actuator (⚡ = armable now) when the recovery watch clears 50%. · auto-refreshes 60s</div>
</div>
<script>
function col(s){return s>=75?'#4ade80':s>=50?'#4F8FFF':s>=25?'#fbbf24':'#f87171'}
function bar(name,score,status){return '<div class="row"><div class="rname">'+name+'</div><div class="bar"><div class="fill" style="width:'+Math.max(3,score)+'%;background:'+col(score)+'"></div></div><div class="rval">'+score+'</div></div>'}
function actLine(label,armable,fire){return '<div class="act"><span class="arm'+(armable?'':' no')+'">'+(armable?'⚡ ARMABLE':'·')+'</span><span>'+label+' → <code>'+fire+'</code></span></div>'}
function renderLead(d){
  var h='<div class="score">'+d.mcp_leadership_index+'<small>/100</small></div>';
  h+='<div class="verdict" style="color:'+col(d.mcp_leadership_index)+'">'+d.verdict+'</div>';
  var dims=(d.dimensions||[]).slice().sort(function(a,b){return a.score-b.score});
  dims.forEach(function(x){h+=bar(x.dimension,x.score,x.status)});
  if(d.top_priority){h+='<div class="callout"><b>TOP PRIORITY · '+d.top_priority.dimension+' ('+d.top_priority.score+')</b><br>'+d.top_priority.move+'</div>'}
  h+='<div class="sec">shadow optimize-loop</div>';
  (d.shadow_actions||[]).forEach(function(a){h+=actLine(a.dimension,a.armable_now,a.would_fire)});
  document.getElementById('lead-body').innerHTML=h;return d;
}
function renderUtil(d){
  var h='<div class="score">'+d.utilization_score+'<small>/100</small></div><div class="verdict" style="color:var(--dim)">onboard · incent · train</div>';
  var f=d.lifecycle_funnel||{};h+='<div class="funnel">';
  ['arrive','identify','activate','return','convert'].forEach(function(k){if(f[k])h+='<div class="fstep"><div class="fl">'+k+'</div><div class="fv">'+f[k]+'</div></div>'});
  h+='</div>';
  var tr=(d.tracks||[]).slice().sort(function(a,b){return a.score-b.score});
  tr.forEach(function(t){h+=bar(t.track,t.score,t.status)});
  if(d.biggest_leak){h+='<div class="callout"><b>BIGGEST LEAK · '+d.biggest_leak.track+' ('+d.biggest_leak.score+')</b><br>'+d.biggest_leak.move+'</div>'}
  h+='<div class="sec">shadow optimize-loop</div>';
  (d.shadow_actions||[]).forEach(function(a){h+=actLine(a.track,a.armable_now,a.would_fire)});
  document.getElementById('util-body').innerHTML=h;return d;
}
function load(){
  Promise.all([
    fetch('/api/v1/mcp/leadership?_='+Date.now(),{cache:'no-store'}).then(function(r){return r.json()}).then(renderLead).catch(function(){document.getElementById('lead-body').innerHTML='<span class=err>engine unavailable</span>';return null}),
    fetch('/api/v1/agents/utilization?_='+Date.now(),{cache:'no-store'}).then(function(r){return r.json()}).then(renderUtil).catch(function(){document.getElementById('util-body').innerHTML='<span class=err>engine unavailable</span>';return null})
  ]).then(function(v){
    var L=v[0],U=v[1],b=document.getElementById('banner');
    if(!L||!U){b.innerHTML='<b>Convergent diagnosis:</b> one engine unavailable — see panels below.';return;}
    var lp=(L.top_priority||{}).dimension||'?',ul=(U.biggest_leak||{}).track||'?',f=U.lifecycle_funnel||{};
    var same=(lp==='retention'&&ul==='incent')||(lp===ul);
    b.innerHTML='<b>Convergent diagnosis:</b> the Leadership Engine\'s top priority is <b style="color:#ffb4b4">'+lp+'</b>; the Utilization Engine\'s biggest leak is <b style="color:#ffb4b4">'+ul+'</b>'+(same?' — the same constraint, <b>the return loop</b>':' (engines diverging — investigate')+'. Live: agents activate heavily ('+(f.activate||'?')+') but don\'t return ('+(f['return']||'?')+'). Onboarding &amp; training strong.';
  });
}
load();setInterval(load,60000);
</script></body></html>"""


@engines_dashboard_bp.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@engines_dashboard_bp.route("/admin/engines", methods=["GET"])
def engines_dashboard():
    return Response(_HTML, mimetype="text/html")
