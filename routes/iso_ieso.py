"""iso_ieso.py — IESO (Ontario) grid data extractor.

Independent Electricity System Operator for Ontario, Canada. Covers
~24-25 GW summer peak across Toronto / Mississauga / Markham / Ottawa —
a top-tier North American data-center market with rapid AI build-out.

────────────────────────────────────────────────────────────────────────
2026-05-31 FIX (#100, ISO coverage expansion) — switched from a (now dead)
live-CSV scraper to an HONEST modeled baseline, mirroring iso_aeso_intl.py
and iso_hydroquebec.py (the other two Canadian operators).

★ 2026-07-28 (shell #41 WS2) — THE ROOT CAUSE BELOW WAS WRONG. IESO IS LIVE.

  The 2026-05-31 block claimed "that entire public reports host sits behind
  an Okta SAML SSO gateway" and that probes of reports-public.ieso.ca
  "return 404 or HTML". Both are false. It probed reports.ieso.ca; the
  tokenless host is reports-PUBLIC.ieso.ca. Probed from this shell today,
  no credentials of any kind:

    https://reports-public.ieso.ca/public/GenOutputCapability/
            PUB_GenOutputCapability.xml
      → HTTP 200, 851,870 B, fetched in 0.9s, parsed in 0.02s.
        <Date>2026-07-28</Date>, hours 1..22, 184-187 generators per hour,
        <FuelType> ∈ {NUCLEAR, GAS, HYDRO, WIND, SOLAR, BIOFUEL, OTHER}.
        HE22: NUCLEAR 10,298 / GAS 5,218 / HYDRO 4,256 / WIND 1,402 /
        SOLAR 0 / BIOFUEL 28 / OTHER 232, total 21,434 MW
        → renewable (hydro+wind+solar) 26.4%, gas 24.3%.

  The sibling PUB_GenOutputbyFuelHourly.xml is the WHOLE YEAR (4.6 MB) and
  has NO daily variant — the directory offers only annual + _2026_vNNN
  files, so it cannot fit fetch_first_working's 12s budget. The per-unit
  GenOutputCapability file is the current DAY only, lives at a self-updating
  URL, and rolls up to the same fuel totals. That is the one we use.

  Ontario is indeed outside EIA-930 — that part was right, and irrelevant
  now.

  This module is now LIVE-ONLY (method="live_ieso_genoutputcapability_v1").
  _baseline_snapshot() is retained as a documented reference model but is NO
  LONGER WRITTEN to grid_data: IESO is a ranked, live-advertised operator,
  and a modeled row in that table is indistinguishable from telemetry.

  NOT ingested: demand. GenOutputCapability reports GENERATION by unit;
  Ontario demand is generation net of the NY/MI/MN/QC interties and is not
  in this file, so demand_mw is deliberately ABSENT rather than aliased to
  total generation.

──────────────── superseded 2026-05-31 analysis, kept for the record ───────
ROOT CAUSE of IESO persisting 0 rows in grid_data:
  The old extractor fetched reports.ieso.ca/public/.../PUB_*.csv. As of
  2026-05-31 that entire public reports host sits behind an Okta SAML SSO
  gateway: every request returns an HTML auth-redirect page (a <form> POST
  to gateway.ieso.ca/.../sso/saml with a base64 SAMLRequest), NOT CSV. So
  fetch_first_working got 200 + HTML, the CSV/JSON numeric parsers found no
  data → 0 metrics → 0 rows. IESO locked down its formerly-public reports
  site behind authentication.

  Ontario is also OUTSIDE EIA's coverage (EIA-930 is US balancing
  authorities only — no MISO-style "respondent=IESO"), so the EIA-930
  pattern that fixed MISO/PJM/BPA is NOT available here. Probes of every
  alternate public host (reports-public.ieso.ca, www.ieso.ca media paths)
  return 404 or HTML, not a fetchable data file.   ← WRONG, see above

  CANONICAL-COUNT IMPLICATION: the "10 live operators" framing in
  iso_orchestrator.py /health is now strictly "8 live + 2 modeled" among
  its non-EIA-BA entries — IESO joins AESO as modeled. (AESO already left
  the orchestrator on 2026-05-30 and runs via iso_aeso_intl; IESO stays
  registered here because it still produces real grid_data rows — just
  modeled, not scraped.) Flagged in the REPORT for the user to update any
  externally-published "10 live ISO feeds" copy.

  Phase 2 (future): IESO offers an authenticated reports API + the
  gridwatch/public datafeeds; once IESO API credentials are provisioned in
  Railway env, this can be upgraded to a live feed.
────────────────────────────────────────────────────────────────────────
"""
import os
import time
import datetime
import xml.etree.ElementTree as ET
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._iso_common import fetch_first_working, scrub_secrets
from routes._swallowed_writes import note_swallowed_write

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass


iso_ieso_bp = Blueprint("iso_ieso", __name__, url_prefix="/api/v1/iso/ieso")
SOURCE_ID = "iso-ieso-baseline"
ISO_CODE = "IESO"


# ─────────────────────────────────────────────────────────────────────
# Baseline generation model — anchored to IESO's published 2024 mix.
# Source: IESO 2024 Year in Review + 2024 Reliability/Outlook reports
#   (https://www.ieso.ca/en/Power-Data / Year-End Data).
# Ontario's grid is nuclear-baseload dominant and very stable YoY.
# ─────────────────────────────────────────────────────────────────────
#
#   Installed capacity:   ~42,000 MW
#   Summer peak demand:   ~24,000-25,000 MW
#   2024 generation mix (energy share, approx):
#     Nuclear  ~52%   (Bruce + Darlington + Pickering — baseload)
#     Hydro    ~24%
#     Natural gas ~12%
#     Wind     ~8%
#     Biofuel/biomass ~0.3%
#     Solar (grid-connected) ~0.4%   (most Ontario solar is embedded/behind-meter)
#   Renewable share (hydro+wind+solar+bio): ~33%
#   Carbon intensity:    ~35 g CO2/kWh (nuclear+hydro dominant — among the
#                        lowest in North America; tracks HQ-low, well below US avg)
#   HOEP (Hourly Ontario Energy Price), 2024 avg: ~CAD $30/MWh (~USD $22/MWh)

GENERATION_MIX = {
    "nuclear":     0.520,
    "hydro":       0.240,
    "natural_gas": 0.120,
    "wind":        0.080,
    "biofuel":     0.003,
    "solar":       0.004,
    "imports":     0.033,   # net of interties (NY/MI/MN/QC) — Ontario often imports off-peak
}

INSTALLED_CAPACITY_MW = 42_000
RENEWABLE_PCT         = 0.327   # hydro + wind + solar + biofuel
CARBON_INTENSITY_G_PER_KWH = 35   # nuclear + hydro dominant

