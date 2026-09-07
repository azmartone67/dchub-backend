"""find_sites.py — the INVERSE query: constraints in, candidate site areas out.

Every other DC Hub siting tool starts from a coordinate somebody already
picked: ``analyze_site`` scores a lat/lon, ``get_composite_site_score`` grades
one, ``rank_sites`` orders candidates the caller already enriched, and
``find_alternatives`` needs a reference site to be alternative TO. None of them
answer the question a site selector actually opens with — "where should I even
be looking?"

This endpoint answers that one, and it does it WITHOUT inventing geography.

  ANCHOR STRATEGY — power first, real coordinates only
  ----------------------------------------------------
  Candidates are anchored on real HIFLD substations at or above a requested
  voltage, because (a) grid access is the binding constraint on nearly every
  data-center siting decision and (b) a substation is a SOURCED point we can
  cite, not a synthesised one. Tessellating a state into grid cells would
  produce tidier-looking "sites" whose coordinates DC Hub made up; that is the
  one thing this endpoint will not do.

  ★ WHAT A CANDIDATE IS NOT
  A candidate is a SEARCH AREA anchored on a substation. It is not a parcel,
  not a listing, not land that is for sale, and carries no claim that anything
  there is available, zoned, or buildable. DC Hub holds no land-ownership or
  land-for-sale data, and hosted parcel boundaries cover one county
  (Loudoun VA) — so ``analyze_parcel`` will 404 for most of these points, by
  design. The honest read is "start looking here", and the response says so in
  ``what_this_is`` rather than leaving the caller to infer it.

  ★ AN UNEVALUABLE CONSTRAINT IS NEVER A PASSED CONSTRAINT
  Each optional constraint is filtered ONLY when its layer actually answered.
  When a layer is missing, errors, or has no queryable coordinate columns, the
  candidate set is NOT filtered by it, every affected constraint is reported
  ``applied: false`` with a reason in ``constraint_coverage``, and the names
  are repeated at the top level in ``unapplied_constraints`` so a caller that
  reads only the envelope still sees it. Silently returning an unfiltered set
  as though it had passed a filter is the failure mode this block exists to
  prevent: it produces confident, wrong shortlists.

Distances and their bases, published per field rather than implied:

  * ``gas_distance_km``   — geodesic to the nearest ``gas_pipelines`` point
    feature (columns ``lat``/``lng``). Point-to-point, both real coordinates.
    NOT ``discovered_pipelines``: that table has no ``latitude`` column at all,
    and this endpoint shipped querying it — the error surfaced only because a
    caller passed max_gas_km and read constraint_coverage. See layer_status.
  * ``fiber_distance_km`` — geodesic from the anchor to the straight-line CHORD
    between a route's endpoints in ``fiber_routes``. An approximation: a real
    route is a polyline, and a route that bows away from its chord is FARTHER
    than this number says, while one that bows toward the anchor is nearer.
    Stamped in ``fiber_distance_basis`` on every candidate. For an engineered
    read call ``get_fiber_readiness`` at the returned coordinate.
  * ``hazard`` / ``moratorium`` — STATE grain, not site grain. A state-level
    FEMA mean says nothing about one parcel's flood exposure; it is carried as
    region context with its grain declared, never as a site verdict.

GET /api/v1/sites/find?state=OH&min_voltage_kv=230&max_gas_km=8&limit=10
GET /api/v1/sites/find?lat=39.04&lon=-77.48&radius_km=60&min_voltage_kv=230

Tier: exact coordinates, operator and capacity are the paid read, gated with
``caller_is_privileged('IDENTIFIED')`` exactly as the HIFLD substation layer
gates them — anonymous callers get ~11 km coarsened coordinates so the result
is still steerable but cannot be used to bulk-harvest the substation table.

Blueprint registration rides on cron_heartbeat_bp.record_once (main.py wiring
is frozen for parallel worktree tracks — same pattern as cluster_latency).

Pure math, parsing, clustering and the coverage builder are module-level
functions with no DB or Flask dependency, so tests/test_find_sites.py can
exercise them without a database and without importing main.
"""
from __future__ import annotations

import hashlib
import math

from flask import Blueprint, jsonify, request

from routes.error_envelope import error_mitigation

find_sites_bp = Blueprint("find_sites", __name__)

