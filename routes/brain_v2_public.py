"""Phase 300 (Phase R-4) — public /brain page.

Surfaces what the Brain v2 self-learning loop has done recently:
  • Layer + activation state
  • Proposed fixes (pending vs approved)
  • Recent learning attempts + outcomes
  • Plain-English explanation of how the loop works

This is the "show your work" surface — turns the autonomy into a visible
asset. Press / AI evaluators / future hires read this and see a system
that monitors itself.

Read-only. No auth. Cached 60s. Pulls from in-memory state in
routes/brain_v2_layer4.py so this page is always consistent with the
machine-readable /api/v1/brain/* endpoints.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from html import escape as _h
from flask import Blueprint, Response

brain_v2_public_bp = Blueprint("brain_v2_public", __name__)


def _get_state():
    """Pull current Brain v2 state from the layer-4 module. Defensive — if
    the module isn't loaded for any reason, returns sensible defaults."""
    try:
        from routes.brain_v2_layer4 import (
            _proposed_fixes, _learning_log,
            ANTHROPIC_API_KEY, BRAIN_MODEL,
        )
        return {
            "loaded": True,
            "active": bool(ANTHROPIC_API_KEY),
            "model": BRAIN_MODEL,
            "proposed": list(_proposed_fixes),
            "log": list(_learning_log),
        }
    except Exception as e:
        return {"loaded": False, "active": False, "model": "?",
                "proposed": [], "log": [], "error": str(e)[:200]}


def _color_for(item: dict) -> str:
    if item.get("approved"): return "var(--green)"
    if item.get("approval_count", 0) >= 1: return "var(--amber)"
    return "var(--tx2)"


