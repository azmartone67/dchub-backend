"""
sec_permit_parser.py  –  DC Hub Phase 2 SEC/EDGAR Permit Date Enrichment
=========================================================================
Sources:
  - SEC EDGAR full-text search API (free, no auth required)
  - EDGAR filing document parser (10-K, 10-Q, 8-K)

Strategy:
  - For each known DC REIT/operator, fetch recent 10-K filings from EDGAR
  - Parse filing text for facility addresses + operational/opening dates
  - Match parsed addresses to DC Hub facilities via fuzzy matching
  - Write high-confidence dates to facility_permits + promote to facilities

Coverage targets (first run):
  Equinix (EQIX), Digital Realty (DLR), Iron Mountain (IRM),
  CyrusOne, QTS, CoreSite, Switch, Vantage, Cyxtera

No API key required — EDGAR is fully public.

Scheduler: runs monthly (1st of month, 03:00 UTC)
Deploy:    ~/workspace/sec_permit_parser.py on Railway
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
    format="%(asctime)s [sec_parser] %(levelname)s %(message)s",
)
log = logging.getLogger("sec_parser")

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL  = os.environ.get("NEON_DATABASE_URL") or os.environ["DATABASE_URL"]
REQUEST_DELAY = float(os.environ.get("SEC_REQUEST_DELAY", "1.0"))
MAX_COMPANIES = int(os.environ.get("SEC_MAX_COMPANIES", "999"))

EDGAR_BASE        = "https://data.sec.gov"
EDGAR_SEARCH_BASE = "https://efts.sec.gov"
EDGAR_HEADERS     = {
    "User-Agent": "DCHub Research dchub.cloud jonathan@dchub.cloud",
    "Accept-Encoding": "gzip, deflate",
}

# Confidence for SEC-sourced dates — highest tier
CONFIDENCE_SEC_EXACT = 0.92
CONFIDENCE_SEC_FUZZY = 0.78

# ── Target companies: CIK → metadata ─────────────────────────────────────────
DC_COMPANIES = {
    "0001101239": {"name": "Equinix",          "ticker": "EQIX", "type": "colo_reit"},
    "0001297996": {"name": "Digital Realty",   "ticker": "DLR",  "type": "hyperscale_reit"},
    "0001020569": {"name": "Iron Mountain",    "ticker": "IRM",  "type": "storage_dc"},
    "0001553610": {"name": "CyrusOne",         "ticker": "CONE", "type": "enterprise_dc"},
    "0001548648": {"name": "QTS Realty",       "ticker": "QTS",  "type": "hyperscale"},
    "0001716129": {"name": "Vertiv",           "ticker": "VRT",  "type": "dc_infra"},
    "0001496048": {"name": "CoreSite Realty",  "ticker": "COR",  "type": "colo"},
    "0001812093": {"name": "Vantage Data Centers", "ticker": None, "type": "hyperscale"},
    "0001817868": {"name": "EdgeConneX",       "ticker": None,   "type": "edge_dc"},
}

# Regex patterns for extracting dates from filing text
DATE_PATTERNS = [
    # "opened in March 2019", "commenced operations in Q2 2020"
    r"(?:opened?|commenced?\s+operations?|became\s+operational|placed\s+in\s+service|"
    r"completed?\s+construction|received?\s+certificate\s+of\s+occupancy|"
    r"co\s+issued?|co\s+received?)\s+(?:in\s+)?([A-Z][a-z]+\s+\d{4}|\d{4}|Q[1-4]\s+\d{4})",
    # "opened March 15, 2019"
    r"(?:opened?|launched?|commissioned?)\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    # "completion date of January 2020"
    r"completion\s+date\s+of\s+([A-Z][a-z]+\s+\d{4})",
    # "placed in service during fiscal year 2021"
    r"placed\s+in\s+service\s+during\s+(?:fiscal\s+year\s+)?(\d{4})",
]

# US state abbreviations for address extraction
US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}

# ── Database helpers ──────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def get_us_facilities(conn) -> list[dict]:
    """Load all US facilities for matching."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, address, city, state, country,
                   permit_confidence, permit_date
            FROM facilities
            WHERE country = 'US'
              AND city IS NOT NULL
              AND state IS NOT NULL
            ORDER BY id
        """)
        return [dict(r) for r in cur.fetchall()]


def upsert_permit(conn, facility_id: str, permit: dict) -> bool:
    """Write permit record and optionally promote to canonical facility columns."""
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
                approved_date = EXCLUDED.approved_date,
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
                approval_date      = %s,
                permit_source      = %s,
                permit_confidence  = %s,
                permit_enriched_at = NOW(),
                raw_permit_id      = %s
            WHERE id = %s
              AND (permit_confidence IS NULL OR permit_confidence < %s)
        """, (
            best_date,
            permit.get("approved_date"),
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


# ── EDGAR API helpers ─────────────────────────────────────────────────────────

async def get_recent_filings(client: httpx.AsyncClient, cik: str, forms=("10-K",)) -> list[dict]:
    """
    Fetch recent filings for a CIK from EDGAR submissions API.
    Returns list of filing metadata dicts.
    """
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json"
    try:
        resp = await client.get(url, headers=EDGAR_HEADERS, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()

        filings = data.get("filings", {}).get("recent", {})
        if not filings:
            return []

        results = []
        forms_set = set(f.upper() for f in forms)
        for i, form in enumerate(filings.get("form", [])):
            if form.upper() not in forms_set:
                continue
            acc = filings["accessionNumber"][i].replace("-", "")
            results.append({
                "cik":        cik_padded,
                "form":       form,
                "filed":      filings["filingDate"][i],
                "accession":  acc,
                "primary_doc": filings.get("primaryDocument", [""])[i] if i < len(filings.get("primaryDocument", [])) else "",
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik_padded)}/{acc}/",
            })
            if len(results) >= 3:  # last 3 filings per form type
                break
        return results
    except Exception as e:
        log.warning("EDGAR submissions error for CIK %s: %s", cik, e)
        return []


