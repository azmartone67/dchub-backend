"""
iso_aeso_intl.py — AESO (Alberta) International ISO ingestion.

Phase ZZZZZ-round33 (2026-05-24). SECOND non-US ISO after Hydro-Québec.

NOTE: there's already a routes/iso_aeso.py for legacy US-context queries.
This module is the INTERNATIONAL-tagged version that feeds the
international DCPI roll-up + /grids/aeso landing page.

Why second international: cheapest grid power in North America (often
negative LMP), surge in crypto + AI mining, cold climate, low political
risk. Alberta is transitioning from coal-dominant to gas+wind.

DATA SOURCES:
  1. LIVE — AESO ETS Current Supply Demand report, tokenless:
       http://ets.aeso.ca/ets_web/ip/Market/Reports/CSDReportServlet
           ?contentType=csv
  2. AESO API (paid premium tier) — https://api.aeso.ca/
  3. OpenEI generation data — has Alberta scraped

★ 2026-07-28 (shell #41 WS2) — THE FEED WAS NEVER DEAD; THE PARSER WAS.

  routes/iso_orchestrator.py removed AESO on 2026-05-30 on the grounds that
  the extractor "persisted 0 rows since registration". Probed from this
  shell today, no token: the servlet returns HTTP 200, 9,680 B, in 0.6s,
  "Last Update : Jul 28, 2026 21:51".

  What actually failed: routes/iso_aeso.py handed the body to
  _iso_common.parse_csv_numeric_columns, which runs csv.DictReader over it.
  The body is SECTIONED, not tabular — DictReader sees one header row and
  264 data rows, takes the LAST one (a per-asset line, e.g. 'Whitecourt
  Power (EAGL)') and returns {}. parse_json_numeric then raises
  JSONDecodeError and also returns {}. 0 metrics → 0 rows → status "ok".
  A parser bug that reported success.

  Layout, verified against the live body:
    2-col key/value block — "Alberta Internal Load (AIL)","11301"
    4-col fuel-group block — GROUP, MC (max capability), TNG (total net
      generation), DCR (dispatched contingency reserve), terminated by a
      "TOTAL" row; then per-asset sections reusing the same 4-col shape.
    Anchoring on the known GROUP names and stopping at the FIRST 4-col
    TOTAL keeps per-asset rows out. At probe time the group TNGs summed to
    11,797 == the report's own TOTAL row, exactly.
    COGENERATION 4,424 / COMBINED CYCLE 3,210 / WIND 2,383 / GAS FIRED
    STEAM 984 / HYDRO 469 / OTHER 260 / SIMPLE CYCLE 66 / SOLAR 1 /
    ENERGY STORAGE 0  → renewable 24.2%, gas 73.6%.

  ★ The live report has NO COAL LINE AT ALL — Alberta finished the phase-
    out. The GENERATION_MIX constant below still asserts 9.4% coal and is
    factually stale; it is now reference-only and no longer written.

  This module is LIVE-ONLY (method="live_aeso_csd_v1"). It is the module
  that actually owns /api/v1/iso/aeso (main.py registers it twice — at
  /aeso-intl and again as "iso_aeso_canonical" at /aeso), so the parser
  belongs HERE. routes/iso_aeso.py is never imported by main.py and stays
  dead; registering it would collide with the canonical alias.
"""
import os
import csv
import io
import time
import datetime
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._iso_common import fetch_first_working, latest_for_iso
from routes._swallowed_writes import note_swallowed_write

iso_aeso_intl_bp = Blueprint("iso_aeso_intl", __name__,
                              url_prefix="/api/v1/iso/aeso-intl")
SOURCE_ID = "iso-aeso-intl-baseline"
ISO_CODE = "AESO"


# AESO 2024 generation mix (published)
# Source: aeso.ca/grid/grid-snapshot/
GENERATION_MIX = {
    "natural_gas":      0.586,   # gas-dominant since coal phase-out accelerated
    "wind":             0.184,   # rapidly growing
    "coal":             0.094,   # phasing out fully by 2030
    "hydro":            0.062,
    "solar":            0.038,   # newly added 2023-2024 utility scale
    "biomass":          0.020,
    "imports":          0.012,
    "other":            0.004,
}

INSTALLED_CAPACITY_MW = 17_200
RENEWABLE_PCT         = 0.284   # wind + solar + hydro + biomass
CARBON_INTENSITY_G_PER_KWH = 480  # gas + remaining coal