_BRAIN_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub · Brain v2 — self-learning autonomy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="What the DC Hub self-learning system has detected, proposed, and learned in the last 24 hours. Live transparency on autonomous bug-fixing.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/brain">
<meta property="og:title" content="DC Hub · Brain v2 — self-learning autonomy">
<meta property="og:description" content="Live transparency on how the DC Hub system monitors itself, learns new bug patterns, and proposes fixes via Anthropic's Claude API.">
<meta property="og:url" content="https://dchub.cloud/brain">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "TechArticle",
  "headline": "DC Hub Brain v2 — autonomous self-learning bug-fix loop",
  "description": "Live status page for the DC Hub Brain v2 system. Reads its own healer findings, asks Claude API for fixes on novel patterns, validates suggestions, and applies them via a 2-cycle approval gate. Public transparency dashboard.",
  "url": "https://dchub.cloud/brain",
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "isAccessibleForFree": true
}
</script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--bg2:#0f1119;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--tx3:#6b7280;--green:#10b981;--amber:#f59e0b;--red:#ef4444;--acc:#6366f1;--acc-light:#818cf8;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}
*{box-sizing:border-box}
body{font-family:Inter,system-ui;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem;}
.eyebrow{font-family:'JetBrains Mono',monospace;font-size:0.74rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.14em;margin-bottom:0.6rem;}
h1{font-size:clamp(2.4rem,5vw,3.2rem);margin:0 0 0.7rem;font-weight:800;letter-spacing:-0.025em;line-height:1.05;}
h1 .grad{background:var(--gradient);-webkit-background-clip:text;background-clip:text;color:transparent;}
.lede{color:var(--tx2);font-size:1.1rem;max-width:760px;margin:0 0 2.5rem;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin:2rem 0;}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.2rem 1.3rem;}
.kpi .v{font-family:'JetBrains Mono',monospace;font-size:2rem;font-weight:800;line-height:1;}
.kpi .v.green{color:var(--green);}.kpi .v.red{color:var(--red);}.kpi .v.amber{color:var(--amber);}
.kpi .l{color:var(--tx2);font-size:0.75rem;margin-top:0.55rem;text-transform:uppercase;letter-spacing:0.08em;}
.section{margin:2.5rem 0;}
.section-title{font-size:1.15rem;font-weight:700;margin:0 0 1rem;letter-spacing:-0.01em;}
.card-list{display:grid;gap:0.8rem;}
.proposal{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.1rem 1.3rem;}
.proposal .head{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:0.6rem;}
.proposal .label{font-family:'JetBrains Mono',monospace;font-size:0.74rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.08em;}
.proposal .badge{display:inline-block;padding:0.2rem 0.6rem;border-radius:99px;font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;}
.proposal .badge.approved{background:rgba(16,185,129,0.12);color:var(--green);border:1px solid rgba(16,185,129,0.25);}
.proposal .badge.pending{background:rgba(245,158,11,0.12);color:var(--amber);border:1px solid rgba(245,158,11,0.25);}
.proposal .find,.proposal .replace{font-family:'JetBrains Mono',monospace;font-size:0.82rem;padding:0.55rem 0.8rem;border-radius:6px;margin:0.35rem 0;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);overflow-x:auto;}
.proposal .find{border-left:3px solid var(--red);}
.proposal .replace{border-left:3px solid var(--green);}
.proposal .meta{color:var(--tx3);font-size:0.78rem;margin-top:0.6rem;}
.log-row{display:flex;justify-content:space-between;gap:1rem;padding:0.55rem 0.8rem;border-bottom:1px solid var(--bd);font-size:0.84rem;}
.log-row:last-child{border-bottom:none;}
.log-time{font-family:'JetBrains Mono',monospace;color:var(--tx3);font-size:0.76rem;flex:0 0 auto;}
.log-issue{color:var(--tx2);flex:1;min-width:0;}
.log-outcome{font-family:'JetBrains Mono',monospace;font-size:0.78rem;}
.log-outcome.ok{color:var(--green);}.log-outcome.warn{color:var(--amber);}.log-outcome.err{color:var(--red);}
.explainer{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:1.4rem 1.5rem;color:var(--tx2);font-size:0.95rem;}
.explainer p{margin:0.7rem 0;}
.explainer code{background:var(--bg);padding:2px 6px;border-radius:4px;color:var(--tx);font-family:'JetBrains Mono',monospace;font-size:0.88rem;}
.explainer a{color:var(--acc-light);text-decoration:none;border-bottom:1px dotted rgba(129,140,248,0.5);}
.foot{margin-top:3rem;color:var(--tx3);font-size:0.8rem;text-align:center;}
.foot a{color:var(--tx2);}
.empty{padding:1.4rem;text-align:center;color:var(--tx3);font-size:0.92rem;background:var(--card);border:1px dashed var(--bd);border-radius:10px;}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">DC Hub · Brain v2 · live</div>
  <h1>The system is <span class="grad">learning to fix itself</span>.</h1>
  <p class="lede">When the DC Hub healer detects a frontend bug it doesn't already know how to fix, it asks Anthropic's Claude API for a safe substitution, validates the suggestion against 5 safety rules, and queues it for human-or-automation review. This page shows every proposal in real time.</p>

  <div class="kpis">
    <div class="kpi"><div class="v {{status_class}}">{{status_text}}</div><div class="l">Layer 4 status</div></div>
    <div class="kpi"><div class="v">{{total_proposals}}</div><div class="l">Proposed fixes</div></div>
    <div class="kpi"><div class="v green">{{approved_count}}</div><div class="l">Approved (≥2 cycles)</div></div>
    <div class="kpi"><div class="v amber">{{pending_count}}</div><div class="l">Pending review</div></div>
    <div class="kpi"><div class="v">{{log_count}}</div><div class="l">Learning attempts (24h)</div></div>
  </div>

  <div class="section">
    <h2 class="section-title">Recent proposals</h2>
    <div class="card-list">
{{proposals_html}}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">Learning log</h2>
    <div style="background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;">
{{log_html}}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">How the loop works</h2>
    <div class="explainer">
      <p><strong>Every hour</strong>, a GitHub Actions cron triggers <code>POST /api/v1/brain/learn</code>.</p>
      <p>The endpoint pulls current findings from <code>/api/v1/heal/findings</code> — the healer's view of every detected stale string, broken link, or placeholder leak across the public site.</p>
      <p>For any issue NOT in the master-heal workflow's hand-curated FIX_MAP, the loop calls Claude with the issue label, the page URL, the count, and a tight 1.5KB HTML snippet. The system prompt forbids modifying typography in meta descriptions or prose — only data-cell placeholders are eligible.</p>
      <p>Claude returns a (find, replace, rationale) JSON object. Five validators reject the suggestion if: find is shorter than 5 chars, replace introduces HTML/JS, the substitution is a no-op, replace is too long, or rationale is missing. Surviving proposals get an approval counter.</p>
      <p><strong>A proposal is only auto-applied after it crosses a 2-cycle approval gate</strong> — i.e., the exact same fix is proposed twice on independent hourly runs. Single-shot Claude hallucinations stay in the queue but never make it to the live site.</p>
      <p>Machine-readable: <a href="/api/v1/brain/status">/api/v1/brain/status</a>, <a href="/api/v1/brain/proposed-fixes">/api/v1/brain/proposed-fixes</a>, <a href="/api/v1/brain/proposed-fixes?approved=true">approved subset</a>. The master-heal cron consumes the approved subset and merges entries into its runtime FIX_MAP.</p>
    </div>
  </div>

  <p class="foot">
    As of {{as_of}}.
    <a href="/freshness">Data freshness</a> · <a href="/dcpi">DCPI</a> · <a href="/api/v1/heal/findings">Heal findings</a> · <a href="/audit/">Audit</a>
  </p>
