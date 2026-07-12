"""cluster_latency.py — physics-bounded latency clustering across candidate
sites (Gemini partnership spec, 2026-07-11).

Gemini's framing (their model prototyped the UI: latency slider × provenance
tier → viable paths): given N candidate sites, which subsets can form a
synchronous / low-latency cluster? Deterministic physics does the pruning
BEFORE anyone pays for detailed routing:

  * geodesic (haversine) distance per site pair
  * one-way physics floor = km × 4.9 µs/km — speed of light in SMF-28
    fiber (group index n≈1.468)
  * round-trip floor = ×2
  * estimated real latency = floor × route_factor 1.4 — typical terrestrial
    fiber path inefficiency (an INFERENCE, stamped as such; the floor is
    physics, the estimate is not a measured or engineered path)

Enrichment from OUR data (all fail-soft — the math must return even with no
DB / missing fiber tables; enrichment is simply omitted):

  * per-site ``dark_screen`` — the EXACT read connectivity_score ships
    (score_connectivity → dark_screen_from_carriers); reused as a callable,
    never re-implemented SQL.
  * per-pair ``confidence_v`` — best supporting evidence:
      "published"  BOTH endpoints show dark-capable carrier presence
                   (dark_screen level strong/moderate) AND a published-source
                   fiber route (routes/fiber_provenance.py's source→v dict)
                   touches the pair's corridor bbox
      "tracked"    both endpoints have ANY carrier presence
      "inferred"   everything else (including enrichment-unavailable)

GET/POST /api/v1/fiber/cluster-latency?sites=lat,lon;lat,lon:label&max_latency_us=1000
OPEN access (derived math, adoption-first) — exempted in
free_tier_gate.is_gated next to /api/v1/fiber/route-plan.

Blueprint registration rides on cron_heartbeat_bp.record_once (main.py
wiring is frozen for parallel worktree tracks — same pattern as
analyst_note / metric_truth_check / dark_availability_zones).

Pure math + parsing + clustering are module-level functions with no DB /
Flask dependency: tests/test_cluster_latency.py (pre-merge tests have no
DB/JWT_SECRET and never import main — reference_dchub_green_main_0709).
"""
from __future__ import annotations

import math
import os
import time

from flask import Blueprint, jsonify, request

from routes.error_envelope import error_mitigation, merge_error_mitigation

cluster_latency_bp = Blueprint("cluster_latency", __name__)

# The endpoint's declared parameter names — the STRICT-SUBSET allow-list any
# error_version:1 suggested_params must validate against (an out-of-contract
# key would break an agent's merge-and-retry loop).
CLUSTER_LATENCY_PARAMS = ("sites", "max_latency_us", "min_confidence")

# A canonical valid ``sites`` value handed back as the deterministic
# corrector when parsing fails (Ashburn / Pittsburgh / Columbus).
_SITES_EXAMPLE = ("39.04,-77.48:ashburn;40.42,-79.99:pittsburgh;"
                  "39.96,-83.0:columbus")

# ── constants (cited verbatim in the provenance method string) ──────────
FIBER_US_PER_KM = 4.9      # one-way µs/km — light in SMF-28 fiber, n≈1.468
FIBER_GROUP_INDEX = 1.468  # SMF-28 group index behind the 4.9 figure
ROUTE_FACTOR = 1.4         # typical terrestrial fiber path inefficiency
DEFAULT_BUDGET_US = 1000.0  # round-trip budget default
MIN_SITES, MAX_SITES = 2, 8
BUDGET_MIN_US, BUDGET_MAX_US = 1.0, 10_000_000.0

CONFIDENCE_RANK = {"inferred": 0, "tracked": 1, "published": 2}

# corridor padding for the published-route bbox check (degrees, ~25-55 km)
PAIR_BBOX_PAD_DEG = 0.25

METHOD_STRING = (
    "deterministic physics screen: haversine great-circle distance per site "
    "pair; one-way floor = km × 4.9 µs/km (speed of light in SMF-28 fiber, "
    "group index n≈1.468); round-trip floor = ×2; est_rtt_us = floor × "
    "route_factor 1.4 (typical terrestrial fiber path inefficiency — an "
    "inference, NOT a measured or engineered path). Per-pair confidence_v = "
    "best supporting evidence: 'published' (both endpoints show dark-capable "
    "carrier presence AND a published-source fiber route touches the "
    "corridor bbox), 'tracked' (both endpoints have any carrier presence), "
    "else 'inferred'. Screening basis: PeeringDB carrier presence "
    "(carrier_facility_presence) + fiber_routes published sources. NOT an "
    "engineered latency quote."
)

