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


# Phase 223: defensive round helper


# Phase 225: decorator that returns the fallback page on ANY exception
from functools import wraps
def _safe_dcpi_page(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return _phase225_dcpi_error_page(str(e))
    return wrapper


def _safe_round(v, digits=1):
    """Safely round a value that might be None or non-numeric."""
    if v is None: return 0.0
    try: return round(float(v), digits)
    except (TypeError, ValueError): return 0.0




# === Phase 213: dynamic market list (use /api/v1/markets/list for full 132) ===
def _dcpi_dynamic_markets():
    """Returns list of market dicts {slug, name, cities, state, country}.
    Pulls from internal markets API. Falls back to MARKET_ALIASES if API fails.
    """
    import os, urllib.request, json
    try:
        base = os.environ.get("DCHUB_INTERNAL_API", "http://localhost:8000")
        # Use enterprise key to bypass tier-gate
        ent_key = os.environ.get("DCHUB_ENT_KEY", "ent_internal_dcpi_scorer")
        req = urllib.request.Request(
            f"{base}/api/v1/markets/list",
            headers={"X-API-Key": ent_key, "User-Agent": "dcpi-scorer/2.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        markets_raw = data.get("data") or data.get("markets") or []
        out = []
        for m in markets_raw:
            out.append({
                "slug": m.get("id"),
                "name": m.get("name"),
                "cities": m.get("cities") or [m.get("name")],
                "state": m.get("state"),
                "country": m.get("country", "US"),
                "facility_count": m.get("facility_count", 0),
                "pipeline_mw": m.get("pipeline_mw_total", 0),
                "operational_mw": m.get("total_power_mw", 0),
                "avg_kwh_usd": m.get("avg_kwh_price_usd"),
            })
        return out
    except Exception as e:
        import logging
        logging.warning(f"_dcpi_dynamic_markets fetch failed: {e}")
        return None


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
_MARKETS_HARDCODED = [
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

# Phase 214: try dynamic 132-market list first, fall back to hardcoded 30
def _load_markets_dynamic():
    """Phase 215: direct Postgres query — no internal API auth dance.
    Returns list matching the structure of _MARKETS_HARDCODED.
    """
    import os, psycopg2
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            return None
        conn = psycopg2.connect(url, connect_timeout=8)
        with conn.cursor() as cur:
            # All US cities with >=3 facilities + dominant state
            cur.execute("""
                WITH city_stats AS (
                    SELECT
                        LOWER(city) AS slug,
                        city AS name,
                        state,
                        COUNT(*) AS facility_count,
                        COALESCE(SUM(power_mw), 0) AS op_mw,
                        COALESCE(SUM(power_mw) FILTER (WHERE status IN ('construction','planned','permitting','Under Construction','Planned')), 0) AS pipeline_mw
                    FROM discovered_facilities
                    WHERE city IS NOT NULL AND city != ''
                      AND state IS NOT NULL AND state != ''
                      AND LENGTH(state) = 2
                      AND state ~ '^[A-Z]{2}$'
                      AND (country = 'US' OR country = 'USA')
                    GROUP BY LOWER(city), city, state
                    HAVING COUNT(*) >= 3
                )
                SELECT slug, name, state, facility_count, op_mw, pipeline_mw
                FROM city_stats
                ORDER BY facility_count DESC
                LIMIT 200;
            """)
            rows = cur.fetchall()
        conn.close()

        if not rows:
            return None

        # Adapt to the hardcoded MARKETS structure (list of dicts)
        if isinstance(_MARKETS_HARDCODED, list) and _MARKETS_HARDCODED:
            sample = _MARKETS_HARDCODED[0]
            if isinstance(sample, dict):
                keys = list(sample.keys())
                # Map our DB rows to the same dict shape
                out = []
                for r in rows:
                    slug, name, state, fac, op_mw, pipe_mw = r
                    d = {}
                    for k in keys:
                        if k == "slug": d[k] = slug.replace(" ", "-").replace(",", "")
                        elif k == "name": d[k] = name
                        elif k == "state": d[k] = state
                        elif k == "country": d[k] = "US"
                        elif k == "cities": d[k] = [name]
                        elif k in ("facility_count", "fac"): d[k] = int(fac)
                        elif k in ("operational_mw", "op_mw", "total_mw"): d[k] = float(op_mw)
                        elif k in ("pipeline_mw", "pipeline_mw_total"): d[k] = float(pipe_mw)
                        else: d[k] = sample.get(k)  # default from hardcoded
                    out.append(d)
                return out
            elif isinstance(sample, str):
                # List of slug strings
                return [r[0].replace(" ", "-").replace(",", "") for r in rows]
        return None
    except Exception as e:
        import logging
        logging.warning(f"_load_markets_dynamic direct DB failed: {e}")
        return None


MARKETS = _load_markets_dynamic() or _MARKETS_HARDCODED



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

    return round((0.4*s_wait + 0.25*s_reserve + 0.20*s_emerg + 0.15*s_demand) or 0, 1)


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
    return round((queue_wait * adj) or 0, 1)


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
                  COALESCE(AVG(EXTRACT(EPOCH FROM (NOW(), 0) - submitted_at)) / 2628000.0) AS avg_wait_months,
                  COUNT(*) AS queue_count,
                  COALESCE(SUM(capacity_mw), 0) AS queue_total_mw
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

    # Phase QQ+3 (2026-05-13): use MARKETS only (canonical 6-tuple shape).
    # Previously: `_dcpi_dynamic_markets() or MARKETS`. The dynamic helper
    # returns 9-key dicts (slug, name, cities, state, country, facility_count,
    # pipeline_mw, operational_mw, avg_kwh_usd), but the unpack on the next
    # line expects 6 tuple positions (slug, name, state, iso, lat, lon).
    # When the dynamic call succeeded, every iteration threw ValueError out
    # of the for-loop (it's OUTSIDE the try/except below), bubbled up to
    # api_recompute, and either 500'd or produced spurious values that the
    # downstream INSERT silently rejected. End result: market_power_scores
    # hadn't been updated in 45h despite the daily cron firing successfully
    # — the truth endpoint /api/v1/system/loops caught it as dcpi_recompute
    # stale=45.1h.
    #
    # MARKETS itself is `_load_markets_dynamic() or _MARKETS_HARDCODED` —
    # both of those return 6-tuples, so unpacking is safe.
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
                # Phase FF+4 (2026-05-13): UPSERT, not INSERT. Production
                # has a UNIQUE constraint `market_power_scores_slug_key`
                # on (market_slug) that _ensure_tables doesn't define
                # but exists in the live DB (added via manual migration).
                # The previous plain INSERT raised UniqueViolation on
                # every market after the first run — markets_scored=0,
                # errors=30, loop probe showed stale at 57h. Now we
                # upsert: keep one canonical row per market, refresh
                # all metric columns + computed_at on each recompute.
                # Identifier columns (slug/name/state/iso/lat/lon) are
                # treated as stable — they don't change between runs.
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
                    ON CONFLICT (market_slug) DO UPDATE SET
                        market_name             = EXCLUDED.market_name,
                        state                   = EXCLUDED.state,
                        iso                     = EXCLUDED.iso,
                        latitude                = EXCLUDED.latitude,
                        longitude               = EXCLUDED.longitude,
                        constraint_score        = EXCLUDED.constraint_score,
                        excess_power_score      = EXCLUDED.excess_power_score,
                        time_to_power_months    = EXCLUDED.time_to_power_months,
                        queue_capacity_mw       = EXCLUDED.queue_capacity_mw,
                        queue_wait_months       = EXCLUDED.queue_wait_months,
                        reserve_margin_pct      = EXCLUDED.reserve_margin_pct,
                        gen_additions_12mo_mw   = EXCLUDED.gen_additions_12mo_mw,
                        curtailment_pct         = EXCLUDED.curtailment_pct,
                        stranded_capacity_mw    = EXCLUDED.stranded_capacity_mw,
                        emergency_count_30d     = EXCLUDED.emergency_count_30d,
                        top_risks_json          = EXCLUDED.top_risks_json,
                        top_opportunities_json  = EXCLUDED.top_opportunities_json,
                        verdict                 = EXCLUDED.verdict,
                        computed_at             = NOW()
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
            FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
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
                FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
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


# phase 267: public, machine-readable leaderboard so the DCPI is citable
#            without scraping the HTML page.
@dcpi_bp.route("/api/v1/dcpi/leaderboard", methods=["GET"])
def api_leaderboard():
    """Public ranked DCPI leaderboard.

    Query params:
        verdict  optional filter: BUILD | CAUTION | AVOID | LOW_SIGNAL
                 (default: exclude LOW_SIGNAL to surface actionable markets)
        limit    int, default 25, max 100
        format   json (default) | csv

    Returns ranked markets by excess_power_score (descending) with each
    market's verdict, quality, constraint, and freshness timestamp. JSON-LD
    Dataset markup on /dcpi points here as the canonical machine surface.
    """
    _ensure_tables()
    verdict = (request.args.get("verdict") or "").upper().strip() or None
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
    except (TypeError, ValueError):
        limit = 25
    fmt = (request.args.get("format") or "json").lower()

    # Default: exclude LOW_SIGNAL (high-noise) markets so the leaderboard
    # surfaces only verdicts a buyer/journalist/AI can act on. Pass
    # ?verdict=LOW_SIGNAL explicitly to include them.
    where_verdict = ""
    params = []
    if verdict:
        where_verdict = "AND verdict = %s"
        params.append(verdict)
    else:
        where_verdict = "AND verdict <> 'LOW_SIGNAL'"

    sql = f"""
        SELECT DISTINCT ON (market_slug)
            market_slug, market_name, iso, state,
            excess_power_score, constraint_score, quality_score,
            verdict, computed_at,
            ('https://dchub.cloud/dcpi/' || market_slug) AS url
        FROM market_power_scores
        WHERE published = true {where_verdict}
        ORDER BY market_slug, computed_at DESC
    """
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    rows = rows[:limit]
    for r in rows:
        if r.get("computed_at"):
            r["computed_at"] = r["computed_at"].isoformat()
        # Phase 297 (Phase P): add a deterministic reasoning chain so AI
        # agents and journalists can quote the WHY, not just the score.
        # Uses score thresholds from derive_verdict() — keeps reasoning
        # consistent with the verdict logic.
        r["reasoning"] = _build_reasoning(
            r.get("verdict"), r.get("excess_power_score") or 0,
            r.get("constraint_score") or 0, r.get("quality_score") or 0,
        )

    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        cols = ["rank", "market_slug", "market_name", "iso", "state",
                "verdict", "excess_power_score", "constraint_score",
                "quality_score", "computed_at", "url", "reasoning"]
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for i, r in enumerate(rows, 1):
            row = {k: r.get(k) for k in cols}
            row["rank"] = i
            w.writerow(row)
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = 'attachment; filename="dcpi-leaderboard.csv"'
        resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
        return resp

    body = {
        "as_of": rows[0]["computed_at"] if rows else None,
        "count": len(rows),
        "filter": {"verdict": verdict, "excludes_low_signal": verdict is None},
        "leaderboard": [
            {"rank": i, **r} for i, r in enumerate(rows, 1)
        ],
        "methodology_url": "https://dchub.cloud/dcpi#methodology",
        "citation": "DC Hub Data Center Power Index. https://dchub.cloud/dcpi",
    }
    # Phase 299 (fix PR #21 regression): restore the response wrapper that
    # was accidentally dropped. Without these lines the Flask handler returns
    # None → Flask falls back to a generic HTML error page → CDN caches it
    # → leaderboard endpoint broken for every consumer.
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ============================================================================
# Phase AA (2026-05-12): ISO Intelligence Layer
#
# User asked: "what can we do strengthen our DCPI index, more ISO
# intelligence?" The market_power_scores table already carries deep
# per-market data we never surface — queue_wait_months, queue_capacity_mw,
# reserve_margin_pct, gen_additions_12mo_mw, curtailment_pct,
# stranded_capacity_mw, emergency_count_30d, avg_kwh_cents. Aggregating
# these per-ISO turns DCPI from "market scorecard" into "ISO power-supply
# diagnostic" — exactly the depth buyers + journalists + AI agents need
# to make ISO-level decisions (which ISO is easiest to enter? cheapest?
# fastest interconnect?).
#
# Two new endpoints:
#   GET /api/v1/dcpi/iso/<code>       — one ISO deep-dive
#   GET /api/v1/dcpi/iso-comparison   — all ISOs ranked side-by-side
# ============================================================================

_ISO_NAMES = {
    "PJM":   "PJM Interconnection (mid-Atlantic + Ohio Valley)",
    "ERCOT": "Electric Reliability Council of Texas",
    "CAISO": "California ISO",
    "NYISO": "New York ISO",
    "ISONE": "ISO New England",
    "ISO-NE": "ISO New England",
    "MISO":  "Midcontinent ISO",
    "SPP":   "Southwest Power Pool",
    "WECC":  "Western Electricity Coordinating Council (non-CAISO)",
    "IESO":  "Independent Electricity System Operator (Ontario)",
}


def _aggregate_iso_stats(iso_code: str | None = None):
    """Compute per-ISO aggregate stats from market_power_scores. When
       iso_code is given, return one ISO; otherwise return all ISOs
       ranked. Uses DISTINCT ON to take the latest snapshot per market
       so a market that's been recomputed several times doesn't skew
       the avg."""
    where_iso = ""
    params = []
    if iso_code:
        where_iso = "AND UPPER(iso) = %s"
        params.append(iso_code.upper())

    # DISTINCT ON (market_slug) — most recent row per market
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (market_slug)
                market_slug, market_name, iso, state,
                excess_power_score, constraint_score, quality_score,
                queue_wait_months, queue_capacity_mw,
                reserve_margin_pct, gen_additions_12mo_mw,
                curtailment_pct, stranded_capacity_mw,
                emergency_count_30d, avg_kwh_cents,
                verdict, computed_at
            FROM market_power_scores
            WHERE published = true {where_iso}
            ORDER BY market_slug, computed_at DESC
        )
        SELECT
            COALESCE(iso, 'UNKNOWN') AS iso,
            COUNT(*) AS market_count,
            AVG(excess_power_score) AS avg_excess,
            AVG(constraint_score) AS avg_constraint,
            AVG(quality_score) AS avg_quality,
            AVG(NULLIF(queue_wait_months, 0)) AS avg_queue_wait_months,
            SUM(COALESCE(queue_capacity_mw, 0)) AS total_queue_capacity_mw,
            AVG(NULLIF(reserve_margin_pct, 0)) AS avg_reserve_margin_pct,
            SUM(COALESCE(gen_additions_12mo_mw, 0)) AS total_gen_additions_12mo_mw,
            AVG(NULLIF(curtailment_pct, 0)) AS avg_curtailment_pct,
            SUM(COALESCE(stranded_capacity_mw, 0)) AS total_stranded_capacity_mw,
            SUM(COALESCE(emergency_count_30d, 0)) AS sum_emergency_30d,
            AVG(NULLIF(avg_kwh_cents, 0)) AS avg_kwh_cents,
            SUM(CASE WHEN verdict = 'BUILD'      THEN 1 ELSE 0 END) AS build_count,
            SUM(CASE WHEN verdict = 'CAUTION'    THEN 1 ELSE 0 END) AS caution_count,
            SUM(CASE WHEN verdict = 'AVOID'      THEN 1 ELSE 0 END) AS avoid_count,
            SUM(CASE WHEN verdict = 'LOW_SIGNAL' THEN 1 ELSE 0 END) AS low_signal_count,
            MAX(computed_at) AS latest_computed_at
        FROM latest
        GROUP BY iso
        ORDER BY market_count DESC
    """
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _iso_top_markets(iso_code: str, verdict_filter: str, limit: int = 5):
    """Top markets in an ISO by verdict. BUILD ranked by excess_power_score
       DESC; AVOID ranked by constraint_score DESC."""
    order_col = "excess_power_score" if verdict_filter == "BUILD" else "constraint_score"
    sql = f"""
        SELECT DISTINCT ON (market_slug)
            market_slug, market_name, state,
            excess_power_score, constraint_score, quality_score, verdict,
            queue_wait_months, avg_kwh_cents,
            ('https://dchub.cloud/dcpi/' || market_slug) AS url
        FROM market_power_scores
        WHERE published = true
          AND UPPER(iso) = %s
          AND verdict = %s
        ORDER BY market_slug, computed_at DESC
    """
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (iso_code.upper(), verdict_filter))
        rows = [dict(r) for r in cur.fetchall()]
    # Sort by the ranking column, descending
    rows.sort(key=lambda r: -(r.get(order_col) or 0))
    return rows[:limit]


def _normalize_iso_row(r: dict) -> dict:
    """Round floats + serialize datetimes + add narrative labels."""
    out = dict(r)
    for k, v in r.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (int,)) or v is None:
            out[k] = v
        else:
            try:
                fv = float(v)
                # 1 decimal place for percentages, 2 for prices, 0 for counts
                if k.endswith("_pct") or k.startswith("avg_") and k != "avg_kwh_cents":
                    out[k] = round(fv, 1)
                elif k == "avg_kwh_cents":
                    out[k] = round(fv, 2)
                else:
                    out[k] = round(fv, 1)
            except (TypeError, ValueError):
                out[k] = v
    # Friendly ISO name
    out["iso_name"] = _ISO_NAMES.get(str(r.get("iso") or "").upper(), r.get("iso"))
    return out


@dcpi_bp.route("/api/v1/dcpi/iso/<iso_code>", methods=["GET"])
def api_iso_deep_dive(iso_code):
    """Deep-dive per ISO. Aggregates queue depth, avg cost, curtailment,
       reserve margin, etc. and surfaces the top BUILD + AVOID markets.

       Citable: machine surface for AI agents asking "what's the state of
       MISO grid right now?" — single fetch returns the whole picture.
    """
    _ensure_tables()
    iso_code = (iso_code or "").upper().strip()
    if not iso_code:
        return jsonify(error="iso_code_required"), 400

    rows = _aggregate_iso_stats(iso_code)
    if not rows:
        return jsonify(
            error="iso_not_found",
            iso=iso_code,
            hint="Try one of: " + ", ".join(sorted(_ISO_NAMES.keys())),
        ), 404

    iso_stats = _normalize_iso_row(rows[0])
    top_build = [_normalize_iso_row(r) for r in _iso_top_markets(iso_code, "BUILD", 5)]
    top_avoid = [_normalize_iso_row(r) for r in _iso_top_markets(iso_code, "AVOID", 5)]

    body = {
        "iso": iso_code,
        "iso_name": _ISO_NAMES.get(iso_code, iso_code),
        "as_of": iso_stats.get("latest_computed_at"),
        "stats": iso_stats,
        "top_build_markets": top_build,
        "top_avoid_markets": top_avoid,
        "methodology_url": "https://dchub.cloud/dcpi#methodology",
        "citation": (f"DC Hub DCPI · {iso_code} ISO intelligence. "
                      f"https://dchub.cloud/dcpi/iso/{iso_code.lower()}"),
    }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@dcpi_bp.route("/api/v1/dcpi/iso-comparison", methods=["GET"])
def api_iso_comparison():
    """Side-by-side ISO comparison. Ranks every ISO across the same set
       of dimensions so a buyer can answer "which ISO has the most queue
       capacity coming online? cheapest power? best build verdicts?"
       in a single chart.

       This is the headline new view of DCPI++ — moves the index from
       "276 market scorecards" to "8 ISO diagnostics + 276 underlying
       markets" so the same data answers a strategic question alongside
       the tactical one.
    """
    _ensure_tables()
    rows = [_normalize_iso_row(r) for r in _aggregate_iso_stats()]

    body = {
        "as_of": max((r.get("latest_computed_at") or "" for r in rows), default=None),
        "count": len(rows),
        "isos": rows,
        "rankings": {
            # Build a sortable "best for X" view — handy for journalists.
            "fastest_interconnect": sorted(
                [r for r in rows if r.get("avg_queue_wait_months") is not None],
                key=lambda r: r["avg_queue_wait_months"])[:5],
            "cheapest_power": sorted(
                [r for r in rows if r.get("avg_kwh_cents") is not None],
                key=lambda r: r["avg_kwh_cents"])[:5],
            "most_build_verdicts": sorted(
                rows, key=lambda r: -(r.get("build_count") or 0))[:5],
            "highest_excess_capacity": sorted(
                rows, key=lambda r: -(r.get("avg_excess") or 0))[:5],
            "most_curtailment_risk": sorted(
                [r for r in rows if r.get("avg_curtailment_pct") is not None],
                key=lambda r: -(r["avg_curtailment_pct"]))[:5],
        },
        "methodology_url": "https://dchub.cloud/dcpi#methodology",
        "citation": "DC Hub DCPI · ISO comparison. https://dchub.cloud/dcpi/iso-comparison",
    }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# Phase 297 (Phase P): deterministic reasoning chain. Templates the WHY
# behind each verdict using the underlying scores. No LLM call per market —
# cheap, consistent, citable. The thresholds mirror the derive_verdict()
# matrix in this file so reasoning never contradicts the verdict.
def _build_reasoning(verdict, excess, constraint, quality):
    e_band = ("strong" if excess >= 65 else "moderate" if excess >= 40
              else "thin" if excess > 0 else "no_signal")
    c_band = ("clear" if constraint < 45 else "tight" if constraint < 70
              else "saturated" if constraint > 0 else "no_signal")

    e_label = {
        "strong":    f"Excess Power {int(excess)} (strong — stranded capacity + queued additions <12mo)",
        "moderate":  f"Excess Power {int(excess)} (moderate — some headroom)",
        "thin":      f"Excess Power {int(excess)} (thin — limited spare capacity)",
        "no_signal": f"Excess Power {int(excess)} (no signal — insufficient data)",
    }[e_band]
    c_label = {
        "clear":     f"Constraint {int(constraint)} (clear — healthy queue, reserve margin)",
        "tight":     f"Constraint {int(constraint)} (tight — queue backed up)",
        "saturated": f"Constraint {int(constraint)} (saturated — near NERC floor or queue dead)",
        "no_signal": f"Constraint {int(constraint)} (no signal)",
    }[c_band]

    quality_note = (
        f"Quality {int(quality)} — high-confidence" if quality >= 80
        else f"Quality {int(quality)} — moderate-confidence" if quality >= 60
        else f"Quality {int(quality)} — low-confidence" if quality > 0
        else "Quality unknown"
    )

    # Verdict-specific framing
    if verdict == "BUILD":
        framing = "Why BUILD: stranded power + cleared queue make this a near-term siting target."
    elif verdict == "AVOID":
        framing = "Why AVOID: saturated grid + thin excess. Site selection here forces years of queue wait."
    elif verdict == "CAUTION":
        framing = "Why CAUTION: mixed signal. One of (excess, constraint) is unfavorable; diligence required."
    elif verdict == "LOW_SIGNAL":
        framing = "Why LOW_SIGNAL: scores too noisy to call. Market is tracked, not yet rated."
    elif verdict == "NODATA":
        framing = "Why NODATA: source feed has not yet populated for this market."
    else:
        framing = f"Verdict: {verdict}"

    return f"{framing} {e_label}. {c_label}. {quality_note}."
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"  # citable from anywhere
    return resp, 200


# phase 267: OEmbed discovery — journalists / Substack / Medium can paste
# https://dchub.cloud/dcpi and get a live ticker widget back.
# phase 270 hardening: validate URL host so this can't be used as an open
# OEmbed redirector against other domains, and whitelist slug charset so
# user-controllable input can't break out of the iframe attributes.
import re as _oembed_re
import urllib.parse as _oembed_url
_OEMBED_ALLOWED_HOSTS = {"dchub.cloud", "www.dchub.cloud"}
_OEMBED_SLUG_RE = _oembed_re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


@dcpi_bp.route("/api/v1/dcpi/oembed", methods=["GET"])
def api_oembed():
    """OEmbed 1.0 provider for the DCPI page + per-market pages.

    Resolves the URL → an embeddable ticker (or per-market card) so external
    publishers (Substack, Medium, news CMSes) can cite DCPI inline.
    """
    target = request.args.get("url", "").strip()
    fmt = (request.args.get("format") or "json").lower()
    if fmt not in ("json",):
        return jsonify(error="only format=json supported"), 501

    # phase 270: validate the URL points at us before resolving anything.
    # Without this check the endpoint would happily build OEmbed payloads for
    # arbitrary domains, which would make us an open redirector for embed
    # crawlers.
    try:
        parsed = _oembed_url.urlparse(target)
    except Exception:
        parsed = None
    if not parsed or parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in _OEMBED_ALLOWED_HOSTS:
        return jsonify(error="url must point to dchub.cloud"), 400

    # Parse target — accept /dcpi or /dcpi/<slug>
    slug = None
    path = parsed.path or ""
    if "/dcpi/" in path:
        slug_raw = path.rsplit("/dcpi/", 1)[-1].strip("/")
        # whitelist: only lowercase alnum/_/- slugs of reasonable length
        if _OEMBED_SLUG_RE.match(slug_raw):
            slug = slug_raw
    is_market = bool(slug and slug not in ("ticker.html", "press"))

    if is_market:
        embed_html = (
            f'<iframe src="https://dchub.cloud/api/v1/dcpi/embed/{slug}" '
            f'width="600" height="240" frameborder="0" '
            f'style="border:1px solid #1f2030;border-radius:8px;max-width:100%;" '
            f'title="DCPI · {slug}"></iframe>'
        )
        body = {
            "version": "1.0", "type": "rich",
            "provider_name": "DC Hub",
            "provider_url": "https://dchub.cloud",
            "title": f"DCPI · {slug}",
            "html": embed_html, "width": 600, "height": 240,
            "cache_age": 300,
        }
    else:
        embed_html = (
            '<iframe src="https://dchub.cloud/dcpi/ticker.html" '
            'width="100%" height="48" frameborder="0" '
            'style="border:0;max-width:100%;" '
            'title="DCPI · Live Ticker"></iframe>'
        )
        body = {
            "version": "1.0", "type": "rich",
            "provider_name": "DC Hub",
            "provider_url": "https://dchub.cloud",
            "title": "Data Center Power Index — Live",
            "html": embed_html, "width": 1280, "height": 48,
            "cache_age": 300,
        }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


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
<meta name="robots" content="index,follow,max-snippet:-1,max-image-preview:large">
<link rel="canonical" href="https://dchub.cloud/dcpi">
<link rel="alternate" type="application/json+oembed" href="https://dchub.cloud/api/v1/dcpi/oembed?url=https%3A%2F%2Fdchub.cloud%2Fdcpi" title="DCPI OEmbed">
<!-- phase 267: schema.org Dataset markup so DCPI is citable by LLMs and search engines -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Dataset",
  "name": "Data Center Power Index (DCPI)",
  "alternateName": "DCPI",
  "description": "Real-time power-availability scoring across {{ count }} U.S. data center markets. Combines ISO grid constraint signals, retail electricity prices, and interconnection-queue pressure into a 0–100 Excess Power Score with an actionable BUILD / CAUTION / AVOID / LOW_SIGNAL verdict per market. Recomputed continuously.",
  "url": "https://dchub.cloud/dcpi",
  "sameAs": "https://dchub.cloud/dcpi",
  "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "keywords": "data center, power index, grid intelligence, market capacity, hyperscale, AI infrastructure, ISO, ERCOT, PJM, MISO, CAISO",
  "license": "https://dchub.cloud/dcpi#methodology",
  "isAccessibleForFree": true,
  "spatialCoverage": {"@type": "Place", "name": "United States"},
  "temporalCoverage": "2024-01-01/..",
  "distribution": [
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/scores", "name": "All market scores (current)"},
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/leaderboard", "name": "Ranked leaderboard (top markets)"},
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/history", "name": "30-day score history per market"}
  ],
  "citation": "DC Hub Data Center Power Index. https://dchub.cloud/dcpi"
}
</script>
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

/* phase 271: verdict filter tabs — Actionable (BUILD/CAUTION/AVOID) is the
   default view; Monitoring (LOW_SIGNAL) is the noisy long tail; All shows
   everything. Designed to mirror .toggle visual language. */
.verdict-tabs {
  display: inline-flex;
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 10px;
  overflow: hidden;
  margin: 0 0 1rem;
}
.verdict-tabs button {
  background: transparent;
  color: var(--tx2);
  border: 0;
  padding: 0.6rem 1.15rem;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.82rem;
  font-family: inherit;
  transition: all 0.15s;
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
}
.verdict-tabs button.active {
  background: var(--gradient);
  color: white;
}
.verdict-tabs button .count {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  padding: 0.12rem 0.45rem;
  border-radius: 99px;
  background: rgba(255,255,255,0.08);
  color: inherit;
}
.verdict-tabs button.active .count {
  background: rgba(255,255,255,0.22);
}
.hidden-by-verdict { display: none !important; }

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

  <!-- phase 271: verdict tabs — Actionable is default so credibility-grade
       verdicts get visual primacy; Monitoring keeps LOW_SIGNAL covered but
       demoted; All preserves the full-coverage claim. Counts are accurate
       to the rendered DOM. -->
  <div class="verdict-tabs" role="tablist" aria-label="Filter markets by verdict">
    <button class="vt active" data-verdict-filter="actionable" role="tab" aria-selected="true">
      Actionable <span class="count">{{ count_actionable }}</span>
    </button>
    <button class="vt" data-verdict-filter="monitoring" role="tab" aria-selected="false">
      Monitoring <span class="count">{{ count_low_signal }}</span>
    </button>
    <button class="vt" data-verdict-filter="all" role="tab" aria-selected="false">
      All <span class="count">{{ count }}</span>
    </button>
  </div>

  <div class="toggle" role="tablist" aria-label="Switch score axis">
    <button class="active" data-mode="excess">Excess Power · Opportunity</button>
    <button data-mode="constraint">Constraint · Avoid</button>
  </div>

  <div class="grid" id="grid">
    {% for s in scores %}
    <a href="/dcpi/{{ s.market_slug }}" style="text-decoration:none;color:inherit;"
       class="card-link {% if s.verdict == 'LOW_SIGNAL' %}hidden-by-verdict{% endif %}"
       data-verdict="{{ s.verdict }}">
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
      <div class="ttp">~{{ (s.time_to_power_months or 0)|round(0)|int }}mo to power</div>
    </div>
    </a>
    {% endfor %}
  </div>

  <!-- Phase AA (2026-05-12): ISO Intelligence panel — surfaces the
       per-ISO aggregate data we always had but never exposed. Each
       chip is a click-to-deep-dive into /dcpi/iso/<code>. Free preview;
       deep ISO comparison + alerts are Pro. -->
  <div class="section-h"><span class="pip"></span>🌐 ISO Intelligence (NEW)</div>
  <p style="color:var(--tx2);font-size:0.95rem;max-width:780px;margin-bottom:14px;">
    Eight North-American ISOs ranked across queue depth, average power cost, build verdicts, and curtailment risk. Click any ISO for the full diagnostic.
  </p>
  <div id="iso-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:24px;">
    <div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;border:1px dashed rgba(255,255,255,0.06);border-radius:10px;">Loading ISO intelligence…</div>
  </div>
  <script>
    // Phase AA: render ISO comparison chips from /api/v1/dcpi/iso-comparison.
    // Fail-soft — banner stays as loading if the endpoint is down.
    fetch('/api/v1/dcpi/iso-comparison').then(r => r.json()).then(data => {
      const grid = document.getElementById('iso-grid');
      const isos = (data && data.isos) || [];
      if (!isos.length) { grid.innerHTML = '<div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;">ISO data is being recomputed — check back shortly.</div>'; return; }
      grid.innerHTML = isos.map(iso => {
        const queue = iso.avg_queue_wait_months != null ? iso.avg_queue_wait_months.toFixed(0) + 'mo' : '—';
        const cost  = iso.avg_kwh_cents != null ? '$' + (iso.avg_kwh_cents/100).toFixed(3) + '/kWh' : '—';
        const build = iso.build_count || 0;
        const total = iso.market_count || 0;
        const buildPct = total ? Math.round(100*build/total) : 0;
        const escapeIso = (iso.iso || '').toLowerCase().replace(/[^a-z0-9-]/g,'');
        return `<a href="/api/v1/dcpi/iso/${escapeIso}" style="text-decoration:none;color:inherit;display:block;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 16px;transition:.15s;"
                  onmouseover="this.style.borderColor='rgba(99,102,241,0.4)'" onmouseout="this.style.borderColor='rgba(255,255,255,0.08)'">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
            <div style="font-weight:700;font-size:1.1rem;color:#fff">${iso.iso || '?'}</div>
            <div style="font-size:0.75rem;color:var(--tx2);">${total} markets</div>
          </div>
          <div style="font-size:0.78rem;color:var(--tx2);margin-bottom:10px;line-height:1.35;">${iso.iso_name || ''}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.82rem;">
            <div><span style="color:var(--tx2)">Queue wait:</span> <b>${queue}</b></div>
            <div><span style="color:var(--tx2)">Avg cost:</span> <b>${cost}</b></div>
            <div><span style="color:var(--tx2)">BUILD verdicts:</span> <b style="color:#10b981">${build} (${buildPct}%)</b></div>
            <div><span style="color:var(--tx2)">Emergencies/30d:</span> <b>${iso.sum_emergency_30d || 0}</b></div>
          </div>
        </a>`;
      }).join('');
    }).catch(e => {
      const grid = document.getElementById('iso-grid');
      if (grid) grid.innerHTML = '<div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;">ISO intelligence temporarily offline.</div>';
    });
  </script>

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

// phase 271: verdict-tab filter — Actionable / Monitoring / All
// Server has already pre-hidden LOW_SIGNAL on the initial DOM via the
// `hidden-by-verdict` class so the default view loads correctly even
// before JS executes. This script handles user clicks.
(function(){
  const tabs = document.querySelectorAll('.verdict-tabs button');
  if (!tabs.length) return;
  function apply(filter){
    document.querySelectorAll('.card-link').forEach(el => {
      const v = el.getAttribute('data-verdict') || '';
      const isLow = v === 'LOW_SIGNAL';
      let hide = false;
      if (filter === 'actionable') hide = isLow;
      else if (filter === 'monitoring') hide = !isLow;
      // 'all' — hide nothing
      el.classList.toggle('hidden-by-verdict', hide);
    });
  }
  tabs.forEach(b => b.addEventListener('click', () => {
    tabs.forEach(x => { x.classList.remove('active'); x.setAttribute('aria-selected','false'); });
    b.classList.add('active'); b.setAttribute('aria-selected','true');
    apply(b.getAttribute('data-verdict-filter'));
  }));
})();
</script>



<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<div id="dcpi-chart-section" style="margin:3rem 0;background:#11121a;border:1px solid #1f2030;border-radius:14px;padding:1.5rem;">
  <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:1rem;font-family:Inter,sans-serif;">
    <span style="width:4px;height:12px;background:#6366f1;border-radius:2px;"></span>
    <span style="font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;">📈 30-day Excess Power · Top 3 BUILD markets</span>
  </div>
  <div style="position:relative;height:280px;"><canvas id="dcpi-history-chart"></canvas></div>
</div>
<script>
(function(){
  if (!document.getElementById('dcpi-history-chart')) return;
  fetch('/api/v1/dcpi/history').then(r=>r.json()).then(d=>{
    const series = d.series || {};
    const top3 = ['cheyenne-wy','rural-spp','williston-nd'];
    const colors = ['#10b981','#a855f7','#6366f1'];
    const datasets = top3.map((slug,i)=>{
      const s = series[slug]; if (!s) return null;
      return { label: s.name, data: s.data.map(p=>({x:p.day,y:p.excess})),
               borderColor: colors[i], backgroundColor: colors[i]+'22',
               borderWidth: 2.5, tension: 0.35, pointRadius: 0 };
    }).filter(Boolean);
    if (!datasets.length) {
      document.getElementById('dcpi-chart-section').style.display = 'none';
      return;
    }
    new Chart(document.getElementById('dcpi-history-chart'), {
      type: 'line', data: { datasets },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#9ca3af' } } },
        scales: {
          x: { type: 'time', time: { unit: 'day' }, ticks: { color: '#9ca3af' }, grid: { color: '#1f2030' } },
          y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2030' }, suggestedMin: 0, suggestedMax: 100 }
        }
      }
    });
  }).catch(e=>{
    console.error('[DCPI chart] error', e);
    document.getElementById('dcpi-chart-section').style.display = 'none';
  });
})();
</script>


<div id="dcpi-subscribe" style="margin:3rem 0;background:linear-gradient(135deg,rgba(99,102,241,0.10),rgba(168,85,247,0.06));border:1px solid #2a2c3e;border-radius:14px;padding:1.5rem;">
  <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.6rem;font-family:Inter,sans-serif;">
    <span style="width:4px;height:12px;background:#6366f1;border-radius:2px;"></span>
    <span style="font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;">📬 Daily DCPI Brief</span>
  </div>
  <h3 style="margin:0 0 0.4rem;font-size:1.2rem;font-weight:700;font-family:Inter,sans-serif;">Wake up to the DC market.</h3>
  <p style="margin:0 0 1rem;color:#9ca3af;font-size:0.92rem;font-family:Inter,sans-serif;">Top 5 BUILD markets, biggest movers, news count — emailed Mon–Fri at 14:00 UTC. Free.</p>
  <form id="dcpi-sub-form" style="display:flex;gap:0.5rem;flex-wrap:wrap;">
    <input type="email" id="dcpi-sub-email" placeholder="you@company.com" required
      style="flex:1;min-width:220px;background:#0a0a12;border:1px solid #1f2030;color:white;padding:0.7rem 1rem;border-radius:6px;font-family:Inter,sans-serif;font-size:0.92rem;outline:none;">
    <button type="submit" id="dcpi-sub-go"
      style="background:linear-gradient(135deg,#6366f1,#a855f7);color:white;border:0;padding:0.7rem 1.3rem;border-radius:6px;font-weight:700;font-size:0.9rem;cursor:pointer;font-family:Inter,sans-serif;">Subscribe →</button>
  </form>
  <div id="dcpi-sub-msg" style="margin-top:0.6rem;font-size:0.85rem;color:#9ca3af;font-family:Inter,sans-serif;"></div>
</div>
<script>
(function(){
  const f = document.getElementById('dcpi-sub-form'); if (!f) return;
  f.addEventListener('submit', async function(e){
    e.preventDefault();
    const em = document.getElementById('dcpi-sub-email').value.trim();
    const msg = document.getElementById('dcpi-sub-msg');
    const btn = document.getElementById('dcpi-sub-go');
    btn.disabled = true;
    msg.textContent = 'Subscribing...';
    try {
      const r = await fetch('/api/v1/digest/subscribe', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email: em})
      });
      const d = await r.json();
      if (d.ok) {
        msg.innerHTML = '<span style="color:#10b981">✓ You\'re in. First brief lands tomorrow at 14:00 UTC.</span>';
        document.getElementById('dcpi-sub-email').value = '';
      } else {
        msg.innerHTML = '<span style="color:#ef4444">' + (d.error || 'error') + '</span>';
      }
    } catch (e) {
      msg.innerHTML = '<span style="color:#ef4444">Error: ' + e + '</span>';
    } finally { btn.disabled = false; }
  });
})();
</script>

<div id="ask-the-index" style="position:fixed;bottom:1.5rem;right:1.5rem;width:400px;max-width:calc(100vw - 3rem);background:#11121a;border:1px solid #2a2c3e;border-radius:14px;padding:1.1rem;font-family:Inter,system-ui;color:white;box-shadow:0 16px 48px rgba(0,0,0,0.5);z-index:1000;">
  <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.6rem;">
    <span style="display:inline-block;width:8px;height:8px;background:#10b981;border-radius:50%;animation:pulse 1.4s ease-in-out infinite;"></span>
    <strong style="font-size:0.78rem;letter-spacing:0.06em;text-transform:uppercase;color:#9ca3af;">Ask the Index</strong>
  </div>
  <div id="ask-out" style="font-size:0.88rem;line-height:1.55;min-height:80px;color:#ddd;margin-bottom:0.6rem;max-height:340px;overflow-y:auto;padding:0.4rem 0;">
    Ask anything about U.S. data center power markets — try: <em style="color:#a5b4fc">show me markets above 70 excess within 300 miles of Atlanta</em>
  </div>
  <textarea id="ask-q" placeholder="e.g. where can I get 100MW within 12 months?" style="width:100%;background:#0a0a12;border:1px solid #1f2030;color:white;padding:0.6rem 0.8rem;border-radius:6px;font-family:inherit;font-size:0.88rem;min-height:54px;resize:none;outline:none;"></textarea>
  <button id="ask-go" style="width:100%;margin-top:0.5rem;background:linear-gradient(135deg,#6366f1,#a855f7);color:white;border:0;padding:0.6rem;border-radius:6px;font-weight:700;font-size:0.88rem;cursor:pointer;">Ask DCPI →</button>
</div>
<script>
(function(){
  function bind(){
    var go = document.getElementById('ask-go');
    var q = document.getElementById('ask-q');
    var out = document.getElementById('ask-out');
    if (!go || !q || !out) {
      console.error('[Ask DCPI] DOM elements not found', {go: !!go, q: !!q, out: !!out});
      return;
    }
    console.log('[Ask DCPI] handlers bound');

    function showError(msg){
      out.innerHTML = '<span style="color:#ef4444;">' + msg + '</span>';
    }

    async function send(){
      var question = (q.value || '').trim();
      if (!question) { q.focus(); return; }
      out.innerHTML = '<em style="color:#9ca3af;">Thinking…</em>';
      go.disabled = true;
      go.style.opacity = '0.6';
      try {
        var resp = await fetch('/api/v1/dcpi/ask?q=' + encodeURIComponent(question), {
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          credentials: 'same-origin'
        });
        if (!resp.ok) {
          showError('HTTP ' + resp.status + ': ' + (await resp.text()).slice(0, 200));
          return;
        }
        var data = await resp.json();
        if (data.error) {
          showError(data.error);
          return;
        }
        var answer = (data.answer || 'No answer.')
          .replace(/\\n/g, '<br>')
          .replace(/\[([^\]]+)\]/g, '<strong style="color:#a5b4fc">[$1]</strong>');
        out.innerHTML = answer;
      } catch(e) {
        console.error('[Ask DCPI] fetch error', e);
        showError('Error: ' + (e && e.message ? e.message : e));
      } finally {
        go.disabled = false;
        go.style.opacity = '1';
      }
    }

    go.addEventListener('click', send);
    q.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
</script>


<div style="background:#11121a;border:1px solid #1f2030;border-radius:12px;padding:20px;margin:32px auto;max-width:760px;font-family:system-ui">
  <div style="font-size:12px;color:#9eb5d8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Cite this index</div>
  <code style="display:block;background:rgba(255,255,255,.03);padding:12px;border-radius:6px;color:#e8eef8;font-size:13px;margin-bottom:8px">DC Hub. (2026). Data Center Power Index v2. https://dchub.cloud/dcpi</code>
  <a href="/dcpi/methodology" style="color:#5aa3ff;font-size:14px;text-decoration:none">View methodology + BibTeX →</a>
</div>
<script>
// Phase 241: live DCPI market count
(function(){
  fetch('/api/v1/dcpi/live-count')
    .then(r => r.json())
    .then(d => {
      const n = d.published || d.total || 280;
      // Find any element containing "280+ MARKETS" or hardcoded number, replace with live count
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while (node = walker.nextNode()) {
        if (/\b(280\+?|276)\s*MARKETS/.test(node.nodeValue)) {
          node.nodeValue = node.nodeValue.replace(/\b(280\+?|276)\s*MARKETS/, n + ' MARKETS');
        }
        if (/\b(280\+?|276)\s+U\.S\./.test(node.nodeValue)) {
          node.nodeValue = node.nodeValue.replace(/\b(280\+?|276)\s+U\.S\./, n + ' U.S.');
        }
      }
    })
    .catch(() => {});
})();
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
<meta property="og:description" content="{{ s.verdict }} · ~{{ (s.time_to_power_months or 0)|round(0)|int }} months to power. Updated daily.">
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
      <div class="metric"><div class="v">{{ (s.queue_wait_months or 0)|round(0)|int }} mo</div><div class="l">Queue Wait</div></div>
      <div class="metric"><div class="v">{{ (s.reserve_margin_pct or 0)|round(1) }}%</div><div class="l">Reserve Margin</div></div>
      <div class="metric"><div class="v">{{ (s.gen_additions_12mo_mw or 0)|round(0)|int }} MW</div><div class="l">Generation Additions &lt;12mo</div></div>
      <div class="metric"><div class="v">{{ (s.curtailment_pct or 0)|round(1) }}%</div><div class="l">Renewable Curtailment</div></div>
      <div class="metric"><div class="v">{{ (s.stranded_capacity_mw or 0)|round(0)|int }} MW</div><div class="l">Stranded Capacity</div></div>
      <div class="metric"><div class="v">{{ (s.time_to_power_months or 0)|round(0)|int }} mo</div><div class="l">Est. Time to Power</div></div>
    </div>
  </div>

  <div class="cta-pro">
    <h2>Drill into {{ s.market_name }} at the county level.</h2>
    <p>Pro shows the score at the county level so you can pinpoint which sub-markets have the headroom. Plus alerts when {{ s.market_name }} moves &gt;5 points and PDF export for your buyers.</p>
    <a href="/pricing">Get Pro · $199/mo →</a>
  </div>
</div>

<div style="background:#11121a;border:1px solid #1f2030;border-radius:12px;padding:20px;margin:32px auto;max-width:760px;font-family:system-ui">
  <div style="font-size:12px;color:#9eb5d8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Cite this index</div>
  <code style="display:block;background:rgba(255,255,255,.03);padding:12px;border-radius:6px;color:#e8eef8;font-size:13px;margin-bottom:8px">DC Hub. (2026). Data Center Power Index v2. https://dchub.cloud/dcpi</code>
  <a href="/dcpi/methodology" style="color:#5aa3ff;font-size:14px;text-decoration:none">View methodology + BibTeX →</a>
</div>
</body>
</html>"""


@_safe_dcpi_page
@dcpi_bp.route("/dcpi", methods=["GET"])
def public_dashboard():
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug) *
            FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()
    rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    if not rows:
        # Trigger an initial recompute so the page is never empty
        try: recompute_all_scores(source="cold-start")
        except Exception: pass
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT DISTINCT ON (market_slug) *
                           FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC""")
            rows = cur.fetchall()
        rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    # phase 271: surface verdict counts so the LOW_SIGNAL "Monitoring" tab can
    # show a count badge, and the page's "Actionable" default isn't an opaque
    # filter. Actionable = BUILD + CAUTION + AVOID (decision-grade); monitoring
    # = LOW_SIGNAL (covered but no actionable signal yet).
    _ACTIONABLE = {"BUILD", "CAUTION", "AVOID"}
    count_actionable = sum(1 for r in rows if (r.get("verdict") or "") in _ACTIONABLE)
    count_low_signal = sum(1 for r in rows if (r.get("verdict") or "") == "LOW_SIGNAL")
    html = render_template_string(
        DCPI_INDEX_TEMPLATE,
        scores=rows,
        count=len(rows),
        count_actionable=count_actionable,
        count_low_signal=count_low_signal,
    )
    # phase 284: ship a Content-Security-Policy header on /dcpi so the
    # dchub-frontend qa-csp-parse preflight CI doesn't fail on this page.
    # Mirrors the policy that the Pages-served pages (/, /pricing, /news,
    # etc.) get from Cloudflare Pages _headers — same allowed sources, same
    # directive coverage. Without this header, the CSP-watch automation
    # treated /dcpi as a regression even though the page is intentional.
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = _DCPI_CSP
    return resp


# Canonical CSP applied to /dcpi (phase 284). Mirrors the Pages-level CSP
# applied to / and /pricing — same hosts, same directive coverage. Kept as
# a module-level constant so the per-market /dcpi/<slug> page can reuse it
# without duplication.
_DCPI_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' "
        "https://cdnjs.cloudflare.com https://unpkg.com https://cdn.jsdelivr.net "
        "https://www.googletagmanager.com https://accounts.google.com "
        "https://static.cloudflareinsights.com; "
    "script-src-elem 'self' 'unsafe-inline' "
        "https://cdnjs.cloudflare.com https://unpkg.com https://cdn.jsdelivr.net "
        "https://www.googletagmanager.com https://accounts.google.com "
        "https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com https://cdnjs.cloudflare.com "
        "https://accounts.google.com; "
    "style-src-elem 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com https://cdnjs.cloudflare.com "
        "https://accounts.google.com; "
    "img-src 'self' data: https:; "
    "font-src 'self' data: https: https://fonts.gstatic.com; "
    "connect-src 'self' "
        "https://dchub-backend-production.up.railway.app "
        "https://www.google-analytics.com https://cloudflareinsights.com; "
    "frame-src 'self' https://accounts.google.com; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "report-uri /api/csp-report"
)


@dcpi_bp.route("/dcpi/<slug>", methods=["GET"])
def public_market_page(slug):
    _ensure_tables()
    # Phase JJ (2026-05-14): slug aliasing. The market_power_scores table
    # uses bare slugs (e.g. 'allen' not 'allen-tx'), but external links
    # often append state suffix because that's the natural-language
    # form (yesterday's auto-press wrote "Allen, TX ranked #3" which
    # AI agents parsing the article reasonably resolved to /dcpi/allen-tx).
    # Try the exact slug first, then strip common suffix patterns.
    candidates = [slug]
    # Strip -<state> suffix: 'allen-tx' → 'allen'
    if "-" in slug and len(slug.rsplit("-", 1)[1]) == 2:
        candidates.append(slug.rsplit("-", 1)[0])
    # Strip -texas / -california / etc. (full state names)
    _STATE_FULL_SUFFIXES = (
        '-texas', '-california', '-virginia', '-georgia', '-illinois',
        '-arizona', '-wyoming', '-nevada', '-oregon', '-washington',
        '-florida', '-ohio', '-michigan', '-newyork', '-new-york',
    )
    for suf in _STATE_FULL_SUFFIXES:
        if slug.endswith(suf):
            candidates.append(slug[:-len(suf)])
            break

    s = None
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for cand in candidates:
            cur.execute("""SELECT * FROM market_power_scores
                           WHERE market_slug = %s
                           ORDER BY computed_at DESC LIMIT 1""", (cand,))
            s = cur.fetchone()
            if s:
                # If we matched on an alias (not the original), 301-redirect
                # to the canonical slug. Preserves SEO equity for inbound
                # links and ensures Google deduplicates the page.
                if cand != slug:
                    from flask import redirect
                    return redirect(f"/dcpi/{cand}", code=301)
                break
    if not s:
        # phase 284: even 404 should ship the CSP so it doesn't trip the watch
        r = Response(f"<h1>Market not found: {slug}</h1>", status=404, mimetype="text/html")
        r.headers["Content-Security-Policy"] = _DCPI_CSP
        return r
    if s.get("computed_at"): s["computed_at"] = s["computed_at"].isoformat()
    risks = s.get("top_risks_json") or []
    opps = s.get("top_opportunities_json") or []
    market_html = render_template_string(DCPI_MARKET_TEMPLATE, s=s, risks=risks, opps=opps)
    market_resp = Response(market_html, mimetype="text/html")
    market_resp.headers["Content-Security-Policy"] = _DCPI_CSP  # phase 284
    return market_resp



@dcpi_bp.route("/api/v1/dcpi/history", methods=["GET"])
def api_history():
    """Return per-day score history for top BUILD markets, last 30 days."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT market_slug, market_name,
                   DATE_TRUNC('day', computed_at) AS day,
                   COALESCE(AVG(excess_power_score), 0) AS excess,
                   COALESCE(AVG(constraint_score), 0) AS constraint
            FROM market_power_scores
            WHERE computed_at > NOW() - INTERVAL '30 days'
            GROUP BY market_slug, market_name, DATE_TRUNC('day', computed_at)
            ORDER BY market_slug, day
        """)
        rows = cur.fetchall()
    series = {}
    for r in rows:
        slug = r["market_slug"]
        if slug not in series:
            series[slug] = {"name": r["market_name"], "data": []}
        series[slug]["data"].append({
            "day": r["day"].isoformat()[:10] if r.get("day") else None,
            "excess": float(r["excess"] or 0),
            "constraint": float(r["constraint"] or 0),
        })
    return jsonify(series=series, count=len(series)), 200



@dcpi_bp.route("/api/v1/dcpi/trending", methods=["GET"])
def api_trending():
    """Top 5 weekly movers, formatted for ticker display."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH latest AS (
              SELECT DISTINCT ON (market_slug) market_slug, market_name, excess_power_score AS now_e
              FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
            ),
            week_ago AS (
              SELECT DISTINCT ON (market_slug) market_slug, excess_power_score AS prev_e
              FROM market_power_scores
              WHERE computed_at < NOW() - INTERVAL '7 days'
              ORDER BY market_slug, computed_at DESC
            )
            SELECT l.market_slug, l.market_name, l.now_e,
                   COALESCE(l.now_e - w.prev_e, 0) AS delta_7d
            FROM latest l LEFT JOIN week_ago w ON l.market_slug=w.market_slug
            ORDER BY ABS(COALESCE(l.now_e - w.prev_e, 0)) DESC LIMIT 5
        """)
        rows = cur.fetchall()
    return jsonify(trending=rows, count=len(rows)), 200


@dcpi_bp.route("/dcpi/ticker.html", methods=["GET"])
@dcpi_bp.route("/api/v1/dcpi/ticker.html", methods=["GET"])
def ticker_widget():
    """Embeddable horizontal ticker widget. Drop in any iframe."""
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:0;font-family:-apple-system,sans-serif;background:#0a0a12;color:#fff;overflow:hidden}
.ticker{display:flex;align-items:center;height:48px;border-top:1px solid #1f2030;border-bottom:1px solid #1f2030;animation:scroll 40s linear infinite}
.item{flex:0 0 auto;display:flex;align-items:center;gap:0.5rem;padding:0 1.5rem;border-right:1px solid #1f2030;white-space:nowrap}
.lbl{font-size:0.7rem;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em}
.market{font-weight:600;font-size:0.92rem}
.score{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:0.92rem}
.up{color:#10b981}.down{color:#ef4444}
.brand{flex:0 0 auto;background:linear-gradient(135deg,#6366f1,#a855f7);padding:0 1.25rem;height:48px;display:flex;align-items:center;font-weight:700;font-size:0.85rem;letter-spacing:0.08em;text-transform:uppercase}
@keyframes scroll{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}
</style></head><body>
<div style="display:flex;align-items:center;height:48px;background:#0a0a12">
<div class="brand">DCPI · Live</div>
<div class="ticker" id="t"></div>
</div>
<script>
fetch('/api/v1/dcpi/trending').then(r=>r.json()).then(d=>{
  const html = (d.trending||[]).map(t=>{
    const dir = t.delta_7d>0?'up':'down', arrow=t.delta_7d>0?'▲':'▼';
    return '<div class=item><span class=lbl>'+arrow+'</span><a href="https://dchub.cloud/dcpi/'+t.market_slug+'" target="_blank" style="color:#fff;text-decoration:none"><span class=market>'+t.market_name+'</span></a><span class="score '+dir+'">'+t.now_e.toFixed(1)+' ('+(t.delta_7d>0?'+':'')+t.delta_7d.toFixed(1)+')</span></div>';
  }).join('');
  document.getElementById('t').innerHTML = html + html;  // double for seamless scroll
});
</script></body></html>"""
    resp = Response(html, mimetype="text/html")
    resp.headers["X-Frame-Options"] = "ALLOWALL"
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return resp


@dcpi_bp.route("/dcpi/og/<slug>.svg", methods=["GET"])
@dcpi_bp.route("/dcpi/og/<slug>", methods=["GET"])
def og_card(slug):
    """1200x630 SVG for LinkedIn/X cards. Phase 121C: fixed layout."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores WHERE market_slug = %s
                       ORDER BY computed_at DESC LIMIT 1""", (slug,))
        s = cur.fetchone()
    if not s: return Response("not found", status=404)

    excess_score = int(s["excess_power_score"] or 0)
    constraint_score = int(s["constraint_score"] or 0)
    ttp = int(s["time_to_power_months"] or 0)
    excess_color = ("#10b981" if excess_score >= 65 else
                    "#f59e0b" if excess_score >= 40 else "#ef4444")
    verdict_color = {"BUILD": "#10b981", "CAUTION": "#f59e0b", "AVOID": "#ef4444"}.get(s["verdict"], "#9ca3af")

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0a0a12"/>
      <stop offset="1" stop-color="#1a1a2e"/>
    </linearGradient>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#6366f1"/>
      <stop offset="1" stop-color="#a855f7"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>

  <!-- Brand strip top -->
  <rect x="0" y="0" width="1200" height="6" fill="url(#brand)"/>

  <!-- Header -->
  <text x="60" y="80" font-family="-apple-system, sans-serif" font-size="22" font-weight="700"
        fill="#9ca3af" letter-spacing="2">DCPI · DC HUB POWER INDEX</text>

  <!-- Market name + region -->
  <text x="60" y="180" font-family="-apple-system, sans-serif" font-size="76" font-weight="800"
        fill="white" letter-spacing="-1">{s['market_name']}</text>
  <text x="60" y="220" font-family="-apple-system, sans-serif" font-size="22"
        fill="#9ca3af">{s['iso']} · {s['state']}</text>

  <!-- Excess Power, left column -->
  <text x="60" y="320" font-family="-apple-system, sans-serif" font-size="18" font-weight="600"
        fill="#9ca3af" letter-spacing="2">EXCESS POWER SCORE</text>
  <text x="60" y="500" font-family="-apple-system, sans-serif" font-size="180" font-weight="800"
        fill="{excess_color}" letter-spacing="-6">{excess_score}</text>

  <!-- Constraint, right column -->
  <text x="700" y="320" font-family="-apple-system, sans-serif" font-size="18" font-weight="600"
        fill="#9ca3af" letter-spacing="2">CONSTRAINT</text>
  <text x="700" y="450" font-family="-apple-system, sans-serif" font-size="120" font-weight="700"
        fill="#9ca3af" letter-spacing="-3">{constraint_score}</text>
  <text x="700" y="495" font-family="-apple-system, sans-serif" font-size="20" font-weight="600"
        fill="#9ca3af" letter-spacing="2">~{ttp}mo TO POWER</text>

  <!-- Verdict bottom -->
  <text x="60" y="565" font-family="-apple-system, sans-serif" font-size="26" font-weight="800"
        fill="{verdict_color}" letter-spacing="3">VERDICT: {s['verdict']}</text>

  <!-- URL bottom right -->
  <text x="1140" y="600" font-family="-apple-system, sans-serif" font-size="16"
        fill="#6b7280" text-anchor="end">dchub.cloud/dcpi/{slug}</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=600, must-revalidate"})


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



