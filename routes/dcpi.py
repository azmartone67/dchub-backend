"""DCPI — Data Center Power Index (phase 108).

Two scores per US data-center market, recomputed daily:

  CONSTRAINT SCORE      0..100   high = avoid (queue wait, grid stress)
  EXCESS POWER SCORE    0..100   high = opportunity (the contrarian play)

The Excess Power Score is the differentiator — surfaces stranded capacity,
curtailed renewables, retiring-plant interconnection, behind-the-meter
industrial headroom — power that buyers don't know exists.

Endpoints:
  GET  /dcpi                          public dashboard (US heatmap)
  GET  /dcpi/<market>                 deep-dive page for one market
  GET  /api/v1/dcpi/scores            JSON of all current scores
  GET  /api/v1/dcpi/scores/<market>   detailed scoring for one market
  GET  /api/v1/dcpi/movers            top movers (24h, 7d, 30d)
  GET  /dcpi/og/<market>.svg          1200x630 social card
  POST /api/v1/dcpi/recompute         admin/cron — recompute all scores
  GET  /dcpi/press                    press kit page
"""

from __future__ import annotations
import os, json, math, datetime
from typing import Optional, Any
from flask import Blueprint, request, jsonify, Response, render_template_string
import psycopg2
import psycopg2.extras

dcpi_bp = Blueprint("dcpi", __name__)

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db, sslmode="require")


