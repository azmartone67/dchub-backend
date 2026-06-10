"""
grid_snapshot.py — cross-ISO snapshot endpoint.

GET /api/v1/grid/snapshot
  Returns latest metrics from ALL ISOs in one response. The killer feature
  for AI agents asking "what does the grid look like right now?" — they
  get cross-ISO comparison in a single call.

GET /api/v1/grid/totals
  Returns summed-up totals across all ISOs (total US demand, total renewable
  generation, etc) — useful for executive dashboards.
"""

from datetime import datetime, timezone
from flask import Blueprint, jsonify
from routes._iso_common import conn


grid_snapshot_bp = Blueprint("grid_snapshot", __name__, url_prefix="/api/v1/grid")

ISO_LIST = ["ERCOT", "CAISO", "NYISO", "MISO", "SPP", "ISONE", "PJM"]


# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_nordpool_intl.py:206 — review and remove one
@grid_snapshot_bp.route("/snapshot", methods=["GET"])
def snapshot():
    """All latest metrics, organized by ISO."""
    by_iso = {iso: {"metrics": {}, "latest_at": None, "metric_count": 0}
              for iso in ISO_LIST}

    with conn() as c, c.cursor() as cur:
        # Use a CTE to grab latest per (iso, metric_name)
        cur.execute("""
            WITH ranked AS (
                SELECT iso, metric_name, metric_value, unit, timestamp,
                       ROW_NUMBER() OVER (
                           PARTITION BY iso, metric_name
                           ORDER BY timestamp DESC
                       ) AS rn
                FROM grid_data
                WHERE iso = ANY(%s)
            )
            SELECT iso, metric_name, metric_value, unit, timestamp
            FROM ranked WHERE rn = 1
            ORDER BY iso, metric_name
        """, (ISO_LIST,))

        for iso, name, val, unit, ts in cur.fetchall():
            if iso not in by_iso:
                continue
            by_iso[iso]["metrics"][name] = {
                "value": float(val) if val is not None else None,
                "unit": unit,
                "timestamp": ts.isoformat() if ts else None,
            }
            by_iso[iso]["metric_count"] += 1
            if (by_iso[iso]["latest_at"] is None) or (ts and (
                by_iso[iso]["latest_at"] is None or
                ts.isoformat() > by_iso[iso]["latest_at"]
            )):
                by_iso[iso]["latest_at"] = ts.isoformat() if ts else None

    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        iso_count=len([i for i in by_iso.values() if i["metric_count"] > 0]),
        total_metric_count=sum(i["metric_count"] for i in by_iso.values()),
        by_iso=by_iso,
    ), 200


@grid_snapshot_bp.route("/totals", methods=["GET"])
def totals():
    """Summed values across all ISOs by metric prefix."""
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            WITH latest AS (
                SELECT iso, metric_name, metric_value,
                       ROW_NUMBER() OVER (PARTITION BY iso, metric_name
                                          ORDER BY timestamp DESC) AS rn
                FROM grid_data
                WHERE iso = ANY(%s)
            )
            SELECT
                COUNT(DISTINCT iso) AS iso_count,
                COUNT(*) AS metric_count,
                COALESCE(SUM(CASE WHEN metric_name LIKE '%%solar%%'
                                  THEN metric_value END), 0) AS total_solar_mw,
                COALESCE(SUM(CASE WHEN metric_name LIKE '%%wind%%'
                                  THEN metric_value END), 0) AS total_wind_mw,
                COALESCE(SUM(CASE WHEN metric_name LIKE '%%nuclear%%'
                                  THEN metric_value END), 0) AS total_nuclear_mw,
                COALESCE(SUM(CASE WHEN metric_name LIKE '%%demand%%'
                                  THEN metric_value END), 0) AS total_demand_mw,
                COALESCE(SUM(CASE WHEN metric_name LIKE '%%hydro%%'
                                  THEN metric_value END), 0) AS total_hydro_mw,
                COALESCE(SUM(CASE WHEN metric_name LIKE '%%natural%%gas%%'
                              OR metric_name LIKE '%%fuel_natural_gas%%'
                                  THEN metric_value END), 0) AS total_gas_mw
            FROM latest WHERE rn = 1
        """, (ISO_LIST,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        result = dict(zip(cols, [float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in row]))

    result["as_of"] = datetime.now(timezone.utc).isoformat()
    result["unit"] = "MW"
    return jsonify(result), 200
