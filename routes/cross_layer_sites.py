"""shell#41 WS5 (2026-07-29) — cross-layer spatial site DISCOVERY.

GET /api/v1/sites/cross-layer

The missing verb in the siting rail. `rank_sites` ranks candidates the CALLER
already has; `site_selection_canvas` reasons at market level with no spatial
predicate. Neither one *produces* a candidate set from the physical layers. This
does: it walks the 126,840-row substation layer as the search space and attaches
fiber, carrier, DCPI-market and (optionally) floodplain evidence to each anchor.

WHY THE SUBSTATION IS THE ANCHOR
  Parcels are blocked — `parcel_boundaries` is 132,557 rows in ONE county
  (Loudoun, VA); `land_parcels` is read by no route at all. Existing data centres
  are competitors, not sites. The substation layer is the only geocoded layer
  dense enough to be a search space, and "a substation of the right class, near
  fiber, in a good market" is the locus a developer actually works backwards from.

WHAT THIS DELIBERATELY REFUSES TO ANSWER  (declared in constraint_coverage, never
approximated — an agent must be able to see the hole)
  • headroom_mw — NO measured MW headroom exists anywhere in this system.
    `substations.available_mva` is populated on ~0 rows (live at Ashburn:
    with_available_mva_gt0 = 0, capacity_coverage_pct = 3.4); the
    /api/v1/grid/status "grid_headroom" number is a voltage-CLASS ladder
    (nlr_intelligence.py:457-465) summed over 10 substations, i.e. nameplate
    class, not headroom; get_grid_scoreboard (47 zones, 7 feeds) carries no
    headroom field at all. WS6 (commit aa3b4b92) already adjudicated this and
    withholds the verdict without a numeric basis. There is nothing to filter on.
  • parcel acreage / zoning / ownership — one county of coverage (WS4).
  • feeder capacity — SPLIT by `hosting_capacity_feeders.capacity_type`
    (2026-07-30). Rows typed 'load' ARE utility-published load-serving
    capacity and are surfaced with value + unit + basis — single-distribution-
    feeder scale (~7.5-32 MW), never campus-scale headroom. Rows typed 'gen'
    answer "can I INJECT", never "can I ENERGISE" — wrong physical quantity,
    still excluded by design. The old clause declared the WHOLE table gen,
    which had become factually wrong (9 of 24 live territories are load).
  • transmission-line distance — `transmission_lines` has ZERO coordinate
    columns across 94,626 rows. Every "near transmission" answer in this
    codebase is really "near the from_sub substation". Offering a line-distance
    filter would imply geometry that does not exist.
  • min_signal_tier — `market_power_scores.signal_tier` is NULL on all served
    rows today; the filter would either drop everything or mean nothing.

WHICH FIBER
  `fcc_fiber_hex` (2,640,850 rows, 52 states, as_of 2025-12-31 = FCC BDC
  served-location coverage) and `carrier_facility_presence` (PeeringDB carrier
  POPs). NOT `fiber_routes` — 320 of a live 500-row sample are
  route_type='metro_inferred' with source_id prefixed 'synth-': a drawn line
  between two PeeringDB facilities, not a surveyed route. It must never back a
  distance claim. Coverage ≠ conduit: a hex means "FCC reports served locations
  in this ~0.7 km² cell", not "there is a strand you can splice".

COLUMN-NAME LANDMINES (one helper against one convention silently returns zero
rows on the others): substations + fcc_fiber_hex use lat/lng · power_plants uses
lat/LON · discovered_facilities uses latitude/longitude · market_power_scores
uses latitude/longitude · carrier_facility_presence uses facility_lat/facility_lng.

COST CONTRACT — the pool lesson (sitemap stampede). A geographic scope is
MANDATORY (400 without one). Every SQL carries a LIMIT. The number of DB round
trips is bounded and INDEPENDENT of `limit`: 1 anchors + 1 markets, +1 feeders
(when anchors exist), +1 iff fiber was asked for, +1 iff carriers were asked
for. Per-point enrichment is banned:
/api/infrastructure/connectivity/score measures 1.2-1.9 s PER POINT against
fcc_fiber_hex, so N of those would blow both the request budget and the pool.
Proximity is resolved by one set-wide bbox query + a coarse grid index in Python.

Registration: SAFE ZONE in main.py (~line 1866), next to temporal_capture.
Late-line registration silently 404s on Railway (press_loop, market_deep_dive
and competitor_recon were each bitten).
"""

from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
# ★ Fail-soft, matching this module's own `try: import requests` convention.
# tests/test_cross_layer_public_reason_hygiene.py execs the route with ALL
# first-party imports BLOCKED so every degraded branch is deterministic — a
# hard top-level import breaks that harness. When the helper is unavailable we
# publish "unknown", which the vocabulary already defines as "read it
# defensively". An absent-or-honest shape beats a guessed one.
try:
    from util.constraint_coverage_shape import shape_of as _shape_of
except Exception:                                    # pragma: no cover
    _shape_of = None


def _cc_shape(value):
    return _shape_of(value) if _shape_of else "unknown"

# Module-level so the floodplain read is injectable in a unit test (a function
# -local `import requests` cannot be stubbed) and so a missing dependency
# degrades to status="unknown" instead of raising inside a live request.
try:
    import requests as _requests
except Exception:                                    # pragma: no cover
    _requests = None

cross_layer_sites_bp = Blueprint("cross_layer_sites", __name__)

SEARCH_VERSION = "cross-layer/2026-07-29"
FIBER_AS_OF = "2025-12-31"          # fcc_fiber_hex.as_of, live-verified
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
DEFAULT_MAX_MARKET_KM = 75.0        # nearest-CENTROID join, never containment

_ANCHOR_SQL_LIMIT = 4000            # same cap depth_master_shell.py:456 uses
_SET_SQL_LIMIT = 20000              # fiber / carrier / feeder set-wide reads
_MAX_RADIUS_KM = 250.0
_MINT_CAP = 50                      # candidates cost 2 round trips each
_FEEDER_PAD_KM = 25.0               # feeder bbox pad around the anchor pool

