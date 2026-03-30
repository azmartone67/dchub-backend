"""
sec_permit_parser.py  –  DC Hub Phase 2 SEC/EDGAR Permit Date Enrichment (v2)
==============================================================================
Strategy (revised after inspecting actual Equinix 10-K):

  SEC 10-K filings list metros (city names), NOT street addresses.
  No operational dates are given directly.

  Our approach:
    1. Parse the Properties section for city names listed as operational
    2. Cross-reference across multiple years of 10-Ks (2018-2025)
    3. Find the FIRST year a city appeared -> earliest confirmed operational date
    4. Match city -> DC Hub facilities in that city
    5. Write as moderate-confidence (0.75) permit records

  This gives us "operational by [year]" dates for thousands of facilities
  tied to the largest operators: Equinix, Digital Realty, Iron Mountain, etc.

No API key required. EDGAR is fully public.
Scheduler: monthly (1st of month, 03:00 UTC)
"""

import os
import re
import time
import json
import logging
import asyncio
from datetime import date, datetime
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sec_parser] %(levelname)s %(message)s",
)
log = logging.getLogger("sec_parser")

DATABASE_URL  = os.environ.get("NEON_DATABASE_URL") or os.environ["DATABASE_URL"]
REQUEST_DELAY = float(os.environ.get("SEC_REQUEST_DELAY", "1.5"))
MAX_COMPANIES = int(os.environ.get("SEC_MAX_COMPANIES", "999"))

EDGAR_HEADERS = {
    "User-Agent": "DCHub Research dchub.cloud jonathan@dchub.cloud",
    "Accept-Encoding": "gzip, deflate",
}

CONFIDENCE_CITY_FIRST_YEAR = 0.75
CONFIDENCE_CITY_KNOWN      = 0.60

# ── Target companies ──────────────────────────────────────────────────────────
DC_COMPANIES = {
    "0001101239": {"name": "Equinix",        "ticker": "EQIX"},
    "0001297996": {"name": "Digital Realty", "ticker": "DLR"},
    "0001020569": {"name": "Iron Mountain",  "ticker": "IRM"},
    "0001553610": {"name": "CyrusOne",       "ticker": "CONE"},
    "0001548648": {"name": "QTS Realty",     "ticker": "QTS"},
    "0001496048": {"name": "CoreSite",       "ticker": "COR"},
}

# City -> state for major DC markets
CITY_STATE_MAP = {
    "ashburn": "VA", "reston": "VA", "culpeper": "VA", "richmond": "VA",
    "manassas": "VA", "leesburg": "VA", "sterling": "VA",
    "atlanta": "GA", "dallas": "TX", "houston": "TX", "austin": "TX",
    "chicago": "IL", "seattle": "WA", "denver": "CO", "phoenix": "AZ",
    "silicon valley": "CA", "san jose": "CA", "santa clara": "CA",
    "los angeles": "CA", "san francisco": "CA", "sacramento": "CA",
    "new york": "NY", "new york city": "NY", "newark": "NJ", "secaucus": "NJ",
    "boston": "MA", "miami": "FL", "jacksonville": "FL", "tampa": "FL",
    "minneapolis": "MN", "kansas city": "MO", "st. louis": "MO",
    "las vegas": "NV", "reno": "NV", "salt lake city": "UT",
    "portland": "OR", "columbus": "OH", "cleveland": "OH", "cincinnati": "OH",
    "pittsburgh": "PA", "philadelphia": "PA", "charlotte": "NC", "raleigh": "NC",
    "nashville": "TN", "detroit": "MI", "washington": "DC",
    "washington, d.c.": "DC", "washington d.c.": "DC",
    "redwood city": "CA", "baltimore": "MD", "beltsville": "MD",
    "des moines": "IA", "omaha": "NE", "mesa": "AZ", "tempe": "AZ",
    "chandler": "AZ", "scottsdale": "AZ", "the dalles": "OR",
    "montgomery": "AL", "birmingham": "AL", "tucson": "AZ",
    "indianapolis": "IN", "albuquerque": "NM",
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def get_us_facilities(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, address, city, state, permit_confidence, permit_date
            FROM facilities
            WHERE country = 'US' AND city IS NOT NULL AND state IS NOT NULL
        """)
        return [dict(r) for r in cur.fetchall()]


def upsert_permit(conn, facility_id: str, permit: dict) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO facility_permits (
                facility_id, permit_number, permit_type, permit_status,
                applied_date, approved_date, issued_date, final_date,
                jurisdiction, jurisdiction_state,
                source, source_url, confidence, raw_data
            ) VALUES (
                %(facility_id)s, %(permit_number)s, %(permit_type)s, %(permit_status)s,
                %(applied_date)s, %(approved_date)s, %(issued_date)s, %(final_date)s,
                %(jurisdiction)s, %(jurisdiction_state)s,
                %(source)s, %(source_url)s, %(confidence)s, %(raw_data)s
            )
            ON CONFLICT (facility_id, permit_number, source) DO UPDATE SET
                final_date    = EXCLUDED.final_date,
                confidence    = EXCLUDED.confidence,
                raw_data      = EXCLUDED.raw_data,
                updated_at    = NOW()
        """, {
            **permit,
            "facility_id": facility_id,
            "raw_data": json.dumps(permit.get("raw_data", {})),
        })

    best_date = permit.get("final_date") or permit.get("approved_date")
    if not best_date:
        return False

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE facilities SET
                permit_date        = %s,
                permit_source      = %s,
                permit_confidence  = %s,
                permit_enriched_at = NOW(),
                raw_permit_id      = %s
            WHERE id = %s
              AND (permit_confidence IS NULL OR permit_confidence < %s)
        """, (
            best_date,
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
            VALUES (%(source)s, %(facilities_attempted)s, %(permits_found)s,
                    %(facilities_enriched)s, %(errors)s, %(duration_seconds)s, %(notes)s)
        """, stats)
    conn.commit()


