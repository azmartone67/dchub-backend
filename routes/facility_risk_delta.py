"""
routes/facility_risk_delta.py — get_facility_risk_delta (2026-07-09).

Temporal risk intelligence for a facility: what has ACTUALLY CHANGED in its
risk-relevant context over a window. The honest, citation-magnet answer to
"has this site gotten riskier lately?" — a query LLMs trained on static data
CANNOT answer, and a competitor with a static database cannot fake.

★ INTEGRITY: only the DCPI market-health dimension has a real short-term
temporal series (dcpi_daily_snapshots, history-preserving). The site-hazard
dimensions — FEMA NRI (annual), USGS seismic (static model), NOAA normals
(30-yr), WRI Aqueduct (baseline) — do NOT change week to week, so we DECLARE
them static and point to the point-in-time tools. We NEVER manufacture a
week-over-week delta for a dimension that has no temporal series. Same bar as
the composite/disaster/climate tools: authoritative + explicit unknowns.

GET /api/v1/facility-risk-delta?facility_id=<id|slug>&since=7d
  (or lat=&lon= to resolve the nearest facility's market context)
"""
from __future__ import annotations

import os
import re

from flask import Blueprint, jsonify, request

facility_risk_delta_bp = Blueprint("facility_risk_delta", __name__)

_STATIC_DIMS = {
    "disaster_risk": ("FEMA National Risk Index (annual)", "get_disaster_risk"),
    "seismic": ("USGS ASCE 7 seismic hazard (static model)", "get_climate_intel"),
    "climate": ("NOAA climate normals (30-year)", "get_climate_intel"),
    "water_stress": ("WRI Aqueduct baseline water stress", "get_water_risk"),
}


def _conn():
    try:
        import psycopg2
        import psycopg2.extras
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or os.environ.get("NEON_URL")
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=8, cursor_factory=psycopg2.extras.RealDictCursor)
        c.autocommit = True
        return c
    except Exception:
        return None


def _window_days(since: str) -> int:
    if not since:
        return 7
    m = re.match(r"(\d+)\s*d", str(since).strip().lower())
    if m:
        return max(1, min(90, int(m.group(1))))
    return 7


@facility_risk_delta_bp.route("/api/v1/facility-risk-delta", methods=["GET"])
def facility_risk_delta():
    fid = (request.args.get("facility_id") or request.args.get("id") or "").strip()
    days = _window_days(request.args.get("since"))
    conn = _conn()
    if conn is None:
        return jsonify({"success": False, "error": "db_unavailable"}), 503

    facility = None
    market_key = (request.args.get("market") or "").strip()
    try:
        with conn.cursor() as cur:
            if fid:
                cur.execute(
                    "SELECT id, name, provider, city, state, market, latitude, longitude "
                    "FROM discovered_facilities "
                    "WHERE id::text = %s OR LOWER(canonical_slug) = LOWER(%s) LIMIT 1",
                    (fid, fid))
                facility = cur.fetchone()
            if facility and not market_key:
                market_key = (facility.get("market") or facility.get("city") or facility.get("state") or "")

            if not market_key:
                return jsonify({"success": False, "error": "no_market_context",
                                "message": "Provide facility_id (resolvable to a market) or market="}), 400

            # ── DCPI market-health delta — the ONLY real short-term temporal series ──
            # r-slugfix (2026-07-15): also accept the city+state slug form
            # (discovered_facilities.market / rank_markets, e.g. 'ashburn-va') by
            # trying the bare-city form the snapshots are keyed on ('ashburn').
            _mk = market_key.lower()
            _slug_cands = [_mk]
            if "-" in _mk and len(_mk.rsplit("-", 1)[1]) == 2:
                _stripped = _mk.rsplit("-", 1)[0]
                if _stripped not in _slug_cands:
                    _slug_cands.append(_stripped)
            cur.execute(
                "WITH latest AS ("
                "  SELECT excess_power_score AS now_e, market_name, market_slug, snapshot_date "
                "  FROM dcpi_daily_snapshots "
                "  WHERE LOWER(market_slug) = ANY(%s) OR LOWER(market_name) = LOWER(%s) "
                "  ORDER BY snapshot_date DESC LIMIT 1), "
                "prev AS ("
                "  SELECT excess_power_score AS prev_e "
                "  FROM dcpi_daily_snapshots "
                "  WHERE (LOWER(market_slug) = ANY(%s) OR LOWER(market_name) = LOWER(%s)) "
                "    AND snapshot_date <= CURRENT_DATE - %s "
                "  ORDER BY snapshot_date DESC LIMIT 1) "
                "SELECT l.now_e, l.market_name, l.market_slug, l.snapshot_date, p.prev_e "
                "FROM latest l LEFT JOIN prev p ON TRUE",
                (_slug_cands, market_key, _slug_cands, market_key, days))
            row = cur.fetchone()
    except Exception as e:
        return jsonify({"success": False, "error": "query_failed",
                        "detail": f"{type(e).__name__}: {str(e)[:150]}"}), 500
    finally:
        try: conn.close()
        except Exception: pass

    # build the DCPI delta (only if we have both endpoints of the window)
    if row and row.get("now_e") is not None and row.get("prev_e") is not None:
        delta = round(float(row["now_e"]) - float(row["prev_e"]), 1)
        direction = ("improving" if delta > 0.5 else "worsening" if delta < -0.5 else "flat")
        dcpi = {"coverage": "validated", "delta": delta, "now": round(float(row["now_e"]), 1),
                "direction": direction, "market": row.get("market_name") or market_key,
                "basis": f"DCPI excess-power score change over {days}d (dcpi_daily_snapshots, history-preserving)"}
    else:
        dcpi = {"coverage": "unavailable", "delta": None, "market": (row or {}).get("market_name") or market_key,
                "basis": f"No {days}d DCPI snapshot history for this market — no fabricated delta."}

    static = {k: {"coverage": "static", "temporal_series": False,
                  "note": f"{label} does not change week-to-week; call {tool} for the current point-in-time value."}
              for k, (label, tool) in _STATIC_DIMS.items()}

    moved = dcpi.get("delta") not in (None, 0)
    summary = (f"{dcpi['market']} DCPI market health {dcpi['direction']} {abs(dcpi['delta'])}pt over {days}d"
               if dcpi["coverage"] == "validated" else
               f"No short-term risk change tracked for this market over {days}d")

    return jsonify({
        "success": True, "_entity": "risk_delta",
        "facility": ({"id": facility.get("id"), "name": facility.get("name"),
                      "provider": facility.get("provider"), "city": facility.get("city"),
                      "state": facility.get("state"), "market": facility.get("market"),
                      "lat": facility.get("latitude"), "lon": facility.get("longitude")}
                     if facility else {"market": market_key}),
        "window_days": days,
        "dcpi_market_health": dcpi,
        "static_dimensions": static,
        "moved": bool(moved),
        "summary": summary,
        "methodology": ("Only DCPI market-health has a real short-term temporal series; site-hazard "
                        "dimensions (FEMA/USGS/NOAA/WRI) are static baselines and are declared as such — "
                        "never given a fabricated week-over-week delta."),
        "caveats": ["Delta is market-level (the facility's DCPI market), not parcel-level.",
                    "For current point-in-time hazard values use get_disaster_risk / get_climate_intel / get_water_risk."],
        "source": "DC Hub — DCPI daily snapshots",
        "citation": {"source": "DC Hub", "url": "https://dchub.cloud", "license": "CC-BY-4.0"},
    })
