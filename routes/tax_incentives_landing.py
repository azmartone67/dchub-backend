"""
tax_incentives_landing.py — public /tax-incentives landing page.

Phase ZZZZZ-round47.6 (2026-05-25). /tax-incentives was a 404 even
though /api/v1/tax-incentives serves data (top 10 free, top 50 with
identified key). This page renders the free top-10 inline and CTAs
to the gated full list. Pure SEO surface for "data center tax
incentives by state".
"""
import datetime
import json
import urllib.request
from flask import Blueprint

tax_incentives_bp = Blueprint("tax_incentives_landing", __name__)


def _fetch_incentives(limit=15):
    try:
        req = urllib.request.Request(
            f"https://api.dchub.cloud/api/v1/tax-incentives?limit={limit}",
            headers={"User-Agent": "DCHub-Landing/1.0", "X-DC-Internal-Warmup": "1"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"data": [], "_total_available": 50, "_hidden_count": 50}


def _stars(rating):
    rating = int(rating or 0)
    return "★" * rating + "☆" * (5 - rating)


@tax_incentives_bp.route("/tax-incentives", methods=["GET"], strict_slashes=False)
def landing():
    payload = _fetch_incentives()
    states = payload.get("data", [])
    hidden = payload.get("_hidden_count", 0)
    total = payload.get("_total_available", 50)
    today = datetime.datetime.utcnow().strftime("%B %Y")

    rows = []
    for s in states[:15]:
        name = s.get("name", s.get("abbr", "?"))
        abbr = s.get("abbr", "")
        rating = _stars(s.get("rating", 0))
        sales = "✓" if s.get("sales_tax") else "—"
        prop = "✓" if s.get("property_tax") else "—"
        income = "✓" if s.get("income_tax") else "—"
        elec = "✓" if s.get("electricity_tax") else "—"
        duration = s.get("duration", "—")
        min_inv = s.get("min_investment", "—")
        jobs = s.get("jobs_required", "—")
        details = (s.get("details") or "")[:120]
        rows.append(f"""
      <tr>
        <td><b>{name}</b> <span style="color:#94a3b8;font-size:.8rem">({abbr})</span></td>
        <td style="color:#fbbf24;font-size:.95rem">{rating}</td>
        <td>{sales}</td><td>{prop}</td><td>{income}</td><td>{elec}</td>
        <td style="font-size:.85rem">{duration}<br><span style="color:#64748b">min {min_inv}</span></td>
        <td style="font-size:.82rem;color:#475569">{details}</td>
      </tr>""")

    rows_html = "".join(rows) or '<tr><td colspan="8">API temporarily unavailable</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data Center Tax Incentives by State — DC Hub</title>
<meta name="description" content="Compare data center tax incentives across all 50 US states: sales/property/income/electricity tax abatements, minimum investment thresholds, jobs requirements, duration.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/tax-incentives">
<meta property="og:title" content="Data Center Tax Incentives by State — DC Hub">
<meta property="og:description" content="Compare 50 US states on sales / property / income / electricity tax incentives for data center deployment.">
<style>
 body{{max-width:1100px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a}}
 h1{{font-size:2.1rem;margin:.3em 0;letter-spacing:-.02em}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 .lead{{color:#475569;font-size:1.05rem;max-width:820px}}
 table{{width:100%;border-collapse:collapse;margin:16px 0;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05);font-size:.9rem}}
 th{{background:#0f172a;color:#fff;text-align:left;padding:10px 12px;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
 th.c{{text-align:center}}
 td{{padding:10px 12px;border-top:1px solid #e2e8f0;vertical-align:top}}
 td:nth-child(3),td:nth-child(4),td:nth-child(5),td:nth-child(6){{text-align:center}}
 tr:hover{{background:#f8fafc}}
 .pane{{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:22px 28px;border-radius:10px;margin:22px 0;text-align:center}}
 .pane h2{{margin:0 0 8px;font-size:1.2rem}}
 .pane .cta{{display:inline-block;background:#fff;color:#6366f1;padding:10px 22px;border-radius:6px;text-decoration:none;font-weight:600;margin-top:10px}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}}
 .footer a{{color:#6366f1;text-decoration:none}}
</style></head><body>
<div class="eyebrow">DC Hub · State Tax Incentives</div>
<h1>Data Center Tax Incentives by State</h1>
<p class="lead">Compare {total} US states on sales / property / income / electricity tax abatements
for data-center deployment. Minimum investment thresholds, jobs requirements, duration. Updated {today}.</p>

<table>
 <thead><tr>
   <th>State</th>
   <th>Rating</th>
   <th class="c">Sales</th><th class="c">Property</th><th class="c">Income</th><th class="c">Elec</th>
   <th>Duration / Min Invest</th>
   <th>Details</th>
 </tr></thead>
 <tbody>{rows_html}
 </tbody>
</table>

<div class="pane">
  <h2>{hidden} more states available with a free dev key</h2>
  <p style="margin:.5em 0;font-size:.95rem">Full 50-state coverage + downloadable CSV + MCP query support.
  Free dev tier: 5 calls/day, no credit card.</p>
  <a class="cta" href="/redeem">Claim free key — 30s</a>
</div>

<p class="footer">
  Data sources: state revenue departments, governor's office announcements, DC Hub research.
  Source URLs on each row in the
  <a href="/api/v1/tax-incentives">JSON API</a> or via the
  <a href="/mcp">MCP tool</a> <code>get_tax_incentives({{"state": "VA"}})</code>.
  ·
  <a href="/">Home</a> · <a href="/dcpi">DCPI</a> · <a href="/architecture">Architecture</a>
</p>
</body></html>"""
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=900, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.6-tax-incentives",
    }
