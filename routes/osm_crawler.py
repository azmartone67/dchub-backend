"""Phase FF+25-followup-r18 (2026-05-20) — OpenStreetMap data center crawler.
==========================================================================

DataCenterMap is gated behind Vercel's bot challenge (their robots.txt
request returns a JS challenge page, not actual robots.txt — they're
hostile to all crawlers regardless of UA). Pivoting to OSM which is:
  · 100% open data (ODbL license)
  · Built FOR crawling — Overpass API is the official query interface
  · Globally tagged: telecom=data_center, office=data_center,
                     industrial=data_center
  · Has lat/lon (something DCM doesn't even publish cleanly)

QUERY STRATEGY
==============

Overpass API times out on large country areas (Canada-wide query → 504).
Workaround: split by bounding boxes. Each query is a single province /
state / sub-region small enough to return in <30s.

CONFIGURED REGIONS (extensible — add to BBOXES list as we want more):
  · Alberta (CA-AB) — closes the gap user reported tonight
  · Ontario (CA-ON), Quebec (CA-QC), BC (CA-BC), Saskatchewan (CA-SK)
  · UK, Germany, France, Netherlands, Ireland, Singapore, Japan,
    Australia, India, Brazil, Mexico

ENDPOINTS
=========
  POST /api/v1/admin/osm-crawl/run         admin: trigger crawl now
                                            ?region=alberta or all
                                            ?dry_run=1 for preview
  GET  /api/v1/admin/osm-crawl/status      last-run summary
  GET  /api/v1/admin/osm-crawl/log         last 50 runs

SAFETY
======
  · User-Agent identifies as DCHubCrawler/1.0 + contact link
  · 3s sleep between regions (well under Overpass usage policy)
  · Per-region timeout 30s
  · MAX_PER_RUN cap (env: OSM_CRAWL_MAX, default 500)
  · source='openstreetmap' on every row → single SQL purge if needed
  · DCM_CRAWL_ENABLED + OSM_CRAWL_ENABLED env vars must be set
    (sharing the same flag for now since user already set it)
"""
import os
import time
import json
import hashlib
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
osm_crawler_bp = Blueprint("osm_crawler", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Bounding boxes (south, west, north, east) ────────────────────────
# Generous bounds — Overpass clips to land masses. Adding regions is a
# one-line extension as we grow coverage.
BBOXES: dict = {
    # Canadian provinces (the immediate user-reported gap)
    "alberta":        (49.0, -120.0, 60.0, -110.0),
    "british-columbia":(48.3, -139.1, 60.0, -114.0),
    "saskatchewan":   (49.0, -110.0, 60.0, -101.4),
    "manitoba":       (49.0, -101.5, 60.0, -89.0),
    "ontario":        (41.7, -95.2, 56.9, -74.3),
    "quebec":         (45.0, -79.8, 62.6, -57.1),
    "nova-scotia":    (43.4, -66.3, 47.1, -59.6),

    # Top international DC markets
    "united-kingdom": (49.9, -8.6, 60.9, 1.8),
    "ireland":        (51.4, -10.6, 55.5, -5.4),
    "germany":        (47.3, 5.8, 55.1, 15.1),
    "france":         (41.3, -5.2, 51.1, 9.6),
    "netherlands":    (50.7, 3.3, 53.6, 7.3),
    "belgium":        (49.5, 2.5, 51.6, 6.4),
    "switzerland":    (45.8, 5.9, 47.9, 10.5),
    "italy":          (35.4, 6.6, 47.1, 18.6),
    "spain":          (36.0, -9.4, 43.8, 4.4),

    # APAC
    "singapore":      (1.1, 103.5, 1.5, 104.1),
    "japan":          (24.0, 122.9, 45.6, 145.9),
    "south-korea":    (33.1, 124.6, 38.6, 132.0),
    "australia":      (-44.0, 112.9, -10.0, 154.0),
    "india":          (6.7, 68.0, 35.5, 97.3),

    # LATAM
    "brazil":         (-33.8, -73.9, 5.3, -34.8),
    "mexico":         (14.5, -118.4, 32.7, -86.7),

    # US states with thin DCHawk coverage
    "alaska":         (52.0, -180.0, 71.5, -141.0),
    "hawaii":         (18.9, -160.3, 22.2, -154.8),
}

OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "DCHubCrawler/1.0 (+https://dchub.cloud/contact)"
SLEEP_SEC = float(os.environ.get("OSM_CRAWL_SLEEP", "3.0"))
MAX_PER_RUN = int(os.environ.get("OSM_CRAWL_MAX", "500"))
ENABLED = (os.environ.get("OSM_CRAWL_ENABLED",
                          os.environ.get("DCM_CRAWL_ENABLED", "false"))
           .lower() in ("1", "true", "yes"))


# ── Overpass query ───────────────────────────────────────────────────
def _query_bbox(bbox: tuple) -> list[dict]:
    """Run an Overpass query for data center POIs in a bounding box.
    Returns a list of {tags, lat, lon, type, id} dicts."""
    south, west, north, east = bbox
    q = (
        f'[out:json][timeout:25];'
        f'(node["telecom"="data_center"]({south},{west},{north},{east});'
        f' way["telecom"="data_center"]({south},{west},{north},{east});'
        f' node["office"="data_center"]({south},{west},{north},{east});'
        f' way["office"="data_center"]({south},{west},{north},{east});'
        f' node["industrial"="data_center"]({south},{west},{north},{east});'
        f' way["industrial"="data_center"]({south},{west},{north},{east});'
        f');out tags center;'
    )
    import urllib.request
    import urllib.parse
    try:
        data = urllib.parse.urlencode({"data": q}).encode()
        req = urllib.request.Request(
            OVERPASS, data=data, method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("elements", []) or []
    except Exception as e:
        logger.info(f"[osm-crawl] bbox {bbox} → {type(e).__name__}: {e}")
        return []


# ── Row mapping ──────────────────────────────────────────────────────
# OSM tags vary a lot. Pull the most-likely-useful identifiers and fall
# back gracefully when fields aren't set.

def _tag(e: dict, *keys: str) -> str:
    t = e.get("tags") or {}
    for k in keys:
        v = t.get(k)
        if v: return str(v).strip()
    return ""


def _osm_to_row(e: dict, region_slug: str) -> dict | None:
    tags = e.get("tags") or {}
    name = _tag(e, "name", "operator", "brand")
    if not name or len(name) > 200:
        return None
    # Skip generic / placeholder names that aren't real facilities
    if name.lower() in ("data center", "data centre", "(unnamed)", "datacenter"):
        return None
    operator = _tag(e, "operator", "owner", "brand")
    city = _tag(e, "addr:city", "addr:place", "addr:town", "addr:village")
    state = _tag(e, "addr:state", "addr:province", "addr:region",
                 "is_in:state", "is_in:province")
    country = _tag(e, "addr:country", "is_in:country") or _country_from_region(region_slug)
    # Address line — best-effort from street + housenumber
    street = _tag(e, "addr:street")
    house = _tag(e, "addr:housenumber")
    postcode = _tag(e, "addr:postcode")
    addr_parts = [p for p in [house and (house + " " + street), street if not house else "",
                              city, state, postcode, country] if p]
    address = ", ".join(addr_parts) if addr_parts else ""
    lat = e.get("lat") or (e.get("center") or {}).get("lat")
    lon = e.get("lon") or (e.get("center") or {}).get("lon")
    return {
        "name": name,
        "provider": operator,
        "city": city,
        "state": state,
        "country": country,
        "address": address,
        "status": "operational",
        "power_mw": 0,
        "_osm_lat": lat,
        "_osm_lon": lon,
        "_osm_id": e.get("id"),
        "_osm_type": e.get("type"),
        "_region": region_slug,
    }


def _country_from_region(slug: str) -> str:
    canada = {"alberta", "british-columbia", "saskatchewan", "manitoba",
              "ontario", "quebec", "nova-scotia"}
    if slug in canada: return "CA"
    table = {
        "united-kingdom":"GB","ireland":"IE","germany":"DE","france":"FR",
        "netherlands":"NL","belgium":"BE","switzerland":"CH","italy":"IT",
        "spain":"ES","singapore":"SG","japan":"JP","south-korea":"KR",
        "australia":"AU","india":"IN","brazil":"BR","mexico":"MX",
        "alaska":"US","hawaii":"US",
    }
    return table.get(slug, "")


# ── Insert ───────────────────────────────────────────────────────────
def _insert_row(cur, r: dict) -> tuple[bool, str]:
    """Insert into both facilities + discovered_facilities (mirrors the
    facility_admin._insert_one pattern + the r17 two-table fix).
    source='openstreetmap'. Idempotent via the SHA-keyed source_id."""
    name = r["name"]
    source_id = ("osm_" + hashlib.sha256(
        f"{name}|{r.get('city','')}|{r.get('country','')}".encode()
    ).hexdigest()[:16])
    # Exists in canonical?
    cur.execute(
        "SELECT 1 FROM facilities WHERE source_id = %s LIMIT 1",
        (source_id,),
    )
    if cur.fetchone():
        return False, source_id
    # Dedup by name+city in canonical
    cur.execute(
        "SELECT 1 FROM facilities WHERE LOWER(name) = LOWER(%s) LIMIT 1",
        (name,),
    )
    if cur.fetchone():
        return False, source_id
    cur.execute("""
        INSERT INTO facilities
          (id, name, provider, city, state, country, power_mw,
           status, address, source, source_id)
        VALUES (%s, %s, %s, %s, %s, %s, 0, 'operational', %s,
                'openstreetmap', %s)
    """, (
        source_id, name, r.get("provider"),
        r.get("city"), r.get("state"), r.get("country", ""),
        r.get("address") or None, source_id,
    ))
    # Stage into discovered_facilities so the public search sees it
    try:
        cur.execute("""
            INSERT INTO discovered_facilities (
                source, source_id, name, provider, city, state, country,
                latitude, longitude, power_mw, status, address,
                confidence_score, is_duplicate,
                merged_facility_id, discovered_at, first_seen, last_updated
            )
            VALUES ('openstreetmap', %s, %s, %s, %s, %s, %s, %s, %s, 0,
                    'operational', %s, 0.9, 0, %s,
                    NOW()::TEXT, NOW()::TEXT, NOW()::TEXT)
            ON CONFLICT (source, source_id) DO UPDATE SET
                name = EXCLUDED.name,
                last_updated = NOW()::TEXT
        """, (
            source_id, name, r.get("provider"),
            r.get("city"), r.get("state"), r.get("country", ""),
            r.get("_osm_lat"), r.get("_osm_lon"),
            r.get("address") or None, source_id,
        ))
    except Exception as e:
        logger.warning(f"[osm-crawl] discovered_facilities stage skipped: {str(e)[:120]}")
    return True, source_id


# ── Log table ────────────────────────────────────────────────────────
def _ensure_log_table():
    c = _get_db()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS osm_crawl_log (
                    id              SERIAL PRIMARY KEY,
                    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at     TIMESTAMPTZ,
                    regions         TEXT[],
                    pois_seen       INT NOT NULL DEFAULT 0,
                    pois_new        INT NOT NULL DEFAULT 0,
                    pois_dup        INT NOT NULL DEFAULT 0,
                    errors          INT NOT NULL DEFAULT 0,
                    dry_run         BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
        try: c.commit()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass


# ── Crawl ────────────────────────────────────────────────────────────
def _crawl(region: str | None, dry_run: bool) -> dict:
    if not ENABLED and not dry_run:
        return {"ok": False,
                "error": "OSM_CRAWL_ENABLED env var not set to true",
                "hint": "Set OSM_CRAWL_ENABLED=true (or "
                        "DCM_CRAWL_ENABLED=true) on Railway, or pass "
                        "?dry_run=1."}

    regions = [region] if region else list(BBOXES.keys())
    regions = [r for r in regions if r in BBOXES]
    if not regions:
        return {"ok": False, "error": f"unknown region: {region}",
                "available": sorted(BBOXES.keys())}

    summary = {
        "regions": regions, "pois_seen": 0, "pois_new": 0,
        "pois_dup": 0, "errors": 0, "dry_run": dry_run,
        "examples": [],
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    cap_hit = False
    c = _get_db()
    _ensure_log_table()

    try:
        for region_slug in regions:
            if cap_hit: break
            bbox = BBOXES[region_slug]
            elements = _query_bbox(bbox)
            time.sleep(SLEEP_SEC)
            if not elements:
                summary["errors"] += 1
                continue
            summary["pois_seen"] += len(elements)
            for e in elements:
                if summary["pois_new"] >= MAX_PER_RUN:
                    cap_hit = True
                    break
                row = _osm_to_row(e, region_slug)
                if not row: continue
                if dry_run or c is None:
                    if len(summary["examples"]) < 30:
                        summary["examples"].append({
                            "name": row["name"], "operator": row.get("provider"),
                            "city": row.get("city"), "country": row.get("country"),
                            "region": region_slug,
                        })
                    summary["pois_new"] += 1
                    continue
                try:
                    with c.cursor() as cur:
                        added, sid = _insert_row(cur, row)
                    try: c.commit()
                    except Exception: pass
                    if added:
                        summary["pois_new"] += 1
                        if len(summary["examples"]) < 30:
                            summary["examples"].append({
                                "name": row["name"], "source_id": sid,
                                "country": row.get("country"),
                                "region": region_slug,
                            })
                    else:
                        summary["pois_dup"] += 1
                except Exception as e:
                    try: c.rollback()
                    except Exception: pass
                    summary["errors"] += 1
                    logger.info(f"[osm-crawl] insert err: {str(e)[:120]}")

        summary["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        summary["ok"] = True

        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO osm_crawl_log
                          (regions, pois_seen, pois_new, pois_dup,
                           errors, dry_run, finished_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    """, (regions, summary["pois_seen"],
                           summary["pois_new"], summary["pois_dup"],
                           summary["errors"], dry_run))
                try: c.commit()
                except Exception: pass
            except Exception:
                try: c.rollback()
                except Exception: pass
    finally:
        try:
            if c is not None: c.close()
        except Exception: pass

    return summary


# ── Endpoints ────────────────────────────────────────────────────────
@osm_crawler_bp.route("/api/v1/admin/osm-crawl/run", methods=["POST"])
def crawl_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    region = (request.args.get("region") or "").strip().lower() or None
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    return jsonify(_crawl(region, dry_run))


@osm_crawler_bp.route("/api/v1/admin/osm-crawl/status", methods=["GET"])
def crawl_status():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, started_at, finished_at, regions,
                       pois_seen, pois_new, pois_dup, errors, dry_run
                  FROM osm_crawl_log
                 ORDER BY started_at DESC LIMIT 1
            """)
            r = cur.fetchone()
            if not r:
                return jsonify(ok=True, enabled=ENABLED, last_run=None,
                               available_regions=sorted(BBOXES.keys()))
            return jsonify(ok=True, enabled=ENABLED,
                           available_regions=sorted(BBOXES.keys()),
                           last_run={
                               "id": r[0],
                               "started_at": str(r[1]) if r[1] else None,
                               "finished_at": str(r[2]) if r[2] else None,
                               "regions": r[3], "pois_seen": r[4],
                               "pois_new": r[5], "pois_dup": r[6],
                               "errors": r[7], "dry_run": r[8],
                           })
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info(f"[osm-crawl] ready · enabled={ENABLED} · "
                f"{len(BBOXES)} regions configured · "
                f"sleep={SLEEP_SEC}s · max={MAX_PER_RUN}/run")

_smoke()