# The endpoint's declared parameter names — the STRICT-SUBSET allow-list any
# error_version:1 suggested_params must validate against.
FIND_SITES_PARAMS = (
    "state", "lat", "lon", "radius_km", "min_voltage_kv",
    "max_gas_km", "max_fiber_km", "exclude_moratorium",
    "cluster_km", "limit",
)

DEFAULT_MIN_KV = 115.0
DEFAULT_CLUSTER_KM = 8.0
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
# Oversample before clustering: dense metros put dozens of substations inside
# one cluster radius, so the top-N by voltage collapses to far fewer than N
# distinct areas. Bounded so a state-wide query stays one cheap indexed read.
OVERSAMPLE = 30
MAX_ANCHOR_ROWS = 800
# Auxiliary layers are pulled once for the whole search region and matched in
# Python — N candidates x 2 layers of per-anchor SQL is the shape that makes
# this endpoint slow enough to hit the 15s edge timeout.
MAX_LAYER_ROWS = 6000

EARTH_R_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """Geodesic distance in km. Returns None if any coordinate is unusable."""
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def point_to_segment_km(plat, plon, alat, alon, blat, blon):
    """Distance from a point to the CHORD a->b, via a local equirectangular
    projection centred on the point.

    An approximation of distance to a fiber route, not a measurement of it:
    the route is a polyline and this is the straight line between its
    endpoints. Callers must surface ``fiber_distance_basis`` alongside it.
    Degenerate segments (a == b) fall back to point distance.
    """
    try:
        plat, plon = float(plat), float(plon)
        alat, alon = float(alat), float(alon)
        blat, blon = float(blat), float(blon)
    except (TypeError, ValueError):
        return None
    # Project to a local planar frame: x east, y north, km units.
    coslat = math.cos(math.radians(plat))
    def _xy(la, lo):
        return ((lo - plon) * 111.32 * coslat, (la - plat) * 110.574)
    ax, ay = _xy(alat, alon)
    bx, by = _xy(blat, blon)
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-12:
        return haversine_km(plat, plon, alat, alon)
    # Projection parameter of the origin (the point) onto the segment, clamped
    # to the segment so an endpoint is returned when the perpendicular foot
    # falls outside it.
    t = -(ax * dx + ay * dy) / seg2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.sqrt(cx * cx + cy * cy)