async def fetch_filing_text(client: httpx.AsyncClient, filing: dict) -> str:
    """
    Fetch the primary document text from an EDGAR filing.
    Strips HTML tags, returns plain text (truncated to 500KB).
    """
    cik_int = int(filing["cik"])
    acc     = filing["accession"]
    doc     = filing.get("primary_doc", "")

    # Try primary doc first, then index
    urls_to_try = []
    if doc:
        urls_to_try.append(
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"
        )
    urls_to_try.append(
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{acc}-index.htm"
    )

    for url in urls_to_try:
        try:
            resp = await client.get(url, headers=EDGAR_HEADERS, timeout=30.0)
            if resp.status_code == 200:
                text = resp.text[:600_000]  # cap at ~600KB
                # Strip HTML
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"&nbsp;", " ", text)
                text = re.sub(r"&amp;", "&", text)
                text = re.sub(r"\s{3,}", "  ", text)
                return text
        except Exception as e:
            log.debug("Fetch error %s: %s", url, e)
            continue
    return ""


# ── Facility address extraction from filing text ──────────────────────────────

FACILITY_SECTION_HEADERS = [
    r"our\s+(?:data\s+centers?|facilities|properties)",
    r"(?:data\s+center|facility|campus)\s+(?:portfolio|locations?|properties)",
    r"item\s+2[\.\s]+properties",
    r"properties\s+and\s+facilities",
]

ADDRESS_PATTERN = re.compile(
    r"(\d+\s+[A-Za-z0-9\s\.\-]+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|"
    r"Road|Rd|Lane|Ln|Court|Ct|Way|Parkway|Pkwy|Place|Pl)\.?)"
    r"[,\s]+([A-Za-z\s]+)[,\s]+("
    + "|".join(US_STATES) +
    r")[,\s]+(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE
)


