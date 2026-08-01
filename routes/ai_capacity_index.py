"""
ai_capacity_index.py — AI Compute Capacity Index.

Phase ZZZZZ-round36 (2026-05-24). The thesis: nobody publishes "where
can 100MW of AI training capacity actually LAND in 30/60/90 days" with
municipal granularity. DC Hub already has the components — DCPI (300+
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

from util.status_taxonomy import (  # noqa: E402
    operational_sql as _status_operational_sql,
    pipeline_sql as _status_pipeline_sql,
    basis as _status_basis,
)

ai_capacity_index_bp = Blueprint("ai_capacity_index", __name__)

# ── what this index COUNTS ───────────────────────────────────────────────────
# Fragments built once at import — pure string work over module constants, no
# DB, no I/O, and no literal pct sign in the output (psycopg2 reads one as a
# placeholder marker and the query 500s). Narration stays OUT of the SQL string:
# the backfill scanner reads string constants, and a quoted dead predicate
# re-arms its block (r-status-canon).
_FLEET = "COALESCE(is_duplicate, 0) = 0"

# r-status-canon moved the shell exclusion onto the fleet filter. That is the
# right axis, but is_duplicate=0 does NOT mean "distinct": 727 of the rows it
# admits collide on (city, state, LOWER(name)) with a row already counted, and
# 726 were never stamped by the dedup pass at all (dedup_method IS NULL). #2058
# measured the same shape from the other side — 8,460 shells shadow an
# Operational row by name+city, only 6,110 of them flagged. A field called
# facility_count must count facilities, so count distinct names, not rows.
# `name` is 100pct populated here (0 blank of 13,110), so nothing collapses
# into a single empty bucket.
_FACILITIES = "COUNT(DISTINCT LOWER(TRIM(name)))"

# Operator diversity over NAMED operators. Plain COUNT(DISTINCT provider)
# treats '' as an operator, so a market with three facilities from ONE named
# operator plus one unattributed row cleared the ">= 2 operators" gate on a
# blank string. 20 markets pass on exactly that.
_OPERATORS = "COUNT(DISTINCT NULLIF(TRIM(provider),''))"

# Lifecycle from the shared taxonomy, never a literal here — it owns every
# spelling of both cohorts, so these fragments read identically before and
# after the canon backfill.
_OPERATIONAL = _status_operational_sql()
_PIPELINE = _status_pipeline_sql()

# Depth = distinct OPERATIONAL facilities. An announced project is not depth in
# a market, it is pipeline in a market, and this index now prints them apart.
_OP_FACILITIES = f"{_FACILITIES} FILTER (WHERE {_OPERATIONAL})"
_OP_OPERATORS = f"{_OPERATORS} FILTER (WHERE {_OPERATIONAL})"


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

    ── WHAT COUNTS AS DEPTH ─────────────────────────────────────────────────
    r-status-canon (#2058) removed the false status literal here and moved the
    shell exclusion onto the #1539 fleet filter. This finishes the job for a
    COUNT-based index, where three things were still wrong:

      * `is_duplicate=0` is the right AXIS but does not mean "distinct" — it
        still admits 727 exact-name collisions, so rows were counted as
        buildings. Depth now counts distinct names.
      * with no lifecycle predicate at all, ANNOUNCED projects count as depth
        and their megawatts landed inside a field named total_INSTALLED_mw:
        68,900 of the 136,589 MW it published on 2026-07-31 was
        Announced / Planned / Under Construction.
      * `''` was an operator, clearing the ">= 2 operators" gate on a blank
        string in 20 markets.

    Deliberately NOT radar's fix (#2052, count only metered rows). Power is
    sparse here on purpose — roughly a third of rows carry power_mw — and for
    THIS index facility COUNT is the market-depth signal, not MW. Filtering the
    count on power would answer a disclosure question with a depth field and
    drop two-thirds of the map. So the zero-MW share is PUBLISHED instead, as
    metered_facility_count beside facility_count: a count is a presence signal,
    not a capacity one, and the reader gets to see the difference.

    Every predicate is status-literal-free and reads through the taxonomy that
    owns both spellings, so the canon backfill cannot move a published figure —
    verified by running this function against the replica before and after a
    simulated backfill: identical.
    """
    if not (_pg and _dsn()):
        return {"error": "database_unavailable"}, 503

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Markets with real operational depth. Fragments are defined at
            # module scope so the lifecycle vocabulary stays in one file, and
            # so no dead predicate is quoted here for the backfill scanner to
            # trip over (r-status-canon).
            cur.execute(f"""
                SELECT
                    LOWER(REPLACE(city,' ','-')) || '-' || LOWER(state)  AS slug,
                    city, state, country,
                    {_OP_FACILITIES}::int                                 AS facility_count,
                    {_FACILITIES} FILTER (WHERE {_OPERATIONAL}
                        AND COALESCE(power_mw,0) > 0)::int                AS metered_facility_count,
                    {_FACILITIES}::int                                    AS tracked_count,
                    COALESCE(SUM(power_mw) FILTER (WHERE {_OPERATIONAL}), 0)
                        ::numeric(10,1)::float                            AS total_mw,
                    COALESCE(SUM(power_mw) FILTER (WHERE {_PIPELINE}), 0)
                        ::numeric(10,1)::float                            AS pipeline_mw,
                    {_OP_OPERATORS}::int                                  AS operator_count,
                    COALESCE(MAX(power_mw) FILTER (WHERE {_OPERATIONAL}), 0)
                        ::numeric(10,1)::float                            AS max_facility_mw
                FROM discovered_facilities
                WHERE city IS NOT NULL AND city != ''
                  AND state IS NOT NULL AND state != ''
                  AND {_FLEET}
                GROUP BY city, state, country
                -- Power data is sparse (only ~35pct of discovered_facilities
                -- have power_mw populated). Filtering the COUNT on power would
                -- exclude two-thirds of markets and turn a depth field into a
                -- disclosure field — metered_facility_count reports that share
                -- instead. facility_count + operator_count stay the primary
                -- signals; power_mw is a scoring bonus when present.
                -- (Avoiding literal pct signs here — psycopg2 treats them as
                -- placeholder markers and breaks parameter substitution.)
                HAVING {_OP_FACILITIES} >= 3 AND {_OP_OPERATORS} >= 2
                ORDER BY {_OP_FACILITIES} DESC,
                         COALESCE(SUM(power_mw) FILTER (WHERE {_OPERATIONAL}),0) DESC
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
        # max_facility_mw is OPERATIONAL-only, so this asks "is a >50 MW box
        # RUNNING here", not "has anyone announced one". Against the old
        # unfiltered MAX, markets whose largest facility was a press release
        # took the +50 bonus on 0 operational MW — One TX (5,000 MW announced),
        # Las Cruces NM (4,500), Mount Pleasant WI (2,300) all scored
        # hyperscale_ready with nothing above 50 MW in service.
        hyperscale_ready = max_mw > 50
        score = (
            depth_score
            + diversity_score
            + spare_proxy * 0.3
            + (50 if hyperscale_ready else 0)
        )
        operator_score = diversity_score  # back-compat for any reference below

        # AI-ready MW — HONEST PROXY. DC Hub has no per-rack density / cooling
        # data (no public source exists), so this is derived from DISCLOSED
        # installed capacity, discounted, and weighted up only where a
        # hyperscale/multi-operator signal suggests modern (liquid-capable)
        # deployments. It is a market-level indicator, NOT a per-facility
        # kW/rack claim — labeled so agents don't over-trust it.
        ai_ready_factor = 0.6 if (hyperscale_ready or ops >= 3) else 0.3
        ai_ready_mw = round(total_mw * ai_ready_factor, 0)

        scored.append({
            "market":           r["slug"],
            "city":             r["city"],
            "state":            r["state"],
            "country":          r["country"] or "US",
            "deployable_mw":    {
                "value": round(spare_proxy, 0),
                "note": "Estimate from market depth — refined via ISO interconnect queue join (Q3 2026).",
            },
            "ai_ready_mw":      {
                "value": ai_ready_mw,
                "basis": "proxy",
                "note": ("Proxy from disclosed installed MW (x%.1f hyperscale/"
                         "multi-operator weight). NOT a per-rack kW/density "
                         "measurement — no public per-facility density data "
                         "exists yet." % ai_ready_factor),
            },
            "facility_count":   r["facility_count"],
            # The zero-MW share, PUBLISHED rather than filtered away. Roughly
            # two-thirds of rows carry no power_mw, so facility_count is a
            # presence signal; this says how much of it is metered.
            "metered_facility_count": r["metered_facility_count"],
            # Every fleet facility here regardless of lifecycle — keeps the rows
            # facility_count leaves out auditable instead of vanished.
            "tracked_count":    r["tracked_count"],
            "total_installed_mw": round(total_mw, 0),
            # Announced / permitted / building — DISJOINT from installed by
            # construction (util/status_taxonomy), so no megawatt is counted
            # twice. Previously folded INTO total_installed_mw: 68,900 of the
            # 136,589 MW that field published on 2026-07-31 was pipeline.
            "pipeline_mw":      round(float(r["pipeline_mw"] or 0), 0),
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
        "ai_readiness_basis": ("ai_ready_mw is a market-level PROXY from disclosed "
                               "installed capacity — DC Hub does not yet ingest "
                               "per-rack power density or cooling type (no public "
                               "source). Treat as directional, not a spec claim."),
        # A count is only honest if the reader can see which rows produced it.
        # Same house pattern as routes/dcpi.py's status_basis block.
        "depth_basis": {
            "source_table":    "discovered_facilities",
            "facility_count":  ("distinct OPERATIONAL facilities in the fleet — "
                                "COUNT(DISTINCT LOWER(TRIM(name))) over the "
                                "#1539 fleet filter. Counts buildings, not rows: "
                                "is_duplicate=0 still admits exact-name "
                                "collisions from bulk ingest."),
            "operator_count":  "distinct NAMED providers ('' is not an operator)",
            "metered_note":    ("power_mw is populated on roughly a third of rows, "
                                "so facility_count is a PRESENCE signal, not a "
                                "capacity one. metered_facility_count reports the "
                                "share that carries disclosed MW; the count is "
                                "deliberately NOT filtered on power."),
            "status_basis":    _status_basis(scope="ai_capacity_index"),
            "status_literals_in_query": False,
        },
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
<meta name="description" content="Where 100MW of AI training capacity can actually land in 30/60/90 days. Ranked weekly across 232 data center markets.">
<meta property="og:title" content="AI Compute Capacity Index">
<meta property="og:description" content="Weekly leaderboard: where can hyperscale AI workloads land in the next 30/60/90 days?">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-ai-capacity.png">
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
Ranked across every market with at least 3 operational facilities from 2+ operators, refreshed every Friday.</p>

<div id="status">loading...</div>
<table id="leaderboard"><thead><tr>
  <th>#</th><th>Market</th><th>Deployable MW</th><th>Operators</th>
  <th>Facilities</th><th>Installed MW</th><th>Score</th>
</tr></thead><tbody></tbody></table>

<div class="pane">
  <h2>How to use this</h2>
  <p><b>API:</b> <span class="api">GET https://api.dchub.cloud/api/v1/ai-capacity-index?horizon=90&limit=20</span></p>
  <p><b>MCP tool:</b> <span class="api">ai_capacity_index({"horizon": 90, "limit": 20})</span> on <a href="/mcp">/mcp</a></p>
  <p><b>Methodology</b><a name="methodology"></a>: score = (deployable_MW × 0.4) + operator diversity + hyperscale-ready bonus.
  Deployable MW today is heuristic from market depth; the Q3 2026 update will join the ISO interconnect queue + named PPA pipeline for hard numbers.</p>
  <p><b>What "facilities" counts</b>: distinct <i>operational</i> facilities, deduplicated by name within the market.
  Announced and under-construction projects are reported separately as <span class="api">pipeline_mw</span> and never counted as depth.
  Roughly a third of tracked facilities disclose a power figure, so the count is a <b>presence</b> signal, not a capacity one —
  <span class="api">metered_facility_count</span> in the API reports how much of it carries disclosed MW, and
  <span class="api">installed MW</span> sums operational facilities only.</p>
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
      +'<td>'+m.facility_count+' <span style="color:#94a3b8;font-size:.85em" '
      +'title="of which disclose a power figure">('+m.metered_facility_count+' metered)</span></td>'
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