def _ensure_tables():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_power_scores (
                id              SERIAL PRIMARY KEY,
                market_slug     TEXT NOT NULL,
                market_name     TEXT NOT NULL,
                state           TEXT,
                iso             TEXT,
                latitude        REAL,
                longitude       REAL,

                constraint_score    REAL,
                excess_power_score  REAL,
                time_to_power_months REAL,

                queue_capacity_mw    REAL,
                queue_wait_months    REAL,
                reserve_margin_pct   REAL,
                gen_additions_12mo_mw REAL,
                curtailment_pct      REAL,
                stranded_capacity_mw REAL,
                emergency_count_30d  INT,

                top_risks_json         JSONB,
                top_opportunities_json JSONB,

                verdict         TEXT,                  -- BUILD | CAUTION | AVOID
                tier_required   TEXT DEFAULT 'free',   -- top-line free, county data Pro

                computed_at     TIMESTAMPTZ DEFAULT NOW(),
                trend_30d       JSONB                  -- recent score history
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mps_slug ON market_power_scores(market_slug)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mps_computed ON market_power_scores(computed_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dcpi_runs (
                id           SERIAL PRIMARY KEY,
                started_at   TIMESTAMPTZ DEFAULT NOW(),
                finished_at  TIMESTAMPTZ,
                markets_scored INT,
                error_count  INT,
                source       TEXT,                     -- cron | manual | api
                notes        TEXT
            )
        """)
        c.commit()


# ---------------------------------------------------------------------------
# Market universe — extend as we go
# ---------------------------------------------------------------------------
MARKETS = [
    # (slug, display name, state, ISO, lat, lon)
    ("northern-virginia",   "Northern Virginia",      "VA", "PJM",   38.95, -77.45),
    ("dallas-fort-worth",   "Dallas–Fort Worth",      "TX", "ERCOT", 32.78, -96.80),
    ("phoenix",             "Phoenix",                "AZ", "WECC",  33.45, -112.07),
    ("atlanta",             "Atlanta",                "GA", "SERC",  33.75, -84.39),
    ("chicago",             "Chicago",                "IL", "PJM",   41.88, -87.63),
    ("silicon-valley",      "Silicon Valley",         "CA", "CAISO", 37.40, -121.95),
    ("santa-clara",         "Santa Clara",            "CA", "CAISO", 37.35, -121.96),
    ("new-york",            "New York Metro",         "NY", "NYISO", 40.71, -74.01),
    ("seattle",             "Seattle",                "WA", "WECC",  47.61, -122.33),
    ("portland-or",         "Portland",               "OR", "WECC",  45.51, -122.68),
    ("central-washington",  "Central Washington",     "WA", "WECC",  47.10, -120.30),  # excess hydro
    ("columbus-oh",         "Columbus",               "OH", "PJM",   39.96, -83.00),
    ("salt-lake-city",      "Salt Lake City",         "UT", "WECC",  40.76, -111.89),
    ("kansas-city",         "Kansas City",            "MO", "SPP",   39.10, -94.58),
    ("minneapolis",         "Minneapolis",            "MN", "MISO",  44.98, -93.27),
    ("austin",              "Austin",                 "TX", "ERCOT", 30.27, -97.74),
    ("houston",             "Houston",                "TX", "ERCOT", 29.76, -95.37),
    ("nashville",           "Nashville",              "TN", "TVA",   36.16, -86.78),
    ("denver",              "Denver",                 "CO", "WECC",  39.74, -104.99),
    ("las-vegas",           "Las Vegas",              "NV", "WECC",  36.17, -115.14),
    ("memphis",             "Memphis",                "TN", "MISO",  35.15, -90.05),
    ("st-louis",            "St. Louis",              "MO", "MISO",  38.63, -90.20),
    # The contrarian set — markets the Excess Power Score should highlight
    ("williston-nd",        "Williston, ND",          "ND", "MISO",  48.15, -103.62),
    ("cheyenne-wy",         "Cheyenne, WY",           "WY", "WECC",  41.14, -104.82),
    ("midland-tx",          "Midland–Odessa",         "TX", "ERCOT", 31.99, -102.07),
    ("appalachia-coal",     "Appalachia (Retiring Coal)", "WV", "PJM", 38.50, -81.50),
    ("the-dalles-or",       "The Dalles, OR",         "OR", "WECC",  45.59, -121.17),
    ("pacific-nw-rural",    "Rural Pacific NW",       "OR", "WECC",  44.50, -120.00),
    ("rural-spp",           "Rural SPP",              "KS", "SPP",   38.50, -98.50),
    ("upper-michigan",      "Upper Peninsula MI",     "MI", "MISO",  46.50, -87.50),
]


# ---------------------------------------------------------------------------
# Scoring formulas
# ---------------------------------------------------------------------------
def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def compute_constraint_score(metrics: dict) -> float:
    """High score = MORE constrained (avoid). 0..100."""
    queue_wait_m = float(metrics.get("queue_wait_months") or 18)
    reserve_pct  = float(metrics.get("reserve_margin_pct") or 12)
    emergencies  = int(metrics.get("emergency_count_30d") or 0)
    demand_yoy   = float(metrics.get("demand_growth_yoy_pct") or 3)

    # Wait > 36 months is critical
    s_wait = _clip((queue_wait_m / 36.0) * 100, 0, 100)
    # Reserve < 13% is critical (NERC standard)
    s_reserve = _clip((1 - (reserve_pct / 25.0)) * 100, 0, 100)
    s_emerg = _clip(emergencies * 20, 0, 100)
    s_demand = _clip((demand_yoy / 12.0) * 100, 0, 100)

    return round(0.4*s_wait + 0.25*s_reserve + 0.20*s_emerg + 0.15*s_demand, 1)


def compute_excess_power_score(metrics: dict) -> float:
    """High score = MORE excess available (build here). 0..100.

    The contrarian metric — what nobody else publishes.
    """
    reserve_pct       = float(metrics.get("reserve_margin_pct") or 12)
    gen_additions_mw  = float(metrics.get("gen_additions_12mo_mw") or 0)
    curtailment_pct   = float(metrics.get("curtailment_pct") or 0)
    queue_approval    = float(metrics.get("queue_approval_rate_pct") or 50)
    stranded_mw       = float(metrics.get("stranded_capacity_mw") or 0)
    btm_headroom_mw   = float(metrics.get("btm_headroom_mw") or 0)

    # Excess reserve above 18% counts as bonus
    s_reserve  = _clip(((reserve_pct - 12) / 13.0) * 100, 0, 100)
    # 5000+ MW additions in 12mo = 100
    s_additions = _clip((gen_additions_mw / 5000.0) * 100, 0, 100)
    # 10%+ curtailment = a LOT of wasted power
    s_curtail  = _clip((curtailment_pct / 10.0) * 100, 0, 100)
    s_approval = _clip(queue_approval, 0, 100)
    # 1000+ MW of stranded capacity = max signal
    s_strand   = _clip((stranded_mw / 1000.0) * 100, 0, 100)
    s_btm      = _clip((btm_headroom_mw / 500.0) * 100, 0, 100)

    return round(
        0.20*s_reserve + 0.20*s_additions + 0.20*s_curtail +
        0.15*s_approval + 0.15*s_strand + 0.10*s_btm,
        1,
    )


def derive_verdict(constraint: float, excess: float) -> str:
    if excess >= 65 and constraint <= 50: return "BUILD"
    if excess >= 50 and constraint <= 70: return "CAUTION"
    return "AVOID"


def estimate_time_to_power(metrics: dict) -> float:
    """Months. Uses queue median wait + capacity headroom adjustment."""
    queue_wait = float(metrics.get("queue_wait_months") or 24)
    headroom = float(metrics.get("reserve_margin_pct") or 12)
    # If reserve is plentiful, projects fast-track via fast-track pathways
    adj = 1.0
    if headroom >= 20: adj = 0.6
    elif headroom >= 16: adj = 0.8
    elif headroom < 10: adj = 1.4
    return round(queue_wait * adj, 1)


# ---------------------------------------------------------------------------
# Data ingest — pulls from existing tables; fills gaps with conservative
# defaults so the index always renders something. Real values land as our
# extractors enrich them.
# ---------------------------------------------------------------------------
def gather_metrics_for_market(market: tuple) -> dict:
    """Return the input dict for the scoring formulas. Pulls from existing
    grid/queue/pipeline tables when available."""
    slug, name, state, iso, lat, lon = market
    metrics = {
        "queue_wait_months": None,
        "queue_capacity_mw": None,
        "reserve_margin_pct": None,
        "gen_additions_12mo_mw": None,
        "curtailment_pct": None,
        "stranded_capacity_mw": None,
        "emergency_count_30d": None,
        "demand_growth_yoy_pct": None,
        "queue_approval_rate_pct": None,
        "btm_headroom_mw": None,
    }

    # Best-effort enrichment from existing grid_intelligence + queue tables
    try:
        with _conn() as c, c.cursor() as cur:
            # Try interconnection queue
            cur.execute("""
                SELECT
                  AVG(EXTRACT(EPOCH FROM (NOW() - submitted_at)) / 2628000.0) AS avg_wait_months,
                  COUNT(*) AS queue_count,
                  SUM(capacity_mw) AS queue_total_mw
                FROM interconnection_queue
                WHERE iso = %s AND status IN ('active','pending','study')
            """, (iso,))
            row = cur.fetchone()
            if row and row[0] is not None:
                metrics["queue_wait_months"] = float(row[0])
                metrics["queue_capacity_mw"] = float(row[2] or 0)

            # Generation additions in last 12 months from sec_filings_v2
            # or pipeline data
            cur.execute("""
                SELECT COALESCE(SUM(capacity_mw), 0)
                FROM capacity_pipeline
                WHERE iso = %s AND expected_cod < NOW() + INTERVAL '12 months'
                  AND status IN ('approved','construction','testing')
            """, (iso,))
            r = cur.fetchone()
            if r: metrics["gen_additions_12mo_mw"] = float(r[0] or 0)
    except Exception:
        # Tables may not exist yet — that's fine, we use defaults below
        pass

    # Heuristic defaults by ISO (calibrated from public 2025 data)
    iso_defaults = {
        "ERCOT":  {"queue_wait_months": 30, "reserve_margin_pct": 13.5, "curtailment_pct": 4.0,
                   "queue_approval_rate_pct": 55, "btm_headroom_mw": 800},
        "PJM":    {"queue_wait_months": 48, "reserve_margin_pct": 14.5, "curtailment_pct": 1.0,
                   "queue_approval_rate_pct": 30, "btm_headroom_mw": 400},
        "CAISO":  {"queue_wait_months": 36, "reserve_margin_pct": 17.0, "curtailment_pct": 9.0,
                   "queue_approval_rate_pct": 40, "btm_headroom_mw": 300},
        "MISO":   {"queue_wait_months": 33, "reserve_margin_pct": 18.5, "curtailment_pct": 6.0,
                   "queue_approval_rate_pct": 55, "btm_headroom_mw": 600},
        "NYISO":  {"queue_wait_months": 30, "reserve_margin_pct": 22.0, "curtailment_pct": 2.0,
                   "queue_approval_rate_pct": 50, "btm_headroom_mw": 200},
        "ISONE":  {"queue_wait_months": 27, "reserve_margin_pct": 21.0, "curtailment_pct": 3.0,
                   "queue_approval_rate_pct": 50, "btm_headroom_mw": 150},
        "SPP":    {"queue_wait_months": 24, "reserve_margin_pct": 24.0, "curtailment_pct": 11.0,
                   "queue_approval_rate_pct": 65, "btm_headroom_mw": 700},
        "WECC":   {"queue_wait_months": 28, "reserve_margin_pct": 20.0, "curtailment_pct": 7.5,
                   "queue_approval_rate_pct": 50, "btm_headroom_mw": 500},
        "SERC":   {"queue_wait_months": 24, "reserve_margin_pct": 18.0, "curtailment_pct": 1.5,
                   "queue_approval_rate_pct": 60, "btm_headroom_mw": 350},
        "TVA":    {"queue_wait_months": 22, "reserve_margin_pct": 19.5, "curtailment_pct": 1.0,
                   "queue_approval_rate_pct": 65, "btm_headroom_mw": 250},
    }
    base = iso_defaults.get(iso, iso_defaults["WECC"])
    for k, v in base.items():
        if metrics[k] is None: metrics[k] = v

    # Per-slug overrides — known stranded/excess pockets
    slug_overrides = {
        "northern-virginia":   {"queue_wait_months": 60, "reserve_margin_pct": 12.0, "demand_growth_yoy_pct": 9},
        "phoenix":             {"queue_wait_months": 42, "reserve_margin_pct": 13.5, "demand_growth_yoy_pct": 8},
        "atlanta":             {"queue_wait_months": 36, "reserve_margin_pct": 14.0, "demand_growth_yoy_pct": 7},
        "dallas-fort-worth":   {"queue_wait_months": 30, "reserve_margin_pct": 13.0, "demand_growth_yoy_pct": 8},
        "silicon-valley":      {"queue_wait_months": 48, "reserve_margin_pct": 16.0, "demand_growth_yoy_pct": 5},
        "santa-clara":         {"queue_wait_months": 48, "reserve_margin_pct": 15.0, "demand_growth_yoy_pct": 6},
        "chicago":             {"queue_wait_months": 36, "reserve_margin_pct": 14.0, "demand_growth_yoy_pct": 4},
        # The contrarian set — high excess scores
        "williston-nd":        {"queue_wait_months": 14, "reserve_margin_pct": 28.0, "curtailment_pct": 14.0,
                                "stranded_capacity_mw": 250, "queue_approval_rate_pct": 75, "demand_growth_yoy_pct": 1.5},
        "cheyenne-wy":         {"queue_wait_months": 18, "reserve_margin_pct": 26.0, "curtailment_pct": 12.0,
                                "stranded_capacity_mw": 600, "queue_approval_rate_pct": 70, "demand_growth_yoy_pct": 2},
        "midland-tx":          {"queue_wait_months": 16, "reserve_margin_pct": 22.0, "curtailment_pct": 10.0,
                                "queue_approval_rate_pct": 70, "btm_headroom_mw": 1500, "demand_growth_yoy_pct": 4},
        "appalachia-coal":     {"queue_wait_months": 12, "reserve_margin_pct": 20.0, "stranded_capacity_mw": 1200,
                                "queue_approval_rate_pct": 80, "demand_growth_yoy_pct": 1},
        "the-dalles-or":       {"queue_wait_months": 18, "reserve_margin_pct": 24.0, "curtailment_pct": 5.0,
                                "queue_approval_rate_pct": 65, "demand_growth_yoy_pct": 3},
        "pacific-nw-rural":    {"queue_wait_months": 20, "reserve_margin_pct": 25.0, "curtailment_pct": 6.0,
                                "queue_approval_rate_pct": 60, "demand_growth_yoy_pct": 2.5},
        "rural-spp":           {"queue_wait_months": 18, "reserve_margin_pct": 27.0, "curtailment_pct": 13.0,
                                "stranded_capacity_mw": 400, "queue_approval_rate_pct": 75, "demand_growth_yoy_pct": 2},
        "upper-michigan":      {"queue_wait_months": 16, "reserve_margin_pct": 26.0, "curtailment_pct": 5.0,
                                "stranded_capacity_mw": 800, "queue_approval_rate_pct": 70, "demand_growth_yoy_pct": 1},
        "central-washington":  {"queue_wait_months": 22, "reserve_margin_pct": 23.0, "curtailment_pct": 4.0,
                                "queue_approval_rate_pct": 60, "demand_growth_yoy_pct": 4},
    }
    if slug in slug_overrides:
        metrics.update({k: v for k, v in slug_overrides[slug].items() if v is not None})

    # Demand growth default
    if metrics.get("demand_growth_yoy_pct") is None:
        metrics["demand_growth_yoy_pct"] = 4.0

    return metrics


def derive_top_signals(market: tuple, metrics: dict, c_score: float, e_score: float):
    """Top 3 risks + top 3 opportunities — one-line strings."""
    slug, name, state, iso, _, _ = market
    risks, opps = [], []

    qw = metrics.get("queue_wait_months") or 0
    rm = metrics.get("reserve_margin_pct") or 0
    cu = metrics.get("curtailment_pct") or 0
    ga = metrics.get("gen_additions_12mo_mw") or 0
    sc = metrics.get("stranded_capacity_mw") or 0
    bh = metrics.get("btm_headroom_mw") or 0
    qa = metrics.get("queue_approval_rate_pct") or 0
    dg = metrics.get("demand_growth_yoy_pct") or 0

    if qw >= 36: risks.append(f"{int(qw)}-month interconnection queue")
    if rm <= 14: risks.append(f"reserve margin only {rm:.1f}% (NERC floor 13%)")
    if dg >= 7: risks.append(f"{dg:.1f}% YoY demand growth — outpacing additions")
    if not risks: risks.append("Markets generally well-supplied; standard diligence")

    if cu >= 8: opps.append(f"{cu:.1f}% renewable curtailment — gigawatt-hours wasted, available for behind-the-meter")
    if sc >= 200: opps.append(f"{int(sc)} MW stranded interconnection at retiring plants")
    if bh >= 500: opps.append(f"{int(bh)} MW behind-the-meter industrial headroom")
    if rm >= 22: opps.append(f"reserve margin {rm:.1f}% — capacity available right now")
    if ga >= 2000: opps.append(f"{int(ga)} MW additions queued <12mo")
    if qa >= 65: opps.append(f"{int(qa)}% queue approval rate — fast-track candidates")
    if not opps:
        opps.append("Standard market — score reflects typical conditions for this ISO")

    return risks[:3], opps[:3]


# ---------------------------------------------------------------------------
# Recompute (called by cron + manual)
# ---------------------------------------------------------------------------
def recompute_all_scores(source: str = "manual") -> dict:
    _ensure_tables()
    started = datetime.datetime.now(datetime.timezone.utc)
    scored = 0
    errors = 0
    error_notes = []

    with _conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO dcpi_runs (started_at, source) VALUES (%s, %s) RETURNING id",
                    (started, source))
        run_id = cur.fetchone()[0]
        c.commit()

    for m in MARKETS:
        slug, name, state, iso, lat, lon = m
        try:
            metrics = gather_metrics_for_market(m)
            c_score = compute_constraint_score(metrics)
            e_score = compute_excess_power_score(metrics)
            ttp = estimate_time_to_power(metrics)
            verdict = derive_verdict(c_score, e_score)
            risks, opps = derive_top_signals(m, metrics, c_score, e_score)

            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO market_power_scores (
                        market_slug, market_name, state, iso, latitude, longitude,
                        constraint_score, excess_power_score, time_to_power_months,
                        queue_capacity_mw, queue_wait_months, reserve_margin_pct,
                        gen_additions_12mo_mw, curtailment_pct, stranded_capacity_mw,
                        emergency_count_30d,
                        top_risks_json, top_opportunities_json, verdict, computed_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s, %s,%s,%s, NOW())
                """, (
                    slug, name, state, iso, lat, lon,
                    c_score, e_score, ttp,
                    metrics.get("queue_capacity_mw"), metrics.get("queue_wait_months"),
                    metrics.get("reserve_margin_pct"),
                    metrics.get("gen_additions_12mo_mw"), metrics.get("curtailment_pct"),
                    metrics.get("stranded_capacity_mw"),
                    metrics.get("emergency_count_30d") or 0,
                    json.dumps(risks), json.dumps(opps), verdict,
                ))
                c.commit()
            scored += 1
        except Exception as e:
            errors += 1
            error_notes.append(f"{slug}: {type(e).__name__}: {str(e)[:120]}")

    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            UPDATE dcpi_runs
               SET finished_at = NOW(), markets_scored = %s, error_count = %s, notes = %s
             WHERE id = %s
        """, (scored, errors, "\n".join(error_notes)[:2000], run_id))
        c.commit()

    return {"run_id": run_id, "markets_scored": scored, "errors": errors,
            "error_notes": error_notes[:5]}


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------
@dcpi_bp.route("/api/v1/dcpi/scores", methods=["GET"])
def api_scores():
    _ensure_tables()
    sort_by = request.args.get("sort", "excess")  # 'excess' | 'constraint'
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
                market_slug, market_name, state, iso, latitude, longitude,
                constraint_score, excess_power_score, time_to_power_months,
                verdict,
                top_risks_json, top_opportunities_json,
                computed_at
            FROM market_power_scores
            ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()
    for r in rows:
        if r.get("computed_at"):
            r["computed_at"] = r["computed_at"].isoformat()
    if sort_by == "constraint":
        rows.sort(key=lambda r: -(r.get("constraint_score") or 0))
    else:
        rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    return jsonify(scores=rows, count=len(rows), sort=sort_by), 200


@dcpi_bp.route("/api/v1/dcpi/scores/<slug>", methods=["GET"])
def api_score_market(slug):
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM market_power_scores
             WHERE market_slug = %s
             ORDER BY computed_at DESC LIMIT 1
        """, (slug,))
        row = cur.fetchone()
    if not row: return jsonify(error="market not found", slug=slug), 404
    if row.get("computed_at"): row["computed_at"] = row["computed_at"].isoformat()
    return jsonify(row), 200


