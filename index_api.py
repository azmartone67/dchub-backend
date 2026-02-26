"""
DC Hub Global Data Center Index (GDCI) — FastAPI Backend
=========================================================
Deploy alongside your existing Railway backend.

Add to your Railway service:
  - Copy this file into your backend directory
  - Include the router in your main FastAPI app:
      from index_api import router as index_router
      app.include_router(index_router, prefix="/api/index")

Environment variables required (already in Railway):
  DATABASE_URL — your existing PostgreSQL connection string

Schema assumptions (adjust TABLE_* constants below to match yours):
  facilities   → id, name, city, country, region, total_mw, available_mw,
                  status, created_at, updated_at
  transactions → id, target_name, buyer, seller, deal_value_usd, deal_date,
                  market, region, facility_mw
  market_intel → id, market, region, avg_rate_per_kw, recorded_at
  substations  → id, city, state, country, capacity_mva, available_mva

Adjust the constants below if your column names differ.
"""

import os
import asyncio
import logging
from datetime import datetime, date, timedelta
from typing import Optional
from functools import lru_cache
import time

import asyncpg
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["DC Hub Global Data Center Index"])

# ─────────────────────────────────────────────
# SCHEMA CONSTANTS — adjust to match your tables
# ─────────────────────────────────────────────
TABLE_FACILITIES    = "facilities"
TABLE_TRANSACTIONS  = "transactions"
TABLE_MARKET_INTEL  = "market_intelligence"
TABLE_SUBSTATIONS   = "substations"

COL_FAC_MARKET      = "city"          # or "market" if you have that column
COL_FAC_REGION      = "region"        # "us", "emea", "apac", "latam"
COL_FAC_COUNTRY     = "country"
COL_FAC_TOTAL_MW    = "total_mw"
COL_FAC_AVAIL_MW    = "available_mw"
COL_FAC_STATUS      = "status"        # "operational", "under_construction", etc.

COL_TXN_MARKET      = "market"
COL_TXN_DATE        = "deal_date"
COL_TXN_VALUE       = "deal_value_usd"
COL_TXN_MW          = "facility_mw"

COL_MI_MARKET       = "market"
COL_MI_RATE         = "avg_rate_per_kw"
COL_MI_DATE         = "recorded_at"

COL_SUB_CITY        = "city"
COL_SUB_COUNTRY     = "country"
COL_SUB_CAPACITY    = "capacity_mva"
COL_SUB_AVAILABLE   = "available_mva"

# Rate index base period (Jan 2025 = 100)
RATE_BASE_DATE      = "2025-01-01"

# Sub-index composite weights (must sum to 1.0)
WEIGHTS = {
    "dhci": 0.30,   # Capacity
    "dhri": 0.25,   # Rate
    "dhpi": 0.20,   # Pipeline
    "dhdi": 0.15,   # Demand
    "dhpw": 0.10,   # Power
}

# Markets to track in the index
TRACKED_MARKETS = [
    {"id": "nova",      "name": "Northern Virginia, US", "region": "us",   "country": "US"},
    {"id": "dal",       "name": "Dallas/Fort Worth, US",  "region": "us",   "country": "US"},
    {"id": "phx",       "name": "Phoenix, US",            "region": "us",   "country": "US"},
    {"id": "chi",       "name": "Chicago, US",            "region": "us",   "country": "US"},
    {"id": "sv",        "name": "Silicon Valley, US",     "region": "us",   "country": "US"},
    {"id": "nyc",       "name": "New York/NJ, US",        "region": "us",   "country": "US"},
    {"id": "atl",       "name": "Atlanta, US",            "region": "us",   "country": "US"},
    {"id": "lhr",       "name": "London, UK",             "region": "emea", "country": "GB"},
    {"id": "fra",       "name": "Frankfurt, Germany",     "region": "emea", "country": "DE"},
    {"id": "ams",       "name": "Amsterdam, Netherlands", "region": "emea", "country": "NL"},
    {"id": "par",       "name": "Paris, France",          "region": "emea", "country": "FR"},
    {"id": "sin",       "name": "Singapore",              "region": "apac", "country": "SG"},
    {"id": "tyo",       "name": "Tokyo, Japan",           "region": "apac", "country": "JP"},
    {"id": "syd",       "name": "Sydney, Australia",      "region": "apac", "country": "AU"},
    {"id": "bom",       "name": "Mumbai, India",          "region": "apac", "country": "IN"},
    {"id": "yyz",       "name": "Toronto, Canada",        "region": "us",   "country": "CA"},
    {"id": "gru",       "name": "São Paulo, Brazil",      "region": "latam","country": "BR"},
    {"id": "mex",       "name": "Mexico City, Mexico",    "region": "latam","country": "MX"},
]

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    return _pool