# ── EDGAR helpers ─────────────────────────────────────────────────────────────

async def get_10k_filings(client: httpx.AsyncClient, cik: str) -> list[dict]:
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        resp = await client.get(url, headers=EDGAR_HEADERS, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        filings = data.get("filings", {}).get("recent", {})
        results = []
        for i, form in enumerate(filings.get("form", [])):
            if form != "10-K":
                continue
            acc = filings["accessionNumber"][i].replace("-", "")
            results.append({
                "cik_int":   int(cik_padded),
                "accession": acc,
                "filed":     filings["filingDate"][i],
                "year":      int(filings["filingDate"][i][:4]),
                "primary_doc": filings.get("primaryDocument", [""])[i] if i < len(filings.get("primaryDocument", [])) else "",
            })
            if len(results) >= 8:
                break
        return results
    except Exception as e:
        log.warning("Submissions error CIK %s: %s", cik, e)
        return []


async def fetch_properties_section(client: httpx.AsyncClient, filing: dict) -> str:
    """Fetch 10-K and return just the Item 2 Properties section text."""
    cik_int = filing["cik_int"]
    acc     = filing["accession"]
    doc     = filing.get("primary_doc", "")

    urls = []
    if doc:
        urls.append(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}")
    # Fallback: scan index for main doc
    urls.append(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/")

    text = ""
    for url in urls:
        try:
            resp = await client.get(url, headers=EDGAR_HEADERS, timeout=60.0)
            if resp.status_code != 200:
                continue
            raw = resp.text

            # If this is the index page, find the main doc link
            if url.endswith("/"):
                links = re.findall(
                    rf'href="(/Archives/edgar/data/{cik_int}/{acc}/[^"]+\.htm)"',
                    raw, re.IGNORECASE
                )
                main = [l for l in links if not re.search(r'ex-?\d|exhibit', l, re.IGNORECASE)]
                if main:
                    resp2 = await client.get(
                        "https://www.sec.gov" + main[0],
                        headers=EDGAR_HEADERS, timeout=60.0
                    )
                    raw = resp2.text

            # Strip HTML
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"&#\d+;", " ", text)
            text = re.sub(r"&[a-zA-Z]+;", " ", text)
            text = re.sub(r"\s{3,}", "  ", text)

            # Find Item 2 Properties
            m2 = re.search(r"ITEM\s+2[\.\s]+PROPERTIES", text, re.IGNORECASE)
            if not m2:
                continue
            m3 = re.search(r"ITEM\s+3[\.\s]+", text[m2.end():], re.IGNORECASE)
            end = m2.end() + m3.start() if m3 else m2.start() + 25000
            return text[m2.start():end]

        except Exception as e:
            log.debug("Fetch error %s: %s", url, e)
            continue

    return ""


def parse_cities(section: str) -> set[str]:
    """Extract city names from an Equinix/DLR-style Properties table."""
    cities = set()

    # Bullet table rows: "Atlanta  ● ●" or "Washington, D.C./Ashburn  ●"
    for m in re.finditer(
        r"([A-Z][A-Za-z][A-Za-z\s\.\,\/\-]{1,35}?)\s{2,}(?:&#9679;|&#x25CF;|●|•|\u25cf|\u2022|\(1\)|\(2\)|[Ll]eased|[Oo]wned)",
        section
    ):
        raw = m.group(1).strip().rstrip(".,/- ")
        for part in re.split(r"[/]", raw):
            part = part.strip().rstrip(".,-()")
            if 2 < len(part) < 40:
                cities.add(part.lower())

    # Prose: "data center in Dallas" / "campus in Ashburn, Virginia"
    for m in re.finditer(
        r"(?:data\s+center|facility|campus|IBX)\s+(?:in|at|near)\s+([A-Z][A-Za-z\s]+?)(?:,|\s+Virginia|\s+Texas|\s+California|[\.\n])",
        section, re.IGNORECASE
    ):
        cities.add(m.group(1).strip().lower())

    # Clean
    return {c.strip(".,/()-") for c in cities if len(c.strip(".,/()-")) > 2 and not c[0].isdigit()}


# ── Matching ──────────────────────────────────────────────────────────────────

def match_by_city(city: str, facilities: list[dict]) -> list[dict]:
    state = CITY_STATE_MAP.get(city.lower())
    city_lower = city.lower()
    results = []
    for fac in facilities:
        fac_city = (fac.get("city") or "").lower()
        if fac_city == city_lower or city_lower in fac_city or fac_city in city_lower:
            if state and fac.get("state", "").upper() != state:
                continue
            results.append(fac)
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

async def process_company(client, cik, meta, facilities, conn) -> dict:
    stats = {"permits_found": 0, "facilities_enriched": 0, "errors": 0}
    log.info("Processing %s (CIK %s)", meta["name"], cik)

    filings = await get_10k_filings(client, cik)
    await asyncio.sleep(REQUEST_DELAY)
    if not filings:
        return stats

    log.info("  %d 10-K filings (%s-%s)", len(filings), filings[-1]["year"], filings[0]["year"])

    city_first: dict[str, tuple[int, dict]] = {}

    for filing in reversed(filings):  # oldest first
        try:
            section = await fetch_properties_section(client, filing)
            await asyncio.sleep(REQUEST_DELAY)
            if not section:
                continue
            cities = parse_cities(section)
            log.info("  %s %s: %d cities", meta["name"], filing["filed"], len(cities))
            for city in cities:
                if city not in city_first or filing["year"] < city_first[city][0]:
                    city_first[city] = (filing["year"], filing)
        except Exception as e:
            stats["errors"] += 1
            log.error("  Filing error %s: %s", filing.get("filed"), e)

    log.info("  %s: %d unique cities total", meta["name"], len(city_first))

    for city, (first_year, filing) in city_first.items():
        matched = match_by_city(city, facilities)
        if not matched:
            continue

        op_date = date(first_year, 1, 1)
        for fac in matched:
            permit = {
                "permit_number":      f"sec_{cik}_{first_year}_{fac['id']}",
                "permit_type":        "sec_10k_property_listing",
                "permit_status":      "operational",
                "applied_date":       None,
                "approved_date":      op_date,
                "issued_date":        None,
                "final_date":         op_date,
                "jurisdiction":       fac.get("city"),
                "jurisdiction_state": fac.get("state"),
                "source":             f"sec_edgar_10k_{meta.get('ticker', cik)}",
                "source_url":         f"https://www.sec.gov/Archives/edgar/data/{filing['cik_int']}/{filing['accession']}/",
                "confidence":         CONFIDENCE_CITY_FIRST_YEAR,
                "raw_data": {
                    "company":    meta["name"],
                    "ticker":     meta.get("ticker"),
                    "city":       city,
                    "first_year": first_year,
                    "filed":      filing["filed"],
                    "note":       "Operational by this year (first 10-K appearance)",
                },
            }
            try:
                stats["permits_found"] += 1
                promoted = upsert_permit(conn, fac["id"], permit)
                if promoted:
                    stats["facilities_enriched"] += 1
                    log.info("  ✓ %s (%s, %s) — by %s via %s",
                             fac["name"], fac["city"], fac["state"], first_year, meta["name"])
                conn.commit()
            except Exception as e:
                stats["errors"] += 1
                conn.rollback()
                log.error("  DB error fac %s: %s", fac["id"], e)

    return stats


async def run():
    log.info("── DC Hub SEC/EDGAR Parser (Phase 2) starting ──")
    conn = get_conn()
    t0 = time.time()
    total = {
        "source": "phase2_sec_edgar",
        "facilities_attempted": 0,
        "permits_found": 0,
        "facilities_enriched": 0,
        "errors": 0,
        "duration_seconds": 0.0,
        "notes": f"Companies: {min(len(DC_COMPANIES), MAX_COMPANIES)}",
    }
    try:
        facilities = get_us_facilities(conn)
        log.info("Loaded %d US facilities", len(facilities))
        companies = list(DC_COMPANIES.items())[:MAX_COMPANIES]
        total["facilities_attempted"] = len(companies)

        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            for cik, meta in companies:
                try:
                    s = await process_company(client, cik, meta, facilities, conn)
                    for k in ("permits_found", "facilities_enriched", "errors"):
                        total[k] += s[k]
                    await asyncio.sleep(REQUEST_DELAY * 3)
                except Exception as e:
                    total["errors"] += 1
                    log.error("Company error %s: %s", meta["name"], e)
    finally:
        total["duration_seconds"] = round(time.time() - t0, 2)
        log_run(conn, total)
        conn.close()

    log.info("── Done: %d enriched, %d permits, %d errors in %.1fs ──",
             total["facilities_enriched"], total["permits_found"],
             total["errors"], total["duration_seconds"])


if __name__ == "__main__":
    asyncio.run(run())
