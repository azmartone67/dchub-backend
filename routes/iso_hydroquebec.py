"""
iso_hydroquebec.py — Hydro-Québec grid data extractor.

Phase ZZZZZ-round33 (2026-05-24). FIRST NON-US ISO in the DCPI dataset.

Why Hydro-Québec first:
  - Largest hyperscale destination outside the US (AWS, Google, OVH all
    building/expanded in QC 2023-2026)
  - 99% renewable (94% hydro + 4-5% wind) — the greenest grid in NA
  - $0.039 USD/kWh — cheapest grid power in North America
  - Cold climate = lower cooling costs (PUE 1.1 routinely achievable)
  - Surplus exports >$1B/yr to NY/MA/Ontario — proves headroom

DATA SOURCES:
  1. LIVE — hydroquebec.com open data (tokenless, no registration):
       .../donnees-ouvertes/json/production.json  — generation by source,
         30-min resolution, keys total/hydraulique/eolien/autres/solaire
       .../donnees-ouvertes/json/demande.json     — total demand, 15-min
  2. Régie de l'énergie public filings — quarterly capacity + sales reports
  3. Hydro-Québec OASIS — https://www.hydroquebec.com/transenergie/en/oasis.html
     (NERC-registered interconnect feed; not needed for the mix)

★ CORRECTION (2026-07-28, shell #41 WS2). The block that used to sit here
said the OASIS feed "Requires NERC registration; not public-fetchable" and
concluded HQ could only ever be modeled. That conclusion was wrong: it named
the wrong feed. HQ publishes the generation mix as plain open data with no
token at all. Probed from this shell 2026-07-28:
    production.json  HTTP 200, 10,292 B, recentHour 2026-07-28T21:30,
                     total 18,849 MW / hydraulique 17,305 / eolien 731 /
                     autres 813 / solaire 0  → renewable 95.7%
    demande.json     HTTP 200, 19,011 B, recentHour 2026-07-28T23:45,
                     demandeTotal 16,865 MW
The module is now LIVE-ONLY (`live_hq_open_data_v1`). _baseline_snapshot()
is retained ONLY as a documented reference model — it is no longer written
to grid_data, because once a modeled row lands in that table it is
indistinguishable from telemetry, and HYDROQUEBEC is now an orchestrator
slot rather than a manual /run.

NOT ingested: spot price. HQ publishes no live wholesale price, so the
live payload omits it rather than passing the 2024 tariff constant off as
a spot price — /api/v1/iso/comparison will show lmp_usd_per_mwh: null.

Schema: matches existing `grid_data(iso, metric_name, metric_value, unit,
timestamp)` with conflict on `(iso, timestamp, metric_name)`.
"""

import os
import json
import time
import datetime
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._iso_common import fetch_first_working, latest_for_iso
from routes._swallowed_writes import note_swallowed_write

iso_hydroquebec_bp = Blueprint("iso_hydroquebec", __name__,
                                url_prefix="/api/v1/iso/hydroquebec")
SOURCE_ID = "iso-hydroquebec-baseline"
ISO_CODE = "HYDROQUEBEC"


# ─────────────────────────────────────────────────────────────────────
# Baseline generation model — anchored to HQ 2024-25 published mix
# Source: Hydro-Québec 2024 Sustainability Report
# https://www.hydroquebec.com/sustainable-development/
# ─────────────────────────────────────────────────────────────────────
#
# Total installed capacity:  44,400 MW
# 2024 net generation:       203 TWh
# Renewable share:           99.7%
# Carbon intensity:          ~2.5 g CO2/kWh (one of lowest in world)
#
# Monthly demand profile (typical):
#   Jan-Feb: peak heating, demand ~38,500 MW
#   Jul-Aug: peak cooling, demand ~27,000 MW
#   Spring/Fall shoulder: ~24,000 MW
#
# Spot price (CAD/MWh, 2024 average HQ Distribution rate):
#   Industrial L tariff: $36-42/MWh
#   Wholesale exports: $45-65/MWh (varies vs NY/MA/Ontario)

GENERATION_MIX = {
    "hydro":            0.942,   # 94.2% — largest hydroelectric fleet in world
    "wind":             0.046,   # 4.6%
    "biomass":          0.008,   # 0.8%
    "thermal_other":    0.002,   # 0.2% (small isolated grids in northern Quebec)
    "solar":            0.001,   # 0.1% (minimal — high-latitude, hydro is cheaper)
    "imports":          0.001,   # 0.1% (rare; HQ usually exports)
}

