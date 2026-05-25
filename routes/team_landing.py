"""
team_landing.py — public /team page.

Phase ZZZZZ-round47.7 (2026-05-25). Founder-only placeholder until
real bios are filled in. Nav and SEO referenced /team but it 404'd.
"""
import datetime
from flask import Blueprint

team_bp = Blueprint("team_landing", __name__)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Team — DC Hub</title>
<meta name="description" content="The people building DC Hub — the leading real-time data center intelligence platform for AI agents.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/team">
<meta property="og:title" content="Team — DC Hub">
<meta property="og:description" content="Meet the team building the leading real-time data center intelligence platform.">
<style>
 body{max-width:880px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.6;color:#0f172a}
 h1{font-size:2.2rem;margin:.3em 0;letter-spacing:-.02em}
 h2{font-size:1.3rem;margin:1.6em 0 .5em;color:#1e293b}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}
 .lead{color:#475569;font-size:1.05rem;max-width:780px}
 .person{display:flex;gap:20px;margin:24px 0;align-items:flex-start;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:22px}
 .avatar{flex:0 0 80px;height:80px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;display:flex;align-items:center;justify-content:center;font-size:1.8rem;font-weight:700;letter-spacing:-.02em}
 .person-meta{flex:1}
 .person-name{font-weight:700;font-size:1.15rem;margin:0 0 4px;color:#0f172a}
 .person-role{color:#6366f1;font-size:.9rem;font-weight:600;margin-bottom:8px}
 .person-bio{color:#475569;font-size:.95rem;line-height:1.55}
 .person-links{margin-top:10px;font-size:.85rem}
 .person-links a{color:#6366f1;text-decoration:none;margin-right:14px}
 .pane{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:22px 28px;border-radius:10px;margin:30px 0;text-align:center}
 .pane h2{color:#fff;margin:0 0 8px;font-size:1.2rem}
 .pane .cta{display:inline-block;background:#fff;color:#6366f1;padding:10px 22px;border-radius:6px;text-decoration:none;font-weight:600;margin-top:10px}
 .footer{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}
 .footer a{color:#6366f1;text-decoration:none}
</style></head><body>
<div class="eyebrow">DC Hub · Team</div>
<h1>The team behind DC Hub</h1>
<p class="lead">A small, focused crew building the real-time intelligence layer between data-center
infrastructure and the AI agents that need to act on it.</p>

<h2>Founders</h2>

<div class="person">
  <div class="avatar">JM</div>
  <div class="person-meta">
    <div class="person-name">Jonathan Martone</div>
    <div class="person-role">Founder &amp; CEO</div>
    <div class="person-bio">
      Started DC Hub after a decade in commercial real estate and infrastructure brokerage —
      saw firsthand that every site-selection decision was happening on PDFs from 2022 while
      hyperscaler capex moved monthly. Built DC Hub to fix that gap: live ISO data, daily DCPI
      scoring, and an MCP server that puts the full intelligence catalog into any AI agent's
      reach.
    </div>
    <div class="person-links">
      <a href="https://www.linkedin.com/in/jonathanmartone/" target="_blank" rel="noopener">LinkedIn</a>
      <a href="mailto:jm@dchub.cloud">jm@dchub.cloud</a>
    </div>
  </div>
</div>

<h2>Powered by Claude</h2>

<div class="person">
  <div class="avatar" style="background:linear-gradient(135deg,#0f172a,#334155)">🧠</div>
  <div class="person-meta">
    <div class="person-name">Brain v2 (Claude Opus 4.7)</div>
    <div class="person-role">Autonomous platform engineer</div>
    <div class="person-bio">
      Brain v2 audits every public surface every 5 minutes, detects regressions, proposes fixes,
      and ships scoped code changes within strict safety constraints. 80+ learning cycles
      logged. The platform velocity you see in the
      <a href="/changelog">changelog</a> — 7 press releases in 7 days, 6 landing pages shipped
      in 30 minutes — is mostly Brain v2 working alongside Jonathan.
    </div>
    <div class="person-links">
      <a href="/architecture">How it works →</a>
      <a href="/transparency">Live ops →</a>
    </div>
  </div>
</div>

<div class="pane">
  <h2>Want to work on this?</h2>
  <p style="margin:.5em 0;font-size:.95rem">DC Hub is growing — we'll be hiring engineers,
  GTM, and infra researchers in 2026. Drop your details and we'll reach out when roles open.</p>
  <a class="cta" href="mailto:careers@dchub.cloud?subject=Interested%20in%20DC%20Hub">careers@dchub.cloud</a>
</div>

<p class="footer">
<a href="/">Home</a> · <a href="/architecture">Architecture</a> · <a href="/dcpi">DCPI</a>
· <a href="/case-studies">Case studies</a> · <a href="/changelog">Changelog</a>
· Updated __DATE__
</p>
</body></html>"""


@team_bp.route("/team", methods=["GET"], strict_slashes=False)
def team():
    html = _TEMPLATE.replace("__DATE__", datetime.datetime.utcnow().strftime("%B %Y"))
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=900, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.7-team",
    }
