"""dark_availability_zones.py — INFERRED dark-availability screening zones
(Gemini dark-fiber phase §4.3, 2026-07-11).

Gemini's framing: "understanding where dark capability intersects with
physical facility presence gives the model an instant screening tool to
eliminate unviable regions BEFORE executing complex routing."

WHAT THIS IS (and is not): every number this module produces is an
INFERENCE — dark-capable carriers (``fiber_providers.dark_fiber = TRUE``)
crossed against PeeringDB facility presence
(``carrier_facility_presence``, 608K rows). A carrier having dark-fiber
capability somewhere in its portfolio AND presence at facilities in a
metro is a SCREENING signal, never a confirmed strand. Every record and
sub-block is stamped ``v: "inferred"`` with the method named
(:data:`METHOD_STRING`). Nothing here may ever be presented as confirmed
availability.

THREE PARTS:
  1. DERIVATION JOB — ``POST /api/v1/admin/dark-zones/rebuild``
     (admin-gated, heartbeat-dispatched daily; kill: DARK_ZONES_DISABLE).
     Groups presence rows by city/state/country, canonicalizes PeeringDB
     carrier names through the alias map below, merges the blank-state/
     blank-country duplicate groups PeeringDB ships (coord-checked so
     'Paris, TX' never merges into 'Paris, FR'), and upserts one row per
     zone into the EXISTING (previously empty) ``fiber_coverage_zones``
     table: zone_type='dark_availability', provider_count = dark-capable
     carrier count, lit_building_count = presence facility count,
     carrier_list = canonical names, notes = the full inferred JSON
     payload. Idempotent (ON CONFLICT zone_id), self-throttled (skips if
     rebuilt <20h ago unless ?force=1), bounded (single aggregate query +
     capped upsert batch), stale rows pruned after 7d.
     DDL note: the table is ensured with a DIRECT psycopg2 connection and
     explicit commit + surfaced errors — deliberately NOT db_utils.safe_db,
     whose DDL path can silently skip (known trap).
  2. POINT SCREEN — :func:`dark_screen_from_carriers`, consumed by
     routes/connectivity_score.py (the get_fiber_readiness backend). Pure
     function over the carrier names already fetched around the query
     point (same radius as the existing distance logic — no extra SQL, no
     dependency on the zones table, so it can never fail on an empty one).
  3. METRO SUMMARY — :func:`metro_dark_zone_summary`, consumed by
     main.py's /api/v1/fiber/metro[/<market>]. Reads the derived zone rows
     (15-min in-process cache) and matches them to a metro through
     METRO_ZONE_CITIES. Fail-soft: empty/missing table or no match ⇒ None
     ⇒ the caller omits the field. Never raises, never errors a response.

Blueprint registration rides on cron_heartbeat_bp.record_once (main.py
wiring is frozen for parallel worktree tracks — same pattern as
analyst_note / metric_truth_check).
"""
from __future__ import annotations

import datetime
import hmac
import json
import math
import os
import re
import time

from flask import Blueprint, jsonify, request

dark_zones_bp = Blueprint("dark_availability_zones", __name__,
                          url_prefix="/api/v1/admin/dark-zones")

ZONE_TYPE = "dark_availability"

# The honesty rail. Ships verbatim on every serving surface.
METHOD_STRING = ("carrier dark-capability × facility presence inference — "
                 "NOT confirmed strand availability; screening signal only")
BASIS_STRING = "capability x presence inference"
DATA_SOURCES = ("inferred: fiber_providers.dark_fiber=TRUE × "
                "carrier_facility_presence (PeeringDB carrier records)")

MAX_ZONES = 5000          # sanity cap on one build (observed ~800 groups)
REBUILD_MIN_HOURS = 20    # self-throttle: wide cron windows re-fire cheaply
PRUNE_AFTER_DAYS = 7      # zones not refreshed by recent builds age out
_ZONES_TTL = 900          # serving cache, seconds
_ZONES_ERR_TTL = 60       # brief negative cache so a down DB isn't hammered