_enrich_cache = {}   # (lat3, lon3) -> (ts, enrichment-or-None)
_ENRICH_TTL = 900


# ── pure: parsing ────────────────────────────────────────────────────────
def parse_sites(raw):
    """Parse the ``sites`` param — semicolon-separated "lat,lon" pairs
    (compare_sites' locations format), each optionally labelled via
    "lat,lon:label". Returns (sites, error): sites = [{label, lat, lon}],
    error = human-readable string or None. Pure; never raises."""
    try:
        raw = str(raw or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return None, ('sites is required — semicolon-separated "lat,lon" '
                      'pairs (2-8), optional labels via "lat,lon:label", '
                      'e.g. "39.04,-77.48:ashburn;40.79,-77.86;33.45,-112.07"')
    sites = []
    for i, tok in enumerate(t for t in raw.split(";") if t.strip()):
        tok = tok.strip()
        coords, _, label = tok.partition(":")
        parts = coords.split(",")
        if len(parts) < 2:
            return None, f'site {i + 1} ("{tok[:40]}") is not "lat,lon"'
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except (TypeError, ValueError):
            return None, f'site {i + 1} ("{tok[:40]}") has non-numeric coordinates'
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return None, f"site {i + 1} is out of range (lat ±90, lon ±180)"
        sites.append({"label": (label.strip()[:64] or f"site_{i + 1}"),
                      "lat": lat, "lon": lon})
    if len(sites) < MIN_SITES:
        return None, f"at least {MIN_SITES} sites required (got {len(sites)})"
    if len(sites) > MAX_SITES:
        return None, f"at most {MAX_SITES} sites supported (got {len(sites)})"
    # duplicate labels would make pairs/clusters ambiguous — de-dupe.
    seen = {}
    for s in sites:
        n = seen.get(s["label"], 0)
        seen[s["label"]] = n + 1
        if n:
            s["label"] = f"{s['label']}_{n + 1}"
    return sites, None


def parse_budget(raw):
    """max_latency_us → clamped float budget. Default on any failure."""
    try:
        b = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_US
    return min(max(b, BUDGET_MIN_US), BUDGET_MAX_US)


def parse_min_confidence(raw):
    """min_confidence → one of the ladder values; default "inferred"
    (include all)."""
    try:
        v = str(raw or "").strip().lower()
    except Exception:
        v = ""
    return v if v in CONFIDENCE_RANK else "inferred"


# ── pure: the math ───────────────────────────────────────────────────────
def haversine_km(la1, lo1, la2, lo2):
    """Great-circle distance in km (R=6371 — same as connectivity_score)."""
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def floor_rtt_us(distance_km):
    """Round-trip physics floor: km × 4.9 µs/km × 2. Nothing beats this."""
    return distance_km * FIBER_US_PER_KM * 2.0


def est_rtt_us(distance_km):
    """Estimated real round-trip: floor × route_factor 1.4 (inference)."""
    return floor_rtt_us(distance_km) * ROUTE_FACTOR


def confidence_for(enrich_a, enrich_b, published_route):
    """The confidence ladder for one pair. enrich_* are the per-site
    enrichment dicts ({carrier_count, dark_level, ...}) or None when
    enrichment is unavailable; published_route is the corridor-bbox
    published-source check (bool, or None when unknown). Pure; floors never
    over-claim — anything unknown degrades to "inferred"."""
    try:
        if not isinstance(enrich_a, dict) or not isinstance(enrich_b, dict):
            return "inferred"
        both_present = ((enrich_a.get("carrier_count") or 0) >= 1
                        and (enrich_b.get("carrier_count") or 0) >= 1)
        if not both_present:
            return "inferred"
        both_dark = (enrich_a.get("dark_level") in ("strong", "moderate")
                     and enrich_b.get("dark_level") in ("strong", "moderate"))
        if both_dark and published_route is True:
            return "published"
        return "tracked"
    except Exception:
        return "inferred"


def find_clusters(n, viable_pairs):
    """Maximal cliques (Bron–Kerbosch, no pivot — n ≤ 8) over the viable-pair
    graph. viable_pairs: iterable of (i, j) index tuples. Returns a list of
    sorted index-lists, size ≥ 2, largest first. Pure."""
    adj = {i: set() for i in range(n)}
    for i, j in viable_pairs:
        adj[i].add(j)
        adj[j].add(i)
    cliques = []

    def bk(r, p, x):
        if not p and not x:
            if len(r) >= 2:
                cliques.append(sorted(r))
            return
        for v in list(p):
            bk(r | {v}, p & adj[v], x & adj[v])
            p.discard(v)
            x.add(v)

    bk(set(), set(range(n)), set())
    cliques.sort(key=lambda c: (-len(c), c))
    return cliques


def build_cluster_response(sites, budget_us=DEFAULT_BUDGET_US,
                           min_confidence="inferred",
                           enrichment=None, published_pair_check=None):
    """The pure core — everything except HTTP and the DB reads.

    sites                 — [{label, lat, lon}] (parse_sites output)
    budget_us             — round-trip budget in µs
    min_confidence        — ladder floor a pair must meet to stay viable
    enrichment            — list aligned to sites: per-site dicts
                            {carrier_count, dark_level, dark_screen} or None;
                            pass None (whole arg) when enrichment is
                            unavailable → confidence_v degrades to "inferred"
                            and dark_screen fields are omitted
    published_pair_check  — callable (i, j) -> bool|None: does a
                            published-source fiber route touch the pair's
                            corridor bbox; None (arg or return) = unknown

    Returns the full response dict (no provenance block — the HTTP layer
    stamps it). Pure given its inputs; never raises on well-formed input."""
    n = len(sites)
    min_rank = CONFIDENCE_RANK.get(min_confidence, 0)
    pairs, viable_idx = [], []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = sites[i], sites[j]
            dist = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            floor = floor_rtt_us(dist)
            est = est_rtt_us(dist)
            ea = enrichment[i] if enrichment else None
            eb = enrichment[j] if enrichment else None
            pub = published_pair_check(i, j) if published_pair_check else None
            conf = confidence_for(ea, eb, pub)
            impossible = floor > budget_us
            viable = (est <= budget_us
                      and CONFIDENCE_RANK[conf] >= min_rank)
            pair = {
                "from": a["label"], "to": b["label"],
                "distance_km": round(dist, 1),
                "floor_rtt_us": round(floor, 1),
                "est_rtt_us": round(est, 1),
                "viable": viable,
                "physics_impossible": impossible,
                "confidence_v": conf,
            }
            if isinstance(ea, dict) or isinstance(eb, dict):
                pair["endpoint_dark_screen"] = {
                    "a": (ea or {}).get("dark_level"),
                    "b": (eb or {}).get("dark_level"),
                }
            pairs.append(pair)
            if viable:
                viable_idx.append((i, j))

    est_by_pair = {(i, j): est_rtt_us(
        haversine_km(sites[i]["lat"], sites[i]["lon"],
                     sites[j]["lat"], sites[j]["lon"]))
        for i in range(n) for j in range(i + 1, n)}
    clusters = [{
        "sites": [sites[k]["label"] for k in c],
        "size": len(c),
        "max_est_rtt_us": round(max(est_by_pair[(a, b)]
                                    for ai, a in enumerate(c)
                                    for b in c[ai + 1:]), 1),
    } for c in find_clusters(n, viable_idx)]

    out_sites = []
    for idx, s in enumerate(sites):
        row = {"label": s["label"], "lat": s["lat"], "lon": s["lon"]}
        e = enrichment[idx] if enrichment else None
        if isinstance(e, dict) and e.get("dark_screen"):
            row["dark_screen"] = e["dark_screen"]
        out_sites.append(row)

    viable_count = len(viable_idx)
    return {
        "sites": out_sites,
        "pairs": pairs,
        "clusters": clusters,
        "viable_count": viable_count,
        "pruned_count": len(pairs) - viable_count,
        "max_latency_us": budget_us,
        "min_confidence": min_confidence,
        "assumptions": {
            "fiber_us_per_km_one_way": FIBER_US_PER_KM,
            "fiber_group_index": FIBER_GROUP_INDEX,
            "route_factor": ROUTE_FACTOR,
            "note": ("floor_rtt_us is a physics floor (nothing beats it); "
                     "est_rtt_us is an inference — route_factor 1.4 typical "
                     "terrestrial path inefficiency, not a measured path"),
        },
    }


# ── enrichment (DB, fail-soft — the math above never depends on this) ───
def _site_enrichment(sites):
    """Per-site enrichment via the EXACT connectivity_score logic
    (score_connectivity → carrier_count + dark_screen; reused as a callable,
    no duplicated SQL). Returns a list aligned to sites (None entries where
    the read failed), or None when the scorer is unusable at all."""
    try:
        from routes.connectivity_score import score_connectivity
    except Exception:
        return None
    if not os.environ.get("DATABASE_URL"):
        return None
    out, any_ok, now = [], False, time.time()
    for s in sites:
        key = (round(s["lat"], 3), round(s["lon"], 3))
        hit = _enrich_cache.get(key)
        if hit and (now - hit[0]) < _ENRICH_TTL:
            out.append(hit[1])
            any_ok = any_ok or hit[1] is not None
            continue
        e = None
        try:
            r = score_connectivity(s["lat"], s["lon"])
            if isinstance(r, dict) and not r.get("error"):
                ds = r.get("dark_screen") if isinstance(
                    r.get("dark_screen"), dict) else None
                e = {"carrier_count": int(r.get("carrier_count") or 0),
                     "dark_level": (ds or {}).get("level"),
                     "dark_screen": ds}
        except Exception:
            e = None
        _enrich_cache[key] = (now, e)
        if len(_enrich_cache) > 2000:
            _enrich_cache.clear()
        out.append(e)
        any_ok = any_ok or e is not None
    return out if any_ok else None


def _published_route_points(sites):
    """One bounded fiber_routes read: endpoints of PUBLISHED-source routes
    (routes/fiber_provenance.py's source→v dict is the single source of what
    counts as published) inside the padded global bbox of all sites.
    Returns a list of (lat, lng) points, or None on any failure (missing
    table / no DB) — the fail-soft contract."""
    try:
        from routes.fiber_provenance import PUBLISHED_SOURCE_PREFIXES
    except Exception:
        return None
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    lats = [s["lat"] for s in sites]
    lons = [s["lon"] for s in sites]
    pad = PAIR_BBOX_PAD_DEG
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '6000'")
            like = " OR ".join(["source LIKE %s"] * len(PUBLISHED_SOURCE_PREFIXES))
            box = (min(lats) - pad, max(lats) + pad,
                   min(lons) - pad, max(lons) + pad)
            cur.execute(
                f"""SELECT start_lat, start_lng, end_lat, end_lng
                      FROM fiber_routes
                     WHERE ({like})
                       AND ((start_lat BETWEEN %s AND %s AND start_lng BETWEEN %s AND %s)
                         OR (end_lat   BETWEEN %s AND %s AND end_lng   BETWEEN %s AND %s))
                     LIMIT 5000""",
                [p + "%" for p in PUBLISHED_SOURCE_PREFIXES] + list(box) + list(box))
            pts = []
            for sla, slo, ela, elo in cur.fetchall():
                if sla is not None and slo is not None:
                    pts.append((float(sla), float(slo)))
                if ela is not None and elo is not None:
                    pts.append((float(ela), float(elo)))
            return pts
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def published_check_from_points(sites, points, pad=PAIR_BBOX_PAD_DEG):
    """(i, j) -> bool|None: any published-route endpoint inside the pair's
    padded bounding box (the cheap corridor check). Pure given points;
    points=None ⇒ always None (unknown)."""
    if points is None:
        return lambda i, j: None

    def check(i, j):
        a, b = sites[i], sites[j]
        lat_lo = min(a["lat"], b["lat"]) - pad
        lat_hi = max(a["lat"], b["lat"]) + pad
        lon_lo = min(a["lon"], b["lon"]) - pad
        lon_hi = max(a["lon"], b["lon"]) + pad
        return any(lat_lo <= p[0] <= lat_hi and lon_lo <= p[1] <= lon_hi
                   for p in points)
    return check


# ── pure: the deterministic state-machine corrector ─────────────────────
def physics_impossible_mitigation(res):
    """When EVERY candidate pair is physics_impossible for the requested
    ``max_latency_us``, return a parameter_adjustment ``_error_mitigation``
    block whose suggested_params carries the minimum viable ``max_latency_us``
    computed from the smallest floor_rtt_us. This is the deterministic
    corrector: the agent merges the one param and re-runs to get at least one
    viable cluster. Returns None when at least one pair is already within
    physics reach (nothing to correct). Pure; never raises."""
    try:
        pairs = res.get("pairs") or []
        if not pairs or not all(p.get("physics_impossible") for p in pairs):
            return None
        floors = [p.get("floor_rtt_us") for p in pairs
                  if isinstance(p.get("floor_rtt_us"), (int, float))]
        if not floors:
            return None
        # Minimum viable budget = smallest est_rtt (= smallest floor ×
        # route_factor): at this budget the closest pair clears BOTH the
        # physics floor and the est_rtt viability test. Round up so the
        # suggested value is genuinely ≥ the requirement.
        min_viable = int(math.ceil(min(floors) * ROUTE_FACTOR))
        env = error_mitigation(
            "max_latency_us_below_physics_floor",
            "parameter_adjustment",
            ("Every candidate pair's round-trip physics floor exceeds "
             f"max_latency_us={res.get('max_latency_us')}; the closest pair "
             f"needs at least {min_viable} µs. Re-run with the suggested "
             "max_latency_us to surface at least one viable low-latency "
             "cluster."),
            suggested_params={"max_latency_us": min_viable},
            allowed_params=CLUSTER_LATENCY_PARAMS,
        )
        return env["_error_mitigation"]
    except Exception:
        return None


# ── HTTP surface ─────────────────────────────────────────────────────────
def _resolve_candidate_sites(src):
    """r-candidate-cluster (2026-07-12, Grok's high-priority review item):
    let tool #73 consume the candidate contract instead of coordinate-only.
    Accepts candidate_ids (array or comma/semicolon string) AND cand_… tokens
    embedded in the sites string. Each resolves to a frozen "lat,lon:label"
    token (label = project_name or the short id) — coordinates come from the
    mint, never re-specified (same no-transposition guarantee as site-score /
    rank_sites). Expired/unknown/read-failed ids are DROPPED AND DECLARED,
    never silently resolved (the fail-closed contract). Returns
    (normalized_sites_string_or_None, contract) where contract =
    {expired:[], unknown:[], read_failed:[], resolved:[], snapshot_id}.
    ★ audit-fix 2026-07-12: a transient store fault no longer makes the ids
    VANISH (old bare-except returned an empty contract, caller fell back to raw
    sites, candidate_ids gone undeclared). Now every id that couldn't resolve
    is declared read_failed so the agent can retry. Pure of Flask; reads the
    replica."""
    contract = {"expired": [], "unknown": [], "read_failed": [],
                "resolved": [], "snapshot_id": None}
    ids = []
    raw_ids = src.get("candidate_ids")
    if isinstance(raw_ids, (list, tuple)):
        ids.extend(str(x).strip() for x in raw_ids if str(x).strip())
    elif raw_ids:
        ids.extend(t.strip() for t in str(raw_ids).replace(";", ",").split(",")
                   if t.strip())
    # cand_ tokens embedded in the sites string (Grok's mixed-input option B)
    raw_sites = str(src.get("sites") or "")
    passthrough = []
    for tok in (t.strip() for t in raw_sites.split(";") if t.strip()):
        head = tok.partition(":")[0].strip()
        if head.startswith("cand_") and "," not in head:
            ids.append(head)
        else:
            passthrough.append(tok)
    ids = list(dict.fromkeys(ids))  # de-dupe, preserve order
    if not ids:
        return None, contract  # no candidates in play — use raw sites as-is
    import psycopg2
    from routes.candidates import load_candidate, CandidateReadUnavailable
    db = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL"))
    conn = None
    try:
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()
    except Exception:
        # connection down → declare ALL ids read_failed (never silently drop);
        # keep any raw-coordinate passthrough so a mixed request still runs.
        contract["read_failed"] = list(ids)
        return (";".join(passthrough) if passthrough else ""), contract
    for cid in ids:
        try:
            cand, expired = load_candidate(cur, cid)
        except CandidateReadUnavailable:
            contract["read_failed"].append(cid)
            continue
        if cand is None:
            contract["unknown"].append(cid)
        elif expired:
            contract["expired"].append(cid)
        elif cand.get("lat") is None or cand.get("lng") is None:
            contract["unknown"].append(cid)  # ungeocoded → unusable here
        else:
            if not contract["snapshot_id"]:
                contract["snapshot_id"] = cand.get("snapshot_id")
            label = (cand.get("project_name") or cid)
            label = str(label).replace(":", " ").replace(";", " ")[:64]
            passthrough.append(f"{cand['lat']},{cand['lng']}:{label}")
            contract["resolved"].append(cid)
    try:
        conn.close()
    except Exception:
        pass
    return ";".join(passthrough), contract


@cluster_latency_bp.route("/api/v1/fiber/cluster-latency",
                          methods=["GET", "POST"])
def cluster_latency():
    src = request.get_json(silent=True) or request.values
    # candidate contract (Grok review): resolve candidate_ids / cand_ tokens to
    # frozen coordinates before parsing. Fail-soft — falls back to raw sites.
    _sites_str, _cand_contract = _resolve_candidate_sites(src)
    _sites_input = _sites_str if _sites_str is not None else src.get("sites")
    sites, err = parse_sites(_sites_input)
    if err:
        _e = {"error": err,
              "example": "?sites=" + _SITES_EXAMPLE + "&max_latency_us=8000"}
        # if candidates were in play, tell the agent WHY the set is short
        if any(_cand_contract[k] for k in ("expired", "unknown", "read_failed", "resolved")):
            _e["_entity"] = "error"
            _e["candidate_contract"] = _cand_contract
        # error_version:1 — a bad ``sites`` value is a parameter_adjustment:
        # wrap the (candidate-aware) error dict so the agent re-runs immediately
        # with a valid ``sites`` string. merge_error_mitigation preserves the
        # candidate_contract / _entity keys set above.
        body = merge_error_mitigation(
            _e,
            "invalid_sites_param",
            "parameter_adjustment",
            err + " — re-run with the suggested sites value (semicolon-"
            "separated lat,lon pairs, 2-8, optional :label).",
            suggested_params={"sites": _SITES_EXAMPLE},
            allowed_params=CLUSTER_LATENCY_PARAMS,
        )
        return jsonify(body), 400
    budget = parse_budget(src.get("max_latency_us"))
    min_conf = parse_min_confidence(src.get("min_confidence"))

    enrichment = _site_enrichment(sites)
    points = _published_route_points(sites)
    res = build_cluster_response(
        sites, budget_us=budget, min_confidence=min_conf,
        enrichment=enrichment,
        published_pair_check=published_check_from_points(sites, points))

    # r-entity (2026-07-12, Grok review): the envelope discriminator every
    # other DC Hub tool carries — agents branch on _entity before parsing.
    res = {"_entity": "latency_clusters", **res}
    # candidate contract: declare resolved/expired/unknown/read_failed so an
    # agent that passed candidate_ids sees exactly what happened to each
    # (fail-closed). I6 (audit-fix 2026-07-12): when candidates were resolved,
    # carry the identity echo — snapshot_id + SEPARATE search/analysis versions
    # so an auditor can tell data drift from implementation drift.
    if any(_cand_contract[k] for k in ("expired", "unknown", "read_failed", "resolved")):
        res["candidate_contract"] = _cand_contract
        if _cand_contract.get("resolved"):
            from datetime import datetime as _dt, timezone as _tz
            res["echo"] = {
                "snapshot_id": _cand_contract.get("snapshot_id"),
                "search_version": "refined-queue/2026-07-11",
                "analysis_version": "cluster-latency/2026-07-12",
                "retrieved_at": _dt.now(_tz.utc).isoformat(),
                "citation": {"source": "DC Hub (dchub.cloud)", "license": "CC-BY-4.0"},
            }

    # provenance-v1: floors are physics but est_rtt/route_factor and the
    # screening enrichment are DC Hub inferences → default_v="inferred";
    # per-pair confidence_v carries anything stronger. Fail-soft.
    try:
        from routes.provenance import attach_provenance
        attach_provenance(
            res,
            source=("DC Hub latency cluster screen — geodesic physics floors "
                    "+ PeeringDB carrier presence (carrier_facility_presence) "
                    "+ fiber_routes published-source corridor check"),
            method=METHOD_STRING,
            default_v="inferred",
        )
    except Exception:
        pass

    # error_version:1 advisory — a SUCCESS response (HTTP 200, not isError)
    # that still carries a corrector when the requested budget made every
    # pair physics-impossible: the agent can widen max_latency_us and re-run.
    try:
        advisory = physics_impossible_mitigation(res)
        if advisory:
            res["_error_mitigation"] = advisory
    except Exception:
        pass
    return jsonify(res), 200