# ─────────────────────────────────────────────
# CACHE (simple in-memory, TTL=1h)
# ─────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 3600  # seconds

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["val"]
    return None

def cache_set(key: str, val):
    _cache[key] = {"val": val, "ts": time.time()}

def cache_invalidate(prefix: str = ""):
    keys = [k for k in _cache if k.startswith(prefix)]
    for k in keys:
        del _cache[k]


# ─────────────────────────────────────────────
# SUB-INDEX CALCULATORS
# ─────────────────────────────────────────────

async def calc_dhci(pool: asyncpg.Pool, market_id: str, market_meta: dict) -> dict:
    """
    DHCI (Capacity Index) = available_mw / total_mw
    Lower vacancy = tighter market = higher constraint score
    """
    try:
        rows = await pool.fetch(f"""
            SELECT
                SUM({COL_FAC_TOTAL_MW})    AS total_mw,
                SUM({COL_FAC_AVAIL_MW})    AS avail_mw
            FROM {TABLE_FACILITIES}
            WHERE ({COL_FAC_MARKET} ILIKE $1 OR {COL_FAC_COUNTRY} = $2)
              AND {COL_FAC_STATUS} = 'operational'
        """, f"%{market_meta['name'].split(',')[0]}%", market_meta["country"])

        row = rows[0] if rows else None
        total = float(row["total_mw"] or 0)
        avail = float(row["avail_mw"] or 0)

        if total == 0:
            return {"value": None, "vacancy_pct": None, "total_mw": 0, "available_mw": 0}

        vacancy_pct = (avail / total) * 100
        # Score: 0% vacancy = 100 constraint; 10%+ vacancy = 0 constraint
        score = max(0, min(100, (1 - vacancy_pct / 10) * 100))

        return {
            "value": round(score, 1),
            "vacancy_pct": round(vacancy_pct, 2),
            "total_mw": round(total, 1),
            "available_mw": round(avail, 1),
        }
    except Exception as e:
        logger.warning(f"DHCI calc failed for {market_id}: {e}")
        return {"value": None, "vacancy_pct": None, "total_mw": 0, "available_mw": 0}


async def calc_dhri(pool: asyncpg.Pool, market_id: str, market_meta: dict) -> dict:
    """
    DHRI (Rate Index) = current avg rate / base period rate × 100
    Base period: Jan 2025
    """
    try:
        city = market_meta["name"].split(",")[0].strip()

        current = await pool.fetchval(f"""
            SELECT AVG({COL_MI_RATE})
            FROM {TABLE_MARKET_INTEL}
            WHERE {COL_MI_MARKET} ILIKE $1
              AND {COL_MI_DATE} >= NOW() - INTERVAL '45 days'
        """, f"%{city}%")

        base = await pool.fetchval(f"""
            SELECT AVG({COL_MI_RATE})
            FROM {TABLE_MARKET_INTEL}
            WHERE {COL_MI_MARKET} ILIKE $1
              AND {COL_MI_DATE} BETWEEN '{RATE_BASE_DATE}'::date
                                     AND ('{RATE_BASE_DATE}'::date + INTERVAL '30 days')
        """, f"%{city}%")

        current_rate = float(current or 0)
        base_rate = float(base or 0)

        if base_rate == 0 or current_rate == 0:
            return {"value": None, "rate_per_kw": current_rate or None, "index_value": None}

        index_val = (current_rate / base_rate) * 100
        return {
            "value": round(min(100, max(0, (index_val - 80) / 0.6)), 1),
            "rate_per_kw": round(current_rate, 2),
            "index_value": round(index_val, 1),
        }
    except Exception as e:
        logger.warning(f"DHRI calc failed for {market_id}: {e}")
        return {"value": None, "rate_per_kw": None, "index_value": None}