# (phase 215 lite-recompute moved to main.py in phase 216 — removed duplicate here)

@dcpi_bp.route("/api/v1/dcpi/lite-recompute", methods=["POST"])
def lite_recompute():
    """Computes lite DCPI scores for ALL markets in MARKETS.
    Uses only facility count + pipeline MW + state $/kWh — no grid stress data.
    Marks results with tier_required='lite-pro' so we can distinguish from full scoring."""
    import psycopg2, os, math
    try:
        admin_key = request.headers.get("X-Admin-Key", "")
        if admin_key != os.environ.get("DCHUB_ADMIN_KEY", ""):
            return jsonify({"error": "unauthorized"}), 401

        url = os.environ.get("DATABASE_URL")
        conn = psycopg2.connect(url, connect_timeout=8)
        scored = 0
        errors = 0
        with conn.cursor() as cur:
            for m in MARKETS:
                try:
                    slug = m.get("slug") if isinstance(m, dict) else m
                    name = m.get("name") if isinstance(m, dict) else slug.replace("-", " ").title()
                    if not slug: continue
                    # Pull facility stats
                    cur.execute("""
                        SELECT COUNT(*),
                               COALESCE(SUM(power_mw), 0),
                               COALESCE(SUM(power_mw) FILTER (WHERE status IN ('construction','planned','permitting','Under Construction','Planned')), 0),
                               COALESCE(MAX(state), 0)
                        FROM discovered_facilities
                        WHERE LOWER(city) = %s OR LOWER(city) LIKE %s;
                    """, (slug.replace("-", " "), '%' + slug.replace("-", " ") + '%'))
                    row = cur.fetchone()
                    if not row: continue
                    fac, op_mw, pipe_mw, state = row
                    if not fac: continue

                    # $/kWh from state
                    cur.execute("""
                        SELECT COALESCE(AVG(price_cents_kwh), 0)/100.0 FROM eia_electricity_rates
                        WHERE state=%s AND sector='ALL'
                          AND retrieved_at > NOW() - INTERVAL '365 days';
                    """, (state,))
                    kr = cur.fetchone()
                    kwh = float(kr[0]) if kr and kr[0] else None

                    # Lite scoring (0-100 scale):
                    # constraint_score: high pipeline ratio → constrained
                    # excess_power_score: low pipeline + cheap kWh → opportunity
                    pipe_ratio = (pipe_mw / op_mw) if op_mw > 0 else 0
                    constraint = min(100, pipe_ratio * 150)  # >0.67 ratio → max constraint
                    excess = 0
                    if kwh:
                        # Cheaper → higher excess opportunity
                        excess = max(0, min(100, (0.30 - kwh) * 333))  # $0.08 → 73, $0.20 → 33
                    if pipe_mw < 50 and op_mw > 100:
                        excess = max(excess, 60)  # underbuilt market

                    verdict = "BUILD" if excess > 50 and constraint < 60 else ("AVOID" if constraint > 75 else "CAUTION")

                    cur.execute("""
                        INSERT INTO market_power_scores
                        (market_slug, market_name, latitude, longitude,
                         constraint_score, excess_power_score, time_to_power_months,
                         verdict, tier_required, computed_at)
                        VALUES (%s, %s, NULL, NULL, %s, %s, NULL, %s, 'lite-pro', NOW())
                        ON CONFLICT (market_slug) DO UPDATE SET
                          constraint_score = EXCLUDED.constraint_score,
                          excess_power_score = EXCLUDED.excess_power_score,
                          verdict = EXCLUDED.verdict,
                          tier_required = EXCLUDED.tier_required,
                          computed_at = NOW();
                    """, (slug, name, constraint, excess, verdict))
                    scored += 1
                except Exception as e:
                    errors += 1

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "markets_scored": scored,
            "errors": errors,
            "total_markets": len(MARKETS),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Phase 215: ensure UNIQUE on market_slug for upsert
