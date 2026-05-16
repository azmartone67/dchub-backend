"""Phase TT (2026-05-15) — site_simulator.

GET /api/v1/site/simulate-buildout — estimates the full 10-year
build-and-operate envelope for a data center at a specific site:
capex, opex, time-to-power, water/grid/permitting risk, tax incentive
offsets, TCO, sensitivity drivers, and a recommendation paragraph.

The tool aggregates several upstream signals (grid headroom + water
risk + retail rates + tax incentives + DCPI verdict) into a single
decision-grade envelope. Returns ranges (low/mid/high) rather than
single numbers — site economics are uncertain by definition.

Powers the MCP tool `simulate_buildout`.

Industry-grounded defaults for the cost model:
    Capex per MW (greenfield turnkey): $8M (low) / $10M (mid) / $12M (high)
    Opex per MW per year (ex-power):   $0.6M (low) / $0.9M (mid) / $1.3M (high)
    PUE assumption:                    1.30 (modern hyperscale)
    Utilization assumption:            55% (24×7 at 55% of nameplate)
    Redundancy multipliers:            N=1.0, N+1=1.15, 2N=1.6, 2N+1=1.8

Sensitivity is calculated by walking each input ±20% and showing the TCO
delta. The top 3 sensitivities are surfaced as `sensitivity_drivers`.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras


site_simulator_bp = Blueprint("site_simulator", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db, sslmode="require", connect_timeout=8)


_REDUNDANCY_MULT = {
    "N":     1.00,
    "N+1":   1.15,
    "2N":    1.60,
    "2N+1":  1.80,
}

_CAPEX_PER_MW_USD = {"low": 8_000_000, "mid": 10_000_000, "high": 12_000_000}
_OPEX_EX_POWER_PER_MW_USD = {"low": 600_000, "mid": 900_000, "high": 1_300_000}

# Conversion constants for the power-cost line item
_HOURS_PER_YEAR    = 8760
_DEFAULT_PUE       = 1.30
_DEFAULT_UTILIZATION = 0.55


def _safe_float(v, default):
    try: return float(v)
    except (TypeError, ValueError): return default


def _pull_signals(state: str):
    """Pull the upstream signals we need for the model. Each block is
    wrapped — a missing table just degrades that input to a neutral default
    rather than failing the whole simulation."""
    sig = {
        "retail_rate_cents_kwh":   None,   # ¢/kWh industrial
        "water_stress_index":      None,   # 1-5 USGS
        "dcpi_verdict":            None,
        "dcpi_excess":             None,
        "dcpi_constraint":         None,
        "time_to_power_months":    None,
        "tax_pct_offset":          0.0,    # 0..0.20 typical
        "tax_summary":             "",
    }
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Retail rate (industrial)
            try:
                cur.execute("""
                    SELECT DISTINCT ON (UPPER(state)) rate_cents_kwh
                      FROM eia_retail_rates
                     WHERE LOWER(sector) = 'industrial'
                       AND UPPER(state) = %s
                     ORDER BY UPPER(state), period DESC
                """, (state.upper(),))
                r = cur.fetchone()
                if r and r.get("rate_cents_kwh") is not None:
                    sig["retail_rate_cents_kwh"] = float(r["rate_cents_kwh"])
            except Exception:
                pass
            # Water stress
            try:
                cur.execute("""
                    SELECT AVG(stress_index) AS s FROM usgs_water_stress
                     WHERE UPPER(state) = %s
                """, (state.upper(),))
                r = cur.fetchone()
                if r and r.get("s") is not None:
                    sig["water_stress_index"] = float(r["s"])
            except Exception:
                pass
            # DCPI (best-match market for the state — highest excess)
            try:
                cur.execute("""
                    SELECT verdict, excess_power_score, constraint_score,
                           time_to_power_months
                      FROM market_power_scores
                     WHERE UPPER(state) = %s
                       AND published = true
                     ORDER BY computed_at DESC, excess_power_score DESC NULLS LAST
                     LIMIT 1
                """, (state.upper(),))
                r = cur.fetchone()
                if r:
                    sig["dcpi_verdict"]          = r.get("verdict")
                    sig["dcpi_excess"]           = _safe_float(r.get("excess_power_score"), None)
                    sig["dcpi_constraint"]       = _safe_float(r.get("constraint_score"), None)
                    sig["time_to_power_months"]  = _safe_float(r.get("time_to_power_months"), None)
            except Exception:
                pass
            # Tax incentives → derive a coarse capex-offset percentage
            try:
                cur.execute("""
                    SELECT sales_tax_exempt, property_tax_abatement,
                           data_center_specific, incentive_details
                      FROM tax_incentives_neon
                     WHERE state_abbr = %s LIMIT 1
                """, (state.upper(),))
                r = cur.fetchone()
                if r:
                    offset = 0.0
                    if r.get("sales_tax_exempt"):      offset += 0.05  # ~5% capex
                    if r.get("property_tax_abatement"): offset += 0.08  # ~8% over horizon
                    if r.get("data_center_specific"):   offset += 0.03  # bonus
                    sig["tax_pct_offset"] = min(0.20, offset)
                    sig["tax_summary"] = (r.get("incentive_details") or "")[:240]
            except Exception:
                pass
    except Exception:
        # DB unavailable — return neutral signals.
        pass
    return sig


def _envelope(capacity_mw: float, redundancy_mult: float,
              duration_years: int, retail_rate_cents_kwh: float,
              tax_pct_offset: float) -> dict:
    """Return {low, mid, high} for capex, opex_per_year, power_cost_per_year, tco."""
    out = {}
    rate_dollars_kwh = retail_rate_cents_kwh / 100.0

    # Annual power consumption (MWh) → kWh → $
    annual_mwh = capacity_mw * _HOURS_PER_YEAR * _DEFAULT_UTILIZATION * _DEFAULT_PUE
    power_cost_per_yr = annual_mwh * 1000 * rate_dollars_kwh

    for band in ("low", "mid", "high"):
        capex = (_CAPEX_PER_MW_USD[band] * capacity_mw * redundancy_mult
                 * (1 - tax_pct_offset))
        opex_ex_power = _OPEX_EX_POWER_PER_MW_USD[band] * capacity_mw
        opex_total_per_yr = opex_ex_power + power_cost_per_yr
        tco = capex + opex_total_per_yr * duration_years
        out[band] = {
            "capex_usd_m":              round(capex / 1_000_000, 1),
            "opex_per_year_usd_m":      round(opex_total_per_yr / 1_000_000, 1),
            "power_cost_per_year_usd_m": round(power_cost_per_yr / 1_000_000, 1),
            "tco_usd_m":                round(tco / 1_000_000, 1),
        }
    out["assumptions"] = {
        "pue":                _DEFAULT_PUE,
        "utilization":        _DEFAULT_UTILIZATION,
        "annual_mwh":         round(annual_mwh),
        "retail_rate_¢/kWh":  retail_rate_cents_kwh,
        "tax_pct_offset":     tax_pct_offset,
        "redundancy_mult":    redundancy_mult,
        "duration_years":     duration_years,
    }
    return out


def _sensitivity(capacity_mw: float, redundancy_mult: float,
                 duration_years: int, retail_rate_cents_kwh: float,
                 tax_pct_offset: float) -> list[dict]:
    """Walk each input ±20%, re-compute mid TCO, return ranked deltas."""
    base = _envelope(capacity_mw, redundancy_mult, duration_years,
                     retail_rate_cents_kwh, tax_pct_offset)["mid"]["tco_usd_m"]
    drivers = []
    perturbations = [
        ("retail_rate",   retail_rate_cents_kwh,  lambda x: _envelope(capacity_mw, redundancy_mult, duration_years, x, tax_pct_offset)["mid"]["tco_usd_m"]),
        ("tax_offset",    tax_pct_offset,         lambda x: _envelope(capacity_mw, redundancy_mult, duration_years, retail_rate_cents_kwh, x)["mid"]["tco_usd_m"]),
        ("capacity_mw",   capacity_mw,            lambda x: _envelope(x, redundancy_mult, duration_years, retail_rate_cents_kwh, tax_pct_offset)["mid"]["tco_usd_m"]),
        ("redundancy",    redundancy_mult,        lambda x: _envelope(capacity_mw, x, duration_years, retail_rate_cents_kwh, tax_pct_offset)["mid"]["tco_usd_m"]),
        ("duration_yrs",  duration_years,         lambda x: _envelope(capacity_mw, redundancy_mult, int(x), retail_rate_cents_kwh, tax_pct_offset)["mid"]["tco_usd_m"]),
    ]
    for label, value, fn in perturbations:
        try:
            up = fn(value * 1.2)
            down = fn(value * 0.8 if value != 0 else value)
            span = abs(up - down)
            drivers.append({
                "input":         label,
                "base_value":    round(value, 3),
                "tco_at_+20pct": up,
                "tco_at_-20pct": down,
                "tco_span_usd_m": round(span, 1),
            })
        except Exception:
            continue
    drivers.sort(key=lambda d: -d["tco_span_usd_m"])
    return drivers


def _risk_flags(sig: dict, capacity_mw: float) -> list[str]:
    flags = []
    if sig.get("water_stress_index") and sig["water_stress_index"] >= 4:
        flags.append("high_water_stress")
    if sig.get("retail_rate_cents_kwh") and sig["retail_rate_cents_kwh"] > 9:
        flags.append("high_power_cost")
    if sig.get("time_to_power_months") and sig["time_to_power_months"] > 36:
        flags.append("slow_time_to_power")
    if sig.get("dcpi_verdict") == "AVOID":
        flags.append("dcpi_avoid_market")
    if capacity_mw >= 200 and sig.get("dcpi_constraint") and sig["dcpi_constraint"] >= 60:
        flags.append("constraint_too_tight_for_hyperscale")
    return flags


@site_simulator_bp.route("/api/v1/site/simulate-buildout", methods=["GET", "OPTIONS"])
def simulate_buildout():
    if request.method == "OPTIONS":
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-Key,Authorization"
        return resp, 200

    lat   = _safe_float(request.args.get("lat"), 0.0)
    lon   = _safe_float(request.args.get("lon"), 0.0)
    state = (request.args.get("state") or "").upper().strip()
    if not state:
        return jsonify(error="state required (US 2-letter code)"), 400
    capacity_mw    = max(1.0, _safe_float(request.args.get("capacity_mw"), 50.0))
    redundancy     = (request.args.get("redundancy") or "N+1").upper().strip()
    duration_years = int(_safe_float(request.args.get("duration_years"), 10))
    duration_years = max(1, min(30, duration_years))

    if redundancy not in _REDUNDANCY_MULT:
        return jsonify(error="redundancy must be N, N+1, 2N, or 2N+1"), 400
    redundancy_mult = _REDUNDANCY_MULT[redundancy]

    sig = _pull_signals(state)

    # Fallback rate if no EIA data for this state — national industrial avg
    rate = sig.get("retail_rate_cents_kwh") or 7.5

    envelope = _envelope(capacity_mw, redundancy_mult, duration_years,
                          rate, sig["tax_pct_offset"])
    sens     = _sensitivity(capacity_mw, redundancy_mult, duration_years,
                             rate, sig["tax_pct_offset"])
    flags    = _risk_flags(sig, capacity_mw)

    # Recommendation paragraph
    bits = [
        f"At {capacity_mw:.0f} MW with {redundancy} redundancy in {state}, "
        f"the {duration_years}-yr TCO envelope is ${envelope['low']['tco_usd_m']}M (low) "
        f"to ${envelope['high']['tco_usd_m']}M (high), mid ${envelope['mid']['tco_usd_m']}M.",
        f"Power cost alone: ${envelope['mid']['power_cost_per_year_usd_m']}M/yr at "
        f"{rate:.1f}¢/kWh industrial.",
    ]
    if sig.get("dcpi_verdict"):
        bits.append(f"DCPI verdict for {state}: {sig['dcpi_verdict']} "
                    f"(excess {sig.get('dcpi_excess')}, constraint {sig.get('dcpi_constraint')}).")
    if sig.get("time_to_power_months"):
        bits.append(f"Best-case time-to-power: ~{int(sig['time_to_power_months'])} months.")
    if sig["tax_pct_offset"] > 0:
        bits.append(f"Tax incentives offset ~{int(sig['tax_pct_offset']*100)}% of capex.")
    if flags:
        bits.append("Risk flags: " + ", ".join(flags) + ".")
    if sens:
        top_driver = sens[0]
        bits.append(f"Top sensitivity: {top_driver['input']} "
                    f"(±20% swings TCO by ${top_driver['tco_span_usd_m']}M).")

    return jsonify(
        site={"lat": lat, "lon": lon, "state": state,
              "capacity_mw": capacity_mw, "redundancy": redundancy,
              "duration_years": duration_years},
        envelope=envelope,
        signals=sig,
        risk_flags=flags,
        sensitivity_drivers=sens,
        recommendation=" ".join(bits),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        methodology=("Capex/opex bands grounded in $8-12M/MW greenfield + "
                     "$0.6-1.3M/MW/yr ex-power industry ranges. Redundancy mult "
                     "1.0/1.15/1.6/1.8 for N/N+1/2N/2N+1. Power = capacity × "
                     "8760 × 0.55 utilization × 1.30 PUE × ¢/kWh. Tax offset "
                     "from tax_incentives_neon (sales 5% + property 8% + DC-bonus "
                     "3%, capped 20%). DCPI verdict + water_stress + retail rate "
                     "pulled live. Sensitivity walks each input ±20%."),
    ), 200
