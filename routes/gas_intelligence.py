"""gas_intelligence.py — get_gas_intelligence synthesizer. 2026-06-24.
=====================================================================

The GAS analogue of get_grid_intelligence. Where _grid_intel_fetch fuses 3
electricity feeds (EIA demand + EIA fuel mix + grid-headroom) into one ISO
brief, this fuses the FOUR gas feeds we already publish into one per-STATE
behind-the-meter-vs-grid brief:

  1. DCGI (Data Center Gas Index)  — routes.dcgi._gas_state_rollup()
       per-state gas-access score, interstate/total pipeline counts, operators,
       composite dcgi + GAS-ADVANTAGED/ADEQUATE/GAS-CONSTRAINED verdict.
  2. get_gas_economics (Powered Land) — routes.powered_land_gas helpers
       live Henry Hub spot, regional basis (SYNTHETIC — labeled), and the 5
       heat-rate gas-to-grid $/MWh scenarios.
  3. gas_pipelines table — routes.dcgi._operator_rollup()
       parent-midstream rollup + interstate flag. PRESENCE / PROXIMITY only —
       NOT throughput or firm capacity (the EIA geofeed has no capacity_mcf).
  4. live grid gas share — main._grid_intel_cached()
       natural-gas generation share for the state's primary ISO, computed from
       the EIA fuel-type mix (NG mw / non-storage total).

HONESTY (DC Hub rule #1 — never fabricate):
  - The COST half is THIN. eia_gas_prices has no delivered-tariff coverage for
    most states, so delivered_price_usd_mmbtu is surfaced as NULL with a note —
    NEVER a fabricated delivered price. The gas-to-grid $/MWh scenarios fall
    back to Henry-Hub-plus-basis when no delivered tariff exists, and that
    fallback is labeled in data_basis.
  - basis_usd_mmbtu is SYNTHETIC (Phase-1 seed) — labeled 'synthetic'.
  - We DO NOT invent storage levels, LNG inventory, or firm pipeline capacity.
    Those are omitted (not nulled-with-a-fake-number).

Tier gating (mirrors get_grid_intelligence / phase19b): anonymous external
callers get a TEASER (region + dcgi_score + dcgi_verdict + henry_hub only) plus
a claim_free_key agent_action; identified+ (any valid X-API-Key) gets the full
fused brief. Internal callers (dchub-* UA / Railway egress) always get full.

Route:
  GET /api/v1/gas/intelligence/<region>   region = US state code or name
                                          (TX, Texas, "new york", NY, ...)
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

# Both withdrawn products meet in this one payload — see util/gas_index.py.
from util.gas_index import (
    gas_index_enabled, gas_to_grid_enabled,
    gas_index_unavailable, gas_to_grid_unavailable,
    strip_index_fields, GAS_INDEX_FIELDS, GAS_TO_GRID_FIELDS,
)

gas_intelligence_bp = Blueprint("gas_intelligence", __name__)


# ── State-name / code normalization ──────────────────────────────────
# Reuse the canonical code→name map already maintained in routes.dcgi so we
# accept "TX", "Texas", "new york" etc. without duplicating a second table.
def _resolve_state_code(region: str):
    """Return a 2-letter uppercase US state code from a code OR full name.
    Returns '' when unresolvable."""
    if not region:
        return ""
    r = region.strip()
    ru = r.upper()
    try:
        from routes.dcgi import _US_STATE_NAMES  # {code: "Full Name"}
    except Exception:
        _US_STATE_NAMES = {}
    # Direct 2-letter code (incl. DC).
    if len(ru) == 2 and ru in _US_STATE_NAMES:
        return ru
    # Full name (case-insensitive) → code.
    rl = r.lower()
    for code, name in _US_STATE_NAMES.items():
        if name.lower() == rl:
            return code
    # Tolerant: hyphen/underscore separated full name ("new-york").
    rl2 = rl.replace("-", " ").replace("_", " ").strip()
    for code, name in _US_STATE_NAMES.items():
        if name.lower() == rl2:
            return code
    # Last resort: a bare 2-letter token even if not in the name map
    # (covers territories PR/GU/VI which DCGI seeds but aren't in _US_STATE_NAMES).
    if len(ru) == 2 and ru.isalpha():
        return ru
    return ""


# EIA RTO respondent codes for the ISO/region the grid-intel feed understands.
# Mirrors main.EIA_RTO_MAP for the ISOs _state_to_iso can emit. Only the ISOs
# with a live EIA-930 respondent yield a gas share; the rest (TVA/SOCO/FRCC/
# WECC are aggregate interconnects, not single EIA-930 RTO respondents) are
# surfaced honestly as 'unavailable' rather than faked.
_ISO_TO_EIA_RTO = {
    "PJM": "PJM", "MISO": "MISO", "ERCOT": "ERCO", "CAISO": "CISO",
    "NYISO": "NYIS", "SPP": "SWPP", "ISONE": "ISNE",
}

# Storage fuel codes excluded from the generation denominator (mirrors the
# main._grid_intel_fetch convention: battery + pumped-storage are not
# generation, so counting them would understate gas share).
_STORAGE_FUELS = {"BAT", "PS"}


def _live_grid_gas_share(state_code: str):
    """Live natural-gas generation share (%) for the state's primary ISO.
    Computed from the EIA fuel-type mix already assembled + cached by the
    grid-intelligence feed (no new upstream call when warm). Returns a dict
    describing source + confidence; share_pct is None when unavailable —
    NEVER fabricated."""
    out = {
        "share_pct": None,
        "iso": None,
        "data_basis": "unavailable",
        "source": None,
        "note": None,
    }
    try:
        from routes.dcpi import _state_to_iso
    except Exception:
        _state_to_iso = None
    iso = (_state_to_iso(state_code) if _state_to_iso else "") or ""
    out["iso"] = iso or None
    rto = _ISO_TO_EIA_RTO.get(iso)
    if not rto:
        out["note"] = (
            f"{state_code}'s primary grid ({iso or 'unknown'}) is not a single "
            "EIA-930 RTO respondent with a live fuel-type breakdown, so a live "
            "gas-generation share is not available for it. Not estimated."
        )
        return out
    try:
        from main import _grid_intel_cached
    except Exception as e:
        out["note"] = "grid feed unavailable: " + type(e).__name__
        return out
    try:
        grid = _grid_intel_cached(iso, rto) or {}
    except Exception as e:
        out["note"] = "grid fetch error: " + type(e).__name__
        return out
    mix = grid.get("generation_mix") or {}
    if not mix:
        out["note"] = (grid.get("eia_genmix_error")
                       or "EIA fuel-type mix temporarily unavailable for " + iso)
        return out
    total = 0.0
    ng = 0.0
    for fuel, rec in mix.items():
        if str(fuel).upper() in _STORAGE_FUELS:
            continue
        mw = (rec or {}).get("mw") if isinstance(rec, dict) else None
        try:
            mw = float(mw)
        except (TypeError, ValueError):
            continue
        if mw <= 0:
            continue
        total += mw
        if str(fuel).upper() == "NG":
            ng += mw
    if total <= 0:
        out["note"] = "EIA fuel-type mix had no usable generation for " + iso
        return out
    out["share_pct"] = round(ng / total * 100.0, 1)
    out["data_basis"] = "live"
    out["source"] = "EIA-930 hourly fuel-type mix via api.eia.gov (NG / non-storage total)"
    out["mix_period"] = grid.get("generation_mix_period")
    # ★2026-08-05: mix_stale_hours now carries TRUE age (wall clock − mix period).
    # It used to mirror the feed-to-feed lag inside the grid snapshot, which
    # excluded the snapshot's own age and so under-reported by >2x — see the
    # FRESHNESS DIVERGENCE block in main.py. The old quantity is still available
    # as mix_lag_at_snapshot_hours for anyone who genuinely wants the feed skew.
    _stale = grid.get("generation_mix_stale_hours")
    if _stale is not None:
        out["mix_stale_hours"] = _stale
        out["mix_age_hours"] = grid.get("generation_mix_age_hours")
        _lag = grid.get("generation_mix_lag_at_snapshot_hours")
        if _lag is not None:
            out["mix_lag_at_snapshot_hours"] = _lag
        if grid.get("generation_mix_note"):
            out["note"] = grid["generation_mix_note"]
    return out


def _build_gas_intel(state_code: str) -> dict:
    """Fuse the four feeds for one US state. Always returns a complete dict;
    thin/missing layers are surfaced as null + a data_basis label, never faked."""
    sc = (state_code or "").upper()

    # ── Layer 1: DCGI (gas access score + verdict + pipeline counts) ──────
    dcgi_row, dcgi_err = {}, None
    try:
        from routes.dcgi import _gas_state_rollup
        states, dcgi_err = _gas_state_rollup()
        dcgi_row = (states or {}).get(sc) or {}
    except Exception as e:
        dcgi_err = "dcgi_rollup_error: " + str(e)[:160]

    # ── Layer 3: parent-midstream operator rollup (gas_pipelines) ─────────
    # Filter the national midstream rollup to the parents present IN this state
    # by re-matching their alias substrings against this state's pipeline rows.
    midstreams = []
    try:
        midstreams = _state_midstreams(sc)
    except Exception:
        midstreams = []

    # ── Layer 2: gas economics (Henry Hub live + basis synthetic + $/MWh) ─
    econ = _gas_economics_for_state(sc)

    # ── Layer 4: live grid gas share ─────────────────────────────────────
    gas_share = _live_grid_gas_share(sc)

    # ── Headline: behind-the-meter gas-to-power vs grid delta ────────────
    # The punchline a BTM developer wants: cheapest credible gas-to-grid
    # scenario ($/MWh, modern CCGT) vs a representative grid wholesale price.
    headline = _headline_btm_vs_grid(econ, gas_share)

    # ── DCGI fields (honest about missing-state) ─────────────────────────
    dcgi_score = dcgi_row.get("dcgi")
    dcgi_verdict = dcgi_row.get("verdict")
    pipelines_total = dcgi_row.get("pipelines")
    pipelines_interstate = dcgi_row.get("interstate")
    operators_count = dcgi_row.get("operators")
    gas_access_score = dcgi_row.get("gas_access_score")

    # Distinguish "state not covered by DCGI" from "rollup failed".
    if not dcgi_row:
        dcgi_basis = ("unavailable" if dcgi_err
                      else "fallback")  # state simply not in the gas_pipelines geofeed
        dcgi_note = (dcgi_err or
                     f"{sc} has no rows in the EIA gas-pipeline geofeed, so DCGI "
                     "is not scored for it. Not estimated.")
    else:
        dcgi_basis = dcgi_row.get("data_basis") or "modeled"
        dcgi_note = None
        if dcgi_basis.startswith("modeled_territory"):
            dcgi_note = ("DCGI for this territory is a MODELED placeholder "
                         "(LNG-only / Tier-2), not gas_pipelines-derived.")

    out = {
        "region": sc,
        "region_name": _state_name(sc),
        "dcgi_score": dcgi_score,
        "dcgi_verdict": dcgi_verdict,

        # Gas access (pipeline counts + operators) — PRESENCE, not throughput.
        "gas_access": {
            "gas_access_score": gas_access_score,
            "pipelines_total": pipelines_total,
            "pipelines_interstate": pipelines_interstate,
            "operators": operators_count,
            "basis": ("PRESENCE / DENSITY only — the EIA pipeline geofeed has no "
                      "per-segment throughput (capacity_mcf), so this is "
                      "infrastructure presence, NOT deliverability or firm capacity."),
        },

        # Layer 2 — economics.
        "henry_hub_usd_mmbtu": econ["henry_hub"],
        "basis_usd_mmbtu": econ["basis"],            # SYNTHETIC — see data_basis
        "delivered_price_usd_mmbtu": econ["delivered"],  # NULL fallback — see data_basis
        "gas_to_grid_usd_per_mwh": econ["scenarios"],     # 5 heat-rate scenarios

        # Layer 4 — live grid gas share.
        "live_grid_gas_share_pct": gas_share["share_pct"],

        # Headline punchline.
        "headline_behind_meter_vs_grid_delta_usd_mwh": headline["delta_usd_mwh"],
        "headline": headline,

        # Layer 3 — pipeline presence (operators + parent midstreams + interstate).
        "pipeline_presence": {
            "basis": ("PROXIMITY / PRESENCE only (operator + parent-midstream + "
                      "interstate flag from the gas_pipelines geofeed). NOT firm "
                      "transport capacity, contracted volume, or throughput."),
            "pipelines_total": pipelines_total,
            "pipelines_interstate": pipelines_interstate,
            "distinct_operators": operators_count,
            "parent_midstreams": midstreams,
        },

        # Per-layer provenance + confidence for every headline field.
        "data_basis": {
            "dcgi_score":     {"source": "DC Hub DCGI (gas_pipelines + eia_gas_prices)",
                               "confidence": dcgi_basis,
                               **({"note": dcgi_note} if dcgi_note else {})},
            "dcgi_verdict":   {"source": "DC Hub DCGI", "confidence": dcgi_basis},
            "gas_access":     {"source": "gas_pipelines geofeed (EIA ArcGIS)",
                               "confidence": ("fallback" if pipelines_total is None else "modeled"),
                               "note": "Pipeline PRESENCE/density — not throughput or firm capacity."},
            "henry_hub_usd_mmbtu":  {"source": econ["henry_hub_source"],
                                     "confidence": econ["henry_hub_basis"]},
            "basis_usd_mmbtu":      {"source": econ["basis_source"],
                                     "confidence": "synthetic",
                                     "note": "Regional basis is a Phase-1 SYNTHETIC seed "
                                             "(typical differential vs Henry Hub), NOT a live "
                                             "spot index. Do not present as a live basis."},
            "delivered_price_usd_mmbtu": {"source": econ["delivered_source"],
                                          "confidence": econ["delivered_basis"],
                                          "note": econ["delivered_note"]},
            "gas_to_grid_usd_per_mwh":   {"source": "heat-rate × gas $/MMBtu / 1000",
                                          "confidence": econ["scenarios_basis"],
                                          "note": econ["scenarios_note"]},
            "live_grid_gas_share_pct":   {"source": gas_share["source"] or "EIA-930 fuel-type mix",
                                          "confidence": gas_share["data_basis"],
                                          **({"iso": gas_share["iso"]} if gas_share["iso"] else {}),
                                          **({"note": gas_share["note"]} if gas_share["note"] else {})},
            "pipeline_presence":         {"source": "gas_pipelines geofeed (EIA ArcGIS)",
                                          "confidence": ("fallback" if pipelines_total is None else "modeled"),
                                          "note": "Operator + parent-midstream PRESENCE, not firm transport capacity."},
            "headline_behind_meter_vs_grid_delta_usd_mwh": {
                "source": "modern-CCGT gas-to-grid $/MWh vs representative grid wholesale",
                "confidence": headline["confidence"],
                "note": headline["note"],
            },
        },

        # Explicit omissions — what we deliberately do NOT fabricate.
        "omitted_no_fabrication": [
            "gas storage levels (no inventory feed at state granularity)",
            "LNG import/export capacity",
            "firm pipeline transport / contracted capacity (geofeed has no capacity_mcf)",
        ],

        "license": "CC BY 4.0 — cite 'DC Hub (dchub.cloud)'",
        "note": ("Fused gas brief: DCGI access score + gas economics (Henry Hub live, "
                 "basis synthetic) + pipeline presence + live ISO gas share. The cost "
                 "half is THIN — delivered tariffs are state-sparse; see data_basis."),
    }

    # ── Withdraw the DCGI composite and the $/MWh figures (2026-08-08) ────
    # This brief is the widest gas surface we publish — it fuses BOTH
    # withdrawn products into one payload, so both flags apply here. The
    # Henry Hub read, the live grid gas share and the pipeline/midstream
    # presence layers are unaffected and stay.
    if not gas_index_enabled():
        for _k in GAS_INDEX_FIELDS:
            out.pop(_k, None)
            out["data_basis"].pop(_k, None)
        out["gas_access"] = strip_index_fields(out["gas_access"])
        out["pipeline_presence"] = strip_index_fields(out["pipeline_presence"])
        out["dcgi_status"] = gas_index_unavailable()
        out["note"] = (
            "Gas brief WITHOUT the DCGI score: the index was withdrawn "
            "2026-08-08 (see dcgi_status). What remains is the live Henry Hub "
            "read, the live ISO gas share, and pipeline/midstream presence — "
            "none of which use the two defective DCGI terms.")

    if not gas_to_grid_enabled():
        for _k in GAS_TO_GRID_FIELDS:
            out.pop(_k, None)
            out["data_basis"].pop(_k, None)
        out.pop("headline", None)
        out["gas_to_grid_status"] = gas_to_grid_unavailable()

    # ★2026-08-31 — ONE ENTRY PER CAPABILITY THAT IS ACTUALLY ABSENT.
    #
    # This read `if not (gas_index_enabled() and gas_to_grid_enabled())` and then
    # appended ONE string naming BOTH products. Two independent kill switches,
    # one combined claim: the moment they disagreed, the response contradicted
    # itself. Measured on the live API 2026-08-31T09:11Z, get_gas_intelligence
    # (TX) returned `dcgi_score: 81.9` and `dcgi_verdict: GAS-ADVANTAGED` while
    # its own `omitted_no_fabrication` array said the DCGI composite had been
    # omitted as withdrawn. Both facts in one payload.
    #
    # That is worse here than in ordinary copy. `omitted_no_fabrication` is the
    # field the tool descriptions tell agents to read to learn what an answer
    # does NOT cover — it is the honesty layer itself. An agent that trusts it
    # would discard a score sitting in the same object.
    #
    # The two switches are separate ON PURPOSE (util/gas_index.py: "deliberately
    # two separate switches so the cheaper fix can ship first"), and on
    # 2026-08-30 they diverged exactly as designed — the DCGI was restored and
    # the gas-to-grid $/MWh was not. So the array is now built per switch: it
    # names what is missing, never what its neighbour's state implies.
    _omitted = []
    if not gas_index_enabled():
        _omitted.append(
            "DCGI composite (withdrawn 2026-08-08 pending correction — see dcgi_status)")
    if not gas_to_grid_enabled():
        _omitted.append(
            "gas-to-grid $/MWh (withdrawn 2026-08-08 pending correction — see gas_to_grid_status)")
    if _omitted:
        out["omitted_no_fabrication"] = list(out["omitted_no_fabrication"]) + _omitted
    return out


def _state_name(sc: str) -> str:
    try:
        from routes.dcgi import _US_STATE_NAMES
        return _US_STATE_NAMES.get(sc, sc)
    except Exception:
        return sc


def _state_midstreams(sc: str):
    """Parent-midstream companies with pipeline PRESENCE in this state.
    Re-matches the _MIDSTREAMS alias table against this state's gas_pipelines
    rows (derived state via the same lat/lng→state matcher DCGI uses)."""
    try:
        from routes._iso_common import conn
        from routes.dcgi import _MIDSTREAMS, _lat_lng_to_state
    except Exception:
        return []
    # operator -> segment count in this state
    op_counts = {}
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")
            cur.execute("SELECT lat, lng, operator FROM gas_pipelines "
                        "WHERE operator IS NOT NULL AND operator <> ''")
            for lat, lng, operator in cur.fetchall():
                if lat is None or lng is None:
                    continue
                try:
                    st = _lat_lng_to_state(float(lat), float(lng))
                except Exception:
                    st = ""
                if (st or "").upper() != sc:
                    continue
                op_counts[operator] = op_counts.get(operator, 0) + 1
    except Exception:
        return []
    out = []
    for m in _MIDSTREAMS:
        al = [a.lower() for a in m["aliases"]]
        segments = 0
        matched = []
        for op, cnt in op_counts.items():
            if any(a in (op or "").lower() for a in al):
                segments += cnt
                matched.append(op)
        if segments > 0:
            out.append({
                "key": m["key"], "name": m["name"], "type": m["type"],
                "hq": m["hq"],
                "pipeline_segments": segments,
                "matched_entities": sorted(set(matched))[:8],
            })
    out.sort(key=lambda x: x["pipeline_segments"], reverse=True)
    return out


def _gas_economics_for_state(sc: str) -> dict:
    """Henry Hub (live) + basis (synthetic) + delivered (null fallback) +
    the 5 heat-rate gas-to-grid $/MWh scenarios, reusing routes.powered_land_gas
    helpers so we don't duplicate the EIA / heat-rate logic."""
    res = {
        "henry_hub": None, "henry_hub_source": "synthetic_seed", "henry_hub_basis": "synthetic",
        "basis": None, "basis_source": "synthetic_seed_basis", "basis_hub_key": None, "basis_hub_name": None,
        "delivered": None, "delivered_source": "eia_gas_prices",
        "delivered_basis": "unavailable", "delivered_note": None,
        "hub_spot": None,
        "scenarios": {}, "scenarios_basis": "modeled", "scenarios_note": None,
        "scenario_gas_price_usd_mmbtu": None,
    }
    try:
        from routes import powered_land_gas as plg
    except Exception as e:
        res["delivered_note"] = "gas economics module unavailable: " + type(e).__name__
        res["scenarios"] = {k: None for k in (
            "new_ccgt_6400_btu_kwh", "avg_ccgt_6800_btu_kwh", "old_ccgt_7500_btu_kwh",
            "peaker_10500_btu_kwh", "old_peaker_12000_btu_kwh")}
        return res

    # Henry Hub spot (live when EIA_API_KEY set; else synthetic seed — labeled).
    try:
        hh, hh_src, _hh_period = plg._fetch_henry_hub_spot()
        res["henry_hub"] = hh
        res["henry_hub_source"] = "eia_v2_natural-gas/pri/fut" if str(hh_src).startswith("eia_") else hh_src
        res["henry_hub_basis"] = "live" if str(hh_src).startswith("eia_") else "synthetic"
    except Exception:
        pass

    # Regional basis — SYNTHETIC seed, labeled. Hub keyed off the state.
    try:
        hub_key = plg._STATE_TO_HUB.get(sc, "henry_hub")
        hub_name = plg._HUB_NAME.get(hub_key, hub_key)
        basis, basis_src = plg._basis_for_hub(hub_key)
        res["basis"] = basis
        res["basis_source"] = basis_src
        res["basis_hub_key"] = hub_key
        res["basis_hub_name"] = hub_name
        if res["henry_hub"] is not None and basis is not None:
            res["hub_spot"] = round(res["henry_hub"] + basis, 3)
    except Exception:
        pass

    # Delivered tariff — the THIN half. eia_gas_prices is state-sparse; most
    # states return None. Surface honestly as NULL with a note — never faked.
    delivered = None
    try:
        elec, elec_src, _ep = plg._delivered_electric(sc)
        ind, ind_src, _ip = plg._delivered_industrial(sc)
        if elec is not None:
            delivered = elec
            res["delivered_source"] = "eia_gas_prices (electric-power sector)"
            res["delivered_basis"] = "live"
        elif ind is not None:
            delivered = ind
            res["delivered_source"] = "eia_gas_prices (industrial sector)"
            res["delivered_basis"] = "live"
        else:
            res["delivered_source"] = "eia_gas_prices"
            res["delivered_basis"] = "unavailable"
            res["delivered_note"] = (
                "No delivered industrial/electric gas tariff is published for "
                f"{sc} in eia_gas_prices (the delivered-price table is sparse / "
                "empty for most states). Surfaced as null — NOT a fabricated "
                "delivered price. Gas-to-grid $/MWh below falls back to "
                "Henry-Hub-plus-basis as the burner-tip proxy.")
    except Exception as e:
        res["delivered_note"] = "delivered lookup error: " + type(e).__name__
    res["delivered"] = delivered

    # Gas-to-grid $/MWh — 5 heat-rate scenarios. Use the best available burner-
    # tip price: delivered tariff > hub spot (HH+basis) > Henry Hub. Label which.
    if delivered is not None:
        gas_price = delivered
        res["scenarios_basis"] = "modeled"
        res["scenarios_note"] = ("$/MWh = heat_rate × delivered gas $/MMBtu / 1000, "
                                 "using the live EIA delivered tariff.")
    elif res["hub_spot"] is not None:
        gas_price = res["hub_spot"]
        res["scenarios_basis"] = "modeled_fallback"
        res["scenarios_note"] = ("No delivered tariff for this state — $/MWh derived from "
                                 "Henry Hub + SYNTHETIC regional basis as the burner-tip "
                                 "proxy. Treat as an estimate, not a delivered-cost quote.")
    elif res["henry_hub"] is not None:
        gas_price = res["henry_hub"]
        res["scenarios_basis"] = "modeled_fallback"
        res["scenarios_note"] = ("No delivered tariff or basis — $/MWh derived from bare "
                                 "Henry Hub. Lower bound; excludes basis + delivery.")
    else:
        gas_price = None
        res["scenarios_basis"] = "unavailable"
        res["scenarios_note"] = "No gas price available — scenarios not computed."
    res["scenario_gas_price_usd_mmbtu"] = gas_price

    try:
        g2g = plg.gas_to_grid_usd_per_mwh
        if gas_price is not None and gas_price > 0:
            res["scenarios"] = {
                "new_ccgt_6400_btu_kwh":    g2g(gas_price, 6_400),
                "avg_ccgt_6800_btu_kwh":    g2g(gas_price, 6_800),
                "old_ccgt_7500_btu_kwh":    g2g(gas_price, 7_500),
                "peaker_10500_btu_kwh":     g2g(gas_price, 10_500),
                "old_peaker_12000_btu_kwh": g2g(gas_price, 12_000),
            }
        else:
            res["scenarios"] = {k: None for k in (
                "new_ccgt_6400_btu_kwh", "avg_ccgt_6800_btu_kwh", "old_ccgt_7500_btu_kwh",
                "peaker_10500_btu_kwh", "old_peaker_12000_btu_kwh")}
    except Exception:
        res["scenarios"] = {k: None for k in (
            "new_ccgt_6400_btu_kwh", "avg_ccgt_6800_btu_kwh", "old_ccgt_7500_btu_kwh",
            "peaker_10500_btu_kwh", "old_peaker_12000_btu_kwh")}
    return res


