"""
DC Hub Global Data Center Index (GDCI) — Flask Blueprint
=========================================================
Drop into your Railway backend directory alongside main.py.

Register in main.py:
    from index_api import index_bp
    app.register_blueprint(index_bp)

Schema assumptions — adjust the TABLE_* / COL_* constants below:
    facilities      → id, name, city, country, region, total_mw, available_mw, status
    transactions    → id, market, deal_date, deal_value_usd, facility_mw
    market_intel    → id, market, avg_rate_per_kw, recorded_at
    substations     → id, city, country, capacity_mva, available_mva
"""

import os
import time
import logging
from datetime import datetime, date
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

logger = logging.getLogger(__name__)

index_bp = Blueprint("index", __name__, url_prefix="/api/index")

# ─────────────────────────────────────────────
# SCHEMA CONSTANTS — adjust to match your tables
# ─────────────────────────────────────────────
TABLE_FACILITIES   = "facilities"
TABLE_TRANSACTIONS = "transactions"
TABLE_MARKET_INTEL = "market_intelligence"
TABLE_SUBSTATIONS  = "substations"

COL_FAC_MARKET     = "city"           # column used to filter by market/city name
COL_FAC_COUNTRY    = "country"
COL_FAC_TOTAL_MW   = "total_mw"
COL_FAC_AVAIL_MW   = "available_mw"
COL_FAC_STATUS     = "status"         # 'operational', 'under_construction', etc.

COL_TXN_MARKET     = "market"
COL_TXN_DATE       = "deal_date"
COL_TXN_VALUE      = "deal_value_usd"
COL_TXN_MW         = "facility_mw"

COL_MI_MARKET      = "market"
COL_MI_RATE        = "avg_rate_per_kw"
COL_MI_DATE        = "recorded_at"

COL_SUB_CITY       = "city"
COL_SUB_COUNTRY    = "country"
COL_SUB_CAPACITY   = "capacity_mva"
COL_SUB_AVAILABLE  = "available_mva"

# Sub-index weights (must sum to 1.0)
WEIGHTS = {
    "dhci": 0.30,
    "dhri": 0.25,
    "dhpi": 0.20,
    "dhdi": 0.15,
    "dhpw": 0.10,
}

TRACKED_MARKETS = [
    {"id": "nova", "name": "Northern Virginia, US", "region": "us",   "country": "US", "city": "Ashburn"},
    {"id": "dal",  "name": "Dallas/Fort Worth, US", "region": "us",   "country": "US", "city": "Dallas"},
    {"id": "phx",  "name": "Phoenix, US",           "region": "us",   "country": "US", "city": "Phoenix"},
    {"id": "chi",  "name": "Chicago, US",           "region": "us",   "country": "US", "city": "Chicago"},
    {"id": "sv",   "name": "Silicon Valley, US",    "region": "us",   "country": "US", "city": "San Jose"},
    {"id": "nyc",  "name": "New York/NJ, US",       "region": "us",   "country": "US", "city": "New York"},
    {"id": "atl",  "name": "Atlanta, US",           "region": "us",   "country": "US", "city": "Atlanta"},
    {"id": "lhr",  "name": "London, UK",            "region": "emea", "country": "GB", "city": "London"},
    {"id": "fra",  "name": "Frankfurt, Germany",    "region": "emea", "country": "DE", "city": "Frankfurt"},
    {"id": "ams",  "name": "Amsterdam, Netherlands","region": "emea", "country": "NL", "city": "Amsterdam"},
    {"id": "par",  "name": "Paris, France",         "region": "emea", "country": "FR", "city": "Paris"},
    {"id": "sin",  "name": "Singapore",             "region": "apac", "country": "SG", "city": "Singapore"},
    {"id": "tyo",  "name": "Tokyo, Japan",          "region": "apac", "country": "JP", "city": "Tokyo"},
    {"id": "syd",  "name": "Sydney, Australia",     "region": "apac", "country": "AU", "city": "Sydney"},
    {"id": "bom",  "name": "Mumbai, India",         "region": "apac", "country": "IN", "city": "Mumbai"},
    {"id": "yyz",  "name": "Toronto, Canada",       "region": "us",   "country": "CA", "city": "Toronto"},
    {"id": "gru",  "name": "São Paulo, Brazil",     "region": "latam","country": "BR", "city": "Sao Paulo"},
    {"id": "mex",  "name": "Mexico City, Mexico",   "region": "latam","country": "MX", "city": "Mexico City"},
]

# ─────────────────────────────────────────────
# DATABASE — reuses your existing DATABASE_URL
# ─────────────────────────────────────────────
_conn_pool = None

