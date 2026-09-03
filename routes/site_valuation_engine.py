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
# r-readiness-capex (2026-08-12): with an ISA signed / queue cleared, the study
# and network-upgrade allocation is settled (often already paid) but the physical
# point-of-interconnection work is NOT. Charge the remaining share, not the full
# greenfield figure and not zero. TUNABLE.
_CAPEX_INTERCONNECT_ISA_SIGNED_FRACTION = 0.50

# OpEx assumptions
_GRID_AVG_LMP_USD_PER_MWH           = 45       # US blended average 2024
_HOURS_PER_YEAR                      = 8760
_CCGT_HEAT_RATE_BTU_PER_KWH         = 6800     # avg modern CCGT
_PEAKER_HEAT_RATE_BTU_PER_KWH       = 10500    # simple-cycle
_GAS_PRICE_FALLBACK_USD_PER_MMBTU   = 3.50     # if EIA data missing

# r-om (2026-06-30): the 3-scenario LCOE previously counted fuel/energy cost
# ONLY. Real all-in $/MWh must also carry fixed O&M (staff, insurance, major
# maintenance — paid per kW of nameplate regardless of dispatch) and variable
# O&M (consumables/wear per MWh generated). Omitting them understated the
# gas-BTM LCOE by ~$6-8/MWh (~$44 → ~$50-52). Grid delivery carries a smaller
# but non-zero fixed O&M. Ranking (gas-BTM cheapest) is unchanged — this is a
# realism/defensibility fix. All three are TUNABLE via the overrides dict.
_CCGT_FIXED_OM_USD_PER_KW_YR        = 30       # $25-40/kW-yr typical CCGT
_CCGT_VARIABLE_OM_USD_PER_MWH       = 3.0      # $2-4/MWh consumables/wear
_GRID_FIXED_OM_USD_PER_KW_YR        = 5        # transmission/delivery fixed O&M

# r-cf (2026-06-30): annual_mwh was mw × 8760 (a 100% capacity factor), which
# is unrealistic — a dedicated BTM plant reserves ~5-10% for maintenance/reserve
# downtime. A lower CF both trims fuel MWh AND shrinks the denominator that
# amortizes capex, raising every scenario's levelized $/MWh. Applied uniformly
# to annual_mwh so all three scenarios stay compared on the same delivered-MWh.
_CAPACITY_FACTOR                     = 0.92    # ~92% avg availability; 0<cf<=1

# NPV horizon + discount rate
_NPV_HORIZON_YEARS                   = 10
_DISCOUNT_RATE                       = 0.08    # standard infrastructure WACC

# Valuation multiples (per MW + per acre, market-typical)
# v2.0 recalibration (2026-06-04): the prior $2M/MW baseline was 3-13x
# the user-supplied industry range of $150K-$800K/MW. Recalibrated to
# the midpoint of that range; verdict multipliers now span the full
# AVOID(0.40) - BUILD(1.65) envelope; per-acre baseline aligned with
# industrial-zoned land comps; spread widened from ±30% to ±50% to
# reflect actual illiquidity of these parcels.
_VALUE_PER_MW_USD_BASE               = 475_000   # midpoint of $150K-$800K/MW industry range
_VALUE_PER_ACRE_USD_BASE             = 15_000    # industrial-zoned land, midpoint $5K-$30K/acre
_VALUE_RANGE_SPREAD                  = 0.50      # ±50% low/high envelope

# v2.1b (2026-06-04) — hard band the engine MUST stay inside. The
# unclamped multiplier stack (verdict 1.65 × bestfit 1.10 × readiness 3.35
# = 6.08× base) can run BUILD+shovel-ready to ~$2.3M/MW, which busts the
# $800K ceiling we publish in the methodology footer. Clamping per-MW
# into [FLOOR, CEIL] keeps the headline figure honest. Side-effect: BUILD
# + fully-shovel-ready hyperscale sites all saturate at $800K/MW
# (no longer differentiated above the cap) — that's the trade we want
# for now; v2.2 can introduce a soft asymptote if the saturation hides
# real signal between premium tiers.
_VALUE_PER_MW_FLOOR_USD              = 150_000   # industry floor
_VALUE_PER_MW_CEIL_USD               = 800_000   # industry ceiling (methodology footer)
# r-entitled (2026-06-30): a FULLY ENTITLED site (zoned + permitted) is de-risked
# — 12-24 mo + entitlement cost/risk removed — and trades ABOVE the raw-land
# ceiling. When both entitlement flags are set the ceiling lifts to this, so
# zoning + permits show as real dollars instead of vanishing under the $800K cap.
_VALUE_PER_MW_CEIL_ENTITLED_USD      = 1_200_000  # ceiling for zoned + permitted sites
# r-powered (2026-06-30): FIRM near-term power (grid TTP <=18mo — delivered/near-
# firm MW, not a multi-year queue) is the market's scarcest asset; powered PJM
# land trades above just-entitled. Ceiling for an entitled site WITH firm power.
_VALUE_PER_MW_CEIL_POWERED_USD       = 1_600_000  # ceiling for entitled + firm-powered sites

# r-moat-developing (2026-08-12): per-moat-flag verdict lift, by AVOID subtype.
# `constrained` (Ashburn-class): the scarcity IS the moat, so a ready parcel
# lifts hard, to ~CAUTION-plus. `developing` (verdict tracks readiness, by the
# classifier's own definition): lifts at roughly half the rate and caps at the
# engine's no-verdict default. `weak_demand` is absent on purpose — no moat.
# Keys MUST stay in sync across both dicts; the lookup falls back defensively.
_MOAT_LIFT_PER_FLAG = {"constrained": 0.22, "developing": 0.12}
_MOAT_LIFT_CAP      = {"constrained": 1.05, "developing": 0.85}

# Site-readiness premium stack (multiplicative). A fully-shovel-ready
# parcel (all 6 flags TRUE) gets a 3.35x premium over raw land:
#   1.30 × 1.25 × 1.10 × 1.20 × 1.30 × 1.20 = 3.35
# This is how a raw AVOID-tier Phoenix site at ~$200K/MW becomes a
# $600-700K/MW shovel-ready site (in the upper-mid of the industry range).
_READINESS_PREMIUMS = {
    "grid_interconnect_ready": 1.30,  # ISA signed / queue cleared
    "substation_on_site":      1.25,  # existing sub within parcel
    "water_secured":           1.10,  # WSA / permit in place
    "fiber_on_site":           1.20,  # dark fiber tap exists
    "zoning_approved":         1.30,  # industrial / data-center zoning
    "permits_in_hand":         1.20,  # SLR / AQMD / etc cleared
}

# v2.1 Phase 3 (2026-06-04) — substation distance → capex tiers.
# Replaces the fixed _CAPEX_SUBSTATION_BUILD_USD with a HIFLD-derived
# proximity bucket. Within 5 mi we assume interconnect-only ($1M tap
# work); 5-25 mi linearly scales to $4M (run a lateral); >25 mi or no
# sub found = full $8M new-build assumption.
_SUBSTATION_PROXIMITY_TIERS = [
    (5.0,  1_000_000, "in_parcel_or_adjacent"),
    (25.0, 4_000_000, "lateral_build"),
    (float("inf"), 8_000_000, "new_substation_build"),
]

# v2.1 — minimum comp count for using market-specific per-MW baseline
# (regression-fit) instead of the global $475K baseline.
_MIN_COMPS_FOR_REGRESSION = 8

# v2.2 — Site sufficiency: data-center parcels TRADE BY THE MW. The land
# is implicit in the $/MW comp (every comp transaction includes the
# acres it sits on at no separate line-item). Summing $/acre × acres on
# TOP of $/MW × MW double-counts the land. The headline valuation is
# now $/MW × MW only; acres are reported as a sufficiency check (does
# the parcel fit the build envelope?), not as additive value.
#
# Industry rule of thumb for hyperscale DC siting (rack-density-agnostic):
#   < 1.0 ac/MW   → undersized; buyer struggles to fit campus + buffer
#   1.0–1.5 ac/MW → tight (single-story Tier III, no expansion room)
#   1.5–2.5 ac/MW → typical hyperscale campus
#   > 2.5 ac/MW   → comfortable / room for solar + substation + expansion
_ACRES_PER_MW_UNDERSIZED  = 1.0   # below this: red flag
_ACRES_PER_MW_TYPICAL_MIN = 1.5
_ACRES_PER_MW_TYPICAL_MAX = 2.5

# Surplus-acreage residual land value: if the parcel has acres above
# ~3× the typical max, those extra acres have separate industrial-land
# value (split-and-resell). Conservative residual until we wire up real
# zoned-industrial comps per market.
_RESIDUAL_LAND_USD_PER_ACRE = 8_000   # $8K/acre baseline for surplus
_SURPLUS_THRESHOLD_ACRES_PER_MW = 3.0  # only acres above 3× MW count as surplus


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


def _coerce_ttp_months(raw) -> Optional[int]:
    """Months-to-firm-power from the payload, or None to use the market queue.

    r-ttp0 (2026-08-12): `0` — "power already flows; zero months to firm MW" —
    used to sit in the discard tuple, so the ONE value that describes an
    ALREADY-ENERGIZED site was the one value silently thrown away, and the site
    fell back to the multi-year market queue. Only None / "" / absent mean "use
    the market queue"; 0 is a real answer, and for powered land it is the whole
    point. bool is rejected because `True` is an int in Python.
    """
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in miles."""
    R = 3958.8
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _nearest_market(lat: float, lon: float) -> Tuple[str, str, float]:
    """Return (slug, state, distance_miles) for the closest DCPI market.
    r-nearestmkt (2026-06-30): query the 119 DCPI markets that carry real
    lat/lng (market_power_scores) instead of only ~30 hand-seeded centroids — a
    site was snapping to a market 100+ mi away and borrowing its verdict
    (Millville NJ → NoVA/VA, 130 mi). Also guarantees the returned slug EXISTS in
    market_power_scores so _fetch_dcpi resolves it. Falls back to the hardcoded
    centroids on any DB issue."""
    try:
        with _db_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT market_slug, state, latitude, longitude "
                        "  FROM market_power_scores "
                        " WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
            best = None
            for slug, st, mlat, mlon in cur.fetchall():
                d = _haversine_miles(lat, lon, float(mlat), float(mlon))
                if best is None or d < best[2]:
                    best = (slug, st, d)
            if best:
                return best
    except Exception:
        pass
    best = None
    for slug, (state, mlat, mlon) in _MARKET_CENTROIDS.items():
        d = _haversine_miles(lat, lon, mlat, mlon)
        if best is None or d < best[2]:
            best = (slug, state, d)
    return best  # type: ignore[return-value]


# ── Verdict-subtype classifier (v2.1c — Ashburn moat fix) ────────
#
# DCPI verdict AVOID is methodologically right for BOTH a no-demand
# rust-belt town AND a saturated hyperscale cluster like Ashburn —
# but the implications for SITE VALUE are opposite:
#
#   constrained  → high demand + slow queue → AVOID for new builds,
#                  BUT shovel-ready parcels are MOAT-protected. The
#                  constraint IS what makes the interconnected parcel
#                  valuable. Ashburn / Northern VA / Phoenix / Santa
#                  Clara live here.
#   weak_demand  → no growth + open queue → AVOID for any build.
#                  Rural rust-belt towns. Site value at the floor.
#   developing   → moderate growth, queue ok → BUILD candidate
#                  whose verdict mainly tracks readiness.
#   n/a          → not AVOID; classifier doesn't change anything.
#
# Threshold tuning is intentionally conservative — we want the
# constrained class to fire ONLY when the data really screams
# "saturated cluster," not for every borderline AVOID.

# Markets known to be constrained even when the recompute hasn't
# tagged them with per-market metrics yet. Used as a defense-in-depth
# whitelist so a marquee Tier-1 PJM/CAISO market never gets a
# generic weak_demand subtype just because the ISO bucket dominates.
# Update this list as DCPI's per-slug overrides expand.
_CONSTRAINED_MARQUEES = {
    "ashburn", "northern-virginia", "columbus", "new-albany",
    "atlanta", "dallas", "dallas-fort-worth",
    "phoenix",
    "santa-clara", "silicon-valley", "los-angeles",
    "chicago", "richmond",
    "miami", "tampa", "jacksonville",
    "london", "frankfurt", "amsterdam", "dublin", "singapore",
    "tokyo", "sydney",
}


def _compute_verdict_subtype(dcpi: dict, slug: str = "") -> str:
    """Classify the WHY behind an AVOID verdict."""
    if not dcpi.get("available"):
        return "unknown"
    verdict = (dcpi.get("verdict") or "").upper()
    if verdict in ("BUILD", "CAUTION"):
        return "n/a"
    ttp = float(dcpi.get("time_to_power_months") or 0)
    excess = float(dcpi.get("excess_power_score") or 0)
    constraint = float(dcpi.get("constraint_score") or 0)
    # Constrained: hyperscale cluster — slow queue + measurable
    # constraint pressure (i.e. real demand bidding against scarce
    # capacity). The marquee whitelist catches markets where the ISO
    # bucket dominates but real-world context says "constrained."
    if ttp >= 36 and constraint >= 50:
        return "constrained"
    if slug and slug.lower() in _CONSTRAINED_MARQUEES:
        return "constrained"
    # Weak demand: low excess + low constraint = nothing happening
    if excess < 25 and constraint < 35:
        return "weak_demand"
    return "developing"


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
                # market_power_scores schema (from routes/dcpi.py:153) has
                # excess_power_score, constraint_score, time_to_power_months,
                # verdict, iso, computed_at but NO composite_score column —
                # the composite is derived via routes.dcpi.derive_composite_score
                # in the canonical handler. Mirror that pattern.
                cur.execute("""
                    SELECT verdict, excess_power_score, constraint_score,
                           time_to_power_months, iso, computed_at
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

        verdict      = row[0]
        excess       = float(row[1] or 0)
        constraint   = float(row[2] or 0)
        ttp_months   = float(row[3] or 36)
        iso          = row[4]
        computed_at  = row[5]

        # Derive composite_score using the canonical formula
        try:
            from routes.dcpi import derive_composite_score
            composite = derive_composite_score(excess, constraint, ttp_months, verdict)
        except Exception:
            # Fallback formula if import fails: weighted average
            # 50% excess + 50% (100 - constraint), then verdict adjustment
            composite = round((excess + (100 - constraint)) / 2.0, 1)

        out = {
            "available":             True,
            "slug":                  matched or slug,
            "verdict":               verdict,
            "composite_score":       composite,
            "excess_power_score":    excess,
            "constraint_score":      constraint,
            "time_to_power_months":  ttp_months,
            "iso":                   iso,
            "last_updated":          computed_at.isoformat() if computed_at else None,
        }
        # v2.1c — classify AVOID by why. Lets the valuation engine
        # and the UI distinguish constraint-driven AVOID (Ashburn,
        # Phoenix) from weak-demand AVOID (rural rust belt). Same
        # verdict letter, opposite economic implications.
        out["verdict_subtype"] = _compute_verdict_subtype(out, slug=slug)
        return out
    except Exception as e:
        return {"available": False, "reason": "query_error",
                "error": str(e)[:200]}
    finally:
        _safe_close(c)


# ── Gas pricing lookup ────────────────────────────────────────────

