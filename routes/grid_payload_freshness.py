"""Shell #35 Moat Deepening (2026-07-26) — pure helpers for the flagship
grid payload: verifiable per-layer freshness + bias-adjusted measured
headroom.

Pure functions only (no Flask, no DB, no network) so tests import this
module directly (house rule: tests NEVER import main). main.py calls
these at RESPONSE time (the grid-intel payload is cached ~30 min, so
ages must be computed per request, not at fetch time).

Design notes:
- This is NOT a fourth freshness system (three exist already — see
  freshness_universal / heartbeat re-stamp / freshness_public). It only
  DERIVES ages from timestamps already present in the payload and points
  at the existing public verifier (/api/v1/freshness).
- Structural headroom offsets are DOCUMENTED measurement artifacts
  (iso_grid_adapters: ERCOT seMW is gross gen → headroom reads ~+13% of
  load high; MISO online gen excludes imports → ~−14% low). We store RAW
  telemetry untouched and adjust only at serving, showing both numbers
  and the method. Same honesty rule as the DCPI ±6pp clamp.
"""

from __future__ import annotations

import datetime

# ── r-ext-freshness (2026-08-08) ─────────────────────────────────────────────
# THE promotion table for grid_ext_metrics. main.py._grid_intel_fetch drives its
# extended-metric promotion off this, and build_freshness_block derives its
# layers from it, so a field cannot reach the payload without BOTH a timestamp
# companion and a freshness layer.
#
# It exists because those two lists drifted apart. Nine categories were promoted
# to top-level fields; only four carried an `*_as_of`; the freshness block
# covered 7 payload fields. The five with no timestamp — reserves, margin,
# capacity, emissions, dc_load_queue_measured — were bare numbers a consumer had
# no way to age.
#
# ★ WHY THAT MATTERS HERE SPECIFICALLY: _grid_ext_metrics_for takes the LATEST
# row per category with NO age bound, and these rows come from gridstatus.io,
# whose free tier is 250 calls/MONTH — it returned 403 "API requests limit
# reached. Usage: 375, Limit: 250" on 2026-07-31 (see gridstatus_client). When
# that feed stops, the last ingested row stays in grid_ext_metrics and this
# endpoint keeps serving it as live telemetry on every US ISO, forever.
#
# ★ THE SLAs BELOW ARE MEASURED, NOT ASPIRATIONAL. routes/grid_data_master_shell
# ingests ONE dataset per tick from a ~22-entry registry on a daily tick, so any
# single gridstatus-sourced category refreshes on the order of WEEKS, not hours.
# An SLA of 4h here would mark every ISO red on a working system and tell a
# reader nothing. What is published unconditionally is the AGE; the SLA only
# decides the `within_sla` flag, and each layer names its own cadence so a
# consumer can apply a stricter bar than ours.
class _Promotion:
    __slots__ = ("field", "as_of_field", "layer", "sla_hours", "cadence")

    def __init__(self, field, layer, sla_hours, cadence):
        self.field = field
        self.as_of_field = None      # set by _promo below; never left unset
        self.layer = layer
        self.sla_hours = sla_hours
        self.cadence = cadence


def _promo(field, layer, sla_hours, cadence, as_of_field=None):
    p = _Promotion(field, layer, sla_hours, cadence)
    # Every promoted field gets an explicit companion. Derived by default so a
    # new entry cannot forget one.
    p.as_of_field = as_of_field or (field + "_as_of")
    return p


_HOUR = 1
_DAY = 24
_GRIDSTATUS_CADENCE = ("gridstatus.io via the Grid Data Master Shell, which "
                       "ingests ONE dataset per daily tick across a ~22-entry "
                       "registry — a given category refreshes on the order of "
                       "weeks. Read age_minutes, not the label.")