# Ontario seasonal demand (MW) — summer cooling peak (Jul/Aug) is the annual
# peak; secondary winter heating bump. Anchored to IESO 2024 monthly peaks.
SEASONAL_DEMAND_MW = {
    1: 19500, 2: 19200, 3: 17800, 4: 16500, 5: 17000, 6: 19500,
    7: 22500, 8: 22000, 9: 18500, 10: 17000, 11: 18000, 12: 19200,
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
# LIVE feed — reports-public.ieso.ca (2026-07-28, shell #41 WS2)
# ─────────────────────────────────────────────────────────────────────
_IESO_URLS = [
    "https://reports-public.ieso.ca/public/GenOutputCapability/"
    "PUB_GenOutputCapability.xml",
]
# Scoreboard-comparable renewable = hydro+wind+solar, matching
# iso_eu_entsoe._RENEWABLE_CATS. BIOFUEL is reported separately and is NOT
# counted, so it can never silently inflate the ranking.
_IESO_RENEWABLE_FUELS = {"HYDRO", "WIND", "SOLAR"}
_LIVE_CACHE = {"data": None, "ts": 0.0}
_LIVE_TTL = 300


def _ln(tag):
    """Local-name of a namespaced XML tag — IMODocument declares a default
    xmlns, so every tag arrives as {http://www.theIMO.com/schema}Foo."""
    return tag.rsplit("}", 1)[-1]


def _parse_ieso_gen_xml(xml_text):
    """PUB_GenOutputCapability → (date, hour, {FUEL: mw}) for the last
    COMPLETE hour, or None. Never returns a partially-written hour.

    The report is written per generator, so the newest hour is only whole
    once every unit has reported. Gate: take the highest hour whose
    reporting-unit count is >= 80% of the day's maximum. At probe time the
    counts were a tight 184-187 across hours 1-22, so a genuinely half-
    written hour is well under the gate and gets skipped rather than
    published as a fuel mix that never existed.
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    day = None
    by_hour, units_per_hour = {}, {}
    for el in root.iter():
        ln = _ln(el.tag)
        if ln == "Date" and day is None:
            day = (el.text or "").strip()
        elif ln == "Generator":
            fuel, outs = None, []
            for child in el:
                cln = _ln(child.tag)
                if cln == "FuelType":
                    fuel = (child.text or "").strip().upper()
                elif cln == "Outputs":
                    for o in child:
                        h = mw = None
                        for f in o:
                            fln = _ln(f.tag)
                            if fln == "Hour":
                                try:
                                    h = int((f.text or "").strip())
                                except (TypeError, ValueError):
                                    h = None
                            elif fln == "EnergyMW":
                                try:
                                    mw = float((f.text or "").strip())
                                except (TypeError, ValueError):
                                    mw = None
                        if h is not None and mw is not None:
                            outs.append((h, mw))
            key = fuel or "UNKNOWN"
            for h, mw in outs:
                bucket = by_hour.setdefault(h, {})
                bucket[key] = bucket.get(key, 0.0) + mw
                units_per_hour[h] = units_per_hour.get(h, 0) + 1
    if not by_hour or not units_per_hour:
        return None
    max_units = max(units_per_hour.values())
    complete = [h for h in sorted(by_hour)
                if units_per_hour[h] >= 0.8 * max_units]
    if not complete:
        return None
    hour = complete[-1]
    mix = {k: v for k, v in by_hour[hour].items() if v}
    if not mix or sum(mix.values()) <= 0:
        return None
    return day, hour, mix


def _live_snapshot():
    """LIVE IESO snapshot, or None when the feed is unreachable/incomplete.

    Same flat {metric: {"value", "unit"}} shape as _baseline_snapshot(),
    plus string provenance keys that callers filter before persisting.
    LIVE-ONLY — never degrades to the model. 5-min cache so /snapshot,
    /comparison and the orchestrator share ONE 851 KB fetch.
    """
    now_ts = time.time()
    if _LIVE_CACHE["data"] is not None and (now_ts - _LIVE_CACHE["ts"]) < _LIVE_TTL:
        return _LIVE_CACHE["data"]
    try:
        text, _url = fetch_first_working(_IESO_URLS, ua="dchub-iso-ieso/1.0",
                                         timeout=6, total_budget=9)
        parsed = _parse_ieso_gen_xml(text)
    except Exception:
        return None
    if not parsed:
        return None
    day, hour, mix = parsed
    total = sum(mix.values())
    renew = sum(v for k, v in mix.items() if k in _IESO_RENEWABLE_FUELS)
    gas = mix.get("GAS", 0.0)

    metrics = {
        "generation_total_mw":   {"value": round(total, 1), "unit": "MW"},
        "renewable_pct":         {"value": round(100.0 * renew / total, 1), "unit": "pct"},
        "gas_pct":               {"value": round(100.0 * gas / total, 1), "unit": "pct"},
        "installed_capacity_mw": {"value": INSTALLED_CAPACITY_MW, "unit": "MW"},
        "method":     "live_ieso_genoutputcapability_v1",
        # HE = hour ending, Ontario EPT. Naming the convention matters: HE22
        # is the hour ENDING at 22:00 local, not the hour starting at 22:00.
        "as_of":      f"{day} HE{hour:02d} America/Toronto (hour ending)",
        "source_url": _IESO_URLS[0],
        "renewable_pct_basis": (f"(HYDRO+WIND+SOLAR)/total generation = "
                                f"{round(renew, 1)}/{round(total, 1)} MW; "
                                f"BIOFUEL excluded"),
        # HONEST NUMBERS: this is generation, not demand. Do not let a
        # consumer read generation_total_mw as load.
        "demand_mw_basis": ("not published in GenOutputCapability — Ontario "
                            "demand is generation net of the NY/MI/MN/QC "
                            "interties, which this report does not carry"),
    }
    for fuel, mw in sorted(mix.items()):
        metrics[f"fuel_{fuel.lower()}_mw"] = {"value": round(mw, 1), "unit": "MW"}

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
    """Realistic current-state snapshot from the baseline model.

    Live values would come from IESO's authenticated reports API (Phase 2).
    Until then this is anchored to IESO's published 2024 mix + Ontario's
    seasonal demand profile, with a diurnal swing applied. Far more accurate
    for a nuclear-baseload system than any generic grid average.
    """
    now = datetime.datetime.utcnow()
    month = now.month
    hour = now.hour

    base = SEASONAL_DEMAND_MW.get(month, 19000)
    # Diurnal: Ontario peaks 17:00-19:00 local, trough 03:00-05:00.
    diurnal = {
        0: 0.92, 1: 0.91, 2: 0.90, 3: 0.89, 4: 0.89, 5: 0.91,
        6: 0.94, 7: 0.98, 8: 1.00, 9: 1.01, 10: 1.02, 11: 1.03,
        12: 1.04, 13: 1.03, 14: 1.02, 15: 1.02, 16: 1.04, 17: 1.07,
        18: 1.08, 19: 1.06, 20: 1.02, 21: 1.00, 22: 0.97, 23: 0.94,
    }
    demand_mw = base * diurnal[hour]

    return {
        "demand_mw":                {"value": round(demand_mw, 1), "unit": "MW"},
        "fuel_nuclear_mw":          {"value": round(demand_mw * GENERATION_MIX["nuclear"], 1), "unit": "MW"},
        "fuel_hydro_mw":            {"value": round(demand_mw * GENERATION_MIX["hydro"], 1), "unit": "MW"},
        "fuel_gas_mw":              {"value": round(demand_mw * GENERATION_MIX["natural_gas"], 1), "unit": "MW"},
        "fuel_wind_mw":             {"value": round(demand_mw * GENERATION_MIX["wind"], 1), "unit": "MW"},
        "fuel_solar_mw":            {"value": round(demand_mw * GENERATION_MIX["solar"], 1), "unit": "MW"},
        "renewable_pct":            {"value": RENEWABLE_PCT,                "unit": "ratio"},
        "carbon_intensity":         {"value": CARBON_INTENSITY_G_PER_KWH,   "unit": "g/kWh"},
        "installed_capacity_mw":    {"value": INSTALLED_CAPACITY_MW,        "unit": "MW"},
        "spot_price_cad_per_mwh":   {"value": 30.00,                        "unit": "CAD/MWh"},  # HOEP 2024 avg
        "spot_price_usd_per_mwh":   {"value": 22.20,                        "unit": "USD/MWh"},
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
                note_swallowed_write("grid_data", where="iso_ieso._persist_metrics")
                pass
        c.commit()
    return rows


def run_extraction():
    """Orchestrator entry — compute the IESO baseline snapshot and persist it.
    Returns a result dict compatible with the orchestrator's expected shape
    (includes rows_inserted + status)."""
    started = time.time()
    summary = {
        "iso": ISO_CODE,
        "method": "live_ieso_genoutputcapability_v1",
        "metrics_extracted": 0,
        "rows_inserted": 0,
        "source": ("reports-public.ieso.ca GenOutputCapability "
                   "(per-unit hourly output + FuelType)"),
    }
    try:
        metrics = _live_snapshot()
        if not metrics:
            # LIVE-ONLY. The modeled baseline is NOT written as a fallback —
            # IESO is a ranked operator and a synthetic row in grid_data is
            # indistinguishable from telemetry once written.
            summary["status"] = "no_new_data"
            summary["method"] = "none"
            summary["note"] = ("reports-public.ieso.ca unreachable or no "
                               "complete hour yet — wrote nothing (LIVE-only, "
                               "no modeled fallback)")
            _heartbeat(SOURCE_ID, status="failure",
                       duration_ms=int((time.time() - started) * 1000),
                       error=summary["note"])
            summary["duration_ms"] = int((time.time() - started) * 1000)
            return summary
        numeric = _numeric_metrics(metrics)
        summary["metrics_extracted"] = len(numeric)
        rows = _persist_metrics(numeric)
        summary["rows_inserted"] = rows
        summary["as_of"] = metrics.get("as_of")
        summary["status"] = "ok"
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows,
                   duration_ms=int((time.time() - started) * 1000),
                   metadata={"method": "live_ieso_genoutputcapability_v1",
                             "as_of": metrics.get("as_of"),
                             "metrics_extracted": len(numeric)})
    except Exception as e:
        summary["status"] = "error"
        summary["error"] = scrub_secrets(f"{type(e).__name__}: {e}")
        _heartbeat(SOURCE_ID, status="failure",
                   duration_ms=int((time.time() - started) * 1000),
                   error=summary["error"])
    summary["duration_ms"] = int((time.time() - started) * 1000)
    return summary


def latest_for_iso(iso):
    """Latest metric value per metric_name for IESO (reads grid_data)."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT metric_name, metric_value, unit, timestamp
               FROM grid_data WHERE iso = %s
               ORDER BY timestamp DESC LIMIT 200""",
            (iso,),
        )
        rows = cur.fetchall()
    by = {}
    for n, v, u, ts in rows:
        if n not in by:
            by[n] = {"metric": n, "value": v, "unit": u,
                     "timestamp": ts.isoformat() if ts else None}
    return list(by.values())


@iso_ieso_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one
@iso_ieso_bp.route("/snapshot", methods=["GET"])
def snapshot():
    # Read-only current snapshot WITHOUT persisting. Serves the LIVE feed
    # when it answers; falls back to the reference model and SAYS SO.
    live = _live_snapshot()
    metrics = live if live else _baseline_snapshot()
    payload = {
        "iso": ISO_CODE,
        "method": (metrics.get("method") if live else "baseline_model_v1"),
        # Live as_of is IESO's own hour-ending stamp, not wall clock.
        "as_of": (metrics.get("as_of") if live
                  else datetime.datetime.utcnow().isoformat() + "Z"),
        "metrics": metrics,
        "installed_capacity_mw": INSTALLED_CAPACITY_MW,
    }
    if not live:
        # The fixed mix + constant renewable share belong to the MODEL only.
        payload["generation_mix"] = GENERATION_MIX
        payload["renewable_pct"] = RENEWABLE_PCT
        payload["degraded_reason"] = ("reports-public.ieso.ca unreachable or "
                                      "no complete hour — showing the reference "
                                      "model, NOT telemetry")
    return jsonify(payload), 200

# AUTO-REPAIR: duplicate route '/latest' also in routes/news_digests_read.py:57 — review and remove one

@iso_ieso_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso=ISO_CODE, method="baseline_model_v1",
                   metrics=latest_for_iso(ISO_CODE)), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:7949 — review and remove one


@iso_ieso_bp.route("/health", methods=["GET"])
def health():
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = %s",
            (ISO_CODE,),
        )
        latest_ts, total = cur.fetchone()
    try:
        live = _live_snapshot()
    except Exception:
        live = None
    return jsonify({
        "iso": ISO_CODE,
        "method": "live_ieso_genoutputcapability_v1" if live else "unavailable",
        "live_feed_ok": bool(live),
        "live_as_of": (live or {}).get("as_of"),
        "latest_data_at": latest_ts.isoformat() if latest_ts else None,
        "total_records": int(total or 0),
        "source_id": SOURCE_ID,
    }), 200