</div>
</body>
</html>"""


@brain_v2_public_bp.route("/brain", methods=["GET"])
def brain_page():
    state = _get_state()
    proposals = state["proposed"]
    log = state["log"][-30:]  # last 30 entries

    approved_count = sum(1 for p in proposals if p.get("approved"))
    pending_count = sum(1 for p in proposals if not p.get("approved"))
    status_active = state.get("active")
    status_text = "ACTIVE" if status_active else ("DORMANT" if state.get("loaded") else "OFFLINE")
    status_class = "green" if status_active else ("amber" if state.get("loaded") else "red")

    # Render proposals (newest first)
    if proposals:
        prop_blocks = []
        for p in reversed(proposals[-12:]):
            badge_class = "approved" if p.get("approved") else "pending"
            badge_text = (f"approved · {p.get('approval_count',1)} cycles"
                          if p.get("approved")
                          else f"pending · {p.get('approval_count',1)}/2 cycles")
            find = _h((p.get("find") or "")[:300])
            replace = _h((p.get("replace") or "")[:300])
            rationale = _h((p.get("rationale") or "")[:300])
            label = _h(p.get("issue_label") or "?")
            url = _h(p.get("source_url") or "")
            proposed_at = _h((p.get("proposed_at") or "")[:19])
            prop_blocks.append(
                f'<div class="proposal">'
                f'<div class="head">'
                f'<span class="label">{label}</span>'
                f'<span class="badge {badge_class}">{badge_text}</span>'
                f'</div>'
                f'<div class="find">- {find}</div>'
                f'<div class="replace">+ {replace}</div>'
                f'<div class="meta">{rationale} · <code>{url}</code> · proposed {proposed_at}</div>'
                f'</div>'
            )
        proposals_html = "\n".join(prop_blocks)
    else:
        proposals_html = '<div class="empty">No proposals yet. Brain v2 fires hourly — first run lands within the hour.</div>'

    # Render log
    if log:
        log_rows = []
        for entry in reversed(log):
            outcome = (entry.get("outcome") or "")[:60]
            cls = ("ok" if outcome.startswith("proposed") or outcome == "approval_count_incremented"
                   else "err" if "fail" in outcome or "error" in outcome or "refused" in outcome
                   else "warn")
            log_rows.append(
                f'<div class="log-row">'
                f'<span class="log-time">{_h((entry.get("t") or "")[:19])}</span>'
                f'<span class="log-issue">{_h((entry.get("issue") or "?")[:60])}</span>'
                f'<span class="log-outcome {cls}">{_h(outcome)}</span>'
                f'</div>'
            )
        log_html = "\n".join(log_rows)
    else:
        log_html = '<div class="empty">No learning attempts logged yet.</div>'

    html = (_BRAIN_PAGE_TEMPLATE
            .replace("{{status_text}}", _h(status_text))
            .replace("{{status_class}}", _h(status_class))
            .replace("{{total_proposals}}", str(len(proposals)))
            .replace("{{approved_count}}", str(approved_count))
            .replace("{{pending_count}}", str(pending_count))
            .replace("{{log_count}}", str(len(state["log"])))
            .replace("{{proposals_html}}", proposals_html)
            .replace("{{log_html}}", log_html)
            .replace("{{as_of}}", datetime.now(timezone.utc).isoformat()))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    return resp