# Seasonal demand pattern (Alberta winter peak heating)
SEASONAL_DEMAND_MW = {
    1: 11200, 2: 10800, 3: 9700, 4: 9000, 5: 9200, 6: 9800,
    7: 10500, 8: 10300, 9: 9400, 10: 9700, 11: 10500, 12: 11000,
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
# LIVE feed — AESO ETS Current Supply Demand (2026-07-28, shell #41 WS2)
# ─────────────────────────────────────────────────────────────────────
_LIVE_URLS = [
    "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSDReportServlet?contentType=csv",
]
# Fuel class per AESO reporting group. COGENERATION is Alberta industrial
# GAS cogen — classing it as "other" would understate gas share by ~38
# points. COAL is retained as a key even though the phase-out means it no
# longer appears, so a restart would be picked up rather than dropped.
_AESO_GROUP_FUEL = {
    "COGENERATION": "gas", "COMBINED CYCLE": "gas",
    "GAS FIRED STEAM": "gas", "SIMPLE CYCLE": "gas", "DUAL FUEL": "gas",
    "COAL": "coal", "WIND": "wind", "SOLAR": "solar", "HYDRO": "hydro",
    "OTHER": "other", "ENERGY STORAGE": "storage",
}
_LIVE_CACHE = {"data": None, "ts": 0.0}
_LIVE_TTL = 300


def _parse_aeso_csd(text):
    """AESO CSD report → (last_update, ail_mw, {GROUP: tng_mw}, total_tng).

    Returns None if the fuel-group block is missing OR its parts do not sum
    to the report's own TOTAL row within 1 MW. That cross-check is the
    guard: if AESO adds a group name we do not know, the sum diverges and
    we publish NOTHING rather than a mix that is quietly missing a fuel.
    """
    last_update, ail, groups, total = None, None, {}, None
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        c0 = (row[0] or "").strip()
        if len(row) == 1 and c0.startswith("Last Update"):
            last_update = c0.split(":", 1)[1].strip() if ":" in c0 else c0
        elif len(row) == 2 and c0 == "Alberta Internal Load (AIL)":
            try:
                ail = float(row[1])
            except (TypeError, ValueError):
                ail = None
        elif len(row) == 4 and total is None:
            # First 4-column block only. The per-asset sections further down
            # reuse this shape, which is exactly what fooled the old parser.
            key = c0.upper()
            if key in _AESO_GROUP_FUEL:
                try:
                    groups[key] = float(row[2])      # TNG column
                except (TypeError, ValueError):
                    pass
            elif key == "TOTAL" and groups:
                try:
                    total = float(row[2])
                except (TypeError, ValueError):
                    total = None
    if not groups or not total or total <= 0:
        return None
    if abs(sum(groups.values()) - total) > 1.0:
        return None      # unrecognised group appeared — refuse a partial mix
    return last_update, ail, groups, total


def _live_snapshot():
    """LIVE AESO snapshot, or None when the feed is unreachable/unparseable.

    Same flat {metric: {"value", "unit"}} shape as _baseline_snapshot(),
    plus string provenance keys that callers filter before persisting.
    LIVE-ONLY. 5-min cache so /snapshot, /latest, /comparison and the
    orchestrator share ONE fetch.
    """
    now_ts = time.time()
    if _LIVE_CACHE["data"] is not None and (now_ts - _LIVE_CACHE["ts"]) < _LIVE_TTL:
        return _LIVE_CACHE["data"]
    try:
        text, _url = fetch_first_working(_LIVE_URLS, ua="dchub-iso-aeso/1.0",
                                         timeout=6, total_budget=8)
        parsed = _parse_aeso_csd(text)
    except Exception:
        return None
    if not parsed:
        return None
    last_update, ail, groups, total = parsed

    def _cls(kind):
        return sum(v for g, v in groups.items() if _AESO_GROUP_FUEL[g] == kind)

    gas, wind, solar = _cls("gas"), _cls("wind"), _cls("solar")
    hydro, coal = _cls("hydro"), _cls("coal")
    other, storage = _cls("other"), _cls("storage")
    renew = wind + solar + hydro

    metrics = {
        "generation_total_mw":   {"value": round(total, 1), "unit": "MW"},
        "fuel_gas_mw":           {"value": round(gas, 1), "unit": "MW"},
        "fuel_wind_mw":          {"value": round(wind, 1), "unit": "MW"},
        "fuel_solar_mw":         {"value": round(solar, 1), "unit": "MW"},
        "fuel_hydro_mw":         {"value": round(hydro, 1), "unit": "MW"},
        "fuel_coal_mw":          {"value": round(coal, 1), "unit": "MW"},
        "fuel_other_mw":         {"value": round(other, 1), "unit": "MW"},
        "fuel_storage_mw":       {"value": round(storage, 1), "unit": "MW"},
        "renewable_pct":         {"value": round(100.0 * renew / total, 1), "unit": "pct"},
        "gas_pct":               {"value": round(100.0 * gas / total, 1), "unit": "pct"},
        "installed_capacity_mw": {"value": INSTALLED_CAPACITY_MW, "unit": "MW"},
        "carbon_intensity":      {"value": CARBON_INTENSITY_G_PER_KWH, "unit": "g/kWh"},
        "method":     "live_aeso_csd_v1",
        "as_of":      last_update,      # AESO's own "Last Update", Alberta time
        "source_url": _LIVE_URLS[0],
        "renewable_pct_basis": (
            f"(WIND+SOLAR+HYDRO)/TOTAL net generation = "
            f"{round(renew, 1)}/{round(total, 1)} MW. ENERGY STORAGE "
            f"({round(storage, 1)} MW) stays in the denominator because "
            f"AESO's own TOTAL row includes it; biomass is inside OTHER "
            f"and is not counted as renewable"),
        "gas_pct_basis": (
            "COGENERATION+COMBINED CYCLE+GAS FIRED STEAM+SIMPLE CYCLE. AESO "
            "publishes no fuel split for COGENERATION, so this is an UPPER "
            "BOUND on gas, not a measured share"),
        "coal_basis": (
            "AESO's live report carries no coal line — Alberta completed the "
            "phase-out; 0 MW here is measured absence, not a missing feed"),
        "carbon_intensity_basis": (
            "AESO 2024 published fleet average — a constant, not a live "
            "measurement, and stale w.r.t. the completed coal phase-out"),
    }
    if isinstance(ail, (int, float)) and ail > 0:
        # AIL is Alberta Internal Load = demand. Distinct from TOTAL net
        # generation (11,301 vs 11,797 at probe time — the delta is net
        # interchange to BC/MT/SK).
        metrics["demand_mw"] = {"value": round(float(ail), 1), "unit": "MW"}
    else:
        metrics["demand_mw_basis"] = ("Alberta Internal Load (AIL) row absent "
                                      "from this report")

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
    now = datetime.datetime.utcnow()
    month = now.month
    hour = now.hour

    base = SEASONAL_DEMAND_MW.get(month, 10000)
    # Diurnal: peak at 17-19 (Alberta), trough 03-04
    diurnal = {
        0: 0.92, 1: 0.91, 2: 0.90, 3: 0.89, 4: 0.89, 5: 0.91,
        6: 0.94, 7: 0.98, 8: 1.00, 9: 1.01, 10: 1.02, 11: 1.03,
        12: 1.04, 13: 1.03, 14: 1.02, 15: 1.02, 16: 1.04, 17: 1.07,
        18: 1.08, 19: 1.06, 20: 1.02, 21: 1.00, 22: 0.97, 23: 0.94,
    }
    demand_mw = base * diurnal[hour]

    return {
        "demand_mw":                {"value": round(demand_mw, 1), "unit": "MW"},
        "fuel_gas_mw":              {"value": round(demand_mw * GENERATION_MIX["natural_gas"], 1), "unit": "MW"},
        "fuel_wind_mw":             {"value": round(demand_mw * GENERATION_MIX["wind"], 1), "unit": "MW"},
        "fuel_coal_mw":             {"value": round(demand_mw * GENERATION_MIX["coal"], 1), "unit": "MW"},
        "fuel_hydro_mw":            {"value": round(demand_mw * GENERATION_MIX["hydro"], 1), "unit": "MW"},
        "fuel_solar_mw":            {"value": round(demand_mw * GENERATION_MIX["solar"], 1), "unit": "MW"},
        "renewable_pct":            {"value": RENEWABLE_PCT,                "unit": "ratio"},
        "carbon_intensity":         {"value": CARBON_INTENSITY_G_PER_KWH,   "unit": "g/kWh"},
        "installed_capacity_mw":    {"value": INSTALLED_CAPACITY_MW,        "unit": "MW"},
        "spot_price_cad_per_mwh":   {"value": 32.40,                        "unit": "CAD/MWh"},
        "spot_price_usd_per_mwh":   {"value": 24.00,                        "unit": "USD/MWh"},
        "negative_lmp_hours_30d":   {"value": 47,                           "unit": "hours"},
    }


def _persist_metrics(metrics):
    if not metrics: return 0
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
                if cur.rowcount > 0: rows += 1
            except Exception:
                note_swallowed_write("grid_data", where="iso_aeso_intl._persist_metrics")
                pass
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_aeso_csd_v1",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": "AESO ETS Current Supply Demand report (tokenless CSV)",
    }
    try:
        metrics = _live_snapshot()
        if not metrics:
            # LIVE-ONLY: no modeled fallback. AESO now runs on the shared
            # 15-min cadence, and the modeled mix still asserts 9.4% coal
            # for a province that finished its phase-out.
            summary["status"] = "no_new_data"
            summary["method"] = "none"
            summary["note"] = ("ets.aeso.ca unreachable or CSD layout changed "
                               "(fuel groups did not sum to the report TOTAL) "
                               "— wrote nothing")
            summary["elapsed_ms"] = int((time.time() - started) * 1000)
            return summary
        numeric = _numeric_metrics(metrics)
        summary["metrics_extracted"] = len(numeric)
        summary["rows_inserted"] = _persist_metrics(numeric)
        summary["as_of"] = metrics.get("as_of")
        summary["status"] = "ok"
    except Exception as e:
        summary["status"] = "error"
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


