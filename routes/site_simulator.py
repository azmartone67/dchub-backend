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

import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras

from util.db_honesty import close_quietly, open_conn, try_fetchone, unpoison
from util.us_states import state_match_pair
from util.water_risk import STRESSED_BAND, read_state_stress


site_simulator_bp = Blueprint("site_simulator", __name__)


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


def _fmt_err(e) -> str:
    """util.db_honesty's error shape, for failures raised outside try_fetch*."""
    return f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"


def _pull_signals(state: str) -> dict:
    """Pull the upstream signals the model needs.

    Every read is INDEPENDENT. One that fails leaves its signal null and names
    itself in `read_errors`; it cannot blank the reads that follow it, and it
    never degrades to a number nobody measured.

    ★ THE BUG THIS SHAPE REPLACES, measured live 2026-09-21.
    This function used `with _conn() as c`. psycopg2's connection context
    manager is a TRANSACTION manager, not a closer, so entering it opened an
    explicit transaction that `autocommit` does not override. The water read
    named `usgs_water_stress.stress_index` — a column that has never existed —
    and every read sat in a bare `except Exception: pass` with NO rollback. So
    the water failure aborted the transaction and each LATER read on that
    connection died of InFailedSqlTransaction: the DCPI verdict and the tax
    offset went dark behind a valid-looking HTTP 200. VA, TX and OH all served
    retail_rate_cents_kwh, water_stress_index and dcpi_verdict as null with
    tax_pct_offset 0.0, under a methodology string that claimed the verdict,
    water stress and retail rate were "pulled live". See util/db_honesty (#2071).
    """
    sig = {
        "retail_rate_cents_kwh":   None,   # ¢/kWh industrial
        "water_stress_score":      None,   # 0-100 WRI Aqueduct, 100 = most stressed
        "water_stress_index":      None,   # 1-5 band derived from that score
        "dcpi_verdict":            None,
        "dcpi_excess":             None,
        "dcpi_constraint":         None,
        "time_to_power_months":    None,
        # ★ None means "not read". 0.0 is reserved for a state we DID read and
        # that genuinely offers no offset — a consumer can branch on null, it
        # cannot detect a failure told as a zero.
        "tax_pct_offset":          None,
        "tax_summary":             "",
        "tax_status":              None,   # registry status, e.g. paused_new_applicants
        "read_errors":             {},
    }
    errs = sig["read_errors"]
    abbr, full = state_match_pair(state)

    c = None
    try:
        c = open_conn()
    except Exception as e:
        errs["connection"] = _fmt_err(e)
        return sig

    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── Retail rate (industrial) ──────────────────────────────────
            # ★ eia_retail_rates.state holds FULL names ("Virginia"), and the
            # table also carries census-region rows ("East North Central"), so
            # match BOTH spellings. `UPPER(state) = 'VA'` returned 0 rows on
            # 2026-09-21 while 'VIRGINIA' returned 10 — a clean query, an empty
            # result, and a published null that read as "no data for VA".
            row, err = try_fetchone(cur, """
                SELECT rate_cents_kwh
                  FROM eia_retail_rates
                 WHERE LOWER(sector) = 'industrial'
                   AND UPPER(state) IN (%s, %s)
                 ORDER BY period DESC
                 LIMIT 1
            """, (abbr, full))
            if err:
                errs["retail_rate"] = err
            elif row and row.get("rate_cents_kwh") is not None:
                sig["retail_rate_cents_kwh"] = float(row["rate_cents_kwh"])

            # ── Water stress ──────────────────────────────────────────────
            # ★ NOT usgs_water_stress. That table has no stress column at all
            # (site_id, site_name, latitude, longitude, state, county,
            # aquifer_name, well_depth_ft, water_level_ft, water_level_date,
            # site_type), it covers 16 states, and its water_level_ft
            # groundwater proxy is the one withdrawn on 2026-07-07 for reading
            # INVERTED — routes/interconnection_queues.py still refuses to
            # score off it. water_risk carries the verified WRI Aqueduct
            # roll-up, whose ingest asserts arid states out-score wet ones
            # before it will write a row.
            # util.water_risk is the ONE read path for this table: the live
            # schema, the 0-100 scale and the 1-5 banding are stated once
            # there instead of being re-learned per surface. It returns
            # (value, error) and never a bare fallback, so a failure is named
            # here rather than published as an indistinguishable null — and
            # it hands back the band, so this route needs no band import.
            water, err = read_state_stress(cur, abbr)
            if err:
                errs["water_stress"] = err
            elif water is not None:
                sig["water_stress_score"] = water["score"]
                sig["water_stress_index"] = water["band"]

            # ── DCPI (best-match market for the state — highest excess) ───
            row, err = try_fetchone(cur, """
                SELECT verdict, excess_power_score, constraint_score,
                       time_to_power_months
                  FROM market_power_scores
                 WHERE UPPER(state) = %s
                   AND published = true
                 ORDER BY computed_at DESC, excess_power_score DESC NULLS LAST
                 LIMIT 1
            """, (abbr,))
            if err:
                errs["dcpi"] = err
            elif row:
                sig["dcpi_verdict"]         = row.get("verdict")
                sig["dcpi_excess"]          = _safe_float(row.get("excess_power_score"), None)
                sig["dcpi_constraint"]      = _safe_float(row.get("constraint_score"), None)
                sig["time_to_power_months"] = _safe_float(row.get("time_to_power_months"), None)

            # ── Tax incentives → a coarse capex-offset percentage ─────────
            # util.tax_incentives is the ONE read path (#5146): a verified
            # registry row supersedes the frozen tax_incentives_neon snapshot,
            # and a program closed to new applicants prices at nothing.
            # It rolls the connection back itself before re-raising, but
            # unpoison() here keeps that true if it ever stops.
            try:
                from util.tax_incentives import state_incentive
                rec = state_incentive(cur, abbr)
            except Exception as e:
                unpoison(cur)
                errs["tax"] = _fmt_err(e)
                rec = None
            if rec is not None:
                sig["tax_status"] = rec.get("status")
                offset = 0.0
                if rec.get("sales_tax_exempt"):       offset += 0.05  # ~5% capex
                if rec.get("property_tax_abatement"): offset += 0.08  # ~8% over horizon
                if rec.get("data_center_specific"):   offset += 0.03  # bonus
                sig["tax_pct_offset"] = min(0.20, offset)
                sig["tax_summary"] = (rec.get("incentive_details") or "")[:240]
            elif "tax" not in errs:
                # Neither store knows the state: a measured absence, so 0.0 is
                # an honest answer here rather than a swallowed failure.
                sig["tax_pct_offset"] = 0.0
    except Exception as e:
        errs.setdefault("cursor", _fmt_err(e))
    finally:
        close_quietly(c)
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
    if sig.get("water_stress_index") and sig["water_stress_index"] >= STRESSED_BAND:
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