# ── carrier alias map ─────────────────────────────────────────────────
# fiber_providers.name (dark_fiber=TRUE) → the PeeringDB carrier_name(s)
# actually present in carrier_facility_presence. Verified against the
# live tables 2026-07-11 (replica dry-query). Two match modes:
#   EXACT  — carrier_name equals one of the listed strings
#   PREFIX — carrier_name starts with the listed string (PeeringDB splits
#            GTT by ASN and Consolidated by region)
# Keys are the CANONICAL display names we emit.
DARK_CARRIER_EXACT = {
    "Zayo": ["Zayo", "Zayo Europe"],
    "Cogent Communications": ["Cogent Communications, Inc."],
    "Lumen (Level 3)": ["Level3 Carrier"],
    "NTT Communications": ["NTT Global IP Network"],
    "Arelion (ex-Telia Carrier)": ["Arelion (Twelve99)",
                                   "Telia International Network"],
    "Crown Castle Fiber": ["Crown Castle"],
    "Uniti Fiber": ["Uniti Fiber"],
    "Everstream": ["Everstream", "Everstream Solutions LLC"],
    "Segra": ["Segra"],
    "FirstLight Fiber": ["FirstLight Fiber"],
    "Lightpath": ["Lightpath"],
    "PacketFabric": ["PacketFabric"],
}
DARK_CARRIER_PREFIX = {
    "GTT Communications": ["GTT Communications"],
    "Consolidated Communications": ["Consolidated Communications"],
}
# Dark-capable providers with NO reliable PeeringDB carrier record —
# deliberately unmapped rather than fuzzy-matched to a wrong company:
#   arcadian (Arcadian Infracom), bandwidth_ig ("Bandwidth" in PDB is
#   Bandwidth.com, a voice CPaaS), fiberlight, srp_telecom, summitig
#   ("Summit" in PDB is ambiguous across ≥4 companies), vivacity,
#   windstream ("Windstream Communication Limited" in PDB is a
#   Bangladeshi ISP, not Windstream Wholesale). Their metro-level
#   knowledge is carried by the curated metro_dark_fiber table, which
#   /api/v1/fiber/metro already serves.

# Reverse lookup, exact names first (built once at import).
_EXACT_LOOKUP = {n: canon for canon, names in DARK_CARRIER_EXACT.items()
                 for n in names}
_PREFIX_LOOKUP = [(p, canon) for canon, prefixes in DARK_CARRIER_PREFIX.items()
                  for p in prefixes]


def canonical_dark_carrier(name):
    """PeeringDB carrier_name → canonical dark-capable provider, or None.
    Pure; never raises."""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    hit = _EXACT_LOOKUP.get(name)
    if hit:
        return hit
    for prefix, canon in _PREFIX_LOOKUP:
        if name.startswith(prefix):
            return canon
    return None