EXT_PROMOTIONS = {
    # grid_ext_metrics category -> promotion
    "load_forecast": _promo("load_forecast_mw", "load_forecast", 30 * _HOUR,
                            _GRIDSTATUS_CADENCE, as_of_field="load_forecast_as_of"),
    "reserves":      _promo("operating_reserves_mw", "reserves", 30 * _DAY,
                            _GRIDSTATUS_CADENCE, as_of_field="operating_reserves_as_of"),
    "margin":        _promo("operating_margin_mw", "margin", 30 * _DAY,
                            _GRIDSTATUS_CADENCE, as_of_field="operating_margin_as_of"),
    "capacity":      _promo("committed_capacity_mw", "committed_capacity", 30 * _DAY,
                            _GRIDSTATUS_CADENCE, as_of_field="committed_capacity_as_of"),
    "emissions":     _promo("marginal_emissions_lb_mwh", "emissions", 30 * _DAY,
                            _GRIDSTATUS_CADENCE, as_of_field="marginal_emissions_as_of"),
    "lmp":           _promo("lmp_usd_mwh", "lmp", 4 * _HOUR,
                            "ISO real-time LMP (5-min) via iso_lmp_snapshots / "
                            "gridstatus ISO-NE",
                            as_of_field="lmp_as_of"),
    "capacity_price": _promo("capacity_price_usd_mw_day", "capacity_price", 400 * _DAY,
                             "annual capacity-auction cycle; a cited seed, "
                             "updated per auction",
                             as_of_field="capacity_price_as_of"),
    "dc_load_queue": _promo("dc_load_queue_gw", "dc_queue", 14 * _DAY,
                            "ISO-published DC-load queue, Depth Master Shell",
                            as_of_field="dc_load_queue_as_of"),
    "dc_load_queue_measured": _promo("dc_load_queue_measured_gw",
                                     "dc_queue_measured", 14 * _DAY,
                                     "DC Hub's own row-level classification of "
                                     "the interconnection queue",
                                     as_of_field="dc_load_queue_measured_as_of"),
}

# Layer → (payload key holding the source timestamp, SLA hours).
# SLA targets mirror the public /api/v1/freshness domain targets where a
# domain exists (iso=4h); slower-moving layers use their real cadence.
#
# r-ext-freshness (2026-08-08): the extended-metric layers are no longer listed
# by hand — they are generated from EXT_PROMOTIONS, which is the same table the
# payload promotes from. That is what stops the two drifting apart again.
FRESHNESS_LAYERS = [
    ("demand",         "demand_period",           4),
    ("fuel_mix",       "generation_mix_period",   30),   # EIA publishes mix slower than demand (documented lag)
    ("telemetry",      None,                      4),         # headroom_measured.observed_at
] + [(p.layer, p.as_of_field, p.sla_hours) for p in EXT_PROMOTIONS.values()]

# layer -> what produces it and how often, published beside every age.
LAYER_CADENCE = {p.layer: p.cadence for p in EXT_PROMOTIONS.values()}
LAYER_CADENCE["demand"] = "EIA hourly RTO demand"
LAYER_CADENCE["fuel_mix"] = ("EIA hourly RTO fuel mix — published several hours "
                             "behind demand; an overnight reading is routinely "
                             "18-24h old")
LAYER_CADENCE["telemetry"] = "DC Hub ISO telemetry poll (~1.5h cron)"

VERIFY_URL = "https://dchub.cloud/api/v1/freshness"

# Documented structural offsets in the measured gen−demand headroom,
# expressed in percentage points of load (see iso_grid_adapters.py
# comments; ERCOT ~+13pp because seMW is GROSS generation, MISO ~−14pp
# because its online-gen feed excludes imports).
STRUCTURAL_OFFSET_PP = {"ERCOT": 13.0, "MISO": -14.0}


def parse_ts(s):
    """Tolerant UTC parse for the timestamp shapes in the grid payload:
    full ISO-8601 (with/without Z or offset), EIA hour 'YYYY-MM-DDTHH',
    date-only. Returns naive-UTC datetime or None. Never raises."""
    if not s:
        return None
    if isinstance(s, datetime.datetime):
        d = s
    else:
        raw = str(s).strip()
        if len(raw) == 13 and raw[10] == "T":      # EIA 'YYYY-MM-DDTHH'
            raw += ":00:00+00:00"
        elif len(raw) == 10:                        # date-only
            raw += "T00:00:00+00:00"
        elif raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            d = datetime.datetime.fromisoformat(raw)
        except Exception:
            return None
    try:
        if d.tzinfo is not None:
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        return None
    return d


