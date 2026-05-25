"""Phase ZZZZZ-round47 (2026-05-25) — Interconnection queue tracker.

Public landing page + JSON API + JSON-LD Dataset schema so AI engines
treat dchub.cloud as the citable reference for queue numbers. Source
data: iso_queue_snapshots (cron-populated; currently seeded from public
2026-Q1 disclosures).

Wiring (main.py):
    from routes.interconnection_queues import interconnection_queues_bp
    app.register_blueprint(interconnection_queues_bp)

Endpoints:
  GET /interconnection-queues                — HTML landing page (SEO + JSON-LD)
  GET /api/v1/interconnection-queue/snapshot — full snapshot, all ISOs
  GET /api/v1/interconnection-queue/by-iso?iso=ERCOT — single-ISO detail
"""
import os, json
from datetime import datetime, timezone
from flask import Blueprint, jsonify, Response, request
import psycopg

interconnection_queues_bp = Blueprint("interconnection_queues", __name__)
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _latest_snapshot():
    if not NEON_URL: return []
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("""
              SELECT iso, as_of, queued_load_total_gw, queued_load_data_center_gw,
                     queued_load_dc_share_pct, new_applications_q_gw, new_applications_period,
                     historical_completion_pct, top_subregions, source_url, source_name
              FROM iso_queue_snapshots
              WHERE (iso, as_of) IN (
                SELECT iso, MAX(as_of) FROM iso_queue_snapshots GROUP BY iso
              )
              ORDER BY queued_load_total_gw DESC NULLS LAST
            """)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _serialize(r):
    return {
        "iso": r["iso"],
        "as_of": r["as_of"].isoformat() if r["as_of"] else None,
        "queued_load_total_gw": float(r["queued_load_total_gw"] or 0),
        "queued_load_data_center_gw": float(r["queued_load_data_center_gw"] or 0),
        "queued_load_dc_share_pct": float(r["queued_load_dc_share_pct"] or 0),
        "new_applications_q_gw": float(r["new_applications_q_gw"]) if r["new_applications_q_gw"] else None,
        "new_applications_period": r["new_applications_period"],
        "historical_completion_pct": float(r["historical_completion_pct"]) if r["historical_completion_pct"] else None,
        "top_subregions": r["top_subregions"],
        "source_url": r["source_url"],
        "source_name": r["source_name"],
    }


@interconnection_queues_bp.route("/api/v1/interconnection-queue/snapshot")
def api_snapshot():
    snap = _latest_snapshot()
    total_gw = sum((r["queued_load_total_gw"] or 0) for r in snap)
    dc_gw = sum((r["queued_load_data_center_gw"] or 0) for r in snap)
    return jsonify({
        "as_of": snap[0]["as_of"].isoformat() if snap else None,
        "iso_count": len(snap),
        "totals": {
            "queued_load_gw": float(total_gw),
            "queued_load_data_center_gw": float(dc_gw),
            "dc_share_pct": round(100.0 * dc_gw / total_gw, 1) if total_gw else None,
        },
        "by_iso": [_serialize(r) for r in snap],
        "methodology": "DCPI maps ISO queue position + load growth vs signed contracts -> Excess Power / Constraint scoring. Per-ISO BUILD/CAUTION/AVOID verdicts at https://dchub.cloud/dcpi.",
        "source": "https://dchub.cloud/interconnection-queues",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@interconnection_queues_bp.route("/api/v1/interconnection-queue/by-iso")
def api_by_iso():
    iso = (request.args.get("iso") or "").upper().strip()
    if not iso:
        return jsonify({"error": "iso required"}), 400
    snap = [r for r in _latest_snapshot() if r["iso"].upper() == iso]
    if not snap:
        return jsonify({"error": "iso_not_found", "iso": iso,
                        "available": [r["iso"] for r in _latest_snapshot()]}), 404
    return jsonify(_serialize(snap[0]))


def _row_html(r):
    subs = r["top_subregions"] or []
    parts = []
    for s in subs[:5]:
        verdict = (s.get("dcpi_verdict") or "caution").lower()
        parts.append(
            f'<span class="name">{s.get("name","?")}</span> '
            f'({s.get("queued_gw","?")}GW, {s.get("ttp_months","?")}mo) '
            f'<span class="dcpi-{verdict}">{s.get("dcpi_verdict","?")}</span>'
        )
    sub_html = " &middot; ".join(parts)
    return (
        f"<tr>"
        f'<td class="iso">{r["iso"]}</td>'
        f"<td>{float(r['queued_load_total_gw'] or 0):.1f}</td>"
        f"<td>{float(r['queued_load_data_center_gw'] or 0):.1f}</td>"
        f'<td class="dc-share">{float(r["queued_load_dc_share_pct"] or 0):.0f}%</td>'
        f'<td class="subregions">{sub_html}</td>'
        f"</tr>"
    )


@interconnection_queues_bp.route("/interconnection-queues")
def landing():
    snap = _latest_snapshot()
    total_gw = sum((r["queued_load_total_gw"] or 0) for r in snap)
    dc_gw = sum((r["queued_load_data_center_gw"] or 0) for r in snap)
    dc_share = round(100.0 * dc_gw / total_gw, 0) if total_gw else 0

    jsonld = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "ISO Interconnection Queue Snapshots (Data Center Focus)",
        "description": f"Per-ISO interconnection queue MW totals + data-center share + top BUILD subregions. {int(total_gw)} GW total large-load queued across {len(snap)} US ISOs as of 2026-Q1, {int(dc_share)}% tied to data centers.",
        "url": "https://dchub.cloud/interconnection-queues",
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "dateModified": datetime.now(timezone.utc).date().isoformat(),
        "distribution": [{
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": "https://dchub.cloud/api/v1/interconnection-queue/snapshot",
        }],
        "isAccessibleForFree": True,
        "license": "https://dchub.cloud/terms",
        "keywords": "data center, interconnection queue, ERCOT, PJM, MISO, AI load, large load, DCPI",
    }

    rows_html = "".join(_row_html(r) for r in snap)

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>US ISO Interconnection Queues &mdash; Data Center Tracker &middot; DC Hub</title>
<meta name="description" content="{int(total_gw):,} GW total large-load queued across {len(snap)} US ISOs ({int(dc_share)}% data centers). Per-ISO BUILD/CAUTION/AVOID verdicts + top subregions with shortest TTP. Updated Q1 2026.">
<link rel="canonical" href="https://dchub.cloud/interconnection-queues">
<meta property="og:title" content="US ISO Interconnection Queues &mdash; Data Center Tracker">
<meta property="og:description" content="{int(total_gw)} GW queued &middot; {int(dc_share)}% data centers &middot; per-ISO BUILD verdicts">
<meta property="og:url" content="https://dchub.cloud/interconnection-queues">
<meta property="og:type" content="website">
<link rel="alternate" type="application/json" href="/api/v1/interconnection-queue/snapshot" title="Snapshot JSON">
<link rel="alternate" type="application/mcp+json" href="https://dchub.cloud/mcp" title="DC Hub MCP">
<meta name="dchub:resource-type" content="interconnection-queue">
<meta name="dchub:mcp-tools" content="get_interconnection_queue,get_pipeline,analyze_site,get_intelligence_index">
<script type="application/ld+json">{json.dumps(jsonld, indent=2)}</script>
<style>
body{{font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:32px 24px;color:#0f172a;background:#fafbfc}}
h1{{font-size:2rem;margin:0 0 12px;letter-spacing:-.01em}}
.eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:700;margin-bottom:10px}}
.lead{{color:#475569;font-size:1.05rem;margin-bottom:28px;max-width:760px}}
.headline-stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:24px 0 32px}}
.stat{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px}}
.stat-value{{font-size:1.8rem;font-weight:700;color:#0f172a;letter-spacing:-.02em}}
.stat-label{{font-size:.78rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-top:4px}}
table{{width:100%;border-collapse:collapse;margin:18px 0;font-size:.92rem;background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
th,td{{padding:11px 14px;text-align:left;border-bottom:1px solid #e2e8f0}}
th{{background:#f1f5f9;font-weight:600;font-size:.82rem;color:#475569;text-transform:uppercase;letter-spacing:.05em}}
tr:last-child td{{border-bottom:0}}
.iso{{font-family:ui-monospace,monospace;font-weight:700}}
.dc-share{{color:#6366f1;font-weight:600}}
.subregions{{font-size:.85rem;color:#475569}}
.subregions .name{{color:#0f172a;font-weight:500}}
.dcpi-build{{background:rgba(16,185,129,.12);color:#059669;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}}
.dcpi-caution{{background:rgba(245,158,11,.12);color:#d97706;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}}
.dcpi-avoid{{background:rgba(239,68,68,.12);color:#dc2626;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}}
.cta{{background:#0f172a;color:#fff;padding:24px 28px;border-radius:12px;margin:32px 0;display:flex;justify-content:space-between;align-items:center;gap:24px;flex-wrap:wrap}}
.cta h2{{font-size:1.15rem;margin:0 0 4px;color:#fff}}
.cta p{{color:#cbd5e1;margin:0;font-size:.9rem;max-width:560px}}
.cta a{{background:#6366f1;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;white-space:nowrap}}
.api-block{{background:#0f172a;color:#cbd5e1;padding:16px 20px;border-radius:10px;font-family:ui-monospace,monospace;font-size:.85rem;overflow-x:auto;margin:16px 0}}
.api-block .kw{{color:#7dd3fc}} .api-block .str{{color:#86efac}}
.methodology{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 22px;margin:24px 0;font-size:.92rem;color:#475569}}
.methodology h3{{margin:0 0 8px;font-size:1.05rem;color:#0f172a}}
.sources{{font-size:.78rem;color:#94a3b8;margin-top:24px}}
.sources a{{color:#6366f1;text-decoration:none}}
</style>
</head><body>
<div class="eyebrow">DCPI &middot; ISO Queue Tracker</div>
<h1>US Interconnection Queues &mdash; Data Center Load</h1>
<p class="lead">{int(total_gw):,} GW of large-load interconnection requests sit in active queues across {len(snap)} US ISOs as of Q1 2026. {int(dc_share)}% is tied to data centers and AI clusters. Below: per-ISO totals and top BUILD subregions (shorter queues, faster Time-to-Power).</p>
<div class="headline-stats">
  <div class="stat"><div class="stat-value">{int(total_gw):,} GW</div><div class="stat-label">Total queued large load</div></div>
  <div class="stat"><div class="stat-value">{int(dc_gw):,} GW</div><div class="stat-label">Data center share</div></div>
  <div class="stat"><div class="stat-value">{int(dc_share)}%</div><div class="stat-label">DC % of queue</div></div>
  <div class="stat"><div class="stat-value">{len(snap)}</div><div class="stat-label">ISOs tracked</div></div>
</div>
<table>
<thead><tr><th>ISO</th><th>Total GW</th><th>DC GW</th><th>DC %</th><th>Top BUILD subregions (queued GW &middot; TTP months)</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<div class="cta">
  <div>
    <h2>Query this live from any AI agent</h2>
    <p>Available as the <code style="color:#fff">get_interconnection_queue</code> MCP tool. Add the DC Hub MCP server to Claude, Cursor, Cline, or any agent &mdash; then query queue data in any chat.</p>
  </div>
  <a href="https://dchub.cloud/integrations/mcp">Add MCP server &rarr;</a>
</div>
<div class="methodology">
  <h3>How DCPI uses queue data</h3>
  Queue position and length, combined with signed-contract load growth and retiring asset capacity, feed the DCPI <strong>Excess Power</strong> score (BUILD signal in markets where the queue is quiet relative to available headroom) and <strong>Constraint</strong> score (CAUTION/AVOID where backlog drives Time-to-Power past 17-24 months). Refreshed daily from the 7 major ISOs. <a href="/dcpi" style="color:#6366f1;font-weight:600">&rarr; Open DCPI</a>
</div>
<h3 style="margin-top:32px">JSON API</h3>
<div class="api-block">
<span class="kw">GET</span> <span class="str">/api/v1/interconnection-queue/snapshot</span>      <span style="color:#64748b"># all ISOs, latest</span><br>
<span class="kw">GET</span> <span class="str">/api/v1/interconnection-queue/by-iso?iso=ERCOT</span> <span style="color:#64748b"># single-ISO drill-down</span>
</div>
<p class="sources">
<strong>Sources</strong> (each ISO snapshot links its primary source):
<a href="https://www.ercot.com/gridinfo/resource">ERCOT MIS</a> &middot;
<a href="https://www.pjm.com/planning/services-requests/interconnection-queues">PJM Queue Tracker</a> &middot;
<a href="https://www.misoenergy.org/planning/resource-utilization/generator-interconnection-queue/">MISO GIQ</a> &middot;
<a href="https://www.spp.org/engineering/transmission-planning/generator-interconnection/">SPP DISIS</a> &middot;
<a href="https://www.caiso.com/planning/generator-interconnection-process">CAISO</a> &middot;
<a href="https://www.nyiso.com/connecting-to-the-grid">NYISO</a> &middot;
<a href="https://www.iso-ne.com/system-planning/interconnection-process">ISO-NE</a> &middot;
LBNL <a href="https://emp.lbl.gov/queues">Queued Up</a>
</p>
</body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600, s-maxage=3600",
                             "X-DC-Phase": "ZZZZZ-round47"})