def bucket_level(n):
    """Dark-capable-carrier count → screening bucket. Pure."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 3:
        return "strong"
    if n >= 1:
        return "moderate"
    return "none"


# ── PART 2: the point screen (consumed by connectivity_score) ─────────
def dark_screen_from_carriers(carrier_names):
    """The instant screening read for one query point.

    Input: iterable of raw PeeringDB carrier names already found within
    the caller's radius. Output: the ``dark_screen`` sub-object, always
    stamped inferred. Pure — no DB, never raises, never returns None
    (level "none" IS the screening answer that eliminates a region)."""
    try:
        canonical = sorted({c for c in
                            (canonical_dark_carrier(n) for n in (carrier_names or []))
                            if c})
    except Exception:
        canonical = []
    return {
        "dark_capable_carriers": len(canonical),
        "carriers": canonical,
        "basis": BASIS_STRING,
        "level": bucket_level(len(canonical)),
        "v": "inferred",
        "method": METHOD_STRING,
    }


# ── derivation: grouping + merge (pure, unit-tested) ──────────────────
def _haversine_km(la1, lo1, la2, lo2):
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def merge_city_groups(groups, merge_km=100.0):
    """Merge PeeringDB's blank-state/blank-country duplicate city groups.

    ``groups``: list of dicts with keys city_key, state, country,
    display_city, fac_count, lat, lng, carriers (set of canonical names).
    A group missing state or country merges into the richest same-city
    group whose non-blank fields don't conflict AND whose median coords
    are within ``merge_km`` (so 'London, , ' near Ontario never merges
    into 'London, , GB'). Pure; returns a NEW list."""
    keep = []      # groups with both fields present (merge targets)
    orphans = []
    for g in groups:
        (keep if (g.get("state") and g.get("country")) else orphans).append(
            dict(g, carriers=set(g.get("carriers") or ())))
    by_city = {}
    for g in keep:
        by_city.setdefault(g["city_key"], []).append(g)

    unmerged = []
    for o in orphans:
        cands = [k for k in by_city.get(o["city_key"], [])
                 if (not o.get("state") or o["state"] == k["state"])
                 and (not o.get("country") or o["country"] == k["country"])
                 and None not in (o.get("lat"), o.get("lng"),
                                  k.get("lat"), k.get("lng"))
                 and _haversine_km(o["lat"], o["lng"],
                                   k["lat"], k["lng"]) <= merge_km]
        if cands:
            tgt = max(cands, key=lambda k: k.get("fac_count") or 0)
            tgt["fac_count"] = (tgt.get("fac_count") or 0) + (o.get("fac_count") or 0)
            tgt["carriers"] |= o["carriers"]
        else:
            unmerged.append(o)
    return keep + unmerged


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "x"


def zone_id_for(city_key, state, country):
    return f"dark:{_slug(country) if country else 'xx'}:" \
           f"{_slug(state) if state else 'xx'}:{_slug(city_key)}"


def zone_row_from_group(g, built_at_iso):
    """One merged group → the fiber_coverage_zones row (dict). Pure."""
    carriers = sorted(g.get("carriers") or ())
    n = len(carriers)
    level = bucket_level(n)
    name = g.get("display_city") or g.get("city_key") or "?"
    if g.get("state"):
        name = f"{name}, {g['state']}"
    notes = {
        "v": "inferred",
        "level": level,
        "dark_capable_carrier_count": n,
        "presence_facility_count": int(g.get("fac_count") or 0),
        "carriers": carriers,
        "method": METHOD_STRING,
        "built_at": built_at_iso,
    }
    return {
        "zone_id": zone_id_for(g.get("city_key") or "", g.get("state"),
                               g.get("country")),
        "zone_name": name,
        "zone_type": ZONE_TYPE,
        "state": g.get("state") or None,
        "country": g.get("country") or None,
        "center_lat": g.get("lat"),
        "center_lng": g.get("lng"),
        "provider_count": n,
        "lit_building_count": int(g.get("fac_count") or 0),
        "dark_fiber_available": n >= 1,
        "carrier_list": ", ".join(carriers),
        "data_sources": DATA_SOURCES,
        "notes": json.dumps(notes),
    }


# ── DB plumbing (metric_truth_check conventions) ──────────────────────
def _disabled():
    return str(os.environ.get("DARK_ZONES_DISABLE", "")).lower() in (
        "1", "true", "yes")


def _admin_ok():
    """Accept X-Admin-Key=DCHUB_ADMIN_KEY or X-Internal-Key=
    DCHUB_INTERNAL_KEY / DCHUB_SYNC_KEY — the headers cron_heartbeat._hit()
    attaches."""
    pairs = [
        (request.headers.get("X-Admin-Key"), os.environ.get("DCHUB_ADMIN_KEY")),
        (request.headers.get("X-Internal-Key"), os.environ.get("DCHUB_INTERNAL_KEY")),
        (request.headers.get("X-Internal-Key"), os.environ.get("DCHUB_SYNC_KEY")),
    ]
    for got, exp in pairs:
        got = (got or "").strip()
        exp = (exp or "").strip()
        if got and exp and hmac.compare_digest(got, exp):
            return True
    return False


def _conn():
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url, connect_timeout=10)
    except Exception:
        return None


def _ensure_zone_table(cur):
    """Ensure fiber_coverage_zones exists (it does on live; this covers
    fresh environments). DIRECT DDL with surfaced failure — NOT
    db_utils.safe_db, whose DDL path silently skips. Matches the live
    schema exactly."""
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fiber_coverage_zones (
                id SERIAL PRIMARY KEY,
                zone_id TEXT UNIQUE,
                zone_name TEXT NOT NULL,
                zone_type TEXT DEFAULT 'metro',
                state TEXT,
                country TEXT DEFAULT 'US',
                center_lat DOUBLE PRECISION,
                center_lng DOUBLE PRECISION,
                provider_count INTEGER DEFAULT 0,
                lit_building_count INTEGER DEFAULT 0,
                fiber_route_count INTEGER DEFAULT 0,
                avg_download_mbps REAL,
                avg_upload_mbps REAL,
                dark_fiber_available BOOLEAN DEFAULT FALSE,
                carrier_list TEXT,
                data_sources TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        return True
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return False


# ── PART 1: the derivation job ─────────────────────────────────────────
def _fetch_presence_groups(cur):
    """One bounded aggregate over carrier_facility_presence: city groups
    of dark-capable carrier presence. MEDIAN coords (percentile_cont) —
    never AVG (the market-coords centroid trap)."""
    exact = [n for names in DARK_CARRIER_EXACT.values() for n in names]
    prefixes = [p for ps in DARK_CARRIER_PREFIX.values() for p in ps]
    like = " OR ".join(["carrier_name LIKE %s"] * len(prefixes))
    cur.execute(f"""
        SELECT LOWER(TRIM(facility_city)) AS city_key,
               UPPER(TRIM(COALESCE(facility_state, '')))   AS state,
               UPPER(TRIM(COALESCE(facility_country, ''))) AS country,
               MIN(TRIM(facility_city))                    AS display_city,
               COUNT(DISTINCT facility_pdb_id)             AS fac_count,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY facility_lat) AS lat,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY facility_lng) AS lng,
               array_agg(DISTINCT carrier_name)            AS raw_names
          FROM carrier_facility_presence
         WHERE facility_lat IS NOT NULL AND facility_lng IS NOT NULL
           AND TRIM(COALESCE(facility_city, '')) <> ''
           AND (carrier_name = ANY(%s) OR {like})
         GROUP BY 1, 2, 3
    """, [exact] + [p + "%" for p in prefixes])
    groups = []
    for city_key, state, country, disp, fc, lat, lng, raw in cur.fetchall():
        carriers = {c for c in (canonical_dark_carrier(n) for n in (raw or []))
                    if c}
        if not carriers:
            continue
        groups.append({"city_key": city_key, "state": state or "",
                       "country": country or "", "display_city": disp,
                       "fac_count": int(fc or 0), "lat": lat, "lng": lng,
                       "carriers": carriers})
    return groups


def rebuild_zones(force=False, now=None):
    """Derive + upsert the dark-availability zones. Idempotent, bounded,
    self-throttled. Returns a report dict; never raises."""
    if _disabled():
        return {"ok": True, "skipped": "DARK_ZONES_DISABLE"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    conn = _conn()
    if conn is None:
        return {"ok": False, "error": "no database connection"}
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '120000'")
        if not _ensure_zone_table(cur):
            return {"ok": False, "error": "fiber_coverage_zones unavailable "
                                          "(DDL failed — surfaced, not skipped)"}
        conn.commit()

        if not force:
            try:
                cur.execute("""
                    SELECT MAX(updated_at) FROM fiber_coverage_zones
                     WHERE zone_type = %s
                """, (ZONE_TYPE,))
                row = cur.fetchone()
                last = row[0] if row else None
                if last is not None:
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=datetime.timezone.utc)
                    if (now - last) < datetime.timedelta(hours=REBUILD_MIN_HOURS):
                        return {"ok": True, "skipped": "fresh",
                                "last_built": last.isoformat()}
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

        groups = _fetch_presence_groups(cur)
        merged = merge_city_groups(groups)
        built_at = now.isoformat()
        rows = [zone_row_from_group(g, built_at) for g in merged][:MAX_ZONES]

        upsert = """
            INSERT INTO fiber_coverage_zones
                (zone_id, zone_name, zone_type, state, country,
                 center_lat, center_lng, provider_count, lit_building_count,
                 dark_fiber_available, carrier_list, data_sources, notes,
                 updated_at)
            VALUES (%(zone_id) ON CONFLICT DO NOTHINGs, %(zone_name)s, %(zone_type)s, %(state)s,
                    %(country)s, %(center_lat)s, %(center_lng)s,
                    %(provider_count)s, %(lit_building_count)s,
                    %(dark_fiber_available)s, %(carrier_list)s,
                    %(data_sources)s, %(notes)s, NOW())
            ON CONFLICT (zone_id) DO UPDATE SET
                zone_name = EXCLUDED.zone_name,
                state = EXCLUDED.state,
                country = EXCLUDED.country,
                center_lat = EXCLUDED.center_lat,
                center_lng = EXCLUDED.center_lng,
                provider_count = EXCLUDED.provider_count,
                lit_building_count = EXCLUDED.lit_building_count,
                dark_fiber_available = EXCLUDED.dark_fiber_available,
                carrier_list = EXCLUDED.carrier_list,
                data_sources = EXCLUDED.data_sources,
                notes = EXCLUDED.notes,
                updated_at = NOW()
        """
        written = 0
        for i in range(0, len(rows), 200):
            for r in rows[i:i + 200]:
                cur.execute(upsert, r)
                written += 1
            conn.commit()

        # prune zones no recent build produced (renames, data corrections)
        pruned = 0
        try:
            cur.execute("""
                DELETE FROM fiber_coverage_zones
                 WHERE zone_type = %s
                   AND updated_at < NOW() - (%s * INTERVAL '1 day')
            """, (ZONE_TYPE, int(PRUNE_AFTER_DAYS)))
            pruned = cur.rowcount or 0
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        levels = {}
        for r in rows:
            lv = bucket_level(r["provider_count"])
            levels[lv] = levels.get(lv, 0) + 1
        _zones_cache["ts"] = 0.0      # serving cache: force refresh
        return {"ok": True, "zones_written": written, "pruned": pruned,
                "levels": levels, "built_at": built_at,
                "method": METHOD_STRING}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── PART 3: metro serving (fail-soft reads of the derived rows) ───────
# Multi-city metros whose market name is not a facility_city. Everything
# else defaults to the market name itself as the city.
METRO_ZONE_CITIES = {
    "northern virginia": ["ashburn", "sterling", "reston", "herndon",
                          "chantilly", "manassas", "vienna", "mclean",
                          "leesburg"],
    "silicon valley": ["san jose", "santa clara", "sunnyvale", "palo alto",
                       "milpitas", "mountain view", "fremont"],
    "new york metro": ["new york", "newark", "secaucus", "jersey city",
                       "weehawken", "piscataway", "brooklyn",
                       "staten island"],
    "dallas fort worth": ["dallas", "fort worth", "plano", "richardson",
                          "carrollton", "irving", "garland", "allen"],
}


def _norm_market(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def zone_cities_for_market(market):
    """Market name → list of normalized zone city names. Pure."""
    norm = _norm_market(market)
    if not norm:
        return []
    return METRO_ZONE_CITIES.get(norm, [norm])


_zones_cache = {"ts": 0.0, "rows": None}


def _load_zones():
    """All derived dark-availability zones (small: ~800 rows), cached
    15 min in-process. [] on ANY failure (missing table, no DB) — the
    fail-soft contract; callers then omit their fields."""
    now = time.time()
    ttl = _ZONES_TTL if _zones_cache["rows"] else _ZONES_ERR_TTL
    if _zones_cache["rows"] is not None and (now - _zones_cache["ts"]) < ttl:
        return _zones_cache["rows"]
    if _zones_cache["rows"] is None and (now - _zones_cache["ts"]) < _ZONES_ERR_TTL:
        return []
    conn = _conn()
    if conn is None:
        _zones_cache["ts"] = now
        return _zones_cache["rows"] or []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT zone_name, state, country, center_lat, center_lng,
                   provider_count, lit_building_count, carrier_list
              FROM fiber_coverage_zones
             WHERE zone_type = %s AND provider_count > 0
        """, (ZONE_TYPE,))
        rows = []
        for (name, state, country, lat, lng, pc, fc, cl) in cur.fetchall():
            rows.append({
                "zone_name": name,
                "city_key": _norm_market((name or "").split(",")[0]),
                "state": (state or "").upper(),
                "country": (country or "").upper(),
                "center_lat": lat, "center_lng": lng,
                "dark_capable_carrier_count": int(pc or 0),
                "presence_facility_count": int(fc or 0),
                "carriers": [c.strip() for c in (cl or "").split(",")
                             if c.strip()],
            })
        _zones_cache["rows"] = rows
        _zones_cache["ts"] = now
        return rows
    except Exception:
        _zones_cache["ts"] = now
        return _zones_cache["rows"] or []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def match_zones_to_market(zones, market, state=None):
    """Filter derived zones to one metro. Pure (tested with fake zones)."""
    cities = set(zone_cities_for_market(market))
    if not cities:
        return []
    st = (state or "").strip().upper()
    out = []
    for z in zones or []:
        if z.get("city_key") not in cities:
            continue
        zst = (z.get("state") or "").strip().upper()
        if st and zst and zst != st:
            continue
        out.append(z)
    out.sort(key=lambda z: (-int(z.get("dark_capable_carrier_count") or 0),
                            -int(z.get("presence_facility_count") or 0)))
    return out


