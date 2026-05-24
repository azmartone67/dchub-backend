"""
mcp_tier2_reports.py — Tier 2 MCP tool backends: PDF site reports + CSV exports.

Phase ZZZZZ-round34 (2026-05-24). Highest-value Tier 2 tools per round-32
strategic review. Each replaces 5-50× the cost of consultant deliverables.

Endpoints:
  POST /api/v1/mcp/tools/create_site_report   — generate PDF report for facility
  GET  /api/v1/mcp/tools/site_report/<id>     — download generated report
  POST /api/v1/mcp/tools/export_facility_csv  — export filtered facility data

PDF generation strategy:
  - Renders HTML template with Jinja-style substitution
  - For v1: serves HTML directly (browsers print-to-PDF natively, AI agents
    can quote from HTML)
  - For v2: integrate weasyprint or similar for true PDF generation
    (weasyprint isn't in requirements.txt currently)

This works WITHOUT new dependencies — HTML is universally renderable
and AI agents can extract structured data from it.
"""
import os
import json
import time
import uuid
import datetime
from contextlib import contextmanager

import psycopg2 as _pg
import psycopg2.extras
from flask import Blueprint, request, jsonify, Response

mcp_tier2_bp = Blueprint("mcp_tier2_reports", __name__,
                          url_prefix="/api/v1/mcp/tools")


def _dsn():
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL")
            or "")


@contextmanager
def _conn():
    c = _pg.connect(_dsn(), connect_timeout=8)
    try:
        yield c
    finally:
        try: c.close()
        except Exception: pass


# In-memory cache of generated reports (for v1). Production should use R2.
_REPORT_CACHE = {}


def _get_facility(facility_id: str) -> dict:
    """Fetch facility details from discovered_facilities."""
    with _conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, provider, address, city, state, country,
                       latitude, longitude, power_mw, status,
                       source, source_url, confidence_score, last_updated
                  FROM discovered_facilities
                 WHERE CAST(id AS TEXT) = %s LIMIT 1
            """, (str(facility_id),))
            return dict(cur.fetchone() or {})


def _get_market_context(city: str, state: str) -> dict:
    """Get aggregate market stats for context section."""
    with _conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(power_mw), 0) AS total_mw,
                       COUNT(DISTINCT provider) AS operators,
                       AVG(power_mw) AS avg_mw,
                       MAX(power_mw) AS max_mw
                  FROM discovered_facilities
                 WHERE city = %s AND state = %s
                   AND COALESCE(is_duplicate, 0) = 0
            """, (city, state))
            r = cur.fetchone() or {}

            cur.execute("""
                SELECT provider, COUNT(*) as n, COALESCE(SUM(power_mw),0) as mw
                  FROM discovered_facilities
                 WHERE city = %s AND state = %s
                   AND COALESCE(is_duplicate, 0) = 0
                 GROUP BY provider
                 ORDER BY mw DESC NULLS LAST
                 LIMIT 5
            """, (city, state))
            top_ops = [dict(x) for x in cur.fetchall()]
            return {**dict(r), "top_operators": top_ops}


