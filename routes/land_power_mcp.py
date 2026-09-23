"""Phase XX (2026-05-16) — Land+Power MCP bridge.

The /land-power page is a 9,700-LOC interactive client-side app that
mounts HIFLD substations/transmission/gas + NREL solar/wind + DCPI +
IX data on a map for human site selection. Agents have no equivalent
entry point. The funnel data confirms the gap:

  - 3,380 calls to get_grid_intelligence by 102 users (paywalled)
  - 3,212 calls to get_fiber_intel by 101 users (paywalled)
  - 0 conversions from MCP platform in 30 days
  - 14,058 upgrade signals in 30 days

Agents are reaching for grid + fiber intel because what they ACTUALLY
want is the Land+Power decision. This module provides:

  GET /api/v1/land-power/site-analysis   — fast cached site analysis
  GET /api/v1/land-power/quick-score     — 0-100 viability score only

…plus the `find_power_site` MCP tool (registered in dchub_mcp_server.py)
that consumes them. The MCP response always includes the link to the
human UI (/land-power?lat=X&lon=Y) so agents can hand off a deep
analysis URL to their user.

Performance: each endpoint targets <200ms p95 by aggregating from
already-cached internal tables (substations, market_power_scores,
eia_retail_rates, tax incentives, water_risk). External
HIFLD calls are NOT made server-side — agents already have lat/lon,
and our substations table mirrors HIFLD nightly so the lookup is local.
"""

from __future__ import annotations

import os
import math
import time
import datetime
from typing import Optional
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras

from util.db_honesty import try_fetchone
from util.us_states import state_match_pair
from util.water_risk import read_state_stress


land_power_mcp_bp = Blueprint("land_power_mcp", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        # Every block below is read-only and individually try/except'd. Without
        # autocommit they all share ONE transaction, so the FIRST failing query
        # aborts it and every later block dies with "current transaction is
        # aborted, commands ignored until end of transaction block" — the
        # endpoint still returns 200, but with land/water/tax/dcpi silently
        # empty and a feasibility score computed from nothing. autocommit makes
        # each statement its own transaction, so one bad query costs one block.
        # (Deliberately NOT savepoints: those are a no-op under autocommit.)
        c.autocommit = True
        return c
    except Exception:
        return None


def _safe_float(v, default):
    try: return float(v)
    except (TypeError, ValueError): return default


# ── In-memory cache: site-analysis has a 60s TTL keyed on rounded lat/lon ──
_SITE_CACHE: dict[str, tuple[float, dict]] = {}
_SITE_CACHE_TTL = 60.0
_SITE_CACHE_MAX = 500


def _cache_key(lat: float, lon: float, capacity_mw: float, radius_km: float) -> str:
    return f"{round(lat, 2)}|{round(lon, 2)}|{int(capacity_mw)}|{int(radius_km)}"


def _cache_get(key: str) -> Optional[dict]:
    hit = _SITE_CACHE.get(key)
    if not hit: return None
    ts, payload = hit
    if (time.time() - ts) > _SITE_CACHE_TTL:
        _SITE_CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: str, payload: dict):
    if len(_SITE_CACHE) >= _SITE_CACHE_MAX:
        # LRU-ish: drop the oldest 20%
        old = sorted(_SITE_CACHE.items(), key=lambda kv: kv[1][0])[:100]
        for k, _ in old: _SITE_CACHE.pop(k, None)
    _SITE_CACHE[key] = (time.time(), payload)


