"""iso_ieso.py — IESO (Ontario) grid data extractor.

Independent Electricity System Operator for Ontario, Canada. Covers
~24-25 GW summer peak across Toronto / Mississauga / Markham / Ottawa —
a top-tier North American data-center market with rapid AI build-out.

────────────────────────────────────────────────────────────────────────
2026-05-31 FIX (#100, ISO coverage expansion) — switched from a (now dead)
live-CSV scraper to an HONEST modeled baseline, mirroring iso_aeso_intl.py
and iso_hydroquebec.py (the other two Canadian operators).

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
  return 404 or HTML, not a fetchable data file.

VERDICT: IESO is NOT reliably free/auth-free fetchable. Per the task's
"do not fake live data" directive, IESO is now a MODELED BASELINE
(method="baseline_model_v1"), exactly like AESO (iso_aeso_intl.py) and
Hydro-Québec (iso_hydroquebec.py) — both already modeled for the same
"public real-time feed requires registration/auth" reason. The baseline is
anchored to IESO's published 2024 generation mix (nuclear+hydro dominant,
remarkably stable year-over-year) + Ontario's seasonal/diurnal demand
profile, which is far more accurate for a nuclear-baseload system than any
generic average. This module now WRITES REAL ROWS to grid_data every run.

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
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify

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
        "method": "baseline_model_v1",
        "metrics_extracted": 0,
        "rows_inserted": 0,
        "note": ("Modeled baseline — IESO's public reports.ieso.ca CSVs now "
                 "require Okta SAML SSO (no longer auth-free), and Ontario is "
                 "outside EIA-930. Anchored to IESO 2024 published mix + "
                 "seasonal demand. Phase 2: live via IESO authenticated API."),
    }
    try:
        metrics = _baseline_snapshot()
        summary["metrics_extracted"] = len(metrics)
        rows = _persist_metrics(metrics)
        summary["rows_inserted"] = rows
        summary["status"] = "ok"
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows,
                   duration_ms=int((time.time() - started) * 1000),
                   metadata={"method": "baseline_model_v1",
                             "metrics_extracted": len(metrics)})
    except Exception as e:
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
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


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_ieso_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_nordpool_intl.py:206 — review and remove one

@iso_ieso_bp.route("/snapshot", methods=["GET"])
def snapshot():
    # Read-only current snapshot WITHOUT persisting (parity with the other
    # modeled Canadian operators' /snapshot).
    return jsonify({
        "iso": ISO_CODE,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "method": "baseline_model_v1",
        "metrics": _baseline_snapshot(),
        "generation_mix": GENERATION_MIX,
        "installed_capacity_mw": INSTALLED_CAPACITY_MW,
        "renewable_pct": RENEWABLE_PCT,
    }), 200
# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one


@iso_ieso_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso=ISO_CODE, method="baseline_model_v1",
# AUTO-REPAIR: duplicate route '/health' also in main.py:3871 — review and remove one
                   metrics=latest_for_iso(ISO_CODE)), 200


@iso_ieso_bp.route("/health", methods=["GET"])
def health():
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = %s",
            (ISO_CODE,),
        )
        latest_ts, total = cur.fetchone()
    return jsonify({
        "iso": ISO_CODE,
        "method": "baseline_model_v1",
        "latest_data_at": latest_ts.isoformat() if latest_ts else None,
        "total_records": int(total or 0),
        "source_id": SOURCE_ID,
    }), 200