# Floodplain is a per-point EXTERNAL call. Bounded three ways so a wide search
# can never turn into a FEMA hammer: call cap, wall-clock budget, and a hard
# `limit` cap whenever the clause is requested.
_FLOOD_MAX_CALLS = 25
_FLOOD_BUDGET_S = 10.0
_FLOOD_LIMIT_CAP = 25
_FLOOD_CACHE: dict = {}
_FLOOD_CACHE_TTL = 6 * 3600
_FLOOD_CACHE_MAX = 4000

_KV_IN_NAME = re.compile(r"(\d{2,3})\s*kv", re.I)


# ── small helpers ──────────────────────────────────────────────────────────
def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _haversine_km(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _conn():
    """Own connection, autocommit, short connect timeout. Returns None (never
    raises) so every caller degrades to a declared-unavailable answer instead of
    a 500. Closed explicitly in a finally — never left to @contextmanager GC."""
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _voltage(max_kv, kv, name):
    """→ (kv | None, basis). HIFLD voltage columns are sparse and store 0 even
    for a substation literally named 'Ashburn 500kV' (depth_master_shell.py:474),
    so a kV embedded in the NAME is the documented fallback. The basis travels
    with the number: never let a name-regex value pass as a measured column."""
    v = _num(max_kv)
    if v and v > 0:
        return v, "column"
    v = _num(kv)
    if v and v > 0:
        return v, "column"
    m = _KV_IN_NAME.search(str(name or ""))
    if m:
        try:
            return float(m.group(1)), "name_regex"
        except ValueError:
            pass
    return None, "unknown"


class _Grid:
    """Coarse ~11 km lat/lng bucket index. Turns the N_anchors × N_points
    nearest-neighbour scan (100 × 20,000 = 2M haversines) into a handful of
    bucket lookups. Pure Python, no PostGIS, no extra query."""

    def __init__(self, cell_deg=0.1):
        self.cell = cell_deg
        self.buckets: dict = {}

    def add(self, lat, lng, payload):
        k = (int(math.floor(lat / self.cell)), int(math.floor(lng / self.cell)))
        self.buckets.setdefault(k, []).append((lat, lng, payload))

    def near(self, lat, lng, radius_km):
        span = max(1, int(math.ceil(radius_km / (111.0 * self.cell))) + 1)
        i0 = int(math.floor(lat / self.cell))
        j0 = int(math.floor(lng / self.cell))
        out = []
        for i in range(i0 - span, i0 + span + 1):
            for j in range(j0 - span, j0 + span + 1):
                b = self.buckets.get((i, j))
                if b:
                    out.extend(b)
        return out


# ── FEMA NFHL — 3-state, fail-CLOSED ───────────────────────────────────────
def _flood_status(lat, lng, timeout=8.0):
    """→ {'status', 'zone', 'zone_subtype', 'sfha', 'source'}.

    status ∈ in_sfha | outside_sfha | undetermined | unmapped | unknown.

    ★ THE TRAP THIS EXISTS TO KILL: the shipped helper
    (risk_assessment_api.py:141, patched in the same change) converted an HTTP
    200 carrying `features: []` into flood_zone 'X' / flood_risk 'low'. Probed
    live 2026-07-29, `features == []` comes back for Alaska interior
    (63.5, -149.5) AND for Ireland (53.0, -8.0) — i.e. the old code would
    confidently certify an Irish site as outside the US 100-year floodplain. "No
    coverage" is NOT "no hazard". Zone 'D' (rural ND, probed) is *undetermined*,
    not safe. Only in_sfha / outside_sfha are answers; everything else fails the
    exclude_sfha filter CLOSED and is counted in dropped_by_constraints."""
    key = (round(float(lat), 3), round(float(lng), 3))
    hit = _FLOOD_CACHE.get(key)
    now = time.time()
    if hit and (now - hit[0]) < _FLOOD_CACHE_TTL:
        return hit[1]
    out = {"status": "unknown", "zone": None, "zone_subtype": None,
           "sfha": None, "source": "FEMA NFHL"}
    if _requests is None:
        return out                       # dependency absent → unknown, never safe
    try:
        # Working base, live-probed 2026-07-29 (0.17-0.53 s/point). The old
        # hazards.fema.gov/gis/nfhl/... base returns HTTP 404 (WebSEAL).
        url = ("https://hazards.fema.gov/arcgis/rest/services"
               "/public/NFHL/MapServer/28/query")
        resp = _requests.get(url, params={
            "geometry": "%s,%s" % (lng, lat),
            "geometryType": "esriGeometryPoint",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=timeout)
        if resp.status_code == 200:
            feats = (resp.json() or {}).get("features") or []
            if not feats:
                out["status"] = "unmapped"
            else:
                at = feats[0].get("attributes") or {}
                zone = at.get("FLD_ZONE")
                sub = at.get("ZONE_SUBTY")
                sfha = at.get("SFHA_TF")
                out.update({"zone": zone, "zone_subtype": sub,
                            "sfha": (sfha == "T") if sfha in ("T", "F") else None})
                if sfha == "T":
                    out["status"] = "in_sfha"
                elif str(zone or "").upper() == "D" or "UNDETERMINED" in str(sub or "").upper():
                    out["status"] = "undetermined"
                elif sfha == "F":
                    out["status"] = "outside_sfha"
                else:
                    out["status"] = "undetermined"
    except Exception:
        out["status"] = "unknown"          # transient → fail closed, never 'safe'
    if len(_FLOOD_CACHE) < _FLOOD_CACHE_MAX:
        _FLOOD_CACHE[key] = (now, out)
    return out


# ── scope parsing ──────────────────────────────────────────────────────────
def _parse_scope(args):
    """→ (scope_dict, error_str). A geographic scope is REQUIRED: an unscoped
    variant over a 126,840-row and a 2,640,850-row table is the sitemap-stampede
    incident again."""
    bbox = (args.get("bbox") or "").strip()
    state = (args.get("state") or "").strip().upper()
    lat = args.get("lat")
    lon = args.get("lon") if args.get("lon") is not None else args.get("lng")

    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            return None, "bbox must be 'min_lat,min_lng,max_lat,max_lng'"
        try:
            mnla, mnlo, mxla, mxlo = (float(p) for p in parts)
        except ValueError:
            return None, "bbox values must be numeric"
        if mnla >= mxla or mnlo >= mxlo:
            return None, "bbox must be min_lat,min_lng,max_lat,max_lng (min < max)"
        if (mxla - mnla) > 20 or (mxlo - mnlo) > 20:
            return None, "bbox is too large (max 20 degrees per side)"
        return {"kind": "bbox", "min_lat": mnla, "min_lng": mnlo,
                "max_lat": mxla, "max_lng": mxlo}, None

    if lat is not None and lon is not None:
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError):
            return None, "lat/lon must be numeric"
        try:
            rad = float(args.get("radius_km") or 50.0)
        except (TypeError, ValueError):
            return None, "radius_km must be numeric"
        if not (0 < rad <= _MAX_RADIUS_KM):
            return None, "radius_km must be between 0 and %d" % int(_MAX_RADIUS_KM)
        return {"kind": "point", "lat": la, "lng": lo, "radius_km": rad}, None

    if state:
        if len(state) != 2:
            return None, "state must be a 2-letter code"
        return {"kind": "state", "state": state}, None

    return None, ("a geographic scope is required — pass bbox=min_lat,min_lng,"
                  "max_lat,max_lng OR lat=&lon=&radius_km= OR state=XX")


def _float_arg(args, name):
    v = args.get(name)
    if v is None or str(v).strip() == "":
        return None, None
    try:
        return float(v), None
    except (TypeError, ValueError):
        return None, "%s must be numeric" % name


def _bool_arg(args, name):
    return str(args.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# The gen half of the feeder story, kept in its original words: it was and
# remains the right sentence for capacity_type='gen' rows. What changed on
# 2026-07-30 is that it may no longer be generalised to the whole table —
# load-typed rows are a different physical quantity and are surfaced.
_FEEDER_GEN_REASON = (
    "hosting_capacity_feeders rows typed 'gen' are DER/generation hosting "
    "capacity ('can I inject'), not load energisation capacity. Wrong "
    "physical quantity — excluded by design, not by omission.")


def _feeder_capacity_clause(rows, basis_map=None):
    """→ the constraint_coverage['feeder_capacity_mw'] dict, from a LIVE read.

    `rows` = [(utility, capacity_mw_max, capacity_type, src_updated), ...]
    near the scope (capacity_mw_max never NULL — the query excludes flag-only
    rows). Split by capacity_type:

      • load rows exist  → the value is the best LOAD-typed feeder, with its
        utility, unit and basis attached. Gen rows NEVER contribute to the
        value, however large — they are counted and their exclusion reason
        travels alongside.
      • only gen rows    → unavailable, keeping the wrong-physical-quantity
        reason (it is correct for gen).
      • no rows at all   → unavailable + reason. Never 0: absence of feeder
        coverage is unknown, not zero.

    The scale caveat is mandatory: these are single DISTRIBUTION feeders
    (~7.5-32 MW territory maxima), not campus-scale transmission service.
    """
    load_rows = [r for r in rows
                 if (r[2] or "gen") == "load" and r[1] is not None]
    gen_n = sum(1 for r in rows if (r[2] or "gen") != "load")
    if load_rows:
        best = max(load_rows, key=lambda r: float(r[1]))
        utility = best[0] or ""
        # PECO stores MVA (its own legend's unit); everything else is MW.
        # The mislabel-proof signal is the utility label, which carries the
        # unit warning end-to-end.
        unit = ("MVA (upper bound on MW — the publisher states no power "
                "factor)") if "MVA" in utility else "MW"
        clause = {
            "status": "validated",
            "value": float(best[1]),
            "unit": unit,
            "utility": utility,
            "as_of": best[3],
            "basis": ("single-distribution-feeder LOAD capacity, utility-"
                      "published — the best load-typed feeder within ~%d km "
                      "of the scope's anchors. NOT campus-scale transmission "
                      "headroom: one feeder, informational, not binding "
                      "interconnection guidance." % int(_FEEDER_PAD_KM)),
            "load_feeder_rows_in_scope": len(load_rows),
            "source": "hosting_capacity_feeders WHERE capacity_type='load' "
                      "(utility-published hosting-capacity GIS)",
        }
        if basis_map:
            ub = basis_map.get(utility)
            if ub:
                clause["utility_capacity_basis"] = ub
        if gen_n:
            clause["gen_rows_excluded"] = gen_n
            clause["gen_exclusion_reason"] = _FEEDER_GEN_REASON
        return clause
    if gen_n:
        return {"status": "unavailable",
                "reason": _FEEDER_GEN_REASON + (
                    " Only gen-typed feeders exist near this scope (%d rows); "
                    "load-typed coverage is listed at "
                    "/api/v1/grid/hosting-capacity/coverage." % gen_n)}
    return {"status": "unavailable", "reason":
            "no utility-published hosting-capacity feeder rows near this "
            "scope. Load-serving feeder capacity is published only for a "
            "limited set of utility territories — see "
            "/api/v1/grid/hosting-capacity/coverage for which."}


# ── the route ──────────────────────────────────────────────────────────────
@cross_layer_sites_bp.route("/api/v1/sites/cross-layer")
def cross_layer_sites():
    args = request.args

    scope, err = _parse_scope(args)
    if err:
        return jsonify({"_entity": "error", "ok": False, "error": "scope_required",
                        "message": err,
                        "example": "/api/v1/sites/cross-layer?lat=39.0437&lon=-77.4875"
                                   "&radius_km=50&min_voltage_kv=230&max_fiber_km=5"}), 400

    numeric = {}
    for k in ("min_voltage_kv", "max_fiber_km", "max_carrier_km",
              "min_dcpi", "max_market_km"):
        val, e = _float_arg(args, k)
        if e:
            return jsonify({"_entity": "error", "ok": False,
                            "error": "bad_parameter", "message": e}), 400
        numeric[k] = val
    max_market_km = numeric["max_market_km"] or DEFAULT_MAX_MARKET_KM
    exclude_sfha = _bool_arg(args, "exclude_sfha")
    include_fiber = _bool_arg(args, "include_fiber") or numeric["max_fiber_km"] is not None
    include_carriers = _bool_arg(args, "include_carriers") or numeric["max_carrier_km"] is not None
    include_flood = _bool_arg(args, "include_flood") or exclude_sfha
    iso_filter = [s.strip().upper() for s in (args.get("iso") or "").split(",") if s.strip()]

    try:
        limit = int(args.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))
    limit_capped_by = None
    if include_flood and limit > _FLOOD_LIMIT_CAP:
        # A floodplain answer is one external call per site. The cap is what
        # keeps a wide search from becoming a FEMA hammer.
        limit = _FLOOD_LIMIT_CAP
        limit_capped_by = "floodplain_external_call_budget"

    # coverage is the honesty spine (mirrors rank_sites' constraint_coverage,
    # interconnection_queues.py:1004). A clause the caller did NOT ask for is
    # ABSENT from this dict — it is never reported as "validated".
    coverage = {
        "headroom_mw": {"status": "unavailable", "reason":
            "No measured megawatt headroom exists in any data layer, so a "
            "headroom value is withheld rather than estimated. Detail: "
            "substations.available_mva is populated on ~0 rows; the "
            "/api/v1/grid/status 'grid_headroom' figure is a voltage-class "
            "ladder (nameplate class, not headroom); get_grid_scoreboard "
            "carries no headroom field. The one transmission-voltage source "
            "ever ingested (Avista) turned out to be a utility GENERATION-"
            "interconnection study (publisher-labelled "
            "'GenerationInterconnectionHeatMap', MW field a pass/fail flag "
            "on nine discrete study sizes) and was reclassified 'gen' on "
            "2026-07-30 — so zero measured transmission LOAD capability "
            "exists. Distribution-feeder LOAD capacity is published for "
            "some territories and reported separately under "
            "feeder_capacity_mw; it is single-feeder scale, not campus "
            "scale."},
        "parcel_acres": {"status": "unavailable", "reason":
            "Parcel data covers only one US county, so acreage, zoning and "
            "ownership are not evaluated. Detail: parcel_boundaries covers "
            "Loudoun County, VA alone (132,557 rows), and the land_parcels "
            "table is not yet served by any public route."},
        # Overwritten below from a LIVE read of hosting_capacity_feeders,
        # split by capacity_type — see _feeder_capacity_clause(). This
        # initial value only survives if the feeder read never ran.
        "feeder_capacity_mw": {"status": "unavailable", "reason":
            "hosting-capacity feeder layer was not read for this scope "
            "(request failed before the feeder query) — value withheld "
            "rather than guessed"},
        "transmission_line_km": {"status": "unavailable", "reason":
            "Distance to transmission lines cannot be measured, because the "
            "line records carry no coordinates. Detail: transmission_lines "
            "has no coordinate columns across any of its 94,626 rows, so a "
            "'near transmission' figure here would silently measure "
            "distance to a substation instead."},
    }
    dropped = {}
    notes = []
    filters_not_applied = []

    conn = _conn()
    if conn is None:
        return jsonify({"_entity": "error", "ok": False, "error": "no_db",
                        "message": "site layer temporarily unavailable — retry"}), 503

    anchors = []
    markets = []
    anchors_truncated = False
    fiber_truncated = False
    carrier_truncated = False
    fiber_pts = None
    carrier_pts = None
    feeder_rows = None
    market_vintage = None
    try:
        cur = conn.cursor()
        try:
            cur.execute("SET statement_timeout = '8000'")
        except Exception:
            pass

        # ── QUERY 1 — anchors (substations). Bounded, LIMIT-ed, scope-required.
        sql = ("SELECT id, name, operator, voltage_kv, max_voltage_kv, "
               "capacity_mva, available_mva, state, county, lat, lng "
               "FROM substations WHERE lat IS NOT NULL AND lng IS NOT NULL")
        params = []
        if scope["kind"] == "bbox":
            sql += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params += [scope["min_lat"], scope["max_lat"], scope["min_lng"], scope["max_lng"]]
        elif scope["kind"] == "point":
            dlat = scope["radius_km"] / 111.0
            dlng = scope["radius_km"] / (111.0 * max(0.05, math.cos(math.radians(scope["lat"]))))
            sql += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params += [scope["lat"] - dlat, scope["lat"] + dlat,
                       scope["lng"] - dlng, scope["lng"] + dlng]
        else:
            sql += " AND upper(state) = %s"
            params.append(scope["state"])
        sql += " LIMIT %s"
        params.append(_ANCHOR_SQL_LIMIT)
        cur.execute(sql, params)
        rows = cur.fetchall()
        anchors_truncated = len(rows) >= _ANCHOR_SQL_LIMIT

        volt_known = 0
        for (sid, name, operator, kv, maxkv, cap_mva, avail_mva,
             st, county, la, lo) in rows:
            la, lo = _num(la), _num(lo)
            if la is None or lo is None:
                continue
            dist = None
            if scope["kind"] == "point":
                dist = _haversine_km(scope["lat"], scope["lng"], la, lo)
                if dist > scope["radius_km"]:
                    continue
            v, basis = _voltage(maxkv, kv, name)
            if basis != "unknown":
                volt_known += 1
            anchors.append({
                "id": sid, "name": name, "operator": operator,
                "voltage_kv": v, "voltage_basis": basis,
                "state": st, "county": county,
                "lat": la, "lng": lo,
                "scope_distance_km": (round(dist, 2) if dist is not None else None),
            })

        evaluated_total = len(anchors)
        if numeric["min_voltage_kv"] is not None:
            keep = [a for a in anchors if a["voltage_kv"] is not None
                    and a["voltage_kv"] >= numeric["min_voltage_kv"]]
            dropped["voltage_kv_below_min"] = sum(
                1 for a in anchors if a["voltage_kv"] is not None
                and a["voltage_kv"] < numeric["min_voltage_kv"])
            dropped["voltage_kv_unknown"] = sum(
                1 for a in anchors if a["voltage_kv"] is None)
            anchors = keep
            coverage["voltage_kv"] = {
                "status": "partial",
                "evaluated_n": volt_known, "total_n": evaluated_total,
                "partial_reason":
                    "HIFLD voltage columns are sparse and store 0 even for "
                    "substations named '<n>kV'; rows with no column value fall "
                    "back to a kV parsed from the NAME (voltage_basis=name_regex). "
                    "A row with neither is dropped by this filter, never assumed.",
            }

        # rank BEFORE enrichment so the enrichment set stays small and bounded.
        if scope["kind"] == "point":
            anchors.sort(key=lambda a: a["scope_distance_km"])
            ranking = "distance_from_scope_point_asc"
        else:
            anchors.sort(key=lambda a: (-(a["voltage_kv"] or 0), str(a["name"] or "")))
            ranking = "voltage_kv_desc (no scope point to measure distance from)"
        pool = anchors[:max(limit * 4, limit)]

        # ── QUERY 2 — DCPI markets. 311 rows; one select, joined in Python.
        try:
            cur.execute(
                "SELECT market_slug, market_name, state, iso, latitude, longitude, "
                "verdict, excess_power_score, constraint_score, time_to_power_months, "
                "signal_tier, computed_at FROM market_power_scores "
                "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
                "AND published = true LIMIT 2000")
            mrows = cur.fetchall()
        except Exception:
            # `published` is absent on some deploys (routes/agent_broadcast.py:305).
            mrows = []
            try:
                cur.execute(
                    "SELECT market_slug, market_name, state, iso, latitude, longitude, "
                    "verdict, excess_power_score, constraint_score, time_to_power_months, "
                    "signal_tier, computed_at FROM market_power_scores "
                    "WHERE latitude IS NOT NULL AND longitude IS NOT NULL LIMIT 2000")
                mrows = cur.fetchall()
            except Exception:
                mrows = []
        for (slug, mname, mstate, miso, mla, mlo, verdict, excess, constraint,
             ttp, sig, computed_at) in mrows:
            mla, mlo = _num(mla), _num(mlo)
            if mla is None or mlo is None:
                continue
            markets.append({"market_slug": slug, "market_name": mname,
                            "state": mstate, "iso": (miso or None),
                            "lat": mla, "lng": mlo, "verdict": verdict,
                            "excess": _num(excess), "constraint": _num(constraint),
                            "ttp": _num(ttp), "signal_tier": sig})
            if computed_at is not None:
                try:
                    d = computed_at.date().isoformat()
                    if market_vintage is None or d > market_vintage:
                        market_vintage = d
                except Exception:
                    pass

        # ── QUERY 3 (conditional) — fiber coverage, ONE set-wide bbox read.
        if include_fiber and pool:
            pad = (numeric["max_fiber_km"] or 10.0)
            fiber_pts, fiber_truncated = _set_read(
                cur, "SELECT lat, lng, brand_name FROM fcc_fiber_hex",
                "lat", "lng", pool, pad)

        # ── QUERY 4 (conditional) — carrier POPs, ONE set-wide bbox read.
        if include_carriers and pool:
            pad = (numeric["max_carrier_km"] or 50.0)
            carrier_pts, carrier_truncated = _set_read(
                cur, ("SELECT facility_lat, facility_lng, carrier_name "
                      "FROM carrier_facility_presence"),
                "facility_lat", "facility_lng", pool, pad)

        # ── QUERY 5 — hosting-capacity feeders near the scope: ONE bounded
        # bbox read, split downstream by capacity_type. Rows typed 'load'
        # are the real "what can a new load DRAW here" number (single-
        # distribution-feeder scale); 'gen' rows stay excluded by design.
        # Load-first ordering so gen rows can never crowd load rows out of
        # the LIMIT. Own try/except: a feeder-table problem degrades this
        # ONE clause to its declared-unread reason, never the whole search.
        if pool:
            try:
                _la = [a["lat"] for a in pool]
                _lo = [a["lng"] for a in pool]
                _pad_lat = _FEEDER_PAD_KM / 111.0
                _pad_lng = _FEEDER_PAD_KM / (111.0 * max(
                    0.05, math.cos(math.radians((min(_la) + max(_la)) / 2))))
                cur.execute(
                    "SELECT utility, capacity_mw_max, capacity_type, "
                    "src_updated FROM hosting_capacity_feeders "
                    "WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
                    "AND capacity_mw_max IS NOT NULL "
                    "ORDER BY (capacity_type = 'load') DESC, "
                    "capacity_mw_max DESC LIMIT %s",
                    [min(_la) - _pad_lat, max(_la) + _pad_lat,
                     min(_lo) - _pad_lng, max(_lo) + _pad_lng,
                     _SET_SQL_LIMIT])
                feeder_rows = cur.fetchall()
            except Exception:
                feeder_rows = None
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        # Fail soft: a declared degraded 200, never a 500 on a live route.
        return jsonify({"_entity": "cross_layer_sites", "ok": False,
                        "error": "query_failed", "detail": str(e)[:200],
                        "scope": scope, "results": [],
                        "constraint_coverage": coverage,
                        "constraint_coverage_shape": _cc_shape(coverage),
                        "_source": "DC Hub — dchub.cloud"}), 200

    # ── enrichment (pure Python over the sets already fetched) ─────────────
    fiber_grid = None
    if fiber_pts is not None:
        fiber_grid = _Grid()
        for la, lo, brand in fiber_pts:
            fiber_grid.add(la, lo, brand)
        coverage["fiber_km"] = {
            "status": "validated" if not fiber_truncated else "partial",
            "source": "fcc_fiber_hex (FCC BDC served-location coverage)",
            "as_of": FIBER_AS_OF, "geography": "US only",
            "meaning": "distance to the nearest hex where the FCC reports SERVED "
                       "LOCATIONS — coverage, NOT a conduit/strand you can splice",
            **({"partial_reason": "set-wide fiber read hit its %d-row cap; "
                                  "distances are upper bounds" % _SET_SQL_LIMIT}
               if fiber_truncated else {}),
        }
    carrier_grid = None
    if carrier_pts is not None:
        carrier_grid = _Grid()
        for la, lo, cname in carrier_pts:
            carrier_grid.add(la, lo, cname)
        coverage["carrier_km"] = {
            "status": "validated" if not carrier_truncated else "partial",
            "source": "carrier_facility_presence (PeeringDB carrier POPs)",
            **({"partial_reason": "set-wide carrier read hit its %d-row cap"
                % _SET_SQL_LIMIT} if carrier_truncated else {}),
        }

    # feeder capacity: LIVE clause from the rows just read. basis_map is the
    # per-utility capacity_basis the ingest module attaches to every serving
    # row — fail-soft: without it the clause still carries its own basis.
    if feeder_rows is not None:
        _basis_map = None
        try:
            from routes.hosting_capacity_ingest import _SOURCE_CAPACITY_BASIS \
                as _basis_map
        except Exception:
            _basis_map = None
        coverage["feeder_capacity_mw"] = _feeder_capacity_clause(
            feeder_rows, _basis_map)

    _derive = None
    try:
        from routes.dcpi import derive_composite_score as _derive
    except Exception:
        _derive = None

    if markets:
        coverage["dcpi"] = {
            "status": "validated" if _derive is not None else "partial",
            "markets_available": len(markets),
            "join": "nearest market CENTROID within max_market_km=%s — NOT "
                    "containment; market.distance_km travels with every row so "
                    "an attribution from hundreds of km away is visible"
                    % max_market_km,
            **({} if _derive is not None else
               {"partial_reason": "composite_score deriver unavailable in this "
                                  "process — verdict/components still returned"}),
        }
    else:
        coverage["dcpi"] = {"status": "unavailable",
                            "reason": "market_power_scores returned no geocoded rows"}

    if numeric["min_dcpi"] is not None and _derive is None:
        # Fail CLOSED: never return rows as if a filter that could not run had run.
        filters_not_applied.append("min_dcpi")

    survivors = []
    n_no_market = 0
    for a in pool:
        row = dict(a)
        if fiber_grid is not None:
            rad = numeric["max_fiber_km"] or 10.0
            best, brands = None, set()
            for la, lo, brand in fiber_grid.near(a["lat"], a["lng"], rad):
                d = _haversine_km(a["lat"], a["lng"], la, lo)
                if best is None or d < best:
                    best = d
                if d <= rad and brand:
                    brands.add(brand)
            row["fiber"] = {"nearest_hex_km": (round(best, 2) if best is not None else None),
                            "provider_count": (len(brands) or None),
                            "as_of": FIBER_AS_OF, "v": "published",
                            "source": "FCC BDC served-location coverage (fcc_fiber_hex)"}
        if carrier_grid is not None:
            rad = numeric["max_carrier_km"] or 50.0
            best, names = None, set()
            for la, lo, cname in carrier_grid.near(a["lat"], a["lng"], rad):
                d = _haversine_km(a["lat"], a["lng"], la, lo)
                if best is None or d < best:
                    best = d
                if d <= rad and cname:
                    names.add(cname)
            row["carriers"] = {"nearest_km": (round(best, 2) if best is not None else None),
                               "count_within_km": (len(names) or None),
                               "radius_km": rad, "v": "published",
                               "source": "PeeringDB carrier presence"}

        best_m, best_d = None, None
        for m in markets:
            d = _haversine_km(a["lat"], a["lng"], m["lat"], m["lng"])
            if best_d is None or d < best_d:
                best_m, best_d = m, d
        if best_m is not None and best_d is not None and best_d <= max_market_km:
            comp = None
            if _derive is not None:
                try:
                    comp = _derive(best_m["excess"], best_m["constraint"],
                                   best_m["ttp"], best_m["verdict"])
                except Exception:
                    comp = None
            row["market"] = {
                "market_slug": best_m["market_slug"],
                "market_name": best_m["market_name"],
                "iso": best_m["iso"], "state": best_m["state"],
                "distance_km": round(best_d, 1),
                "verdict": best_m["verdict"],
                "composite_score": comp,
                "excess_power_score": best_m["excess"],
                "constraint_score": best_m["constraint"],
                "time_to_power_months": best_m["ttp"],
                # UNRECORDED is not LOW. NULL on every served row today.
                "signal_tier": best_m["signal_tier"],
                "signal_tier_basis": (None if best_m["signal_tier"] else
                                      "unrecorded: the row's writer recorded no "
                                      "tier — unknown, NOT low"),
                "v": "inferred",
                "join_method": "nearest_market_centroid",
            }
        else:
            row["market"] = None
            n_no_market += 1

        # fail-closed market predicates
        if iso_filter:
            miso = ((row.get("market") or {}).get("iso") or "").upper()
            if miso not in iso_filter:
                dropped["iso_not_matched"] = dropped.get("iso_not_matched", 0) + 1
                continue
        if numeric["min_dcpi"] is not None:
            if _derive is None:
                continue                       # filter could not run → no rows
            c = (row.get("market") or {}).get("composite_score")
            if c is None or c < numeric["min_dcpi"]:
                dropped["dcpi_below_min_or_unmeasured"] = \
                    dropped.get("dcpi_below_min_or_unmeasured", 0) + 1
                continue
        if numeric["max_fiber_km"] is not None:
            fk = (row.get("fiber") or {}).get("nearest_hex_km")
            if fk is None or fk > numeric["max_fiber_km"]:
                dropped["fiber_km_over_max_or_unmeasured"] = \
                    dropped.get("fiber_km_over_max_or_unmeasured", 0) + 1
                continue
        if numeric["max_carrier_km"] is not None:
            ck = (row.get("carriers") or {}).get("nearest_km")
            if ck is None or ck > numeric["max_carrier_km"]:
                dropped["carrier_km_over_max_or_unmeasured"] = \
                    dropped.get("carrier_km_over_max_or_unmeasured", 0) + 1
                continue
        survivors.append(row)

    if n_no_market:
        notes.append("%d anchor(s) had no DCPI market centroid within %s km"
                     % (n_no_market, max_market_km))

    results = survivors[:limit]

    # ── floodplain: bounded post-filter over survivors ONLY ────────────────
    if include_flood:
        started = time.time()
        calls = 0
        kept = []
        for r in results:
            if calls >= _FLOOD_MAX_CALLS or (time.time() - started) > _FLOOD_BUDGET_S:
                r["flood"] = {"status": "not_checked", "reason":
                              "external-call budget exhausted (max %d calls / %.0f s)"
                              % (_FLOOD_MAX_CALLS, _FLOOD_BUDGET_S),
                              "source": "FEMA NFHL"}
            else:
                key = (round(r["lat"], 3), round(r["lng"], 3))
                cached = _FLOOD_CACHE.get(key)
                if not (cached and (time.time() - cached[0]) < _FLOOD_CACHE_TTL):
                    calls += 1
                fs = _flood_status(r["lat"], r["lng"])
                r["flood"] = dict(fs, v="published")
            if exclude_sfha and r["flood"].get("status") != "outside_sfha":
                dropped["floodplain_%s" % r["flood"].get("status")] = \
                    dropped.get("floodplain_%s" % r["flood"].get("status"), 0) + 1
                continue
            kept.append(r)
        if exclude_sfha:
            results = kept
        coverage["floodplain"] = {
            "status": "partial",
            "checked_n": calls, "call_cap": _FLOOD_MAX_CALLS,
            "source": "FEMA NFHL layer 28 (live per-point query)",
            "three_state": "in_sfha | outside_sfha | undetermined | unmapped | "
                           "unknown — only outside_sfha satisfies exclude_sfha",
            "fail_closed":
                "an unmapped, undetermined (zone D) or transiently-unknown point "
                "NEVER satisfies exclude_sfha. FEMA returns an empty feature set "
                "for any point outside NFHL coverage (verified live for Alaska "
                "interior and for Ireland) — reading that as 'zone X, low risk' "
                "is a confident falsehood, which is worse than declining.",
        }

    # ── candidate minting (fail-soft; NEVER breaks the search response) ────
    snapshot_id = "snapxl_" + (market_vintage or
                               datetime.now(timezone.utc).date().isoformat())
    minted = 0
    mint_meta = {}
    try:
        from routes.candidates import (mint_candidates,
                                       SEARCH_VERSION as _CAND_SV,
                                       TTL_DAYS as _CAND_TTL)
        mint_batch = []
        for r in results[:_MINT_CAP]:
            mint_batch.append({
                "queue_id": "sub:%s" % r.get("id"),
                "project_name": r.get("name"),
                "iso": ((r.get("market") or {}).get("iso")),
                "state": r.get("state"), "county": r.get("county"),
                "fuel_type": None, "capacity_mw": None,
                "lat": r["lat"], "lng": r["lng"],
                "fiber_km": ((r.get("fiber") or {}).get("nearest_hex_km")),
                "coordinate_precision": "substation_point",
            })
        if mint_batch:
            mint_cur = conn.cursor()
            mint_candidates(mint_cur, mint_batch, snapshot_id, {
                "surface": "cross-layer", "scope": scope,
                "min_voltage_kv": numeric["min_voltage_kv"],
                "max_fiber_km": numeric["max_fiber_km"],
                "max_carrier_km": numeric["max_carrier_km"],
                "min_dcpi": numeric["min_dcpi"],
                "max_market_km": max_market_km,
                "iso": iso_filter or None, "exclude_sfha": exclude_sfha})
            for r, m in zip(results, mint_batch):
                r["candidate_id"] = m.get("candidate_id")
                r["expires_at"] = m.get("expires_at")
                minted += 1
            mint_meta = {"candidate_search_version": _CAND_SV,
                         "candidate_ttl_days": _CAND_TTL}
    except Exception:
        minted = 0                     # search still serves; candidates absent
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # ── tier gate: the DCPI NUMBERS are Pro; the market list + verdict is free.
    paid = _is_paid()
    if not paid:
        for r in results:
            if r.get("market"):
                for f in ("composite_score", "excess_power_score",
                          "constraint_score", "time_to_power_months"):
                    r["market"][f] = None

    for r in results:
        r["representative_point"] = {"lat": r["lat"], "lng": r["lng"]}
        r["anchor"] = {"type": "substation", "id": r.pop("id", None),
                       "name": r.pop("name", None),
                       "operator": r.pop("operator", None),
                       "voltage_kv": r.pop("voltage_kv", None),
                       "voltage_basis": r.pop("voltage_basis", None)}
        r["site_evaluation_handoff"] = {
            "analyze_site": {"lat": r["lat"], "lon": r["lng"],
                             "include_risk": True, "include_fiber": True},
            "get_water_risk": {"lat": r["lat"], "lng": r["lng"]},
        }

    if numeric["max_fiber_km"] is not None:
        coverage.setdefault("fiber_km", {})["filter_applied"] = True
    if scope["kind"] == "point":
        coverage["substation_km"] = {"status": "validated",
                                     "source": "substations (HIFLD, 126,840 geocoded rows)"}

    payload = {
        "_entity": "cross_layer_sites",
        "ok": True,
        "snapshot_id": snapshot_id,
        "search_version": SEARCH_VERSION,
        **mint_meta,
        "scope": scope,
        "ranking": ranking,
        "filters_applied": {
            "min_voltage_kv": numeric["min_voltage_kv"],
            "max_fiber_km": numeric["max_fiber_km"],
            "max_carrier_km": numeric["max_carrier_km"],
            "min_dcpi": numeric["min_dcpi"],
            "max_market_km": max_market_km,
            "iso": iso_filter or None,
            "exclude_sfha": exclude_sfha,
            "limit": limit,
            **({"limit_capped_by": limit_capped_by} if limit_capped_by else {}),
        },
        **({"filters_not_applied": filters_not_applied} if filters_not_applied else {}),
        "count_returned": len(results),
        "anchors_in_scope": evaluated_total,
        "anchors_after_anchor_filters": len(anchors),
        "anchors_truncated": anchors_truncated,
        "anchors_truncated_note": (
            "the substation read hit its %d-row cap — narrow the scope; the "
            "returned set is a bounded sample, not the complete answer"
            % _ANCHOR_SQL_LIMIT) if anchors_truncated else None,
        "candidates_minted": minted,
        "constraint_coverage": coverage,
        "constraint_coverage_shape": _cc_shape(coverage),
        "dropped_by_constraints": dropped,
        "results": results,
        "db_round_trips": 2 + (1 if fiber_pts is not None else 0)
                            + (1 if carrier_pts is not None else 0)
                            + (1 if feeder_rows is not None else 0),
        "_source": "DC Hub — dchub.cloud",
        "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0 — cite as \"DC Hub, dchub.cloud\"",
        "provenance": _provenance(),
        "note": (
            "Cross-layer site DISCOVERY over the substation layer as anchor "
            "(126,840 geocoded rows). Fiber = FCC BDC served-location COVERAGE "
            "(fcc_fiber_hex, US only, as_of " + FIBER_AS_OF + ") — coverage, not "
            "conduit; fiber_routes is excluded because 64% of a live sample is "
            "synthetic 'metro_inferred' geometry. Carriers = PeeringDB POPs. "
            "DCPI is joined by NEAREST MARKET CENTROID within max_market_km "
            "(default 75) — never containment, and market.distance_km always "
            "travels with the row. Voltage is available for roughly 6 in 10 "
            "substations and falls back to a kV parsed from the name "
            "(voltage_basis says which). NO measured MW headroom exists in any "
            "layer, so no headroom filter is offered — see "
            "constraint_coverage.headroom_mw. Parcel acreage/zoning is one "
            "county of coverage. Every unmeasured value is null, never 0."
        ),
    }
    if not paid:
        payload["_locked_fields"] = ["market.composite_score",
                                     "market.excess_power_score",
                                     "market.constraint_score",
                                     "market.time_to_power_months"]
        payload["_required_tier"] = "pro"
        payload["_upgrade_cta"] = (
            "Site discovery, verdicts, fiber and carrier distances are free. The "
            "numeric DCPI scores are Pro — https://dchub.cloud/pricing")
    if notes:
        payload["notes"] = notes

    resp = jsonify(payload)
    # main.py's Bundle-6A after_request edge-caches every 200 under the
    # '/api/v1/sites/' PREFIX keyed by URL only (main.py:29352). This response
    # VARIES BY TIER (the DCPI numbers are masked for non-paid callers), so a
    # URL-keyed shared cache would serve one caller's Pro numbers to an
    # anonymous one. Setting Cache-Control here makes that hook skip us.
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _set_read(cur, select_sql, lat_col, lng_col, pool, pad_km):
    """ONE set-wide bbox read over an enrichment layer, sized to the union bbox
    of the anchor pool padded by pad_km. → (rows, truncated). Never per-point:
    /api/infrastructure/connectivity/score measures 1.2-1.9 s PER POINT against
    fcc_fiber_hex, so N calls would blow the request budget and the pool."""
    lats = [a["lat"] for a in pool]
    lngs = [a["lng"] for a in pool]
    mnla, mxla = min(lats), max(lats)
    mnlo, mxlo = min(lngs), max(lngs)
    dlat = max(pad_km, 1.0) / 111.0
    dlng = max(pad_km, 1.0) / (111.0 * max(0.05, math.cos(math.radians((mnla + mxla) / 2))))
    cur.execute(
        select_sql + (" WHERE %s BETWEEN %%s AND %%s AND %s BETWEEN %%s AND %%s LIMIT %%s"
                      % (lat_col, lng_col)),
        [mnla - dlat, mxla + dlat, mnlo - dlng, mxlo + dlng, _SET_SQL_LIMIT])
    out = []
    for la, lo, label in cur.fetchall():
        la, lo = _num(la), _num(lo)
        if la is None or lo is None:
            continue
        out.append((la, lo, label))
    return out, (len(out) >= _SET_SQL_LIMIT)


def _is_paid():
    """Best-effort plan read. Fail-soft AND fail-CLOSED: if we cannot establish
    a paid plan we mask the Pro numbers rather than leak them."""
    ranks = {"anonymous": 0, "anon": 0, "free": 0, "identified": 1, "starter": 2,
             "developer": 3, "pro": 4, "founding": 4, "enterprise": 5,
             "admin": 6, "internal": 6}
    paid = {"starter", "developer", "pro", "founding", "enterprise",
            "admin", "internal"}
    plan = "anonymous"
    try:
        from util.tier_gate import resolve_tier
        t, ctx = resolve_tier()
        plan = (ctx.get("plan") or t.name).lower()
    except Exception:
        pass
    try:
        from map_tier_gating import _detect_caller_tier
        ct, _ = _detect_caller_tier()
        ct = (ct or "anon").lower()
        if ranks.get(ct, -1) > ranks.get(plan, -1):
            plan = ct
    except Exception:
        pass
    try:
        admin = os.environ.get("DCHUB_ADMIN_KEY")
        if admin and request.headers.get("X-Admin-Key") == admin:
            plan = "admin"
    except Exception:
        pass
    return plan in paid


def _provenance():
    try:
        from routes.provenance import provenance_block
        return provenance_block(
            source=("HIFLD substations · FCC Broadband Data Collection fiber "
                    "coverage (as_of %s) · PeeringDB carrier presence · DC Hub "
                    "Power Index (DCPI) · FEMA NFHL" % FIBER_AS_OF),
            method=("substation anchors inside a caller-supplied geographic "
                    "scope, enriched by set-wide bbox reads + haversine; DCPI by "
                    "nearest market centroid (inferred, distance disclosed); "
                    "floodplain by live per-point FEMA NFHL query with a "
                    "three-state result that fails closed on no coverage"),
            default_v="inferred")
    except Exception:
        return None
