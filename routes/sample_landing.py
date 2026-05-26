"""
Phase r42d (2026-05-25) — /sample landing page.

Marketing surface that turns "we have an API" into "you can have one in
30 seconds." Pulls the live monthly narrative + quarterly narrative +
a fresh DCPI mover into one screen. Each block has:

  - The actual prose (no lorem ipsum)
  - A "copy curl" button (the actual one-liner that produced it)
  - A "share to LinkedIn / X" button (pre-filled)

If a journalist or partner lands here, they can be running our data
in under a minute. If a hyperscaler analyst lands here, they see the
analyst voice we use is comparable to what they pay CBRE for.

URL: /sample (HTML, public, no auth)
"""

import os
import html as _html
import datetime as _dt
import logging
from flask import Blueprint, Response, request

logger = logging.getLogger(__name__)
sample_landing_bp = Blueprint("sample_landing", __name__)


def _safe_pull_monthly():
    try:
        from routes.report_narrative import attach_narrative
        from routes.monthly_trend import _compute_report
        d = _compute_report()
        return attach_narrative(d, kind="monthly")
    except Exception as e:
        logger.warning(f"_safe_pull_monthly failed: {e}")
        return {}


def _safe_pull_quarterly():
    try:
        from routes.report_narrative import attach_narrative
        from routes.comprehensive_report import _gather
        d = _gather(quarter_window=True)
        return attach_narrative(d, kind="quarterly")
    except Exception as e:
        logger.warning(f"_safe_pull_quarterly failed: {e}")
        return {}


def _safe_pull_dcpi_top():
    """Pull the top BUILD market from DCPI to showcase the data layer."""
    try:
        import requests
        r = requests.get("http://localhost:8080/api/v1/dcpi/scores",
                         params={"verdict": "BUILD", "limit": 1},
                         timeout=4)
        if r.status_code == 200:
            data = (r.json() or {}).get("scores") or []
            if data:
                return data[0]
    except Exception as e:
        logger.warning(f"_safe_pull_dcpi_top failed: {e}")
    return {}


def _fmt_paragraphs(text: str) -> str:
    """Convert paragraph-break text to escaped <p> blocks."""
    if not text:
        return ""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n".join(f"<p>{_html.escape(p)}</p>" for p in paragraphs)


def _share_links(quote: str, permalink: str) -> str:
    """Build pre-filled share buttons. The quote should be the headline
    sentence; LinkedIn/X will populate around the permalink."""
    import urllib.parse as _u
    q = _u.quote(quote[:240])
    p = _u.quote(permalink)
    return (
        f'<a class="share" href="https://www.linkedin.com/sharing/share-offsite/?url={p}" '
        f'target="_blank" rel="noopener">in Share on LinkedIn</a>'
        f'<a class="share" href="https://twitter.com/intent/tweet?text={q}&url={p}" '
        f'target="_blank" rel="noopener">𝕏 Share on X</a>'
    )


