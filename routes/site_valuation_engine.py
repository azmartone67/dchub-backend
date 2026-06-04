"""site_valuation_engine.py — r80 (2026-06-04)

"What is this site worth?" — DC Hub's first seller-side valuation tool.

The wedge: nobody publishes a Grid-vs-Gas-BTM-vs-Gas-to-Grid NPV
comparison for a specific (lat, lon, acres, target_mw) tuple. DCH does.
DatacenterHawk gives facility footprints. CBRE / JLL do bespoke
advisory at $25K+ engagement. LandSearch / LoopNet give generic
$/acre. None of them tie POWER ECONOMICS to LAND VALUE.

Architecture: stitches data from existing endpoints (DCPI, gas pricing,
site-forecast, deals comparables) into a single valuation. PRO-gated.

Endpoints (Phase 1):

  POST /api/v1/site/value      — calculate valuation (PRO; free = teaser)
  GET  /sites/value            — HTML page with form + results
  GET  /api/v1/site/value/methodology — public methodology doc

Inputs:
  {
    "lat": 33.45, "lon": -112.07,           # required
    "acres": 50,                              # required
    "target_mw": 100,                         # required
    "deadline_months": 24,                    # optional (default 24)
    "gas_distance_miles": null                # optional (auto-detected if null)
  }

Outputs (PRO tier, full payload):
  - dcpi_context: verdict, composite, time-to-power-months
  - scenarios: {grid_only, gas_btm, gas_to_grid_hybrid}
       each: {capex_usd, annual_opex_usd, time_to_power_months,
              ten_year_npv_usd, levelized_$/MWh}
  - best_fit: {scenario, rationale}
  - valuation: {$/mw_low, $/mw_mid, $/mw_high,
                 $/acre_low, $/acre_mid, $/acre_high,
                 site_value_usd_low, site_value_usd_mid, site_value_usd_high}
  - comparable_sales: [up to 10 nearby market transactions from deals table]
  - methodology_url + warnings

Outputs (free tier teaser):
  - dcpi_context (public)
  - best_fit.scenario (label only)
  - valuation.site_value_usd_mid (single midpoint, no range)
  - + upgrade_hint
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import os
import sys
from typing import Any, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

site_valuation_engine_bp = Blueprint("site_valuation_engine", __name__)


# ── DB connection (per-route pattern) ─────────────────────────────

def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _safe_close(c):
    if c is None: return
    try: c.close()
    except Exception: pass


# ── Tier gating ───────────────────────────────────────────────────

def _resolve_tier() -> str:
    """Return caller's tier as uppercase string (FREE / STARTER / PRO /
    DEVELOPER / ENTERPRISE). Uses the same _resolve_caller_tier helper
    as lp_sites + powered_land_gas for consistency."""
    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
        return (tier or "FREE").upper()
    except Exception:
        return "FREE"


def _is_pro_plus() -> bool:
    return _resolve_tier() in ("PRO", "DEVELOPER", "ENTERPRISE")


# ── Industry constants (Phase 1 — replaceable with live data Phase 2) ─

# CapEx multiples (per kW of nameplate, typical greenfield 2024-2026)
_CAPEX_GRID_INTERCONNECT_USD_PER_KW = 175      # $50-300/kW range, mid $175
_CAPEX_GAS_CCGT_USD_PER_KW          = 1050     # new CCGT, EIA AEO 2025
_CAPEX_GAS_PEAKER_USD_PER_KW        = 750      # simple-cycle gas turbine
_CAPEX_GAS_PIPELINE_TAP_USD         = 3_500_000  # 1-tap to lateral pipeline
_CAPEX_SUBSTATION_BUILD_USD         = 8_000_000  # if no existing sub w/i 50km

# OpEx assumptions
_GRID_AVG_LMP_USD_PER_MWH           = 45       # US blended average 2024
_HOURS_PER_YEAR                      = 8760
_CCGT_HEAT_RATE_BTU_PER_KWH         = 6800     # avg modern CCGT
_PEAKER_HEAT_RATE_BTU_PER_KWH       = 10500    # simple-cycle
_GAS_PRICE_FALLBACK_USD_PER_MMBTU   = 3.50     # if EIA data missing

# NPV horizon + discount rate
_NPV_HORIZON_YEARS                   = 10
_DISCOUNT_RATE                       = 0.08    # standard infrastructure WACC

# Valuation multiples (per MW + per acre, market-typical)
_VALUE_PER_MW_USD_BASE               = 2_000_000  # $2M/MW baseline greenfield
_VALUE_PER_ACRE_USD_BASE             = 75_000     # $75K/acre baseline
_VALUE_RANGE_SPREAD                  = 0.30       # ±30% low/high envelope


# ── Nearest DCPI market lookup ────────────────────────────────────

# Hand-seeded centroids for the top 30 markets. Used to map (lat, lon)
# → market slug for the DCPI query. Phase 2 will replace with a SQL
# nearest-market lookup against the markets table.
_MARKET_CENTROIDS = {
    "ashburn":          ("VA", 39.04, -77.48),
    "northern-virginia":("VA", 38.95, -77.40),
    "phoenix":          ("AZ", 33.45, -112.07),
    "dallas":           ("TX", 32.78, -96.80),
    "dallas-fort-worth":("TX", 32.78, -96.80),
    "atlanta":          ("GA", 33.75, -84.39),
    "chicago":          ("IL", 41.88, -87.63),
    "santa-clara":      ("CA", 37.35, -121.96),
    "silicon-valley":   ("CA", 37.35, -121.96),
    "los-angeles":      ("CA", 34.05, -118.24),
    "portland":         ("OR", 45.52, -122.68),
    "boardman":         ("OR", 45.84, -119.71),
    "the-dalles":       ("OR", 45.60, -121.18),
    "cheyenne":         ("WY", 41.14, -104.82),
    "salt-lake-city":   ("UT", 40.76, -111.89),
    "denver":           ("CO", 39.74, -104.99),
    "boise":            ("ID", 43.62, -116.20),
    "reno":             ("NV", 39.53, -119.81),
    "las-vegas":        ("NV", 36.17, -115.14),
    "albuquerque":      ("NM", 35.08, -106.65),
    "san-antonio":      ("TX", 29.42, -98.49),
    "austin":           ("TX", 30.27, -97.74),
    "houston":          ("TX", 29.76, -95.37),
    "kansas-city":      ("MO", 39.10, -94.58),
    "columbus":         ("OH", 39.96, -83.00),
    "new-albany":       ("OH", 40.08, -82.81),
    "richmond":         ("VA", 37.54, -77.43),
    "raleigh":          ("NC", 35.78, -78.64),
    "charlotte":        ("NC", 35.23, -80.84),
    "tampa":            ("FL", 27.95, -82.46),
    "jacksonville":     ("FL", 30.33, -81.66),
    "miami":            ("FL", 25.76, -80.19),
    "nashville":        ("TN", 36.16, -86.78),
}


def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in miles."""
    R = 3958.8
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _nearest_market(lat: float, lon: float) -> Tuple[str, str, float]:
    """Return (slug, state, distance_miles) for the closest centroid."""
    best = None
    for slug, (state, mlat, mlon) in _MARKET_CENTROIDS.items():
        d = _haversine_miles(lat, lon, mlat, mlon)
        if best is None or d < best[2]:
            best = (slug, state, d)
    return best  # type: ignore[return-value]