def _fetch_gas_economics(slug: str, state: str, lat: float = None) -> dict:
    """Use the existing powered_land_gas helpers to derive gas $/MMBtu
    and $/MWh for the slug. Falls back to representative values."""
    # r-gasfix (2026-06-30): the old import names (_HUB_FOR_STATE,
    # _HUB_DEFINITIONS, _fetch_gas_price_basis) NEVER existed in
    # powered_land_gas — the ImportError was swallowed by the except below, so
    # EVERY market silently fell back to a flat $3.50/MMBtu. Use the real hub
    # model: Henry Hub seed + the hub's basis differential (Waha discount,
    # Algonquin premium, …). Pure in-memory maps — instant, per-hub, no live EIA
    # call in the valuation hot path (keeps the POST fast; a live-EIA refresh of
    # the HH seed belongs on the gas endpoint / a cron, not here).
    try:
        from routes.powered_land_gas import (
            _STATE_TO_HUB, _MARKET_HUB_OVERRIDE, _HUB_NAME,
            SYNTHETIC_HENRY_HUB_USD_MMBTU, SYNTHETIC_BASIS_BY_HUB,
        )
        _s = (slug or "").lower()
        _st = (state or "").upper()
        hub_key = (_MARKET_HUB_OVERRIDE.get(_s)
                   or _STATE_TO_HUB.get(_st, "henry_hub"))
        # r-southnj (2026-06-30, client-audit): NJ maps state-wide to Transco
        # Zone 6 NY — a NORTH-Jersey/NYC delivery zone. SOUTH Jersey (Millville/
        # Cumberland, ~lat<40.2) prices off Tetco M3 (South Jersey Gas / PA
        # basin), cheaper + not NYC-constrained. Correct the basin by latitude.
        if _st == "NJ" and lat is not None and lat < 40.2:
            hub_key = "tetco_m3"
        hub_name = _HUB_NAME.get(hub_key, "Henry Hub")
        basis = float(SYNTHETIC_BASIS_BY_HUB.get(hub_key, 0.0))
        delivered = max(1.0, SYNTHETIC_HENRY_HUB_USD_MMBTU + basis)
        return {
            "available":         True,
            "hub_key":           hub_key,
            "hub_name":          hub_name,
            "basis_vs_hh":       round(basis, 2),
            "gas_$/MMBtu":       round(delivered, 2),
            "data_basis":        f"hub_model (HH ${SYNTHETIC_HENRY_HUB_USD_MMBTU:.2f} + {basis:+.2f} basis)",
            "$/MWh_ccgt_avg":    round(delivered * _CCGT_HEAT_RATE_BTU_PER_KWH / 1000, 2),
            "$/MWh_peaker":      round(delivered * _PEAKER_HEAT_RATE_BTU_PER_KWH / 1000, 2),
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


# ── Power cost + tax abatement (real per-state operating economics) ───────
_NATIONAL_IND_POWER_USD_MWH = 87.0   # EIA industrial median $/MWh (2026-04), refreshable
# r-largeload (2026-06-30): a 100MW+ transmission-connected data center pays FAR
# less than the EIA industrial RETAIL rate — it connects at transmission voltage
# (bypassing distribution), buys near wholesale + capacity/transmission adders,
# and negotiates. ~0.60× retail lands in the realistic $60-100/MWh all-in band
# (NJ $152→$91, VA $99→$59, TX $63→$38) instead of an implausible $150+ grid LCOE.
# TUNABLE. Floored so it never drops below a wholesale-ish $35.
_LARGE_LOAD_POWER_FACTOR = 0.60
_LARGE_LOAD_POWER_FLOOR_USD = 35.0


def _fetch_power_cost_usd_mwh(state: str) -> dict:
    """Per-state ALL-IN industrial power cost ($/MWh) from EIA
    (eia_electricity_rates). r-power (2026-06-30): replaces the flat $45/MWh
    'grid LMP' — which was wholesale-energy-only and sat BELOW every real state
    ($59 cheapest) — with the delivered industrial rate a grid-powered load
    actually pays (TX $63 · WA $70 · VA $99 · NJ $152 · CA $199). This is where
    power cost belongs: the operating NPV, NOT the land base (premium markets
    like NoVA have pricey power AND the highest land value). Falls back
    COM -> ALL -> national median."""
    try:
        with _db_conn() as c:
            cur = c.cursor()
            for sector in ('IND', 'COM', 'ALL'):
                cur.execute(
                    "SELECT price_cents_kwh FROM eia_electricity_rates "
                    " WHERE state=%s AND sector=%s AND price_cents_kwh>0 "
                    " ORDER BY period DESC LIMIT 1", (state, sector))
                r = cur.fetchone()
                if r and r[0]:
                    retail = round(float(r[0]) * 10, 1)   # cents/kWh → $/MWh
                    adj = round(max(_LARGE_LOAD_POWER_FLOOR_USD,
                                    retail * _LARGE_LOAD_POWER_FACTOR), 1)
                    return {"usd_mwh": adj, "retail_usd_mwh": retail,
                            "sector": sector, "source": "eia_electricity_rates (large-load adj)"}
    except Exception:
        pass
    _def = round(max(_LARGE_LOAD_POWER_FLOOR_USD,
                     _NATIONAL_IND_POWER_USD_MWH * _LARGE_LOAD_POWER_FACTOR), 1)
    return {"usd_mwh": _def, "retail_usd_mwh": _NATIONAL_IND_POWER_USD_MWH,
            "sector": "default", "source": "national_median (large-load adj)"}


def _fetch_tax_abatement(state: str) -> dict:
    """Property-tax abatement value factor from tax_incentives_neon (50 states).
    A DC-specific, long-duration abatement de-risks a decade+ of opex → a modest,
    transferable site-value premium (capped +8%)."""
    try:
        with _db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT property_tax_abatement, COALESCE(duration_years,0), "
                "       COALESCE(data_center_specific,false), COALESCE(max_benefit,'') "
                "  FROM tax_incentives_neon WHERE state_abbr=%s LIMIT 1", (state,))
            r = cur.fetchone()
            if r and r[0]:
                dur = int(r[1] or 0); dc = bool(r[2]); maxb = (r[3] or '')
                pct = 0.03 + min(0.04, (dur / 20.0) * 0.04) + (0.02 if dc else 0.0)
                pct = round(min(0.08, pct), 3)
                return {"abatement": True, "factor": round(1 + pct, 3), "pct": pct,
                        "duration_years": dur, "data_center_specific": dc,
                        "max_benefit": maxb,
                        "note": (f"{'Data-center-specific ' if dc else ''}property-tax "
                                 f"abatement ({dur} yr{'s' if dur != 1 else ''}"
                                 f"{', ' + maxb if maxb else ''}) — +{round(pct*100)}% site value. "
                                 + ("" if dc else "★ Generic STATE incentive (not DC-specific, "
                                    "not site-verified) — confirm the parcel's actual abatement "
                                    "term; a longer site-specific deal is worth more."))}
    except Exception:
        pass
    return {"abatement": False, "factor": 1.0, "pct": 0.0,
            "note": "No property-tax abatement on record for this state."}


def _site_state(lat: float, lon: float, fallback: str) -> tuple:
    """Resolve the site's ACTUAL state from lat/lon via the free FCC Area API.
    r-sitestate (2026-06-30): _nearest_market returns the nearest of ~30 hand-
    seeded market centroids + ITS state, which can be 100+ mi off (Millville NJ
    resolved to NoVA/VA). Power cost + tax abatement are STATE-keyed, so use the
    real site state. Fail-soft: 3s timeout → fall back to the nearest-market
    state so a geocode hiccup never breaks (or slows) the valuation."""
    try:
        import requests
        r = requests.get("https://geo.fcc.gov/api/census/area",
                         params={"lat": lat, "lon": lon, "format": "json"},
                         timeout=3)
        if r.status_code == 200:
            for res in (r.json().get("results") or []):
                st = res.get("state_code")
                if st:
                    return st, "fcc_geocode"
    except Exception:
        pass
    return fallback, "nearest_market_fallback"


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
            # r-comps (2026-06-30): the real `deals` columns are seller/buyer/
            # value($M)/mw/market/type (NOT target/acquirer/value_usd/
            # announced_date) — the old query threw on EVERY call and was
            # swallowed, so comps never rendered. These are M&A/JV transactions
            # (enterprise value), shown as market CONTEXT — NOT land comps and
            # NOT the price anchor (deals price whole operating companies at
            # $M-per-MW, the wrong reference class for raw land).
            for tbl in ("deals", "transactions", "discovered_deals"):
                try:
                    cur.execute(f"""
                        SELECT seller, buyer, value, mw, market,
                               COALESCE(type,''), COALESCE(deal_date::text, date::text, '')
                          FROM {tbl}
                         WHERE (LOWER(COALESCE(market, '')) LIKE %s
                                OR LOWER(COALESCE(market, '')) LIKE %s)
                           AND value > 0
                         ORDER BY COALESCE(deal_date::text, date::text, '') DESC
                         LIMIT %s
                    """, ("%" + slug + "%",
                          "%" + (slug.replace("-", " ")) + "%",
                          limit))
                    rows = cur.fetchall()
                    if rows:
                        out = []
                        for r in rows:
                            _vm = float(r[2]) if r[2] is not None else None   # $ millions
                            _mw = float(r[3]) if r[3] else None
                            out.append({
                                "target":         r[0],   # seller / acquired
                                "acquirer":       r[1],   # buyer
                                "value_usd":      round(_vm * 1e6, 0) if _vm else None,
                                "mw":             _mw,
                                "market":         r[4],
                                "deal_type":      r[5],
                                "announced_date": r[6] or None,
                                "context_only":   True,   # M&A context, NOT a land comp
                            })
                        return out
                except Exception:
                    continue
            return []
    except Exception:
        return []
    finally:
        _safe_close(c)


# ── Phase 3 (v2.1) helpers — graceful-fallback overrides ──────────

def _fetch_nearest_substation(lat: float, lon: float,
                                max_miles: float = 50.0) -> dict:
    """v2.1 item 4 — query substations table for nearest sub by haversine
    distance. Returns the proximity tier + dollar capex assumption that
    replaces the fixed _CAPEX_SUBSTATION_BUILD_USD. Falls back to
    'new_substation_build' tier if table missing or no hit."""
    c = _db_conn()
    if c is None:
        return {"available": False, "capex_usd": 8_000_000,
                  "tier": "new_substation_build", "reason": "db_unavailable"}
    try:
        with c.cursor() as cur:
            # Pre-filter by lat/lon bounding box (50 mi ≈ 0.72 deg lat)
            # before running haversine — keeps the query fast on a large
            # substations table.
            lat_delta = max_miles / 69.0
            lon_delta = max_miles / max(50.0, 69.0 * math.cos(math.radians(lat)))
            cur.execute("""
                SELECT name, lat, lng
                  FROM substations
                 WHERE lat BETWEEN %s AND %s
                   AND lng BETWEEN %s AND %s
                 LIMIT 200
            """, (lat - lat_delta, lat + lat_delta,
                  lon - lon_delta, lon + lon_delta))
            rows = cur.fetchall() or []
        if not rows:
            return {"available": True, "capex_usd": 8_000_000,
                      "tier": "new_substation_build",
                      "miles_to_nearest": None,
                      "reason": "no_sub_within_radius"}
        # Find the nearest by exact haversine.
        nearest = None
        for name, slat, slng in rows:
            try:
                d = _haversine_miles(lat, lon, float(slat), float(slng))
            except (TypeError, ValueError):
                continue
            if nearest is None or d < nearest["miles"]:
                nearest = {"name": name, "miles": d}
        if nearest is None:
            return {"available": True, "capex_usd": 8_000_000,
                      "tier": "new_substation_build",
                      "miles_to_nearest": None,
                      "reason": "no_valid_coordinates"}
        # Walk proximity tiers — first match wins.
        for max_band, capex, tier_name in _SUBSTATION_PROXIMITY_TIERS:
            if nearest["miles"] <= max_band:
                return {
                    "available":        True,
                    "capex_usd":        capex,
                    "tier":             tier_name,
                    "miles_to_nearest": round(nearest["miles"], 2),
                    "nearest_name":     nearest["name"],
                }
        # Unreachable (last tier has float("inf"))
        return {"available": True, "capex_usd": 8_000_000,
                  "tier": "new_substation_build",
                  "miles_to_nearest": round(nearest["miles"], 2)}
    except Exception as e:
        return {"available": False, "capex_usd": 8_000_000,
                  "tier": "new_substation_build",
                  "reason": "query_error", "error": str(e)[:200]}
    finally:
        _safe_close(c)


def _fetch_market_comp_baseline(slug: str, state: str) -> dict:
    """r-comps (2026-06-30): NEUTRALIZED. This was meant to override the base
    $/MW with a regression on 'comparable' deals, but (a) it queried columns that
    never existed (value_usd/mw_purchased) so it always failed silently, and
    (b) the only transaction data (deals) is M&A ENTERPRISE value ($4-14M/MW —
    ~100x raw land), the wrong reference class; land_parcels (real land comps) is
    empty. The base is intentionally MODEL-driven (DCPI demand + operating
    economics), NOT comp-anchored — so this returns unavailable by design."""
    return {"available": False, "reason": "no_land_comps",
            "note": "Base is model-driven; M&A deals are enterprise value, not land comps."}

def _fetch_live_queue_ttp(iso: str, slug: str) -> dict:
    """v2.1 item 3 — derive months-to-energization from the live ISO queue
    snapshot instead of the DCPI's slower-refreshing time_to_power_months
    field. Calls /api/v1/interconnection-queue/snapshot internally."""
    if not iso:
        return {"available": False, "reason": "no_iso_supplied"}
    try:
        import requests as _rq
        base = (os.environ.get("DCHUB_INTERNAL_API")
                  or "http://127.0.0.1:8080").strip()
        r = _rq.get(f"{base}/api/v1/interconnection-queue/snapshot",
                    params={"iso": iso}, timeout=4,
                    headers={"User-Agent": "dchub-site-valuation/2.1"})
        if r.status_code != 200:
            return {"available": False, "reason": f"HTTP {r.status_code}"}
        d = r.json() or {}
        by_iso = d.get("by_iso") or {}
        # r-queuefix (2026-06-30, client-audit): the endpoint returns by_iso as a
        # LIST of ISO records, not a dict — `.get(iso)` threw ('list' has no .get)
        # and silently fell back to the DCPI TTP. Handle both shapes.
        if isinstance(by_iso, list):
            iso_data = next((x for x in by_iso if isinstance(x, dict)
                             and str(x.get("iso", "")).upper() == (iso or "").upper()), {})
        else:
            iso_data = by_iso.get(iso) or by_iso.get((iso or "").upper()) or {}
        depth_mw = iso_data.get("active_queue_mw") or iso_data.get("total_mw")
        velocity = iso_data.get("avg_completions_per_year_mw") or iso_data.get("90d_velocity_mw_per_year")
        if depth_mw and velocity and velocity > 0:
            # Months to clear ahead = (queue_depth / annual_velocity) × 12
            months = round((float(depth_mw) / float(velocity)) * 12, 0)
            return {
                "available":       True,
                "iso":             iso,
                "queue_depth_mw":  float(depth_mw),
                "velocity_mw_yr": float(velocity),
                "months_to_power": months,
                "source":          "live_iso_queue_snapshot",
            }
        return {"available": False, "reason": "iso_metrics_missing_in_snapshot",
                  "iso_keys": list(iso_data.keys())[:8]}
    except Exception as e:
        return {"available": False, "reason": "query_error",
                  "error": str(e)[:200]}


# ── 3-scenario NPV calculator ─────────────────────────────────────

def _npv(annual_cashflows: list, discount_rate: float = _DISCOUNT_RATE) -> float:
    return sum(cf / ((1 + discount_rate) ** (year + 1))
               for year, cf in enumerate(annual_cashflows))