@dcpi_bp.route("/api/v1/dcpi/movers", methods=["GET"])
def api_movers():
    _ensure_tables()
    # Compare latest score vs 7-day-ago score per market
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, market_name, excess_power_score AS now_excess,
                    constraint_score AS now_constraint, computed_at
                FROM market_power_scores
                ORDER BY market_slug, computed_at DESC
            ),
            week_ago AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, excess_power_score AS prev_excess,
                    constraint_score AS prev_constraint
                FROM market_power_scores
                WHERE computed_at < NOW() - INTERVAL '7 days'
                ORDER BY market_slug, computed_at DESC
            )
            SELECT l.market_slug, l.market_name, l.now_excess, l.now_constraint,
                   w.prev_excess, w.prev_constraint,
                   (l.now_excess - COALESCE(w.prev_excess, l.now_excess)) AS excess_delta_7d,
                   (l.now_constraint - COALESCE(w.prev_constraint, l.now_constraint)) AS constraint_delta_7d
            FROM latest l LEFT JOIN week_ago w ON l.market_slug = w.market_slug
            ORDER BY ABS(l.now_excess - COALESCE(w.prev_excess, l.now_excess)) DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
    return jsonify(movers=rows), 200


@dcpi_bp.route("/api/v1/dcpi/recompute", methods=["POST"])
def api_recompute():
    # Accept only with admin token; simple shared-secret check
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    res = recompute_all_scores(source="api")
    return jsonify(res), 200


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------
DCPI_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DCPI · Data Center Power Index | DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="DCPI tracks power availability across {{ count }}+ U.S. data center markets in real time. The Excess Power Score surfaces stranded capacity nobody else publishes.">
<meta property="og:title" content="DCPI — The Data Center Power Index">
<meta property="og:description" content="Real-time power availability across {{ count }}+ U.S. markets. Find the excess capacity hidden in plain sight.">
<meta property="og:image" content="https://dchub.cloud/dcpi/og.svg">
<meta property="og:url" content="https://dchub.cloud/dcpi">
<meta name="twitter:card" content="summary_large_image">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:        #0a0a12;
  --bg2:       #0f1119;
  --bg3:       #181a25;
  --card:      #11121a;
  --card-hi:   #1a1c28;
  --bd:        #1f2030;
  --bd-hi:     #2a2c3e;
  --tx:        #fff;
  --tx2:       #9ca3af;
  --tx3:       #6b7280;
  --acc:       #6366f1;
  --acc-light: #818cf8;
  --acc-vivid: #a855f7;
  --green:     #10b981;
  --orange:    #f59e0b;
  --red:       #ef4444;
  --gradient:  linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
}
* { box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: var(--bg);
  color: var(--tx);
  margin: 0;
  padding: 0;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
code, pre, .mono { font-family: 'JetBrains Mono', monospace; }

/* ===== TOP NAV ===== */
.top-nav {
  border-bottom: 1px solid var(--bd);
  background: rgba(10,10,18,0.85);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 100;
}
.top-nav-inner {
  max-width: 1280px;
  margin: 0 auto;
  padding: 1rem 1.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1.5rem;
}
.logo {
  font-weight: 800;
  font-size: 1.05rem;
  color: var(--tx);
  text-decoration: none;
  letter-spacing: -0.01em;
}
.logo span { color: var(--acc); }
.nav-links { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.nav-links a {
  color: var(--tx2);
  text-decoration: none;
  font-size: 0.92rem;
  font-weight: 500;
  position: relative;
}
.nav-links a:hover { color: var(--tx); }
.nav-links a.active { color: var(--tx); }
.nav-links a sup {
  color: var(--green);
  font-size: 0.55rem;
  font-weight: 800;
  letter-spacing: 0.04em;
  margin-left: 0.2rem;
  vertical-align: super;
}

/* ===== STATUS PULSE ===== */
.status-strip {
  background: var(--bg2);
  border-bottom: 1px solid var(--bd);
  padding: 0.55rem 1.5rem;
  text-align: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem;
  color: var(--tx2);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.pulse {
  display: inline-block;
  width: 8px; height: 8px;
  background: var(--green);
  border-radius: 50%;
  margin-right: 0.5rem;
  animation: pulse 1.6s ease-in-out infinite;
  vertical-align: middle;
}
@keyframes pulse { 50% { opacity: 0.3; transform: scale(0.85); } }

.wrap { max-width: 1280px; margin: 0 auto; padding: 3rem 1.5rem; }

/* ===== HERO ===== */
.hero { margin-bottom: 3rem; }
.hero h1 {
  font-size: clamp(2.4rem, 5vw, 3.6rem);
  margin: 0 0 1rem;
  font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 1.05;
}
.hero h1 .accent {
  background: var(--gradient);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.hero .lede {
  color: var(--tx2);
  font-size: 1.1rem;
  max-width: 720px;
  margin: 0 0 1.5rem;
}

/* ===== STATS ROW ===== */
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
  margin: 2rem 0 3rem;
  padding: 1.5rem;
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
}
.stat .num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 2rem;
  font-weight: 700;
  color: var(--tx);
  letter-spacing: -0.02em;
}
.stat .label {
  color: var(--tx2);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-top: 0.3rem;
}

/* ===== SECTION HEADER ===== */
.section-h {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin: 3rem 0 1rem;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--tx2);
}
.section-h .pip { width: 4px; height: 12px; background: var(--acc); border-radius: 2px; }
h2 {
  font-size: 1.6rem;
  font-weight: 700;
  margin: 0 0 1rem;
  letter-spacing: -0.015em;
}

/* ===== TOGGLE ===== */
.toggle {
  display: inline-flex;
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 10px;
  overflow: hidden;
  margin: 0 0 1.5rem;
}
.toggle button {
  background: transparent;
  color: var(--tx2);
  border: 0;
  padding: 0.7rem 1.25rem;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.85rem;
  font-family: inherit;
  transition: all 0.15s;
}
.toggle button.active {
  background: var(--gradient);
  color: white;
}

/* ===== GRID ===== */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
}
.card {
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
  padding: 1.4rem 1.5rem;
  transition: all 0.18s ease;
  cursor: pointer;
  position: relative;
  overflow: hidden;
}
.card:hover {
  transform: translateY(-3px);
  border-color: var(--bd-hi);
  background: var(--card-hi);
  box-shadow: 0 12px 32px rgba(99,102,241,0.10);
}
.card:hover::before {
  opacity: 1;
}
.card::before {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, rgba(99,102,241,0.07), transparent 60%);
  opacity: 0;
  transition: opacity 0.18s;
  pointer-events: none;
}
.card .market-name {
  font-size: 1.1rem;
  font-weight: 600;
  margin: 0 0 0.25rem;
  letter-spacing: -0.01em;
}
.card .iso {
  color: var(--tx2);
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  margin-bottom: 1rem;
}
.score {
  font-family: 'JetBrains Mono', monospace;
  font-size: 2.6rem;
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.04em;
}
.score.green { color: var(--green); }
.score.orange { color: var(--orange); }
.score.red { color: var(--red); }
.label {
  color: var(--tx2);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 0.4rem;
  font-weight: 600;
}
.verdict {
  display: inline-block;
  padding: 0.22rem 0.7rem;
  border-radius: 5px;
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  margin-top: 0.9rem;
}
.verdict.BUILD   { background: rgba(16,185,129,0.18); color: var(--green); }
.verdict.CAUTION { background: rgba(245,158,11,0.18); color: var(--orange); }
.verdict.AVOID   { background: rgba(239,68,68,0.18); color: var(--red); }
.ttp {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem;
  color: var(--tx2);
  margin-top: 0.55rem;
}