def compute_dcpi_score():
    return {
        "iso": ISO_CODE,
        "code": "aeso-intl",
        "name": "AESO (Alberta, Canada)",
        "region": "ca",
        "composite_score": 82.7,
        "verdict": "BUILD",
        "rank_factors": {
            "cheap_power":     92,  # cheapest grid power in NA, negative LMP common
            "renewable_mix":   42,  # still gas+coal heavy; improving rapidly
            "headroom":        88,  # plenty of generation capacity, growing solar/wind
            "policy_support":  78,  # AB government active on AI/DC attraction
            "fiber_density":   68,  # Calgary/Edmonton OK; rural sparse
            "climate_risk":    95,  # cold climate, very low natural disaster
            "water_avail":     72,  # rivers + groundwater
        },
        "advantages": [
            "Cheapest grid power in North America — negative LMP 47 hours/30d typical",
            "Cold climate enables free-air cooling 9+ months/yr",
            "Pro-AI, pro-crypto regulatory stance",
            "USMCA + Canadian data residency",
            "Surplus generation — capacity headroom for 2GW+ new DC load",
        ],
        "considerations": [
            "Renewable mix still <30% — carbon intensity ~480 g/kWh (vs HQ 2.5)",
            "Coal phase-out by 2030 may temporarily tighten supply 2027-2029",
            "Fiber density drops sharply outside Calgary/Edmonton metros",
            "Less mature DC ecosystem than QC — fewer existing colocations",
        ],
        "key_markets": [
            "Calgary metro (primary — most existing DC capacity)",
            "Edmonton (newer hyperscale interest)",
            "Drumheller (Hut 8 crypto cluster, AI transition)",
            "Medicine Hat (cheapest land + power)",
        ],
        "data_source": "AESO 2024 grid snapshot + ETS public reports",
        "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_aeso_intl_bp.route("/run", methods=["POST", "GET"])
def http_run():
    summary = run_extraction()
    return jsonify(summary), 200 if not summary.get("errors") else 207

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_aeso_intl_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    from routes.tier_gate import jsonify_gated_snapshot
    return jsonify_gated_snapshot({
        "iso": ISO_CODE,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "method": "baseline_model_v1",
        "metrics": _baseline_snapshot(),
        "generation_mix": GENERATION_MIX,
        "installed_capacity_mw": INSTALLED_CAPACITY_MW,
        "renewable_pct": RENEWABLE_PCT,
    }, 200)
# AUTO-REPAIR: duplicate route '/latest' also in routes/news_digests_read.py:57 — review and remove one


@iso_aeso_intl_bp.route("/latest", methods=["GET"])
def http_latest():
    # 2026-05-30: parity alias for the other ISOs' /latest.
    # Served at /api/v1/iso/aeso/latest (canonical alias) + /aeso-intl/latest.
    #
    # ★ 2026-07-28: this used to return _baseline_snapshot() — a payload
    # computed on the fly, so it looked identical whether ingestion had ever
    # written a row or not. iso_bpa/iso_tva /latest read grid_data; this now
    # does the same, so an empty list means "nothing has been ingested",
    # which is the answer the caller was asking for.
    try:
        return jsonify({
            "iso": ISO_CODE,
            "source": "grid_data",
            "metrics": latest_for_iso(ISO_CODE),
        }), 200
    except Exception as e:
        return jsonify({"iso": ISO_CODE, "source": "grid_data",
# AUTO-REPAIR: duplicate route '/dcpi-score' also in routes/iso_uk_elexon.py:254 — review and remove one
                        "metrics": [], "error": str(e)[:200]}), 200


@iso_aeso_intl_bp.route("/dcpi-score", methods=["GET"])
# AUTO-REPAIR: duplicate route '/health' also in main.py:7900 — review and remove one
def http_dcpi_score():
    return jsonify(compute_dcpi_score()), 200


@iso_aeso_intl_bp.route("/health", methods=["GET"])
def http_health():
    """Real state: rows in grid_data + whether the CSD feed answers now.
    The previous body was a hardcoded status:"operational"."""
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
        "blueprint": "iso_aeso_intl_bp",
        "method": "live_aeso_csd_v1" if live else "unavailable",
        "live_feed_ok": bool(live),
        "live_as_of": (live or {}).get("as_of"),
        "latest_data_at": latest_ts.isoformat() if latest_ts else None,
        "total_records": int(total or 0),
        "db_error": db_error,
        "source_id": SOURCE_ID,
    }), 200
