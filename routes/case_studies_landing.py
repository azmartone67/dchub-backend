"""
case_studies_landing.py — public /case-studies page (placeholder).

Phase ZZZZZ-round47.8 (2026-05-25). Nav linked /case-studies but it
404'd. Until real customer stories are gathered, this page surfaces
the public proof we DO have:

  - Daily press releases (auto-generated from live data)
  - Hyperscaler $1B+ deal tracker
  - DCPI methodology
  - Aggregate platform stats

That's better than 404 and lets enterprise buyers see real outputs
while we collect cite-able customer wins.
"""
import datetime
import os
from contextlib import contextmanager
from flask import Blueprint

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

case_studies_bp = Blueprint("case_studies_landing", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _fetch_recent_press(limit=4):
    if not (_pg and _dsn()):
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT slug, title, created_at, summary
                FROM press_releases
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, created_at, NULL FROM press_releases
                    ORDER BY created_at DESC LIMIT %s""", (limit,))
                return cur.fetchall()
        except Exception:
            return []


def _fetch_hyperscaler_top(limit=3):
    if not (_pg and _dsn()):
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT headline, actor, value_display, detected_at
                FROM hyperscaler_alerts
                ORDER BY value_usd DESC NULLS LAST, detected_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception:
        return []


@case_studies_bp.route("/case-studies", methods=["GET"], strict_slashes=False)
def case_studies():
    press = _fetch_recent_press(4)
    deals = _fetch_hyperscaler_top(3)
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")

    press_rows = ""
    for slug, title, ts, summary in press:
        date_label = ts.strftime("%b %d")
        summary_html = f'<p class="cs-summary">{summary[:280]}</p>' if summary else ""
        press_rows += f"""
    <div class="case">
      <div class="case-meta"><span class="badge">Auto-published</span> {date_label}</div>
      <h3><a href="/press-release/{slug}">{title}</a></h3>
      {summary_html}
    </div>"""

    deal_rows = ""
    for headline, actor, value, ts in deals:
        date_label = ts.strftime("%b %d")
        deal_rows += f"""
    <div class="case">
      <div class="case-meta"><span class="badge orange">{value or '—'}</span> · {actor or '—'} · {date_label}</div>
      <h3>{headline}</h3>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proof &amp; Case Studies — DC Hub</title>
<meta name="description" content="DC Hub in action: live press releases auto-generated from DCPI data, hyperscaler $1B+ deal tracker, and platform metrics. Real outputs, daily cadence.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/case-studies">
<meta property="og:title" content="Proof &amp; Case Studies — DC Hub">
<meta property="og:description" content="Live press releases, deal tracker, and platform proof from DC Hub.">
<style>
 body{{max-width:960px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a}}
 h1{{font-size:2.1rem;margin:.3em 0;letter-spacing:-.02em}}
 h2{{font-size:1.25rem;margin:1.8em 0 .6em;color:#1e293b;border-bottom:1px solid #e2e8f0;padding-bottom:6px}}
 h3{{margin:.4em 0;font-size:1.05rem;color:#0f172a}}
 h3 a{{color:inherit;text-decoration:none}}
 h3 a:hover{{color:#6366f1}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 .lead{{color:#475569;font-size:1.05rem;max-width:820px}}
 .case{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:18px 22px;margin:14px 0}}
 .case-meta{{color:#64748b;font-size:.82rem;margin-bottom:6px}}
 .cs-summary{{color:#475569;font-size:.92rem;margin:.4em 0 0;line-height:1.5}}
 .badge{{display:inline-block;background:#dcfce7;color:#15803d;padding:2px 8px;border-radius:3px;font-size:.78rem;font-weight:600;margin-right:6px}}
 .badge.orange{{background:#fed7aa;color:#92400e}}
 .pane{{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:24px 28px;border-radius:10px;margin:24px 0;text-align:center}}
 .pane h2{{color:#fff;border:none;margin:0 0 8px;font-size:1.25rem}}
 .pane .cta{{display:inline-block;background:#fff;color:#6366f1;padding:10px 22px;border-radius:6px;text-decoration:none;font-weight:600;margin-top:10px}}
 .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:18px 0 24px}}
 .stat{{background:#fff;border:1px solid #e2e8f0;padding:14px;border-radius:8px;text-align:center}}
 .stat-num{{font-size:1.55rem;font-weight:700;color:#6366f1;letter-spacing:-.02em}}
 .stat-label{{color:#64748b;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}}
 .footer a{{color:#6366f1;text-decoration:none}}
 .note{{background:#fef3c7;border:1px solid #fbbf24;padding:14px 20px;border-radius:8px;margin:18px 0;font-size:.92rem;color:#92400e}}
</style></head><body>
<div class="eyebrow">DC Hub · Proof &amp; Case Studies</div>
<h1>Real outputs, daily cadence</h1>
<p class="lead">Most "case studies" pages are polished decks made once and never updated.
Below is the opposite — DC Hub's live, auto-published proof: press releases generated
daily from DCPI data, the $1B+ hyperscaler deal tracker, and the platform metrics that
back every claim.</p>

<div class="stat-grid">
  <div class="stat"><div class="stat-num">285</div><div class="stat-label">DCPI markets</div></div>
  <div class="stat"><div class="stat-num">21,000+</div><div class="stat-label">Facilities</div></div>
  <div class="stat"><div class="stat-num">170+</div><div class="stat-label">Countries</div></div>
  <div class="stat"><div class="stat-num">96+</div><div class="stat-label">AI platforms</div></div>
  <div class="stat"><div class="stat-num">$324B+</div><div class="stat-label">M&amp;A tracked</div></div>
  <div class="stat"><div class="stat-num">369 GW</div><div class="stat-label">Pipeline</div></div>
</div>

<div class="note">
  <b>Named-customer case studies are publishing Q3 2026.</b> Until then, this page surfaces
  the auto-generated proof — press releases that quote our live DCPI data, deal tracking
  that catches every $1B+ announcement within hours, and the methodology behind both.
  Email <a href="mailto:sales@dchub.cloud">sales@dchub.cloud</a> if you'd like to be
  featured.
</div>

<h2>Daily DCPI press cadence</h2>
<p style="color:#64748b;font-size:.9rem">Each release is generated from live data,
sourced and citable, and serves as a public proof-of-platform.</p>
{press_rows or '<p>No recent press; refresh tomorrow.</p>'}

<h2>$1B+ hyperscaler deals tracked this week</h2>
<p style="color:#64748b;font-size:.9rem">DC Hub's hyperscaler tracker catches every
public $1B+ AI infrastructure deal. Live feed at <a href="/hyperscaler-deals">/hyperscaler-deals</a>.</p>
{deal_rows or '<p>No deals tracked yet.</p>'}

<div class="pane">
  <h2>Want your story here?</h2>
  <p style="margin:.5em 0;font-size:.95rem">If DC Hub or our MCP server helped you ship faster,
  pick a better site, or skip a stale-data trap — we'd love to share the story (anonymized
  or named, your call).</p>
  <a class="cta" href="mailto:sales@dchub.cloud?subject=Case%20Study%20Idea">Share your story →</a>
</div>

<p class="footer">
<a href="/">Home</a> · <a href="/dcpi">DCPI</a> · <a href="/hyperscaler-deals">Deal tracker</a>
· <a href="/changelog">Changelog</a> · <a href="/team">Team</a> · Updated {today}
</p>
</body></html>"""
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=900, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.8-case-studies",
    }