def summarize_zones(matched, compact=False):
    """Matched zones → the ``dark_availability_zones`` sub-block, or None
    when there is nothing to say (callers omit the field). Pure."""
    if not matched:
        return None
    union = sorted({c for z in matched for c in (z.get("carriers") or [])})
    n = len(union)
    level = bucket_level(n)
    if compact:
        return {"v": "inferred", "level": level,
                "dark_capable_carrier_count": n}
    return {
        "v": "inferred",
        "method": METHOD_STRING,
        "basis": BASIS_STRING,
        "level": level,
        "dark_capable_carrier_count": n,
        "carriers": union,
        "presence_facility_count": sum(
            int(z.get("presence_facility_count") or 0) for z in matched),
        "zones": [{
            "zone_name": z.get("zone_name"),
            "state": z.get("state") or None,
            "dark_capable_carrier_count":
                int(z.get("dark_capable_carrier_count") or 0),
            "carriers": z.get("carriers") or [],
            "presence_facility_count":
                int(z.get("presence_facility_count") or 0),
            "level": bucket_level(z.get("dark_capable_carrier_count")),
            "v": "inferred",
        } for z in matched[:8]],
    }


def metro_dark_zone_summary(market, state=None, compact=False):
    """The one-call helper main.py's fiber/metro endpoint uses. Fail-soft:
    None on empty table / no match / any error ⇒ caller omits the field."""
    try:
        return summarize_zones(
            match_zones_to_market(_load_zones(), market, state=state),
            compact=compact)
    except Exception:
        return None