def _methodology(sig: dict, rate_measured) -> str:
    """The methodology string, reporting what THIS call actually read.

    ★ The old string asserted "DCPI verdict + water_stress + retail rate
    pulled live" unconditionally — including on the responses where all three
    were null because a dead column had aborted the transaction. A methodology
    that cannot be falsified by its own response is not a methodology.
    """
    text = ("Capex/opex bands grounded in $8-12M/MW greenfield + "
            "$0.6-1.3M/MW/yr ex-power industry ranges. Redundancy mult "
            "1.0/1.15/1.6/1.8 for N/N+1/2N/2N+1. Power = capacity × "
            "8760 × 0.55 utilization × 1.30 PUE × ¢/kWh. Tax offset "
            "from the state incentive record (sales 5% + property 8% + "
            "DC-bonus 3%, capped 20%; a program paused or repealed for "
            "new applicants counts 0). Water stress is WRI Aqueduct baseline "
            "via water_risk — water_stress_score is 0-100 (100 = most "
            "stressed) and water_stress_index bands it 1-5. DCPI verdict, "
            "water stress and the retail rate are read live per request, each "
            "independently: a read that fails leaves its signal null and is "
            "named in signals.read_errors, never replaced by a number. "
            "Sensitivity walks each input ±20%.")
    if rate_measured is None:
        text += (" No industrial retail rate was read for this state, so the "
                 "cost model used the 7.5¢/kWh national fallback and "
                 "signals.retail_rate_cents_kwh is null.")
    if sig.get("tax_pct_offset") is None:
        text += (" The tax incentive record could not be read, so capex is "
                 "priced at no offset and signals.tax_pct_offset is null.")
    if sig.get("read_errors"):
        text += (" Failed reads this call: %s."
                 % ", ".join(sorted(sig["read_errors"])))
    return text


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

    # Fallback rate if no EIA data for this state — national industrial avg.
    # The fallback drives the cost model but is NOT written back into signals:
    # retail_rate_cents_kwh stays null so the response cannot pass 7.5 off as
    # a reading, and the methodology below says the substitution happened.
    rate_measured = sig.get("retail_rate_cents_kwh")
    rate = rate_measured if rate_measured is not None else 7.5

    # Likewise for the offset: null means we could not read it, and the model
    # prices such a site at no incentive rather than inventing one.
    tax_offset = sig["tax_pct_offset"] or 0.0

    envelope = _envelope(capacity_mw, redundancy_mult, duration_years,
                          rate, tax_offset)
    sens     = _sensitivity(capacity_mw, redundancy_mult, duration_years,
                             rate, tax_offset)
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
    if sig["tax_pct_offset"]:
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
        methodology=_methodology(sig, rate_measured),
    ), 200
