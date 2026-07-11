"""get_retirement_headroom REST surface (2026-07-11, Gemini co-design round 3).

GET /api/v1/retirement-headroom — deterministic retirement-adjacency scan:
generators with FILED EIA-860M retirements inside the horizon, each joined to
its nearest substations, local queue pressure, and the standard
site_evaluation_handoff. A retiring plant is a concrete headroom event (its
POI frees injection capacity) — this is filings, not forecasting, and the
meta.caveat says so (the envelope's honesty contract).

region_iso filters on the generator's own EIA balancing_authority_code —
real BA membership, NOT a state-footprint approximation. The handoff carries
the LIVE analyze_site arg names (lat/lon/capacity_mw — handoff parity beats
symmetry with the co-design sketch; capacity_mw = the CALLER's target_mw,
the DC being sited, not the retiring plant's MW).

Data layer: generator_retirements (eia_retirements.py ingest, monthly via
POST /api/jobs/eia-retirements + manual backfill). Reads the replica.
"""

from __future__ import annotations

import os
import math
from flask import Blueprint, jsonify, request

retirement_headroom_bp = Blueprint("retirement_headroom", __name__)

# ISO name → EIA balancing_authority_code (accepts common spellings; matching
# is alnum-normalized, so iso-ne / ISONE / ISO-NE all work).
_ISO_TO_BA = {
    "MISO": "MISO", "PJM": "PJM", "ERCOT": "ERCO", "ERCO": "ERCO",
    "SPP": "SWPP", "SWPP": "SWPP", "CAISO": "CISO", "CISO": "CISO",
    "NYISO": "NYIS", "NYIS": "NYIS", "ISONE": "ISNE", "ISNE": "ISNE",
}


def _conn():
    import psycopg2
    db = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL"))
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# In-progress queue statuses mirror the refined-queue endpoint's semantics
# (SPP never says "active" — the cross-ISO lesson).
_DEAD_STATUS_RE = r"withdraw|cancel|terminat|suspend|commercial operation|deactivat"