async def calc_dhpi(pool: asyncpg.Pool, market_id: str, market_meta: dict) -> dict:
    """
    DHPI (Pipeline Index) = MW under construction / total operational MW
    Higher pipeline = more supply incoming = lower constraint
    """
    try:
        city = market_meta["name"].split(",")[0].strip()

        pipeline_mw = await pool.fetchval(f"""
            SELECT SUM({COL_FAC_TOTAL_MW})
            FROM {TABLE_FACILITIES}
            WHERE ({COL_FAC_MARKET} ILIKE $1 OR {COL_FAC_COUNTRY} = $2)
              AND {COL_FAC_STATUS} IN ('under_construction', 'planned', 'development')
        """, f"%{city}%", market_meta["country"])

        operational_mw = await pool.fetchval(f"""
            SELECT SUM({COL_FAC_TOTAL_MW})
            FROM {TABLE_FACILITIES}
            WHERE ({COL_FAC_MARKET} ILIKE $1 OR {COL_FAC_COUNTRY} = $2)
              AND {COL_FAC_STATUS} = 'operational'
        """, f"%{city}%", market_meta["country"])

        pipeline = float(pipeline_mw or 0)
        operational = float(operational_mw or 1)
        ratio = pipeline / operational * 100

        return {
            "value": round(min(100, ratio * 2), 1),
            "pipeline_mw": round(pipeline, 1),
            "operational_mw": round(operational, 1),
            "pipeline_ratio_pct": round(ratio, 2),
        }
    except Exception as e:
        logger.warning(f"DHPI calc failed for {market_id}: {e}")
        return {"value": None, "pipeline_mw": None, "pipeline_ratio_pct": None}


async def calc_dhdi(pool: asyncpg.Pool, market_id: str, market_meta: dict) -> dict:
    """
    DHDI (Demand Index) = net MW absorbed via transactions (trailing 90 days)
    Uses M&A / transaction data as demand signal
    """
    try:
        city = market_meta["name"].split(",")[0].strip()

        absorbed_mw = await pool.fetchval(f"""
            SELECT SUM({COL_TXN_MW})
            FROM {TABLE_TRANSACTIONS}
            WHERE ({COL_TXN_MARKET} ILIKE $1)
              AND {COL_TXN_DATE} >= NOW() - INTERVAL '90 days'
              AND {COL_TXN_VALUE} > 0
        """, f"%{city}%")

        mw = float(absorbed_mw or 0)
        # Score: 0 MW = 0; 1000+ MW = 100
        score = min(100, (mw / 1000) * 100)

        return {
            "value": round(score, 1),
            "absorbed_mw_90d": round(mw, 1),
        }
    except Exception as e:
        logger.warning(f"DHDI calc failed for {market_id}: {e}")
        return {"value": None, "absorbed_mw_90d": None}


async def calc_dhpw(pool: asyncpg.Pool, market_id: str, market_meta: dict) -> dict:
    """
    DHPW (Power Index) = grid headroom score
    Higher available_mva vs total = more power headroom = lower constraint
    Score is inverted: 100 = abundant power, 0 = none
    """
    try:
        city = market_meta["name"].split(",")[0].strip()

        rows = await pool.fetch(f"""
            SELECT
                SUM({COL_SUB_CAPACITY})  AS total_mva,
                SUM({COL_SUB_AVAILABLE}) AS avail_mva
            FROM {TABLE_SUBSTATIONS}
            WHERE {COL_SUB_CITY} ILIKE $1
               OR {COL_SUB_COUNTRY} = $2
        """, f"%{city}%", market_meta["country"])

        row = rows[0] if rows else None
        total = float(row["total_mva"] or 0)
        avail = float(row["avail_mva"] or 0)

        if total == 0:
            return {"value": None, "available_mva": None, "headroom_pct": None}

        headroom = (avail / total) * 100
        # Power score: high headroom = high score (good)
        score = round(headroom, 1)

        return {
            "value": score,
            "available_mva": round(avail, 1),
            "total_mva": round(total, 1),
            "headroom_pct": round(headroom, 2),
        }
    except Exception as e:
        logger.warning(f"DHPW calc failed for {market_id}: {e}")
        return {"value": None, "available_mva": None, "headroom_pct": None}


def compute_composite(sub_indices: dict) -> Optional[float]:
    """Weighted composite. Returns None if any required sub-index is missing."""
    total_weight = 0
    score = 0
    for key, weight in WEIGHTS.items():
        val = sub_indices.get(key, {}).get("value")
        if val is not None:
            score += val * weight
            total_weight += weight
    if total_weight == 0:
        return None
    # Normalize to the weights we actually have data for
    return round(score / total_weight, 1)


def score_to_label(score: Optional[float]) -> str:
    if score is None:
        return "Insufficient Data"
    if score < 40:   return "Buyer's Market"
    if score < 60:   return "Balanced"
    if score < 75:   return "Constrained"
    return "Critical"

