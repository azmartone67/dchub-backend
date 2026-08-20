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

INFRA SAFETY (r-osm-flap, 2026-06-18)
=====================================
  · NO pooled DB connection is held across an Overpass HTTP fetch.
    The connection is acquired ONLY around each bbox's insert work and
    closed before the next fetch (fixes "FORCED RECLAIM: held 62s by
    osm_crawler.py" → pool exhaustion → site-wide flapping).
  · On Overpass 429/504/timeout: back off (OSM_CRAWL_BACKOFF_S, def 8s)
    then SKIP that bbox — no retry-hammering.
  · WALL-CLOCK BUDGET per run (OSM_CRAWL_BUDGET_S, def 180s) + a
    max-bboxes-per-run cap (OSM_CRAWL_MAX_BBOXES, def 6). A single
    POST /api/v1/admin/osm-crawl/run can never run ~10 min (was 582s);
    it stops cleanly + returns a summary, remaining regions next run.
"""
import os
from internal_auth import accepted_internal_keys
import time
import json
import hashlib
import logging
import datetime
from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)
osm_crawler_bp = Blueprint("osm_crawler", __name__)


_INTERNAL_KEYS = accepted_internal_keys()
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

    # Continental US (CONUS) — Item 2b (2026-06-13). Pre-fix only AK + HI
    # covered the US, so the OSM crawler never swept the lower 48 (the
    # single biggest DC market on earth). Split into regional boxes small
    # enough that each Overpass query (6 telecom/office/industrial
    # sub-queries @ 25s timeout) returns without a 504. Boundaries chosen
    # to bias toward known DC clusters (Northern Virginia, Dallas, Phoenix,
    # Bay Area, Chicago) without overlap.
    "us-northeast":   (38.5, -80.6, 47.6, -66.9),   # ME→VA, incl NoVA/NY/NJ
    "us-southeast":   (24.4, -88.5, 38.5, -75.0),   # FL→VA, Atlanta corridor
    "us-mid-atlantic-oh": (36.5, -88.5, 42.5, -80.5),  # OH/KY/TN/WV overlap-trim
    "us-midwest-east": (38.5, -91.5, 49.4, -80.6),  # IL/IN/MI/WI/OH (Chicago)
    "us-midwest-west": (36.0, -104.1, 49.4, -91.5), # MN/IA/MO/KS/NE/Dakotas
    "us-south-central": (25.8, -106.7, 36.5, -88.5),# TX/OK/AR/LA/MS/AL (Dallas)
    "us-mountain":    (31.3, -117.3, 49.0, -104.1), # CO/UT/AZ/NM/NV/ID/MT/WY
    "us-pacific-nw":  (41.9, -124.9, 49.1, -116.5), # WA/OR/N-ID
    "us-california":  (32.5, -124.5, 42.1, -114.1), # CA (Bay Area/LA/Silicon V)
}

OVERPASS = os.environ.get("OSM_OVERPASS_URL",
                          "https://overpass-api.de/api/interpreter")
# ★ 2026-08-10: was "DCHubCrawler/1.0 (+https://dchub.cloud/contact)".
# overpass-api.de now answers HTTP 406 to any User-Agent containing the token
# "crawler" — measured 3/3 deterministic against the live service, while the
# IDENTICAL query under a UA without that token got through (429/504, i.e. the
# normal rate limiter). The bisect, run 2026-08-10 against
# https://overpass-api.de/api/interpreter:
#     DCHubCrawler/1.0 (+https://dchub.cloud/contact)  → 406
#     DCHubCrawler/1.0                                 → 406
#     dchub-crawler/1.0 (+https://dchub.cloud/contact) → 406
#     Mozilla/5.0                                      → 406
#     DCHub/1.0 (+https://dchub.cloud)                 → 429  (accepted)
#     curl/8.7.1                                       → 429  (accepted)
# 406 was NOT in the handled-status set below, so it fell through to a generic
# "error", every bbox returned zero elements, and the run reported "swept 0
# POIs" — 12 consecutive zero-row runs, green until 2026-08-08 made the
# zero-fetch branch exit 1. Keep the contact URL (Overpass asks for one); just
# never ship the word "crawler" in it. Env-overridable so a future block can be
# worked around without a deploy.
USER_AGENT = os.environ.get("OSM_USER_AGENT",
                            "DCHubBot/1.0 (+https://dchub.cloud/contact)")
SLEEP_SEC = float(os.environ.get("OSM_CRAWL_SLEEP", "3.0"))
MAX_PER_RUN = int(os.environ.get("OSM_CRAWL_MAX", "500"))
ENABLED = (os.environ.get("OSM_CRAWL_ENABLED",
                          os.environ.get("DCM_CRAWL_ENABLED", "false"))
           .lower() in ("1", "true", "yes"))

# ── r-osm-flap (2026-06-18) — infra-safety caps ──────────────────────
# The crawler was flapping the whole site: it held ONE pooled DB
# connection across slow Overpass HTTP fetches (FORCED RECLAIM: held
# 62s by osm_crawler.py → site-wide pool exhaustion) and the run loop
# swept every bbox synchronously (SLOW REQUEST 582s on one POST). Three
# guardrails, all env-tunable:
#   1. Wall-clock budget per run — stop cleanly + return a summary so a
#      single request can never run ~10 minutes (default 180s).
#   2. Max bboxes per run — a hard cap on how many regions one POST will
#      sweep (default 6); the rest are picked up on the next invocation.
#   3. Overpass backoff — on 429/504/timeout, sleep then SKIP that bbox
#      (no retry-hammering of overpass-api.de).
# The DB connection is now acquired ONLY around each bbox's insert work
# (fetch-then-short-lived-connection-then-close), never across a fetch.
OSM_CRAWL_BUDGET_S = float(os.environ.get("OSM_CRAWL_BUDGET_S", "180"))
OSM_CRAWL_MAX_BBOXES = int(os.environ.get("OSM_CRAWL_MAX_BBOXES", "6"))
# Polite backoff after a throttle/timeout from Overpass, then skip.
OSM_BACKOFF_S = float(os.environ.get("OSM_CRAWL_BACKOFF_S", "8"))


# ── Overpass query ───────────────────────────────────────────────────
def _query_bbox(bbox: tuple) -> tuple[list[dict], str]:
    """Run an Overpass query for data center POIs in a bounding box.

    Returns (elements, status) where status is:
      · "ok"       — query succeeded (elements may be an empty list)
      · "throttle" — Overpass returned 429 (Too Many Requests)
      · "timeout"  — Overpass returned 504 / the socket timed out
      · "rejected" — Overpass refused the REQUEST ITSELF (403/406). Not
                     transient and not our rate: the service is turning this
                     client away, and every bbox will get the same answer.
                     Split out from "error" on 2026-08-10 because a 406 on the
                     User-Agent looked identical to a parse failure and cost 12
                     silent zero-row runs.
      · "error"    — any other failure
    The caller uses the status to BACK OFF + SKIP on throttle/timeout
    instead of hammering overpass-api.de (which is what drove the
    repeated 429/504 storm in the logs)."""
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
    import urllib.error
    import socket
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
            return (body.get("elements", []) or [], "ok")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.info(f"[osm-crawl] bbox {bbox} → HTTP 429 (throttle)")
            return ([], "throttle")
        if e.code in (502, 503, 504):
            logger.info(f"[osm-crawl] bbox {bbox} → HTTP {e.code} (timeout)")
            return ([], "timeout")
        if e.code in (403, 406):
            # The service is refusing this CLIENT, not this moment. Retrying
            # and sweeping the next bbox both waste the run — every region
            # gets the same answer. Say so loudly enough to be actionable.
            logger.error(
                "[osm-crawl] bbox %s → HTTP %d REJECTED — Overpass refused the "
                "request itself. Most likely the User-Agent (%r): "
                "overpass-api.de 406s any UA containing 'crawler'. Override "
                "with OSM_USER_AGENT / OSM_OVERPASS_URL.", bbox, e.code,
                USER_AGENT)
            return ([], "rejected")
        logger.info(f"[osm-crawl] bbox {bbox} → HTTP {e.code}")
        return ([], "error")
    except (socket.timeout, TimeoutError) as e:
        logger.info(f"[osm-crawl] bbox {bbox} → socket timeout: {e}")
        return ([], "timeout")
    except Exception as e:
        logger.info(f"[osm-crawl] bbox {bbox} → {type(e).__name__}: {e}")
        return ([], "error")


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
        "status": "Operational",
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
    """Insert into both facilities + discovered_facilities.

    FIX r22 (2026-05-20): OSM crawl reported pois_new=58 for UK but
    facilities count stayed at 12,556 — silent INSERT failure. Root
    cause: the SELECT-dedup queries shared a cursor with the INSERT,
    and if the SELECT touched anything in an aborted transaction
    state (likely the LOWER(name) check on Unicode strings), the
    subsequent INSERT was silently dropped on transaction abort.

    Fixes applied:
      · Use RETURNING id on the INSERT and return added=True ONLY
        when we got a row back. No more "thought it inserted" lies.
      · Use SAVEPOINTs around each statement so a single failure
        doesn't poison the whole row's insert sequence.
      · Explicit rollback of any aborted transaction state before
        each statement.
    """
    name = r["name"]
    source_id = ("osm_" + hashlib.sha256(
        f"{name}|{r.get('city','')}|{r.get('country','')}".encode()
    ).hexdigest()[:16])

    # ── 1. Dedup: check if already in canonical ──
    try:
        cur.execute(
            "SELECT 1 FROM facilities WHERE source_id = %s LIMIT 1",
            (source_id,),
        )
        if cur.fetchone():
            return False, source_id
        cur.execute(
            "SELECT 1 FROM facilities WHERE LOWER(name) = LOWER(%s) LIMIT 1",
            (name,),
        )
        if cur.fetchone():
            return False, source_id
    except Exception as e:
        logger.warning(f"[osm-crawl] dedup query failed for {name[:40]}: {str(e)[:100]}")
        try: cur.connection.rollback()
        except Exception: pass
        return False, source_id

    # ── 2. INSERT with RETURNING — confirms actual landing ──
    inserted_id = None
    try:
        cur.execute("""
            INSERT INTO facilities
              (id, name, provider, city, state, country, power_mw,
               status, address, source, source_id)
            -- ★2026-08-11: power_mw and status are NULL, not 0 /
            -- 'Operational'. OSM tells us a data centre EXISTS; it does not
            -- tell us its capacity or whether it is running. Asserting both
            -- stamped a fabricated 'Operational' on every discovered row,
            -- including 758 named only "OSM DC <id>". (No percent sign may
            -- appear in this string, comments included — psycopg2 scans the
            -- whole query and an undoubled one raises here.)
            VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s,
                    'openstreetmap', %s)
            RETURNING id
        """, (
            source_id, name, r.get("provider"),
            r.get("city"), r.get("state"), r.get("country") or None,
            r.get("address") or None, source_id,
        ))
        row = cur.fetchone()
        inserted_id = row[0] if row else None
    except Exception as e:
        logger.warning(f"[osm-crawl] canonical INSERT failed for "
                       f"{name[:40]}: {str(e)[:160]}")
        try: cur.connection.rollback()
        except Exception: pass
        return False, source_id

    if not inserted_id:
        # No row came back — INSERT silently dropped (shouldn't happen
        # with RETURNING but defensive)
        return False, source_id

    # ── 3. Stage into discovered_facilities (best-effort) ──
    try:
        cur.execute("""
            INSERT INTO discovered_facilities (
                source, source_id, name, provider, city, state, country,
                latitude, longitude, power_mw, status, address,
                confidence_score, is_duplicate,
                merged_facility_id, discovered_at, first_seen, last_updated
            )
            -- ★2026-08-11: power_mw + status NULL (see above). The 0.9
            -- confidence_score is deliberately left alone — it is a per-writer
            -- constant across this whole family, and correcting that is its
            -- own change rather than a drive-by here.
            VALUES ('openstreetmap', %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                    NULL, %s, 0.9, 0, %s,
                    NOW(), NOW(), NOW())
            ON CONFLICT (source, source_id) DO UPDATE SET
                name = EXCLUDED.name,
                last_updated = NOW()
        """, (
            source_id, name, r.get("provider"),
            r.get("city"), r.get("state"), r.get("country") or None,
            r.get("_osm_lat"), r.get("_osm_lon"),
            r.get("address") or None, source_id,
        ))
    except Exception as e:
        logger.warning(f"[osm-crawl] discovered_facilities stage "
                       f"skipped for {name[:40]}: {str(e)[:120]}")
        # Don't abort the row — canonical is already in. Commit will
        # clear the aborted state via the savepoint convention.
        try: cur.connection.rollback()
        except Exception: pass
        # Re-run the canonical INSERT since rollback wiped it
        try:
            cur.execute("""
                INSERT INTO facilities
                  (id, name, provider, city, state, country, power_mw,
                   status, address, source, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s,
                        'openstreetmap', %s)
                ON CONFLICT DO NOTHING
            """, (
                source_id, name, r.get("provider"),
                r.get("city"), r.get("state"), r.get("country") or None,
                r.get("address") or None, source_id,
            ))
        except Exception:
            note_swallowed_write("facilities", where="osm_crawler._insert_row")
            pass

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
        # r-osm-flap telemetry — how the run terminated + what it touched.
        "regions_processed": [],
        "regions_skipped": [],
        "throttled": 0,
        # 2026-08-10: set when Overpass refuses the CLIENT (403/406) rather
        # than throttling it. Distinct from `throttled` because the remedy is
        # different — a rejected client never recovers by waiting.
        "rejected": False,
        "stopped_reason": None,
    }
    cap_hit = False
    _ensure_log_table()

    # r-osm-flap (2026-06-18): wall-clock budget so a single
    # POST /api/v1/admin/osm-crawl/run can never run ~10 minutes
    # (observed 582s). budget + max-bboxes are env-tunable.
    deadline = time.monotonic() + OSM_CRAWL_BUDGET_S

    for idx, region_slug in enumerate(regions):
        if cap_hit:
            summary["stopped_reason"] = "max_per_run"
            break
        # Wall-clock budget: stop cleanly + return a summary. The
        # remaining regions are recorded as skipped (next run sweeps them).
        if time.monotonic() >= deadline:
            summary["stopped_reason"] = "time_budget"
            summary["regions_skipped"].extend(regions[idx:])
            break
        # Max-bboxes-per-run cap.
        if len(summary["regions_processed"]) >= OSM_CRAWL_MAX_BBOXES:
            summary["stopped_reason"] = "max_bboxes"
            summary["regions_skipped"].extend(regions[idx:])
            break

        bbox = BBOXES[region_slug]

        # ── NETWORK FETCH — NO DB CONNECTION HELD HERE ──────────────
        # The connection is acquired only AFTER the fetch, around the
        # insert work, then released before the next fetch. This is the
        # fix for "FORCED RECLAIM: held 62s by osm_crawler.py" — a
        # pooled connection was being held across these slow Overpass
        # HTTP calls, exhausting the pool and flapping the site.
        elements, status = _query_bbox(bbox)

        if status in ("throttle", "timeout"):
            # Back off + SKIP this bbox — do NOT retry-hammer Overpass.
            summary["errors"] += 1
            summary["throttled"] += 1
            summary["regions_skipped"].append(region_slug)
            logger.info(f"[osm-crawl] {region_slug} {status} → backoff "
                        f"{OSM_BACKOFF_S}s + skip")
            time.sleep(OSM_BACKOFF_S)
            continue
        if status == "rejected":
            # Client-level refusal (403/406). Every remaining bbox will get the
            # same answer, so sweeping them only buries the cause under a pile
            # of identical failures. Stop and name it.
            summary["errors"] += 1
            summary["rejected"] = True
            summary["regions_skipped"].extend(regions[idx:])
            summary["stopped_reason"] = "overpass_rejected_client"
            logger.error("[osm-crawl] aborting sweep — Overpass rejected the "
                         "client at %s; remaining regions skipped", region_slug)
            break
        if status != "ok":
            summary["errors"] += 1
            summary["regions_skipped"].append(region_slug)
            time.sleep(SLEEP_SEC)
            continue

        summary["regions_processed"].append(region_slug)
        # Polite delay between bbox fetches (Overpass usage policy).
        time.sleep(SLEEP_SEC)

        if not elements:
            continue
        summary["pois_seen"] += len(elements)

        # ── DB WORK — SHORT-LIVED CONNECTION, this bbox only ────────
        # Acquire here (after the fetch), insert this bbox's rows, then
        # release in finally before looping to the next fetch. A whole
        # bbox of inserts completes well under the 60s reclaim window.
        if dry_run:
            for e in elements:
                if summary["pois_new"] >= MAX_PER_RUN:
                    cap_hit = True
                    break
                row = _osm_to_row(e, region_slug)
                if not row:
                    continue
                if len(summary["examples"]) < 30:
                    summary["examples"].append({
                        "name": row["name"], "operator": row.get("provider"),
                        "city": row.get("city"), "country": row.get("country"),
                        "region": region_slug,
                    })
                summary["pois_new"] += 1
            continue

        c = _get_db()
        if c is None:
            # No DB — treat like dry-run accounting so the run still
            # finishes cleanly and reports.
            for e in elements:
                if summary["pois_new"] >= MAX_PER_RUN:
                    cap_hit = True
                    break
                row = _osm_to_row(e, region_slug)
                if not row:
                    continue
                summary["pois_new"] += 1
            continue
        try:
            for e in elements:
                if summary["pois_new"] >= MAX_PER_RUN:
                    cap_hit = True
                    break
                row = _osm_to_row(e, region_slug)
                if not row:
                    continue
                try:
                    # FIX r22: ensure clean transaction state before
                    # each row so a previous row's aborted state
                    # doesn't silently kill THIS row's INSERT.
                    try: c.rollback()
                    except Exception: pass
                    with c.cursor() as cur:
                        added, sid = _insert_row(cur, row)
                    try: c.commit()
                    except Exception as _ce:
                        logger.warning(f"[osm-crawl] commit failed for "
                                       f"{row.get('name','')[:40]}: {_ce}")
                        added = False
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
        finally:
            # Release the connection BEFORE the next bbox fetch so it is
            # never held across an Overpass HTTP call.
            try: c.close()
            except Exception: pass

    if summary["stopped_reason"] is None:
        summary["stopped_reason"] = "completed"
    summary["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    summary["ok"] = True

    # ── Log the run (own short-lived connection) ────────────────────
    if not dry_run:
        lc = _get_db()
        if lc is not None:
            try:
                with lc.cursor() as cur:
                    cur.execute("""
                        INSERT INTO osm_crawl_log
                          (regions, pois_seen, pois_new, pois_dup,
                           errors, dry_run, finished_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                    """, (summary["regions_processed"], summary["pois_seen"],
                           summary["pois_new"], summary["pois_dup"],
                           summary["errors"], dry_run))
                try: lc.commit()
                except Exception: pass
            except Exception:
                try: lc.rollback()
                except Exception: pass
            finally:
                try: lc.close()
                except Exception: pass

        # ── Overpass-throttle sentinel (2026-07-27) ──────────────────
        # `errors` conflates three causes (throttle skip / non-ok fetch /
        # insert failure) and the run log persists only the COUNT, so a
        # rising number reads as vague "crawler errors" when the real
        # story is: Overpass throttled us and we SKIPPED most regions.
        # File the breakdown as a brain finding so it is durable and
        # visible without reading logs. Fail-soft: never affect the crawl.
        try:
            _thr = int(summary.get("throttled") or 0)
            _skipped = list(summary.get("regions_skipped") or [])
            _done = list(summary.get("regions_processed") or [])
            _alert = int(os.environ.get("OSM_THROTTLE_ALERT", "3"))
            # ★ ONLY throttle counts as a problem. `regions_skipped` also
            # collects the deliberate rotation cap (34 regions configured
            # vs OSM_CRAWL_MAX_BBOXES=6/run and a 180s budget), so a
            # "skipped > done" rule would fire on EVERY healthy run and
            # train the board to ignore this detector.
            if _thr >= _alert:
                from routes.brain_findings_writer import upsert_brain_finding
                fc = _get_db()
                if fc is not None:
                    try:
                        with fc.cursor() as cur:
                            upsert_brain_finding(
                                cur,
                                issue="ingest_health:osm_overpass_throttle",
                                url="dchub://ingest/osm-crawl",
                                count=_thr,
                                detail=("[warn] Overpass THROTTLED %d of %d "
                                        "attempted region(s) this run (crawled "
                                        "%d, seen=%d new=%d, errors=%d). "
                                        "Throttled regions are lost coverage — "
                                        "distinct from the deliberate rotation "
                                        "cap (OSM_CRAWL_MAX_BBOXES=%s/run over "
                                        "%d configured regions). Levers: raise "
                                        "OSM_BACKOFF_S (now %ss) or lower the "
                                        "per-run bbox cap so fewer queries get "
                                        "rate-limited. Sample skipped: %s"
                                        % (_thr, _thr + len(_done), len(_done),
                                           summary.get("pois_seen", 0),
                                           summary.get("pois_new", 0),
                                           summary.get("errors", 0),
                                           os.environ.get("OSM_CRAWL_MAX_BBOXES", "6"),
                                           len(BBOXES),
                                           os.environ.get("OSM_BACKOFF_S", "8"),
                                           ", ".join(_skipped[:6])))[:2000],
                                detector="osm_crawler", status="open")
                        fc.commit()
                    finally:
                        try: fc.close()
                        except Exception: pass
        except Exception as _se:
            logger.info("[osm-crawl] throttle sentinel skipped: %s",
                        str(_se)[:120])

    return summary


# ── Endpoints ────────────────────────────────────────────────────────
@osm_crawler_bp.route("/api/v1/admin/osm-crawl/run", methods=["POST"])
def crawl_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    region = (request.args.get("region") or "").strip().lower() or None
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    # 2026-07-27: the full crawl takes ~3+ minutes, which NO proxy in front
    # of this app survives — the CF worker returns its own 503 envelope and
    # the Railway proxy returns 502 "Application failed to respond" (and a
    # deploy mid-crawl kills the request outright). Three red cron days came
    # from that, not from the crawler. `async=1` spawns it and returns
    # immediately; the caller polls /status, which reads osm_crawl_log —
    # the crawl already persists every run there, so nothing is lost.
    if (request.args.get("async") or "").lower() in ("1", "true", "yes"):
        import threading

        def _bg():
            try:
                _crawl(region, dry_run)
            except Exception as e:  # never surface in the request path
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "osm crawl (bg) failed: %s", str(e)[:160])
                except Exception:
                    pass

        threading.Thread(target=_bg, name="osm-crawl-bg",
                         daemon=True).start()
        return jsonify(ok=True, spawned=True, mode="async", region=region,
                       dry_run=dry_run,
                       poll="/api/v1/admin/osm-crawl/status"), 202
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
                f"sleep={SLEEP_SEC}s · max={MAX_PER_RUN}/run · "
                f"budget={OSM_CRAWL_BUDGET_S}s · "
                f"max_bboxes={OSM_CRAWL_MAX_BBOXES}/run · "
                f"backoff={OSM_BACKOFF_S}s")

_smoke()
