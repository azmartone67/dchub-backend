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

# Layer → (payload key holding the source timestamp, SLA hours).
# SLA targets mirror the public /api/v1/freshness domain targets where a
# domain exists (iso=4h); slower-moving layers use their real cadence.
FRESHNESS_LAYERS = [
    ("demand",         "demand_period",           4),
    ("fuel_mix",       "generation_mix_period",   30),   # EIA publishes mix slower than demand (documented lag)
    ("lmp",            "lmp_as_of",               4),
    ("load_forecast",  "load_forecast_as_of",     30),
    ("capacity_price", "capacity_price_as_of",    24 * 400),  # annual auction cycle
    ("dc_queue",       None,                      24 * 14),   # extended_metrics.dc_load_queue.as_of
    ("telemetry",      None,                      4),         # headroom_measured.observed_at
]

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
        "lmp": payload.get("lmp_as_of")
               or (ext.get("rt_lmp_hub_avg") or {}).get("as_of"),
        "load_forecast": payload.get("load_forecast_as_of"),
        "capacity_price": payload.get("capacity_price_as_of"),
        "dc_queue": (ext.get("dc_load_queue") or {}).get("as_of"),
        "telemetry": (payload.get("headroom_measured") or {}).get("observed_at"),
    }
    layers, core_ok = {}, True
    for layer, _key, sla_h in FRESHNESS_LAYERS:
        ts = sources.get(layer)
        if not ts:
            continue
        age = _age_minutes(ts, now)
        if age is None:
            continue
        within = age <= sla_h * 60
        layers[layer] = {"as_of": str(ts), "age_minutes": age,
                         "sla_hours": sla_h, "within_sla": within}
        if sla_h <= 4 and not within:
            core_ok = False
    if not layers:
        return {}
    return {
        "layers": layers,
        "within_sla_core": core_ok,
        "checked_at": now.replace(microsecond=0).isoformat() + "Z",
        "verify_url": VERIFY_URL,
        "note": ("Ages computed at response time from each layer's source "
                 "timestamp. Cross-check the public SLA endpoint — no "
                 "tracked rival exposes verifiable data age."),
    }


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
