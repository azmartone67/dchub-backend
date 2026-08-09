"""
permit_scraper.py  –  DC Hub Phase 1 Permit Data Enrichment
============================================================
Sources:
  - BuildingPermit.io  (primary: structured API, ~75% US county coverage)
  - Municode / OpenGov portal fallback (secondary: HTML scrape by jurisdiction)

Matching logic:
  - Match by address → facility_id  (exact + fuzzy fallback)
  - Confidence scoring: 0.0–1.0
  - Never overwrites a higher-confidence existing record

Scheduler:  runs weekly (Sunday 02:00 UTC)
Deploy:     ~/workspace/scripts/permit_scraper.py  on Railway
"""

import os
import re
import time
import json
import logging
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [permit_scraper] %(levelname)s %(message)s",
)
log = logging.getLogger("permit_scraper")

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL       = os.environ["DATABASE_URL"]
BUILDING_PERMIT_KEY = os.environ.get("BUILDING_PERMIT_API_KEY", "")   # BuildingPermit.io
BATCH_SIZE         = int(os.environ.get("PERMIT_BATCH_SIZE", "100"))
REQUEST_DELAY      = float(os.environ.get("PERMIT_REQUEST_DELAY", "1.2"))  # seconds between API calls
MAX_FACILITIES     = int(os.environ.get("PERMIT_MAX_FACILITIES", "500"))   # per run cap

# Confidence weights per source
CONFIDENCE = {
    "buildingpermit_exact":   0.85,
    "buildingpermit_fuzzy":   0.65,
    "county_portal_exact":    0.80,
    "county_portal_fuzzy":    0.60,
}

# Permit types we care about (data centers are industrial/commercial)
RELEVANT_PERMIT_TYPES = {
    "building", "commercial", "industrial", "electrical",
    "mechanical", "co", "certificate_of_occupancy", "zoning",
    "construction", "new_construction",
}

# Keywords that confirm a data center permit
DC_KEYWORDS = [
    "data center", "datacenter", "colocation", "colo",
    "server", "computing facility", "network operations",
    "critical facility", "raised floor",
]


# ── Database helpers ──────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def get_queued_facilities(conn) -> list[dict]:
    """Get facilities from permit_enrichment_queue (newly approved)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT f.id, f.name, f.address, f.city, f.state, f.country,
                   f.operational_year, f.permit_confidence
            FROM permit_enrichment_queue q
            JOIN facilities f ON f.id = q.facility_id
            WHERE q.status = 'pending'
              AND f.permit_date IS NULL
              AND f.country = 'US'
            ORDER BY q.queued_at ASC
            LIMIT 50
        """)
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            ids = [r['id'] for r in rows]
            cur.execute(
                "UPDATE permit_enrichment_queue SET status='processing' WHERE facility_id = ANY(%s)",
                (ids,)
            )
        return rows


def mark_queue_processed(conn, facility_ids: list):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE permit_enrichment_queue SET status='done', processed_at=NOW() WHERE facility_id = ANY(%s)",
            (facility_ids,)
        )
    conn.commit()