def get_pool():
    global _conn_pool
    if _conn_pool is None:
        db_url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if db_url:
            _conn_pool = pg_pool.ThreadedConnectionPool(1, 5, dsn=db_url)
    return _conn_pool

def query(sql, params=None):
    """Execute a query and return all rows as dicts."""
    pool = get_pool()
    if pool is None:
        return []
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Index query error: {e}")
        return []
    finally:
        pool.putconn(conn)

def scalar(sql, params=None):
    """Return first column of first row."""
    rows = query(sql, params)
    if rows:
        return list(rows[0].values())[0]
    return None

# ─────────────────────────────────────────────
# SIMPLE IN-MEMORY CACHE (1 hour TTL)
# ─────────────────────────────────────────────
_cache = {}
CACHE_TTL = 3600

def cache_get(key):
    e = _cache.get(key)
    if e and time.time() - e["ts"] < CACHE_TTL:
        return e["val"]
    return None

def cache_set(key, val):
    _cache[key] = {"val": val, "ts": time.time()}

def cache_clear():
    _cache.clear()

# ─────────────────────────────────────────────
# SCORE HELPERS
# ─────────────────────────────────────────────
def score_label(s):
    if s is None: return "Insufficient Data"
    if s < 40:    return "Buyer's Market"
    if s < 60:    return "Balanced"
    if s < 75:    return "Constrained"
    return "Critical"

def score_color(s):
    if s is None: return "gray"
    if s < 40:    return "green"
    if s < 60:    return "amber"
    if s < 75:    return "purple"
    return "red"

# ─────────────────────────────────────────────
# SUB-INDEX CALCULATORS
# ─────────────────────────────────────────────
def calc_dhci(market):
    """Capacity Index — vacancy % from facility table."""
    try:
        city = market["city"]
        country = market["country"]
        rows = query(f"""
            SELECT
                SUM({COL_FAC_TOTAL_MW})  AS total_mw,
                SUM({COL_FAC_AVAIL_MW})  AS avail_mw
            FROM {TABLE_FACILITIES}
            WHERE ({COL_FAC_MARKET} ILIKE %s OR {COL_FAC_COUNTRY} = %s)
              AND {COL_FAC_STATUS} = 'operational'
        """, (f"%{city}%", country))

        row = rows[0] if rows else {}
        total = float(row.get("total_mw") or 0)
        avail = float(row.get("avail_mw") or 0)
        if total == 0:
            return {"value": None, "vacancy_pct": None, "total_mw": 0, "available_mw": 0}

        vacancy = (avail / total) * 100
        score = max(0, min(100, (1 - vacancy / 10) * 100))
        return {
            "value": round(score, 1),
            "vacancy_pct": round(vacancy, 2),
            "total_mw": round(total, 1),
            "available_mw": round(avail, 1),
        }
    except Exception as e:
        logger.warning(f"DHCI error ({market['id']}): {e}")
        return {"value": None, "vacancy_pct": None, "total_mw": 0, "available_mw": 0}


def calc_dhri(market):
    """Rate Index — avg $/kW/month, indexed to Jan 2025 = 100."""
    try:
        city = market["city"]
        current = scalar(f"""
            SELECT AVG({COL_MI_RATE}) FROM {TABLE_MARKET_INTEL}
            WHERE {COL_MI_MARKET} ILIKE %s
              AND {COL_MI_DATE} >= NOW() - INTERVAL '45 days'
        """, (f"%{city}%",))

        base = scalar(f"""
            SELECT AVG({COL_MI_RATE}) FROM {TABLE_MARKET_INTEL}
            WHERE {COL_MI_MARKET} ILIKE %s
              AND {COL_MI_DATE} BETWEEN '2025-01-01' AND '2025-01-31'
        """, (f"%{city}%",))

        cur_rate = float(current or 0)
        base_rate = float(base or 0)
        if cur_rate == 0 or base_rate == 0:
            return {"value": None, "rate_per_kw": cur_rate or None, "index_value": None}

        index_val = (cur_rate / base_rate) * 100
        score = min(100, max(0, (index_val - 80) / 0.6))
        return {
            "value": round(score, 1),
            "rate_per_kw": round(cur_rate, 2),
            "index_value": round(index_val, 1),
        }
    except Exception as e:
        logger.warning(f"DHRI error ({market['id']}): {e}")
        return {"value": None, "rate_per_kw": None, "index_value": None}


