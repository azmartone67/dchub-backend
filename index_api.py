"""
DC Hub Global Data Center Index (GDCI) — Self-Configuring Flask Blueprint
==========================================================================
After this version is deployed, you NEVER edit this file again.
All configuration lives in the `gdci_config` table in Neon.

Register in main.py:
    from index_api import index_bp
    app.register_blueprint(index_bp)

On first boot this file:
  1. Creates the gdci_config table if it doesn't exist
  2. Seeds default config from your known tables
  3. Auto-detects which tables exist and enables them
  4. Runs forever reading config from Neon — zero GitHub edits needed

To update config without touching GitHub:
  POST /api/index/admin/config  (X-Admin-Key header)

To add a new market:
  POST /api/index/admin/markets (X-Admin-Key header)

To see all data sources and their status:
  GET  /api/index/admin/sources (X-Admin-Key header)
"""

import os
import time
import logging
from datetime import datetime, date
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

index_bp = Blueprint("index", __name__, url_prefix="/api/index")

# ─────────────────────────────────────────────
# DATABASE — reuses main app's shared pool via db_utils
# No separate pool = no pool exhaustion
# ─────────────────────────────────────────────

def query(sql, params=None):
    """Run a SELECT, return list of dicts."""
    try:
        from db_utils import get_db
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Index query error: {e} | SQL: {sql[:120]}")
        return []

def execute(sql, params=None):
    """Run INSERT/UPDATE/CREATE."""
    try:
        from db_utils import get_db
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Index execute error: {e}")
        return False

def scalar(sql, params=None):
    rows = query(sql, params)
    if rows:
        return list(rows[0].values())[0]
    return None

def table_exists(table_name):
    result = scalar(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=%s AND table_schema='public')",
        (table_name,)
    )
    return bool(result)