@retirement_headroom_bp.route("/api/v1/retirement-headroom")
def api_retirement_headroom():
    a = request.args

    def _num(k, d=None):
        try:
            v = a.get(k)
            return float(v) if v not in (None, "") else d
        except Exception:
            return d

    target_mw = _num("target_mw", 50) or 50
    horizon = int(_num("horizon_months", 24) or 24)
    horizon = max(1, min(horizon, 120))
    region_iso = (a.get("region_iso") or a.get("iso") or "").strip()
    fuel = (a.get("fuel_filter") or a.get("fuel") or "").strip()
    try:
        limit = max(1, min(int(a.get("limit") or 10), 25))
    except Exception:
        limit = 10

    ba_codes = None
    if region_iso:
        toks = ["".join(ch for ch in t.upper() if ch.isalnum())
                for t in region_iso.replace(";", ",").split(",") if t.strip()]
        ba_codes = sorted({_ISO_TO_BA[t] for t in toks if t in _ISO_TO_BA})
        if not ba_codes:
            return jsonify(ok=False, _entity="error",
                           error=f"unknown region_iso '{region_iso}'",
                           known=sorted(set(_ISO_TO_BA.keys()))), 400

    conn = _conn()
    if conn is None:
        return jsonify(ok=False, _entity="error", error="no_db"), 503
    try:
        cur = conn.cursor()
        where = ["status = 'planned_retirement'",
                 "retirement_date IS NOT NULL",
                 "retirement_date >= CURRENT_DATE",
                 "retirement_date < CURRENT_DATE + (%s || ' months')::interval",
                 "capacity_mw >= %s"]
        params = [str(horizon), target_mw]
        if ba_codes:
            where.append("ba_code = ANY(%s)")
            params.append(ba_codes)
        if fuel and all(c.isalnum() or c in " /+-" for c in fuel):
            where.append("fuel_category ~* %s")
            params.append(fuel)
        cur.execute(
            "SELECT plant_name, generator_id, capacity_mw, fuel_category, prime_mover, "
            "retirement_date, state, county, lat, lng, ba_code, source_month "
            "FROM generator_retirements WHERE " + " AND ".join(where) +
            " ORDER BY retirement_date ASC, capacity_mw DESC LIMIT %s",
            params + [limit])
        rows = cur.fetchall()

        cur.execute("SELECT MAX(source_month) FROM generator_retirements")
        _vintage = (cur.fetchone() or [None])[0]

        data = []
        for (pname, gid, mw, fuelcat, pm, rdate, state, county, lat, lng,
             ba, _sm) in rows:
            item = {
                "generator": {
                    "name": pname, "generator_id": gid,
                    "capacity_mw": float(mw) if mw is not None else None,
                    "fuel_category": fuelcat, "prime_mover": pm,
                    "retirement_date": str(rdate),
                    "state": state, "county": county,
                },
                "iso_context": ba,
            }
            if lat is not None and lng is not None:
                item["representative_point"] = {"lat": float(lat), "lng": float(lng)}
                # nearest substations: state-scoped bbox, haversine top-3
                cur.execute(
                    "SELECT name, lat, lng FROM substations "
                    "WHERE state = %s AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s",
                    (state, lat - 0.5, lat + 0.5, lng - 0.7, lng + 0.7))
                subs = [(n, _haversine_km(lat, lng, sla, sln))
                        for n, sla, sln in cur.fetchall()
                        if sla is not None and sln is not None]
                subs.sort(key=lambda x: x[1])
                item["nearest_substations"] = [
                    {"name": n, "distance_km": round(d, 1)} for n, d in subs[:3]]
                item["substations_within_25km"] = sum(1 for _, d in subs if d <= 25)
                # the deterministic rail: pre-filled next calls, LIVE arg names
                item["site_evaluation_handoff"] = {
                    "analyze_site": {"lat": float(lat), "lon": float(lng),
                                     "capacity_mw": target_mw,
                                     "include_fiber": True, "include_risk": True},
                    "get_water_risk": {"lat": float(lat), "lng": float(lng)},
                }
            else:
                item["representative_point"] = None
                item["coordinate_caveat"] = ("no coordinates on the EIA row and no "
                                             "county match in substations — hand-off "
                                             "unavailable for this generator")
            # queue pressure: competing in-progress MW filed in the same county
            # (falls back to state-level, flagged, when county is absent)
            if county:
                cur.execute(
                    "SELECT COALESCE(SUM(capacity_mw),0)::float, COUNT(*) FROM interconnect_queue "
                    "WHERE state = %s AND lower(coalesce(county,'')) = lower(%s) "
                    "AND coalesce(queue_status,'') !~* %s",
                    (state, county, _DEAD_STATUS_RE))
                qmw, qn = cur.fetchone()
                item["queue_pressure"] = {"competing_mw": round(qmw, 1),
                                          "competing_projects": int(qn),
                                          "scope": "county",
                                          "county_state": f"{county}, {state}"}
            else:
                cur.execute(
                    "SELECT COALESCE(SUM(capacity_mw),0)::float, COUNT(*) FROM interconnect_queue "
                    "WHERE state = %s AND coalesce(queue_status,'') !~* %s",
                    (state, _DEAD_STATUS_RE))
                qmw, qn = cur.fetchone()
                item["queue_pressure"] = {"competing_mw": round(qmw, 1),
                                          "competing_projects": int(qn),
                                          "scope": "state (no county on EIA row)"}
            data.append(item)

        total_mw = round(sum((d["generator"]["capacity_mw"] or 0) for d in data), 1)
        return jsonify({
            "_entity": "retirement_headroom_results",
            "ok": True,
            "meta": {
                "source": "DC Hub (dchub.cloud) via EIA-860M",
                "baseline_age": _vintage,
                "caveat": ("Represents filed planned retirements; actual "
                           "decommissioning timelines are subject to ISO "
                           "reliability reviews (RMR agreements can extend "
                           "operation past the filed date)."),
            },
            "filters_applied": {"target_mw": target_mw, "horizon_months": horizon,
                                "region_iso": ba_codes, "fuel_filter": fuel or None,
                                "limit": limit},
            "count_returned": len(data),
            "total_retiring_mw": total_mw,
            "data": data,
            "_cite": "DC Hub (dchub.cloud) — EIA-860M filed generator retirements",
        }), 200
    except Exception as e:
        # pre-ingest (table absent) degrades to an honest empty-with-reason
        if "generator_retirements" in str(e):
            return jsonify(ok=False, _entity="error",
                           error="retirement data not yet ingested — run POST /api/jobs/eia-retirements"), 503
        return jsonify(ok=False, _entity="error", error=str(e)[:200]), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