def calc_dhpi(market):
    """Pipeline Index — MW under construction / total operational."""
    try:
        city = market["city"]
        country = market["country"]
        pipeline = scalar(f"""
            SELECT SUM({COL_FAC_TOTAL_MW}) FROM {TABLE_FACILITIES}
            WHERE ({COL_FAC_MARKET} ILIKE %s OR {COL_FAC_COUNTRY} = %s)
              AND {COL_FAC_STATUS} IN ('under_construction','planned','development')
        """, (f"%{city}%", country)) or 0

        operational = scalar(f"""
            SELECT SUM({COL_FAC_TOTAL_MW}) FROM {TABLE_FACILITIES}
            WHERE ({COL_FAC_MARKET} ILIKE %s OR {COL_FAC_COUNTRY} = %s)
              AND {COL_FAC_STATUS} = 'operational'
        """, (f"%{city}%", country)) or 1

        p = float(pipeline)
        o = float(operational)
        ratio = (p / o) * 100
        return {
            "value": round(min(100, ratio * 2), 1),
            "pipeline_mw": round(p, 1),
            "operational_mw": round(o, 1),
            "pipeline_ratio_pct": round(ratio, 2),
        }
    except Exception as e:
        logger.warning(f"DHPI error ({market['id']}): {e}")
        return {"value": None, "pipeline_mw": None, "pipeline_ratio_pct": None}


def calc_dhdi(market):
    """Demand Index — net MW absorbed via transactions (trailing 90 days)."""
    try:
        city = market["city"]
        mw = scalar(f"""
            SELECT SUM({COL_TXN_MW}) FROM {TABLE_TRANSACTIONS}
            WHERE {COL_TXN_MARKET} ILIKE %s
              AND {COL_TXN_DATE} >= NOW() - INTERVAL '90 days'
              AND {COL_TXN_VALUE} > 0
        """, (f"%{city}%",)) or 0

        score = min(100, (float(mw) / 1000) * 100)
        return {"value": round(score, 1), "absorbed_mw_90d": round(float(mw), 1)}
    except Exception as e:
        logger.warning(f"DHDI error ({market['id']}): {e}")
        return {"value": None, "absorbed_mw_90d": None}


def calc_dhpw(market):
    """Power Index — grid headroom score from substations table."""
    try:
        city = market["city"]
        country = market["country"]
        rows = query(f"""
            SELECT SUM({COL_SUB_CAPACITY}) AS total, SUM({COL_SUB_AVAILABLE}) AS avail
            FROM {TABLE_SUBSTATIONS}
            WHERE {COL_SUB_CITY} ILIKE %s OR {COL_SUB_COUNTRY} = %s
        """, (f"%{city}%", country))

        row = rows[0] if rows else {}
        total = float(row.get("total") or 0)
        avail = float(row.get("avail") or 0)
        if total == 0:
            return {"value": None, "available_mva": None, "headroom_pct": None}

        headroom = (avail / total) * 100
        return {
            "value": round(headroom, 1),
            "available_mva": round(avail, 1),
            "total_mva": round(total, 1),
            "headroom_pct": round(headroom, 2),
        }
    except Exception as e:
        logger.warning(f"DHPW error ({market['id']}): {e}")
        return {"value": None, "available_mva": None, "headroom_pct": None}


def compute_composite(sub):
    total_w = 0
    score = 0
    for key, w in WEIGHTS.items():
        val = sub.get(key, {}).get("value")
        if val is not None:
            score += val * w
            total_w += w
    if total_w == 0:
        return None
    return round(score / total_w, 1)


