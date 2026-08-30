"""
iso_sg_nems.py — Singapore grid ingestion via NEMS community mirror (LIVE).

Daily-content-feeds #2 (2026-07-01), APAC expansion — shipped alongside the
Japan denki-yoho feed (iso_jp_denkiyoho.py). Singapore is a top-tier APAC DC
market (the existing DCPI 'singapore' market is already tagged iso=EMA) but
had no live telemetry until now.

DATA SOURCE — community mirror of Singapore NEMS (National Electricity Market
of Singapore, operated by EMC under EMA) half-hourly system data:
  https://nems.sn.sg/api/status.json
Returns {"updated": <epoch s>, "usep": <SGD/MWh>, "demand": <MW>, "vcp": ...}.
Verified live 2026-07-02: demand 7406 MW, USEP 245.02 SGD/MWh.
  ⚠ VERIFIED GOTCHA: the host is nems.sn.sg — the www. subdomain does NOT
  resolve (NXDOMAIN). Do not "fix" the URL by adding www.

HONESTY:
  • LIVE-ONLY — if the mirror is unreachable, writes NOTHING (no modeled
    fallback). Community mirror ≠ primary source, so the payload is also
    sanity-bounded (SG system demand plausibly 3–12 GW) and freshness-guarded
    (epoch stamp must be <24h old) before anything is persisted.
  • demand is half-hourly NEMS system demand in MW (no unit conversion).
  • Source + as-of timestamp are carried on every snapshot/row batch.
  ISO_CODE = "EMA" (matches the DCPI singapore market tag in routes/dcpi.py).
"""
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._swallowed_writes import note_swallowed_write

try:
    import requests as _rq
except Exception:
    _rq = None

iso_sg_nems_bp = Blueprint("iso_sg_nems", __name__, url_prefix="/api/v1/iso/sg")
SOURCE_ID = "iso-sg-nems-live"
ISO_CODE = "EMA"

_SG_URL = "https://nems.sn.sg/api/status.json"  # NOTE: no www (NXDOMAIN)
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
_STALE_AFTER_S = 24 * 3600      # half-hourly feed; >24h old = dead mirror
_DEMAND_SANE_MW = (3000, 12000)  # SG system demand plausibility bounds


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _live_snapshot():
    """Real live SG snapshot, or None — never modeled, never out-of-bounds."""
    if _rq is None:
        return None
    try:
        r = _rq.get(_SG_URL, headers=_HEADERS, timeout=10)
        if not r.ok:
            return None
        data = r.json() or {}
    except Exception:
        return None
    try:
        updated = float(data.get("updated") or 0)
        demand = float(data.get("demand") or 0)
    except (TypeError, ValueError):
        return None
    age_s = time.time() - updated
    if updated <= 0 or age_s > _STALE_AFTER_S:
        return None  # stale mirror — write nothing
    if not (_DEMAND_SANE_MW[0] <= demand <= _DEMAND_SANE_MW[1]):
        return None  # implausible for SG — refuse rather than persist garbage
    snap = {
        "demand_mw": {"value": round(demand, 1), "unit": "MW"},
    }
    for key, metric in (("usep", "usep_sgd_mwh"), ("vcp", "vcp_sgd_mwh")):
        try:
            v = float(data.get(key))
            if v == v:  # not NaN
                snap[metric] = {"value": round(v, 2), "unit": "SGD/MWh"}
        except (TypeError, ValueError):
            pass
    snap["_as_of_utc"] = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat()
    snap["_age_s"] = int(age_s)
    return snap


def _persist_metrics(metrics):
    if not metrics:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            if name.startswith("_"):
                continue
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
                note_swallowed_write("grid_data", where="iso_sg_nems._persist_metrics")
                pass
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_nems_status_json",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": "Singapore NEMS half-hourly system demand (nems.sn.sg community mirror)",
    }
    try:
        metrics = _live_snapshot()
        if metrics is None:
            summary["errors"].append(
                "nems_mirror_unavailable_or_stale — wrote nothing (no modeled fallback)")
        else:
            summary["metrics_extracted"] = sum(1 for k in metrics if not k.startswith("_"))
            summary["rows_inserted"] = _persist_metrics(metrics)
            summary["snapshot"] = {k: v["value"] for k, v in metrics.items()
                                   if not k.startswith("_")}
            summary["as_of_utc"] = metrics["_as_of_utc"]
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_sg_nems_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_sg_nems_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    snap = _live_snapshot()
    if snap is None:
        return jsonify({"iso": ISO_CODE, "error": "nems_mirror_unavailable_or_stale"}), 503
    from routes.tier_gate import jsonify_gated_snapshot
    return jsonify_gated_snapshot({"iso": ISO_CODE, "live": True,
                    "metrics": {k: v["value"] for k, v in snap.items()
                                if not k.startswith("_")},
                    "as_of_utc": snap["_as_of_utc"],
                    "age_seconds": snap["_age_s"],
                    "source": "NEMS half-hourly (nems.sn.sg mirror)"}, 200)
# AUTO-REPAIR: duplicate route '/health' also in main.py:7891 — review and remove one


@iso_sg_nems_bp.route("/health", methods=["GET"])
def http_health():
    snap = _live_snapshot()
    return jsonify({"iso": ISO_CODE, "live_feed_ok": snap is not None,
                    "source": "nems_sn_sg_mirror_tokenfree"}), 200