# ── DCPI lookup ───────────────────────────────────────────────────

def _fetch_dcpi(slug: str) -> dict:
    """Query market_power_scores table for the verdict + components.

    Schema reference: routes/dcpi.py:1722 — canonical handler uses
    `SELECT * FROM market_power_scores WHERE market_slug = %s ORDER BY
    computed_at DESC LIMIT 1`. We mirror that pattern + try the alias
    table for metro slugs (northern-virginia → ashburn, etc.).
    """
    c = _db_conn()
    if c is None:
        return {"available": False, "reason": "db_unavailable"}
    try:
        # Resolve metro alias if needed (best-effort, swallow errors)
        candidates = [slug]
        try:
            from routes.dcpi import DCPI_METRO_ALIASES
            _alias = DCPI_METRO_ALIASES.get(slug.lower())
            if _alias and _alias not in candidates:
                candidates.append(_alias)
        except Exception:
            pass

        with c.cursor() as cur:
            row = None
            matched = None
            for cand in candidates:
                cur.execute("""
                    SELECT verdict, composite_score, excess_power_score,
                           constraint_score, time_to_power_months,
                           iso, computed_at
                      FROM market_power_scores
                     WHERE market_slug = %s
                     ORDER BY computed_at DESC
                     LIMIT 1
                """, (cand,))
                row = cur.fetchone()
                if row:
                    matched = cand
                    break
        if not row:
            return {"available": False, "reason": "no_row_for_slug",
                    "slug": slug}
        return {
            "available":             True,
            "slug":                  matched or slug,
            "verdict":               row[0],
            "composite_score":       float(row[1] or 0),
            "excess_power_score":    float(row[2] or 0),
            "constraint_score":      float(row[3] or 0),
            "time_to_power_months":  float(row[4] or 36),
            "iso":                   row[5],
            "last_updated":          row[6].isoformat() if row[6] else None,
        }
    except Exception as e:
        return {"available": False, "reason": "query_error",
                "error": str(e)[:200]}
    finally:
        _safe_close(c)


# ── Gas pricing lookup ────────────────────────────────────────────