def _compute_scenarios(target_mw: int, dcpi: dict, gas: dict,
                        overrides: dict = None) -> dict:
    """Compute Grid / BTM / Hybrid scenarios. Returns per-scenario:
      - capex_usd
      - annual_opex_usd
      - time_to_power_months
      - ten_year_npv_usd  (capex + 10yr opex discounted; NEGATIVE because
                            it's a cost-only view at this stage)
      - levelized_usd_per_mwh

    v2.1 overrides dict (all optional):
      substation_capex_usd  — replaces _CAPEX_SUBSTATION_BUILD_USD (item 4)
      live_queue_ttp_months — replaces grid_ttp (item 3)
      heat_rate_ccgt        — replaces _CCGT_HEAT_RATE_BTU_PER_KWH (item 5)
      heat_rate_peaker      — replaces _PEAKER_HEAT_RATE_BTU_PER_KWH (item 5)
      gas_usd_mmbtu_override — replaces gas economics (item 1, utility tariff)

    v2.1b (2026-06-04) new user-facing overrides — wired through the
    "Adjust assumptions" UI panel so a sophisticated user can stress-
    test scenarios against their own LMP/discount/heat-rate beliefs:
      grid_lmp_usd_per_mwh   — replaces _GRID_AVG_LMP_USD_PER_MWH ($45)
      discount_rate          — replaces _DISCOUNT_RATE (0.08)

    r-om / r-cf (2026-06-30) new all-in-LCOE overrides:
      capacity_factor        — replaces _CAPACITY_FACTOR (0.92); clamped (0,1]
      ccgt_fixed_om_usd_per_kw_yr    — replaces _CCGT_FIXED_OM_USD_PER_KW_YR
      ccgt_variable_om_usd_per_mwh   — replaces _CCGT_VARIABLE_OM_USD_PER_MWH
      grid_fixed_om_usd_per_kw_yr    — replaces _GRID_FIXED_OM_USD_PER_KW_YR
    """
    overrides = overrides or {}
    mw = max(1, int(target_mw))
    kw = mw * 1000
    # r-cf — annual generation is nameplate throttled by capacity factor, not a
    # 100% duty cycle. Clamp to (0,1] so a bad override can't zero the denominator.
    capacity_factor = float(overrides.get("capacity_factor") or _CAPACITY_FACTOR)
    capacity_factor = min(1.0, max(0.01, capacity_factor))
    annual_mwh = mw * _HOURS_PER_YEAR * capacity_factor

    # v2.1 item 4 — substation capex from HIFLD proximity (with fallback)
    sub_capex = float(overrides.get("substation_capex_usd")
                      or _CAPEX_SUBSTATION_BUILD_USD)
    # v2.1 item 5 — custom heat rates (default unchanged)
    heat_rate_ccgt = float(overrides.get("heat_rate_ccgt")
                           or _CCGT_HEAT_RATE_BTU_PER_KWH)
    # v2.1b — user-tunable LMP + discount (defaults to industry baselines)
    grid_lmp = float(overrides.get("grid_lmp_usd_per_mwh")
                     or _GRID_AVG_LMP_USD_PER_MWH)
    discount_rate = float(overrides.get("discount_rate")
                          or _DISCOUNT_RATE)
    # r-om — fixed O&M ($/kW-yr, dispatch-independent) + variable O&M ($/MWh).
    ccgt_fixed_om = float(overrides.get("ccgt_fixed_om_usd_per_kw_yr")
                          or _CCGT_FIXED_OM_USD_PER_KW_YR)
    ccgt_var_om = float(overrides.get("ccgt_variable_om_usd_per_mwh")
                        or _CCGT_VARIABLE_OM_USD_PER_MWH)
    grid_fixed_om = float(overrides.get("grid_fixed_om_usd_per_kw_yr")
                          or _GRID_FIXED_OM_USD_PER_KW_YR)
    # v2.1 item 1 — utility-tariff override for gas $/MWh in opex calc.
    # If supplied, recomputes $/MWh = ($/MMBtu × heat_rate × annual_mwh).
    gas_mmbtu_override = overrides.get("gas_usd_mmbtu_override")
    if gas_mmbtu_override:
        # Convert utility tariff $/MMBtu → $/MWh via heat rate.
        # $/MMBtu × (Btu/kWh / 1e6) × 1000 = $/MWh
        ccgt_dollars_per_mwh = (float(gas_mmbtu_override)
                                  * (heat_rate_ccgt / 1_000_000.0) * 1000.0)
    else:
        ccgt_dollars_per_mwh = gas.get("$/MWh_ccgt_avg", 25)

    # r-lcoe (2026-06-30): levelized cost must divide DISCOUNTED cost by
    # DISCOUNTED energy. The old denominator (annual_mwh × 10) was undiscounted,
    # producing LCOE BELOW the fuel cost (grid $33 < its own $45/MWh fuel —
    # impossible). Discount the MWh on the same 8% curve as the cash flows.
    discounted_mwh = _npv([annual_mwh] * _NPV_HORIZON_YEARS, discount_rate)

    # ── Grid-only ────────────────────────────────────────────────
    # v2.1 item 3 — prefer live queue depth over DCPI's TTP if available
    # r-ttp0: `or` discards a stated 0 (already-energized site) and falls back to
    # the market queue. Explicit None test — 0 months is a legitimate answer.
    _ttp_override = overrides.get("live_queue_ttp_months")
    grid_ttp = (_ttp_override if _ttp_override is not None
                else (dcpi.get("time_to_power_months", 36)
                      if dcpi.get("available") else 36))
    # r-readiness-capex (2026-08-12): readiness flags fed the VALUATION multiplier
    # but never the COST model, so the engine charged a full greenfield
    # interconnect + new-substation build at sites that already have both. A site
    # reporting a substation inside the parcel was still billed the $8M new-build,
    # which inflated grid-only capex and handed best-fit to gas-BTM — recommending
    # a nine-figure CCGT at sites whose whole thesis is that power already exists.
    _rdy = overrides.get("readiness") or {}
    if _rdy.get("substation_on_site"):
        sub_capex = 0.0
    _interconnect_per_kw = _CAPEX_GRID_INTERCONNECT_USD_PER_KW
    if _rdy.get("grid_interconnect_ready"):
        _interconnect_per_kw *= _CAPEX_INTERCONNECT_ISA_SIGNED_FRACTION
    grid_capex = kw * _interconnect_per_kw + sub_capex
    # r-om — LMP energy + delivery-side fixed O&M (no plant-side variable O&M;
    # the LMP already embeds generation costs).
    grid_opex_yr = annual_mwh * grid_lmp + kw * grid_fixed_om
    grid_npv = -(grid_capex + _npv([grid_opex_yr] * _NPV_HORIZON_YEARS,
                                    discount_rate))
    grid_levelized = abs(grid_npv) / discounted_mwh

    # ── Gas BTM (CCGT) ───────────────────────────────────────────
    btm_ttp = 14  # typical CCGT build + tap, faster than ISO queue
    btm_capex = kw * _CAPEX_GAS_CCGT_USD_PER_KW + _CAPEX_GAS_PIPELINE_TAP_USD
    # r-om — fuel + variable O&M (per generated MWh) + fixed O&M (per nameplate kW).
    btm_opex_yr = (annual_mwh * (ccgt_dollars_per_mwh + ccgt_var_om)
                   + kw * ccgt_fixed_om)
    btm_npv = -(btm_capex + _npv([btm_opex_yr] * _NPV_HORIZON_YEARS,
                                  discount_rate))
    btm_levelized = abs(btm_npv) / discounted_mwh

    # ── Gas-to-Grid Hybrid ───────────────────────────────────────
    hybrid_ttp = 24  # gas-first + grid follow-on
    hybrid_capex = (kw * _CAPEX_GAS_CCGT_USD_PER_KW * 0.7
                    + kw * _interconnect_per_kw * 0.5
                    + _CAPEX_GAS_PIPELINE_TAP_USD
                    + sub_capex * 0.5)
    # 70% gas-fueled, 30% sold-to-grid as ancillary
    # r-hybrid (2026-06-30): a 70/30 gas+grid blend pays for BOTH fuels — the
    # old formula fueled only 70% and subtracted a phantom 10%×LMP "sell credit"
    # with no offtake, artificially suppressing opex. Blend both costs.
    # r-om — energy blended 70/30; variable O&M only on the gas-generated share;
    # fixed O&M split across the gas (70%) and grid (30%) nameplate fractions.
    hybrid_opex_yr = (annual_mwh * (0.7 * (ccgt_dollars_per_mwh + ccgt_var_om)
                                    + 0.3 * grid_lmp)
                      + kw * (0.7 * ccgt_fixed_om + 0.3 * grid_fixed_om))
    hybrid_npv = -(hybrid_capex + _npv([hybrid_opex_yr] * _NPV_HORIZON_YEARS,
                                       discount_rate))
    hybrid_levelized = abs(hybrid_npv) / discounted_mwh

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
        # v2.1b — echo the active assumptions back so the UI can show
        # "default" vs "user-edited" badges in the scenario cards.
        "_assumptions": {
            "grid_lmp_usd_per_mwh":  round(grid_lmp, 2),
            "grid_lmp_default":      _GRID_AVG_LMP_USD_PER_MWH,
            "ccgt_heat_rate":        round(heat_rate_ccgt, 0),
            "ccgt_heat_rate_default":_CCGT_HEAT_RATE_BTU_PER_KWH,
            "ccgt_gas_usd_per_mwh":  round(ccgt_dollars_per_mwh, 2),
            "ccgt_gas_default":      gas.get("$/MWh_ccgt_avg", 25),
            "discount_rate":         round(discount_rate, 4),
            "discount_rate_default": _DISCOUNT_RATE,
            # r-cf / r-om — all-in LCOE tunables (capacity factor + O&M)
            "capacity_factor":              round(capacity_factor, 3),
            "capacity_factor_default":      _CAPACITY_FACTOR,
            "ccgt_fixed_om_usd_per_kw_yr":  round(ccgt_fixed_om, 2),
            "ccgt_fixed_om_default":        _CCGT_FIXED_OM_USD_PER_KW_YR,
            "ccgt_variable_om_usd_per_mwh": round(ccgt_var_om, 2),
            "ccgt_variable_om_default":     _CCGT_VARIABLE_OM_USD_PER_MWH,
            "grid_fixed_om_usd_per_kw_yr":  round(grid_fixed_om, 2),
            "grid_fixed_om_default":        _GRID_FIXED_OM_USD_PER_KW_YR,
            "edited": any([
                bool(overrides.get("grid_lmp_usd_per_mwh")),
                bool(overrides.get("heat_rate_ccgt")),
                bool(overrides.get("gas_usd_mmbtu_override")),
                bool(overrides.get("discount_rate")),
                bool(overrides.get("capacity_factor")),
                bool(overrides.get("ccgt_fixed_om_usd_per_kw_yr")),
                bool(overrides.get("ccgt_variable_om_usd_per_mwh")),
                bool(overrides.get("grid_fixed_om_usd_per_kw_yr")),
            ]),
        },
    }


def _pick_best_fit(scenarios: dict, dcpi: dict, deadline_months: int,
                    power_source: str = "auto", firm_grid: bool = False) -> dict:
    """Choose the best scenario based on:
      1. Lowest levelized $/MWh that meets deadline_months
      2. Tiebreak: NPV (less negative wins)
      3. If grid TTP > deadline → exclude grid_only
      4. If DCPI verdict is AVOID → prefer BTM (don't trust grid),
         UNLESS the grid is FIRM (interconnect ready + near-term TTP) or the
         caller explicitly asked to value on grid.

    power_source: "auto" (default), "grid" (value on the actual grid, no on-site
    gas assumed), or "gas" (force gas-BTM). A firm-grid site should not be
    auto-wrapped in gas just because the market verdict is AVOID.
    """
    # v2.1b shipped a `_assumptions` metadata key inside scenarios dict.
    # Filter out anything starting with `_` so we only iterate real
    # scenarios — otherwise we KeyError on s["time_to_power_months"]
    # when the iterator hits the metadata entry.
    real_scenarios = {k: v for k, v in scenarios.items() if not k.startswith("_")}

    # Explicit caller override — value on the requested power path.
    if power_source == "grid" and "grid_only" in real_scenarios:
        g = real_scenarios["grid_only"]
        return {
            "scenario":   "grid_only",
            "rationale":  (f"Grid-based valuation (by request) — firm interconnect, "
                            f"{g['time_to_power_months']:.0f}-mo to full power at "
                            f"${g['levelized_usd_per_mwh']:.2f}/MWh; on-site gas not assumed."),
        }
    if power_source == "gas" and "gas_btm" in real_scenarios:
        s = real_scenarios["gas_btm"]
        return {
            "scenario":   "gas_btm",
            "rationale":  (f"Gas-BTM (by request) — ${s['levelized_usd_per_mwh']:.2f}/MWh, "
                            f"{s['time_to_power_months']:.0f}-mo to power."),
        }

    candidates = []
    for name, s in real_scenarios.items():
        if s["time_to_power_months"] > deadline_months:
            continue
        candidates.append((name, s))
    if not candidates:
        # No scenario meets deadline → pick fastest
        candidates = [(name, s) for name, s in real_scenarios.items()]
        candidates.sort(key=lambda x: x[1]["time_to_power_months"])
        best_name, best_s = candidates[0]
        return {
            "scenario":   best_name,
            "rationale":  (f"No scenario meets {deadline_months}-month deadline; "
                            f"selected fastest ({best_s['time_to_power_months']:.0f} months). "
                            f"Consider extending deadline."),
        }
    # A firm grid (interconnect ready + meets deadline) is trustworthy — don't
    # force gas on it even in an AVOID market.
    _g = dict(candidates).get("grid_only")
    _grid_is_firm = bool(firm_grid and _g and _g["time_to_power_months"] <= deadline_months)
    if (dcpi.get("verdict") == "AVOID" and "gas_btm" in dict(candidates)
            and not _grid_is_firm):
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


def _site_sufficiency(target_mw: int, acres: float, stories: int = 1) -> dict:
    """v2.2 — Acres-per-MW sufficiency check + surplus calculation.
    Surfaces 'is this parcel right-sized for the build?' as a separate
    signal from the headline $/MW valuation.

    r-density (2026-06-30): a MULTI-STORY campus fits more MW on the same land,
    so the effective land per MW scales with `stories`. A 2-3 story AI hall
    (~6 MW/acre) should NOT read 'undersized' at its raw single-story ac/MW.
    Category is judged on the EFFECTIVE (story-adjusted) ratio; the raw ac/MW
    is still reported."""
    if target_mw <= 0:
        return {"category": "invalid", "acres_per_mw": None}
    stories = max(1, int(stories or 1))
    raw_ratio = acres / target_mw
    eff_ratio = (acres * stories) / target_mw          # story-adjusted
    _sn = f" (×{stories} stories → {eff_ratio:.2f} effective ac/MW)" if stories > 1 else ""
    if eff_ratio < _ACRES_PER_MW_UNDERSIZED:
        category = "undersized"
        note = (f"Only {raw_ratio:.2f} ac/MW{_sn} — below the 1.0 ac/MW "
                  f"minimum needed to fit the campus + transformer + "
                  f"buffer. Buyer may not be able to build full {target_mw} MW.")
    elif eff_ratio < _ACRES_PER_MW_TYPICAL_MIN:
        category = "tight"
        note = (f"{raw_ratio:.2f} ac/MW{_sn} — workable, tight footprint; "
                  f"little room for solar/expansion.")
    elif eff_ratio <= _ACRES_PER_MW_TYPICAL_MAX:
        category = "typical"
        note = (f"{raw_ratio:.2f} ac/MW{_sn} — typical hyperscale campus "
                  f"sizing with normal support footprint.")
    elif eff_ratio <= _SURPLUS_THRESHOLD_ACRES_PER_MW:
        category = "comfortable"
        note = (f"{raw_ratio:.2f} ac/MW{_sn} — comfortable; room for "
                  f"solar, substation, and modest expansion.")
    else:
        category = "surplus"
        note = (f"{raw_ratio:.2f} ac/MW{_sn} — surplus land. Acres above "
                  f"{_SURPLUS_THRESHOLD_ACRES_PER_MW:.1f} ac/MW have "
                  f"separate residual industrial-land value.")
    # multi-story needs less land, so the surplus threshold per MW divides by stories
    surplus_acres = max(0.0, acres - target_mw * _SURPLUS_THRESHOLD_ACRES_PER_MW / stories)
    return {
        "acres":                 acres,
        "stories":               stories,
        "target_mw":             target_mw,
        "acres_per_mw":          round(raw_ratio, 2),
        "effective_acres_per_mw": round(eff_ratio, 2),
        "typical_band":          [_ACRES_PER_MW_TYPICAL_MIN,
                                     _ACRES_PER_MW_TYPICAL_MAX],
        "category":              category,
        "note":                  note,
        "surplus_acres":         round(surplus_acres, 1),
        "residual_land_value":   round(surplus_acres * _RESIDUAL_LAND_USD_PER_ACRE, 0),
    }


