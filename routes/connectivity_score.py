"""DC Hub — parcel FIBER-READINESS / connectivity scorer (2026-06-14).

The single answer engineering site-selectors screen on (and the reason Accurus churned to
FiberLocator): for THIS lat/lon — near-net distance to a carrier-served facility, how many
distinct carriers are reachable, and is there single-carrier risk. All computed from data
already in Neon (carrier_facility_presence 608K rows w/ lat/lng + carrier_name; fcc_fiber_hex
2.6M coverage hexes). One helper, reusable by the MCP get_fiber_readiness tool, score_facility,
the site-planner panel, and the map.

Standalone file + own blueprint so concurrent backend refactors can't revert it. Register:
  from routes.connectivity_score import connectivity_score_bp
  app.register_blueprint(connectivity_score_bp)

  GET /api/infrastructure/connectivity/score?lat=&lon=&radius=
    full (keyed/internal): {score, near_net_bucket, nearest_carrier_km, carrier_count,
                            top_carriers, single_carrier_risk, fiber_coverage_km, verdict_short, factors}
    teaser (anon): {near_net_bucket, nearest_carrier_km, _teaser, _upgrade}
"""
from __future__ import annotations
import os, time, math
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras

# provenance-v1 (2026-07-11): collection-level stamp helper. Fail-soft — the
# scorer must keep working even if the provenance module is unavailable.
try:
    from routes.provenance import attach_provenance as _attach_provenance
except Exception:
    _attach_provenance = None

# dark-fiber §4.3 (2026-07-11): INFERRED dark-availability point screen —
# dark-capable carriers (fiber_providers.dark_fiber=TRUE, alias-mapped to
# PeeringDB names) among the carriers already found around the query point.
# Pure function over names we already fetched (same radius, no extra SQL, no
# zones-table dependency). Fail-soft: import failure just omits the field.
try:
    from routes.dark_availability_zones import (
        dark_screen_from_carriers as _dark_screen)
except Exception:
    _dark_screen = None

connectivity_score_bp = Blueprint("connectivity_score_r88", __name__)

_cache = {}          # key -> (ts, data)
_TTL = 900           # 15 min; the bbox scans are ~0.2-0.7s
_MI = 1.60934        # km per mile (FiberLocator buckets are in miles)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _haversine_km(la1, lo1, la2, lo2):
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bucket(km):
    """FiberLocator-style near-net buckets (miles -> km)."""
    if km is None:
        return "build-required"
    if km <= 0.1 * _MI:
        return "on-net"
    if km <= 1.0 * _MI:
        return "near-net"
    if km <= 2.0 * _MI:
        return "acceptable"
    return "build-required"


def score_connectivity(lat, lon, radius_km=50.0):
    """Core helper — returns the FULL connectivity read for one point. Reusable across surfaces."""
    c = _conn()
    if c is None:
        return {"error": "no_db"}
    out = {"lat": lat, "lon": lon, "radius_km": radius_km}
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.05, math.cos(math.radians(lat))))
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = '9000'")
            # 1) carriers within radius (bbox prefilter -> haversine in py)
            cur.execute(
                """SELECT carrier_name, facility_lat, facility_lng
                   FROM carrier_facility_presence
                   WHERE facility_lat BETWEEN %s AND %s AND facility_lng BETWEEN %s AND %s
                     AND carrier_name IS NOT NULL AND carrier_name <> ''""",
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon))
            carriers = {}          # carrier_name -> nearest km
            for cn, fla, flo in cur.fetchall():
                if fla is None or flo is None:
                    continue
                d = _haversine_km(lat, lon, fla, flo)
                if d <= radius_km and d < carriers.get(cn, 1e9):
                    carriers[cn] = d
            carrier_count = len(carriers)
            nearest_km = round(min(carriers.values()), 2) if carriers else None
            top = sorted(carriers.items(), key=lambda x: x[1])[:6]
            top_carriers = [{"carrier": k, "distance_km": round(v, 2)} for k, v in top]

            # 2) fiber coverage: nearest FCC fiber hex (tight 15km bbox = fast)
            hb = 15.0 / 111.0
            hbl = 15.0 / (111.0 * max(0.05, math.cos(math.radians(lat))))
            cur.execute(
                """SELECT lat, lng FROM fcc_fiber_hex
                   WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s LIMIT 4000""",
                (lat - hb, lat + hb, lon - hbl, lon + hbl))
            hexes = cur.fetchall()
            fiber_hex_km = None
            if hexes:
                fiber_hex_km = round(min(_haversine_km(lat, lon, hla, hlo)
                                         for hla, hlo in hexes if hla is not None), 2)
    except Exception as e:
        try: c.close()
        except Exception: pass
        return {"error": "query_failed", "detail": str(e)[:200], "lat": lat, "lon": lon}
    finally:
        try: c.close()
        except Exception: pass

    bucket = _bucket(nearest_km)
    single_carrier_risk = carrier_count < 2

    # component scores -> weighted composite (distance 45% / carrier depth 40% / diversity 15%)
    dist_score = {"on-net": 100, "near-net": 82, "acceptable": 55}.get(
        bucket, max(0.0, 40.0 - (nearest_km or 99.0)))
    depth_score = 100.0 * (1.0 - math.exp(-carrier_count / 8.0))   # 1->12, 5->47, 10->71, 25->96
    div_score = 0.0 if single_carrier_risk else (60.0 if carrier_count < 4 else 100.0)
    composite = 0.45 * dist_score + 0.40 * depth_score + 0.15 * div_score
    score = int(round(max(0.0, min(100.0, composite))))

    if carrier_count == 0:
        verdict = (f"No carrier-served facility within {radius_km:.0f} km — greenfield fiber "
                   f"build required; budget long lateral construction.")
    elif single_carrier_risk:
        verdict = (f"Single-carrier risk — only {carrier_count} carrier "
                   f"({top_carriers[0]['carrier']}) within {radius_km:.0f} km; no path diversity.")
    elif bucket in ("on-net", "near-net") and carrier_count >= 5:
        verdict = (f"Carrier-rich: {carrier_count} carriers, fiber {bucket} "
                   f"({nearest_km} km) — strong connectivity and route diversity.")
    elif bucket == "build-required":
        verdict = (f"Fiber build required — nearest carrier facility {nearest_km} km; "
                   f"{carrier_count} carriers in range once lateral is built.")
    else:
        verdict = f"Workable: {carrier_count} carriers, fiber {bucket} ({nearest_km} km)."

    out.update({
        "score": score,
        "near_net_bucket": bucket,
        "nearest_carrier_km": nearest_km,
        "carrier_count": carrier_count,
        "top_carriers": top_carriers,
        "single_carrier_risk": single_carrier_risk,
        "fiber_coverage_km": fiber_hex_km,
        "verdict_short": verdict,
        "factors": {"distance": round(dist_score, 1), "carrier_depth": round(depth_score, 1),
                    "diversity": round(div_score, 1)},
        "source": "DC Hub (dchub.cloud) — PeeringDB carrier presence + FCC fiber coverage",
    })
    # dark-fiber §4.3: the instant screening read — which of the carriers in
    # range are dark-CAPABLE. Always stamped v:"inferred" with the method
    # named ("… NOT confirmed strand availability; screening signal only");
    # level "none" is itself the answer that eliminates a region. Fail-soft.
    if _dark_screen is not None:
        try:
            out["dark_screen"] = _dark_screen(carriers.keys())
        except Exception:
            pass
    return out