# ── Major US Internet Exchange Points (mirrors land-power-app.js list) ──
# Source: PeeringDB; pre-loaded to avoid an external call per request.
_INTERNET_EXCHANGES = [
    {"name": "Equinix Ashburn IX",  "city": "Ashburn, VA",  "lat": 39.0438, "lon": -77.4874, "participants": 850, "peak_tbps": 12.5},
    {"name": "DE-CIX New York",     "city": "New York, NY", "lat": 40.7614, "lon": -73.9776, "participants": 420, "peak_tbps":  4.2},
    {"name": "CoreSite Any2 LA",    "city": "Los Angeles, CA","lat": 34.0522,"lon": -118.2437,"participants": 310, "peak_tbps":  3.1},
    {"name": "Equinix Chicago IX",  "city": "Chicago, IL",  "lat": 41.8781, "lon": -87.6298, "participants": 280, "peak_tbps":  2.8},
    {"name": "DE-CIX Dallas",       "city": "Dallas, TX",   "lat": 32.7767, "lon": -96.7970, "participants": 195, "peak_tbps":  2.1},
    {"name": "Equinix San Jose IX", "city": "San Jose, CA", "lat": 37.3382, "lon": -121.8863,"participants": 380, "peak_tbps":  5.5},
    {"name": "CoreSite Any2 Denver","city": "Denver, CO",   "lat": 39.7392, "lon": -104.9903,"participants": 145, "peak_tbps":  1.2},
    {"name": "Equinix Seattle IX",  "city": "Seattle, WA",  "lat": 47.6062, "lon": -122.3321,"participants": 220, "peak_tbps":  2.4},
    {"name": "NOTA Miami",          "city": "Miami, FL",    "lat": 25.7617, "lon": -80.1918, "participants": 175, "peak_tbps":  1.8},
    {"name": "Equinix Atlanta IX",  "city": "Atlanta, GA",  "lat": 33.7490, "lon": -84.3880, "participants": 165, "peak_tbps":  1.6},
    {"name": "DE-CIX Phoenix",      "city": "Phoenix, AZ",  "lat": 33.4484, "lon": -112.0740,"participants":  85, "peak_tbps":  0.9},
]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance, kilometers."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlamb/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_ix(lat: float, lon: float) -> dict:
    """Find the nearest Internet Exchange Point."""
    best = None
    best_d = 9e9
    for ix in _INTERNET_EXCHANGES:
        d = _haversine_km(lat, lon, ix["lat"], ix["lon"])
        if d < best_d:
            best_d, best = d, ix
    if not best:
        return {}
    return {**best, "distance_km": round(best_d, 1)}


def _build_analysis(lat: float, lon: float, state: str,
                     capacity_mw: float, radius_km: float) -> dict:
    """Pure SQL aggregation. No external calls. Returns the full site
    analysis dict consumed by both the REST endpoint and the MCP tool.
    Designed for <200ms p95.
    """
    result: dict = {
        "site":          {"lat": lat, "lon": lon, "state": state,
                          "capacity_mw": capacity_mw, "radius_km": radius_km},
        "power":         {},
        "fiber":         {},
        "land":          {},
        "water":         {},
        "tax":           {},
        "dcpi":          {},
        "feasibility_score": None,
        "verdict":       None,
        "interactive_map_url": (
            f"https://dchub.cloud/land-power?lat={lat:.4f}&lon={lon:.4f}"
            f"&zoom=10&capacity_mw={capacity_mw:.0f}"
        ),
        "narrative":     "",
    }

    conn = _conn()
    if conn is None:
        # Best-effort: still compute IX + return URL
        ix = _nearest_ix(lat, lon)
        if ix: result["fiber"]["nearest_ix"] = ix
        result["narrative"] = (
            "Database unavailable — partial result. Try the interactive map: "
            + result["interactive_map_url"]
        )
        return result

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── Power: substations within radius ──
            substations_count = 0
            substation_capacity_mva = 0
            nearest_substation_km = None
            try:
                # Column names are load-bearing: `substations` has voltage_kv
                # (real) / lat / lng. It has NO voltage, latitude or longitude —
                # this query asked for all three and had never once returned a
                # row on prod. See the autocommit note in _conn() for why that
                # silently emptied every block after this one.
                cur.execute("""
                    SELECT name, voltage_kv, capacity_mva, lines,
                           (3959 * acos(LEAST(1.0,
                              cos(radians(%s)) * cos(radians(lat)) *
                              cos(radians(lng) - radians(%s)) +
                              sin(radians(%s)) * sin(radians(lat))
                           ))) * 1.609 AS distance_km
                      FROM substations
                     WHERE lat IS NOT NULL AND lng IS NOT NULL
                       AND lat BETWEEN %s AND %s
                       AND lng BETWEEN %s AND %s
                     ORDER BY distance_km ASC
                     LIMIT 10
                """, (lat, lon, lat,
                      lat - (radius_km/111.0), lat + (radius_km/111.0),
                      lon - (radius_km/(111.0 * max(0.1, math.cos(math.radians(lat))))),
                      lon + (radius_km/(111.0 * max(0.1, math.cos(math.radians(lat)))))))
                near = [r for r in cur.fetchall() if r.get("distance_km") is not None
                                                  and r["distance_km"] <= radius_km]
                substations_count = len(near)
                if near:
                    nearest_substation_km = round(near[0]["distance_km"], 1)
                    # Prefer the REAL nameplate (capacity_mva) and fall back to
                    # the voltage-class proxy only where it is null. voltage_kv
                    # is a numeric column — the previous string-digit parse
                    # would have yielded 0 for every row even had it resolved.
                    for s in near:
                        mva = s.get("capacity_mva")
                        if mva is not None:
                            try:
                                substation_capacity_mva += float(mva)
                                continue
                            except (TypeError, ValueError):
                                pass
                        try: v_num = float(s.get("voltage_kv") or 0)
                        except (TypeError, ValueError): v_num = 0
                        if v_num >= 500:   substation_capacity_mva += 1200
                        elif v_num >= 345: substation_capacity_mva += 700
                        elif v_num >= 230: substation_capacity_mva += 400
                        elif v_num >= 138: substation_capacity_mva += 200
                        elif v_num >= 69:  substation_capacity_mva += 80
                    substation_capacity_mva = round(substation_capacity_mva)
                    result["power"]["nearest_substations"] = [
                        {"name": s.get("name"), "voltage_kv": s.get("voltage_kv"),
                         "capacity_mva": s.get("capacity_mva"),
                         "distance_km": round(s["distance_km"], 1)}
                        for s in near[:5]
                    ]
                result["power"]["substations_in_radius"] = substations_count
                result["power"]["nearest_substation_km"] = nearest_substation_km
                result["power"]["est_substation_capacity_mva"] = substation_capacity_mva
            except Exception as e:
                result["power"]["_error"] = str(e)[:100]

            # ── DCPI verdict for the state ──
            if state:
                try:
                    cur.execute("""
                        SELECT verdict, excess_power_score, constraint_score,
                               time_to_power_months, queue_capacity_mw,
                               market_name
                          FROM market_power_scores
                         WHERE UPPER(state) = %s
                         ORDER BY computed_at DESC, excess_power_score DESC NULLS LAST
                         LIMIT 1
                    """, (state.upper(),))
                    r = cur.fetchone()
                    if r:
                        result["dcpi"] = {
                            "verdict":              r.get("verdict"),
                            "excess_power_score":   r.get("excess_power_score"),
                            "constraint_score":     r.get("constraint_score"),
                            "time_to_power_months": r.get("time_to_power_months"),
                            "queue_capacity_mw":    r.get("queue_capacity_mw"),
                            "best_market_in_state": r.get("market_name"),
                        }
                except Exception as e:
                    result["dcpi"]["_error"] = str(e)[:100]

                abbr, full = state_match_pair(state)

                # ── Retail rate (industrial) ──
                # ★ eia_retail_rates.state holds FULL names ("Virginia"), and
                # the table also carries census-region rows ("East North
                # Central"), so match BOTH spellings. Measured live
                # 2026-09-21: `UPPER(state) = 'VA'` -> 0 rows;
                # `'VIRGINIA'` -> 10 (industrial 10.09 c/kWh, period 2026).
                # The query never failed — it just answered "no data for VA"
                # forever, and _quick_score's rate factor never fired.
                r, err = try_fetchone(cur, """
                    SELECT rate_cents_kwh, period
                      FROM eia_retail_rates
                     WHERE UPPER(state) IN (%s, %s)
                       AND LOWER(sector) = 'industrial'
                     ORDER BY period DESC LIMIT 1
                """, (abbr, full))
                if err:
                    result["power"]["_rate_error"] = err
                elif r and r.get("rate_cents_kwh") is not None:
                    result["power"]["industrial_rate_cents_kwh"] = round(
                        float(r["rate_cents_kwh"]), 2)

                # ── Water stress ──
                # ★ NOT usgs_water_stress. It has no stress column of any
                # kind (its columns are site_id / site_name / latitude /
                # longitude / state / county / aquifer_name / well_depth_ft /
                # water_level_ft / water_level_date / site_type), it covers
                # 16 states, and its water_level_ft groundwater proxy is the
                # one withdrawn 2026-07-07 for reading INVERTED. So
                # `AVG(stress_index)` raised UndefinedColumn on every call
                # and stress_index was served null for every site. water_risk
                # carries the verified WRI Aqueduct roll-up (0-100), banded to
                # the 1-5 index _quick_score compares on.
                # util.water_risk is the ONE read path for that table: the
                # live schema, the 0-100 scale and the banding live there
                # instead of in a per-surface hand-copy, and it returns
                # (value, error) so a failure stays NAMED in result["water"]
                # rather than served as an indistinguishable null.
                # ★ THIS CURSOR IS A RealDictCursor (see _build_analysis
                # above). util.water_risk._cell reads a dict row BY NAME for
                # exactly that reason: a positional row[0] raises KeyError: 0,
                # which escapes try_fetchone's error capture and unwinds into
                # the caller's own `except` — taking the tax and land reads
                # that follow water down with it.
                water, err = read_state_stress(cur, abbr)
                if err:
                    result["water"]["_error"] = err
                elif water is not None and water["band"] is not None:
                    result["water"]["stress_index"] = water["band"]

                # ── Tax incentives ──
                # util.tax_incentives, not tax_incentives_neon: the table
                # froze on 2026-03-17 and reads nine changed programs as open.
                try:
                    from util.tax_incentives import state_incentive
                    r = state_incentive(cur, state)
                    if r:
                        result["tax"] = {
                            "sales_tax_exempt":      bool(r.get("sales_tax_exempt")),
                            "property_tax_abatement": bool(r.get("property_tax_abatement")),
                            "data_center_specific":   bool(r.get("data_center_specific")),
                            "detail":                 (r.get("incentive_details") or "")[:240],
                            "status":                 r.get("status"),
                            "last_verified":          r.get("last_verified"),
                            "source_url":             r.get("source_url"),
                        }
                except Exception:
                    pass

            # ── Land: comparable existing facilities within radius ──
            try:
                cur.execute("""
                    SELECT COUNT(*) AS n,
                           COALESCE(SUM(power_mw), 0) AS total_mw,
                           COALESCE(MAX(power_mw), 0) AS max_mw
                      FROM discovered_facilities
                     WHERE latitude  BETWEEN %s AND %s
                       AND longitude BETWEEN %s AND %s
                       AND (country = 'US' OR country = 'USA' OR country IS NULL)
                """, (lat - (radius_km/111.0), lat + (radius_km/111.0),
                      lon - (radius_km/(111.0 * max(0.1, math.cos(math.radians(lat))))),
                      lon + (radius_km/(111.0 * max(0.1, math.cos(math.radians(lat)))))))
                r = cur.fetchone()
                if r:
                    result["land"]["comparable_facilities_in_radius"] = int(r["n"] or 0)
                    result["land"]["existing_operational_mw"]          = round(float(r["total_mw"] or 0), 1)
                    result["land"]["largest_nearby_mw"]                = round(float(r["max_mw"] or 0), 1)
            except Exception as e:
                result["land"]["_error"] = str(e)[:100]
    finally:
        try: conn.close()
        except Exception: pass

    # ── Fiber: nearest IX ──
    ix = _nearest_ix(lat, lon)
    if ix: result["fiber"]["nearest_ix"] = ix

    # ── Composite feasibility score 0-100 ──
    score = 50.0  # neutral start
    notes = []

    # Power factor
    sub_cap = result["power"].get("est_substation_capacity_mva", 0)
    needed = capacity_mw * 1.3   # headroom buffer
    if sub_cap >= needed * 2:    score += 15; notes.append("abundant substation capacity")
    elif sub_cap >= needed:      score += 8;  notes.append("adequate substation capacity")
    elif sub_cap > 0:            score -= 5;  notes.append("limited substation capacity within radius")
    else:                        score -= 10; notes.append("no substations indexed within radius")

    near_sub = result["power"].get("nearest_substation_km")
    if near_sub is not None:
        if near_sub <= 5:    score += 8;  notes.append(f"substation {near_sub}km away — minimal interconnect cost")
        elif near_sub <= 15: score += 3
        elif near_sub > 25:  score -= 5;  notes.append(f"nearest substation {near_sub}km — interconnect cost rises")

    # DCPI factor
    verdict = (result["dcpi"].get("verdict") or "").upper()
    if verdict == "BUILD":   score += 12; notes.append("DCPI verdict BUILD for this state")
    elif verdict == "AVOID": score -= 12; notes.append("DCPI verdict AVOID — caution")

    # Retail rate factor
    rate = result["power"].get("industrial_rate_cents_kwh")
    if rate is not None:
        if rate <= 6:    score += 8; notes.append(f"low industrial rate {rate}¢/kWh")
        elif rate >= 11: score -= 8; notes.append(f"high industrial rate {rate}¢/kWh")

    # Water factor
    water = result["water"].get("stress_index")
    if water is not None and water >= 4:
        score -= 8; notes.append(f"high water stress (WRI band {water}/5) — cooling risk")

    # Tax factor
    tax = result["tax"]
    if tax and (tax.get("sales_tax_exempt") or tax.get("property_tax_abatement")):
        score += 5; notes.append("state tax incentives apply")

    # Fiber factor
    fix = result["fiber"].get("nearest_ix") or {}
    if fix.get("distance_km") is not None:
        if fix["distance_km"] <= 50:   score += 5; notes.append(f"major IX ({fix['name']}) {fix['distance_km']}km away")
        elif fix["distance_km"] > 300: score -= 5; notes.append(f"nearest IX is {fix['distance_km']}km — high-latency region")

    # Land/comp factor
    comps = result["land"].get("comparable_facilities_in_radius", 0)
    if comps >= 5:   score += 5; notes.append(f"{comps} comparable DCs already in the area")
    elif comps == 0: notes.append("no comparable DCs in radius — pioneer market")

    score = max(0, min(100, round(score)))
    result["feasibility_score"] = score
    if   score >= 80: result["verdict"] = "STRONG_SITE"
    elif score >= 60: result["verdict"] = "VIABLE_SITE"
    elif score >= 40: result["verdict"] = "MARGINAL_SITE"
    else:             result["verdict"] = "WEAK_SITE"

    # ── Narrative paragraph ──
    bits = [
        f"Site at ({lat:.3f}, {lon:.3f}) in {state or 'unspecified state'} "
        f"for ~{capacity_mw:.0f} MW: feasibility score {score}/100 ({result['verdict']})."
    ]
    if notes:
        bits.append("Drivers: " + "; ".join(notes[:5]) + ".")
    bits.append(f"View the interactive map for layer-by-layer detail: "
                f"{result['interactive_map_url']}")
    result["narrative"] = " ".join(bits)

    return result


# ── Land & Power details are Pro (owner, 2026-09-22) ────────────────────────
# The feasibility score and the power, land, water and tax figures behind it
# are the Land & Power product, and only Pro opens them (util/plan_tease.py
# lp_gate). A keyless caller gets the wall and no data. A key or session below
# Pro (free, Developer, a $10 pack) gets HTTP 200 with the verdict bands, the
# counts and the names, every figure null and coordinates at two decimals; a
# pack paid before the cutover keeps the full answer until it is spent. The
# MCP server (X-Internal-Key) is unchanged and applies its own masks.

_ANALYSIS_LOCKED = [
    "feasibility_score",
    "power.nearest_substation_km", "power.est_substation_capacity_mva",
    "power.industrial_rate_cents_kwh",
    "power.nearest_substations[].voltage_kv", "power.nearest_substations[].capacity_mva",
    "power.nearest_substations[].distance_km",
    "fiber.nearest_ix.distance_km", "fiber.nearest_ix.peak_tbps",
    "land.existing_operational_mw", "land.largest_nearby_mw",
    "water.stress_index", "tax.detail",
    "dcpi.excess_power_score", "dcpi.constraint_score",
    "dcpi.time_to_power_months", "dcpi.queue_capacity_mw",
    "narrative (figures)",
]


def _tease_map_url(lat, lon, capacity_mw):
    from util.plan_tease import round2
    return (f"https://dchub.cloud/land-power?lat={round2(lat)}&lon={round2(lon)}"
            f"&zoom=10&capacity_mw={capacity_mw:.0f}")


def _tease_narrative(state, verdict, dcpi_verdict):
    bits = [f"Site in {state or 'an unspecified state'}: feasibility verdict "
            f"{verdict or 'unscored'}."]
    if dcpi_verdict:
        bits.append(f"DCPI verdict for the state: {dcpi_verdict}.")
    bits.append("The feasibility score, its drivers and the power, land, water and "
                "tax figures behind it come with Pro.")
    return " ".join(bits)


def _tease_analysis(full: dict, lat: float, lon: float, state: str,
                    capacity_mw: float, radius_km: float) -> dict:
    """The keyless/free view of a site analysis: bands, counts and names only."""
    from util.plan_tease import round2, TEASE_ROWS
    power = full.get("power") or {}
    fiber_ix = (full.get("fiber") or {}).get("nearest_ix") or {}
    land = full.get("land") or {}
    tax = full.get("tax") or {}
    dcpi = full.get("dcpi") or {}
    subs = power.get("nearest_substations") or []
    out = {
        "site": {"lat": round2(lat), "lon": round2(lon), "state": state,
                 "capacity_mw": capacity_mw, "radius_km": radius_km},
        "power": {
            "substations_in_radius": power.get("substations_in_radius"),
            "nearest_substation_km": None,
            "est_substation_capacity_mva": None,
            "industrial_rate_cents_kwh": None,
            "nearest_substations": [
                {"name": s.get("name"), "voltage_kv": None,
                 "capacity_mva": None, "distance_km": None}
                for s in subs[:TEASE_ROWS]],
        },
        "fiber": ({"nearest_ix": {"name": fiber_ix.get("name"),
                                  "city": fiber_ix.get("city"),
                                  "distance_km": None, "peak_tbps": None}}
                  if fiber_ix else {}),
        "land": {"comparable_facilities_in_radius":
                 land.get("comparable_facilities_in_radius"),
                 "existing_operational_mw": None, "largest_nearby_mw": None},
        "water": {"stress_index": None},
        "tax": ({"sales_tax_exempt": tax.get("sales_tax_exempt"),
                 "property_tax_abatement": tax.get("property_tax_abatement"),
                 "data_center_specific": tax.get("data_center_specific"),
                 "detail": None, "status": tax.get("status"),
                 "last_verified": tax.get("last_verified"),
                 "source_url": tax.get("source_url")} if tax else {}),
        "dcpi": ({"verdict": dcpi.get("verdict"),
                  "best_market_in_state": dcpi.get("best_market_in_state"),
                  "excess_power_score": None, "constraint_score": None,
                  "time_to_power_months": None, "queue_capacity_mw": None}
                 if dcpi else {}),
        "feasibility_score": None,
        "verdict": full.get("verdict"),
        "interactive_map_url": _tease_map_url(lat, lon, capacity_mw),
        "narrative": _tease_narrative(state, full.get("verdict"), dcpi.get("verdict")),
    }
    if full.get("generated_at"):
        out["generated_at"] = full["generated_at"]
    return out


def _analysis(lat, lon, state, capacity_mw, radius_km):
    """(payload, cache_state) through the 60s in-process cache."""
    key = _cache_key(lat, lon, capacity_mw, radius_km)
    cached = _cache_get(key)
    if cached is not None:
        return cached, "hit"
    out = _build_analysis(lat, lon, state, capacity_mw, radius_km)
    out["generated_at"] = (datetime.datetime.now(datetime.timezone.utc)
                           .isoformat().replace("+00:00", "Z"))
    _cache_set(key, out)
    return out, "miss"


@land_power_mcp_bp.route("/api/v1/land-power/site-analysis", methods=["GET", "OPTIONS"])
def site_analysis():
    if request.method == "OPTIONS":
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200

    lat   = _safe_float(request.args.get("lat"), None)
    lon   = _safe_float(request.args.get("lon"), None)
    state = (request.args.get("state") or "").upper().strip()
    if lat is None or lon is None:
        return jsonify(error="lat and lon required",
                       hint="GET /api/v1/land-power/site-analysis?lat=39.04&lon=-77.48&state=VA&capacity_mw=100"), 400

    capacity_mw = max(1.0, _safe_float(request.args.get("capacity_mw"), 100.0))
    radius_km   = max(5.0, min(100.0, _safe_float(request.args.get("radius_km"), 25.0)))

    from util.plan_tease import lp_early_wall
    _wall = lp_early_wall()
    if _wall is not None:
        return _wall

    def _full():
        out, state_ = _analysis(lat, lon, state, capacity_mw, radius_km)
        resp = jsonify({**out, "_cache": state_})
        resp.headers["Cache-Control"] = "public, max-age=60"
        resp.headers["X-LP-Cache"] = state_
        return resp, 200

    def _tease():
        out, _ = _analysis(lat, lon, state, capacity_mw, radius_km)
        return (_tease_analysis(out, lat, lon, state, capacity_mw, radius_km),
                _ANALYSIS_LOCKED, 1)

    from util.plan_tease import lp_gate
    return lp_gate(_full, _tease)


@land_power_mcp_bp.route("/api/v1/land-power/quick-score", methods=["GET", "OPTIONS"])
def quick_score():
    """Lightweight: just the score + verdict + interactive_map_url. For
    agents that need a fast yes/no signal before deciding to spend a
    deeper call."""
    if request.method == "OPTIONS":
        resp = jsonify(ok=True); resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    lat = _safe_float(request.args.get("lat"), None)
    lon = _safe_float(request.args.get("lon"), None)
    state = (request.args.get("state") or "").upper().strip()
    if lat is None or lon is None:
        return jsonify(error="lat and lon required"), 400
    capacity_mw = max(1.0, _safe_float(request.args.get("capacity_mw"), 100.0))

    from util.plan_tease import lp_early_wall
    _wall = lp_early_wall()
    if _wall is not None:
        return _wall

    def _full():
        out = _build_analysis(lat, lon, state, capacity_mw, radius_km=25.0)
        return jsonify({
            "feasibility_score": out.get("feasibility_score"),
            "verdict":           out.get("verdict"),
            "dcpi_verdict":      out.get("dcpi", {}).get("verdict"),
            "interactive_map_url": out.get("interactive_map_url"),
            "narrative":         out.get("narrative"),
        }), 200

    def _tease():
        out, _ = _analysis(lat, lon, state, capacity_mw, 25.0)
        dcpi_verdict = (out.get("dcpi") or {}).get("verdict")
        return ({
            "feasibility_score": None,
            "verdict":           out.get("verdict"),
            "dcpi_verdict":      dcpi_verdict,
            "interactive_map_url": _tease_map_url(lat, lon, capacity_mw),
            "narrative":         _tease_narrative(state, out.get("verdict"), dcpi_verdict),
        }, ["feasibility_score", "narrative (figures)"], 1)

    from util.plan_tease import lp_gate
    return lp_gate(_full, _tease)


# ── The Land & Power map's upgrade modal and wall (2026-09-22) ──────────────
# The map is a static page and cannot sign a /go/c link, so it asks here. One
# rung: Pro, the only plan that opens Land & Power. Its price comes from
# tier_registry. A caller presenting a valid key gets a link bound to that
# key's hash, so the purchase lands on the key the map is using; anyone else
# gets a caller-independent link. `access` is what the Land & Power routes
# give this caller ('full', 'preview' or 'wall', util/plan_tease.lp_access), so
# the page draws what the server will serve.
@land_power_mcp_bp.route("/api/v1/land-power/upgrade-ladder", methods=["GET", "OPTIONS"])
def upgrade_ladder():
    if request.method == "OPTIONS":
        return ("", 204)
    from util import plan_tease as pt
    key = pt.presented_key()
    lad = pt.lp_ladder(key)
    rungs = lad.get("upgrade_options") or []
    for r in rungs:
        r["opens"] = "all"
    resp = jsonify({"success": True, "rungs": rungs, "key_bound": lad["key_bound"],
                    "access": pt.lp_access(), "required_plan": pt.LP_PLAN,
                    "free_key": pt.lp_free_key()})
    resp.headers["Cache-Control"] = (pt.TEASE_CACHE if pt.lp_anonymous()
                                     and not pt._auto_issued_key() else pt.NO_STORE)
    return resp, 200


@land_power_mcp_bp.route("/api/v1/land-power/track", methods=["POST", "OPTIONS"])
def land_power_track():
    """Fire-and-forget analytics ping from the Land & Power map's site evaluations.
    Public + intentionally lightweight: accept the body and return 204. No auth, so
    the map's non-blocking telemetry can never 401 the browser console
    (the endpoint simply didn't exist before — Devin/owner QA 2026-06-08)."""
    hdrs = {"Access-Control-Allow-Origin": "*"}
    if request.method == "OPTIONS":
        return ("", 204, hdrs)
    try:
        request.get_json(silent=True)  # accept + discard; analytics payload is {filters:[]}
    except Exception:
        pass
    return ("", 204, hdrs)