def score_to_color(score: Optional[float]) -> str:
    if score is None: return "gray"
    if score < 40:   return "green"
    if score < 60:   return "amber"
    if score < 75:   return "purple"
    return "red"


# ─────────────────────────────────────────────
# RESPONSE MODELS
# ─────────────────────────────────────────────

class SubIndexData(BaseModel):
    value: Optional[float]
    label: str
    color: str

class MarketIndexData(BaseModel):
    market_id: str
    market_name: str
    region: str
    country: str
    composite_score: Optional[float]
    composite_label: str
    composite_color: str
    dhci: dict
    dhri: dict
    dhpi: dict
    dhdi: dict
    dhpw: dict
    computed_at: str

class IndexSummary(BaseModel):
    issue: str
    published_at: str
    global_composite: Optional[float]
    global_label: str
    global_color: str
    global_vacancy_pct: Optional[float]
    markets_covered: int
    markets: list


# ─────────────────────────────────────────────
# CORE CALCULATION
# ─────────────────────────────────────────────

async def calculate_market_index(pool: asyncpg.Pool, market: dict) -> dict:
    mid = market["id"]
    cache_key = f"market:{mid}:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    dhci, dhri, dhpi, dhdi, dhpw = await asyncio.gather(
        calc_dhci(pool, mid, market),
        calc_dhri(pool, mid, market),
        calc_dhpi(pool, mid, market),
        calc_dhdi(pool, mid, market),
        calc_dhpw(pool, mid, market),
    )

    sub = {"dhci": dhci, "dhri": dhri, "dhpi": dhpi, "dhdi": dhdi, "dhpw": dhpw}
    composite = compute_composite(sub)

    result = {
        "market_id":        mid,
        "market_name":      market["name"],
        "region":           market["region"],
        "country":          market["country"],
        "composite_score":  composite,
        "composite_label":  score_to_label(composite),
        "composite_color":  score_to_color(composite),
        "dhci":             dhci,
        "dhri":             dhri,
        "dhpi":             dhpi,
        "dhdi":             dhdi,
        "dhpw":             dhpw,
        "computed_at":      datetime.utcnow().isoformat() + "Z",
    }
    cache_set(cache_key, result)
    return result


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@router.get("/markets", summary="All markets with sub-index scores")
async def get_all_markets(
    region: Optional[str] = Query(None, description="Filter: us | emea | apac | latam"),
):
    """
    Returns the full index for all tracked markets.
    Sorted by composite score descending (most constrained first).
    Cached for 1 hour.
    """
    cache_key = f"all_markets:{region or 'all'}:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    pool = await get_pool()
    markets = TRACKED_MARKETS
    if region:
        markets = [m for m in markets if m["region"] == region.lower()]

    results = await asyncio.gather(*[
        calculate_market_index(pool, m) for m in markets
    ])

    # Sort by composite score descending (most constrained first)
    results = sorted(
        [r for r in results],
        key=lambda x: x["composite_score"] or 0,
        reverse=True
    )

    response = {
        "count": len(results),
        "region_filter": region,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "markets": results,
    }
    cache_set(cache_key, response)
    return response


@router.get("/composite", summary="Global DHI composite score")
async def get_composite():
    """
    Returns the global DHI composite score — weighted average across all markets.
    This is the headline number cited by media.
    """
    cache_key = f"composite:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    pool = await get_pool()
    results = await asyncio.gather(*[
        calculate_market_index(pool, m) for m in TRACKED_MARKETS
    ])

    # Global composite = average of market composites (weighted by total_mw eventually)
    valid_scores = [r["composite_score"] for r in results if r["composite_score"] is not None]
    global_score = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else None

    # Global vacancy — average of DHCI vacancy_pct
    vacancies = [r["dhci"].get("vacancy_pct") for r in results if r["dhci"].get("vacancy_pct") is not None]
    global_vacancy = round(sum(vacancies) / len(vacancies), 2) if vacancies else None

    # Total global MW tracked
    total_mw = sum(r["dhci"].get("total_mw", 0) or 0 for r in results)
    total_pipeline = sum(r["dhpi"].get("pipeline_mw", 0) or 0 for r in results)

    response = {
        "issue": f"{date.today().strftime('%B %Y')} Issue",
        "composite_score": global_score,
        "composite_label": score_to_label(global_score),
        "composite_color": score_to_color(global_score),
        "global_vacancy_pct": global_vacancy,
        "total_tracked_mw": round(total_mw, 1),
        "total_pipeline_mw": round(total_pipeline, 1),
        "markets_covered": len(TRACKED_MARKETS),
        "markets_with_data": len(valid_scores),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "citation": f'According to the DC Hub Global Data Center Index (GDCI), the global composite score reached {global_score} in {date.today().strftime("%B %Y")}, indicating a {score_to_label(global_score).lower()} environment. Source: DC Hub GDCI, dchub.cloud/index',
    }
    cache_set(cache_key, response)
    return response