def _full_access():
    """Full read for keyed or internal callers; anon gets the near-net teaser.
    (Tier refinement free-vs-Developer happens at the MCP layer, like get_fiber_intel.)"""
    if request.headers.get("X-API-Key") or request.headers.get("Authorization"):
        return True
    ua = (request.headers.get("User-Agent") or "").lower()
    return ua.startswith("dchub") or "dchub-internal" in ua


@connectivity_score_bp.route("/api/infrastructure/connectivity/score", methods=["GET", "POST"])
def connectivity_score():
    src = request.get_json(silent=True) or request.args
    try:
        lat = float(src.get("lat"))
        lon = float(src.get("lon"))
    except (TypeError, ValueError):
        return jsonify(error="lat and lon are required (decimal degrees)"), 400
    try:
        radius = float(src.get("radius") or src.get("radius_km") or 50.0)
    except (TypeError, ValueError):
        radius = 50.0
    radius = min(max(radius, 5.0), 200.0)
    full = _full_access()

    key = (round(lat, 3), round(lon, 3), round(radius, 1), full)
    hit = _cache.get(key)
    now = time.time()
    if hit and (now - hit[0]) < _TTL:
        return jsonify(hit[1]), 200

    data = score_connectivity(lat, lon, radius)
    if data.get("error"):
        return jsonify(data), (503 if data["error"] == "no_db" else 500)

    if not full:
        data = {
            "lat": data["lat"], "lon": data["lon"], "radius_km": data["radius_km"],
            "near_net_bucket": data["near_net_bucket"],
            "nearest_carrier_km": data["nearest_carrier_km"],
            "fiber_coverage_km": data["fiber_coverage_km"],
            "_teaser": True,
            "_upgrade": ("Add X-API-Key (free tier: claim_free_key) for the full fiber-readiness "
                         "score — distinct carrier count, top carriers, single-carrier-risk flag, "
                         "the 0-100 connectivity score, and the inferred dark-fiber "
                         "availability screen (dark_screen)."),
            "source": data["source"],
        }
    # provenance-v1 (2026-07-11): the score is a DC Hub-derived composite,
    # not a carrier quote → default_v="inferred". Stamped on both the full
    # and teaser shapes, before caching (idempotent; never overwrites).
    try:
        if _attach_provenance:
            _attach_provenance(
                data,
                source=("DC Hub connectivity scorer — PeeringDB carrier "
                        "presence (carrier_facility_presence) + FCC BDC "
                        "fiber coverage hexes (fcc_fiber_hex)"),
                method=("derived composite: nearest-carrier distance (45%) + "
                        "distinct-carrier depth (40%) + path diversity (15%) "
                        "around the queried point — a DC Hub inference from "
                        "public datasets, not a carrier quote or surveyed "
                        "lateral"),
                default_v="inferred",
            )
    except Exception:
        pass
    _cache[key] = (now, data)
    return jsonify(data), 200