def _fetch_gas_economics(slug: str, state: str) -> dict:
    """Use the existing powered_land_gas helpers to derive gas $/MMBtu
    and $/MWh for the slug. Falls back to representative values."""
    try:
        from routes.powered_land_gas import (
            _HUB_FOR_STATE, _HUB_DEFINITIONS,
            _fetch_gas_price_basis,
        )
        hub_key = _HUB_FOR_STATE.get(state, "henry_hub")
        hub_def = _HUB_DEFINITIONS.get(hub_key, {})
        hub_name = hub_def.get("name", "Henry Hub")
        prices = _fetch_gas_price_basis(slug, state, hub_key)
        delivered = (prices.get("delivered_electric_usd_mmbtu")
                      or prices.get("delivered_industrial_usd_mmbtu")
                      or prices.get("hub_spot_usd_mmbtu")
                      or prices.get("henry_hub_spot_usd_mmbtu"))
        if delivered is None:
            delivered = _GAS_PRICE_FALLBACK_USD_PER_MMBTU
            data_basis = "fallback"
        else:
            data_basis = prices.get("data_basis", "live")
        return {
            "available":         True,
            "hub_key":           hub_key,
            "hub_name":          hub_name,
            "gas_$/MMBtu":       round(float(delivered), 2),
            "data_basis":        data_basis,
            "$/MWh_ccgt_avg":    round(float(delivered) * _CCGT_HEAT_RATE_BTU_PER_KWH / 1000, 2),
            "$/MWh_peaker":      round(float(delivered) * _PEAKER_HEAT_RATE_BTU_PER_KWH / 1000, 2),
        }
    except Exception as e:
        return {
            "available":         False,
            "reason":            "powered_land_gas_unavailable",
            "error":             str(e)[:200],
            "gas_$/MMBtu":       _GAS_PRICE_FALLBACK_USD_PER_MMBTU,
            "$/MWh_ccgt_avg":    round(_GAS_PRICE_FALLBACK_USD_PER_MMBTU * _CCGT_HEAT_RATE_BTU_PER_KWH / 1000, 2),
            "$/MWh_peaker":      round(_GAS_PRICE_FALLBACK_USD_PER_MMBTU * _PEAKER_HEAT_RATE_BTU_PER_KWH / 1000, 2),
        }


# ── Comparable sales lookup ───────────────────────────────────────

def _fetch_comparable_sales(slug: str, state: str, limit: int = 10) -> list:
    """Recent transactions in the same market — pulled from the deals
    table. Phase 1: filter by market name match; Phase 2 will index by
    actual lat/lon proximity."""
    c = _db_conn()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            # Try several deal table names — schema isn't fully unified
            # across the codebase. Use a list of fallbacks.
            for tbl in ("deals", "transactions", "discovered_deals"):
                try:
                    cur.execute(f"""
                        SELECT target, acquirer, value_usd, announced_date,
                               market, deal_type
                          FROM {tbl}
                         WHERE LOWER(COALESCE(market, '')) LIKE %s
                            OR LOWER(COALESCE(market, '')) LIKE %s
                         ORDER BY announced_date DESC NULLS LAST
                         LIMIT %s
                    """, ("%" + slug + "%",
                          "%" + (slug.replace("-", " ")) + "%",
                          limit))
                    rows = cur.fetchall()
                    if rows:
                        out = []
                        for r in rows:
                            out.append({
                                "target":         r[0],
                                "acquirer":       r[1],
                                "value_usd":      float(r[2]) if r[2] is not None else None,
                                "announced_date": r[3].isoformat() if r[3] else None,
                                "market":         r[4],
                                "deal_type":      r[5],
                            })
                        return out
                except Exception:
                    continue
            return []
    except Exception:
        return []
    finally:
        _safe_close(c)


# ── 3-scenario NPV calculator ─────────────────────────────────────

def _npv(annual_cashflows: list, discount_rate: float = _DISCOUNT_RATE) -> float:
    return sum(cf / ((1 + discount_rate) ** (year + 1))
               for year, cf in enumerate(annual_cashflows))