def _age_minutes(ts, now):
    d = parse_ts(ts)
    if d is None:
        return None
    try:
        return max(0, int((now - d).total_seconds() // 60))
    except Exception:
        return None


def build_freshness_block(payload: dict, now=None) -> dict:
    """Verifiable per-layer freshness derived from timestamps ALREADY in
    the payload. Core layers (SLA <= 4h) roll up to within_sla_core."""
    now = now or datetime.datetime.utcnow()
    ext = payload.get("extended_metrics") or {}
    sources = {
        "demand": payload.get("demand_period"),
        "fuel_mix": payload.get("generation_mix_period"),
        "telemetry": (payload.get("headroom_measured") or {}).get("observed_at"),
    }
    # r-ext-freshness (2026-08-08): every promoted extended metric contributes a
    # layer, derived from the SAME table main.py promotes from. The timestamp is
    # read from the promoted `*_as_of` field, falling back to the
    # extended_metrics row it came from, so a layer is missing only when the
    # metric itself is absent — never because nobody added it to a second list.
    for cat, promo in EXT_PROMOTIONS.items():
        ts = payload.get(promo.as_of_field) or (ext.get(cat) or {}).get("as_of")
        if promo.layer == "lmp" and not ts:
            ts = (ext.get("rt_lmp_hub_avg") or {}).get("as_of")
        sources[promo.layer] = ts
    layers, core_ok = {}, True
    unaged = []
    for layer, _key, sla_h in FRESHNESS_LAYERS:
        ts = sources.get(layer)
        age = _age_minutes(ts, now) if ts else None
        if age is None:
            # A metric that IS in the payload but carries no usable timestamp is
            # named, not skipped. Silently omitting it is how five promoted
            # fields went un-aged for months.
            promo = next((p for p in EXT_PROMOTIONS.values() if p.layer == layer), None)
            if promo is not None and payload.get(promo.field) is not None:
                unaged.append({"layer": layer, "field": promo.field,
                               "reason": "no parseable source timestamp — "
                                         "treat the value as of UNKNOWN age"})
            continue
        within = age <= sla_h * 60
        entry = {"as_of": str(ts), "age_minutes": age,
                 "sla_hours": sla_h, "within_sla": within}
        if layer in LAYER_CADENCE:
            entry["source_cadence"] = LAYER_CADENCE[layer]
        layers[layer] = entry
        if sla_h <= 4 and not within:
            core_ok = False
    if not layers and not unaged:
        return {}
    stale = [{"layer": k, "age_minutes": v["age_minutes"], "sla_hours": v["sla_hours"]}
             for k, v in layers.items() if not v["within_sla"]]
    out = {
        "layers": layers,
        "within_sla_core": core_ok,
        "stale_layers": stale,
        "checked_at": now.replace(microsecond=0).isoformat() + "Z",
        "verify_url": VERIFY_URL,
        "note": ("Ages computed at response time from each layer's source "
                 "timestamp. Every promoted extended metric has a layer here — "
                 "if a field is in the payload and NOT in layers, its age is "
                 "unknown and it is listed in unaged_layers. Cross-check the "
                 "public SLA endpoint — no tracked rival exposes verifiable "
                 "data age."),
    }
    if unaged:
        out["unaged_layers"] = unaged
    return out


def adjust_headroom(iso: str, gen_mw, load_mw, headroom_mw) -> dict:
    """Raw + structurally-adjusted headroom for ISOs with a documented
    measurement offset. Returns {} when inputs are unusable."""
    try:
        load = float(load_mw)
        headroom = float(headroom_mw)
    except (TypeError, ValueError):
        return {}
    if load <= 0:
        return {}
    offset_pp = STRUCTURAL_OFFSET_PP.get((iso or "").upper())
    raw_pct = round(headroom / load * 100.0, 1)
    out = {"headroom_mw_raw": round(headroom, 1),
           "reserve_margin_pct_raw": raw_pct}
    if offset_pp is None:
        return out
    adj_mw = headroom - (offset_pp / 100.0) * load
    out.update({
        "structural_offset_pp": offset_pp,
        "headroom_mw_adjusted": round(adj_mw, 1),
        "reserve_margin_pct_adjusted": round(raw_pct - offset_pp, 1),
        "adjustment_method": (
            "Documented measurement artifact: ERCOT gen feed (seMW) is "
            "gross generation (+13pp of load); MISO online gen excludes "
            "imports (−14pp). Raw telemetry is stored unmodified; the "
            "adjustment is applied at serving, both values shown."),
    })
    return out


def measured_headroom_block(row: dict, iso: str, now=None,
                            max_age_hours: float = 24.0) -> dict:
    """Build the headroom_measured payload block from one grid_telemetry
    row dict (observed_at, online_gen_mw, load_mw, headroom_mw,
    reserve_margin_pct, source). Returns {} when stale or unusable —
    never surface a dead feed as 'measured'."""
    if not row:
        return {}
    now = now or datetime.datetime.utcnow()
    observed = parse_ts(row.get("observed_at"))
    if observed is None:
        return {}
    age_min = max(0, int((now - observed).total_seconds() // 60))
    if age_min > max_age_hours * 60:
        return {}
    block = {
        "iso": (iso or "").upper(),
        "observed_at": observed.isoformat() + "Z",
        "age_minutes": age_min,
        "online_gen_mw": row.get("online_gen_mw"),
        "load_mw": row.get("load_mw"),
        "source": row.get("source") or "iso_telemetry",
        "basis": "measured (ISO telemetry gen − demand), not modeled",
    }
    block.update(adjust_headroom(iso, row.get("online_gen_mw"),
                                 row.get("load_mw"), row.get("headroom_mw")))
    return block if "headroom_mw_raw" in block else {}