def get_facilities_needing_permits(conn, limit: int) -> list[dict]:
    """
    Return facilities missing permit_date, prioritising:
      1. US facilities (country = 'United States')
      2. Those with a known address/city/state
      3. Operational facilities first
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                id, name, address, city, state_province, postal_code,
                country, operational_year, permit_confidence
            FROM facilities
            WHERE
                (country = 'United States' OR country IS NULL)
                AND permit_date IS NULL
                AND address IS NOT NULL
                AND city IS NOT NULL
                AND state_province IS NOT NULL
            ORDER BY
                CASE WHEN status = 'operational' THEN 0 ELSE 1 END,
                permit_confidence ASC NULLS FIRST,
                id ASC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def upsert_permit(conn, facility_id: str, permit: dict) -> bool:
    """
    Insert or update a permit record.
    Returns True if the facility's canonical permit_date was updated.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO facility_permits (
                facility_id, permit_number, permit_type, permit_status,
                applied_date, approved_date, issued_date, final_date,
                jurisdiction, jurisdiction_state,
                source, source_url, confidence, raw_data
            ) VALUES (
                %(facility_id) ON CONFLICT DO NOTHINGs, %(permit_number)s, %(permit_type)s, %(permit_status)s,
                %(applied_date)s, %(approved_date)s, %(issued_date)s, %(final_date)s,
                %(jurisdiction)s, %(jurisdiction_state)s,
                %(source)s, %(source_url)s, %(confidence)s, %(raw_data)s
            )
            ON CONFLICT (facility_id, permit_number, source) DO UPDATE SET
                permit_status  = EXCLUDED.permit_status,
                final_date     = EXCLUDED.final_date,
                approved_date  = EXCLUDED.approved_date,
                confidence     = EXCLUDED.confidence,
                raw_data       = EXCLUDED.raw_data,
                updated_at     = NOW()
        """, {
            **permit,
            "facility_id": facility_id,
            "raw_data": json.dumps(permit.get("raw_data", {})),
        })

    # Promote to canonical facility columns if confidence is higher
    best_date = permit.get("final_date") or permit.get("approved_date") or permit.get("issued_date")
    if not best_date:
        return False

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE facilities SET
                permit_date         = %s,
                approval_date       = %s,
                co_date             = CASE WHEN %s = 'co' THEN %s ELSE co_date END,
                permit_source       = %s,
                permit_confidence   = %s,
                permit_enriched_at  = NOW(),
                raw_permit_id       = %s
            WHERE id = %s
              AND (permit_confidence IS NULL OR permit_confidence < %s)
        """, (
            best_date,
            permit.get("approved_date"),
            permit.get("permit_type", ""),
            permit.get("final_date"),
            permit["source"],
            permit["confidence"],
            permit.get("permit_number"),
            facility_id,
            permit["confidence"],
        ))
        return cur.rowcount > 0