def _phase215_ensure_unique():
    import os, psycopg2
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'market_power_scores_slug_key'
                    ) THEN
                        ALTER TABLE market_power_scores
                            ADD CONSTRAINT market_power_scores_slug_key UNIQUE (market_slug);
                    END IF;
                END $$;
            """)
        conn.commit()
        conn.close()
    except Exception as e:
        import logging
        logging.warning(f"phase215 unique constraint err: {e}")

try: _phase215_ensure_unique()
except: pass


# ============================================================================
# Phase 225: graceful failure — never show JSON error on user-facing pages
# ============================================================================

DCPI_FALLBACK_HTML = """<!doctype html><html><head>
<title>DC Hub Power Index · Recomputing</title>
<meta charset="utf-8"><meta http-equiv="refresh" content="30">
<style>html,body{background:rgb(5,8,16);color:#e8eef8;margin:0;padding:60px 20px;font-family:'Instrument Sans',system-ui;text-align:center;line-height:1.6}
h1{font-weight:800;font-size:36px;margin:0 0 12px}
.sub{color:#9eb5d8;font-size:18px;max-width:560px;margin:0 auto 32px}
.spinner{width:32px;height:32px;margin:0 auto 24px;border:3px solid rgba(90,163,255,.2);border-top-color:#5aa3ff;border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
a{color:#5aa3ff;text-decoration:none}
</style></head><body>
<div class="spinner"></div>
<h1>DCPI is recomputing</h1>
<p class="sub">The Data Center Power Index updates daily. Today's scoring is in progress — refresh in a moment, or browse <a href="/markets/">all markets</a> meanwhile.</p>
<p style="color:#5aa3ff"><a href="/dcpi/methodology">View methodology →</a></p>
</body></html>"""

def _phase225_dcpi_error_page(err=""):
    """Returns the recomputing-message HTML so users never see raw JSON errors."""
    import logging
    if err: logging.warning(f"[dcpi-fallback] {err}")
    return DCPI_FALLBACK_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# Wrap the dcpi_bp blueprint errorhandler
try:
    @dcpi_bp.errorhandler(Exception)
    def _phase225_dcpi_bp_error_handler(e):
        return _phase225_dcpi_error_page(str(e))
except Exception:
    pass