def calculate_market(market):
    cache_key = f"mkt:{market['id']}:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    dhci = calc_dhci(market)
    dhri = calc_dhri(market)
    dhpi = calc_dhpi(market)
    dhdi = calc_dhdi(market)
    dhpw = calc_dhpw(market)

    sub = {"dhci": dhci, "dhri": dhri, "dhpi": dhpi, "dhdi": dhdi, "dhpw": dhpw}
    composite = compute_composite(sub)

    result = {
        "market_id":       market["id"],
        "market_name":     market["name"],
        "region":          market["region"],
        "country":         market["country"],
        "composite_score": composite,
        "composite_label": score_label(composite),
        "composite_color": score_color(composite),
        "dhci": dhci, "dhri": dhri, "dhpi": dhpi,
        "dhdi": dhdi, "dhpw": dhpw,
        "computed_at":     datetime.utcnow().isoformat() + "Z",
    }
    cache_set(cache_key, result)
    return result


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@index_bp.route("/health")
def health():
    try:
        pool = get_pool()
        if pool:
            scalar("SELECT 1")
            return jsonify({"status": "ok", "db": "connected", "ts": datetime.utcnow().isoformat() + "Z"})
        return jsonify({"status": "ok", "db": "no pool", "ts": datetime.utcnow().isoformat() + "Z"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


@index_bp.route("/markets")
def get_markets():
    region = request.args.get("region", "").lower()
    cache_key = f"all:{region or 'all'}:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    markets = TRACKED_MARKETS
    if region:
        markets = [m for m in markets if m["region"] == region]

    results = [calculate_market(m) for m in markets]
    results.sort(key=lambda x: x["composite_score"] or 0, reverse=True)

    response = {
        "count": len(results),
        "region_filter": region or None,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "markets": results,
    }
    cache_set(cache_key, response)
    return jsonify(response)


@index_bp.route("/composite")
def get_composite():
    cache_key = f"composite:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    results = [calculate_market(m) for m in TRACKED_MARKETS]
    valid = [r["composite_score"] for r in results if r["composite_score"] is not None]
    global_score = round(sum(valid) / len(valid), 1) if valid else None

    vacancies = [r["dhci"].get("vacancy_pct") for r in results if r["dhci"].get("vacancy_pct") is not None]
    global_vacancy = round(sum(vacancies) / len(vacancies), 2) if vacancies else None

    total_mw = sum(r["dhci"].get("total_mw") or 0 for r in results)
    total_pipe = sum(r["dhpi"].get("pipeline_mw") or 0 for r in results)
    month = date.today().strftime("%B %Y")

    response = {
        "issue": f"{month} Issue",
        "composite_score": global_score,
        "composite_label": score_label(global_score),
        "composite_color": score_color(global_score),
        "global_vacancy_pct": global_vacancy,
        "total_tracked_mw": round(total_mw, 1),
        "total_pipeline_mw": round(total_pipe, 1),
        "markets_covered": len(TRACKED_MARKETS),
        "markets_with_data": len(valid),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "citation": f'According to the DC Hub Global Data Center Index (GDCI), the global composite score reached {global_score} in {month}, indicating a {score_label(global_score).lower()} environment. Source: DC Hub GDCI, dchub.cloud/index',
    }
    cache_set(cache_key, response)
    return jsonify(response)


@index_bp.route("/market/<market_id>")
def get_market(market_id):
    market = next((m for m in TRACKED_MARKETS if m["id"] == market_id.lower()), None)
    if not market:
        return jsonify({"error": f"Unknown market: {market_id}",
                        "valid": [m["id"] for m in TRACKED_MARKETS]}), 404
    return jsonify(calculate_market(market))


@index_bp.route("/regions")
def get_regions():
    cache_key = f"regions:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    results = [calculate_market(m) for m in TRACKED_MARKETS]
    regions = {}
    for r in results:
        reg = r["region"]
        if reg not in regions:
            regions[reg] = {"scores": [], "vacancies": [], "names": []}
        if r["composite_score"] is not None:
            regions[reg]["scores"].append(r["composite_score"])
        if r["dhci"].get("vacancy_pct") is not None:
            regions[reg]["vacancies"].append(r["dhci"]["vacancy_pct"])
        regions[reg]["names"].append(r["market_name"])

    out = {}
    for reg, d in regions.items():
        avg = round(sum(d["scores"]) / len(d["scores"]), 1) if d["scores"] else None
        out[reg] = {
            "region": reg.upper(),
            "composite_score": avg,
            "composite_label": score_label(avg),
            "composite_color": score_color(avg),
            "avg_vacancy_pct": round(sum(d["vacancies"]) / len(d["vacancies"]), 2) if d["vacancies"] else None,
            "markets": d["names"],
            "market_count": len(d["names"]),
        }

    response = {"generated_at": datetime.utcnow().isoformat() + "Z", "regions": out}
    cache_set(cache_key, response)
    return jsonify(response)


@index_bp.route("/citation/<market_id>")
def get_citation(market_id):
    market = next((m for m in TRACKED_MARKETS if m["id"] == market_id.lower()), None)
    if not market:
        return jsonify({"error": f"Unknown market: {market_id}"}), 404

    data = calculate_market(market)
    vac  = data["dhci"].get("vacancy_pct")
    rate = data["dhri"].get("rate_per_kw")
    score = data["composite_score"]
    month = date.today().strftime("%B %Y")

    parts = [f'According to the DC Hub Global Data Center Index (GDCI),',
             f'{market["name"]} recorded a DHI score of {score}, indicating a {score_label(score).lower()} environment in {month}.']
    if vac is not None:
        parts.append(f'Vacancy stands at {vac}%.')
    if rate is not None:
        parts.append(f'Average colocation rate is ${rate}/kW/month.')
    parts.append('Source: DC Hub GDCI, dchub.cloud/index')

    return jsonify({
        "market_id": market_id,
        "market_name": market["name"],
        "citation": " ".join(parts),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })


@index_bp.route("/admin/refresh", methods=["POST"])
def refresh_cache():
    secret = request.args.get("secret", "")
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return jsonify({"error": "Invalid secret"}), 403
    cache_clear()
    return jsonify({"cleared": True, "at": datetime.utcnow().isoformat() + "Z"})