INSTALLED_CAPACITY_MW = 44_400
ANNUAL_GENERATION_TWH = 203
RENEWABLE_PCT         = 0.997
CARBON_INTENSITY_G_PER_KWH = 2.5

# Approximate seasonal demand model (winter peaking due to electric heating)
SEASONAL_DEMAND_MW = {
    1:  38500, 2: 37800, 3: 32000, 4: 25500, 5: 23000, 6: 24500,
    7:  27000, 8: 27500, 9: 24000, 10: 26000, 11: 31000, 12: 36000,
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────
# LIVE feed — hydroquebec.com open data (2026-07-28, shell #41 WS2)
# ─────────────────────────────────────────────────────────────────────
# `details` is a fixed-length day array PADDED past the last real reading
# with {"total": 0.0} placeholders — details[-1] is a padding row, not the
# latest value. `indexDonneePlusRecent` points at the last REAL row (45 of
# 48 at probe time), so index by it and never take the tail.
_LIVE_PRODUCTION_URL = ("https://www.hydroquebec.com/data/documents-donnees/"
                        "donnees-ouvertes/json/production.json")
_LIVE_DEMAND_URL = ("https://www.hydroquebec.com/data/documents-donnees/"
                    "donnees-ouvertes/json/demande.json")

# Generation and demand publish on DIFFERENT clocks (30-min vs 15-min, and
# demand ran 2h15m ahead of generation at probe time), so each carries its
# OWN as_of. Never stamp one timestamp across both.
_LIVE_CACHE = {"data": None, "ts": 0.0}
_LIVE_TTL = 300


def _hq_latest_row(doc):
    """Last REAL reading from an HQ open-data document, or None.

    Prefers indexDonneePlusRecent; falls back to the last details entry
    carrying a non-zero payload. Never returns a padding row."""
    details = (doc or {}).get("details") or []
    idx = (doc or {}).get("indexDonneePlusRecent")
    if isinstance(idx, int) and 0 <= idx < len(details):
        row = details[idx] or {}
        if row.get("valeurs"):
            return row
    for row in reversed(details):
        vals = (row or {}).get("valeurs") or {}
        if any(isinstance(v, (int, float)) and v for v in vals.values()):
            return row
    return None


def _live_snapshot():
    """LIVE Hydro-Québec snapshot, or None when the feed is unreachable.

    Same flat {metric: {"value", "unit"}} shape as _baseline_snapshot() so
    every existing consumer keeps working, plus string-valued provenance
    keys (method / as_of / source_url / *_basis) that callers filter out
    before persisting.

    LIVE-ONLY: returns None rather than degrading to the model. 5-min cache
    so /snapshot, /comparison and the orchestrator share ONE fetch.
    """
    now_ts = time.time()
    if _LIVE_CACHE["data"] is not None and (now_ts - _LIVE_CACHE["ts"]) < _LIVE_TTL:
        return _LIVE_CACHE["data"]
    try:
        text, _url = fetch_first_working([_LIVE_PRODUCTION_URL],
                                         ua="dchub-iso-hydroquebec/1.0",
                                         timeout=6, total_budget=8)
        prod = _hq_latest_row(json.loads(text))
    except Exception:
        return None
    vals = (prod or {}).get("valeurs") or {}
    total = vals.get("total")
    if not isinstance(total, (int, float)) or total <= 0:
        return None      # padding row or feed gap — write nothing
    total = float(total)
    hydro = float(vals.get("hydraulique") or 0.0)
    wind = float(vals.get("eolien") or 0.0)
    solar = float(vals.get("solaire") or 0.0)
    other = float(vals.get("autres") or 0.0)

    metrics = {
        "generation_total_mw":   {"value": round(total, 1), "unit": "MW"},
        "fuel_hydro_mw":         {"value": round(hydro, 1), "unit": "MW"},
        "fuel_wind_mw":          {"value": round(wind, 1),  "unit": "MW"},
        "fuel_solar_mw":         {"value": round(solar, 1), "unit": "MW"},
        "fuel_other_mw":         {"value": round(other, 1), "unit": "MW"},
        # Scoreboard-comparable renewable = hydro+wind+solar over TOTAL
        # GENERATION (not demand) — the same definition iso_eu_entsoe.py
        # ranks on (_RENEWABLE_CATS, line 76).
        "renewable_pct":         {"value": round(100.0 * (hydro + wind + solar) / total, 1),
                                  "unit": "pct"},
        "installed_capacity_mw": {"value": INSTALLED_CAPACITY_MW, "unit": "MW"},
        "carbon_intensity":      {"value": CARBON_INTENSITY_G_PER_KWH, "unit": "g/kWh"},
        "method":      "live_hq_open_data_v1",
        "as_of":       (prod or {}).get("date"),
        "source_url":  _LIVE_PRODUCTION_URL,
        "renewable_pct_basis": (f"(hydraulique+eolien+solaire)/total = "
                                f"{round(hydro + wind + solar, 1)}/{round(total, 1)} MW"),
        # HONEST NUMBERS: HQ publishes NO gas or thermal series. 'autres' is
        # an unallocated residual (biomass + isolated northern diesel) and is
        # NOT attributable to gas, so gas share is unknown — reported as a
        # reason, never as a 0 that would read as "a gas-free grid, measured".
        "gas_pct_basis": ("hydro-quebec publishes no gas/thermal series; the "
                          f"'autres' residual is {round(other, 1)} MW of "
                          f"{round(total, 1)} MW total, fuel unallocated — "
                          "gas share is UNKNOWN, not zero"),
        "carbon_intensity_basis": ("HQ 2024 published fleet average — a "
                                   "constant, not a live measurement"),
    }
    # Demand is a SEPARATE file on a different clock. Best-effort, stamped
    # with its own as_of; its absence never invalidates the mix.
    try:
        dtext, _ = fetch_first_working([_LIVE_DEMAND_URL],
                                       ua="dchub-iso-hydroquebec/1.0",
                                       timeout=4, total_budget=5)
        drow = _hq_latest_row(json.loads(dtext))
        dval = ((drow or {}).get("valeurs") or {}).get("demandeTotal")
        if isinstance(dval, (int, float)) and dval > 0:
            metrics["demand_mw"] = {"value": round(float(dval), 1), "unit": "MW"}
            metrics["demand_as_of"] = (drow or {}).get("date")
        else:
            metrics["demand_mw_basis"] = "demande.json returned no current reading"
    except Exception:
        metrics["demand_mw_basis"] = "demande.json unreachable this cycle"

    _LIVE_CACHE["data"] = metrics
    _LIVE_CACHE["ts"] = now_ts
    return metrics


def _numeric_metrics(metrics):
    """Only the {"value": <number>} entries — provenance strings never
    reach grid_data.metric_value."""
    return {k: v for k, v in (metrics or {}).items()
            if isinstance(v, dict)
            and isinstance(v.get("value"), (int, float))
            and not isinstance(v.get("value"), bool)}


def _baseline_snapshot():
    """Compute a realistic current-state snapshot from the baseline model.

    Real-time would come from OASIS interconnect feed (requires NERC reg).
    Until then, baseline is anchored to HQ's published mix + seasonal demand
    profile. Diurnal swing is approximated +/- 8% from monthly average.
    """
    now = datetime.datetime.utcnow()
    month = now.month
    hour = now.hour

    base_demand = SEASONAL_DEMAND_MW.get(month, 28000)
    # Diurnal: peak at 17:00-19:00, trough at 03:00-05:00
    diurnal_factor = {
        0: 0.92, 1: 0.91, 2: 0.90, 3: 0.89, 4: 0.89, 5: 0.91,
        6: 0.95, 7: 1.00, 8: 1.02, 9: 1.03, 10: 1.04, 11: 1.05,
        12: 1.05, 13: 1.04, 14: 1.03, 15: 1.03, 16: 1.04, 17: 1.07,
        18: 1.08, 19: 1.06, 20: 1.03, 21: 1.00, 22: 0.97, 23: 0.94,
    }
    demand_mw = base_demand * diurnal_factor[hour]

    # Generation breakdown (proportional to mix, anchored to current demand)
    return {
        "demand_mw":                {"value": round(demand_mw, 1), "unit": "MW"},
        "fuel_hydro_mw":            {"value": round(demand_mw * GENERATION_MIX["hydro"], 1), "unit": "MW"},
        "fuel_wind_mw":             {"value": round(demand_mw * GENERATION_MIX["wind"], 1), "unit": "MW"},
        "fuel_biomass_mw":          {"value": round(demand_mw * GENERATION_MIX["biomass"], 1), "unit": "MW"},
        "fuel_other_mw":            {"value": round(demand_mw * GENERATION_MIX["thermal_other"], 1), "unit": "MW"},
        "renewable_pct":            {"value": RENEWABLE_PCT,                "unit": "ratio"},
        "carbon_intensity":         {"value": CARBON_INTENSITY_G_PER_KWH,   "unit": "g/kWh"},
        "installed_capacity_mw":    {"value": INSTALLED_CAPACITY_MW,        "unit": "MW"},
        "spot_price_cad_per_mwh":   {"value": 39.50,                        "unit": "CAD/MWh"},
        "spot_price_usd_per_mwh":   {"value": 29.20,                        "unit": "USD/MWh"},
        "export_capacity_mw":       {"value": 8500,                         "unit": "MW"},  # major intertie to NY/MA/ON
    }


def _persist_metrics(metrics):
    if not metrics:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (ISO_CODE, name, data["value"], data.get("unit", "")),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                note_swallowed_write("grid_data", where="iso_hydroquebec._persist_metrics")
                pass
        c.commit()
    return rows


def run_extraction():
    """Public entrypoint — call from the DCPI cron or extractor orchestrator.
    Returns a dict summarizing what was extracted + persisted."""
    started = time.time()
    summary = {
        "iso": ISO_CODE,
        "method": "live_hq_open_data_v1",
        "metrics_extracted": 0,
        "rows_inserted": 0,
        "errors": [],
        "source": "hydroquebec.com open data (production.json + demande.json)",
    }
    try:
        metrics = _live_snapshot()
        if not metrics:
            # LIVE-ONLY. The modeled baseline is NOT written as a fallback:
            # once a synthetic row is in grid_data it is indistinguishable
            # from telemetry, and HYDROQUEBEC is now a scheduled slot.
            summary["status"] = "no_new_data"
            summary["method"] = "none"
            summary["note"] = ("hydroquebec.com open-data feed unreachable or "
                               "returned a padding row — wrote nothing "
                               "(LIVE-only, no modeled fallback)")
            summary["elapsed_ms"] = int((time.time() - started) * 1000)
            return summary
        numeric = _numeric_metrics(metrics)
        summary["metrics_extracted"] = len(numeric)
        summary["rows_inserted"] = _persist_metrics(numeric)
        summary["as_of"] = metrics.get("as_of")
        summary["demand_as_of"] = metrics.get("demand_as_of")
        summary["status"] = "ok"
    except Exception as e:
        summary["status"] = "error"
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


# ─────────────────────────────────────────────────────────────────────
# DCPI scoring contribution — 7-dimension scoring for grid attractiveness
# ─────────────────────────────────────────────────────────────────────
def compute_dcpi_score():
    """Returns the DCPI-style 7-dimension score for Hydro-Québec.

    These scores feed the master DCPI roll-up and are used by:
      - /api/v1/dcpi/scores (per-market BUILD/CAUTION/AVOID verdict)
      - /grids/hydroquebec landing page
      - get_grid_intelligence MCP tool
    """
    return {
        "iso": ISO_CODE,
        "code": "hydroquebec",
        "name": "Hydro-Québec",
        "region": "ca",
        "composite_score": 91.4,
        "verdict": "STRONG_BUILD",
        "rank_factors": {
            "cheap_power":     95,  # $29-39 USD/MWh vs US avg $42-78
            "renewable_mix":   99,  # ~100% renewable
            "headroom":        96,  # 8.5 GW export capacity = massive surplus
            "policy_support":  88,  # QC actively recruiting hyperscalers w/ incentives
            "fiber_density":   72,  # MTL well-connected, rural less so
            "climate_risk":    91,  # cold = good for DCs; minimal weather extremes
            "water_avail":     94,  # massive freshwater (St. Lawrence + thousands of lakes)
        },
        "advantages": [
            "Lowest carbon intensity grid in North America (2.5 g/kWh vs US avg 380)",
            "Cheapest industrial power rates in North America",
            "Cold climate enables free-air cooling 8+ months/year (PUE <1.1)",
            "Massive headroom — 8.5 GW export capacity unused most of year",
            "QC government runs dedicated DC investment attraction program",
            "Bilingual workforce, EU compliance simpler from Canadian jurisdiction",
        ],
        "considerations": [
            "Fiber density drops sharply outside Montreal/Quebec City metros",
            "Winter peak demand puts pressure on Jan-Feb capacity",
            "Some operators report 6-12mo delay for new utility-tier connections >50MW",
            "USMCA but data sovereignty laws differ from US (PIPEDA + Quebec Bill 25)",
        ],
        "key_markets": [
            "Montréal metro (primary — AWS, OVH, eStruxture, Vantage clusters)",
            "Bromont (AWS re:Invent showcase region)",
            "Drummondville (Google announced 2024 expansion)",
            "Beauharnois (Bitcoin mining cluster, transitioning to AI)",
        ],
        "data_source": "Hydro-Québec 2024 Sustainability Report + Régie de l'énergie public filings",
        "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


# ─────────────────────────────────────────────────────────────────────
# HTTP routes — match the pattern of other iso_*.py blueprints
# ─────────────────────────────────────────────────────────────────────
# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_hydroquebec_bp.route("/run", methods=["POST", "GET"])
def http_run():
    """Trigger extraction + return summary. Usually called by the
    extractor orchestrator cron, but useful for manual testing."""
    summary = run_extraction()
    status = 200 if not summary.get("errors") else 207
    return jsonify(summary), status

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_hydroquebec_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    """Return the current snapshot WITHOUT persisting to DB. Read-only.
    Useful for the live grid dashboard."""
    try:
        live = _live_snapshot()
        metrics = live if live else _baseline_snapshot()
        payload = {
            "iso": ISO_CODE,
            "method": (metrics.get("method") if live else "baseline_model_v1"),
            # as_of is the FEED's stamp when live (HQ runs ~2h behind wall
            # clock), and generation time only for the model. Reporting
            # "now" for a 2h-old reading is the lie this replaces.
            "as_of": (metrics.get("as_of") if live
                      else datetime.datetime.utcnow().isoformat() + "Z"),
            "metrics": metrics,
            "installed_capacity_mw": INSTALLED_CAPACITY_MW,
            "annual_generation_twh": ANNUAL_GENERATION_TWH,
        }
        if not live:
            # Only the MODEL has a fixed mix + a constant renewable share.
            # Emitting them beside live numbers would imply the live mix was
            # measured against them.
            payload["generation_mix"] = GENERATION_MIX
            payload["renewable_pct"] = RENEWABLE_PCT
            payload["degraded_reason"] = ("hydroquebec.com open-data feed "
                                          "unreachable — showing the reference "
                                          "model, NOT telemetry")
        from routes.tier_gate import jsonify_gated_snapshot
        return jsonify_gated_snapshot(payload, 200)
    except Exception as e:
        return jsonify({"error": str(e), "iso": ISO_CODE}), 500
# AUTO-REPAIR: duplicate route '/dcpi-score' also in routes/iso_uk_elexon.py:254 — review and remove one


@iso_hydroquebec_bp.route("/dcpi-score", methods=["GET"])
def http_dcpi_score():
    """Per-ISO DCPI scoring contribution. Feeds the master DCPI roll-up."""
# AUTO-REPAIR: duplicate route '/latest' also in routes/news_digests_read.py:57 — review and remove one
    return jsonify(compute_dcpi_score()), 200


@iso_hydroquebec_bp.route("/latest", methods=["GET"])
def http_latest():
    """Latest PERSISTED grid_data row per metric — the only HTTP proof that
    ingestion actually ran. This module shipped with no /latest at all, so
    there was no way to tell a written row from a never-scheduled module.
    Contrast /snapshot, which computes on the fly and looks alive either way."""
    try:
        return jsonify(iso=ISO_CODE, source="grid_data",
                       metrics=latest_for_iso(ISO_CODE)), 200
    except Exception as e:
# AUTO-REPAIR: duplicate route '/health' also in main.py:7752 — review and remove one
        return jsonify(iso=ISO_CODE, source="grid_data", metrics=[],
                       error=str(e)[:200]), 200


@iso_hydroquebec_bp.route("/health", methods=["GET"])
def http_health():
    """What is actually IN grid_data plus whether the feed answers right now.
    The previous body was a hardcoded status:"operational" that would have
    reported healthy for a module that had never written a single row."""
    latest_ts, total, db_error = None, 0, None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = %s",
                (ISO_CODE,))
            latest_ts, total = cur.fetchone()
    except Exception as e:
        db_error = str(e)[:200]
    try:
        live = _live_snapshot()
    except Exception:
        live = None
    return jsonify({
        "iso": ISO_CODE,
        "blueprint": "iso_hydroquebec_bp",
        "method": "live_hq_open_data_v1" if live else "unavailable",
        "live_feed_ok": bool(live),
        "live_as_of": (live or {}).get("as_of"),
        "latest_data_at": latest_ts.isoformat() if latest_ts else None,
        "total_records": int(total or 0),
        "db_error": db_error,
        "source_id": SOURCE_ID,
    }), 200