# Representative grid wholesale $/MWh by ISO for the headline delta. These are
# REPRESENTATIVE round-trip wholesale levels (not a live LMP) — clearly labeled
# 'representative' so the headline is never mistaken for a live grid quote. The
# live LMP feed is grid-side and out of scope for this gas synthesizer.
_REPRESENTATIVE_GRID_WHOLESALE_USD_MWH = {
    "ERCOT": 38.0, "PJM": 42.0, "MISO": 36.0, "CAISO": 52.0,
    "NYISO": 48.0, "SPP": 30.0, "ISONE": 55.0,
}


def _headline_btm_vs_grid(econ: dict, gas_share: dict) -> dict:
    """The punchline: cheapest credible behind-the-meter gas-to-grid $/MWh
    (modern CCGT) vs a representative grid wholesale price for the ISO.
    delta < 0 => gas is cheaper than the grid (BTM-favorable)."""
    out = {
        "delta_usd_mwh": None,
        "gas_to_grid_new_ccgt_usd_mwh": None,
        "representative_grid_wholesale_usd_mwh": None,
        "interpretation": None,
        "confidence": "unavailable",
        "note": None,
    }
    scen = (econ.get("scenarios") or {})
    btm = scen.get("new_ccgt_6400_btu_kwh")
    out["gas_to_grid_new_ccgt_usd_mwh"] = btm
    iso = gas_share.get("iso")
    grid_px = _REPRESENTATIVE_GRID_WHOLESALE_USD_MWH.get(iso)
    out["representative_grid_wholesale_usd_mwh"] = grid_px
    if btm is None or grid_px is None:
        out["note"] = (
            "Headline needs both a gas-to-grid $/MWh (modern CCGT) and a grid "
            "wholesale benchmark for the ISO; one is unavailable so the delta is "
            "not computed (not estimated).")
        return out
    delta = round(btm - grid_px, 2)
    out["delta_usd_mwh"] = delta
    out["confidence"] = ("modeled" if econ.get("scenarios_basis") == "modeled"
                         else "modeled_fallback")
    out["interpretation"] = (
        f"Behind-the-meter modern-CCGT gas power ≈ ${btm}/MWh vs a representative "
        f"{iso} grid wholesale ≈ ${grid_px}/MWh → gas is "
        + ("CHEAPER" if delta < 0 else "PRICIER")
        + f" by ${abs(delta)}/MWh "
        + ("(behind-the-meter favorable" if delta < 0 else "(grid favorable")
        + " on energy cost alone; excludes the 5-7yr interconnect-queue time "
          "advantage of BTM and capex/emissions).")
    out["note"] = (
        "The grid benchmark is a REPRESENTATIVE wholesale level per ISO, NOT a "
        "live LMP. The gas $/MWh confidence flows from the gas-price basis "
        f"({econ.get('scenarios_basis')}). Directional, not a settlement quote.")
    return out