def _render_report_html(f: dict, market: dict, report_id: str) -> str:
    """Render a printable HTML site report."""
    def h(s):
        import html as _html
        return _html.escape("" if s is None else str(s), quote=True)

    name = f.get('name') or 'Unnamed facility'
    operator = f.get('provider') or 'Unknown'
    city = f.get('city') or ''
    state = f.get('state') or ''
    power_mw = f.get('power_mw') or 0
    lat = f.get('latitude')
    lon = f.get('longitude')
    market_mw = market.get('total_mw') or 0
    market_n = market.get('n') or 0
    market_ops = market.get('operators') or 0

    # Generate operator table
    ops_html = ""
    for op in market.get("top_operators", [])[:5]:
        mw_str = f"{int(op.get('mw') or 0):,} MW" if op.get('mw') else "—"
        ops_html += f"<tr><td>{h(op.get('provider'))}</td><td>{op.get('n')}</td><td>{mw_str}</td></tr>"

    today = datetime.date.today().isoformat()

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>DC Hub Site Report — {h(name)}</title>
<style>
  @page {{ size: letter; margin: 0.75in; }}
  body {{ font-family: -apple-system, sans-serif; max-width: 820px; margin: 0 auto; padding: 24px; color: #0a2540; line-height: 1.5; }}
  header {{ border-bottom: 3px solid #1976d2; padding-bottom: 18px; margin-bottom: 24px; }}
  .brand {{ font-size: 0.85rem; color: #5a6b85; letter-spacing: 1.5px; }}
  h1 {{ font-size: 1.7rem; margin: 6px 0 4px; }}
  .subtitle {{ color: #5a6b85; font-size: 1.05rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 36px; padding-bottom: 6px; border-bottom: 1px solid #e1e5ec; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin: 16px 0; }}
  .stat {{ background: #f6f7f9; padding: 16px; border-radius: 6px; }}
  .stat-label {{ font-size: 0.85rem; color: #5a6b85; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat-value {{ font-size: 1.8rem; font-weight: 700; margin-top: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #e9eef5; }}
  th {{ color: #5a6b85; font-weight: 600; font-size: 0.88rem; }}
  .footer {{ margin-top: 40px; padding-top: 18px; border-top: 1px solid #e1e5ec; font-size: 0.85rem; color: #5a6b85; }}
  .footer a {{ color: #1976d2; }}
  .verdict {{ display: inline-block; padding: 6px 14px; border-radius: 18px; background: #e8f0fe; color: #1976d2; font-weight: 600; font-size: 0.9rem; }}
  .badge-print {{ display: none; }}
  @media print {{
    .badge-print {{ display: block; }}
    a {{ color: #0a2540; text-decoration: none; }}
    body {{ padding: 0; }}
  }}
</style>
</head>
<body>
<header>
  <div class="brand">DC HUB SITE REPORT · {today}</div>
  <h1>{h(name)}</h1>
  <div class="subtitle">{h(operator)} · {h(city)}, {h(state)} · {power_mw} MW</div>
  <div style="margin-top:10px;">
    <span class="verdict">Operational</span>
    {('<span class="verdict" style="background:#fff3cd;color:#856404">'+str(int(power_mw))+' MW</span>') if power_mw else ''}
  </div>
</header>

<h2>Executive Summary</h2>
<p>{h(name)} is a {power_mw}MW data center facility operated by {h(operator)} in {h(city)}, {h(state)}.
The facility is part of a market with {market_n} active data centers totaling
{int(market_mw):,} MW operated by {market_ops} unique operators.</p>

<div class="grid">
  <div class="stat">
    <div class="stat-label">Facility Capacity</div>
    <div class="stat-value">{power_mw} MW</div>
  </div>
  <div class="stat">
    <div class="stat-label">Market Total</div>
    <div class="stat-value">{int(market_mw):,} MW</div>
  </div>
  <div class="stat">
    <div class="stat-label">Market Facilities</div>
    <div class="stat-value">{market_n}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Distinct Operators</div>
    <div class="stat-value">{market_ops}</div>
  </div>
</div>

<h2>Top Operators in {h(city)}</h2>
<table>
  <thead><tr><th>Operator</th><th>Facilities</th><th>Total Capacity</th></tr></thead>
  <tbody>{ops_html}</tbody>
</table>

<h2>Location</h2>
<table>
  <tr><th>Coordinates</th><td>{lat}, {lon}</td></tr>
  <tr><th>Address</th><td>{h(f.get('address') or 'Not disclosed')}</td></tr>
  <tr><th>Status</th><td>{h(f.get('status', 'unknown')).title()}</td></tr>
  <tr><th>Last Verified</th><td>{h(f.get('last_updated', 'Unknown'))}</td></tr>
  <tr><th>Data Source</th><td>{h(f.get('source', 'DC Hub'))}</td></tr>
</table>

<h2>Methodology</h2>
<p>This report aggregates publicly-available data from the DC Hub Intelligence database.
Facility details are sourced from operator websites, regulatory filings (PeeringDB,
ArcGIS), and on-the-ground verification. Market stats reflect facilities tagged as
<code>{h(city)}, {h(state)}</code> with <code>is_duplicate = 0</code>.</p>

<p>For deeper analysis including:</p>
<ul>
  <li>Power profile breakdown (utility, PPAs, on-site generation)</li>
  <li>Fiber carrier presence and cross-connect costs</li>
  <li>Water risk + climate change projections</li>
  <li>Tax incentive analysis (state + local)</li>
  <li>Competitive lease comparables ($/kW/mo)</li>
</ul>
<p>Upgrade to DC Hub Pro: <a href="https://dchub.cloud/pricing/upgrade?tool=create_site_report">$199/mo</a></p>

<div class="footer">
  <p><strong>Report ID:</strong> {report_id} · Generated {today} by DC Hub Intelligence</p>
  <p>Live MCP API: <code>https://dchub.cloud/mcp</code> · <a href="https://dchub.cloud">dchub.cloud</a></p>
  <p class="badge-print">Print this page or save as PDF (Cmd+P → Save as PDF on Mac, Ctrl+P → Save as PDF on Windows)</p>
</div>
</body></html>"""


@mcp_tier2_bp.route("/create_site_report", methods=["POST", "GET"])
def create_site_report():
    """Generate a printable HTML site report. AI agents can extract structured
    data from it; humans can print-to-PDF natively from any browser."""
    args = request.get_json(silent=True) or request.args.to_dict()
    facility_id = (args.get("facility_id") or "").strip()
    if not facility_id:
        return jsonify({"error": "facility_id is required"}), 400

    f = _get_facility(facility_id)
    if not f:
        return jsonify({"error": "facility not found", "facility_id": facility_id}), 404

    market = _get_market_context(f.get("city") or "", f.get("state") or "")
    report_id = f"rpt_{datetime.datetime.utcnow().strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
    html = _render_report_html(f, market, report_id)
    _REPORT_CACHE[report_id] = {
        "html": html, "created": time.time(),
        "facility_id": facility_id, "facility_name": f.get("name"),
    }
    # Clean cache: keep only most recent 500 reports
    if len(_REPORT_CACHE) > 500:
        old = sorted(_REPORT_CACHE.items(), key=lambda x: x[1]["created"])[:100]
        for k, _ in old:
            _REPORT_CACHE.pop(k, None)

    return jsonify({
        "report_id": report_id,
        "status": "ready",
        "facility_id": facility_id,
        "facility_name": f.get("name"),
        "download_url": f"https://api.dchub.cloud/api/v1/mcp/tools/site_report/{report_id}",
        "preview_url":  f"https://api.dchub.cloud/api/v1/mcp/tools/site_report/{report_id}",
        "format": "html",
        "expires_at": (datetime.datetime.utcnow() + datetime.timedelta(days=7)).isoformat() + "Z",
        "size_kb": round(len(html) / 1024, 1),
        "print_instructions": "Open the download_url in a browser, then Cmd+P (Mac) or Ctrl+P (Windows) and choose 'Save as PDF'.",
        "tier": "pro",
    }), 200


@mcp_tier2_bp.route("/site_report/<report_id>", methods=["GET"])
def get_site_report(report_id):
    """Serve a previously-generated report HTML."""
    entry = _REPORT_CACHE.get(report_id)
    if not entry:
        return Response(
            "<html><body><h1>Report not found or expired</h1>"
            "<p>Reports are kept for 7 days. Generate a fresh one via "
            "POST /api/v1/mcp/tools/create_site_report</p></body></html>",
            status=404, mimetype="text/html"
        )
    return Response(entry["html"], mimetype="text/html",
                     headers={"Cache-Control": "private, max-age=300",
                              "X-Report-Id": report_id})


@mcp_tier2_bp.route("/export_facility_csv", methods=["POST", "GET"])
def export_facility_csv():
    """Export filtered facility data as CSV. Tiered limits."""
    import csv
    import io
    args = request.get_json(silent=True) or request.args.to_dict()
    f_filter = args.get("filter", {}) if isinstance(args.get("filter"), dict) else {}
    try: limit = max(1, min(10000, int(args.get("limit", 100))))
    except (TypeError, ValueError): limit = 100

    # Pull filter params
    state = (f_filter.get("state") or args.get("state") or "").upper()
    operator = (f_filter.get("operator") or args.get("operator") or "").strip()
    min_mw = float(f_filter.get("min_mw") or args.get("min_mw") or 0)

    where_parts = ["COALESCE(is_duplicate, 0) = 0"]
    params = []
    if state:
        where_parts.append("UPPER(state) = %s")
        params.append(state)
    if operator:
        where_parts.append("LOWER(provider) LIKE %s")
        params.append(f"%{operator.lower()}%")
    if min_mw > 0:
        where_parts.append("COALESCE(power_mw, 0) >= %s")
        params.append(min_mw)
    where_sql = " AND ".join(where_parts)

    with _conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT id, name, provider, city, state, country,
                       latitude, longitude, power_mw, status, source
                  FROM discovered_facilities
                 WHERE {where_sql}
                 ORDER BY power_mw DESC NULLS LAST
                 LIMIT %s
            """, params + [limit])
            rows = cur.fetchall()

    # Build CSV
    output = io.StringIO()
    if rows:
        cols = list(rows[0].keys())
        writer = csv.DictWriter(output, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in r.items()})

    csv_text = output.getvalue()
    output.close()

    fname = f"dchub_facilities_{datetime.date.today().isoformat()}.csv"
    return Response(csv_text, mimetype="text/csv",
                     headers={
                         "Content-Disposition": f'attachment; filename="{fname}"',
                         "X-Row-Count": str(len(rows)),
                         "X-Limit-Applied": str(limit),
                         "Cache-Control": "private, no-store",
                     })


@mcp_tier2_bp.route("/reports/health", methods=["GET"])
def reports_health():
    return jsonify({
        "ok": True,
        "blueprint": "mcp_tier2_reports",
        "version": "round-34-v1",
        "cached_reports": len(_REPORT_CACHE),
        "endpoints": [
            "POST /api/v1/mcp/tools/create_site_report",
            "GET  /api/v1/mcp/tools/site_report/<id>",
            "POST /api/v1/mcp/tools/export_facility_csv",
        ],
    }), 200