def _compute_scenarios(target_mw: int, dcpi: dict, gas: dict) -> dict:
    """Compute Grid / BTM / Hybrid scenarios. Returns per-scenario:
      - capex_usd
      - annual_opex_usd
      - time_to_power_months
      - ten_year_npv_usd  (capex + 10yr opex discounted; NEGATIVE because
                            it's a cost-only view at this stage)
      - levelized_usd_per_mwh
    """
    mw = max(1, int(target_mw))
    kw = mw * 1000
    annual_mwh = mw * _HOURS_PER_YEAR

    # ── Grid-only ────────────────────────────────────────────────
    grid_ttp = dcpi.get("time_to_power_months", 36) if dcpi.get("available") else 36
    grid_capex = kw * _CAPEX_GRID_INTERCONNECT_USD_PER_KW + _CAPEX_SUBSTATION_BUILD_USD
    grid_opex_yr = annual_mwh * _GRID_AVG_LMP_USD_PER_MWH
    grid_npv = -(grid_capex + _npv([grid_opex_yr] * _NPV_HORIZON_YEARS))
    grid_levelized = abs(grid_npv) / (annual_mwh * _NPV_HORIZON_YEARS)

    # ── Gas BTM (CCGT) ───────────────────────────────────────────
    btm_ttp = 14  # typical CCGT build + tap, faster than ISO queue
    btm_capex = kw * _CAPEX_GAS_CCGT_USD_PER_KW + _CAPEX_GAS_PIPELINE_TAP_USD
    btm_opex_yr = annual_mwh * gas.get("$/MWh_ccgt_avg", 25)
    btm_npv = -(btm_capex + _npv([btm_opex_yr] * _NPV_HORIZON_YEARS))
    btm_levelized = abs(btm_npv) / (annual_mwh * _NPV_HORIZON_YEARS)

    # ── Gas-to-Grid Hybrid ───────────────────────────────────────
    hybrid_ttp = 24  # gas-first + grid follow-on
    hybrid_capex = (kw * _CAPEX_GAS_CCGT_USD_PER_KW * 0.7
                    + kw * _CAPEX_GRID_INTERCONNECT_USD_PER_KW * 0.5
                    + _CAPEX_GAS_PIPELINE_TAP_USD
                    + _CAPEX_SUBSTATION_BUILD_USD * 0.5)
    # 70% gas-fueled, 30% sold-to-grid as ancillary
    hybrid_opex_yr = annual_mwh * gas.get("$/MWh_ccgt_avg", 25) * 0.7 \
                      - annual_mwh * _GRID_AVG_LMP_USD_PER_MWH * 0.10  # grid sell credit
    hybrid_npv = -(hybrid_capex + _npv([hybrid_opex_yr] * _NPV_HORIZON_YEARS))
    hybrid_levelized = abs(hybrid_npv) / (annual_mwh * _NPV_HORIZON_YEARS)

    return {
        "grid_only": {
            "capex_usd":            round(grid_capex, 0),
            "annual_opex_usd":      round(grid_opex_yr, 0),
            "time_to_power_months": round(grid_ttp, 1),
            "ten_year_npv_usd":     round(grid_npv, 0),
            "levelized_usd_per_mwh":round(grid_levelized, 2),
        },
        "gas_btm": {
            "capex_usd":            round(btm_capex, 0),
            "annual_opex_usd":      round(btm_opex_yr, 0),
            "time_to_power_months": round(btm_ttp, 1),
            "ten_year_npv_usd":     round(btm_npv, 0),
            "levelized_usd_per_mwh":round(btm_levelized, 2),
        },
        "gas_to_grid_hybrid": {
            "capex_usd":            round(hybrid_capex, 0),
            "annual_opex_usd":      round(hybrid_opex_yr, 0),
            "time_to_power_months": round(hybrid_ttp, 1),
            "ten_year_npv_usd":     round(hybrid_npv, 0),
            "levelized_usd_per_mwh":round(hybrid_levelized, 2),
        },
    }


def _pick_best_fit(scenarios: dict, dcpi: dict, deadline_months: int) -> dict:
    """Choose the best scenario based on:
      1. Lowest levelized $/MWh that meets deadline_months
      2. Tiebreak: NPV (less negative wins)
      3. If grid TTP > deadline → exclude grid_only
      4. If DCPI verdict is AVOID → prefer BTM (don't trust grid)
    """
    candidates = []
    for name, s in scenarios.items():
        if s["time_to_power_months"] > deadline_months:
            continue
        candidates.append((name, s))
    if not candidates:
        # No scenario meets deadline → pick fastest
        candidates = [(name, s) for name, s in scenarios.items()]
        candidates.sort(key=lambda x: x[1]["time_to_power_months"])
        best_name, best_s = candidates[0]
        return {
            "scenario":   best_name,
            "rationale":  (f"No scenario meets {deadline_months}-month deadline; "
                            f"selected fastest ({best_s['time_to_power_months']:.0f} months). "
                            f"Consider extending deadline."),
        }
    if dcpi.get("verdict") == "AVOID" and "gas_btm" in dict(candidates):
        return {
            "scenario":   "gas_btm",
            "rationale":  (f"DCPI verdict AVOID + grid time-to-power "
                            f"{dcpi.get('time_to_power_months', 36):.0f} months — "
                            f"gas BTM bypasses ISO queue, fastest path to power."),
        }
    candidates.sort(key=lambda x: x[1]["levelized_usd_per_mwh"])
    best_name, best_s = candidates[0]
    return {
        "scenario":   best_name,
        "rationale":  (f"Lowest levelized cost (${best_s['levelized_usd_per_mwh']:.2f}/MWh) "
                        f"of scenarios meeting {deadline_months}-month deadline."),
    }