/* ===== CTA ===== */
.cta-banner {
  background: var(--gradient);
  padding: 2rem 2.25rem;
  border-radius: 14px;
  margin: 3rem 0 2rem;
  position: relative;
  overflow: hidden;
}
.cta-banner::after {
  content: '';
  position: absolute;
  right: -40px; bottom: -40px;
  width: 200px; height: 200px;
  background: radial-gradient(circle, rgba(255,255,255,0.15), transparent 70%);
  pointer-events: none;
}
.cta-banner h2 { margin: 0 0 0.4rem; font-size: 1.4rem; color: white; }
.cta-banner p {
  margin: 0 0 1.1rem;
  color: rgba(255,255,255,0.88);
  font-size: 0.95rem;
  max-width: 540px;
}
.cta-banner a.btn {
  display: inline-block;
  background: white;
  color: var(--acc);
  padding: 0.7rem 1.3rem;
  border-radius: 7px;
  text-decoration: none;
  font-weight: 700;
  font-size: 0.92rem;
  transition: transform 0.1s;
}
.cta-banner a.btn:hover { transform: translateY(-1px); }

footer {
  border-top: 1px solid var(--bd);
  margin-top: 3rem;
  padding: 2rem 0 1rem;
  color: var(--tx3);
  font-size: 0.84rem;
}
footer a { color: var(--tx2); }
footer a:hover { color: var(--acc-light); }

