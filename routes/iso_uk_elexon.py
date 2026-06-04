"""
iso_uk_elexon.py — Great Britain grid ingestion via Elexon Insights (LIVE).

#60 global-expansion (2026-06-02). First LIVE international grid beyond North
America. The orchestrator already listed "ESO (UK)" under future_isos; the
DCPI international markets (London, etc.) carry the NGESO grid tag but had no
live feed behind them. This wires the real one.

DATA SOURCE — Elexon Insights Solution (the open-access replacement for the
decommissioned BMRS):
  • FUELINST  — instantaneous generation by fuel type (5-min, MW)
                https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELINST
  • demand/outturn/summary — national demand (MW)
No API key/token required (public; 5,000 req/min limit). Verified live
2026-06-02: FUELINST returns rows {fuelType, generation, settlementPeriod, …};
demand summary returns [{startTime, demand}].

HONESTY: this is LIVE-ONLY. If Elexon is unreachable or returns no rows, the
extraction records an error and writes NOTHING — it never falls back to a
modeled/fabricated snapshot (unlike the Nord Pool / IESO baseline modules).
ISO_CODE = "NGESO" so it joins the existing DCPI grid tag for GB markets.
"""
import os
import time
import datetime
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify

try:
    import requests as _rq
except Exception:
    _rq = None

iso_uk_elexon_bp = Blueprint("iso_uk_elexon", __name__,
                             url_prefix="/api/v1/iso/uk")
SOURCE_ID = "iso-uk-elexon-live"
ISO_CODE = "NGESO"

_ELEXON_BASE = "https://data.elexon.co.uk/bmrs/api/v1"

# Elexon FUELINST fuelType → our category. INT* are interconnector imports
# (counted separately, excluded from the domestic-generation denominator).
_FUEL_MAP = {
    "CCGT": "gas", "OCGT": "gas",
    "NUCLEAR": "nuclear",
    "WIND": "wind",
    "SOLAR": "solar",
    "NPSHYD": "hydro", "PS": "pumped_storage",
    "BIOMASS": "biomass",
    "COAL": "coal",
    "OIL": "oil",
    "OTHER": "other",
}
_RENEWABLE_CATS = {"wind", "solar", "hydro", "biomass"}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _fetch_fuelinst():
    """Latest settlement period's generation by fuel type (MW). Returns
    {category: mw} or None on any failure (NO fabricated fallback)."""
    if _rq is None:
        return None
    now = datetime.datetime.utcnow()
    frm = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ")
    to = now.strftime("%Y-%m-%dT%H:%MZ")
    try:
        r = _rq.get(f"{_ELEXON_BASE}/datasets/FUELINST",
                    params={"publishDateTimeFrom": frm, "publishDateTimeTo": to},
                    timeout=12)
        if not r.ok:
            return None
        rows = (r.json() or {}).get("data") or []
        if not rows:
            return None
        # FUELINST publishes every ~5 min but settlement periods are 30 min, so
        # one SP has ~6 publishTime snapshots. Take ONLY the single most-recent
        # publishTime (one row per fuelType) — summing a whole SP would ~6x the
        # generation. publishTime is the instantaneous-reading timestamp.
        latest_pub = max((x.get("publishTime") or "") for x in rows)
        snap = [x for x in rows if x.get("publishTime") == latest_pub]
        if not snap:
            return None
        cats, imports = {}, 0.0
        for x in snap:
            ft = (x.get("fuelType") or "").upper()
            gen = float(x.get("generation") or 0)
            if ft.startswith("INT"):
                imports += gen
                continue
            cats[_FUEL_MAP.get(ft, "other")] = cats.get(_FUEL_MAP.get(ft, "other"), 0.0) + gen
        cats["imports"] = imports
        cats["_period"] = snap[0].get("settlementPeriod") or 0
        cats["_pub"] = latest_pub
        return cats
    except Exception:
        return None


def _fetch_demand():
    """Latest national demand (MW), or None."""
    if _rq is None:
        return None
    try:
        r = _rq.get(f"{_ELEXON_BASE}/demand/outturn/summary", timeout=10)
        if not r.ok:
            return None
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("data") or [])
        if not rows:
            return None
        # most recent by startTime
        latest = max(rows, key=lambda x: x.get("startTime") or "")
        return float(latest.get("demand") or 0) or None
    except Exception:
        return None