@sample_landing_bp.route("/sample", methods=["GET"], strict_slashes=False)
def sample():
    """Render the live-sample landing page."""
    monthly = _safe_pull_monthly()
    quarterly = _safe_pull_quarterly()
    dcpi_top = _safe_pull_dcpi_top()

    m_narr = (monthly.get("narrative_summary") or {}).get("text", "")
    q_narr = (quarterly.get("narrative_summary") or {}).get("text", "")
    m_label = monthly.get("month_label") or "current month"
    q_label = (f"Q{(_dt.date.today().month - 1)//3 + 1} "
               f"{_dt.date.today().year}")

    # Pull a one-sentence headline from each narrative for share buttons
    m_lead = (m_narr.split(". ")[0][:240] + ".") if m_narr else "DC Hub monthly trend snapshot."
    q_lead = (q_narr.split(". ")[0][:240] + ".") if q_narr else "DC Hub quarterly deep-dive."

    # DCPI top-build hero stat
    dcpi_market = dcpi_top.get("market", "—")
    dcpi_score = dcpi_top.get("composite_score") or dcpi_top.get("score") or "—"
    dcpi_iso = dcpi_top.get("iso") or "—"
    dcpi_verdict = dcpi_top.get("verdict") or "BUILD"

    # Stats from monthly report
    h = monthly.get("headline") or {}
    df = monthly.get("deal_flow") or {}
    curr = df.get("current") or {}
    facilities = h.get("facilities_total", 0)
    total_gw = (h.get("total_mw") or 0) / 1000
    deals_count = curr.get("count", 0)
    deals_val = curr.get("value", 0)

    monthly_para_html = _fmt_paragraphs(m_narr)
    quarterly_para_html = _fmt_paragraphs(q_narr)
    m_share = _share_links(m_lead, "https://dchub.cloud/reports/monthly")
    q_share = _share_links(q_lead, "https://dchub.cloud/reports/quarterly-deep")

    # Generated dates
    m_gen = ((monthly.get("narrative_summary") or {}).get("generated_at") or "")[:10]
    q_gen = ((quarterly.get("narrative_summary") or {}).get("generated_at") or "")[:10]

    return Response(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>DC Hub — Live sample · {m_label}</title>
  <meta name="description" content="Live data-center market intelligence. {facilities:,} facilities tracked, {total_gw:.1f} GW capacity, {deals_count} M&A deals this month. Free + machine-readable + CC-BY-4.0. Copy a curl, share a quote, link a report.">
  <meta name="robots" content="index,follow">
  <link rel="canonical" href="https://dchub.cloud/sample">
  <meta property="og:title" content="DC Hub — Live alternative to CBRE / JLL H2 reports">
  <meta property="og:description" content="{facilities:,} facilities · {total_gw:.1f} GW · {deals_count} deals this month · CC-BY-4.0 · auto-narrative by Claude. Sample now in 30 seconds.">
  <meta property="og:type" content="website">
  <meta property="og:url" content="https://dchub.cloud/sample">
  <style>
    :root {{
      --ink: #0a0e1a;
      --paper: #f8fafc;
      --violet: #6366f1;
      --emerald: #10b981;
      --rose: #ef4444;
      --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Inter", "Helvetica Neue", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--ink); color: #e5e7eb; font-family: var(--sans); line-height: 1.55; }}
    .wrap {{ max-width: 920px; margin: 0 auto; padding: 60px 28px 100px; }}
    .eyebrow {{ font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: .12em; color: var(--violet); margin-bottom: 14px; }}
    h1 {{ font-size: 42px; line-height: 1.1; margin: 0 0 24px; letter-spacing: -.02em; }}
    h2 {{ font-size: 24px; margin: 48px 0 8px; letter-spacing: -.01em; }}
    .lede {{ font-size: 18px; color: #cbd5e1; margin-bottom: 32px; max-width: 70ch; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 40px 0 60px; }}
    .stat {{ background: rgba(255,255,255,.04); padding: 18px 16px; border-radius: 8px; border-left: 3px solid var(--violet); }}
    .stat-num {{ font-size: 28px; font-weight: 700; letter-spacing: -.02em; display: block; }}
    .stat-lbl {{ font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #94a3b8; margin-top: 6px; }}
    .narr-block {{ background: rgba(99,102,241,.06); border-left: 3px solid var(--violet); padding: 26px 30px; border-radius: 6px; margin: 16px 0 28px; }}
    .narr-block p {{ margin: 0 0 14px; font-size: 16px; line-height: 1.65; }}
    .narr-block p:last-child {{ margin-bottom: 0; }}
    .narr-meta {{ font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: .12em; color: var(--violet); margin-bottom: 14px; }}
    pre.curl {{ background: rgba(0,0,0,.4); border: 1px solid rgba(255,255,255,.08); border-radius: 6px; padding: 14px 18px; font-family: var(--mono); font-size: 13px; overflow-x: auto; color: #c7d2fe; margin: 12px 0; }}
    .copy-btn {{ background: var(--violet); color: #fff; border: 0; padding: 8px 14px; border-radius: 4px; font-family: var(--mono); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; cursor: pointer; margin-right: 8px; }}
    .copy-btn:hover {{ background: #4f46e5; }}
    .share {{ display: inline-block; background: rgba(255,255,255,.06); color: #cbd5e1; padding: 8px 14px; border-radius: 4px; font-family: var(--mono); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; text-decoration: none; margin-right: 8px; }}
    .share:hover {{ background: rgba(255,255,255,.12); color: #fff; }}
    .actions {{ margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .hero-card {{ background: linear-gradient(135deg, rgba(16,185,129,.08), rgba(99,102,241,.06)); border: 1px solid rgba(16,185,129,.2); padding: 32px 36px; border-radius: 10px; margin: 24px 0 40px; }}
    .verdict {{ display: inline-block; background: var(--emerald); color: var(--ink); padding: 4px 12px; border-radius: 4px; font-weight: 700; font-family: var(--mono); font-size: 11px; letter-spacing: .08em; }}
    .license-foot {{ background: rgba(16,185,129,.06); border-left: 3px solid var(--emerald); padding: 20px 24px; border-radius: 6px; margin-top: 60px; font-size: 14px; color: #cbd5e1; }}
    a {{ color: #93c5fd; }}
    code {{ background: rgba(255,255,255,.06); padding: 2px 6px; border-radius: 3px; font-family: var(--mono); font-size: 13px; color: #c7d2fe; }}
    .partners {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; }}
    .partner-chip {{ background: rgba(255,255,255,.04); padding: 10px 16px; border-radius: 6px; font-family: var(--mono); font-size: 12px; color: #94a3b8; border: 1px solid rgba(255,255,255,.08); }}
    @media (max-width: 700px) {{ h1 {{ font-size: 30px; }} .stats {{ grid-template-columns: 1fr 1fr; }} .wrap {{ padding: 36px 20px 80px; }} }}
  </style>
</head>
<body>
<div class="wrap">

  <div class="eyebrow">DC Hub · Live sample · CC-BY-4.0 · {m_gen or _dt.date.today().isoformat()}</div>
  <h1>The live alternative to CBRE / JLL H2 reports — sample it now.</h1>
  <p class="lede">DC Hub is the live data layer underneath the data-center industry. {facilities:,} facilities tracked, {total_gw:.1f} GW power, {deals_count} M&amp;A deals tracked this month. Every number is machine-readable, refreshed daily, and CC-BY-4.0-licensed — free for journalists, partners, and AI agents. The narratives below are auto-generated by Claude from the underlying structured data.</p>

  <div class="stats">
    <div class="stat"><span class="stat-num">{facilities:,}</span><span class="stat-lbl">Facilities</span></div>
    <div class="stat"><span class="stat-num">{total_gw:.1f} GW</span><span class="stat-lbl">Power tracked</span></div>
    <div class="stat"><span class="stat-num">{deals_count:,}</span><span class="stat-lbl">M&amp;A deals · this month</span></div>
    <div class="stat"><span class="stat-num">${deals_val:,.0f}M</span><span class="stat-lbl">Disclosed value · this month</span></div>
  </div>

  <!-- Hero DCPI card -->
  <h2>Today's #1 BUILD market</h2>
  <div class="hero-card">
    <div style="font-size:28px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px">{_html.escape(str(dcpi_market))} <span class="verdict">{_html.escape(str(dcpi_verdict))}</span></div>
    <div style="font-family:var(--mono);font-size:13px;color:#94a3b8">ISO: {_html.escape(str(dcpi_iso))} · DCPI composite: <strong style="color:#fff">{dcpi_score}/100</strong></div>
    <div class="actions" style="margin-top:18px">
      <pre class="curl" style="flex:1;margin:0">curl https://dchub.cloud/api/v1/dcpi/scores?verdict=BUILD&amp;limit=5</pre>
    </div>
    <div class="actions">
      <button class="copy-btn" onclick="navigator.clipboard.writeText('curl https://dchub.cloud/api/v1/dcpi/scores?verdict=BUILD\\u0026limit=5')">Copy curl</button>
      <a class="share" href="https://dchub.cloud/dcpi" target="_blank">Open full DCPI →</a>
    </div>
  </div>

  <!-- Monthly narrative -->
  <h2>{m_label} — auto-narrative</h2>
  <p style="color:#94a3b8;font-size:14px;margin-top:0">Generated by claude-haiku-4-5 from the live monthly report. Cached 1 hour. Drop-in CBRE-style analyst voice on live data.</p>
  <div class="narr-block">
    <div class="narr-meta">Executive summary · {m_gen}</div>
    {monthly_para_html or '<p style="color:#94a3b8"><em>Narrative will appear once ANTHROPIC_API_KEY is set on the deployment.</em></p>'}
  </div>
  <div class="actions">
    <pre class="curl" style="flex:1;margin:0">curl -s https://dchub.cloud/api/v1/reports/monthly/narrative | jq .narrative</pre>
  </div>
  <div class="actions">
    <button class="copy-btn" onclick="navigator.clipboard.writeText('curl -s https://dchub.cloud/api/v1/reports/monthly/narrative | jq .narrative')">Copy curl</button>
    <a class="share" href="https://dchub.cloud/reports/monthly.md" target="_blank">View as Markdown</a>
    <a class="share" href="https://dchub.cloud/reports/monthly" target="_blank">View full HTML →</a>
    {m_share}
  </div>

  <!-- Quarterly narrative -->
  <h2>{q_label} — quarterly deep-dive auto-narrative</h2>
  <p style="color:#94a3b8;font-size:14px;margin-top:0">350-word structural read across 90 days of capital, capacity, and verdicts. The structural-shift section is where we go beyond CBRE's H2 outlook.</p>
  <div class="narr-block">
    <div class="narr-meta">Executive summary · {q_gen}</div>
    {quarterly_para_html or '<p style="color:#94a3b8"><em>Narrative will appear once ANTHROPIC_API_KEY is set on the deployment.</em></p>'}
  </div>
  <div class="actions">
    <pre class="curl" style="flex:1;margin:0">curl -s https://dchub.cloud/api/v1/reports/quarterly-deep/narrative | jq .narrative</pre>
  </div>
  <div class="actions">
    <button class="copy-btn" onclick="navigator.clipboard.writeText('curl -s https://dchub.cloud/api/v1/reports/quarterly-deep/narrative | jq .narrative')">Copy curl</button>
    <a class="share" href="https://dchub.cloud/reports/quarterly-deep.md" target="_blank">View as Markdown</a>
    <a class="share" href="https://dchub.cloud/reports/quarterly-deep" target="_blank">View full HTML →</a>
    {q_share}
  </div>

  <!-- How to integrate -->
  <h2>For your stack</h2>
  <p>Three integration patterns, ordered easiest → most-integrated:</p>

  <h3 style="font-size:17px;margin-top:24px">1 · Quote in your article</h3>
  <pre class="curl">curl -s https://dchub.cloud/api/v1/reports/monthly/narrative \\
  | jq -r '.narrative'</pre>

  <h3 style="font-size:17px;margin-top:24px">2 · Embed in a blog or Substack</h3>
  <pre class="curl">curl https://dchub.cloud/reports/monthly.md</pre>
  <p style="color:#94a3b8;font-size:14px">Returns the full monthly report as paste-ready markdown. Drop directly into Substack, Ghost, Notion, or your CMS.</p>

  <h3 style="font-size:17px;margin-top:24px">3 · Wire it to an AI agent (MCP)</h3>
  <pre class="curl">Endpoint: https://dchub.cloud/mcp
Auth:     X-API-Key: &lt;your key from /pricing&gt;
Tools:    27 (search_facilities, get_market_dcpi_rank, compare_isos, ...)
Spec:     https://dchub.cloud/llms.txt</pre>
  <p style="color:#94a3b8;font-size:14px">Claude.ai, Claude Code, and any MCP-aware agent can pull live data directly.</p>

  <!-- License -->
  <div class="license-foot">
    <strong style="color:#10b981">CC-BY-4.0 · open license, attribution required</strong><br>
    Everything you see here can be re-used commercially with credit:<br>
    <code>DC Hub. (2026). https://dchub.cloud. Licensed CC-BY-4.0.</code><br><br>
    Used today by journalists, hyperscaler capacity-planning teams, and AI-research agents.
    <div class="partners">
      <span class="partner-chip">Substack / Ghost</span>
      <span class="partner-chip">Slack / Discord briefings</span>
      <span class="partner-chip">Claude · ChatGPT · Gemini</span>
      <span class="partner-chip">Private-equity desks</span>
      <span class="partner-chip">Hyperscaler capacity teams</span>
    </div>
  </div>

  <p style="text-align:center;color:#64748b;font-size:13px;margin-top:60px">
    DC Hub · <a href="https://dchub.cloud/">dchub.cloud</a> ·
    <a href="https://dchub.cloud/pricing">Pricing</a> ·
    <a href="https://dchub.cloud/llms.txt">/llms.txt</a> ·
    <a href="https://dchub.cloud/mcp">/mcp</a>
  </p>

</div>
</body>
</html>""",
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=600",
            "Link": '<https://creativecommons.org/licenses/by/4.0/>; rel="license"',
            "X-License": "CC-BY-4.0",
        },
    )