def clean_kv(v):
    """HIFLD uses -999999 (and 0/negatives) as a NODATA sentinel for voltage.

    Same sanitisation the live HIFLD substation layer applies — real substation
    voltage is ~1-1500 kV, so anything outside that is NODATA, not a reading.
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 2000 else None


_OP_PLACEHOLDERS = {"unknown", "discovered", "n/a", "na", "none", "null",
                    "not available", "not applicable", "tbd", "other", ""}


def clean_operator(v):
    v = (v or "").strip()
    return None if v.lower() in _OP_PLACEHOLDERS else v


def site_ref(name, lat, lon):
    """Stable, opaque reference for a candidate.

    Deliberately NOT called candidate_id: ``rank_sites`` resolves a
    ``candidate_id`` against get_refined_queue's mint, and feeding it one of
    these would look valid and silently resolve to nothing. Pass the returned
    lat/lon to analyze_site instead.
    """
    key = "%s|%.4f|%.4f" % ((name or "").strip().lower(), float(lat), float(lon))
    return "site_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def bbox_for(lat, lon, radius_km):
    """Bounding box around a point. Longitude degrees shrink with latitude;
    the cosine is floored so a near-polar query cannot divide by ~0."""
    d_lat = radius_km / 111.0
    d_lon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon)


def bbox_of_points(points, pad_km=0.0):
    """Tightest bbox containing every (lat, lon), optionally padded.

    Used to scope the auxiliary layer pulls for a state-mode search, where the
    caller gave no radius: the anchors themselves define the region.
    """
    lats = [p[0] for p in points if p[0] is not None]
    lons = [p[1] for p in points if p[1] is not None]
    if not lats or not lons:
        return None
    lat_pad = pad_km / 111.0
    mid = sum(lats) / len(lats)
    lon_pad = pad_km / (111.0 * max(math.cos(math.radians(mid)), 0.01))
    return (min(lats) - lat_pad, max(lats) + lat_pad,
            min(lons) - lon_pad, max(lons) + lon_pad)


def cluster_anchors(anchors, cluster_km, limit):
    """Greedy spatial dedup, highest voltage first.

    ``anchors`` must already be ordered by desirability (voltage desc). An
    anchor is accepted unless it sits within ``cluster_km`` of one already
    accepted, so a metro with 40 substations inside 8 km yields one candidate
    AREA rather than 40 near-identical rows. Returns at most ``limit``.
    """
    accepted = []
    for a in anchors:
        if len(accepted) >= limit:
            break
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            continue
        too_close = False
        for b in accepted:
            d = haversine_km(lat, lon, b["lat"], b["lon"])
            if d is not None and d < cluster_km:
                too_close = True
                break
        if not too_close:
            accepted.append(a)
    return accepted


def build_coverage(requested, evaluated, notes):
    """Per-argument disposition, the ``argument_disposition`` shape.

    ``requested`` — {arg: value} the caller actually sent (absent args are not
    reported; an argument nobody sent has no disposition to report).
    ``evaluated`` — {arg: bool} whether the layer answered well enough to
    filter on.
    ``notes``     — {arg: (reason, instead)} for anything not applied.

    Returns (coverage, unapplied) so the caller can echo the unapplied names at
    the top level too.
    """
    coverage = {}
    unapplied = []
    for arg, value in requested.items():
        ok = bool(evaluated.get(arg))
        entry = {"declared": value, "applied": ok}
        if not ok:
            reason, instead = notes.get(arg, ("layer did not answer", None))
            entry["reason"] = reason
            if instead:
                entry["instead"] = instead
            unapplied.append(arg)
        coverage[arg] = entry
    return coverage, unapplied


def build_layer_status(evaluated, notes, row_counts, queried):
    """Per-LAYER health, reported ALWAYS — not only for constraints the caller
    happened to request.

    ★ Why this exists. constraint_coverage answers "was the filter you asked
    for applied?", so it is silent about a layer nobody filtered on. That left
    a hole: with no max_gas_km, a broken gas layer produced
    `gas_distance_km: null` on every candidate, which reads as "no gas nearby"
    — the confident wrong answer, with nothing anywhere in the response to
    contradict it. It shipped that way, and the defect surfaced only because a
    caller passed max_gas_km and read the coverage block.

    `answered` is the field that disambiguates a null distance:
      True  + rows 0  -> the layer was read and there is genuinely nothing near
      False           -> the layer failed; the null means UNKNOWN, not absent
      None            -> not queried at all (nothing asked for it)
    """
    out = {}
    for key, table in (("gas", "gas_pipelines"),
                       ("fiber", "fiber_routes"),
                       ("moratorium", "permitting_intel")):
        arg = {"gas": "max_gas_km", "fiber": "max_fiber_km",
               "moratorium": "exclude_moratorium"}[key]
        if not queried.get(key):
            out[key] = {"table": table, "answered": None,
                        "note": "not queried — nothing in this request needed it"}
            continue
        ok = bool(evaluated.get(arg))
        entry = {"table": table, "answered": ok,
                 "rows_in_region": row_counts.get(key) if ok else None}
        if not ok:
            reason, instead = notes.get(arg, ("layer did not answer", None))
            entry["reason"] = reason
            if instead:
                entry["instead"] = instead
        out[key] = entry
    return out


def _fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def assemble_candidates(anchors, gas_pts, fiber_segs, moratoria,
                        max_gas_km=None, max_fiber_km=None,
                        exclude_moratorium=False, evaluated=None, full=False):
    """Compute distances, apply the constraints that are ACTUALLY evaluable,
    and shape each surviving anchor into a candidate.

    ★ The invariant this function exists to hold: a constraint filters ONLY
    when ``evaluated[arg]`` is true AND the caller supplied a threshold. A
    layer that errored or returned nothing queryable leaves the candidate set
    UNFILTERED rather than silently dropping every row (which reads as "no
    sites match your constraints") or silently keeping every row as though it
    had passed. Both of those are wrong in the same expensive direction.

    Pure: no DB, no Flask, no clock. The route supplies the layer pulls.
    """
    evaluated = evaluated or {}
    gas_ok = bool(evaluated.get("max_gas_km")) and max_gas_km is not None
    fiber_ok = bool(evaluated.get("max_fiber_km")) and max_fiber_km is not None
    mor_ok = bool(evaluated.get("exclude_moratorium")) and exclude_moratorium

    candidates = []
    for a in anchors:
        alat, alon = a.get("lat"), a.get("lon")
        gas_d = None
        if gas_pts:
            ds = [d for d in (haversine_km(alat, alon, p[0], p[1]) for p in gas_pts)
                  if d is not None]
            gas_d = round(min(ds), 2) if ds else None
        fiber_d = None
        if fiber_segs:
            ds = [d for d in (point_to_segment_km(alat, alon, s[0], s[1], s[2], s[3])
                              for s in fiber_segs) if d is not None]
            fiber_d = round(min(ds), 2) if ds else None

        st = (a.get("state") or "").upper()
        hits = moratoria.get(st, []) if bool(evaluated.get("exclude_moratorium")) else []

        if gas_ok and (gas_d is None or gas_d > max_gas_km):
            continue
        if fiber_ok and (fiber_d is None or fiber_d > max_fiber_km):
            continue
        if mor_ok and hits:
            continue

        out_lat, out_lon = alat, alon
        if not full:
            # Coarsen to ~0.1 degrees (~11 km) for anonymous callers, the same
            # coarsening the HIFLD substation layer applies. Steerable, not
            # harvestable.
            out_lat = round(float(alat), 1) if alat is not None else None
            out_lon = round(float(alon), 1) if alon is not None else None

        candidates.append({
            "site_ref": site_ref(a.get("name"), alat, alon),
            "lat": out_lat,
            "lon": out_lon,
            "coordinate_precision_km": 0.1 if full else 11.0,
            "anchor": {
                "type": "substation",
                "name": a.get("name"),
                "voltage_kv": a.get("voltage_kv"),
                "city": a.get("city"),
                "state": a.get("state"),
                "status": a.get("status"),
                "operator": a.get("operator") if full else None,
                "capacity_mva": a.get("capacity_mva") if full else None,
            },
            "gas_distance_km": gas_d,
            "fiber_distance_km": fiber_d,
            "fiber_distance_basis": (
                "straight-line chord between the route's endpoints in "
                "fiber_routes — NOT the true polyline path; call "
                "get_fiber_readiness at this coordinate for an engineered read"),
            "moratorium": {
                "grain": "state",
                "state_records": len(hits),
                "records": hits[:3] or None,
            } if bool(evaluated.get("exclude_moratorium")) else None,
            "next_calls": [
                "analyze_site lat=%s lon=%s" % (out_lat, out_lon),
                "get_fiber_readiness lat=%s lon=%s" % (out_lat, out_lon),
                "get_permitting_intel state=%s" % (a.get("state") or ""),
            ],
        })
    return candidates


@find_sites_bp.route("/api/v1/sites/find", methods=["GET"])
def find_sites():
    """Constraint -> candidate site areas. See the module docstring."""
    state = (request.args.get("state") or "").strip().upper()[:2]
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lon is None:
        lon = request.args.get("lng", type=float)
    radius_km = _fnum(request.args.get("radius_km"), 80.0) or 80.0
    radius_km = max(5.0, min(400.0, radius_km))
    min_kv = _fnum(request.args.get("min_voltage_kv"), DEFAULT_MIN_KV) or DEFAULT_MIN_KV
    max_gas_km = _fnum(request.args.get("max_gas_km"))
    max_fiber_km = _fnum(request.args.get("max_fiber_km"))
    exclude_moratorium = (request.args.get("exclude_moratorium", "").strip().lower()
                          in ("1", "true", "yes"))
    cluster_km = _fnum(request.args.get("cluster_km"), DEFAULT_CLUSTER_KM) or DEFAULT_CLUSTER_KM
    cluster_km = max(0.0, min(100.0, cluster_km))
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))

    has_point = lat is not None and lon is not None
    if not state and not has_point:
        # Fail closed. A geography-free search would scan 126k substations and
        # return whatever the index happened to order first, which reads like
        # an answer and is not one.
        payload = {
            "success": False,
            "error": "a geography is required: pass state=XX, or lat + lon (+ radius_km)",
        }
        payload.update(error_mitigation(
            "geography_required", "parameter_adjustment",
            "find_sites never scans nationwide — scope the search to one state "
            "or one point, then widen radius_km if the result set is thin.",
            suggested_params={"state": "OH", "min_voltage_kv": 230},
            allowed_params=FIND_SITES_PARAMS))
        return jsonify(payload), 400

    try:
        from routes.tier_gate import caller_is_privileged
        full = caller_is_privileged("IDENTIFIED")
    except Exception:
        full = False

    conn = None
    try:
        from main import get_pg_connection, return_pg_connection
        conn = get_pg_connection()
        cur = conn.cursor()

        # ── anchors ──────────────────────────────────────────────────────────
        conds = ["lat IS NOT NULL", "lng IS NOT NULL", "voltage_kv >= %s"]
        params = [min_kv]
        if has_point:
            b = bbox_for(lat, lon, radius_km)
            conds += ["lat BETWEEN %s AND %s", "lng BETWEEN %s AND %s"]
            params += [b[0], b[1], b[2], b[3]]
        if state:
            conds.append("UPPER(state) = %s")
            params.append(state)
        n_rows = min(MAX_ANCHOR_ROWS, limit * OVERSAMPLE)
        cur.execute(
            "SELECT id, name, city, state, status, voltage_kv, capacity_mva, "
            "lat, lng, owner, operator FROM substations WHERE "
            + " AND ".join(conds)
            # ★ id is the TIEBREAKER, not decoration. Ordering on voltage_kv
            # alone is unstable wherever voltages tie, and they tie constantly:
            # Virginia alone has many 765 kV substations, so Postgres was free
            # to return a different top-N per call and two identical requests
            # produced different candidate sets. For a tool whose output feeds
            # a diligence shortlist, an unreproducible answer is a broken one.
            # id is the SERIAL PRIMARY KEY, so this is total and cheap.
            + " ORDER BY voltage_kv DESC NULLS LAST, id ASC LIMIT %s",
            params + [n_rows])
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]

        anchors = []
        for r in raw:
            kv = clean_kv(r.get("voltage_kv"))
            if kv is None or kv < min_kv:
                # clean_kv nulls the NODATA sentinel; a row that only passed the
                # SQL filter because of it is not a >= min_kv substation.
                continue
            anchors.append({
                "name": r.get("name"), "city": r.get("city"),
                "state": r.get("state"), "status": r.get("status"),
                "voltage_kv": kv, "capacity_mva": r.get("capacity_mva"),
                "lat": r.get("lat"), "lon": r.get("lng"),
                "operator": clean_operator(r.get("operator")) or clean_operator(r.get("owner")),
            })
        anchors = cluster_anchors(anchors, cluster_km, limit)

        requested, evaluated, notes = {}, {}, {}
        if max_gas_km is not None:
            requested["max_gas_km"] = max_gas_km
        if max_fiber_km is not None:
            requested["max_fiber_km"] = max_fiber_km
        if exclude_moratorium:
            requested["exclude_moratorium"] = True

        region = bbox_of_points([(a["lat"], a["lon"]) for a in anchors],
                                pad_km=max(max_gas_km or 0, max_fiber_km or 0, 25.0))

        # ── gas: point features, real point-to-point distance ────────────────
        gas_pts = []
        if anchors and region:
            try:
                cur.execute(
                    "SELECT lat, lng FROM gas_pipelines "
                    "WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
                    "LIMIT %s", (region[0], region[1], region[2], region[3], MAX_LAYER_ROWS))
                gas_pts = [(r[0], r[1]) for r in cur.fetchall()]
                evaluated["max_gas_km"] = True
            except Exception as e:
                conn.rollback()
                notes["max_gas_km"] = (
                    "gas layer (gas_pipelines) did not answer: %s" % str(e)[:120],
                    "call get_infrastructure at each returned coordinate for the gas read")
                evaluated["max_gas_km"] = False
        # ── fiber: chord approximation, basis stamped per candidate ──────────
        fiber_segs = []
        if anchors and region:
            try:
                cur.execute(
                    "SELECT start_lat, start_lng, end_lat, end_lng FROM fiber_routes "
                    "WHERE start_lat IS NOT NULL AND start_lng IS NOT NULL "
                    "AND ((start_lat BETWEEN %s AND %s AND start_lng BETWEEN %s AND %s) "
                    "  OR (end_lat BETWEEN %s AND %s AND end_lng BETWEEN %s AND %s)) "
                    "LIMIT %s",
                    (region[0], region[1], region[2], region[3],
                     region[0], region[1], region[2], region[3], MAX_LAYER_ROWS))
                fiber_segs = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
                evaluated["max_fiber_km"] = True
            except Exception as e:
                conn.rollback()
                notes["max_fiber_km"] = (
                    "fiber layer (fiber_routes) did not answer: %s" % str(e)[:120],
                    "call get_fiber_readiness at each returned coordinate")
                evaluated["max_fiber_km"] = False

        # ── moratoria: STATE grain ───────────────────────────────────────────
        moratoria = {}
        if exclude_moratorium:
            try:
                cur.execute(
                    "SELECT UPPER(state), jurisdiction, title, source_url "
                    "FROM permitting_intel WHERE row_status = 'published' "
                    "AND class = 'moratorium' AND state IS NOT NULL")
                for st, juris, title, url in cur.fetchall():
                    moratoria.setdefault(st, []).append(
                        {"jurisdiction": juris, "title": title, "source_url": url})
                evaluated["exclude_moratorium"] = True
            except Exception as e:
                conn.rollback()
                notes["exclude_moratorium"] = (
                    "permitting layer did not answer: %s" % str(e)[:120],
                    "call get_permitting_intel state=XX before committing to a jurisdiction")
                evaluated["exclude_moratorium"] = False

        return_pg_connection(conn)
        conn = None

        # ── assemble + filter ────────────────────────────────────────────────
        candidates = assemble_candidates(
            anchors, gas_pts, fiber_segs, moratoria,
            max_gas_km=max_gas_km, max_fiber_km=max_fiber_km,
            exclude_moratorium=exclude_moratorium,
            evaluated=evaluated, full=full)

        coverage, unapplied = build_coverage(requested, evaluated, notes)

        payload = {
            "success": True,
            "_entity": "site_candidates",
            "what_this_is": (
                "POWER-ANCHORED SEARCH AREAS, each centred on a real HIFLD "
                "substation at or above the requested voltage. These are NOT "
                "parcels, listings, or land known to be for sale — DC Hub holds "
                "no land-ownership or availability data. Treat each as 'start "
                "looking here', then score the coordinate with analyze_site."),
            "count": len(candidates),
            "anchors_considered": len(anchors),
            "filter": {
                "state": state or None,
                "lat": lat, "lon": lon,
                "radius_km": radius_km if has_point else None,
                "min_voltage_kv": min_kv,
                "max_gas_km": max_gas_km,
                "max_fiber_km": max_fiber_km,
                "exclude_moratorium": exclude_moratorium,
                "cluster_km": cluster_km,
                "limit": limit,
            },
            "candidates": candidates,
            "constraint_coverage": coverage,
            "constraint_coverage_shape": "argument_disposition",
            "unapplied_constraints": unapplied,
            "layer_status": build_layer_status(
                evaluated, notes,
                {"gas": len(gas_pts), "fiber": len(fiber_segs),
                 "moratorium": sum(len(v) for v in moratoria.values())},
                {"gas": bool(anchors and region), "fiber": bool(anchors and region),
                 "moratorium": bool(exclude_moratorium)}),
            "basis": {
                "anchors": {
                    "table": "substations",
                    "population": "HIFLD-derived electric substations with a "
                                  "usable voltage_kv and coordinates",
                    "scope": "United States",
                },
                "gas": {"table": "gas_pipelines",
                        "population": "natural-gas pipeline point features (HIFLD/EIA), lat/lng point geometry"},
                "fiber": {"table": "fiber_routes",
                          "population": "route records with endpoint coordinates; "
                                        "distance is a chord approximation"},
                "moratoria": {"table": "permitting_intel",
                              "population": "published moratorium records",
                              "grain": "state"},
            },
            "citation": {
                "source": "DC Hub", "url": "https://dchub.cloud",
                "license": "CC-BY-4.0", "cite_as": "DC Hub, dchub.cloud",
            },
        }
        if unapplied:
            payload["unapplied_constraints_warning"] = (
                "%d requested constraint(s) could NOT be evaluated and the "
                "candidate set was NOT filtered by them: %s. Read "
                "constraint_coverage before treating this as a screened list."
                % (len(unapplied), ", ".join(unapplied)))
        if not full:
            payload["_gated"] = True
            payload["_pricing_url"] = "https://dchub.cloud/pricing"
            payload["_upgrade_cta"] = (
                "Free preview: candidate coordinates are coarsened to ~11 km and "
                "operator/capacity are withheld. Exact coordinates and anchor "
                "detail require a free key or sign-in — dchub.cloud/pricing")
        return jsonify(payload)

    except Exception as e:
        if conn is not None:
            try:
                from main import return_pg_connection
                conn.rollback()
                return_pg_connection(conn)
            except Exception:
                pass
        payload = {"success": False, "error": str(e)[:200], "count": 0, "candidates": []}
        payload.update(error_mitigation(
            "find_sites_failed", "transient_backoff",
            "The candidate search did not complete. Retry once; if it repeats, "
            "narrow the geography (a smaller radius_km or a single state).",
            allowed_params=FIND_SITES_PARAMS))
        return jsonify(payload), 500