def _compute_valuation(target_mw: int, acres: float, dcpi: dict,
                        best_fit: dict, scenarios: dict,
                        readiness: dict = None,
                        market_baseline: dict = None,
                        stories: int = 1,
                        abatement: dict = None) -> dict:
    """Compute $-range valuation. v2.0 (2026-06-04) recalibrated to the
    user-supplied $150K-$800K/MW industry range. Uses:
      - $475K/MW baseline (midpoint of industry range)
      - Verdict multipliers spanning 0.40 (AVOID) - 1.65 (BUILD)
      - Best-fit scenario multiplier (gas_btm premium for fast TTP)
      - Site-readiness premium stack (6 boolean flags, multiplicative)
      - ±50% envelope (was ±30% — too tight for illiquid parcels)
    """
    # Per-MW value adjusted by verdict (v2.0: wider spread)
    verdict_raw = dcpi.get("verdict")
    verdict_mult_base = {
        "BUILD":   1.65,   # premium markets (was 1.20)
        "CAUTION": 1.00,   # baseline (was 0.95)
        "AVOID":   0.40,   # deep discount, slow TTP (was 0.75)
        None:      0.85,
    }.get(verdict_raw, 0.85)

    # v2.1c — Constraint-moat attenuation. When a market is
    # AVOID-by-constraint (Ashburn, Phoenix, Santa Clara) AND the
    # parcel has "moat" flags (interconnect / substation / permits)
    # already in hand, the AVOID penalty should LIFT — the same
    # scarcity that produced the AVOID verdict is precisely what makes
    # a shovel-ready parcel valuable. (Greenfield in Ashburn = AVOID,
    # but a parcel with grid interconnect already signed sells at $1M+
    # /MW because the queue is closed to everyone else.)
    #
    # Attenuation only fires when the verdict is AVOID, the subtype
    # is constrained (NOT weak_demand), and at least one moat flag is
    # set. Each flag adds 0.22 to the verdict mult, capped at 1.05.
    # Weak-demand AVOID gets no attenuation — there's no moat in a
    # market nobody wants to build in.
    readiness = readiness or {}
    subtype = (dcpi.get("verdict_subtype") or "n/a")
    moat_flags_active = sum(1 for f in
        ("grid_interconnect_ready", "substation_on_site", "permits_in_hand")
        if readiness.get(f))
    moat_attenuation_applied = False
    # r-moat-developing (2026-08-12): attenuation fired ONLY for `constrained`,
    # but the classifier documents `developing` as the class "whose verdict mainly
    # tracks readiness" (see _compute_verdict_subtype). So the one subtype defined
    # by readiness was the one that could not be moved by readiness — a developing
    # AVOID with a substation and a live interconnect took the same full 0.40×
    # slam as bare dirt. It lifts at a lower rate than `constrained`, and to a
    # lower cap: there is no demand-side scarcity moat here, so readiness carries
    # the site toward — never past — the engine's own no-verdict default (0.85).
    # `weak_demand` stays excluded: no moat in a market nobody wants to build in.
    _lift = _MOAT_LIFT_PER_FLAG.get(subtype)
    if verdict_raw == "AVOID" and _lift and moat_flags_active >= 1:
        verdict_mult = min(_MOAT_LIFT_CAP.get(subtype, 1.05),
                           verdict_mult_base + (moat_flags_active * _lift))
        moat_attenuation_applied = True
    else:
        verdict_mult = verdict_mult_base

    # Best-fit scenario adjustment (BTM premium if grid is constrained)
    # Tightened from v1.0 (gas_btm 1.10 → 1.05) since the verdict mult
    # already captures most of the "is grid usable?" signal.
    bestfit_mult = {
        "grid_only":          1.00,
        "gas_btm":            1.05,  # small premium for fast time-to-power
        "gas_to_grid_hybrid": 1.10,  # revenue optionality is worth more than pure BTM
    }.get(best_fit["scenario"], 1.00)

    # Site-readiness premium stack — new in v2.0.
    # readiness dict has 6 boolean flags; each TRUE flag multiplies the
    # baseline by its premium. Raw land (all FALSE) → 1.0x; fully
    # shovel-ready (all TRUE) → 3.35x. This is the input set v1.0 was
    # missing — user noted "Phoenix-100MW with grid/water/fiber/zoning
    # complete should be worth ~$60M" which only resolves when readiness
    # is in the model.
    # (`readiness` already defaulted above for the moat check)
    readiness_mult = 1.0
    readiness_applied = {}
    for flag, premium in _READINESS_PREMIUMS.items():
        if readiness.get(flag):
            readiness_mult *= premium
            readiness_applied[flag] = premium
    # r-decorr (2026-06-30): grid_interconnect_ready (+30%) and substation_on_site
    # (+25%) both proxy "grid access" — stacking them multiplicatively (1.30×1.25
    # = 1.625×) double-counts. When BOTH are set, correct their combined premium
    # down to a single grid-access factor (~+45%).
    if (readiness.get("grid_interconnect_ready")
            and readiness.get("substation_on_site")):
        _combined_raw = (_READINESS_PREMIUMS["grid_interconnect_ready"]
                         * _READINESS_PREMIUMS["substation_on_site"])
        readiness_mult *= (1.45 / _combined_raw)   # ×0.892
    # r-diminish: 4+ stacked premiums are correlated (a shovel-ready site tends to
    # have several at once), so multiplicative stacking overstates. Dampen the
    # premium ABOVE 1.0, scaling with how many are stacked. TUNABLE.
    _n = len(readiness_applied)
    if readiness_mult > 1.0 and _n >= 3:
        readiness_mult = 1.0 + (readiness_mult - 1.0) * (0.95 ** (_n - 2))
    readiness_mult = round(readiness_mult, 3)

    # v2.1 item 2 — regression-fit per-MW baseline if we have ≥8 sized
    # comps in this market. Falls back to the $475K global baseline.
    market_baseline = market_baseline or {}
    if market_baseline.get("available"):
        base_per_mw = float(market_baseline["median_per_mw"])
        baseline_source = (f"market_regression (n={market_baseline['n_comps']} "
                              f"comps, table={market_baseline.get('table')})")
    else:
        base_per_mw = _VALUE_PER_MW_USD_BASE
        baseline_source = "global_baseline_v2.0"

    per_mw_uncapped = base_per_mw * verdict_mult * bestfit_mult * readiness_mult
    per_acre_uncapped = _VALUE_PER_ACRE_USD_BASE * verdict_mult * readiness_mult

    # v2.1b clamp: keep per-MW inside the published industry band. r-entitled
    # (2026-06-30): a FULLY entitled site (zoned AND permitted) lifts the
    # ceiling — the de-risking (12-24 mo + entitlement cost) is real value that
    # would otherwise vanish under the $800K raw-land cap.
    _entitled = bool(readiness.get("zoning_approved") and readiness.get("permits_in_hand"))
    # r-powered: firm near-term power (grid time-to-power <= 18 mo — delivered/
    # near-firm MW, not a multi-year queue) on an entitled site lifts the ceiling
    # again (powered land is the scarcest asset).
    _grid_ttp = (scenarios.get("grid_only") or {}).get("time_to_power_months") or 99
    _firm_powered = bool(_entitled and _grid_ttp <= 18)
    if _firm_powered:
        per_mw_ceiling = _VALUE_PER_MW_CEIL_POWERED_USD
    elif _entitled:
        per_mw_ceiling = _VALUE_PER_MW_CEIL_ENTITLED_USD
    else:
        per_mw_ceiling = _VALUE_PER_MW_CEIL_USD
    per_mw_mid = max(_VALUE_PER_MW_FLOOR_USD,
                     min(per_mw_ceiling, per_mw_uncapped))
    if per_mw_mid >= per_mw_ceiling - 1:
        band_status = "ceiling_saturated"
    elif per_mw_mid <= _VALUE_PER_MW_FLOOR_USD + 1:
        band_status = "floor_saturated"
    else:
        band_status = "in_band"
    # Entitlement premium = dollars the ceiling-lift unlocked for a zoned +
    # permitted site (value above what the raw-land $800K cap would allow).
    _raw_cap_price = min(_VALUE_PER_MW_CEIL_USD, per_mw_uncapped)
    _entitlement_lift_per_mw = round(max(0.0, per_mw_mid - _raw_cap_price), 0)
    # Per-acre figure kept informational (shown in response) but NOT
    # summed into site_value_mid — see v2.2 rationale above.
    scale_factor = per_mw_mid / per_mw_uncapped if per_mw_uncapped > 0 else 1.0
    per_acre_mid = per_acre_uncapped * scale_factor

    # v2.2 — Data-center sites trade by the MW. Land cost is implicit in
    # every $/MW comp. Headline valuation = $/MW × MW (no acres summed in).
    # Surplus land (acres beyond ~3× target_mw) is added as a small
    # residual via the site_sufficiency block — those acres ARE separately
    # marketable as industrial land.
    suff = _site_sufficiency(int(target_mw), float(acres), stories=stories)
    surplus_residual = float(suff.get("residual_land_value") or 0)
    # r-abate (2026-06-30): a property-tax abatement de-risks a decade+ of opex →
    # a modest, transferable premium ON TOP of the $/MW × MW (post-clamp, so it's
    # not eaten by the ceiling), surfaced as its own line.
    _abate = abatement or {}
    _abate_factor = float(_abate.get("factor") or 1.0)
    _mw_value = per_mw_mid * target_mw
    _abate_premium = round(_mw_value * (_abate_factor - 1.0), 0)
    site_value_mid = _mw_value * _abate_factor + surplus_residual

    entitlement = {
        "entitled":       _entitled,
        "firm_powered":   _firm_powered,
        "grid_ttp_months": _grid_ttp,
        "ceiling_used":   per_mw_ceiling,
        "raw_ceiling":    _VALUE_PER_MW_CEIL_USD,
        "lift_per_mw":    _entitlement_lift_per_mw,
        "lift_usd":       round(_entitlement_lift_per_mw * target_mw, 0),
        "note": ((f"Firm near-term power ({int(_grid_ttp)}-mo to target MW) + "
                  "zoned & permitted — powered land is the scarcest asset; prices "
                  f"to the ${_VALUE_PER_MW_CEIL_POWERED_USD:,.0f}/MW powered ceiling."
                  if _firm_powered else
                  "Zoned + permitted — entitlement removes ~12-24 mo and the "
                  "entitlement cost/risk, so the site prices above the "
                  f"${_VALUE_PER_MW_CEIL_USD:,.0f}/MW raw-land ceiling.")
                 if _entitled else
                 "Needs BOTH zoning approved AND permits in hand to price above "
                 f"the ${_VALUE_PER_MW_CEIL_USD:,.0f}/MW raw-land ceiling."),
    }

    spread = _VALUE_RANGE_SPREAD
    return {
        "$/mw_low":            round(per_mw_mid * (1 - spread), 0),
        "$/mw_mid":            round(per_mw_mid, 0),
        "$/mw_high":           round(per_mw_mid * (1 + spread), 0),
        "$/mw_uncapped":       round(per_mw_uncapped, 0),
        "$/mw_band_floor":     _VALUE_PER_MW_FLOOR_USD,
        "$/mw_band_ceiling":   per_mw_ceiling,
        "$/mw_band_status":    band_status,
        "entitlement":         entitlement,
        "tax_abatement":       {
            "applied":     bool(_abate.get("abatement")),
            "factor":      _abate_factor,
            "premium_usd": _abate_premium,
            "note":        _abate.get("note", ""),
        },
        # v2.2 — $/acre is INFORMATIONAL only (not summed into site_value_mid).
        # Data-center comps trade by the MW; land is implicit in $/MW.
        # Kept for context — these are the residual land values per acre,
        # what surplus acres above ~3× target_mw would fetch separately.
        "$/acre_residual_mid": _RESIDUAL_LAND_USD_PER_ACRE,
        "$/acre_legacy_mid":   round(per_acre_mid, 0),  # pre-v2.2 figure
        "site_value_usd_low":  round(site_value_mid * (1 - spread), 0),
        "site_value_usd_mid":  round(site_value_mid, 0),
        "site_value_usd_high": round(site_value_mid * (1 + spread), 0),
        "site_value_breakdown": {
            "mw_contribution_usd":      round(per_mw_mid * target_mw, 0),
            "surplus_land_residual_usd": round(surplus_residual, 0),
            "depiction":                ("Site trades by MW. Land cost is "
                                            "implicit in the $/MW comp. Surplus "
                                            "acres above 3× MW have separate "
                                            "industrial-land residual."),
        },
        "site_sufficiency": suff,
        "multipliers": {
            "verdict_mult":    verdict_mult,
            "verdict_mult_base": verdict_mult_base,
            "bestfit_mult":    bestfit_mult,
            "readiness_mult":  readiness_mult,
            "readiness_applied": readiness_applied,
            "baseline_per_mw_usd": round(base_per_mw, 0),
            "baseline_source":  baseline_source,
            "band_clamp":      band_status,
            # v2.1c — constraint-moat attenuation provenance
            "verdict_subtype": subtype,
            "moat_attenuation_applied": moat_attenuation_applied,
            "moat_flags_active": moat_flags_active,
            "moat_explainer": (
                f"AVOID-by-{subtype}: shovel-ready parcels in saturated "
                f"clusters carry a moat premium ({moat_flags_active} of "
                f"3 moat flags set). Verdict mult lifted "
                f"from {verdict_mult_base:.2f}× to {verdict_mult:.2f}×."
                if moat_attenuation_applied else
                (f"AVOID-by-{subtype}: no moat attenuation. Constraint-"
                 f"AVOID parcels only carry a moat premium when at least "
                 f"one of grid_interconnect_ready / substation_on_site / "
                 f"permits_in_hand is true."
                 if verdict_raw == "AVOID" and subtype == "constrained"
                 else None)),
        },
        "_methodology":        ("v2.2: $/MW × MW only (sites trade by the MW; "
                                  "land cost is implicit in every $/MW comp). "
                                  "Surplus acres above 3× target_mw add small "
                                  "residual land value at $8K/acre. Verdict "
                                  "spans AVOID(0.40)-BUILD(1.65); 6 readiness "
                                  "premiums (grid/sub/water/fiber/zoning/permits) "
                                  "stack multiplicatively to ~3.35× for "
                                  "shovel-ready. Per-MW clamped to industry "
                                  "$150K-$800K band. ±50% envelope."),
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
        # r-density (2026-06-30): build stories (1 = single-story). A multi-story
        # AI campus fits more MW per acre, so this drives the sufficiency check.
        stories = max(1, min(6, int(payload.get("stories") or 1)))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "invalid_payload",
            "hint":  "Required: lat, lon, acres, target_mw. Optional: deadline_months, readiness flags.",
        }), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and acres > 0 and target_mw > 0):
        return jsonify({
            "ok": False,
            "error": "invalid_payload",
            "hint":  "lat ∈ [-90,90], lon ∈ [-180,180], acres > 0, target_mw > 0.",
        }), 400

    # v2.0 (2026-06-04): site-readiness flags. Each TRUE flag adds its
    # premium to the valuation. Accepts a nested "readiness" object OR
    # flat top-level booleans (e.g. {"grid_interconnect_ready": true}).
    readiness_raw = payload.get("readiness") or {}
    readiness = {}
    for flag in _READINESS_PREMIUMS.keys():
        val = readiness_raw.get(flag, payload.get(flag, False))
        readiness[flag] = bool(val)

    # v2.1 — optional user overrides (graceful fallback to defaults).
    user_heat_rate_ccgt = payload.get("heat_rate_ccgt")    # Btu/kWh
    user_gas_mmbtu      = payload.get("utility_gas_usd_mmbtu")  # tariff override
    # v2.1b — new tunables surfaced in the "Adjust assumptions" panel
    user_grid_lmp       = payload.get("grid_lmp_usd_per_mwh")   # $/MWh
    user_discount_rate  = payload.get("discount_rate")          # 0.0-1.0
    # r-powered: months until the site's target MW is firm/delivered. Overrides
    # the DCPI/queue grid time-to-power — a POWERED site (firm near-term MW) is
    # NOT a multi-year-queue site, and short TTP unlocks the powered-land ceiling.
    user_grid_ttp = _coerce_ttp_months(payload.get("grid_ttp_months"))

    # power_source: how to value the site's power path. "auto" (default) lets the
    # engine pick lowest-cost/feasible; "grid" values on the actual firm grid
    # (no on-site gas assumed); "gas" forces gas-BTM. A firm-grid site in an AVOID
    # market should NOT be auto-wrapped in gas — that contradicts having the grid.
    power_source = (payload.get("power_source") or "auto")
    power_source = str(power_source).strip().lower()
    if power_source not in ("auto", "grid", "gas"):
        power_source = "auto"

    # Gather data
    slug, state, dist = _nearest_market(lat, lon)
    # r-sitestate: the nearest MARKET centroid can be 100+ mi off (Millville→NoVA),
    # so resolve the site's ACTUAL state for the STATE-keyed factors below.
    site_state, site_state_src = _site_state(lat, lon, state)
    dcpi = _fetch_dcpi(slug)
    gas = _fetch_gas_economics(slug, site_state, lat)
    # r-power / r-abate (2026-06-30): real per-state operating economics.
    power = _fetch_power_cost_usd_mwh(site_state)
    abatement = _fetch_tax_abatement(site_state)

    # v2.1 Phase 3 overrides — each falls back to v2.0 behavior silently
    sub_proximity = _fetch_nearest_substation(lat, lon)       # item 4
    market_baseline = _fetch_market_comp_baseline(slug, state) # item 2
    live_queue = _fetch_live_queue_ttp(dcpi.get("iso", ""), slug)  # item 3

    overrides = {
        "substation_capex_usd":  sub_proximity.get("capex_usd"),
        # r-ttp0: `or` treats a user-stated 0 months as absent and falls through
        # to the market queue. Test for None explicitly — a stated 0 must win.
        "live_queue_ttp_months": (user_grid_ttp
                                    if user_grid_ttp is not None
                                    else (live_queue.get("months_to_power")
                                          if live_queue.get("available") else None)),
        # r-readiness-capex: the readiness flags describe what is PHYSICALLY
        # already built. They priced the site but never reached the cost model.
        "readiness":             readiness,
        "heat_rate_ccgt":        user_heat_rate_ccgt,  # item 5
        "gas_usd_mmbtu_override": user_gas_mmbtu,      # item 1
        # v2.1b — the two new user-tunable assumptions from the UI panel.
        # grid power cost defaults to the REAL per-state industrial rate (was a
        # flat $45); the user panel still overrides it.
        "grid_lmp_usd_per_mwh":  user_grid_lmp or power.get("usd_mwh"),
        "discount_rate":         user_discount_rate,
    }

    scenarios = _compute_scenarios(target_mw, dcpi, gas, overrides=overrides)
    # firm grid = interconnect ready AND a user-stated near-term time-to-power.
    # r-ttp0: `and user_grid_ttp` was False for a stated 0 — the most firmly
    # powered site possible scored as not-firm.
    _firm_grid = bool(readiness.get("grid_interconnect_ready")
                      and user_grid_ttp is not None)
    best_fit = _pick_best_fit(scenarios, dcpi, deadline_months,
                              power_source=power_source, firm_grid=_firm_grid)
    valuation = _compute_valuation(target_mw, acres, dcpi, best_fit,
                                     scenarios, readiness=readiness,
                                     market_baseline=market_baseline,
                                     stories=stories, abatement=abatement)
    comps = _fetch_comparable_sales(slug, state)

    base = {
        "ok":            True,
        "engine_version": "v2.2",
        "as_of":         _dt.datetime.utcnow().isoformat() + "Z",
        "input":         {"lat": lat, "lon": lon, "acres": acres,
                          "target_mw": target_mw,
                          "deadline_months": deadline_months,
                          "readiness": readiness,
                          "heat_rate_ccgt": user_heat_rate_ccgt,
                          "utility_gas_usd_mmbtu": user_gas_mmbtu},
        "phase_3_inputs": {
            "substation_proximity": sub_proximity,
            "market_baseline":      market_baseline,
            "live_queue_ttp":       live_queue,
        },
        "market_context": {
            "nearest_market_slug":  slug,
            "nearest_market_state": state,
            "miles_from_centroid":  round(dist, 1),
            "site_state":           site_state,
            "site_state_source":    site_state_src,
            "power_cost_usd_mwh":   power.get("usd_mwh"),
            "power_cost_source":    f"{power.get('sector')} · {power.get('source')}",
        },
        "dcpi_context":   {
            "verdict":              dcpi.get("verdict"),
            # v2.1c — subtype distinguishes why a market got AVOID.
            # constrained (Ashburn/Phoenix/Santa Clara) → the constraint
            # is the moat; shovel-ready parcels there are premium.
            # weak_demand (rural rust belt) → no demand, floor value.
            # developing → moderate, mainly tracks readiness.
            # n/a → BUILD or CAUTION.
            "verdict_subtype":      dcpi.get("verdict_subtype"),
            "verdict_explainer":    {
                "constrained":  ("High demand + slow queue + low excess capacity. "
                                  "AVOID for greenfield, but shovel-ready parcels "
                                  "carry a moat premium because the constraint "
                                  "blocks competitors."),
                "weak_demand":  ("Open queue + minimal demand growth. AVOID even "
                                  "for shovel-ready — no buyers."),
                "developing":   ("Moderate growth, queue accessible. Verdict mainly "
                                  "tracks parcel readiness."),
                "n/a":          ("BUILD or CAUTION verdict — subtype not "
                                  "applicable."),
                "unknown":      "DCPI data unavailable for this market.",
            }.get(dcpi.get("verdict_subtype") or "n/a"),
            "composite_score":      dcpi.get("composite_score"),
            "excess_power_score":   dcpi.get("excess_power_score"),
            "constraint_score":     dcpi.get("constraint_score"),
            "time_to_power_months": dcpi.get("time_to_power_months"),
            "iso":                  dcpi.get("iso"),
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
            "site_value_usd_mid":     valuation["site_value_usd_mid"],
            "$/mw_mid":               valuation["$/mw_mid"],
            # v2.2 — the "site trades by MW" depiction is a methodology
            # fact, not a paywalled insight. Surface it on the teaser so
            # even free visitors see the breakdown + sufficiency.
            "site_value_breakdown":   valuation.get("site_value_breakdown"),
            "site_sufficiency":       valuation.get("site_sufficiency"),
            # v2.1b — band-clamp metadata is methodology, not gated data.
            # Surfacing on teaser lets free visitors + tests see the
            # clamp behavior (in_band / ceiling_saturated / floor_saturated)
            # without unlocking the full valuation envelope.
            "$/mw_uncapped":          valuation.get("$/mw_uncapped"),
            "$/mw_band_floor":        valuation.get("$/mw_band_floor"),
            "$/mw_band_ceiling":      valuation.get("$/mw_band_ceiling"),
            "$/mw_band_status":       valuation.get("$/mw_band_status"),
        },
        "scenarios_teaser":  {
            "grid_only":          {"time_to_power_months": scenarios["grid_only"]["time_to_power_months"]},
            "gas_btm":            {"time_to_power_months": scenarios["gas_btm"]["time_to_power_months"]},
            "gas_to_grid_hybrid": {"time_to_power_months": scenarios["gas_to_grid_hybrid"]["time_to_power_months"]},
            # v2.1b — assumption overrides are user inputs + defaults,
            # not derived $/MWh. Safe to expose pre-paywall so users can
            # confirm their LMP / discount / heat-rate overrides took
            # effect before they upgrade. Full opex math still gated.
            "_assumptions":       scenarios.get("_assumptions"),
        },
        "upgrade_hint": {
            "human_message":  ("Full 3-scenario NPV + CapEx/OpEx breakdown, "
                                "$-range valuation envelope, gas hub pricing, "
                                "and comparable-sale lookups are a PRO feature."),
            "tier_required":  "pro",
            "signup_url":     "https://dchub.cloud/pricing",
            "stripe_url":     "https://buy.stripe.com/00w28o7BqaXLeP31QIaZi04",
            "what_you_unlock": [
                "Full $-range valuation envelope (low/mid/high per MW + per acre, ±50%)",
                "6 site-readiness premiums (grid/sub/water/fiber/zoning/permits)",
                "Grid / Gas-BTM / Gas-to-Grid 10-yr Total-Cost-NPV comparison",
                "CapEx + OpEx breakdown per scenario",
                "Levelized cost $/MWh per scenario",
                "Best-fit scenario rationale",
                "Comparable transactions from 4,000+ tracked M&A deals pipeline",
                "Live gas hub pricing (Henry Hub + regional basis)",
                "Multiplier breakdown (verdict × best-fit × readiness stack)",
            ],
        },
    })
    return jsonify(base), 200


