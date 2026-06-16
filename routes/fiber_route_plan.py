"""
fiber_route_plan.py — diverse fibre route planner (Phase 1, standalone).

GET/POST /api/v1/fiber/route-plan?from=lat,lng&to=lat,lng&n=4
Returns N diverse, road-following routes from a candidate site to a target POP,
each with length + crossings, plus shared-corridor analysis, an indicative cost
breakdown, and a diversity read. Routes are INDICATIVE (auto-routed via OSRM) —
not engineered alignments; the response says so. Standalone blueprint + 1-line
register in main.py (no contested files), per the conflict-proof pattern.
"""
import math, json, urllib.request, urllib.parse
from flask import Blueprint, request, jsonify

fiber_route_plan_bp = Blueprint("fiber_route_plan", __name__)
OSRM = "https://router.project-osrm.org/route/v1/driving/"

# cost model defaults (the partner's weekend figures — all overridable via query)
COST = dict(p50_civils_per_m=250, infraco_per_m=25, design_per_m=55,
            f720_per_m=35, f1440_per_m=55, bore_per_m=1250,
            duct_opex_per_m_yr=4, om_opex_per_m_yr=2, ring_km=25.0)

def _osrm(coords, alternatives=False):
    # 1-replica backend: cap the OSRM wait so a slow/down router can't tie up a
    # worker (see reference_dchub_backend_flapping). Fail soft -> [].
    s = ";".join(f"{x:.6f},{y:.6f}" for x, y in coords)
    q = "?overview=full&geometries=geojson" + ("&alternatives=3" if alternatives else "")
    try:
        with urllib.request.urlopen(OSRM + s + q, timeout=9) as r:
            d = json.load(r)
    except Exception:
        return []
    if d.get("code") != "Ok":
        return []
    return [(rt["geometry"]["coordinates"], rt["distance"] / 1000.0) for rt in d["routes"]]

# in-process result cache (single replica) — identical from/to/n queries are
# instant, so repeat/demo/map traffic never re-hammers OSRM.
_PLAN_CACHE = {}

def _gm(a, b):
    dx = (a[0] - b[0]) * math.cos(math.radians((a[1] + b[1]) / 2)) * 111320
    dy = (a[1] - b[1]) * 110540
    return math.hypot(dx, dy)