def _compute_valuation(target_mw: int, acres: float, dcpi: dict,
                        best_fit: dict, scenarios: dict) -> dict:
    """Compute $-range valuation. Phase 1 uses industry-multiple baselines
    adjusted by DCPI verdict and best-fit scenario. Phase 2 will fit a
    regression against the comparable_sales table."""
    # Per-MW value adjusted by verdict
    verdict_mult = {
        "BUILD":   1.20,
        "CAUTION": 0.95,
        "AVOID":   0.75,
        None:      0.95,
    }.get(dcpi.get("verdict"), 0.95)

    # Best-fit scenario adjustment (BTM premium if grid is constrained)
    bestfit_mult = {
        "grid_only":          1.00,
        "gas_btm":            1.10,  # premium for fast time-to-power
        "gas_to_grid_hybrid": 1.05,
    }.get(best_fit["scenario"], 1.00)

    per_mw_mid = _VALUE_PER_MW_USD_BASE * verdict_mult * bestfit_mult
    per_acre_mid = _VALUE_PER_ACRE_USD_BASE * verdict_mult

    site_value_mid = per_mw_mid * target_mw + per_acre_mid * acres

    spread = _VALUE_RANGE_SPREAD
    return {
        "$/mw_low":            round(per_mw_mid * (1 - spread), 0),
        "$/mw_mid":            round(per_mw_mid, 0),
        "$/mw_high":           round(per_mw_mid * (1 + spread), 0),
        "$/acre_low":          round(per_acre_mid * (1 - spread), 0),
        "$/acre_mid":          round(per_acre_mid, 0),
        "$/acre_high":         round(per_acre_mid * (1 + spread), 0),
        "site_value_usd_low":  round(site_value_mid * (1 - spread), 0),
        "site_value_usd_mid":  round(site_value_mid, 0),
        "site_value_usd_high": round(site_value_mid * (1 + spread), 0),
        "_methodology":        ("Phase 1 baseline: $2M/MW × verdict × best-fit "
                                  "multiplier + $75K/acre × verdict multiplier, "
                                  "±30% envelope. Phase 2 fits regression to "
                                  "comparable_sales table for tighter ranges."),
    }


# ── Main endpoint ─────────────────────────────────────────────────

@site_valuation_engine_bp.route("/api/v1/site/value", methods=["POST", "OPTIONS"])
def site_value():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200
    payload = request.get_json(silent=True) or {}
    try:
        lat = float(payload.get("lat") or 0)
        lon = float(payload.get("lon") or 0)
        acres = float(payload.get("acres") or 0)
        target_mw = int(payload.get("target_mw") or 0)
        deadline_months = int(payload.get("deadline_months") or 24)
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "invalid_payload",
            "hint":  "Required: lat, lon, acres, target_mw. Optional: deadline_months.",
        }), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and acres > 0 and target_mw > 0):
        return jsonify({
            "ok": False,
            "error": "invalid_payload",
            "hint":  "lat ∈ [-90,90], lon ∈ [-180,180], acres > 0, target_mw > 0.",
        }), 400

    # Gather data
    slug, state, dist = _nearest_market(lat, lon)
    dcpi = _fetch_dcpi(slug)
    gas = _fetch_gas_economics(slug, state)
    scenarios = _compute_scenarios(target_mw, dcpi, gas)
    best_fit = _pick_best_fit(scenarios, dcpi, deadline_months)
    valuation = _compute_valuation(target_mw, acres, dcpi, best_fit, scenarios)
    comps = _fetch_comparable_sales(slug, state)

    base = {
        "ok":            True,
        "as_of":         _dt.datetime.utcnow().isoformat() + "Z",
        "input":         {"lat": lat, "lon": lon, "acres": acres,
                          "target_mw": target_mw,
                          "deadline_months": deadline_months},
        "market_context": {
            "nearest_market_slug":  slug,
            "nearest_market_state": state,
            "miles_from_centroid":  round(dist, 1),
        },
        "dcpi_context":   {
            "verdict":              dcpi.get("verdict"),
            "composite_score":      dcpi.get("composite_score"),
            "excess_power_score":   dcpi.get("excess_power_score"),
            "constraint_score":     dcpi.get("constraint_score"),
            "time_to_power_months": dcpi.get("time_to_power_months"),
            "iso":                  dcpi.get("iso"),
            "_debug_available":     dcpi.get("available"),
            "_debug_reason":        dcpi.get("reason"),
            "_debug_error":         dcpi.get("error"),
        },
    }

    # ── PRO tier: full payload ─────────────────────────────────
    if _is_pro_plus():
        base.update({
            "scenarios":            scenarios,
            "best_fit":             best_fit,
            "valuation":            valuation,
            "gas_context":          gas,
            "comparable_sales":     comps[:10],
            "methodology_url":      "/api/v1/site/value/methodology",
        })
        return jsonify(base), 200

    # ── Free / Starter: teaser ─────────────────────────────────
    base.update({
        "best_fit":          {"scenario": best_fit["scenario"]},
        "valuation_teaser":  {
            "site_value_usd_mid": valuation["site_value_usd_mid"],
            "$/mw_mid":           valuation["$/mw_mid"],
        },
        "scenarios_teaser":  {
            "grid_only":          {"time_to_power_months": scenarios["grid_only"]["time_to_power_months"]},
            "gas_btm":            {"time_to_power_months": scenarios["gas_btm"]["time_to_power_months"]},
            "gas_to_grid_hybrid": {"time_to_power_months": scenarios["gas_to_grid_hybrid"]["time_to_power_months"]},
        },
        "upgrade_hint": {
            "human_message":  ("Full 3-scenario NPV + CapEx/OpEx breakdown, "
                                "$-range valuation envelope, gas hub pricing, "
                                "and comparable-sale lookups are a PRO feature."),
            "tier_required":  "pro",
            "signup_url":     "https://dchub.cloud/pricing",
            "stripe_url":     "https://buy.stripe.com/00w28o7BqaXLeP31QIaZi04",
            "what_you_unlock": [
                "Full $-range valuation envelope (low/mid/high per MW + per acre)",
                "Grid / Gas-BTM / Gas-to-Grid 10-year NPV comparison",
                "CapEx + OpEx breakdown per scenario",
                "Levelized cost $/MWh per scenario",
                "Best-fit scenario rationale",
                "Comparable transactions from $324B+ tracked M&A pipeline",
                "Live gas hub pricing (Henry Hub + regional basis)",
            ],
        },
    })
    return jsonify(base), 200