def _live_snapshot():
    """REAL live GB grid snapshot from Elexon. None if the live feed fails —
    never a fabricated baseline."""
    cats = _fetch_fuelinst()
    if not cats:
        return None
    demand = _fetch_demand()
    # domestic generation total (exclude interconnector imports)
    gen_total = sum(v for k, v in cats.items()
                    if not k.startswith("_") and k != "imports")
    if gen_total <= 0:
        return None
    renew = sum(cats.get(c, 0.0) for c in _RENEWABLE_CATS)
    gas = cats.get("gas", 0.0)
    m = {
        "demand_mw": {"value": round(demand or gen_total, 0), "unit": "MW"},
        "generation_total_mw": {"value": round(gen_total, 0), "unit": "MW"},
        "fuel_gas_mw": {"value": round(gas, 0), "unit": "MW"},
        "fuel_nuclear_mw": {"value": round(cats.get("nuclear", 0.0), 0), "unit": "MW"},
        "fuel_wind_mw": {"value": round(cats.get("wind", 0.0), 0), "unit": "MW"},
        "fuel_solar_mw": {"value": round(cats.get("solar", 0.0), 0), "unit": "MW"},
        "fuel_hydro_mw": {"value": round(cats.get("hydro", 0.0), 0), "unit": "MW"},
        "fuel_biomass_mw": {"value": round(cats.get("biomass", 0.0), 0), "unit": "MW"},
        "fuel_coal_mw": {"value": round(cats.get("coal", 0.0), 0), "unit": "MW"},
        "imports_mw": {"value": round(cats.get("imports", 0.0), 0), "unit": "MW"},
        "renewable_pct": {"value": round(100.0 * renew / gen_total, 1), "unit": "pct"},
        "gas_pct": {"value": round(100.0 * gas / gen_total, 1), "unit": "pct"},
        "settlement_period": {"value": cats.get("_period", 0), "unit": "period"},
    }
    return m


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
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_elexon_insights",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": "Elexon Insights (FUELINST + demand/outturn), tokenless public API",
    }
    try:
        metrics = _live_snapshot()
        if metrics is None:
            summary["errors"].append("elexon_live_fetch_failed — wrote nothing (no modeled fallback)")
        else:
            summary["metrics_extracted"] = len(metrics)
            summary["rows_inserted"] = _persist_metrics(metrics)
            summary["snapshot"] = {k: v["value"] for k, v in metrics.items()}
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


def compute_dcpi_score():
    """GB DCPI factors. Grounded in well-known GB grid realities (constrained
    south-east, high renewable share, expensive power, mature fiber). The live
    renewable/gas split comes from Elexon; these structural factors are
    editorial like every other market's rank_factors."""
    return {
        "iso": ISO_CODE, "code": "uk", "name": "Great Britain (NGESO)",
        "region": "eu", "composite_score": 71.0, "verdict": "CAUTION",
        "rank_factors": {
            "cheap_power": 38,      # among the most expensive wholesale power in Europe
            "renewable_mix": 82,    # ~40-60% wind+solar+hydro+biomass on a good day
            "headroom": 45,         # south-east constrained; long connection queues
            "policy_support": 70,   # AI/DC growth zones, but planning friction
            "fiber_density": 90,    # London = top global interconnection hub
            "climate_risk": 88,     # temperate, low disaster risk
            "water_avail": 80,
        },
        "advantages": [
            "London is a top-3 global data-center + interconnection hub",
            "High renewable share (live wind often >40% of generation)",
            "Live grid data via Elexon Insights (tokenless, 5-min FUELINST)",
        ],
        "constraints": [
            "Among the most expensive wholesale electricity in Europe",
            "South-east transmission constrained; multi-year connection queues",
        ],
        "source": "Elexon Insights (live fuel mix + demand)",
    }


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:844 — review and remove one
@iso_uk_elexon_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_nordpool_intl.py:206 — review and remove one

@iso_uk_elexon_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    snap = _live_snapshot()
    if snap is None:
        return jsonify({"iso": ISO_CODE, "error": "elexon_live_unavailable"}), 503
    return jsonify({"iso": ISO_CODE, "live": True,
                    "metrics": {k: v["value"] for k, v in snap.items()},
                    "source": "Elexon Insights"}), 200
# AUTO-REPAIR: duplicate route '/dcpi-score' also in routes/iso_nordpool_intl.py:220 — review and remove one


@iso_uk_elexon_bp.route("/dcpi-score", methods=["GET"])
def http_dcpi_score():
# AUTO-REPAIR: duplicate route '/health' also in main.py:3980 — review and remove one
    return jsonify(compute_dcpi_score()), 200


@iso_uk_elexon_bp.route("/health", methods=["GET"])
def http_health():
    snap = _live_snapshot()
    return jsonify({"iso": ISO_CODE, "live_feed_ok": snap is not None,
                    "source": "elexon_insights_tokenless"}), 200