def diverse_routes(frm, to, n=4):
    """Up to n diverse road routes (lng,lat). OSRM alternatives first, then
    perpendicular-offset midpoints to force distinct corridors."""
    out = [g for g in _osrm([frm, to], alternatives=True)]
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    L = math.hypot(dx, dy) or 1e-6
    px, py = -dy / L, dx / L          # unit perpendicular
    k = 0
    while len(out) < n and k < 12:
        side = 1 if k % 2 == 0 else -1
        mag = L * 0.18 * (1 + k // 2)   # grow the offset each pair
        mid = ((frm[0] + to[0]) / 2 + px * mag * side,
               (frm[1] + to[1]) / 2 + py * mag * side)
        r = _osrm([frm, mid, to])
        if r:
            g = r[0]
            if all(_route_distinct(g[0], o[0]) for o in out):
                out.append(g)
        k += 1
    return out[:n]

def _route_distinct(a, b, thresh=35, frac=0.55):
    """True if route a is not mostly co-located with b (downsampled)."""
    A, B = a[::4], b[::4]
    near = sum(1 for p in A if any(_gm(p, q) < thresh for q in B))
    return (near / max(1, len(A))) < frac

def shared_analysis(routes, endpoints, thresh=35, end_buffer=900):
    """Pairwise shared-street km (excludes within end_buffer of either endpoint)."""
    def near_end(p): return any(_gm(p, e) < end_buffer for e in endpoints)
    total = 0.0; pairs = []
    for i in range(len(routes)):
        for j in range(i + 1, len(routes)):
            A = routes[i][::3]; B = routes[j][::3]; close = [p for p in A
                if not near_end(p) and any(_gm(p, q) < thresh for q in B)]
            km = sum(_gm(close[m], close[m + 1]) for m in range(len(close) - 1)) / 1000 if len(close) > 1 else 0
            if km > 0.2:
                pairs.append({"routes": [i + 1, j + 1], "shared_km": round(km, 1)})
                total += km
    return round(total, 1), pairs

def min_separation_m(routes, endpoints, end_buffer=900):
    """Smallest distance between any two routes away from the shared endpoints."""
    def near_end(p): return any(_gm(p, e) < end_buffer for e in endpoints)
    best = 1e9
    for i in range(len(routes)):
        for j in range(i + 1, len(routes)):
            for p in routes[i][::5]:
                if near_end(p): continue
                for q in routes[j][::5]:
                    best = min(best, _gm(p, q))
    return None if best > 1e8 else round(best, 1)

def cost(total_km, fibre="720F", bore_m=0, ring=True, c=COST):
    civ = total_km * 1000 * (c["infraco_per_m"] + c["design_per_m"])
    fibre_per_m = c["f1440_per_m"] if fibre == "1440F" else c["f720_per_m"]
    fib = total_km * 1000 * fibre_per_m
    bore = bore_m * c["bore_per_m"]
    ring_km = c["ring_km"] if ring else 0
    ring_cost = ring_km * 1000 * (c["infraco_per_m"] + c["design_per_m"])
    capex = civ + fib + bore + ring_cost
    opex = total_km * 1000 * (c["duct_opex_per_m_yr"] + c["om_opex_per_m_yr"])
    return {"capex_usd": round(capex), "opex_usd_yr": round(opex),
            "fibre": fibre, "bore_m": bore_m, "ring_km": ring_km,
            "note": "indicative — derived from default unit rates; not a quote"}

def plan(frm, to, n=4, fibre="720F", bore_m=0):
    ck = (round(frm[0], 5), round(frm[1], 5), round(to[0], 5), round(to[1], 5), n, fibre, bore_m)
    if ck in _PLAN_CACHE:
        return _PLAN_CACHE[ck]
    routes = diverse_routes(frm, to, n)
    if not routes:
        return {"error": "no route", "from": frm, "to": to}
    geoms = [g for g, _ in routes]; dists = [round(d, 1) for _, d in routes]
    endpoints = [frm, to]
    total = round(sum(dists), 1)
    shared_km, pairs = shared_analysis(geoms, endpoints)
    sep = min_separation_m(geoms, endpoints)
    res = {
        "from": frm, "to": to, "n": len(geoms),
        "routes": [{"id": i + 1, "length_km": dists[i],
                    "geometry": {"type": "LineString", "coordinates": geoms[i]}}
                   for i in range(len(geoms))],
        "total_route_km": total,
        "diversity": {"min_separation_m_midhaul": sep,
                      "shared_street_km": shared_km, "shared_pairs": pairs,
                      "note": "shared street = same road; engineer opposite verges / "
                              "separate duct bank for >=50 ft (acceptable worst case)"},
        "cost": cost(total, fibre, bore_m),
        "disclaimer": "INDICATIVE auto-routed corridors (OSRM driving paths) — not "
                      "engineered alignments. Subject to survey, DBYD and carrier confirmation.",
    }
    if len(_PLAN_CACHE) > 500:
        _PLAN_CACHE.clear()
    _PLAN_CACHE[ck] = res
    return res

def _geocode(addr):
    u = "https://dchub.cloud/api/v1/geocode?address=" + urllib.parse.quote(addr)
    with urllib.request.urlopen(u, timeout=15) as r:
        d = json.load(r)
    return (d["lng"], d["lat"]) if "lat" in d else None

def _parse_pt(s):
    if "," in s and all(_isnum(x) for x in s.split(",")[:2]):
        a, b = (float(x) for x in s.split(",")[:2]); return (b, a)  # "lat,lng" -> (lng,lat)
    return _geocode(s)

def _isnum(x):
    try: float(x); return True
    except: return False

@fiber_route_plan_bp.route("/api/v1/route-plan", methods=["GET", "POST"])
def route_plan():
    a = request.values
    frm = _parse_pt(a.get("from", "")); to = _parse_pt(a.get("to", ""))
    if not frm or not to:
        return jsonify({"error": "from and to required (lat,lng or address)",
                        "example": "?from=-27.43611,153.12225&to=-27.46552,153.02964&n=4"}), 400
    try:
        n = max(1, min(6, int(a.get("n", 4))))
    except Exception:
        n = 4
    fibre = "1440F" if a.get("fibre") == "1440F" else "720F"
    bore_m = float(a.get("bore_m", 0) or 0)
    return jsonify(plan(frm, to, n, fibre, bore_m))

if __name__ == "__main__":
    import sys
    SITE = (153.12225, -27.43611); B1 = (153.029645, -27.465524)
    r = plan(SITE, B1, n=4, bore_m=500)
    print("routes:", r["n"], "| lengths km:", [x["length_km"] for x in r["routes"]])
    print("total km:", r["total_route_km"])
    print("diversity:", r["diversity"]["min_separation_m_midhaul"], "m min sep |",
          r["diversity"]["shared_street_km"], "km shared |", r["diversity"]["shared_pairs"])
    print("cost:", r["cost"])