def extract_facility_mentions(text: str, company_name: str) -> list[dict]:
    """
    Extract facility address + date mentions from a filing's text.
    Returns list of {address, city, state, date, context} dicts.
    """
    results = []

    # Find sections likely to contain facility listings
    section_text = text
    for header_pat in FACILITY_SECTION_HEADERS:
        m = re.search(header_pat, text, re.IGNORECASE)
        if m:
            # Take 20K chars after the section header
            section_text = text[m.start():m.start() + 20000]
            break

    # Find all address mentions
    for addr_match in ADDRESS_PATTERN.finditer(section_text):
        street = addr_match.group(1).strip()
        city   = addr_match.group(2).strip().rstrip(",")
        state  = addr_match.group(3).strip().upper()

        if state not in US_STATES:
            continue

        # Get surrounding context (500 chars before/after)
        start = max(0, addr_match.start() - 500)
        end   = min(len(section_text), addr_match.end() + 500)
        context = section_text[start:end]

        # Look for date near this address
        found_date = None
        for pat in DATE_PATTERNS:
            dm = re.search(pat, context, re.IGNORECASE)
            if dm:
                found_date = parse_date_string(dm.group(1))
                if found_date:
                    break

        results.append({
            "street":  street,
            "city":    city,
            "state":   state,
            "date":    found_date,
            "context": context[:300],
            "company": company_name,
        })

    return results


def parse_date_string(s: str) -> Optional[date]:
    """Parse a variety of date string formats into a date object."""
    s = s.strip()

    # Year only: "2019" → Jan 1 of that year (low precision)
    if re.match(r"^\d{4}$", s):
        try:
            return date(int(s), 1, 1)
        except ValueError:
            return None

    # Quarter: "Q2 2020" → April 1
    qm = re.match(r"Q([1-4])\s+(\d{4})", s, re.IGNORECASE)
    if qm:
        quarter_start = {1: 1, 2: 4, 3: 7, 4: 10}
        return date(int(qm.group(2)), quarter_start[int(qm.group(1))], 1)

    # Month Year: "March 2019"
    for fmt in ("%B %Y", "%b %Y"):
        try:
            d = datetime.strptime(s, fmt)
            return date(d.year, d.month, 1)
        except ValueError:
            continue

    # Full date: "March 15, 2019" or "March 15 2019"
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    return None


# ── Facility matching ─────────────────────────────────────────────────────────

def match_facility(mention: dict, facilities: list[dict]) -> Optional[tuple[dict, float]]:
    """
    Match a filing mention to a DC Hub facility.
    Returns (facility, confidence_score) or None.
    """
    state = mention.get("state", "").upper()
    city  = mention.get("city", "").strip()

    # Pre-filter by state + city for efficiency
    candidates = [
        f for f in facilities
        if f.get("state", "").upper() == state
    ]
    if not candidates:
        return None

    # Score each candidate
    best_score = 0.0
    best_fac   = None

    mention_addr = f"{mention.get('street','')} {city} {state}".upper()

    for fac in candidates:
        fac_addr = f"{fac.get('address','')} {fac.get('city','')} {fac.get('state','')}".upper()
        score = fuzz.token_sort_ratio(mention_addr, fac_addr) / 100.0

        # Boost score if city matches exactly
        if fac.get("city", "").lower() == city.lower():
            score = min(1.0, score + 0.10)

        if score > best_score:
            best_score = score
            best_fac   = fac

    if best_score >= 0.60:
        return best_fac, best_score
    return None


# ── Main enrichment loop ──────────────────────────────────────────────────────