# ── Tier gating ───────────────────────────────────────────────────────
def _is_internal(req) -> bool:
    ua = (req.headers.get("User-Agent") or "").lower()
    ip = (req.headers.get("Cf-Connecting-Ip")
          or (req.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
          or req.remote_addr or "")
    if any(m in ua for m in ("dchub-", "dchubhealer", "brain-radar",
                             "brain-v2-headless", "uptimerobot")):
        return True
    try:
        from railway_egress import is_railway_egress
        return bool(is_railway_egress(ip))
    except Exception:
        return False


def _is_identified(req) -> bool:
    """identified+ (any valid X-API-Key / JWT). Mirrors get_grid_intelligence:
    the presence of a valid key/JWT lifts the teaser."""
    # Fast path: any API key shape present (matches phase19b_grid_intelligence
    # which lifts the teaser on _has_key). The auth_context resolver is the
    # authoritative tier check below; this keeps parity with the grid endpoint.
    try:
        from routes.auth_context import get_auth_context, TIER_IDENTIFIED
        ctx = get_auth_context(req)
        if ctx.is_at_least(TIER_IDENTIFIED):
            return True
    except Exception:
        pass
    # Parity fallback with the grid endpoint's _has_key gate.
    return bool(req.headers.get("X-API-Key") or req.args.get("api_key"))


def _teaser(full: dict) -> dict:
    """Anonymous external teaser — region + dcgi_score + dcgi_verdict +
    henry_hub only (matches the grid endpoint's headline-only gate)."""
    # ★ A THIRD ungated numeric DCGI surface: this teaser handed anonymous
    #   callers dcgi_score + dcgi_verdict in the clear, alongside
    #   /api/v1/dcgi/scores/<STATE> and the /dcgi noscript table. `full` no
    #   longer carries those keys once the index is withdrawn, so the .get()s
    #   below would emit bare nulls — replaced with the reason instead, since
    #   a null score reads as a transient miss and invites a retry.
    _idx_off = "dcgi_status" in full
    out = {
        "region":        full.get("region"),
        "region_name":   full.get("region_name"),
        "dcgi_score":    full.get("dcgi_score"),
        "dcgi_verdict":  full.get("dcgi_verdict"),
        "henry_hub_usd_mmbtu": full.get("henry_hub_usd_mmbtu"),
        # Everything else gated.
        "gas_access":               "<gated: identified-tier or higher>",
        "basis_usd_mmbtu":          "<gated: identified-tier or higher>",
        "delivered_price_usd_mmbtu": "<gated: identified-tier or higher>",
        "gas_to_grid_usd_per_mwh":  "<gated: identified-tier or higher>",
        "live_grid_gas_share_pct":  "<gated: identified-tier or higher>",
        "headline_behind_meter_vs_grid_delta_usd_mwh": "<gated: identified-tier or higher>",
        "pipeline_presence":        "<gated: identified-tier or higher>",
        "data_basis": {k: full["data_basis"][k]
                       for k in ("dcgi_score", "henry_hub_usd_mmbtu")
                       if k in full.get("data_basis", {})},
        "gated": True,
        "tier_required": "identified",
        "message": (
            f"The DCGI score for {full.get('region_name') or full.get('region')} is "
            "WITHDRAWN as of 2026-08-08 — see dcgi_status for which terms were "
            "wrong. You still get the live Henry Hub spot. The rest of the fused "
            "brief — pipeline presence and live ISO gas share — requires a free "
            "dev key (email-only signup, no credit card)."
            if _idx_off else
            f"You got the DCGI headline for {full.get('region_name') or full.get('region')} "
            f"(score {full.get('dcgi_score')} — {full.get('dcgi_verdict')}) and the live "
            f"Henry Hub spot. The full fused brief — gas-to-grid $/MWh, behind-the-meter-vs-"
            "grid delta, pipeline presence, and live ISO gas share — requires a free dev key "
            "(email-only signup, no credit card)."
        ),
        "agent_action": {
            "type":    "claim_free_key",
            "method":  "POST",
            "url":     "https://dchub.cloud/api/v1/keys/claim",
            "headers": {"Content-Type": "application/json"},
            "body":    {"client_name": "<your agent identifier>"},
            "then":    f"Retry GET /api/v1/gas/intelligence/{full.get('region')} with header 'X-API-Key: <api_key>'",
        },
        "license": "CC BY 4.0 — cite 'DC Hub (dchub.cloud)'",
    }
    if _idx_off:
        # Drop the two withdrawn keys entirely rather than leaving nulls, and
        # carry the reason into the anonymous surface too — this is the tier
        # that was reading the score without an account.
        out.pop("dcgi_score", None)
        out.pop("dcgi_verdict", None)
        out["dcgi_status"] = full["dcgi_status"]
    if "gas_to_grid_status" in full:
        out.pop("gas_to_grid_usd_per_mwh", None)
        out.pop("headline_behind_meter_vs_grid_delta_usd_mwh", None)
        out["gas_to_grid_status"] = full["gas_to_grid_status"]
    return out


@gas_intelligence_bp.route("/api/v1/gas/intelligence/<region>", methods=["GET", "OPTIONS"])
def gas_intelligence(region):
    """Fused per-state gas brief — the gas analogue of get_grid_intelligence.
    region = a US state code or name (TX, Texas, "new york", NY, ...).
    Anonymous external callers get a teaser; identified+ get the full brief."""
    if request.method == "OPTIONS":
        return ("", 204)

    sc = _resolve_state_code(region or "")
    if not sc:
        return jsonify({
            "error": "unknown region",
            "detail": "region must be a US state code or name (e.g. TX, Texas).",
            "region_requested": region,
        }), 400

    try:
        full = _build_gas_intel(sc)
    except Exception as e:
        return jsonify({
            "error": "gas_intelligence_failed",
            "detail": str(e)[:200],
            "region_requested": sc,
        }), 500

    if _is_internal(request) or _is_identified(request):
        resp = jsonify(full)
    else:
        resp = jsonify(_teaser(full))
    # Same private cache posture as the grid + gas-pricing endpoints.
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp, 200


def register_gas_intelligence(app):
    app.register_blueprint(gas_intelligence_bp)