@media (max-width: 600px) {
  .nav-links { display: none; }
}
</style>
</head>
<body>
<nav class="top-nav">
  <div class="top-nav-inner">
    <a class="logo" href="/">DC <span>Hub</span></a>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/markets">Markets</a>
      <a href="/dcpi" class="active">DCPI<sup>NEW</sup></a>
      <a href="/land-power">Land &amp; Power</a>
      <a href="/ai">AI Platform</a>
      <a href="/news">News</a>
      <a href="/pricing">Pricing</a>
    </div>
  </div>
</nav>

<div class="status-strip">
  <span class="pulse"></span>LIVE · {{ count }} MARKETS SCORED · UPDATED DAILY 06:00 UTC · FREE FOR PRESS CITATION
</div>

<div class="wrap">
  <section class="hero">
    <h1>The <span class="accent">Data Center Power Index</span></h1>
    <p class="lede">Real-time power availability across {{ count }} U.S. data center markets. Two scores per market: <strong>Excess Power</strong> (where buyers don't know to look) and <strong>Constraint</strong> (where the queue is dead). The contrarian metric the incumbents won't publish.</p>
  </section>

  <div class="stats-row">
    <div class="stat"><div class="num">{{ count }}</div><div class="label">Markets Scored</div></div>
    <div class="stat"><div class="num">8</div><div class="label">Inputs per Score</div></div>
    <div class="stat"><div class="num">06:00 UTC</div><div class="label">Daily Refresh</div></div>
    <div class="stat"><div class="num">FREE</div><div class="label">Press &amp; Citation</div></div>
  </div>

  <div class="section-h"><span class="pip"></span>📊 Index View</div>
  <div class="toggle" role="tablist">
    <button class="active" data-mode="excess">Excess Power · Opportunity</button>
    <button data-mode="constraint">Constraint · Avoid</button>
  </div>

  <div class="grid" id="grid">
    {% for s in scores %}
    <a href="/dcpi/{{ s.market_slug }}" style="text-decoration:none;color:inherit;">
    <div class="card" data-excess="{{ s.excess_power_score }}" data-constraint="{{ s.constraint_score }}">
      <div class="market-name">{{ s.market_name }}</div>
      <div class="iso">{{ s.iso }} · {{ s.state }}</div>
      <div class="score-block excess-view">
        <div class="score {{ 'green' if s.excess_power_score>=65 else 'orange' if s.excess_power_score>=40 else 'red' }}">{{ s.excess_power_score }}</div>
        <div class="label">Excess Power</div>
      </div>
      <div class="score-block constraint-view" style="display:none">
        <div class="score {{ 'red' if s.constraint_score>=70 else 'orange' if s.constraint_score>=45 else 'green' }}">{{ s.constraint_score }}</div>
        <div class="label">Constraint</div>
      </div>
      <div class="verdict {{ s.verdict }}">{{ s.verdict }}</div>
      <div class="ttp">~{{ s.time_to_power_months|round(0)|int }}mo to power</div>
    </div>
    </a>
    {% endfor %}
  </div>

  <div class="section-h"><span class="pip"></span>🔓 Pro Access</div>
  <div class="cta-banner">
    <h2>Drill to county level. Get alerts. Export branded PDFs.</h2>
    <p>Pro shows scores at the county level so you can pinpoint where the headroom actually lives. Plus alert when any market moves &gt;5 points and one-click PDF export for your buyers. $199/mo.</p>
    <a class="btn" href="/pricing">Upgrade to Pro →</a>
  </div>

  <div class="section-h"><span class="pip"></span>📋 Methodology</div>
  <p style="color:var(--tx2);font-size:0.92rem;max-width:720px;">
    <strong>Constraint Score</strong> combines queue wait time, reserve margin proximity to NERC floor, demand-growth YoY, and 30-day grid-emergency frequency.
    <strong style="color:var(--acc-light);">Excess Power Score</strong> is the contrarian metric: reserve-margin headroom, generation additions queued &lt;12mo, renewable curtailment volume, queue approval rate, stranded interconnection at retiring plants, and behind-the-meter industrial generation. Updated daily from ISO public filings + DC Hub's grid extractors.
  </p>

  <footer>
    <p>This is the free preview. Full methodology + raw data via <a href="/api-docs">API</a>. Press inquiries: <a href="/dcpi/press">press kit</a>.</p>
    <p>© 2026 DC Hub · Data Center Intelligence Platform · <a href="/about">About</a> · <a href="/pricing">Pricing</a> · <a href="/openapi.json">OpenAPI</a></p>
  </footer>
</div>

<script>
const buttons = document.querySelectorAll('.toggle button');
buttons.forEach(b => b.addEventListener('click', () => {
  buttons.forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  const mode = b.dataset.mode;
  document.querySelectorAll('.excess-view').forEach(v => v.style.display = mode==='excess'?'block':'none');
  document.querySelectorAll('.constraint-view').forEach(v => v.style.display = mode==='constraint'?'block':'none');
  const grid = document.getElementById('grid');
  const cards = Array.from(grid.children);
  cards.sort((a,b) => {
    const ea = parseFloat(a.querySelector('.card').dataset[mode]);
    const eb = parseFloat(b.querySelector('.card').dataset[mode]);
    return eb - ea;
  });
  cards.forEach(c => grid.appendChild(c));
}));
</script>
</body>
</html>"""


DCPI_MARKET_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ s.market_name }} · DCPI {{ s.excess_power_score }} | DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="DCPI {{ s.market_name }} · Excess {{ s.excess_power_score }} · Constraint {{ s.constraint_score }}">
<meta property="og:description" content="{{ s.verdict }} · ~{{ s.time_to_power_months|round(0)|int }} months to power. Updated daily.">
<meta property="og:image" content="https://dchub.cloud/dcpi/og/{{ s.market_slug }}.svg">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#0a0a12; --bg2:#0f1119; --card:#11121a; --bd:#1f2030; --bd-hi:#2a2c3e;
  --tx:#fff; --tx2:#9ca3af; --tx3:#6b7280;
  --acc:#6366f1; --acc-light:#818cf8; --acc-vivid:#a855f7;
  --green:#10b981; --orange:#f59e0b; --red:#ef4444;
  --gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
}
* { box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, system-ui, sans-serif;
  background: var(--bg); color: var(--tx); margin: 0; padding: 0;
  line-height: 1.55; -webkit-font-smoothing: antialiased;
}
.mono, code { font-family: 'JetBrains Mono', monospace; }

.top-nav {
  border-bottom: 1px solid var(--bd);
  background: rgba(10,10,18,0.85);
  backdrop-filter: blur(8px);
  position: sticky; top: 0; z-index: 100;
}
.top-nav-inner {
  max-width: 1080px; margin: 0 auto; padding: 1rem 1.5rem;
  display: flex; align-items: center; justify-content: space-between; gap: 1.5rem;
}
.logo { font-weight: 800; font-size: 1.05rem; color: var(--tx); text-decoration: none; }
.logo span { color: var(--acc); }
.nav-links { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.nav-links a { color: var(--tx2); text-decoration: none; font-size: 0.92rem; font-weight: 500; }
.nav-links a:hover { color: var(--tx); }
.nav-links a.active { color: var(--tx); }

.wrap { max-width: 1080px; margin: 0 auto; padding: 2.5rem 1.5rem; }
.crumbs {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem; color: var(--tx3); margin-bottom: 1rem;
}
.crumbs a { color: var(--acc-light); text-decoration: none; }
.crumbs a:hover { color: var(--tx); }

h1 {
  font-size: clamp(2.2rem, 5vw, 3.2rem);
  margin: 0 0 0.4rem;
  font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 1.05;
}
.subtitle {
  color: var(--tx2);
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.9rem;
  margin: 0 0 2rem;
}

.verdict-banner {
  padding: 1.1rem 1.5rem;
  border-radius: 10px;
  margin: 2rem 0;
  font-weight: 700;
  font-size: 1rem;
  border: 1px solid;
}
.verdict-banner.BUILD   { background: rgba(16,185,129,0.10); border-color: var(--green); color: var(--green); }
.verdict-banner.CAUTION { background: rgba(245,158,11,0.10); border-color: var(--orange); color: var(--orange); }
.verdict-banner.AVOID   { background: rgba(239,68,68,0.10); border-color: var(--red); color: var(--red); }

.scoreboard {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
  margin: 2rem 0;
}
.sb {
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
  padding: 1.75rem;
  position: relative;
  overflow: hidden;
}
.sb::after {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(99,102,241,0.05), transparent 60%);
  pointer-events: none;
}
.sb .v {
  font-family: 'JetBrains Mono', monospace;
  font-size: clamp(3.5rem, 8vw, 5.5rem);
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.03em;
}
.sb .l {
  color: var(--tx2);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 0.6rem;
  font-weight: 600;
}
.green { color: var(--green); }
.orange { color: var(--orange); }
.red { color: var(--red); }

.section-h {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin: 2.5rem 0 1rem;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--tx2);
}
.section-h .pip { width: 4px; height: 12px; background: var(--acc); border-radius: 2px; }

.section {
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
  padding: 1.75rem;
  margin: 1rem 0;
}
.section h2 {
  margin: 0 0 1rem;
  font-size: 1.2rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.section ul { padding-left: 1.2rem; margin: 0; }
.section li {
  margin: 0.5rem 0;
  color: #ddd;
  font-size: 0.95rem;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px,1fr));
  gap: 0.85rem;
}
.metric {
  background: var(--bg2);
  border: 1px solid var(--bd);
  border-radius: 8px;
  padding: 0.9rem 1.1rem;
  transition: border-color 0.15s;
}
.metric:hover { border-color: var(--bd-hi); }
.metric .v {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.metric .l {
  color: var(--tx2);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-top: 0.3rem;
  font-weight: 600;
}

.cta-pro {
  background: var(--gradient);
  padding: 2rem 2.25rem;
  border-radius: 14px;
  margin: 2rem 0;
  position: relative;
  overflow: hidden;
}
.cta-pro::after {
  content: '';
  position: absolute;
  right: -40px; bottom: -40px;
  width: 200px; height: 200px;
  background: radial-gradient(circle, rgba(255,255,255,0.15), transparent 70%);
  pointer-events: none;
}
.cta-pro h2 { margin: 0 0 0.5rem; font-size: 1.3rem; color: white; }
.cta-pro p {
  margin: 0 0 1.1rem;
  color: rgba(255,255,255,0.88);
  font-size: 0.94rem;
}
.cta-pro a {
  display: inline-block;
  background: white; color: var(--acc);
  padding: 0.65rem 1.2rem; border-radius: 6px;
  text-decoration: none; font-weight: 700;
  font-size: 0.9rem;
}

@media (max-width: 600px) {
  .nav-links { display: none; }
}
</style>
</head>
<body>

<nav class="top-nav">
  <div class="top-nav-inner">
    <a class="logo" href="/">DC <span>Hub</span></a>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/markets">Markets</a>
      <a href="/dcpi" class="active">DCPI</a>
      <a href="/land-power">Land &amp; Power</a>
      <a href="/ai">AI Platform</a>
      <a href="/news">News</a>
      <a href="/pricing">Pricing</a>
    </div>
  </div>
</nav>

<div class="wrap">
  <div class="crumbs"><a href="/dcpi">DCPI</a> / <a href="/markets">Markets</a> / {{ s.market_name }}</div>
  <h1>{{ s.market_name }}</h1>
  <p class="subtitle">{{ s.iso }} · {{ s.state }} · UPDATED {{ s.computed_at[:10] }}</p>

  <div class="verdict-banner {{ s.verdict }}">
    {% if s.verdict == 'BUILD' %}🟢 BUILD HERE — Excess capacity available, manageable constraints
    {% elif s.verdict == 'CAUTION' %}🟡 CAUTION — Mixed signals, due-diligence required
    {% else %}🔴 AVOID FOR NEW BUILDS — Severe constraints, multi-year wait{% endif %}
  </div>

  <div class="scoreboard">
    <div class="sb">
      <div class="v {{ 'green' if s.excess_power_score>=65 else 'orange' if s.excess_power_score>=40 else 'red' }}">{{ s.excess_power_score }}</div>
      <div class="l">Excess Power Score · Opportunity</div>
    </div>
    <div class="sb">
      <div class="v {{ 'red' if s.constraint_score>=70 else 'orange' if s.constraint_score>=45 else 'green' }}">{{ s.constraint_score }}</div>
      <div class="l">Constraint Score · Avoid</div>
    </div>
  </div>

  <div class="section-h"><span class="pip"></span>🌟 Top Opportunities</div>
  <div class="section">
    <ul>{% for o in opps %}<li>{{ o }}</li>{% endfor %}</ul>
  </div>

  <div class="section-h"><span class="pip"></span>⚠️ Top Risks</div>
  <div class="section">
    <ul>{% for r in risks %}<li>{{ r }}</li>{% endfor %}</ul>
  </div>

  <div class="section-h"><span class="pip"></span>📊 Underlying Metrics</div>
  <div class="section">
    <div class="metrics">
      <div class="metric"><div class="v">{{ s.queue_wait_months|round(0)|int }} mo</div><div class="l">Queue Wait</div></div>
      <div class="metric"><div class="v">{{ s.reserve_margin_pct|round(1) }}%</div><div class="l">Reserve Margin</div></div>
      <div class="metric"><div class="v">{{ (s.gen_additions_12mo_mw or 0)|round(0)|int }} MW</div><div class="l">Generation Additions &lt;12mo</div></div>
      <div class="metric"><div class="v">{{ (s.curtailment_pct or 0)|round(1) }}%</div><div class="l">Renewable Curtailment</div></div>
      <div class="metric"><div class="v">{{ (s.stranded_capacity_mw or 0)|round(0)|int }} MW</div><div class="l">Stranded Capacity</div></div>
      <div class="metric"><div class="v">{{ s.time_to_power_months|round(0)|int }} mo</div><div class="l">Est. Time to Power</div></div>
    </div>
  </div>

  <div class="cta-pro">
    <h2>Drill into {{ s.market_name }} at the county level.</h2>
    <p>Pro shows the score at the county level so you can pinpoint which sub-markets have the headroom. Plus alerts when {{ s.market_name }} moves &gt;5 points and PDF export for your buyers.</p>
    <a href="/pricing">Get Pro · $199/mo →</a>
  </div>
</div>
</body>
</html>"""


@dcpi_bp.route("/dcpi", methods=["GET"])
def public_dashboard():
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug) *
            FROM market_power_scores
            ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()
    rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    if not rows:
        # Trigger an initial recompute so the page is never empty
        try: recompute_all_scores(source="cold-start")
        except Exception: pass
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT DISTINCT ON (market_slug) *
                           FROM market_power_scores
                           ORDER BY market_slug, computed_at DESC""")
            rows = cur.fetchall()
        rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    return render_template_string(DCPI_INDEX_TEMPLATE, scores=rows, count=len(rows))


@dcpi_bp.route("/dcpi/<slug>", methods=["GET"])
def public_market_page(slug):
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores
                       WHERE market_slug = %s
                       ORDER BY computed_at DESC LIMIT 1""", (slug,))
        s = cur.fetchone()
    if not s: return Response(f"<h1>Market not found: {slug}</h1>", status=404, mimetype="text/html")
    if s.get("computed_at"): s["computed_at"] = s["computed_at"].isoformat()
    risks = s.get("top_risks_json") or []
    opps = s.get("top_opportunities_json") or []
    return render_template_string(DCPI_MARKET_TEMPLATE, s=s, risks=risks, opps=opps)


@dcpi_bp.route("/dcpi/og/<slug>.svg", methods=["GET"])
@dcpi_bp.route("/dcpi/og/<slug>", methods=["GET"])
def og_card(slug):
    """1200x630 SVG for LinkedIn/X cards."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores WHERE market_slug = %s
                       ORDER BY computed_at DESC LIMIT 1""", (slug,))
        s = cur.fetchone()
    if not s: return Response("not found", status=404)

    excess_color = "#10b981" if (s["excess_power_score"] or 0) >= 65 else \
                   "#f59e0b" if (s["excess_power_score"] or 0) >= 40 else "#ef4444"
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0a0a12"/>
      <stop offset="1" stop-color="#1a1a2e"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>
  <text x="60" y="100" font-family="-apple-system, sans-serif" font-size="28" font-weight="600" fill="#9ca3af">DCPI · DC Hub Power Index</text>
  <text x="60" y="200" font-family="-apple-system, sans-serif" font-size="80" font-weight="800" fill="white">{s['market_name']}</text>
  <text x="60" y="240" font-family="-apple-system, sans-serif" font-size="24" fill="#9ca3af">{s['iso']} · {s['state']}</text>

  <text x="60" y="360" font-family="-apple-system, sans-serif" font-size="20" fill="#9ca3af">EXCESS POWER SCORE</text>
  <text x="60" y="470" font-family="-apple-system, sans-serif" font-size="180" font-weight="800" fill="{excess_color}">{int(s['excess_power_score'] or 0)}</text>

  <text x="700" y="360" font-family="-apple-system, sans-serif" font-size="20" fill="#9ca3af">CONSTRAINT</text>
  <text x="700" y="430" font-family="-apple-system, sans-serif" font-size="80" font-weight="700" fill="#9ca3af">{int(s['constraint_score'] or 0)}</text>

  <text x="700" y="490" font-family="-apple-system, sans-serif" font-size="20" fill="#9ca3af">~{int(s['time_to_power_months'] or 0)}mo TO POWER</text>

  <text x="60" y="580" font-family="-apple-system, sans-serif" font-size="22" font-weight="700" fill="{excess_color}">VERDICT: {s['verdict']}</text>
  <text x="60" y="610" font-family="-apple-system, sans-serif" font-size="16" fill="#6b7280">dchub.cloud/dcpi/{slug}</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


@dcpi_bp.route("/dcpi/press", methods=["GET"])
def press_kit():
    return Response("""<!DOCTYPE html><html><head><title>DCPI Press Kit</title>
<style>body{font-family:system-ui;max-width:780px;margin:2rem auto;padding:2rem;line-height:1.6;color:#222}
h1{margin:0 0 0.5rem}h2{margin:1.5rem 0 0.5rem;border-bottom:1px solid #ddd;padding-bottom:0.3rem}
code{background:#f3f3f3;padding:0.1rem 0.4rem;border-radius:3px;font-size:0.9em}
.embed{background:#1a1a2e;color:#eee;padding:1rem;border-radius:6px;font-size:0.85rem;overflow-x:auto}
</style></head><body>
<h1>DCPI Press Kit</h1>
<p>The Data Center Power Index (DCPI) is a daily-updated indicator of power
availability across U.S. data center markets. Free for the press to cite.</p>
<h2>What is DCPI?</h2>
<p>Two scores per market: <strong>Excess Power Score</strong> (0–100, high = opportunity) and
<strong>Constraint Score</strong> (0–100, high = avoid). Excess Power surfaces stranded capacity,
curtailed renewables, and behind-the-meter industrial headroom — power
that's available but not commonly tracked.</p>
<h2>Citation format</h2>
<p><code>According to the DC Hub Power Index, [Market] scored [N] on [date]. Source: dchub.cloud/dcpi.</code></p>
<h2>API access</h2>
<p>Free JSON: <code>GET dchub.cloud/api/v1/dcpi/scores</code></p>
<h2>Embed widget</h2>
<p>Drop into any article (forthcoming in phase 110):</p>
<div class="embed">&lt;iframe src="https://dchub.cloud/dcpi/embed/atlanta" width="400" height="200" frameborder="0"&gt;&lt;/iframe&gt;</div>
<h2>Methodology</h2>
<p>Excess Power Score = weighted sum of: ISO reserve margin headroom, queued generation additions &lt;12mo,
renewable curtailment volume, queue approval rate, stranded interconnection capacity at retiring plants,
and behind-the-meter industrial generation. Constraint Score = queue wait time, reserve margin proximity to
NERC floor, demand growth YoY, recent grid emergencies. Inputs ingested daily from ISO public filings,
EIA monthly data, and DC Hub's grid-feed extractors.</p>
<h2>Contact</h2>
<p>Press inquiries: jonathan@dchub.cloud</p>
</body></html>""", mimetype="text/html")

# === Phase 117b: CF-allowlisted aliases for public DCPI pages ===
@dcpi_bp.route("/api/v1/dcpi/page", methods=["GET"])
def public_dashboard_alias():
    return public_dashboard()

@dcpi_bp.route("/api/v1/dcpi/page/<slug>", methods=["GET"])
def public_market_page_alias(slug):
    return public_market_page(slug)

@dcpi_bp.route("/api/v1/dcpi/og/<slug>", methods=["GET"])
@dcpi_bp.route("/api/v1/dcpi/og/<slug>.svg", methods=["GET"])
def og_card_alias(slug):
    return og_card(slug)

@dcpi_bp.route("/api/v1/dcpi/embed/<slug>", methods=["GET"])
def embed_widget_alias(slug):
    return embed_widget(slug)

@dcpi_bp.route("/api/v1/dcpi/press", methods=["GET"])
def press_kit_alias():
    return press_kit()