async def process_company(
    client: httpx.AsyncClient,
    cik: str,
    company_meta: dict,
    facilities: list[dict],
    conn,
) -> dict:
    stats = {"permits_found": 0, "facilities_enriched": 0, "errors": 0}

    log.info("Processing %s (CIK %s)", company_meta["name"], cik)

    # Fetch recent 10-K and 10-Q filings
    filings = await get_recent_filings(client, cik, forms=("10-K", "10-Q"))
    await asyncio.sleep(REQUEST_DELAY)

    if not filings:
        log.info("  No filings found for %s", company_meta["name"])
        return stats

    log.info("  Found %d filings for %s", len(filings), company_meta["name"])

    for filing in filings[:2]:  # Process last 2 filings max per company
        try:
            text = await fetch_filing_text(client, filing)
            await asyncio.sleep(REQUEST_DELAY)

            if not text:
                log.debug("  Empty text for filing %s", filing["accession"])
                continue

            log.info("  Parsing %s %s (%s, %d chars)",
                     company_meta["name"], filing["form"], filing["filed"], len(text))

            mentions = extract_facility_mentions(text, company_meta["name"])
            log.info("  Found %d facility mentions", len(mentions))

            for mention in mentions:
                if not mention.get("date"):
                    continue  # skip mentions without a date

                match = match_facility(mention, facilities)
                if not match:
                    continue

                fac, score = match
                confidence = CONFIDENCE_SEC_EXACT if score >= 0.88 else CONFIDENCE_SEC_FUZZY

                permit = {
                    "permit_number":     f"sec_{cik}_{filing['accession']}_{fac['id']}",
                    "permit_type":       "sec_filing",
                    "permit_status":     "operational",
                    "applied_date":      None,
                    "approved_date":     mention["date"],
                    "issued_date":       None,
                    "final_date":        mention["date"],
                    "jurisdiction":      mention.get("city"),
                    "jurisdiction_state": mention.get("state"),
                    "source":            f"sec_edgar_{filing['form'].lower().replace('-','')}",
                    "source_url":        filing["url"],
                    "confidence":        confidence,
                    "raw_data": {
                        "cik":      cik,
                        "ticker":   company_meta.get("ticker"),
                        "company":  company_meta["name"],
                        "form":     filing["form"],
                        "filed":    filing["filed"],
                        "context":  mention.get("context", "")[:500],
                        "address":  mention.get("street"),
                        "city":     mention.get("city"),
                        "state":    mention.get("state"),
                        "match_score": round(score, 3),
                    },
                }

                stats["permits_found"] += 1
                promoted = upsert_permit(conn, fac["id"], permit)
                if promoted:
                    stats["facilities_enriched"] += 1
                    log.info("  ✓ Enriched %s (%s, %s) — %s from %s",
                             fac["name"], fac["city"], fac["state"],
                             mention["date"], filing["form"])

            conn.commit()

        except Exception as e:
            stats["errors"] += 1
            conn.rollback()
            log.error("  Error processing filing %s: %s", filing.get("accession"), e)

    return stats


async def run():
    log.info("── DC Hub SEC/EDGAR Parser (Phase 2) starting ──")
    conn = get_conn()
    t0   = time.time()

    total_stats = {
        "source":                "phase2_sec_edgar",
        "facilities_attempted":  0,
        "permits_found":         0,
        "facilities_enriched":   0,
        "errors":                0,
        "duration_seconds":      0.0,
        "notes":                 f"Companies: {len(DC_COMPANIES)}",
    }

    try:
        facilities = get_us_facilities(conn)
        log.info("Loaded %d US facilities for matching", len(facilities))

        companies = list(DC_COMPANIES.items())[:MAX_COMPANIES]
        total_stats["facilities_attempted"] = len(companies)

        async with httpx.AsyncClient(
            headers=EDGAR_HEADERS,
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            for cik, meta in companies:
                try:
                    stats = await process_company(client, cik, meta, facilities, conn)
                    total_stats["permits_found"]       += stats["permits_found"]
                    total_stats["facilities_enriched"] += stats["facilities_enriched"]
                    total_stats["errors"]              += stats["errors"]
                    await asyncio.sleep(REQUEST_DELAY * 2)  # be polite to EDGAR
                except Exception as e:
                    total_stats["errors"] += 1
                    log.error("Company error %s: %s", meta["name"], e)

    finally:
        total_stats["duration_seconds"] = round(time.time() - t0, 2)
        log_run(conn, total_stats)
        conn.close()

    log.info(
        "── Run complete: %d facilities enriched, %d permits found, %d errors in %.1fs ──",
        total_stats["facilities_enriched"],
        total_stats["permits_found"],
        total_stats["errors"],
        total_stats["duration_seconds"],
    )


if __name__ == "__main__":
    asyncio.run(run())
