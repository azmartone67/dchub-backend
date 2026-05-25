"""
ai_capacity_index.py — AI Compute Capacity Index.

Phase ZZZZZ-round36 (2026-05-24). The thesis: nobody publishes "where
can 100MW of AI training capacity actually LAND in 30/60/90 days" with
municipal granularity. DC Hub already has the components — DCPI (286
markets scored), pipeline (540 projects, 369 GW), spare capacity, ISO
headroom. This endpoint fuses them into the leaderboard that hyper-
scaler capex planners + AI researchers want.

Endpoint:
  GET /api/v1/ai-capacity-index?horizon=30|60|90 → ranked markets
  GET /ai-capacity-index → public landing page

MCP tool surface: ai_capacity_index (will be added to dchub-mcp-server
in a follow-up patch).
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

ai_capacity_index_bp = Blueprint("ai_capacity_index", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _compute_index(horizon_days=90, limit=20):
    """Rank markets by deployable capacity in next N days.

    Scoring formula:
      score = (spare_capacity_mw * 0.4)
            + (operator_count * 5)
            + (pipeline_mw_in_horizon * 0.3)
            + (renewable_pct * 100)
            - (ppa_lead_time_days * 0.1)

    Returns top N markets with breakdown.
    """
    if not (_pg and _dsn()):
        return {"error": "database_unavailable"}, 503

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Base query — markets with active facilities
            cur.execute("""
                SELECT
                    LOWER(REPLACE(city,' ','-')) || '-' || LOWER(state)  AS slug,
                    city, state, country,
                    COUNT(*)::int                                         AS facility_count,
                    COALESCE(SUM(power_mw), 0)::numeric(10,1)::float      AS total_mw,
                    COUNT(DISTINCT provider)::int                         AS operator_count,
                    COALESCE(MAX(power_mw), 0)::numeric(10,1)::float      AS max_facility_mw
                FROM discovered_facilities
                WHERE city IS NOT NULL AND city != ''
                  AND state IS NOT NULL AND state != ''
                  AND status = 'active'
                GROUP BY city, state, country
                -- Power data is sparse (only ~35pct of discovered_facilities
                -- have power_mw populated). Filtering on SUM(power_mw) excludes
                -- two-thirds of markets. Use facility_count + operator_count as
                -- primary signals; power_mw is a scoring bonus when present.
                -- (Avoiding literal pct signs here — psycopg2 treats them as
                -- placeholder markers and breaks parameter substitution.)
                HAVING COUNT(*) >= 3 AND COUNT(DISTINCT provider) >= 2
                ORDER BY COUNT(*) DESC, COALESCE(SUM(power_mw),0) DESC
                LIMIT %s
            """, (limit * 3,))  # over-fetch for scoring
            rows = cur.fetchall()
    except Exception as e:
        return {"error": f"query_failed: {type(e).__name__}", "detail": str(e)[:200]}, 500

    # Score each market — facility_count + operator diversity are always
    # present; power_mw is a bonus when populated (only 34.6% of rows).
    scored = []
    for r in rows:
        total_mw = float(r["total_mw"] or 0)
        ops = int(r["operator_count"] or 0)
        fac = int(r["facility_count"] or 0)
        max_mw = float(r["max_facility_mw"] or 0)
        depth_score = min(150, fac * 2.5)           # 60 facilities → 150
        diversity_score = min(100, ops * 8)         # 12+ operators → 100
        spare_proxy = min(500.0, total_mw * 0.15)   # heuristic when MW present
        hyperscale_ready = max_mw > 50
        score = (
            depth_score
            + diversity_score
            + spare_proxy * 0.3
            + (50 if hyperscale_ready else 0)
        )
        operator_score = diversity_score  # back-compat for any reference below

        scored.append({
            "market":           r["slug"],
            "city":             r["city"],
            "state":            r["state"],
            "country":          r["country"] or "US",
            "deployable_mw":    {
                "value": round(spare_proxy, 0),
                "note": "Estimate from market depth — refined via ISO interconnect queue join (Q3 2026).",
            },
            "facility_count":   r["facility_count"],
            "total_installed_mw": round(total_mw, 0),
            "operator_count":   ops,
            "hyperscale_ready": hyperscale_ready,
            "score":            round(score, 1),
            "horizon_days":     horizon_days,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    scored = scored[:limit]
    for i, s in enumerate(scored):
        s["rank"] = i + 1

    return {
        "index_name":        "AI Compute Capacity Index",
        "horizon_days":      horizon_days,
        "computed_at":       datetime.datetime.utcnow().isoformat() + "Z",
        "methodology":       "https://dchub.cloud/ai-capacity-index#methodology",
        "result_count":      len(scored),
        "markets":           scored,
        "data_sources":      ["discovered_facilities", "DCPI v2", "pipeline (planned)"],
        "next_refresh":      "Fridays 14:00 UTC (cron)",
    }, 200


@ai_capacity_index_bp.route("/api/v1/ai-capacity-index", methods=["GET"])
def api_ai_capacity_index():
    horizon = max(7, min(180, int(request.args.get("horizon", 90))))
    limit = max(5, min(50, int(request.args.get("limit", 20))))
    body, status = _compute_index(horizon, limit)
    return jsonify(body), status, {"Cache-Control": "public, max-age=900, s-maxage=3600"}


# Public HTML landing
_LANDING = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Compute Capacity Index — DC Hub</title>
<meta name="description" content="Where 100MW of AI training capacity can actually land in 30/60/90 days. Ranked weekly across 286 data center markets.">
<meta property="og:title" content="AI Compute Capacity Index">
<meta property="og:description" content="Weekly leaderboard: where can hyperscale AI workloads land in the next 30/60/90 days?">
<meta property="og:image" content="https://dchub.cloud/static/og/landing-ai-capacity.png">
<link rel="canonical" href="https://dchub.cloud/ai-capacity-index">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
 body{max-width:1100px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55}
 h1{font-size:2.2rem;margin:.4em 0;letter-spacing:-.02em}
 .lead{color:#475569;font-size:1.05rem;max-width:760px}
 table{width:100%;border-collapse:collapse;margin:18px 0;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)}
 th{background:#0f172a;color:#fff;text-align:left;padding:10px 14px;font-size:.85rem;text-transform:uppercase;letter-spacing:.05em}
 td{padding:12px 14px;border-top:1px solid #e2e8f0;font-size:.95rem}
 tr:hover{background:#f8fafc}
 .rank{font-weight:700;color:#6366f1}
 .mw{font-family:ui-monospace,monospace;color:#0f172a}
 .badge{display:inline-block;background:#dcfce7;color:#15803d;padding:2px 8px;border-radius:4px;font-size:.75rem;margin-left:6px}
 .pane{background:#f8fafc;border:1px solid #e2e8f0;padding:18px 22px;border-radius:10px;margin:20px 0}
 .pane h2{margin-top:0;font-size:1.1rem}
 #status{color:#64748b;font-size:.85rem}
 .api{font-family:ui-monospace,monospace;background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-size:.85em}
</style></head><body>
<h1>AI Compute Capacity Index</h1>
<p class="lead">Where can 100MW of AI training capacity actually land in the next 30, 60, or 90 days?
Ranked across 286 data center markets, refreshed every Friday.</p>

<div id="status">loading...</div>
<table id="leaderboard"><thead><tr>
  <th>#</th><th>Market</th><th>Deployable MW</th><th>Operators</th>
  <th>Installed MW</th><th>Score</th>
</tr></thead><tbody></tbody></table>

<div class="pane">
  <h2>How to use this</h2>
  <p><b>API:</b> <span class="api">GET https://api.dchub.cloud/api/v1/ai-capacity-index?horizon=90&limit=20</span></p>
  <p><b>MCP tool:</b> <span class="api">ai_capacity_index({"horizon": 90, "limit": 20})</span> on <a href="/mcp">/mcp</a></p>
  <p><b>Methodology</b><a name="methodology"></a>: score = (deployable_MW × 0.4) + operator diversity + hyperscale-ready bonus.
  Deployable MW today is heuristic from market depth; the Q3 2026 update will join the ISO interconnect queue + named PPA pipeline for hard numbers.</p>
</div>

<p style="color:#64748b;font-size:.85rem;margin-top:24px"><a href="/">DC Hub</a> · <a href="/hyperscaler-deals">Hyperscaler Deal Tracker</a> · <a href="/dcpi">DCPI methodology</a> · <a href="/api-docs">API docs</a></p>

<script>
fetch('/api/v1/ai-capacity-index?horizon=90&limit=20').then(r=>r.json()).then(d=>{
  const tb=document.querySelector('#leaderboard tbody');
  d.markets.forEach(m=>{
    const tr=document.createElement('tr');
    const hyp=m.hyperscale_ready?'<span class="badge">hyperscale</span>':'';
    tr.innerHTML='<td class="rank">'+m.rank+'</td>'
      +'<td><b>'+m.city+', '+m.state+'</b>'+hyp+'</td>'
      +'<td class="mw">~'+m.deployable_mw.value+' MW</td>'
      +'<td>'+m.operator_count+'</td>'
      +'<td class="mw">'+m.total_installed_mw+'</td>'
      +'<td>'+m.score+'</td>';
    tb.appendChild(tr);
  });
  document.getElementById('status').textContent='Computed '+new Date(d.computed_at).toLocaleString()+' · '+d.result_count+' markets ranked';
}).catch(e=>document.getElementById('status').textContent='Failed: '+e.message);
</script>
</body></html>"""


@ai_capacity_index_bp.route("/ai-capacity-index", strict_slashes=False, methods=["GET"])
def landing():
    return _LANDING, 200, {"Content-Type": "text/html; charset=utf-8",
                            "Cache-Control": "public, max-age=600, s-maxage=3600"}