# ── Methodology endpoint (public) ─────────────────────────────────

@site_valuation_engine_bp.route("/api/v1/site/value/methodology", methods=["GET"])
def site_value_methodology():
    return jsonify({
        "ok":           True,
        "version":      "v2.2 (2026-06-04)",
        "summary":      ("Three-scenario NPV comparison for a (lat, lon, "
                          "acres, target_mw, readiness flags + optional "
                          "heat-rate + utility-gas-tariff) tuple. v2.1 adds "
                          "5 Phase-3 live-data overrides with graceful "
                          "fallback to v2.0 constants when upstream is missing."),
        "v2_2_changelog": [
            "MW-only depiction — site_value_usd_mid = $/MW × MW. The "
              "$/acre line was being SUMMED on top, double-counting land "
              "that's already implicit in every $/MW comp. Now informational only.",
            "Site sufficiency block — acres/MW ratio categorized as "
              "undersized/tight/typical/comfortable/surplus with industry "
              "thresholds (typical band 1.5-2.5 ac/MW for hyperscale).",
            "Surplus residual — acres above 3× target_mw add $8K/acre as "
              "separately-marketable industrial land. Conservative pending "
              "per-market zoned-industrial comps.",
        ],
        "v2_1_changelog": [
            "Phase 3 #1 — Optional utility_gas_usd_mmbtu input replaces state-avg "
              "gas pricing; flows through CCGT $/MWh via heat rate.",
            "Phase 3 #2 — Market-specific per-MW baseline from regression on "
              "comparable_sales (deals table) when >=8 sized comps exist; "
              "falls back to global $475K/MW baseline otherwise.",
            "Phase 3 #3 — Live ISO queue depth → time-to-power override "
              "(queue_mw / annual_velocity_mw × 12) replaces DCPI's slower "
              "time_to_power_months when /interconnection-queue/snapshot has data.",
            "Phase 3 #4 — Substation capex from HIFLD haversine proximity "
              "(<5mi=$1M, 5-25mi=$4M, >25mi=$8M) replaces fixed $8M assumption.",
            "Phase 3 #5 — Optional heat_rate_ccgt input replaces fixed 6800 "
              "Btu/kWh; flows through to BTM + Hybrid scenarios.",
        ],
        "v2_changelog": [
            "Recalibrated baseline from $2M/MW → $475K/MW (midpoint of $150K-$800K industry range)",
            "Widened verdict spread from 0.75-1.20 → 0.40-1.65 (full industry envelope)",
            "Added 6 site-readiness premiums: grid/substation/water/fiber/zoning/permits (multiplicative, up to 3.35x for shovel-ready)",
            "Widened envelope from ±30% → ±50% (real-world illiquidity spread)",
            "Per-acre baseline $75K → $15K (industrial-zoned land comp)",
        ],
        "substation_proximity_tiers": [
            {"max_miles": t[0] if t[0] != float("inf") else "inf",
             "capex_usd": t[1], "tier": t[2]}
            for t in _SUBSTATION_PROXIMITY_TIERS
        ],
        "regression_min_comps": _MIN_COMPS_FOR_REGRESSION,
        "constants":   {
            "capex_grid_interconnect_usd_per_kw":  _CAPEX_GRID_INTERCONNECT_USD_PER_KW,
            "capex_gas_ccgt_usd_per_kw":            _CAPEX_GAS_CCGT_USD_PER_KW,
            "capex_gas_pipeline_tap_usd":           _CAPEX_GAS_PIPELINE_TAP_USD,
            "ccgt_heat_rate_btu_per_kwh":           _CCGT_HEAT_RATE_BTU_PER_KWH,
            "peaker_heat_rate_btu_per_kwh":         _PEAKER_HEAT_RATE_BTU_PER_KWH,
            "grid_avg_lmp_usd_per_mwh":             _GRID_AVG_LMP_USD_PER_MWH,
            "ccgt_fixed_om_usd_per_kw_yr":          _CCGT_FIXED_OM_USD_PER_KW_YR,
            "ccgt_variable_om_usd_per_mwh":         _CCGT_VARIABLE_OM_USD_PER_MWH,
            "grid_fixed_om_usd_per_kw_yr":          _GRID_FIXED_OM_USD_PER_KW_YR,
            "capacity_factor":                      _CAPACITY_FACTOR,
            "npv_discount_rate":                    _DISCOUNT_RATE,
            "npv_horizon_years":                    _NPV_HORIZON_YEARS,
            "value_per_mw_usd_base":                _VALUE_PER_MW_USD_BASE,
            "value_per_acre_usd_base":              _VALUE_PER_ACRE_USD_BASE,
            "value_range_spread":                   _VALUE_RANGE_SPREAD,
        },
        "verdict_multipliers": {
            "BUILD":   1.65,
            "CAUTION": 1.00,
            "AVOID":   0.40,
            "null":    0.85,
        },
        "bestfit_multipliers": {
            "grid_only":          1.00,
            "gas_btm":            1.05,
            "gas_to_grid_hybrid": 1.10,
        },
        "site_readiness_premiums": _READINESS_PREMIUMS,
        "site_readiness_max_stack": round(
            1.0 * 1.30 * 1.25 * 1.10 * 1.20 * 1.30 * 1.20, 3
        ),
        "data_sources": {
            "dcpi_verdict":          "dcpi_scores table — refreshed daily",
            "gas_pricing":           "routes/powered_land_gas.py — EIA v2 API",
            "comparable_sales":      "deals table — 4,000+ M&A deals tracked",
            "market_centroids":      "hand-seeded top-30 markets (Phase 2: full markets table)",
        },
        "npv_definition": ("10-yr NPV column is a COST-BASIS figure: "
                           "capex + 10-year discounted opex. Negative because "
                           "it represents the total cost of power delivery, "
                           "not net cashflow. Compare scenarios by levelized "
                           "$/MWh + time-to-power, not by NPV magnitude alone."),
        "phase_4_roadmap": [
            "Revenue-side NPV (project earnings) so net-NPV is meaningful",
            "Per-utility tariff AUTO-lookup from utility-territory mapping (currently user-supplied)",
            "Regression with verdict + MW interaction terms (currently flat median)",
            "Direct HIFLD voltage-kV check (currently any sub counts)",
            "Live LMP per scenario (currently US-blended $45/MWh)",
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
.methcard { background: var(--panel); border: 1px solid var(--accent2); border-radius: 12px; padding: 20px 22px; margin: 24px 0; }
.methcard h3 { font-size: 17px; }
.methgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
@media (max-width: 768px) { .methgrid { grid-template-columns: 1fr; } }
.methh { font-size: 11px; font-weight: 700; color: var(--accent2); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px; }
.methul { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.55; }
.methul li { margin-bottom: 5px; }
.printbtn { background: var(--accent); color: #000; border: 0; border-radius: 6px; padding: 8px 14px; font-size: 12px; font-weight: 700; cursor: pointer; font-family: inherit; }
@media print {
  @page { margin: 14mm; }
  body { background: #fff !important; color: #111 !important; }
  header, .header, nav, .nav, form, #valForm, .presets, .preset, .upgrade, .no-print,
    #readiness-grid, .readiness-toggles, .adjust-panel { display: none !important; }
  .hero .prov, .hero .loc { color: #111 !important; }
  #results { display: block !important; }
  .card, .section, .scen-card, .methcard, table { break-inside: avoid; page-break-inside: avoid;
    background: #fff !important; color: #111 !important; border-color: #bbb !important; }
  .methcard { border: 1.5px solid #333 !important; }
  .num, .stat, h1, h3, h4, b { color: #111 !important; }
  a { color: #111 !important; text-decoration: none; }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="kicker">⌖  Site Valuation Engine  ·  <span style="background:#0EA5E9;color:#000;padding:2px 8px;border-radius:4px;font-weight:800">PRO+ PREMIUM</span></div>
  <h1>What is your site worth?</h1>
  <p class="tagline">3-scenario NPV: <b>Grid</b> vs <b>Gas BTM</b> vs <b>Gas-to-Grid Hybrid</b>. Built for sellers, landowners, and developers pricing power-ready parcels. Powered by DCPI verdicts across 234+ markets and live gas hub pricing.</p>

  <div id="error" class="hidden error" style="margin:0 0 16px;font-size:14px;font-weight:600"></div>

  <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin:0 0 16px;">
    <div style="font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;font-weight:700;margin-bottom:10px;">
      Quick demo locations &nbsp;·&nbsp; click to load
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button type="button" class="preset" data-lat="33.45"  data-lon="-112.07" data-acres="50"  data-mw="100" data-label="Phoenix, AZ">Phoenix, AZ</button>
      <button type="button" class="preset" data-lat="39.04"  data-lon="-77.48"  data-acres="80"  data-mw="200" data-label="Ashburn, VA">Ashburn, VA</button>
      <button type="button" class="preset" data-lat="32.78"  data-lon="-96.80"  data-acres="60"  data-mw="150" data-label="Dallas, TX">Dallas, TX</button>
      <button type="button" class="preset" data-lat="41.14"  data-lon="-104.82" data-acres="100" data-mw="150" data-label="Cheyenne, WY">Cheyenne, WY</button>
      <button type="button" class="preset" data-lat="45.84"  data-lon="-119.71" data-acres="80"  data-mw="100" data-label="Boardman, OR">Boardman, OR</button>
      <button type="button" class="preset" data-lat="40.08"  data-lon="-82.81"  data-acres="100" data-mw="200" data-label="New Albany, OH">New Albany, OH</button>
    </div>
    <style>.preset{background:var(--panel2);color:var(--accent2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:12px;font-family:inherit;cursor:pointer;font-weight:600}.preset:hover{border-color:var(--accent);color:#fff}</style>
  </div>

  <form id="valForm">
    <label>Site label &nbsp;<span style="color:var(--accent2);font-size:11px">optional · PDF title, e.g. "A1 Campus — Millville NJ"</span><br><input id="site_label" type="text" maxlength="80" placeholder="Powered-Land Site"></label>
    <label>Lat &nbsp;<span style="color:var(--accent2);font-size:11px">[-90 → 90]</span><br><input id="lat" type="number" step="any" min="-90" max="90" value="33.45" inputmode="decimal" required></label>
    <label>Lon &nbsp;<span style="color:var(--accent2);font-size:11px">[-180 → 180, W is negative]</span><br><input id="lon" type="number" step="any" min="-180" max="180" value="-112.07" inputmode="decimal" required></label>
    <label>Acres &nbsp;<span style="color:var(--accent2);font-size:11px">> 0</span><br><input id="acres" type="number" step="any" min="0.1" value="50" inputmode="decimal" required></label>
    <label>Target MW &nbsp;<span style="color:var(--accent2);font-size:11px">> 0</span><br><input id="target_mw" type="number" step="1" min="1" value="100" required></label>
    <label>Deadline (months)<br><input id="deadline_months" type="number" step="1" min="1" max="120" value="24"></label>
    <label>Building stories &nbsp;<span style="color:var(--accent2);font-size:11px">1 = single-story · 2-3 = AI hall</span><br><input id="stories" type="number" step="1" min="1" max="6" value="1"></label>
    <label>Firm power (months) &nbsp;<span style="color:var(--accent2);font-size:11px">months to firm/delivered MW · blank = market queue</span><br><input id="grid_ttp_months" type="number" step="1" min="0" max="120" placeholder="e.g. 12" inputmode="numeric"></label>
    <label>Power basis &nbsp;<span style="color:var(--accent2);font-size:11px">how to value the power path</span><br><select id="power_source"><option value="auto">Auto (lowest-cost)</option><option value="grid">Grid only (no gas)</option><option value="gas">Gas-BTM</option></select></label>
    <label>CCGT heat rate (Btu/kWh) &nbsp;<span style="color:var(--accent2);font-size:11px">optional · default 6800</span><br><input id="heat_rate_ccgt" type="number" step="50" min="5500" max="12000" placeholder="6800"></label>
    <label>Utility gas tariff ($/MMBtu) &nbsp;<span style="color:var(--accent2);font-size:11px">optional · default = state avg</span><br><input id="utility_gas_usd_mmbtu" type="number" step="0.05" min="0" max="40" placeholder="3.50"></label>
    <label>API Key (PRO unlock)<br><input id="api_key" type="password" placeholder="dchub_..." autocomplete="off"></label>
  </form>

  <!-- v2.0: Site-readiness toggles. Multiplicative premium stack. -->
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin:0 0 16px;">
    <div style="font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;font-weight:700;margin-bottom:6px;">
      Site readiness &nbsp;·&nbsp; toggle what's actually in place
    </div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:14px;">
      Each toggle is a premium. Raw land = 1.00×; fully shovel-ready ≈ 2.4× (grid +
      substation are de-correlated, and stacked premiums show diminishing returns).
    </div>
    <div id="readiness-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;">
      <label class="rcb"><input type="checkbox" id="r_grid"><span><b>Grid interconnect ready</b><br><span class="rcb-sub">ISA signed / queue cleared &nbsp;·&nbsp; +30%</span></span></label>
      <label class="rcb"><input type="checkbox" id="r_sub"><span><b>Substation on-site</b><br><span class="rcb-sub">existing sub within parcel &nbsp;·&nbsp; +25%</span></span></label>
      <label class="rcb"><input type="checkbox" id="r_water"><span><b>Water secured</b><br><span class="rcb-sub">WSA / permit in place &nbsp;·&nbsp; +10%</span></span></label>
      <label class="rcb"><input type="checkbox" id="r_fiber"><span><b>Fiber on-site</b><br><span class="rcb-sub">dark fiber tap exists &nbsp;·&nbsp; +20%</span></span></label>
      <label class="rcb"><input type="checkbox" id="r_zoning"><span><b>Zoning approved</b><br><span class="rcb-sub">industrial / data-center zone &nbsp;·&nbsp; +30%</span></span></label>
      <label class="rcb"><input type="checkbox" id="r_permits"><span><b>Permits in hand</b><br><span class="rcb-sub">SLR / AQMD / etc cleared &nbsp;·&nbsp; +20%</span></span></label>
    </div>
    <div style="margin-top:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
      <span style="font-size:12px;color:var(--muted)">Current readiness stack:</span>
      <span id="r-mult" style="font-family:monospace;background:var(--panel2);color:var(--accent2);padding:4px 10px;border-radius:4px;font-weight:700">1.000×</span>
      <button type="submit" form="valForm" style="margin-left:auto;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;">Calculate valuation →</button>
    </div>
    <style>
      .rcb { display:flex; align-items:flex-start; gap:10px; padding:10px 12px; background:var(--panel2); border:1px solid var(--border); border-radius:8px; cursor:pointer; font-size:13px; color:var(--fg); }
      .rcb:hover { border-color:var(--accent); }
      .rcb input { margin-top:4px; accent-color:var(--accent); }
      .rcb-sub { color:var(--muted); font-size:11px; font-weight:400; }
    </style>
  </div>

  <details id="assumptions" style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 20px;margin:0 0 16px;">
    <summary style="cursor:pointer;font-size:11px;color:var(--accent2);letter-spacing:0.1em;text-transform:uppercase;font-weight:700;outline:none;">
      ⚙  Adjust assumptions &nbsp;·&nbsp; LMP, gas, heat rate, discount
    </summary>
    <div style="font-size:12px;color:var(--muted);margin:12px 0 14px;">
      Defaults are the US-blended industry baselines that drive the <b>$/MWh</b>
      scenario cards below. Override any of these to stress-test against your
      utility tariff, regional LMP, or WACC. Edited values are flagged in the
      results.
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;">
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)">
        Grid LMP ($/MWh) <span style="color:var(--accent2);font-size:10px">default 45</span>
        <input id="a_lmp" type="number" step="0.5" min="5" max="500"
               placeholder="45" style="background:#0a0a0a;border:1px solid var(--border);color:var(--fg);padding:8px 10px;border-radius:6px;font-size:13px;font-family:inherit;">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)">
        Gas price ($/MMBtu) <span style="color:var(--accent2);font-size:10px">default 3.50</span>
        <input id="a_gas" type="number" step="0.10" min="0.5" max="50"
               placeholder="3.50" style="background:#0a0a0a;border:1px solid var(--border);color:var(--fg);padding:8px 10px;border-radius:6px;font-size:13px;font-family:inherit;">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)">
        CCGT heat rate (Btu/kWh) <span style="color:var(--accent2);font-size:10px">default 6,800</span>
        <input id="a_hr" type="number" step="50" min="5000" max="15000"
               placeholder="6800" style="background:#0a0a0a;border:1px solid var(--border);color:var(--fg);padding:8px 10px;border-radius:6px;font-size:13px;font-family:inherit;">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)">
        Discount rate (%) <span style="color:var(--accent2);font-size:10px">default 8</span>
        <input id="a_dr" type="number" step="0.25" min="1" max="25"
               placeholder="8" style="background:#0a0a0a;border:1px solid var(--border);color:var(--fg);padding:8px 10px;border-radius:6px;font-size:13px;font-family:inherit;">
      </label>
    </div>
  </details>

  <div id="results" class="hidden"></div>

  <p style="font-size:12px;color:var(--muted);margin-top:32px;">
    Methodology: <a href="/api/v1/site/value/methodology" style="color:var(--accent2)">/api/v1/site/value/methodology</a> · Engine v2.2 (2026-06-30) — per-MW clamped to a $150K raw floor and an $800K industry ceiling that <b>lifts to $1.2M for a fully entitled site</b> (zoned + permitted — de-risking prices above raw land); <b>verdict subtype</b> (constrained / weak_demand / developing) distinguishes Ashburn-class AVOID from rust-belt AVOID; <b>constraint-moat attenuation</b> lifts AVOID-by-constraint sites with grid+sub+permits in hand; <b>multi-story density</b> factor judges land sufficiency at ~stories×; 4 editable assumptions via the ⚙ panel; 6 site-readiness premiums; ±50% envelope.
  </p>
</div>

<script>
// Quick demo location presets — one click to load + auto-submit-friendly
document.querySelectorAll('.preset').forEach(btn => {
  btn.addEventListener('click', () => {
    document.getElementById('lat').value         = btn.dataset.lat;
    document.getElementById('lon').value         = btn.dataset.lon;
    document.getElementById('acres').value       = btn.dataset.acres;
    document.getElementById('target_mw').value   = btn.dataset.mw;
    document.getElementById('error').classList.add('hidden');
    // Visual confirmation
    document.querySelectorAll('.preset').forEach(b => b.style.borderColor = '');
    btn.style.borderColor = 'var(--accent)';
    btn.style.color       = '#fff';
  });
});

// v2.0: live readiness-stack multiplier display
const READINESS_PREMIUMS = {
  r_grid:    1.30,  r_sub:     1.25,  r_water: 1.10,
  r_fiber:   1.20,  r_zoning:  1.30,  r_permits: 1.20,
};
function updateReadinessMult() {
  let m = 1.0, n = 0;
  Object.entries(READINESS_PREMIUMS).forEach(([id, premium]) => {
    if (document.getElementById(id) && document.getElementById(id).checked) { m *= premium; n++; }
  });
  // mirror the backend: de-correlate grid×substation (both proxy grid access)
  const gc = document.getElementById('r_grid'), sc = document.getElementById('r_sub');
  if (gc && gc.checked && sc && sc.checked) {
    m *= (1.45 / (READINESS_PREMIUMS.r_grid * READINESS_PREMIUMS.r_sub));
  }
  // diminishing returns on 3+ stacked premiums
  if (m > 1.0 && n >= 3) m = 1.0 + (m - 1.0) * Math.pow(0.95, n - 2);
  const el = document.getElementById('r-mult');
  if (el) el.textContent = m.toFixed(3) + '×';
}
Object.keys(READINESS_PREMIUMS).forEach(id => {
  const cb = document.getElementById(id);
  if (cb) cb.addEventListener('change', updateReadinessMult);
});

function showFieldError(msg) {
  const e = document.getElementById('error');
  e.innerHTML = msg;
  e.classList.remove('hidden');
  e.scrollIntoView({behavior:'smooth', block:'nearest'});
}

// Pre-flight: catch the common typo where users enter a positive lon
// > 180 (forgot minus sign + decimal point — e.g. typed "833076" for
// what they meant as "-83.3076"). Suggest the corrected value
// instead of letting the API 400 silently.
function _suggestLonFix(lon) {
  const abs = Math.abs(lon);
  if (abs <= 180) return null;
  // Strip leading zeros + treat the trailing digits as decimal places.
  // 833076 → assume "83.3076" (US continental west = -83.3076)
  const s = String(Math.trunc(abs));
  if (s.length < 4) return null;
  const intPart = s.slice(0, s.length - 4);
  const decPart = s.slice(s.length - 4);
  const candidate = -parseFloat(`${intPart}.${decPart}`);
  if (candidate >= -180 && candidate <= 0) return candidate;
  return null;
}

document.getElementById('valForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  document.getElementById('error').classList.add('hidden');
  document.getElementById('results').classList.add('hidden');

  const lat = parseFloat(document.getElementById('lat').value);
  let   lon = parseFloat(document.getElementById('lon').value);
  const acres     = parseFloat(document.getElementById('acres').value);
  const target_mw = parseInt(document.getElementById('target_mw').value);

  // Client-side range check (browser min/max also enforces, but covers
  // pasted values + provides a friendlier error than the API's JSON).
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    showFieldError('<b>Lat out of range.</b> Use a value between -90 and 90 (e.g. 33.45 for Phoenix).');
    return;
  }
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
    const fix = _suggestLonFix(lon);
    const hint = fix !== null
      ? ` Did you mean <b style="color:var(--accent2)">${fix.toFixed(4)}</b>? <button type="button" id="autofixLon" style="background:var(--accent);color:#fff;border:0;padding:4px 10px;border-radius:4px;margin-left:8px;font-weight:600;cursor:pointer">Use ${fix.toFixed(4)}</button>`
      : ' Remember: longitudes <b>west of Greenwich are NEGATIVE</b> (Phoenix = -112.07, NOT 112.07).';
    showFieldError(`<b>Lon out of range (${lon}).</b> Use a value between -180 and 180.${hint}`);
    // Wire the autofix button
    if (fix !== null) {
      setTimeout(() => {
        const btn = document.getElementById('autofixLon');
        if (btn) btn.addEventListener('click', () => {
          document.getElementById('lon').value = fix.toFixed(4);
          document.getElementById('error').classList.add('hidden');
          document.getElementById('valForm').requestSubmit();
        });
      }, 0);
    }
    return;
  }
  if (!Number.isFinite(acres) || acres <= 0) {
    showFieldError('<b>Acres must be > 0.</b>');
    return;
  }
  if (!Number.isFinite(target_mw) || target_mw <= 0) {
    showFieldError('<b>Target MW must be > 0.</b>');
    return;
  }

  // v2.0: collect 6 site-readiness flags + post them to the API
  const readiness = {
    grid_interconnect_ready: document.getElementById('r_grid').checked,
    substation_on_site:      document.getElementById('r_sub').checked,
    water_secured:           document.getElementById('r_water').checked,
    fiber_on_site:           document.getElementById('r_fiber').checked,
    zoning_approved:         document.getElementById('r_zoning').checked,
    permits_in_hand:         document.getElementById('r_permits').checked,
  };

  // v2.1 — optional overrides (only sent if non-empty)
  const hr  = parseFloat(document.getElementById('heat_rate_ccgt').value);
  const tar = parseFloat(document.getElementById('utility_gas_usd_mmbtu').value);

  const body = {
    lat, lon, acres, target_mw,
    deadline_months: parseInt(document.getElementById('deadline_months').value || 24),
    stories: parseInt(document.getElementById('stories').value || 1),
    /* r-ttp0: the field's own min is 0 and 0 is what an energized site enters —
       `g > 0` threw it away before it ever reached the API. */
    grid_ttp_months: (function(){ const g = parseInt(document.getElementById('grid_ttp_months').value); return Number.isFinite(g) && g >= 0 ? g : undefined; })(),
    power_source: (document.getElementById('power_source') || {}).value || 'auto',
    readiness: readiness,
  };
  if (Number.isFinite(hr) && hr > 0)  body.heat_rate_ccgt        = hr;
  if (Number.isFinite(tar) && tar > 0) body.utility_gas_usd_mmbtu = tar;
  // v2.1b — new assumption overrides from the "Adjust assumptions" panel
  const a_lmp = parseFloat(document.getElementById('a_lmp').value);
  const a_gas = parseFloat(document.getElementById('a_gas').value);
  const a_hr  = parseFloat(document.getElementById('a_hr').value);
  const a_dr  = parseFloat(document.getElementById('a_dr').value);
  if (Number.isFinite(a_lmp) && a_lmp > 0) body.grid_lmp_usd_per_mwh = a_lmp;
  // a_gas overrides the same gas tariff override as utility_gas_usd_mmbtu —
  // the assumption-panel value wins if both are set.
  if (Number.isFinite(a_gas) && a_gas > 0) body.utility_gas_usd_mmbtu = a_gas;
  if (Number.isFinite(a_hr)  && a_hr  > 0) body.heat_rate_ccgt        = a_hr;
  if (Number.isFinite(a_dr)  && a_dr  > 0) body.discount_rate         = a_dr / 100.0;
  const apiKey = document.getElementById('api_key').value.trim();
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-API-Key'] = apiKey;

  // The valuation POST is a heavy idempotent calc; a cold/flapping backend can
  // trip the edge failover 503 once, then succeed. Retry transient 5xx/network
  // failures transparently (up to 3 attempts, backoff) instead of surfacing a
  // scary 503 the user has to manually re-click.
  const _btn = document.querySelector('button[form="valForm"][type="submit"]');
  const _btnTxt = _btn ? _btn.textContent : '';
  if (_btn) { _btn.disabled = true; _btn.textContent = 'Calculating…'; }
  let d = null, lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const r = await fetch('/api/v1/site/value', {
        method: 'POST', headers, body: JSON.stringify(body),
      });
      if (r.status >= 500 && attempt < 3) {   // transient origin/failover — retry
        await new Promise(res => setTimeout(res, 800 * attempt));
        continue;
      }
      d = await r.json();
      break;
    } catch (err) {
      lastErr = err;
      if (attempt >= 3) break;
      await new Promise(res => setTimeout(res, 800 * attempt));
    }
  }
  if (_btn) { _btn.disabled = false; _btn.textContent = _btnTxt; }
  if (!d) {
    showFieldError('<b>Service busy.</b> The valuation engine was warming up — please click Calculate valuation again.');
    return;
  }
  if (!d.ok) {
    showFieldError(`<b>${d.error || 'error'}:</b> ${d.hint || 'Unknown error'}`);
    return;
  }
  window.__dcLast = d; window.__dcBody = body; window.__dcHeaders = headers;
  document.getElementById('results').innerHTML = renderResults(d);
  document.getElementById('results').classList.remove('hidden');
  document.getElementById('results').scrollIntoView({behavior:'smooth', block:'start'});
});

function fmt$(v) { return '$' + Math.round(v).toLocaleString(); }
function fmtM$(v) { return '$' + (v / 1_000_000).toFixed(1) + 'M'; }

// ===== Owner one-pager exports (branded PDF) =====
// Branded owner one-pager builders — pure (no DOM). These exact function bodies
// get embedded into routes/site_valuation_engine.py (_PAGE_HTML <script>).
// dcOnePager(d, title)         -> single-valuation branded HTML doc
// dcTwoTier(rawD, entD, title) -> two-tier (pre-dev vs entitled) branded HTML doc

function dcMoney(x){ const n=Number(x); if(!isFinite(n)) return '—';
  return Math.abs(n)>=1e6 ? '$'+(n/1e6).toFixed(1)+'M' : '$'+Math.round(n).toLocaleString(); }
function dcD0(x){ const n=Number(x); return isFinite(n) ? '$'+Math.round(n).toLocaleString() : '—'; }
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function titleCase(s){ return String(s||'').replace(/[-_]+/g,' ').split(' ').map(w=> w ? w.charAt(0).toUpperCase()+w.slice(1) : w).join(' '); }

function dcCss(){ return `
@page { size: letter; margin: 8mm; }
* { margin:0; padding:0; box-sizing:border-box; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
body { font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; background:#0a0a0f; color:#fafafa; font-size:10.5px; line-height:1.36; }
.wrap { padding:1px 3px; }
.top { display:flex; justify-content:space-between; align-items:baseline; border-bottom:1px solid rgba(255,255,255,.12); padding-bottom:7px; margin-bottom:9px; }
.brand { font-weight:800; letter-spacing:.02em; font-size:15px; } .brand span { color:#818cf8; }
.tag { font-family:'JetBrains Mono',monospace; font-size:9.5px; color:#a1a1aa; letter-spacing:.13em; text-transform:uppercase; }
h1 { font-size:21px; font-weight:700; letter-spacing:-.02em; margin-bottom:3px; }
.sub { color:#c4c4cc; font-size:11px; margin-bottom:9px; }
.pillg { display:inline-block; border:1px solid #10b981; color:#10b981; border-radius:999px; padding:1px 8px; font-size:9px; font-weight:700; font-family:'JetBrains Mono',monospace; }
.pill { display:inline-block; border:1px solid #f59e0b; color:#f59e0b; border-radius:999px; padding:1px 8px; font-size:9px; font-weight:700; font-family:'JetBrains Mono',monospace; }
.hero { background:linear-gradient(135deg,rgba(99,102,241,.15),rgba(168,85,247,.06)); border:1px solid rgba(99,102,241,.35); border-radius:13px; padding:12px 16px; margin-bottom:9px; }
.range { font-size:28px; font-weight:800; letter-spacing:-.02em; }
.range small { font-size:13px; color:#a1a1aa; font-weight:500; }
.mid { color:#a1a1aa; font-size:11px; margin-top:3px; }
.bar { display:flex; gap:7px; margin-top:9px; flex-wrap:wrap; }
.chip { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.1); border-radius:8px; padding:6px 10px; font-size:10px; }
.chip b { display:block; font-size:12.5px; margin-top:2px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:9px; margin-bottom:9px; }
.card { background:#131319; border:1px solid rgba(255,255,255,.09); border-radius:11px; padding:11px 14px; margin-bottom:9px; }
.card h3 { font-size:10.5px; text-transform:uppercase; letter-spacing:.07em; color:#818cf8; margin-bottom:8px; }
.build div { display:flex; justify-content:space-between; padding:3px 0; font-size:11.5px; }
.build .subk { color:#a1a1aa; font-size:10px; }
.build .tot { border-top:1px solid rgba(255,255,255,.14); margin-top:3px; padding-top:6px; font-weight:700; font-size:12.5px; }
.build .up { color:#10b981; }
.tiers { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:9px; }
.tier { border-radius:13px; padding:12px 15px; }
.tierA { background:linear-gradient(135deg,rgba(148,163,184,.13),rgba(148,163,184,.04)); border:1px solid rgba(148,163,184,.4); }
.tierB { background:linear-gradient(135deg,rgba(16,185,129,.14),rgba(16,185,129,.05)); border:1px solid rgba(16,185,129,.45); }
.tlabel { font-size:9.5px; text-transform:uppercase; letter-spacing:.07em; font-weight:700; margin-bottom:4px; }
.tierA .tlabel { color:#94a3b8; } .tierB .tlabel { color:#10b981; }
.big { font-size:26px; font-weight:800; letter-spacing:-.02em; }
.rng { color:#a1a1aa; font-size:10.5px; margin-top:2px; }
.perm { font-family:'JetBrains Mono',monospace; font-size:10px; color:#c4c4cc; margin-top:1px; }
.bd { margin-top:8px; border-top:1px solid rgba(255,255,255,.1); padding-top:6px; }
.bd div { display:flex; justify-content:space-between; padding:2px 0; font-size:10.5px; }
.bd .tot { font-weight:700; border-top:1px solid rgba(255,255,255,.12); margin-top:3px; padding-top:4px; }
.bd .up { color:#10b981; }
.unlock { background:#131319; border:1px dashed rgba(16,185,129,.55); border-radius:11px; padding:8px 14px; margin-bottom:9px; display:flex; justify-content:space-between; align-items:center; }
.unlock .l { font-size:11px; } .unlock .l b { color:#10b981; } .unlock .r { font-size:19px; font-weight:800; color:#10b981; }
table { width:100%; border-collapse:collapse; font-size:11px; }
th { text-align:left; color:#71717a; font-weight:600; font-size:9.5px; text-transform:uppercase; letter-spacing:.05em; padding:3px 6px; border-bottom:1px solid rgba(255,255,255,.1); }
td { padding:5px 6px; border-bottom:1px solid rgba(255,255,255,.05); font-family:'JetBrains Mono',monospace; }
tr.best td { color:#10b981; font-weight:700; }
.disc { font-size:10.5px; color:#c4c4cc; }
.meth { background:#131319; border:1px solid #818cf8; border-radius:11px; padding:9px 14px; margin-bottom:5px; }
.meth h3 { font-size:12px; margin-bottom:5px; }
.mrow { display:grid; grid-template-columns:1fr 1fr; gap:15px; margin-top:5px; }
.mrow ul { padding-left:15px; font-size:10px; line-height:1.34; } .mrow li { margin-bottom:1px; }
.mh { font-size:9.5px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:#818cf8; margin-bottom:3px; }
.foot { color:#71717a; font-size:8.5px; text-align:center; margin-top:4px; border-top:1px solid rgba(255,255,255,.1); padding-top:6px; }
.pbar { text-align:center; margin:10px 0 4px; }
.pbtn { background:#818cf8; color:#0a0a0f; border:0; border-radius:7px; padding:8px 18px; font-weight:700; font-size:12px; cursor:pointer; font-family:inherit; }
@media print { .pbar { display:none; } }
`; }

function scenRows(sc, bf){
  const order = ['gas_btm','grid_only','gas_to_grid_hybrid'];
  const labels = { gas_btm:'Gas-BTM (CCGT)', grid_only:'Grid (interconnect)', gas_to_grid_hybrid:'Gas-to-grid hybrid' };
  return order.filter(k=>sc[k]).map(k=>{ const s=sc[k]||{}; const star = k===bf?' ★':'';
    return `<tr class="${k===bf?'best':''}"><td>${labels[k]}${star}</td><td>${dcD0(s.levelized_usd_per_mwh)}/MWh</td><td>${dcMoney(s.capex_usd)}</td><td>${Math.round(s.time_to_power_months||0)} mo</td></tr>`;
  }).join(''); }

function methCard(d){
  const mc=d.market_context||{}, inp=d.input||{}, v=d.valuation||{};
  const stories = inp.stories||1;
  return `<div class="meth"><h3>Indicative Valuation — Methodology &amp; Caveats</h3>
    <div class="disc">A <b>model-based indicative valuation</b> for screening &amp; negotiation — <b>not a certified (USPAP) appraisal</b>. Use the range, not a single point.</div>
    <div class="mrow">
      <div><div class="mh">Grounded in real data</div><ul>
        <li>Geography — FCC geocode (${esc(mc.site_state||'')})</li><li>Power — EIA per-state, large-load adjusted${mc.power_cost_usd_mwh?(' ($'+mc.power_cost_usd_mwh+'/MWh)'):''}</li>
        <li>Gas — per-hub basis model</li><li>Verdict + moat — nearest PJM/DCPI market</li><li>NPV / LCOE — exact discounted math</li></ul></div>
      <div><div class="mh">Confirm before relying</div><ul>
        <li><b>No land comps</b> — base $/MW is model-derived, not comp-anchored</li>
        <li>Ceilings ($0.8M raw / $1.2M entitled / $1.6M powered per MW) are assumed caps, not observed prices</li>
        <li>LCOE = energy + capex floor (excl. O&amp;M ~$3–5/MWh); capex screening-grade</li>
        <li>Tax abatement is the <b>state</b> record — confirm the parcel's actual term</li>
        <li>Land fit assumes ${stories} building stor${stories==1?'y':'ies'}</li></ul></div>
    </div>
    <div class="disc" style="margin-top:6px;border-top:1px solid rgba(255,255,255,.1);padding-top:6px"><b>How to use:</b> a screening range &amp; negotiating anchor. For a term-sheet number, corroborate with 1–2 broker/appraiser comparables.</div>
  </div>`; }

function foot(d){ const mc=d.market_context||{}; const as_of=(d.as_of||'').slice(0,10);
  return `<div class="foot">Prepared with DC Hub data-center &amp; energy intelligence · Engine ${esc(d.engine_version||'v2.3')} · Market ${esc(titleCase(mc.nearest_market_slug||''))}, ${esc(mc.site_state||'')} · Sources: DCPI, EIA, tax-incentives · ${as_of}</div>`; }

function shell(title, tag, bodyHtml, withPrint){
  return `<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title><style>${dcCss()}</style></head><body><div class="wrap">
    <div class="top"><div class="brand">DC HUB <span>SITE VALUATION</span></div><div class="tag">${esc(tag)}</div></div>
    ${bodyHtml}
    ${withPrint?'<div class="pbar"><button class="pbtn" onclick="window.print()">🖨 Print / Save as PDF</button></div>':''}
  </div></body></html>`; }

function dcOnePager(d, title){
  const v=d.valuation||{}, mc=d.market_context||{}, dcpi=d.dcpi_context||{}, sc=d.scenarios||{}, inp=d.input||{};
  const bf=(d.best_fit||{}).scenario||'', ent=v.entitlement||{}, ab=v.tax_abatement||{}, suf=v.site_sufficiency||{}, mm=v.multipliers||{};
  const mw=inp.target_mw||0;
  const mwContrib = Number((v.site_value_breakdown||{}).mw_contribution_usd || (Number(v['$/mw_mid'])||0)*mw);
  const entLift = Number(ent.lift_usd||0);
  const rawLand = mwContrib - entLift;
  const abPrem = Number(ab.premium_usd||0);
  const abPct = ab.factor ? Math.round((Number(ab.factor)-1)*100) : 0;
  const g = sc.grid_only||{};
  const pills = [];
  if (ent.firm_powered) pills.push('<span class="pillg">FIRM POWER</span>');
  else if (ent.entitled) pills.push('<span class="pillg">ENTITLED</span>');
  if (dcpi.verdict) pills.push(`<span class="pill">${esc(dcpi.verdict)}${mm.moat_flags_active>=2?' · moat':''}</span>`);
  const build = [];
  build.push(`<div><span>${entLift>0?'Raw-land value (capped $800K/MW)':('Powered raw-land ('+dcD0(v['$/mw_mid'])+'/MW)')}</span><b>${dcMoney(rawLand)}</b></div>`);
  if (entLift>0) build.push(`<div class="subk"><span>+ Entitlement${ent.firm_powered?' + firm power':''} → ${dcD0(v['$/mw_mid'])}/MW</span><b class="up">+${dcMoney(entLift)}</b></div>`);
  if (abPrem>0) build.push(`<div><span>+ Tax abatement (+${abPct}%)</span><b class="up">+${dcMoney(abPrem)}</b></div>`);
  build.push(`<div class="tot"><span>Indicative midpoint</span><b>${dcMoney(v.site_value_usd_mid)}</b></div>`);
  const whyMoat = mm.moat_flags_active>=2 ? ` A constraint <b>moat</b> (shovel-ready in a saturated cluster) lifts the verdict multiplier to ${(mm.verdict_mult||1).toFixed(2)}×.` : '';
  const body = `
  <h1>${esc(title)}</h1>
  <div class="sub">${mw} MW · ${suf.effective_acres_per_mw||''} eff. ac/MW · ${esc(titleCase(mc.nearest_market_slug||''))} · ${esc(mc.site_state||'')} ${pills.join(' ')}</div>
  <div class="hero">
    <div class="range">${dcMoney(v.site_value_usd_low)} – ${dcMoney(v.site_value_usd_high)} <small>indicative range · ${mw} MW</small></div>
    <div class="mid">midpoint <b>${dcMoney(v.site_value_usd_mid)}</b> · ${dcD0(v['$/mw_mid'])}/MW × ${mw} MW · ±50% envelope</div>
    <div class="bar">
      <div class="chip">Market<b>${esc(titleCase(mc.nearest_market_slug||''))} · ${esc(mc.site_state||'')}</b></div>
      <div class="chip">Power (large-load)<b>$${mc.power_cost_usd_mwh||'—'}/MWh</b></div>
      <div class="chip">Best path<b>${esc(titleCase(bf))} · ${Math.round((sc[bf]||{}).time_to_power_months||0)} mo</b></div>
      <div class="chip">Land fit<b>${esc(titleCase(suf.category||''))} (${suf.effective_acres_per_mw||''} ac/MW)</b></div>
    </div>
  </div>
  <div class="grid2">
    <div class="card"><h3>Value build-up (${mw} MW)</h3><div class="build">${build.join('')}</div></div>
    <div class="card"><h3>Why this number</h3><div class="disc">${esc(dcpi.verdict||'')} verdict from the nearest market.${whyMoat} ${ent.entitled?'Zoned + permitted prices above the raw-land ceiling.':'Pre-entitlement (raw powered land).'} ${ab.applied?('The '+(mc.site_state||'')+' tax abatement de-risks opex (+'+abPct+'%).'):''}</div></div>
  </div>
  <div class="card"><h3>Power path — scenarios (10-yr NPV)</h3>
    <table><tr><th>Scenario</th><th>Levelized cost</th><th>CapEx</th><th>Time-to-power</th></tr>${scenRows(sc,bf)}</table></div>
  ${methCard(d)}
  ${foot(d)}`;
  return shell(title, 'Indicative Valuation', body, true);
}

function dcTwoTier(rawD, entD, title){
  const va=rawD.valuation||{}, vb=entD.valuation||{};
  const mc=entD.market_context||{}, sc=entD.scenarios||{}, inp=entD.input||{};
  const g=sc.grid_only||{}, gas=sc.gas_btm||{}, bf=(entD.best_fit||{}).scenario||'';
  const mw=inp.target_mw||0;
  const aLand = Number((va.site_value_breakdown||{}).mw_contribution_usd||0) - Number((va.entitlement||{}).lift_usd||0);
  const aAb = Number((va.tax_abatement||{}).premium_usd||0);
  const bMw = Number((vb.site_value_breakdown||{}).mw_contribution_usd||0);
  const bLift = Number((vb.entitlement||{}).lift_usd||0);
  const bLand = bMw - bLift;
  const bAb = Number((vb.tax_abatement||{}).premium_usd||0);
  const gap = Number(vb.site_value_usd_mid||0) - Number(va.site_value_usd_mid||0);
  const powerLabel = bf==='grid_only' ? 'actual firm grid (no on-site gas)' : 'lowest-cost power path';
  const body = `
  <h1>${esc(title)} · ${mw} MW</h1>
  <div class="sub">${esc(titleCase(mc.nearest_market_slug||''))} · ${esc(mc.site_state||'')} · <span class="pillg">${bf==='grid_only'?'GRID-POWERED (no gas)':esc(titleCase(bf))}</span></div>
  <div class="tiers">
    <div class="tier tierA">
      <div class="tlabel">Tier A · Pre-development (as-is, not zoned)</div>
      <div class="big">${dcMoney(va.site_value_usd_mid)}</div>
      <div class="rng">range ${dcMoney(va.site_value_usd_low)} – ${dcMoney(va.site_value_usd_high)}</div>
      <div class="perm">${dcD0(va['$/mw_mid'])}/MW × ${mw} MW</div>
      <div class="bd">
        <div><span>Powered raw-land</span><b>${dcMoney(aLand)}</b></div>
        ${aAb>0?`<div><span>+ Tax abatement</span><b>+${dcMoney(aAb)}</b></div>`:''}
        <div class="tot"><span>Midpoint</span><b>${dcMoney(va.site_value_usd_mid)}</b></div>
      </div>
    </div>
    <div class="tier tierB">
      <div class="tlabel">Tier B · Fully zoned, entitled &amp; permitted</div>
      <div class="big">${dcMoney(vb.site_value_usd_mid)}</div>
      <div class="rng">range ${dcMoney(vb.site_value_usd_low)} – ${dcMoney(vb.site_value_usd_high)}</div>
      <div class="perm">${dcD0(vb['$/mw_mid'])}/MW × ${mw} MW</div>
      <div class="bd">
        <div><span>Raw-land (capped $800K/MW)</span><b>${dcMoney(bLand)}</b></div>
        ${bLift>0?`<div><span>+ Entitlement (→${dcD0(vb['$/mw_mid'])}/MW)</span><b class="up">+${dcMoney(bLift)}</b></div>`:''}
        ${bAb>0?`<div><span>+ Tax abatement</span><b class="up">+${dcMoney(bAb)}</b></div>`:''}
        <div class="tot"><span>Midpoint</span><b>${dcMoney(vb.site_value_usd_mid)}</b></div>
      </div>
    </div>
  </div>
  <div class="unlock">
    <div class="l">⬆ <b>Entitlement is the value-creation play</b> — zoning + permits on the same power roughly <b>doubles</b> the site. Highest-ROI action before a sale.</div>
    <div class="r">+${dcMoney(gap)}</div>
  </div>
  <div class="card"><h3>Power basis — ${esc(powerLabel)}</h3>
    <div class="bar">
      <div class="chip">Levelized energy<b>${dcD0(g.levelized_usd_per_mwh)}/MWh</b></div>
      <div class="chip">Time to full ${mw} MW<b>${Math.round(g.time_to_power_months||0)} mo</b></div>
      <div class="chip">Interconnect capex<b>${dcMoney(g.capex_usd)}</b></div>
      <div class="chip">Large-load power<b>$${mc.power_cost_usd_mwh||'—'}/MWh</b></div>
    </div>
    ${bf==='grid_only'&&gas.capex_usd?`<div class="disc" style="margin-top:7px">Valued on the site's real grid — on-site gas <b>not assumed</b>; grid interconnect (${dcMoney(g.capex_usd)}) avoids the ~${dcMoney(gas.capex_usd)} a CCGT build would add.</div>`:''}
  </div>
  ${methCard(entD)}
  ${foot(entD)}`;
  return shell(title, 'Grid Valuation · Two-Tier', body, true);
}

// ── Owner one-pager export — DOM wiring (A: single, B: two-tier) ──
function dcOpenPrint(htmlDoc){
  var w = window.open('', '_blank');
  if(!w){ alert('Popup blocked — allow popups on this site to download the one-pager.'); return; }
  w.document.open(); w.document.write(htmlDoc); w.document.close(); w.focus();
}
function dcTitle(d){
  var el = document.getElementById('site_label');
  var lbl = (el && el.value) ? el.value.trim() : '';
  if(lbl) return lbl;
  var mc = (d && d.market_context) || {};
  var mk = titleCase(mc.nearest_market_slug || 'Site');
  return 'Powered-Land Site — ' + mk + (mc.site_state ? ', ' + mc.site_state : '');
}
function dcExportOnePager(){
  var d = window.__dcLast;
  if(!d || !d.valuation){ alert('Run a valuation first, then download the one-pager.'); return; }
  dcOpenPrint(dcOnePager(d, dcTitle(d)));
}
async function dcExportTwoTier(btn){
  var base = window.__dcBody, headers = window.__dcHeaders;
  if(!base){ alert('Run a valuation first, then generate the two-tier sheet.'); return; }
  var txt = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = 'Generating…'; }
  try {
    var rd = Object.assign({}, base.readiness || {});
    var rawBody = Object.assign({}, base, { readiness: Object.assign({}, rd, { zoning_approved:false, permits_in_hand:false }) });
    var entBody = Object.assign({}, base, { readiness: Object.assign({}, rd, { zoning_approved:true,  permits_in_hand:true  }) });
    var results = await Promise.all([
      fetch('/api/v1/site/value', { method:'POST', headers: headers, body: JSON.stringify(rawBody) }).then(function(r){ return r.json(); }),
      fetch('/api/v1/site/value', { method:'POST', headers: headers, body: JSON.stringify(entBody) }).then(function(r){ return r.json(); })
    ]);
    var rawR = results[0], entR = results[1];
    if(!rawR || !rawR.ok || !entR || !entR.ok){ alert('Could not generate the two-tier sheet — please try again.'); return; }
    dcOpenPrint(dcTwoTier(rawR, entR, dcTitle(entR)));
  } catch(e){ alert('Two-tier export failed: ' + e); }
  finally { if(btn){ btn.disabled = false; btn.textContent = txt; } }
}
function renderResults(d) {
  const dcpi = d.dcpi_context || {};
  const verdictClass = 'verdict-' + (dcpi.verdict || 'UNKNOWN');
  // v2.1c — surface the AVOID subtype so users see WHY a market got
  // its verdict. Constrained AVOID (Ashburn) and weak-demand AVOID
  // (rural rust belt) get opposite parcel-value implications.
  const subtype = dcpi.verdict_subtype || 'n/a';
  const subtypeChip = (subtype !== 'n/a' && subtype !== 'unknown')
    ? `<span title="${(dcpi.verdict_explainer||'').replace(/"/g,'&quot;')}"
             style="display:inline-block;margin-left:8px;font-size:10px;letter-spacing:0.08em;text-transform:uppercase;
                    background:${subtype==='constrained'?'#0EA5E9':(subtype==='weak_demand'?'#dc2626':'var(--panel2)')};
                    color:${subtype==='constrained'?'#000':'#fff'};
                    padding:3px 8px;border-radius:4px;font-weight:700;cursor:help;">
        ${subtype.replace(/_/g,' ')} <span style="opacity:0.7">ⓘ</span>
      </span>`
    : '';
  const subtypeNote = (subtype !== 'n/a' && subtype !== 'unknown' && dcpi.verdict_explainer)
    ? `<div style="margin-top:8px;font-size:12px;color:var(--muted);background:var(--panel2);border-left:3px solid ${subtype==='constrained'?'var(--accent)':(subtype==='weak_demand'?'#dc2626':'var(--border)')};padding:8px 10px;border-radius:4px;">
         <b style="color:var(--fg)">Why ${dcpi.verdict}?</b> ${dcpi.verdict_explainer}
       </div>`
    : '';
  let html = `
    <div class="row">
      <div class="card">
        <h3>Market context</h3>
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
          <div class="verdict ${verdictClass}">${dcpi.verdict || 'unknown'}</div>
          ${subtypeChip}
        </div>
        <div class="stat">${(d.market_context.nearest_market_slug || '').replace(/-/g, ' ')}</div>
        <div class="stat-label">${d.market_context.nearest_market_state} · ${d.market_context.miles_from_centroid} mi from centroid · ISO: ${dcpi.iso || 'n/a'}</div>
        <div style="margin-top:12px;font-size:13px;color:var(--muted)">
          DCPI composite: <b>${(dcpi.composite_score || 0).toFixed(1)}</b> · Excess power: <b>${(dcpi.excess_power_score || 0).toFixed(1)}</b> · Time-to-power: <b>${(dcpi.time_to_power_months || 0).toFixed(0)} months</b>
        </div>
        ${subtypeNote}
      </div>`;

  if (d.valuation) {
    const m = d.valuation.multipliers || {};
    const applied = m.readiness_applied || {};
    const appliedKeys = Object.keys(applied);
    const readinessLine = appliedKeys.length === 0
      ? '<i>raw land — no readiness flags set</i>'
      : appliedKeys.map(k => k.replace(/_/g, ' ') + ' (+' + Math.round((applied[k]-1)*100) + '%)').join(' · ');
    // v2.1b — surface the band clamp visibly so users know what just happened
    const bandStatus = d.valuation['$/mw_band_status'] || 'in_band';
    const floor = d.valuation['$/mw_band_floor'] || 150000;
    const ceil  = d.valuation['$/mw_band_ceiling'] || 800000;
    const uncapped = d.valuation['$/mw_uncapped'];
    const ent = d.valuation.entitlement || {};
    let bandNote = '';
    if (bandStatus === 'ceiling_saturated') {
      const capMsg = ent.entitled
        ? `raw stack would price at ${fmt$(uncapped)}/MW; a zoned + permitted site is de-risked and prices <b>above</b> the ${fmt$(ent.raw_ceiling || 800000)}/MW raw-land ceiling — clamped to the entitled cap ${fmt$(ceil)}/MW.`
        : `raw stack would price at ${fmt$(uncapped)}/MW; clamped to industry cap ${fmt$(ceil)}/MW. Adding zoning + permits would lift this ceiling.`;
      bandNote = `<div style="margin-top:10px;background:rgba(245,158,11,0.08);border:1px solid var(--warn);border-radius:6px;padding:8px 10px;font-size:12px;color:var(--warn)">
        <b>Saturated at ceiling:</b> ${capMsg}
      </div>`;
    } else if (bandStatus === 'floor_saturated') {
      bandNote = `<div style="margin-top:10px;background:rgba(220,38,38,0.08);border:1px solid #dc2626;border-radius:6px;padding:8px 10px;font-size:12px;color:#fca5a5">
        <b>At industry floor:</b> clamped at ${fmt$(floor)}/MW. Sub-floor sites typically indicate raw land + AVOID verdict + no readiness.
      </div>`;
    } else {
      bandNote = `<div style="margin-top:8px;font-size:11px;color:var(--muted);opacity:0.7">
        In-band: per-MW ${fmt$(d.valuation['$/mw_mid'])} sits between industry floor ${fmt$(floor)} and ceiling ${fmt$(ceil)}.
      </div>`;
    }
    // Entitlement premium — the dollars zoning + permits unlock above the raw cap
    if (ent.entitled && (ent.lift_usd || 0) > 0) {
      bandNote += `<div style="margin-top:8px;background:rgba(16,185,129,0.10);border:1px solid #10b981;border-radius:6px;padding:8px 10px;font-size:12px;color:#10b981">
        <b>Entitlement premium: +${fmtM$(ent.lift_usd)}</b> &nbsp;(${fmt$(ent.lift_per_mw)}/MW) — zoned + permitted removes ~12-24 mo and the entitlement cost/risk, pricing the site above the ${fmt$(ent.raw_ceiling || 800000)}/MW raw-land ceiling.
      </div>`;
    } else if (ent.entitled) {
      bandNote += `<div style="margin-top:8px;font-size:11px;color:#10b981;opacity:0.85">✓ Zoned + permitted — entitlement is already priced into the $/MW (site sits below the raw-land ceiling, no lift needed).</div>`;
    }
    // Tax-abatement premium (property-tax abatement de-risks opex)
    const ta = d.valuation.tax_abatement || {};
    if (ta.applied && (ta.premium_usd || 0) > 0) {
      bandNote += `<div style="margin-top:8px;background:rgba(14,165,233,0.10);border:1px solid #0ea5e9;border-radius:6px;padding:8px 10px;font-size:12px;color:#0ea5e9">
        <b>Tax-abatement premium: +${fmtM$(ta.premium_usd)}</b> — ${ta.note}
      </div>`;
    }
    // v2.2 — site sufficiency block + MW-only breakdown
    const bd = d.valuation.site_value_breakdown || {};
    const suff = d.valuation.site_sufficiency || {};
    const suffColor = {
      undersized: '#dc2626', tight: '#f59e0b', typical: '#10b981',
      comfortable: '#10b981', surplus: '#0ea5e9',
    }[suff.category] || '#9ca3af';
    html += `
      <div class="card">
        <h3>Site value — by the MW</h3>
        <div class="stat">${fmtM$(d.valuation.site_value_usd_mid)}</div>
        <div class="stat-label">midpoint · ${fmtM$(d.valuation.site_value_usd_low)} – ${fmtM$(d.valuation.site_value_usd_high)}  <span style="opacity:0.6">(±50% envelope)</span></div>
        <div style="margin-top:14px;font-size:14px;color:var(--fg)">
          <b style="font-size:18px;color:var(--accent2)">${fmt$(d.valuation['$/mw_mid'])}</b> <span style="color:var(--muted);font-size:12px">per MW</span>
          &nbsp;×&nbsp;
          <b>${d.input.target_mw} MW</b>
          &nbsp;=&nbsp;
          <b>${fmtM$(bd.mw_contribution_usd || 0)}</b>
        </div>
        ${bd.surplus_land_residual_usd > 0 ? `
        <div style="margin-top:6px;font-size:12.5px;color:var(--muted)">
          + surplus land residual: <b style="color:var(--fg)">${fmtM$(bd.surplus_land_residual_usd)}</b>
          <span style="opacity:0.7"> · ${suff.surplus_acres} acres × $${(8000).toLocaleString()}/ac</span>
        </div>` : ''}
        ${bandNote}
        ${suff.category && suff.category !== 'invalid' ? `
        <div style="margin-top:14px;background:var(--panel2);border:1px solid var(--border);border-left:3px solid ${suffColor};padding:10px 12px;border-radius:4px;font-size:12.5px">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="background:${suffColor};color:#fff;font-weight:700;padding:2px 8px;border-radius:3px;text-transform:uppercase;font-size:10.5px;letter-spacing:0.06em">${suff.category}</span>
            <span style="color:var(--fg);font-weight:700">${suff.acres_per_mw} ac/MW</span>
            <span style="color:var(--muted);font-size:11px">typical band: ${suff.typical_band ? suff.typical_band[0] + '–' + suff.typical_band[1] : '?'} ac/MW</span>
          </div>
          <div style="margin-top:6px;color:var(--muted);font-size:11.5px;line-height:1.45">${suff.note || ''}</div>
        </div>` : ''}
        <div style="margin-top:12px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--border);padding-top:10px;line-height:1.5">
          <b>How sites trade:</b> data-center parcels are priced by the MW. Land cost is implicit in every $/MW comp — that's why we don't add $/acre × acres on top (would double-count). Acres are reported above as a build-sufficiency check.
        </div>
        <div style="margin-top:10px;font-size:12px;color:var(--muted);border-top:1px solid var(--border);padding-top:10px">
          Stack: verdict <b style="color:var(--fg)">${(m.verdict_mult||1).toFixed(2)}×</b>${m.moat_attenuation_applied?`<span style="background:var(--accent);color:#000;font-size:9px;padding:2px 5px;border-radius:3px;margin-left:4px;font-weight:700;letter-spacing:0.05em">MOAT ${(m.verdict_mult_base||0).toFixed(2)}→${(m.verdict_mult||0).toFixed(2)}</span>`:''}
          · best-fit <b style="color:var(--fg)">${(m.bestfit_mult||1).toFixed(2)}×</b>
          · readiness <b style="color:var(--accent2)">${(m.readiness_mult||1).toFixed(3)}×</b>
          <br><span style="opacity:0.85">Readiness applied: ${readinessLine}</span>
          ${m.moat_explainer?`<br><span style="display:block;margin-top:8px;padding:8px 10px;background:rgba(14,165,233,0.08);border-left:3px solid var(--accent);border-radius:4px;font-size:11px;color:var(--accent2);"><b>Constraint-moat:</b> ${m.moat_explainer}</span>`:''}
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
    // v2.1b — assumption strip so users see which $/MWh inputs were used
    const a = d.scenarios._assumptions || {};
    const editChip = (key, val, def, unit) => {
      const edited = Number(val) !== Number(def);
      return `<span style="background:${edited ? 'var(--accent)' : 'var(--panel2)'};color:${edited ? '#000' : 'var(--muted)'};padding:3px 8px;border-radius:4px;font-size:11px;font-family:monospace;margin-right:6px;font-weight:${edited ? '700' : '500'}">${key}: <b>${val}${unit}</b>${edited ? ' ✎' : ''}</span>`;
    };
    html += '<h3 style="margin-top:24px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">3-scenario NPV comparison ' +
            (a.edited ? '<span style="background:var(--accent);color:#000;font-size:10px;padding:3px 8px;border-radius:4px;font-weight:700;letter-spacing:0.08em">USER-EDITED ASSUMPTIONS</span>' : '') +
            '</h3>';
    if (a && typeof a.grid_lmp_usd_per_mwh !== 'undefined') {
      html += `<div style="margin:6px 0 14px;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;align-items:center;gap:0">
        <span style="margin-right:8px">Assumptions used:</span>
        ${editChip('Grid power',   a.grid_lmp_usd_per_mwh, a.grid_lmp_default, '/MWh')}${(d.market_context && d.market_context.power_cost_usd_mwh) ? `<span style="color:var(--muted);font-size:10px;margin-right:8px">(${d.market_context.site_state || ''} large-load rate, EIA industrial ×0.6)</span>` : ''}
        ${editChip('CCGT gas',     a.ccgt_gas_usd_per_mwh, a.ccgt_gas_default, '/MWh')}
        ${editChip('CCGT heat',    a.ccgt_heat_rate,       a.ccgt_heat_rate_default, ' Btu/kWh')}
        ${editChip('Discount',     (a.discount_rate*100).toFixed(2), (a.discount_rate_default*100).toFixed(2), '%')}
      </div>`;
    }
    html += '<div style="font-size:11px;color:var(--muted);margin:0 0 10px">LCOE = fuel/energy + amortized CapEx at 100% capacity factor; excludes fixed/variable O&M (~$3–5/MWh) — an energy-cost <b>floor</b>, not an all-in bid. CapEx multiples are screening-grade.</div>';
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
          <tr><td title="capex + 10yr opex discounted; cost-only, not net cashflow">10-yr Total Cost (NPV)</td><td style="text-align:right">${fmtM$(s.ten_year_npv_usd)}</td></tr>
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
    html += '<h3 style="margin-top:24px">Recent M&A in this market</h3>';
    html += '<div style="font-size:11px;color:var(--muted);margin:-4px 0 8px">Market <b>context</b> — these are company / portfolio M&A (enterprise value), <b>not land comps</b>. The site value above is model-derived, not anchored to these.</div>';
    html += '<table><tr><th>Date</th><th>Seller</th><th>Buyer</th><th>Deal value</th><th>MW</th><th>Type</th></tr>';
    d.comparable_sales.forEach(c => {
      html += `<tr><td>${c.announced_date || '—'}</td><td>${c.target || '—'}</td><td>${c.acquirer || '—'}</td><td>${c.value_usd ? fmtM$(c.value_usd) : '—'}</td><td>${c.mw ? Math.round(c.mw) : '—'}</td><td>${c.deal_type || '—'}</td></tr>`;
    });
    html += '</table>';
  }

  // ── Indicative Valuation — Methodology & Caveats (client-readable + printable) ──
  const _v = d.valuation || {}, _mc = d.market_context || {}, _in = d.input || {};
  const _saturated = _v['$/mw_band_status'] === 'ceiling_saturated';
  html += `
    <div class="methcard" id="methcard">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap">
        <h3 style="margin:0">Indicative Valuation — Methodology &amp; Caveats</h3>
        <button onclick="dcExportOnePager()" class="printbtn no-print">⬇ Owner one-pager (PDF)</button>
        <button onclick="dcExportTwoTier(this)" class="printbtn no-print">⬇ Two-tier: pre-dev vs entitled</button>
        <button onclick="window.print()" class="printbtn no-print" style="background:transparent;color:var(--accent2);border:1px solid var(--border)">🖨 Print page</button>
      </div>
      <p style="font-size:13px;color:var(--muted);margin:8px 0 12px">
        This is a <b>model-based indicative valuation</b> for screening and negotiation — <b>not a certified (USPAP) appraisal</b>.
        Use the <b>range</b>, not a single point.${_saturated ? ' The midpoint sits at a modeled ceiling — treat it as an upper anchor.' : ''}
      </p>
      <div style="font-size:24px;font-weight:700">${fmtM$(_v.site_value_usd_low)} – ${fmtM$(_v.site_value_usd_high)}</div>
      <div style="font-size:12px;color:var(--muted);margin:2px 0 16px">midpoint ${fmtM$(_v.site_value_usd_mid)} · ±50% envelope · ${_in.target_mw || ''} MW · ${_mc.site_state || ''} · verdict ${(d.dcpi_context && d.dcpi_context.verdict) || ''} · best path: ${(typeof bestName !== 'undefined' ? bestName : '').replace(/_/g, ' ')}</div>
      <div class="methgrid">
        <div>
          <div class="methh">Grounded in real data</div>
          <ul class="methul">
            <li>Site state + geography — FCC geocode (${_mc.site_state || ''})</li>
            <li>Power cost — EIA per-state${_mc.power_cost_usd_mwh ? (' ($' + _mc.power_cost_usd_mwh + '/MWh, large-load)') : ''}</li>
            <li>Gas — per-hub basis model</li>
            <li>Market verdict — nearest DCPI market${_mc.miles_from_centroid ? (' (' + Math.round(_mc.miles_from_centroid) + ' mi)') : ''}</li>
            <li>Tax incentive — state record</li>
            <li>3-scenario NPV — exact discounted math</li>
          </ul>
        </div>
        <div>
          <div class="methh">Model assumptions — confirm before relying</div>
          <ul class="methul">
            <li><b>No land comps</b> — base $/MW is model-derived (DCPI demand), not comp-anchored.</li>
            <li>The entitled ceiling ($1.2M/MW) is an <b>assumed cap</b> for zoned+permitted sites, not an observed price.</li>
            <li>LCOE = energy + amortized capex at 100% CF; <b>excludes O&amp;M</b> (~$3–5/MWh) — an energy floor. CapEx is screening-grade.</li>
            <li>Tax abatement uses the <b>state record</b> — confirm the parcel's actual term.</li>
            <li>Land sufficiency assumes ${_in.stories || 1} building stor${(_in.stories || 1) == 1 ? 'y' : 'ies'}.</li>
          </ul>
        </div>
      </div>
      <p style="font-size:11.5px;color:var(--muted);margin:12px 0 0;border-top:1px solid var(--border);padding-top:10px">
        <b>How to use:</b> a screening range + negotiating anchor. For a term-sheet number, corroborate with 1–2 broker/appraiser comparables.
        &nbsp;·&nbsp; Source: DC Hub · Engine ${d.engine_version || 'v2.2'} · ${(d.as_of || '').slice(0, 10)} · DCPI, EIA, tax-incentives, deals (M&A context).
      </p>
    </div>`;

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


# ── PRO+ paywall hero (free / starter visitors see this in place of the form) ─

# Why server-side render instead of client-toggle:
#   We want the gate to be unambiguous on first paint — search engines,
#   click-tracked CTAs, and the user's *visible* upgrade signal. The full
#   tool HTML still loads underneath (hidden) so a paid visitor pasting a
#   PRO key on the form still works without a round-trip.

_PRO_HERO_BANNER = """
<div style="background:linear-gradient(135deg,#0EA5E9 0%,#0284C7 60%,#0369A1 100%);
            border-radius:16px;padding:32px 28px;margin:0 0 28px;
            box-shadow:0 10px 40px rgba(14,165,233,0.35);position:relative;overflow:hidden;">
  <div style="display:inline-block;background:rgba(0,0,0,0.35);color:#fff;
              font-size:11px;font-weight:800;letter-spacing:0.18em;
              padding:6px 12px;border-radius:999px;text-transform:uppercase;
              margin-bottom:14px;">
    🔒 &nbsp; PRO + DEVELOPER + ENTERPRISE ONLY
  </div>
  <h2 style="margin:0 0 8px;color:#fff;font-size:30px;font-weight:800;letter-spacing:-0.01em;">
    Site Valuation Engine is a premium tool
  </h2>
  <p style="margin:0 0 20px;color:rgba(255,255,255,0.92);font-size:16px;line-height:1.5;max-width:760px;">
    Unlock the full 3-scenario NPV engine, comparable-sale envelope, and
    DCPI verdict-weighted valuation for any US site. Free + Starter
    visitors get a teaser midpoint only.
  </p>
  <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
    <a href="/pricing" style="background:#fff;color:#0369A1;font-weight:700;
       padding:14px 28px;border-radius:8px;text-decoration:none;font-size:15px;
       box-shadow:0 4px 12px rgba(0,0,0,0.2);">
      Unlock with PRO — $199/mo &nbsp;→
    </a>
    <a href="/pricing#enterprise" style="color:#fff;font-weight:600;
       padding:14px 4px;text-decoration:underline;font-size:14px;">
      See Enterprise tiers
    </a>
    <span style="color:rgba(255,255,255,0.75);font-size:12px;margin-left:6px;">
      Already a subscriber? Paste your API key on the form below.
    </span>
  </div>
</div>
"""

# Minimal PRO+ confirmation banner (so paid users see something too)
_PRO_OK_BANNER = """
<div style="background:rgba(16,185,129,0.08);border:1px solid #10b981;
            border-radius:12px;padding:14px 18px;margin:0 0 24px;
            color:#10b981;font-size:13px;font-weight:600;
            display:flex;align-items:center;gap:10px;">
  <span style="font-size:16px">✓</span>
  <span>PRO access confirmed — full valuation envelope, NPV envelope, and comparable sales unlocked.</span>
</div>
"""


@site_valuation_engine_bp.route("/sites/value", methods=["GET"],
                                 strict_slashes=False)
def site_value_page():
    """Render the Site Valuation Engine.

    Tier-aware: free / starter visitors see a prominent PRO+ paywall
    banner at the top of the page; PRO+ get a green confirmation
    banner. The form stays accessible in both cases so a subscriber
    can paste an API key and run live valuations.
    """
    try:
        tier = _resolve_tier()
    except Exception:
        tier = "FREE"
    is_pro_plus = tier in ("PRO", "DEVELOPER", "ENTERPRISE")
    banner = _PRO_OK_BANNER if is_pro_plus else _PRO_HERO_BANNER

    # Inject the banner directly after the opening <div class="wrap"> so it
    # is the first thing the visitor sees, above the kicker + form.
    body = _PAGE_HTML.replace(
        '<div class="wrap">',
        '<div class="wrap">\n  ' + banner.strip(),
        1,
    )
    # Stamp the tier into a hidden meta tag for client-side telemetry +
    # quick diagnostics (curl -s ... | grep dc-tier).
    body = body.replace(
        '<title>',
        f'<meta name="dc-tier" content="{tier}">\n<title>',
        1,
    )

    resp = Response(body, content_type="text/html; charset=utf-8")
    # Vary on tier-determining headers so CF doesn't serve a free
    # visitor's banner to a PRO user (and vice versa).
    resp.headers["Cache-Control"] = "private, no-store, max-age=0"
    resp.headers["Vary"] = "Cookie, Authorization, X-API-Key"
    resp.headers["X-DC-Tier"] = tier
    return resp