# ── HTTP surface ───────────────────────────────────────────────────────
@dark_zones_bp.route("/rebuild", methods=["POST"])
def rebuild_endpoint():
    if not _admin_ok():
        return jsonify(error="admin key required"), 403
    if _disabled():
        return jsonify(ok=True, skipped="DARK_ZONES_DISABLE"), 200
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    return jsonify(rebuild_zones(force=force)), 200


# AUTO-REPAIR: duplicate route '/status' also in ai_agent.py:357 — review and remove one
@dark_zones_bp.route("/status", methods=["GET"])
def status_endpoint():
    if not _admin_ok():
        return jsonify(error="admin key required"), 403
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="no database connection"), 200
    try:
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT COUNT(*), MAX(updated_at),
                       COUNT(*) FILTER (WHERE provider_count >= 3),
                       COUNT(*) FILTER (WHERE provider_count BETWEEN 1 AND 2)
                  FROM fiber_coverage_zones WHERE zone_type = %s
            """, (ZONE_TYPE,))
            total, last, strong, moderate = cur.fetchone()
        except Exception:
            return jsonify(ok=True, zones=0,
                           note="fiber_coverage_zones missing or empty"), 200
        return jsonify(ok=True, zones=int(total or 0),
                       levels={"strong": int(strong or 0),
                               "moderate": int(moderate or 0)},
                       last_built=(last.isoformat() if last else None),
                       method=METHOD_STRING), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass
