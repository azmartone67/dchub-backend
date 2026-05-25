"""
dcpi_intl_landing.py — public landing for international DCPI coverage.

Phase ZZZZZ-round47.1 (2026-05-25). Pages worker advertises /dcpi/intl
(+ /dcpi/international alias) in PHASE_282_RAILWAY_PATHS but the backend
had no route — both 404'd. This blueprint fills the gap with a public,
SEO-indexed page summarizing the three international ISOs DCPI now covers:

  - AESO (Alberta, Canada)
  - Hydro-Québec (Canada)
  - Nord Pool (15 Nordic + Baltic zones)

The page fetches live snapshots from the three /api/v1/iso/*-intl
endpoints and renders generation mix + capacity inline. JS-free,
sub-1s render, indexable.

Routes:
  GET /dcpi/intl           — the landing page
  GET /dcpi/international  — alias
"""
import datetime
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint

dcpi_intl_bp = Blueprint("dcpi_intl_landing", __name__)


_INTL_ISOS = [
    ("AESO",          "Alberta, Canada",   "/api/v1/iso/aeso-intl/snapshot"),
    ("Hydro-Québec",  "Québec, Canada",    "/api/v1/iso/hydroquebec/snapshot"),
    ("Nord Pool",     "Nordic + Baltic (15 zones)", "/api/v1/iso/nordpool-intl/snapshot"),
]

_BASE = "https://api.dchub.cloud"


def _fetch_snapshot(path, timeout=6):
    try:
        req = urllib.request.Request(
            f"{_BASE}{path}",
            headers={"User-Agent": "DCHub-Landing/1.0", "X-DC-Internal-Warmup": "1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _gather_snapshots():
    with ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(lambda iso: (iso, _fetch_snapshot(iso[2])), _INTL_ISOS))


def _format_mix(mix):
    """Format generation mix dict → readable bars."""
    if not mix:
        return "<em>data unavailable</em>"
    # Sort by share descending, drop near-zero
    sorted_mix = sorted(((k, float(v)) for k, v in mix.items() if v and float(v) > 0.005),
                         key=lambda x: -x[1])
    parts = []
    for name, share in sorted_mix[:6]:
        pct = round(share * 100, 1)
        color = {
            "hydro":   "#3b82f6", "wind":   "#22c55e", "solar":  "#fbbf24",
            "nuclear": "#a855f7", "natural_gas": "#f97316", "coal":  "#374151",
            "biomass": "#84cc16", "imports": "#94a3b8",
        }.get(name, "#94a3b8")
        parts.append(
            f'<span style="display:inline-block;background:{color};color:#fff;'
            f'padding:2px 8px;border-radius:3px;font-size:.8rem;margin:2px 4px 2px 0">'
            f'{name} {pct}%</span>'
        )
    return "".join(parts)


def _build_page():
    items = _gather_snapshots()
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")
    rows = []
    for (name, region, _path), snap in items:
        if snap:
            cap = snap.get("installed_capacity_mw") or snap.get("installed_capacity_gw")
            cap_str = f"{int(cap):,} MW" if isinstance(cap, (int, float)) and cap > 100 else (
                f"{cap} GW" if cap else "—"
            )
            mix_html = _format_mix(snap.get("generation_mix"))
            as_of = (snap.get("as_of") or "")[:19].replace("T", " ") + " UTC"
        else:
            cap_str = "data unavailable"
            mix_html = "<em>fetch failed — see <code>/api/v1/iso/*-intl/snapshot</code></em>"
            as_of = "—"
        rows.append(f"""
    <tr>
      <td><b>{name}</b><br><span style="color:#64748b;font-size:.85rem">{region}</span></td>
      <td>{cap_str}</td>
      <td>{mix_html}</td>
      <td style="color:#64748b;font-size:.82rem">{as_of}</td>
    </tr>""")

    rows_html = "".join(rows)
    page = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCPI International — AESO, Hydro-Québec, Nord Pool | DC Hub</title>
<meta name="description" content="DC Hub DCPI extends beyond US ISOs — live data center placement intelligence for AESO (Alberta), Hydro-Québec, and Nord Pool (15 Nordic and Baltic zones).">
<meta property="og:title" content="DCPI International Coverage — DC Hub">
<meta property="og:description" content="Live AESO + Hydro-Québec + Nord Pool grid data feeding DCPI scoring for international data center placement.">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-dcpi.png">
<link rel="canonical" href="https://dchub.cloud/dcpi/intl">
<style>
 body{{max-width:1000px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a}}
 h1{{font-size:2.1rem;margin:.3em 0;letter-spacing:-.02em}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 .lead{{color:#475569;font-size:1.05rem;max-width:780px}}
 table{{width:100%;border-collapse:collapse;margin:18px 0;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
 th{{background:#0f172a;color:#fff;text-align:left;padding:10px 14px;font-size:.85rem;text-transform:uppercase;letter-spacing:.05em}}
 td{{padding:14px;border-top:1px solid #e2e8f0;font-size:.95rem;vertical-align:top}}
 tr:hover{{background:#f8fafc}}
 .pane{{background:#f8fafc;border:1px solid #e2e8f0;padding:18px 22px;border-radius:10px;margin:20px 0}}
 .pane h2{{margin-top:0;font-size:1.1rem}}
 code{{background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-family:ui-monospace,monospace;font-size:.88em}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:24px}}
 .footer a{{color:#6366f1;text-decoration:none}}
</style></head><body>
<div class="eyebrow">DC Hub · DCPI International</div>
<h1>International Data Center Placement Intelligence</h1>
<p class="lead">DCPI extends beyond the 286 US/Canadian ISO zones with first-class
coverage of three international grids. All three feeds are live and updated daily.</p>

<table>
  <thead><tr>
    <th>Grid</th>
    <th>Installed Capacity</th>
    <th>Generation Mix</th>
    <th>As Of</th>
  </tr></thead>
  <tbody>{rows_html}
  </tbody>
</table>

<div class="pane">
  <h2>Why international DCPI matters</h2>
  <p>Hyperscaler capex is spilling out of saturated US markets (Northern VA at AVOID,
  Atlanta caution) and into jurisdictions with low-carbon power surplus. Hydro-Québec
  (94% hydro), Nord Pool (51% hydro + 19% wind + 18% nuclear), and AESO (Alberta, with
  emerging wind-solar at low LCOE) are three frontiers operator capex planners now
  actively evaluate.</p>
  <p><b>API:</b> <code>GET /api/v1/iso/aeso-intl/snapshot</code> · <code>/api/v1/iso/hydroquebec/snapshot</code> · <code>/api/v1/iso/nordpool-intl/snapshot</code></p>
  <p><b>MCP tool:</b> <code>get_grid_data({{"iso": "AESO"}})</code> works for all three.</p>
</div>

<p class="footer"><a href="/dcpi">DCPI methodology</a> · <a href="/ai-capacity-index">AI Capacity Index</a> · <a href="/hyperscaler-deals">$1B+ deal tracker</a> · <a href="/dcpi/methodology">scoring formula</a> · Generated {today}</p>
</body></html>"""
    return page


@dcpi_intl_bp.route("/dcpi/intl", methods=["GET"])
@dcpi_intl_bp.route("/dcpi/international", methods=["GET"])
def landing():
    html = _build_page()
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=600, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.1-dcpi-intl",
    }


@dcpi_intl_bp.route("/api/v1/dcpi/intl/health", methods=["GET"])
def health():
    items = _gather_snapshots()
    return {
        "blueprint":    "dcpi_intl_landing",
        "isos_covered": [name for (name, _, _), _ in items],
        "live_count":   sum(1 for _, snap in items if snap),
        "total_count":  len(items),
    }, 200