@router.get("/market/{market_id}", summary="Single market detail")
async def get_market(market_id: str):
    """
    Full sub-index breakdown for a single market.
    """
    market = next((m for m in TRACKED_MARKETS if m["id"] == market_id.lower()), None)
    if not market:
        valid = [m["id"] for m in TRACKED_MARKETS]
        raise HTTPException(status_code=404, detail=f"Market '{market_id}' not found. Valid: {valid}")

    pool = await get_pool()
    return await calculate_market_index(pool, market)


@router.get("/regions", summary="Regional aggregate scores")
async def get_regions():
    """
    US / EMEA / APAC / LATAM aggregate index scores.
    """
    cache_key = f"regions:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    pool = await get_pool()
    all_results = await asyncio.gather(*[
        calculate_market_index(pool, m) for m in TRACKED_MARKETS
    ])

    regions = {}
    for r in all_results:
        reg = r["region"]
        if reg not in regions:
            regions[reg] = {"scores": [], "vacancies": [], "markets": []}
        if r["composite_score"] is not None:
            regions[reg]["scores"].append(r["composite_score"])
        if r["dhci"].get("vacancy_pct") is not None:
            regions[reg]["vacancies"].append(r["dhci"]["vacancy_pct"])
        regions[reg]["markets"].append(r["market_name"])

    result = {}
    for reg, data in regions.items():
        scores = data["scores"]
        vacancies = data["vacancies"]
        avg_score = round(sum(scores) / len(scores), 1) if scores else None
        result[reg] = {
            "region": reg.upper(),
            "composite_score": avg_score,
            "composite_label": score_to_label(avg_score),
            "composite_color": score_to_color(avg_score),
            "avg_vacancy_pct": round(sum(vacancies) / len(vacancies), 2) if vacancies else None,
            "markets": data["markets"],
            "market_count": len(data["markets"]),
        }

    response = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "regions": result,
    }
    cache_set(cache_key, response)
    return response


@router.get("/citation/{market_id}", summary="Pre-formatted citation string for media")
async def get_citation(market_id: str):
    """
    Returns a pre-formatted citation string for journalists and analysts.
    e.g. 'According to the DC Hub GDCI, Northern Virginia vacancy reached 1.6%...'
    """
    market = next((m for m in TRACKED_MARKETS if m["id"] == market_id.lower()), None)
    if not market:
        raise HTTPException(status_code=404, detail=f"Unknown market: {market_id}")

    pool = await get_pool()
    data = await calculate_market_index(pool, market)
    dhci = data.get("dhci", {})
    dhri = data.get("dhri", {})

    vac = dhci.get("vacancy_pct")
    rate = dhri.get("rate_per_kw")
    score = data.get("composite_score")
    label = data.get("composite_label", "")
    month = date.today().strftime("%B %Y")

    parts = [f'According to the DC Hub Global Data Center Index (GDCI),']
    parts.append(f'{market["name"]} recorded a DHI score of {score}, indicating a {label.lower()} environment in {month}.')
    if vac is not None:
        parts.append(f'Vacancy stands at {vac}%.')
    if rate is not None:
        parts.append(f'Average colocation rate is ${rate}/kW/month.')
    parts.append(f'Source: DC Hub GDCI, dchub.cloud/index')

    return {
        "market_id": market_id,
        "market_name": market["name"],
        "citation": " ".join(parts),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


@router.post("/admin/refresh", summary="Force cache refresh (admin)")
async def refresh_cache(secret: str = Query(...)):
    """Clears the in-memory cache. Requires ADMIN_SECRET env var."""
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")
    cache_invalidate()
    return {"cleared": True, "at": datetime.utcnow().isoformat() + "Z"}


@router.get("/health", summary="Health check")
async def health():
    try:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
        return {"status": "ok", "db": "connected", "ts": datetime.utcnow().isoformat() + "Z"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})