# ── Methodology endpoint (public) ─────────────────────────────────

@site_valuation_engine_bp.route("/api/v1/site/value/methodology", methods=["GET"])
def site_value_methodology():
    return jsonify({
        "ok":           True,
        "version":      "Phase 1 (2026-06-04)",
        "summary":      ("Three-scenario NPV comparison for a (lat, lon, "
                          "acres, target_mw) tuple: Grid-only, Gas BTM (CCGT), "
                          "Gas-to-Grid Hybrid. Inputs sourced from DCPI verdict, "
                          "live gas hub pricing, and 234+ market DCPI scores. "
                          "Valuation envelope is industry-multiple baseline "
                          "adjusted by verdict and best-fit scenario, ±30% "
                          "range. Phase 2 fits regression to comparable sales."),
        "constants":   {
            "capex_grid_interconnect_usd_per_kw":  _CAPEX_GRID_INTERCONNECT_USD_PER_KW,
            "capex_gas_ccgt_usd_per_kw":            _CAPEX_GAS_CCGT_USD_PER_KW,
            "capex_gas_pipeline_tap_usd":           _CAPEX_GAS_PIPELINE_TAP_USD,
            "ccgt_heat_rate_btu_per_kwh":           _CCGT_HEAT_RATE_BTU_PER_KWH,
            "peaker_heat_rate_btu_per_kwh":         _PEAKER_HEAT_RATE_BTU_PER_KWH,
            "grid_avg_lmp_usd_per_mwh":             _GRID_AVG_LMP_USD_PER_MWH,
            "npv_discount_rate":                    _DISCOUNT_RATE,
            "npv_horizon_years":                    _NPV_HORIZON_YEARS,
            "value_per_mw_usd_base":                _VALUE_PER_MW_USD_BASE,
            "value_per_acre_usd_base":              _VALUE_PER_ACRE_USD_BASE,
            "value_range_spread":                   _VALUE_RANGE_SPREAD,
        },
        "data_sources": {
            "dcpi_verdict":          "dcpi_scores table — refreshed daily",
            "gas_pricing":           "routes/powered_land_gas.py — EIA v2 API",
            "comparable_sales":      "deals table — $324B+ M&A tracked",
            "market_centroids":      "hand-seeded top-30 markets (Phase 2: full markets table)",
        },
        "phase_2_roadmap": [
            "Per-utility delivered gas tariff (not state-avg)",
            "Regression-fit valuation envelope from comparable_sales",
            "Live ISO queue depth per scenario time-to-power",
            "Real substation proximity (HIFLD) replacing capex assumption",
            "Custom heat-rate input (currently fixed at 6800 Btu/kWh CCGT)",
        ],
    }), 200


# ── HTML page (/sites/value) ──────────────────────────────────────