def column_exists(table_name, col_name):
    result = scalar("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.columns
            WHERE table_name=%s AND column_name=%s AND table_schema='public'
        )
    """, (table_name, col_name))
    return bool(result)

# ─────────────────────────────────────────────
# CACHE
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

def cache_clear(prefix=""):
    keys = [k for k in list(_cache.keys()) if k.startswith(prefix)]
    for k in keys:
        del _cache[k]

# ─────────────────────────────────────────────
# DEFAULT CONFIG — seeded into Neon on first boot
# Change these by POSTing to /api/index/admin/config
# ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Sub-index weights (must sum to 1.0)
    "weights_dhci": "0.30",
    "weights_dhri": "0.25",
    "weights_dhpi": "0.20",
    "weights_dhdi": "0.15",
    "weights_dhpw": "0.10",

    # facilities table (confirmed schema)
    "fac_table":              "facilities",
    "fac_city_col":           "city",
    "fac_country_col":        "country",
    "fac_mw_col":             "power_mw",
    "fac_status_col":         "status",
    "fac_status_operational": "active,Operational",
    "fac_status_pipeline":    "Under Construction,Construction,Planned,Planning,Announced,announced,planned",

    # transactions table (confirmed schema)
    "txn_table":      "deals",
    "txn_market_col": "market",
    "txn_date_col":   "date",
    "txn_value_col":  "value",
    "txn_mw_col":     "mw",

    # market_intelligence (DHRI — enable when table exists)
    "mi_table":      "market_intelligence",
    "mi_market_col": "market",
    "mi_rate_col":   "avg_rate_per_kw",
    "mi_date_col":   "recorded_at",
    "mi_enabled":    "true",

    # discovered_power_plants (energy auto-discovery)
    "power_table":    "discovered_power_plants",
    "power_city_col": "city",
    "power_state_col":"state",
    "power_mw_col":   "capacity_mw",
    "power_enabled":  "true",           # confirmed exists

    # discovered_transmission_lines (energy auto-discovery)
    "tx_table":    "discovered_transmission_lines",
    "tx_city_col": "city",
    "tx_enabled":  "true",              # confirmed exists

    # substations (Land & Power tool)
    "sub_table":      "substations",
    "sub_city_col":   "city",
    "sub_country_col":"country",
    "sub_cap_col":    "capacity_mva",
    "sub_avail_col":  "available_mva",
    "sub_enabled":    "true",           # confirmed exists

    # fiber routes
    "fiber_table":    "fiber_routes",
    "fiber_city_col": "city",
    "fiber_enabled":  "true",           # confirmed exists

    # KMZ / energy discovery export
    "kmz_enabled":    "false",
}

def init_config_table():
    execute("""
        CREATE TABLE IF NOT EXISTS gdci_config (
            key        VARCHAR(100) PRIMARY KEY,
            value      TEXT NOT NULL,
            description TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    for k, v in DEFAULT_CONFIG.items():
        execute("""
            INSERT INTO gdci_config (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO NOTHING
        """, (k, v))

def get_config():
    cached = cache_get("__config__")
    if cached:
        return cached
    rows = query("SELECT key, value FROM gdci_config")
    cfg = {r["key"]: r["value"] for r in rows} if rows else {}
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    _cache["__config__"] = {"val": cfg, "ts": time.time()}
    return cfg

def set_config(key, value, description=None):
    execute("""
        INSERT INTO gdci_config (key, value, description, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (key) DO UPDATE
            SET value=%s, description=COALESCE(%s, gdci_config.description), updated_at=NOW()
    """, (key, value, description, value, description))
    cache_clear("__config__")

# ─────────────────────────────────────────────
# MARKETS
# ─────────────────────────────────────────────
DEFAULT_MARKETS = [
    {"id":"nova","name":"Northern Virginia, US","region":"us",  "country":"US","city":"Ashburn",     "state":"VA"},
    {"id":"dal", "name":"Dallas/Fort Worth, US","region":"us",  "country":"US","city":"Dallas",      "state":"TX"},
    {"id":"phx", "name":"Phoenix, US",          "region":"us",  "country":"US","city":"Phoenix",     "state":"AZ"},
    {"id":"chi", "name":"Chicago, US",          "region":"us",  "country":"US","city":"Chicago",     "state":"IL"},
    {"id":"sv",  "name":"Silicon Valley, US",   "region":"us",  "country":"US","city":"San Jose",    "state":"CA"},
    {"id":"nyc", "name":"New York/NJ, US",      "region":"us",  "country":"US","city":"New York",    "state":"NY"},
    {"id":"atl", "name":"Atlanta, US",          "region":"us",  "country":"US","city":"Atlanta",     "state":"GA"},
    {"id":"sea", "name":"Seattle, US",          "region":"us",  "country":"US","city":"Seattle",     "state":"WA"},
    {"id":"den", "name":"Denver, US",           "region":"us",  "country":"US","city":"Denver",      "state":"CO"},
    {"id":"lhr", "name":"London, UK",           "region":"emea","country":"GB","city":"London",      "state":None},
    {"id":"fra", "name":"Frankfurt, Germany",   "region":"emea","country":"DE","city":"Frankfurt",   "state":None},
    {"id":"ams", "name":"Amsterdam, Netherlands","region":"emea","country":"NL","city":"Amsterdam",  "state":None},
    {"id":"par", "name":"Paris, France",        "region":"emea","country":"FR","city":"Paris",       "state":None},
    {"id":"dub", "name":"Dublin, Ireland",      "region":"emea","country":"IE","city":"Dublin",      "state":None},
    {"id":"sin", "name":"Singapore",            "region":"apac","country":"SG","city":"Singapore",   "state":None},
    {"id":"tyo", "name":"Tokyo, Japan",         "region":"apac","country":"JP","city":"Tokyo",       "state":None},
    {"id":"syd", "name":"Sydney, Australia",    "region":"apac","country":"AU","city":"Sydney",      "state":None},
    {"id":"bom", "name":"Mumbai, India",        "region":"apac","country":"IN","city":"Mumbai",      "state":None},
    {"id":"yyz", "name":"Toronto, Canada",      "region":"us",  "country":"CA","city":"Toronto",     "state":"ON"},
    {"id":"gru", "name":"São Paulo, Brazil",    "region":"latam","country":"BR","city":"Sao Paulo",  "state":None},
    {"id":"mex", "name":"Mexico City, Mexico",  "region":"latam","country":"MX","city":"Mexico City","state":None},
]

def get_markets():
    if table_exists("gdci_markets"):
        rows = query("SELECT * FROM gdci_markets WHERE enabled=true ORDER BY region, name")
        if rows:
            return rows
    return DEFAULT_MARKETS

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

def calc_dhci(market, cfg):
    """Capacity Index — facility counts + MW from facilities table."""
    try:
        city    = market["city"]
        country = market["country"]
        tbl     = cfg["fac_table"]
        c_city  = cfg["fac_city_col"]
        c_cty   = cfg["fac_country_col"]
        c_mw    = cfg["fac_mw_col"]
        c_stat  = cfg["fac_status_col"]
        op_val  = cfg["fac_status_operational"]
        pipe_vals = [v.strip().lower() for v in cfg["fac_status_pipeline"].split(",")]

        total = query(f"""
            SELECT COUNT(*) AS n, SUM({c_mw}) AS mw FROM {tbl}
            WHERE ({c_city} ILIKE %s OR {c_cty} = %s)
        """, (f"%{city}%", country))

        op_vals = [v.strip() for v in op_val.split(",")]
        operational = query(f"""
            SELECT COUNT(*) AS n, SUM({c_mw}) AS mw FROM {tbl}
            WHERE ({c_city} ILIKE %s OR {c_cty} = %s)
              AND {c_stat} = ANY(%s)
        """, (f"%{city}%", country, op_vals))

        pipeline = query(f"""
            SELECT COUNT(*) AS n, SUM({c_mw}) AS mw FROM {tbl}
            WHERE ({c_city} ILIKE %s OR {c_cty} = %s)
              AND LOWER({c_stat}) = ANY(%s)
        """, (f"%{city}%", country, pipe_vals))

        t = total[0]       if total       else {}
        o = operational[0] if operational else {}
        p = pipeline[0]    if pipeline    else {}

        total_n  = int(t.get("n")  or 0)
        op_n     = int(o.get("n")  or 0)
        total_mw = float(t.get("mw") or 0)
        op_mw    = float(o.get("mw") or 0)
        pipe_mw  = float(p.get("mw") or 0)

        if total_n == 0:
            return {"value": None, "vacancy_pct": None, "total_mw": 0,
                    "operational_count": 0, "total_count": 0}

        non_op  = total_n - op_n
        vacancy = (non_op / total_n) * 100
        score   = max(0, min(100, (1 - vacancy / 50) * 100))

        return {
            "value":             round(score, 1),
            "vacancy_pct":       round(vacancy, 2),
            "total_mw":          round(total_mw, 1),
            "operational_mw":    round(op_mw, 1),
            "pipeline_mw":       round(pipe_mw, 1),
            "total_count":       total_n,
            "operational_count": op_n,
        }
    except Exception as e:
        logger.warning(f"DHCI error ({market['id']}): {e}")
        return {"value": None, "vacancy_pct": None, "total_mw": 0}


def calc_dhri(market, cfg):
    """Rate Index — from market_intelligence table. Null until table exists."""
    try:
        if cfg.get("mi_enabled","false").lower() != "true":
            return {"value": None, "rate_per_kw": None, "index_value": None,
                    "note": "Activate: POST /api/index/admin/config {\"mi_enabled\":\"true\"}"}
        if not table_exists(cfg["mi_table"]):
            return {"value": None, "rate_per_kw": None, "index_value": None}

        city   = market["city"]
        tbl    = cfg["mi_table"]
        c_mkt  = cfg["mi_market_col"]
        c_rate = cfg["mi_rate_col"]
        c_date = cfg["mi_date_col"]

        current = scalar(f"""
            SELECT AVG({c_rate}) FROM {tbl}
            WHERE {c_mkt} ILIKE %s AND {c_date} >= NOW() - INTERVAL '45 days'
        """, (f"%{city}%",))

        base = scalar(f"""
            SELECT AVG({c_rate}) FROM {tbl}
            WHERE {c_mkt} ILIKE %s
              AND {c_date} BETWEEN '2025-01-01' AND '2025-01-31'
        """, (f"%{city}%",))

        cur_rate  = float(current or 0)
        base_rate = float(base or 0)
        if cur_rate == 0 or base_rate == 0:
            return {"value": None, "rate_per_kw": cur_rate or None, "index_value": None}

        index_val = (cur_rate / base_rate) * 100
        score     = min(100, max(0, (index_val - 80) / 0.6))
        return {"value": round(score,1), "rate_per_kw": round(cur_rate,2), "index_value": round(index_val,1)}
    except Exception as e:
        logger.warning(f"DHRI error ({market['id']}): {e}")
        return {"value": None, "rate_per_kw": None, "index_value": None}


def calc_dhpi(market, cfg):
    """Pipeline Index — pipeline MW / operational MW."""
    try:
        city      = market["city"]
        country   = market["country"]
        tbl       = cfg["fac_table"]
        c_city    = cfg["fac_city_col"]
        c_cty     = cfg["fac_country_col"]
        c_mw      = cfg["fac_mw_col"]
        c_stat    = cfg["fac_status_col"]
        op_val    = cfg["fac_status_operational"]
        pipe_vals = [v.strip().lower() for v in cfg["fac_status_pipeline"].split(",")]

        pipeline = scalar(f"""
            SELECT SUM({c_mw}) FROM {tbl}
            WHERE ({c_city} ILIKE %s OR {c_cty} = %s)
              AND LOWER({c_stat}) = ANY(%s)
        """, (f"%{city}%", country, pipe_vals)) or 0

        op_vals2 = [v.strip() for v in op_val.split(",")]
        operational = scalar(f"""
            SELECT SUM({c_mw}) FROM {tbl}
            WHERE ({c_city} ILIKE %s OR {c_cty} = %s)
              AND {c_stat} = ANY(%s)
        """, (f"%{city}%", country, op_vals2)) or 1

        p = float(pipeline)
        o = float(operational)
        ratio = (p / o) * 100
        return {
            "value":              round(min(100, ratio * 2), 1),
            "pipeline_mw":        round(p, 1),
            "operational_mw":     round(o, 1),
            "pipeline_ratio_pct": round(ratio, 2),
        }
    except Exception as e:
        logger.warning(f"DHPI error ({market['id']}): {e}")
        return {"value": None, "pipeline_mw": None, "pipeline_ratio_pct": None}


def calc_dhdi(market, cfg):
    """Demand Index — MW absorbed from transactions (trailing 90 days)."""
    try:
        city    = market["city"]
        tbl     = cfg["txn_table"]
        c_mkt   = cfg["txn_market_col"]
        c_date  = cfg["txn_date_col"]
        c_value = cfg["txn_value_col"]
        c_mw    = cfg["txn_mw_col"]

        mw = scalar(f"""
            SELECT SUM({c_mw}) FROM {tbl}
            WHERE ({c_mkt} ILIKE %s OR {c_mkt} ILIKE '%%global%%')
              AND {c_date}::date >= NOW()::date - 90
              AND {c_value} > 0
              AND {c_mw} IS NOT NULL
        """, (f"%{city}%",)) or 0

        deal_count = scalar(f"""
            SELECT COUNT(*) FROM {tbl}
            WHERE {c_mkt} ILIKE %s
              AND {c_date}::date >= NOW()::date - 90
              AND {c_value} > 0
        """, (f"%{city}%",)) or 0

        mw_val = float(mw)
        score  = min(100, (mw_val / 500) * 100 + int(deal_count) * 2)
        return {
            "value":           round(score, 1),
            "absorbed_mw_90d": round(mw_val, 1),
            "deal_count_90d":  int(deal_count),
        }
    except Exception as e:
        logger.warning(f"DHDI error ({market['id']}): {e}")
        return {"value": None, "absorbed_mw_90d": None}


def calc_dhpw(market, cfg):
    """
    Power Index — multi-source in priority order:
    1. substations table (Land & Power tool) — direct grid headroom
    2. discovered_power_plants (energy auto-discovery) — capacity proxy
    """
    try:
        city    = market["city"]
        country = market["country"]
        state   = market.get("state")

        # Source 1: substations
        if cfg.get("sub_enabled","false").lower() == "true" and table_exists(cfg["sub_table"]):
            tbl   = cfg["sub_table"]
            c_cty = cfg["sub_city_col"]
            c_cnt = cfg["sub_country_col"]
            c_cap = cfg["sub_cap_col"]
            c_avl = cfg["sub_avail_col"]
            rows  = query(f"""
                SELECT SUM({c_cap}) AS total, SUM({c_avl}) AS avail
                FROM {tbl} WHERE {c_cty} ILIKE %s OR {c_cnt} = %s
            """, (f"%{city}%", country))
            row   = rows[0] if rows else {}
            total = float(row.get("total") or 0)
            avail = float(row.get("avail") or 0)
            if total > 0:
                headroom = (avail / total) * 100
                return {"value": round(headroom,1), "source": "substations",
                        "available_mva": round(avail,1), "total_mva": round(total,1),
                        "headroom_pct": round(headroom,2)}

        # Source 2: discovered_power_plants
        if cfg.get("power_enabled","false").lower() == "true" and table_exists(cfg["power_table"]):
            tbl    = cfg["power_table"]
            c_city = cfg["power_city_col"]
            c_st   = cfg["power_state_col"]
            c_mw   = cfg["power_mw_col"]

            where  = f"({c_city} ILIKE %s"
            params = [f"%{city}%"]
            if state and column_exists(tbl, c_st):
                where  += f" OR {c_st} = %s"
                params.append(state)
            where += ")"

            plant_mw    = scalar(f"SELECT SUM({c_mw}) FROM {tbl} WHERE {where}", params) or 0
            plant_count = scalar(f"SELECT COUNT(*) FROM {tbl} WHERE {where}", params) or 0

            mw_val = float(plant_mw)
            cnt    = int(plant_count)
            if mw_val > 0 or cnt > 0:
                score = min(100, (mw_val / 5000) * 100 + cnt * 0.5)
                return {"value": round(score,1), "source": "discovered_power_plants",
                        "total_mw": round(mw_val,1), "plant_count": cnt}

        return {"value": None, "source": "none",
                "note": "POST /api/index/admin/config {\"power_enabled\":\"true\"} to activate"}
    except Exception as e:
        logger.warning(f"DHPW error ({market['id']}): {e}")
        return {"value": None, "source": "error"}


def calc_connectivity(market, cfg):
    """Supplemental — fiber routes + transmission lines count."""
    result = {"fiber_routes": None, "transmission_lines": None}
    try:
        city  = market["city"]
        state = market.get("state")

        if cfg.get("fiber_enabled","false").lower() == "true" and table_exists(cfg["fiber_table"]):
            n = scalar(f"SELECT COUNT(*) FROM {cfg['fiber_table']} WHERE {cfg['fiber_city_col']} ILIKE %s",
                       (f"%{city}%",))
            result["fiber_routes"] = int(n or 0)

        if cfg.get("tx_enabled","false").lower() == "true" and table_exists(cfg["tx_table"]):
            tbl = cfg["tx_table"]
            c   = cfg["tx_city_col"]
            where  = f"({c} ILIKE %s"
            params = [f"%{city}%"]
            if state and column_exists(tbl, "state"):
                where  += " OR state = %s"
                params.append(state)
            where += ")"
            n = scalar(f"SELECT COUNT(*) FROM {tbl} WHERE {where}", params)
            result["transmission_lines"] = int(n or 0)
    except Exception as e:
        logger.warning(f"Connectivity error ({market['id']}): {e}")
    return result


def compute_composite(sub, cfg):
    weights = {
        "dhci": float(cfg.get("weights_dhci", 0.30)),
        "dhri": float(cfg.get("weights_dhri", 0.25)),
        "dhpi": float(cfg.get("weights_dhpi", 0.20)),
        "dhdi": float(cfg.get("weights_dhdi", 0.15)),
        "dhpw": float(cfg.get("weights_dhpw", 0.10)),
    }
    score   = 0
    total_w = 0
    for key, w in weights.items():
        val = sub.get(key, {}).get("value")
        if val is not None:
            score   += val * w
            total_w += w
    if total_w == 0:
        return None
    return round(score / total_w, 1)


def calculate_market(market):
    cache_key = f"mkt:{market['id']}:{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    cfg  = get_config()
    dhci = calc_dhci(market, cfg)
    dhri = calc_dhri(market, cfg)
    dhpi = calc_dhpi(market, cfg)
    dhdi = calc_dhdi(market, cfg)
    dhpw = calc_dhpw(market, cfg)
    conn = calc_connectivity(market, cfg)

    sub       = {"dhci":dhci,"dhri":dhri,"dhpi":dhpi,"dhdi":dhdi,"dhpw":dhpw}
    composite = compute_composite(sub, cfg)

    result = {
        "market_id":       market["id"],
        "market_name":     market["name"],
        "region":          market["region"],
        "country":         market["country"],
        "composite_score": composite,
        "composite_label": score_label(composite),
        "composite_color": score_color(composite),
        "dhci":            dhci,
        "dhri":            dhri,
        "dhpi":            dhpi,
        "dhdi":            dhdi,
        "dhpw":            dhpw,
        "connectivity":    conn,
        "computed_at":     datetime.utcnow().isoformat() + "Z",
    }
    cache_set(cache_key, result)
    return result


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
def check_admin(req):
    secret   = os.environ.get("ADMIN_SECRET") or os.environ.get("DCHUB_ADMIN_KEY","")
    provided = req.headers.get("X-Admin-Key") or req.args.get("admin_key","")
    return bool(secret and provided == secret)


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@index_bp.route("/health")
def health():
    try:
        scalar("SELECT 1")
        cfg_count = len(query("SELECT key FROM gdci_config") or [])
        return jsonify({"status":"ok","db":"connected","config_keys":cfg_count,
                        "ts":datetime.utcnow().isoformat()+"Z"})
    except Exception as e:
        return jsonify({"status":"error","detail":str(e)}), 503


@index_bp.route("/markets")
def get_markets_route():
    region    = request.args.get("region","").lower()
    cache_key = f"all:{region or 'all'}:{date.today().isoformat()}"
    cached    = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    markets = get_markets()
    if region:
        markets = [m for m in markets if m["region"] == region]

    results = [calculate_market(m) for m in markets]
    results.sort(key=lambda x: x["composite_score"] or 0, reverse=True)

    response = {"count":len(results),"region_filter":region or None,
                "generated_at":datetime.utcnow().isoformat()+"Z","markets":results}
    cache_set(cache_key, response)
    return jsonify(response)


@index_bp.route("/composite")
def get_composite():
    cache_key = f"composite:{date.today().isoformat()}"
    cached    = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    markets = get_markets()
    results = [calculate_market(m) for m in markets]
    valid   = [r["composite_score"] for r in results if r["composite_score"] is not None]
    gs      = round(sum(valid)/len(valid),1) if valid else None

    vacancies = [r["dhci"].get("vacancy_pct") for r in results if r["dhci"].get("vacancy_pct") is not None]
    gv        = round(sum(vacancies)/len(vacancies),2) if vacancies else None
    total_mw  = sum(r["dhci"].get("total_mw") or 0 for r in results)
    total_p   = sum(r["dhpi"].get("pipeline_mw") or 0 for r in results)
    month     = date.today().strftime("%B %Y")

    response = {
        "issue":              f"{month} Issue",
        "composite_score":    gs,
        "composite_label":    score_label(gs),
        "composite_color":    score_color(gs),
        "global_vacancy_pct": gv,
        "total_tracked_mw":   round(total_mw,1),
        "total_pipeline_mw":  round(total_p,1),
        "markets_covered":    len(markets),
        "markets_with_data":  len(valid),
        "generated_at":       datetime.utcnow().isoformat()+"Z",
        "citation":           f'According to the DC Hub Global Data Center Index (GDCI), the global composite score reached {gs} in {month}, indicating a {score_label(gs).lower()} environment. Source: DC Hub GDCI, dchub.cloud/index',
    }
    cache_set(cache_key, response)
    return jsonify(response)


@index_bp.route("/market/<market_id>")
def get_market(market_id):
    markets = get_markets()
    market  = next((m for m in markets if m["id"] == market_id.lower()), None)
    if not market:
        return jsonify({"error":f"Unknown market: {market_id}","valid":[m["id"] for m in markets]}), 404
    return jsonify(calculate_market(market))


@index_bp.route("/regions")
def get_regions():
    cache_key = f"regions:{date.today().isoformat()}"
    cached    = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    results = [calculate_market(m) for m in get_markets()]
    regions = {}
    for r in results:
        reg = r["region"]
        if reg not in regions:
            regions[reg] = {"scores":[],"vacancies":[],"names":[]}
        if r["composite_score"] is not None:
            regions[reg]["scores"].append(r["composite_score"])
        if r["dhci"].get("vacancy_pct") is not None:
            regions[reg]["vacancies"].append(r["dhci"]["vacancy_pct"])
        regions[reg]["names"].append(r["market_name"])

    out = {}
    for reg, d in regions.items():
        avg = round(sum(d["scores"])/len(d["scores"]),1) if d["scores"] else None
        out[reg] = {
            "region":          reg.upper(),
            "composite_score": avg,
            "composite_label": score_label(avg),
            "composite_color": score_color(avg),
            "avg_vacancy_pct": round(sum(d["vacancies"])/len(d["vacancies"]),2) if d["vacancies"] else None,
            "markets":         d["names"],
            "market_count":    len(d["names"]),
        }
    response = {"generated_at":datetime.utcnow().isoformat()+"Z","regions":out}
    cache_set(cache_key, response)
    return jsonify(response)


@index_bp.route("/citation/<market_id>")
def get_citation(market_id):
    markets = get_markets()
    market  = next((m for m in markets if m["id"] == market_id.lower()), None)
    if not market:
        return jsonify({"error":f"Unknown market: {market_id}"}), 404

    data  = calculate_market(market)
    vac   = data["dhci"].get("vacancy_pct")
    rate  = data["dhri"].get("rate_per_kw")
    score = data["composite_score"]
    month = date.today().strftime("%B %Y")

    parts = [f'According to the DC Hub Global Data Center Index (GDCI),',
             f'{market["name"]} recorded a DHI score of {score}, indicating a {score_label(score).lower()} environment in {month}.']
    if vac  is not None: parts.append(f'Vacancy stands at {vac}%.')
    if rate is not None: parts.append(f'Average colocation rate is ${rate}/kW/month.')
    parts.append('Source: DC Hub GDCI, dchub.cloud/index')

    return jsonify({"market_id":market_id,"market_name":market["name"],
                    "citation":" ".join(parts),"generated_at":datetime.utcnow().isoformat()+"Z"})


# ─── ADMIN ROUTES ─────────────────────────────

@index_bp.route("/admin/config", methods=["GET"])
def admin_get_config():
    if not check_admin(request):
        return jsonify({"error":"Unauthorized"}), 403
    rows = query("SELECT key, value, description, updated_at FROM gdci_config ORDER BY key")
    return jsonify({"config":rows,"count":len(rows)})


@index_bp.route("/admin/config", methods=["POST"])
def admin_set_config():
    """
    Update config without touching GitHub. Examples:

    Enable power plants:
      {"power_enabled": "true"}

    Enable substations (Land & Power data):
      {"sub_enabled": "true", "sub_table": "your_substation_table"}

    Enable transmission lines:
      {"tx_enabled": "true"}

    Enable fiber:
      {"fiber_enabled": "true", "fiber_table": "fiber_routes"}

    Change facility MW column:
      {"fac_mw_col": "capacity_mw"}

    Adjust weights:
      {"weights_dhci": "0.35", "weights_dhri": "0.20"}
    """
    if not check_admin(request):
        return jsonify({"error":"Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error":"No JSON body"}), 400

    updated = []
    for k, v in data.items():
        set_config(k, str(v))
        updated.append(k)

    cache_clear()
    return jsonify({"updated":updated,"count":len(updated),
                    "note":"Cache cleared — new config active immediately."})


@index_bp.route("/admin/sources", methods=["GET"])
def admin_sources():
    """Show all data sources, which exist, which are enabled, how to activate."""
    if not check_admin(request):
        return jsonify({"error":"Unauthorized"}), 403

    cfg = get_config()
    sources = [
        {"name":"Facilities (DHCI+DHPI)","table":cfg["fac_table"],
         "exists":table_exists(cfg["fac_table"]),"enabled":True,"status":"always on"},
        {"name":"Transactions (DHDI)","table":cfg["txn_table"],
         "exists":table_exists(cfg["txn_table"]),"enabled":True,"status":"always on"},
        {"name":"Market Intelligence (DHRI)","table":cfg["mi_table"],
         "exists":table_exists(cfg["mi_table"]),"enabled":cfg.get("mi_enabled","false")=="true",
         "activate":'POST /api/index/admin/config {"mi_enabled":"true"}'},
        {"name":"Power Plants (DHPW)","table":cfg["power_table"],
         "exists":table_exists(cfg["power_table"]),"enabled":cfg.get("power_enabled","false")=="true",
         "activate":'POST /api/index/admin/config {"power_enabled":"true"}'},
        {"name":"Substations (DHPW priority)","table":cfg["sub_table"],
         "exists":table_exists(cfg["sub_table"]),"enabled":cfg.get("sub_enabled","false")=="true",
         "activate":'POST /api/index/admin/config {"sub_enabled":"true","sub_table":"your_table"}'},
        {"name":"Transmission Lines (connectivity)","table":cfg["tx_table"],
         "exists":table_exists(cfg["tx_table"]),"enabled":cfg.get("tx_enabled","false")=="true",
         "activate":'POST /api/index/admin/config {"tx_enabled":"true"}'},
        {"name":"Fiber Routes (connectivity)","table":cfg["fiber_table"],
         "exists":table_exists(cfg["fiber_table"]),"enabled":cfg.get("fiber_enabled","false")=="true",
         "activate":'POST /api/index/admin/config {"fiber_enabled":"true"}'},
    ]
    return jsonify({"sources":sources})


@index_bp.route("/admin/refresh", methods=["POST"])
def refresh_cache():
    if not check_admin(request):
        return jsonify({"error":"Unauthorized"}), 403
    cache_clear()
    return jsonify({"cleared":True,"at":datetime.utcnow().isoformat()+"Z"})


@index_bp.route("/admin/markets", methods=["POST"])
def admin_add_market():
    """Add a market without touching GitHub.
    Body: {"id":"las","name":"Las Vegas, US","region":"us","country":"US","city":"Las Vegas","state":"NV"}
    """
    if not check_admin(request):
        return jsonify({"error":"Unauthorized"}), 403

    data    = request.get_json(silent=True) or {}
    missing = [f for f in ["id","name","region","country","city"] if f not in data]
    if missing:
        return jsonify({"error":f"Missing: {missing}"}), 400

    execute("""
        CREATE TABLE IF NOT EXISTS gdci_markets (
            id VARCHAR(20) PRIMARY KEY, name VARCHAR(100) NOT NULL,
            region VARCHAR(20) NOT NULL, country VARCHAR(10) NOT NULL,
            city VARCHAR(100) NOT NULL, state VARCHAR(10),
            enabled BOOLEAN DEFAULT true, added_at TIMESTAMP DEFAULT NOW()
        )
    """)

    if not scalar("SELECT COUNT(*) FROM gdci_markets"):
        for m in DEFAULT_MARKETS:
            execute("""
                INSERT INTO gdci_markets (id,name,region,country,city,state)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
            """, (m["id"],m["name"],m["region"],m["country"],m["city"],m.get("state")))

    execute("""
        INSERT INTO gdci_markets (id,name,region,country,city,state,enabled)
        VALUES (%s,%s,%s,%s,%s,%s,true)
        ON CONFLICT (id) DO UPDATE
          SET name=%s,region=%s,country=%s,city=%s,state=%s,enabled=true
    """, (data["id"],data["name"],data["region"],data["country"],data["city"],data.get("state"),
          data["name"],data["region"],data["country"],data["city"],data.get("state")))

    cache_clear()
    return jsonify({"added":data["id"],"market":data["name"]})


# ─────────────────────────────────────────────
# BOOTSTRAP — auto-detect tables on every boot
# ─────────────────────────────────────────────
try:
    init_config_table()
    _cfg = get_config()
    # Auto-enable any source tables that exist in Neon but aren't enabled yet
    _auto = [
        ("power_table", "power_enabled"),
        ("tx_table",    "tx_enabled"),
        ("fiber_table", "fiber_enabled"),
    ]
    _activated = []
    for tbl_key, ena_key in _auto:
        tbl_name = _cfg.get(tbl_key,"")
        if tbl_name and table_exists(tbl_name) and _cfg.get(ena_key,"false").lower() != "true":
            set_config(ena_key, "true")
            _activated.append(tbl_name)
    if _activated:
        cache_clear("__config__")
        logger.info(f"📊 Index: auto-enabled tables: {', '.join(_activated)}")
    logger.info(f"📊 DC Hub Index: self-configuration complete ({len(_cfg)} keys)")
except Exception as _e:
    logger.warning(f"📊 DC Hub Index: bootstrap warning: {_e}")