def log_run(conn, stats: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO permit_scrape_log
                (source, facilities_attempted, permits_found,
                 facilities_enriched, errors, duration_seconds, notes)
            VALUES (%(source) ON CONFLICT DO NOTHINGs, %(facilities_attempted)s, %(permits_found)s,
                    %(facilities_enriched)s, %(errors)s, %(duration_seconds)s, %(notes)s)
        """, stats)
    conn.commit()


# ── Address normalisation ─────────────────────────────────────────────────────

def normalise_address(addr: str) -> str:
    addr = addr.upper().strip()
    replacements = {
        r"\bSTREET\b": "ST", r"\bAVENUE\b": "AVE", r"\bBOULEVARD\b": "BLVD",
        r"\bDRIVE\b": "DR",  r"\bROAD\b": "RD",   r"\bLANE\b": "LN",
        r"\bCOURT\b": "CT",  r"\bCIRCLE\b": "CIR", r"\bPLACE\b": "PL",
        r"\bNORTH\b": "N",   r"\bSOUTH\b": "S",    r"\bEAST\b": "E",
        r"\bWEST\b": "W",
    }
    for pattern, repl in replacements.items():
        addr = re.sub(pattern, repl, addr)
    return addr


def address_match_score(facility: dict, permit_address: str) -> float:
    fac_addr = normalise_address(
        f"{facility.get('address','')} {facility.get('city','')} {facility.get('state_province','')}"
    )
    permit_addr = normalise_address(permit_address)
    return fuzz.token_sort_ratio(fac_addr, permit_addr) / 100.0


def is_dc_permit(description: str) -> bool:
    desc = (description or "").lower()
    return any(kw in desc for kw in DC_KEYWORDS)


# ── BuildingPermit.io ─────────────────────────────────────────────────────────

async def query_buildingpermit_io(
    client: httpx.AsyncClient,
    facility: dict,
) -> list[dict]:
    """
    Query BuildingPermit.io address-level endpoint.
    Docs: https://buildingpermit.io/api-docs
    Falls back to lat/lng bounding box if address lookup returns nothing.
    """
    if not BUILDING_PERMIT_KEY:
        return []

    headers = {"Authorization": f"Bearer {BUILDING_PERMIT_KEY}"}
    address_str = (
        f"{facility['address']}, {facility['city']}, "
        f"{facility['state_province']} {facility.get('postal_code','')}"
    ).strip().rstrip(",")

    try:
        resp = await client.get(
            "https://api.buildingpermit.io/v1/permits",
            params={
                "address": address_str,
                "permit_type": "building,commercial,industrial,electrical,co",
                "limit": 20,
            },
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        permits = data.get("permits") or data.get("results") or []
        return permits
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return []
        log.warning("BuildingPermit.io HTTP %s for facility %s", e.response.status_code, facility["id"])
        return []
    except Exception as e:
        log.warning("BuildingPermit.io error for facility %s: %s", facility["id"], e)
        return []


def parse_buildingpermit_record(raw: dict, facility: dict) -> Optional[dict]:
    """Convert a raw BuildingPermit.io record to our schema."""

    def parse_date(val) -> Optional[date]:
        if not val:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(str(val)[:10], fmt[:10]).date()
            except ValueError:
                continue
        return None

    permit_addr = raw.get("address") or raw.get("street_address") or ""
    score = address_match_score(facility, permit_addr)
    if score < 0.50:
        return None  # too weak a match

    ptype = (raw.get("permit_type") or raw.get("type") or "").lower().replace(" ", "_")
    if ptype not in RELEVANT_PERMIT_TYPES and not is_dc_permit(raw.get("description", "")):
        return None

    confidence_key = "buildingpermit_exact" if score >= 0.90 else "buildingpermit_fuzzy"

    return {
        "permit_number":      raw.get("permit_number") or raw.get("id") or f"bp_{facility['id']}_{ptype}",
        "permit_type":        ptype,
        "permit_status":      (raw.get("status") or "unknown").lower(),
        "applied_date":       parse_date(raw.get("applied_date") or raw.get("application_date")),
        "approved_date":      parse_date(raw.get("approved_date") or raw.get("approval_date")),
        "issued_date":        parse_date(raw.get("issued_date") or raw.get("issue_date")),
        "final_date":         parse_date(raw.get("final_date") or raw.get("co_date") or raw.get("completion_date")),
        "jurisdiction":       raw.get("jurisdiction") or facility.get("city"),
        "jurisdiction_state": facility.get("state_province"),
        "source":             "buildingpermit_io",
        "source_url":         raw.get("url") or raw.get("source_url"),
        "confidence":         CONFIDENCE[confidence_key] * score,
        "raw_data":           raw,
    }


# ── County portal fallback (OpenGov / Accela pattern) ────────────────────────

COUNTY_PORTALS = {
    # state → portal pattern (add more as discovered)
    "VA": "https://energov.fairfaxcounty.gov/EnerGoV/Citizen/api/Permits",
    "TX": "https://permitsearch.austintexas.gov/api/permits",
    "GA": "https://epermitcentral.com/api/search",
}


async def query_county_portal(
    client: httpx.AsyncClient,
    facility: dict,
) -> list[dict]:
    """
    Attempt county portal lookup for states where we have known endpoints.
    Returns raw records (portal-specific shapes — parsed downstream).
    """
    state = facility.get("state_province", "").upper()
    portal_url = COUNTY_PORTALS.get(state)
    if not portal_url:
        return []

    try:
        resp = await client.get(
            portal_url,
            params={
                "address": facility.get("address", ""),
                "city": facility.get("city", ""),
                "type": "commercial,industrial",
                "limit": 10,
            },
            timeout=12.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results") or data.get("permits") or data.get("items") or []
    except Exception as e:
        log.debug("County portal error (%s) for facility %s: %s", state, facility["id"], e)
        return []


def parse_county_portal_record(raw: dict, facility: dict) -> Optional[dict]:
    """Generic parser for county portal records — adapts common field shapes."""

    def parse_date(val) -> Optional[date]:
        if not val:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(str(val)[:10], fmt).date()
            except ValueError:
                continue
        return None

    addr_fields = ["address", "street", "siteAddress", "site_address", "propertyAddress"]
    permit_addr = next((raw[f] for f in addr_fields if f in raw and raw[f]), "")
    score = address_match_score(facility, permit_addr)
    if score < 0.55:
        return None

    confidence_key = "county_portal_exact" if score >= 0.88 else "county_portal_fuzzy"
    ptype = (raw.get("permitType") or raw.get("permit_type") or raw.get("type") or "building").lower()

    return {
        "permit_number":      raw.get("permitNumber") or raw.get("permit_number") or raw.get("id") or "unknown",
        "permit_type":        ptype.replace(" ", "_"),
        "permit_status":      (raw.get("status") or raw.get("permitStatus") or "unknown").lower(),
        "applied_date":       parse_date(raw.get("applicationDate") or raw.get("applied_date")),
        "approved_date":      parse_date(raw.get("approvalDate") or raw.get("approved_date")),
        "issued_date":        parse_date(raw.get("issuedDate") or raw.get("issue_date")),
        "final_date":         parse_date(raw.get("finalDate") or raw.get("co_date") or raw.get("completionDate")),
        "jurisdiction":       raw.get("jurisdiction") or facility.get("city"),
        "jurisdiction_state": facility.get("state_province"),
        "source":             f"county_portal_{facility.get('state_province','').lower()}",
        "source_url":         raw.get("url"),
        "confidence":         CONFIDENCE[confidence_key] * score,
        "raw_data":           raw,
    }


# ── Main enrichment loop ──────────────────────────────────────────────────────

async def enrich_batch(facilities: list[dict], conn) -> dict:
    stats = {
        "source": "phase1_scraper",
        "facilities_attempted": len(facilities),
        "permits_found": 0,
        "facilities_enriched": 0,
        "errors": 0,
        "duration_seconds": 0.0,
        "notes": "",
    }
    t0 = time.time()

    async with httpx.AsyncClient(
        headers={"User-Agent": "DCHub-PermitScraper/1.0 (dchub.cloud)"},
        follow_redirects=True,
    ) as client:
        for fac in facilities:
            fac_enriched = False
            try:
                # ── BuildingPermit.io ──
                bp_records = await query_buildingpermit_io(client, fac)
                await asyncio.sleep(REQUEST_DELAY)

                for raw in bp_records:
                    parsed = parse_buildingpermit_record(raw, fac)
                    if parsed:
                        stats["permits_found"] += 1
                        promoted = upsert_permit(conn, fac["id"], parsed)
                        if promoted:
                            fac_enriched = True

                # ── County portal fallback ──
                county_records = await query_county_portal(client, fac)
                await asyncio.sleep(REQUEST_DELAY * 0.5)

                for raw in county_records:
                    parsed = parse_county_portal_record(raw, fac)
                    if parsed:
                        stats["permits_found"] += 1
                        promoted = upsert_permit(conn, fac["id"], parsed)
                        if promoted:
                            fac_enriched = True

                conn.commit()
                if fac_enriched:
                    stats["facilities_enriched"] += 1
                    log.info("✓ Enriched facility %s (%s, %s)", fac["id"], fac.get("name",""), fac.get("city",""))

            except Exception as e:
                stats["errors"] += 1
                conn.rollback()
                log.error("Error processing facility %s: %s", fac["id"], e)

    stats["duration_seconds"] = round(time.time() - t0, 2)
    return stats


async def run():
    log.info("── DC Hub Permit Scraper (Phase 1) starting ──")
    conn = get_conn()
    try:
        # Drain newly approved facilities from queue first
        queued = get_queued_facilities(conn)
        if queued:
            log.info("Processing %d queued newly-approved facilities", len(queued))
            await enrich_batch(queued, conn)
            mark_queue_processed(conn, [f["id"] for f in queued])

        facilities = get_facilities_needing_permits(conn, MAX_FACILITIES)
        log.info("Found %d facilities needing permit enrichment", len(facilities))

        if not facilities:
            log.info("Nothing to do — all facilities already enriched or no US addresses found")
            return

        # Process in batches to keep memory bounded
        total_stats = {
            "source": "phase1_scraper",
            "facilities_attempted": 0,
            "permits_found": 0,
            "facilities_enriched": 0,
            "errors": 0,
            "duration_seconds": 0.0,
            "notes": f"Batch size: {BATCH_SIZE}, total facilities: {len(facilities)}",
        }

        for i in range(0, len(facilities), BATCH_SIZE):
            batch = facilities[i:i + BATCH_SIZE]
            log.info("Processing batch %d/%d (%d facilities)",
                     i // BATCH_SIZE + 1, -(-len(facilities) // BATCH_SIZE), len(batch))
            batch_stats = await enrich_batch(batch, conn)

            for key in ("facilities_attempted", "permits_found", "facilities_enriched", "errors"):
                total_stats[key] += batch_stats[key]
            total_stats["duration_seconds"] += batch_stats["duration_seconds"]

        log.run_stats = total_stats
        log_run(conn, total_stats)
        log.info(
            "── Run complete: %d/%d facilities enriched, %d permits found, %d errors in %.1fs ──",
            total_stats["facilities_enriched"],
            total_stats["facilities_attempted"],
            total_stats["permits_found"],
            total_stats["errors"],
            total_stats["duration_seconds"],
        )

    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(run())