_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Site Valuation — Grid vs Gas BTM vs Gas-to-Grid · DC Hub</title>
<meta name="description" content="Price your data-center site: 3-scenario NPV comparison + DCPI verdict + comparable sales. Subscribers only.">
<link rel="canonical" href="https://dchub.cloud/sites/value">
<style>
:root { --bg:#0a0a0a; --panel:#111827; --panel2:#1f2937; --fg:#f3f4f6; --muted:#9ca3af; --accent:#0EA5E9; --accent2:#38BDF8; --border:#374151; --ok:#10b981; --warn:#f59e0b; }
* { box-sizing: border-box; }
body { font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--fg); margin: 0; min-height: 100vh; padding: 24px; line-height: 1.55; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 36px; font-weight: 700; margin: 0 0 8px; }
.tagline { color: var(--muted); font-size: 16px; margin: 0 0 24px; max-width: 720px; }
.kicker { font-size: 12px; color: var(--accent2); letter-spacing: 0.12em; text-transform: uppercase; font-weight: 700; margin-bottom: 8px; }
form { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin: 24px 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; }
form label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--muted); }
form input { background: #0a0a0a; border: 1px solid var(--border); color: var(--fg); padding: 10px 12px; border-radius: 6px; font-size: 14px; font-family: inherit; }
form input:focus { outline: none; border-color: var(--accent); }
form button { grid-column: 1 / -1; background: var(--accent); color: #fff; border: 0; border-radius: 6px; padding: 12px 24px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }
form button:hover { background: var(--accent2); }
.row { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin: 16px 0; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
.card h3 { font-size: 18px; margin: 0 0 4px; }
.card .verdict { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px; }
.verdict-BUILD { background: var(--ok); color: #fff; }
.verdict-CAUTION { background: var(--warn); color: #000; }
.verdict-AVOID { background: #dc2626; color: #fff; }
.stat { font-size: 28px; font-weight: 700; color: var(--accent2); margin: 8px 0 4px; }
.stat-label { font-size: 12px; color: var(--muted); }
.scen-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 16px 0; }
.scen-card { background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.scen-card.best { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(14, 165, 233, 0.2); }
.scen-card h4 { margin: 0 0 8px; font-size: 14px; color: var(--accent2); }
.scen-card .num { font-size: 24px; font-weight: 700; }
.scen-card .small { font-size: 11px; color: var(--muted); }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }
th, td { padding: 8px 12px; border-bottom: 1px solid var(--border); text-align: left; }
th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
.upgrade { background: linear-gradient(135deg, rgba(14, 165, 233, 0.1), rgba(56, 189, 248, 0.1)); border: 1px solid var(--accent); border-radius: 12px; padding: 24px; margin: 16px 0; }
.upgrade h3 { margin: 0 0 8px; color: var(--accent2); }
.upgrade a.btn { display: inline-block; background: var(--accent); color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 600; margin-top: 12px; }
.hidden { display: none; }
.error { color: #dc2626; padding: 12px; background: rgba(220, 38, 38, 0.1); border-radius: 8px; }
@media (max-width: 768px) { .scen-grid { grid-template-columns: 1fr; } h1 { font-size: 28px; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="kicker">⌖  Site Valuation Engine  ·  PRO</div>
  <h1>What is your site worth?</h1>
  <p class="tagline">3-scenario NPV: <b>Grid</b> vs <b>Gas BTM</b> vs <b>Gas-to-Grid Hybrid</b>. Built for sellers, landowners, and developers pricing power-ready parcels. Powered by DCPI verdicts across 234+ markets and live gas hub pricing.</p>

  <form id="valForm">
    <label>Lat<br><input id="lat" type="number" step="0.0001" value="33.45" required></label>
    <label>Lon<br><input id="lon" type="number" step="0.0001" value="-112.07" required></label>
    <label>Acres<br><input id="acres" type="number" step="0.1" value="50" required></label>
    <label>Target MW<br><input id="target_mw" type="number" step="1" value="100" required></label>
    <label>Deadline (months)<br><input id="deadline_months" type="number" step="1" value="24"></label>
    <label>API Key (PRO unlock)<br><input id="api_key" type="password" placeholder="dchub_..."></label>
    <button type="submit">Calculate valuation →</button>
  </form>

  <div id="results" class="hidden"></div>
  <div id="error" class="hidden error"></div>

  <p style="font-size:12px;color:var(--muted);margin-top:32px;">
    Methodology: <a href="/api/v1/site/value/methodology" style="color:var(--accent2)">/api/v1/site/value/methodology</a> · Phase 1 ships today; Phase 2 adds per-utility tariffs, regression-fit valuation envelope, and live ISO queue depth.
  </p>
</div>

<script>
document.getElementById('valForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  document.getElementById('error').classList.add('hidden');
  document.getElementById('results').classList.add('hidden');

  const body = {
    lat: parseFloat(document.getElementById('lat').value),
    lon: parseFloat(document.getElementById('lon').value),
    acres: parseFloat(document.getElementById('acres').value),
    target_mw: parseInt(document.getElementById('target_mw').value),
    deadline_months: parseInt(document.getElementById('deadline_months').value || 24),
  };
  const apiKey = document.getElementById('api_key').value.trim();
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-API-Key'] = apiKey;

  try {
    const r = await fetch('/api/v1/site/value', {
      method: 'POST', headers, body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok) {
      document.getElementById('error').textContent = d.hint || d.error || 'Unknown error';
      document.getElementById('error').classList.remove('hidden');
      return;
    }
    document.getElementById('results').innerHTML = renderResults(d);
    document.getElementById('results').classList.remove('hidden');
  } catch (err) {
    document.getElementById('error').textContent = 'Request failed: ' + err.message;
    document.getElementById('error').classList.remove('hidden');
  }
});

function fmt$(v) { return '$' + Math.round(v).toLocaleString(); }
function fmtM$(v) { return '$' + (v / 1_000_000).toFixed(1) + 'M'; }
function renderResults(d) {
  const dcpi = d.dcpi_context || {};
  const verdictClass = 'verdict-' + (dcpi.verdict || 'UNKNOWN');
  let html = `
    <div class="row">
      <div class="card">
        <h3>Market context</h3>
        <div class="verdict ${verdictClass}">${dcpi.verdict || 'unknown'}</div>
        <div class="stat">${(d.market_context.nearest_market_slug || '').replace(/-/g, ' ')}</div>
        <div class="stat-label">${d.market_context.nearest_market_state} · ${d.market_context.miles_from_centroid} mi from centroid · ISO: ${dcpi.iso || 'n/a'}</div>
        <div style="margin-top:12px;font-size:13px;color:var(--muted)">
          DCPI composite: <b>${(dcpi.composite_score || 0).toFixed(1)}</b> · Excess power: <b>${(dcpi.excess_power_score || 0).toFixed(1)}</b> · Time-to-power: <b>${(dcpi.time_to_power_months || 0).toFixed(0)} months</b>
        </div>
      </div>`;

  if (d.valuation) {
    html += `
      <div class="card">
        <h3>Valuation envelope</h3>
        <div class="stat">${fmtM$(d.valuation.site_value_usd_mid)}</div>
        <div class="stat-label">midpoint · ${fmtM$(d.valuation.site_value_usd_low)} – ${fmtM$(d.valuation.site_value_usd_high)}</div>
        <div style="margin-top:12px;font-size:13px;color:var(--muted)">
          <b>${fmt$(d.valuation['$/mw_mid'])}</b> / MW &nbsp;·&nbsp; <b>${fmt$(d.valuation['$/acre_mid'])}</b> / acre
        </div>
      </div>`;
  } else if (d.valuation_teaser) {
    html += `
      <div class="card">
        <h3>Valuation midpoint</h3>
        <div class="stat">${fmtM$(d.valuation_teaser.site_value_usd_mid)}</div>
        <div class="stat-label">teaser · full envelope is PRO</div>
      </div>`;
  }
  html += '</div>';

  if (d.scenarios) {
    const bestName = d.best_fit && d.best_fit.scenario;
    html += '<h3 style="margin-top:24px">3-scenario NPV comparison</h3>';
    html += '<div class="scen-grid">';
    [['grid_only', 'Grid-only'], ['gas_btm', 'Gas BTM (CCGT)'], ['gas_to_grid_hybrid', 'Gas-to-Grid Hybrid']].forEach(([key, label]) => {
      const s = d.scenarios[key];
      if (!s) return;
      const cls = key === bestName ? 'scen-card best' : 'scen-card';
      html += `<div class="${cls}">
        <h4>${label}${key === bestName ? ' ★' : ''}</h4>
        <div class="num">${fmt$(s.levelized_usd_per_mwh)}/MWh</div>
        <div class="small">levelized cost</div>
        <table style="margin-top:8px;font-size:12px">
          <tr><td>CapEx</td><td style="text-align:right">${fmtM$(s.capex_usd)}</td></tr>
          <tr><td>Annual OpEx</td><td style="text-align:right">${fmtM$(s.annual_opex_usd)}</td></tr>
          <tr><td>10-yr NPV</td><td style="text-align:right">${fmtM$(s.ten_year_npv_usd)}</td></tr>
          <tr><td>Time to power</td><td style="text-align:right">${s.time_to_power_months}mo</td></tr>
        </table>
      </div>`;
    });
    html += '</div>';
    if (d.best_fit && d.best_fit.rationale) {
      html += `<div class="card" style="margin-top:12px"><b>Best fit:</b> ${d.best_fit.rationale}</div>`;
    }
  } else if (d.scenarios_teaser) {
    html += '<h3 style="margin-top:24px">Time-to-power per scenario (teaser)</h3>';
    html += '<div class="scen-grid">';
    [['grid_only', 'Grid-only'], ['gas_btm', 'Gas BTM (CCGT)'], ['gas_to_grid_hybrid', 'Gas-to-Grid Hybrid']].forEach(([key, label]) => {
      const s = d.scenarios_teaser[key];
      html += `<div class="scen-card">
        <h4>${label}</h4>
        <div class="num">${s.time_to_power_months}mo</div>
        <div class="small">time to power</div>
      </div>`;
    });
    html += '</div>';
  }

  if (d.comparable_sales && d.comparable_sales.length > 0) {
    html += '<h3 style="margin-top:24px">Comparable transactions</h3>';
    html += '<table><tr><th>Date</th><th>Target</th><th>Acquirer</th><th>Value</th><th>Type</th></tr>';
    d.comparable_sales.forEach(c => {
      html += `<tr><td>${c.announced_date || '—'}</td><td>${c.target || '—'}</td><td>${c.acquirer || '—'}</td><td>${c.value_usd ? fmtM$(c.value_usd) : '—'}</td><td>${c.deal_type || '—'}</td></tr>`;
    });
    html += '</table>';
  }

  if (d.upgrade_hint) {
    html += `<div class="upgrade">
      <h3>${d.upgrade_hint.human_message}</h3>
      <p style="color:var(--muted);font-size:13px">Unlock with PRO:</p>
      <ul style="margin:0;padding-left:20px;font-size:13px">${(d.upgrade_hint.what_you_unlock || []).map(x => `<li>${x}</li>`).join('')}</ul>
      <a class="btn" href="${d.upgrade_hint.signup_url}">Upgrade to PRO →</a>
    </div>`;
  }
  return html;
}
</script>
</body></html>"""


@site_valuation_engine_bp.route("/sites/value", methods=["GET"],
                                 strict_slashes=False)
def site_value_page():
    return Response(_PAGE_HTML, mimetype="text/html; charset=utf-8")
