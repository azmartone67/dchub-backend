"""
DC Hub: Facility Square Footage Enrichment Script
===================================================
Searches the web for square footage data for each facility
and backfills the sqft column in the facilities table.

USAGE:
    # Dry run (no DB writes, just prints findings)
    python3 enrich_sqft.py --dry-run --limit 20

    # Production run (writes to DB)
    python3 enrich_sqft.py --limit 100

    # Target specific provider
    python3 enrich_sqft.py --provider "Equinix" --limit 50

    # Resume from where you left off (skip already-enriched)
    python3 enrich_sqft.py --skip-existing --limit 500

REQUIRES:
    pip install anthropic psycopg2-binary --break-system-packages
    
    Environment variables:
    - ANTHROPIC_API_KEY (your Anthropic API key)
    - DATABASE_URL (Neon PostgreSQL connection string)

COST ESTIMATE:
    ~$0.003-0.005 per facility (Sonnet 4 with web search)
    1,000 facilities ≈ $3-5
    11,000 facilities ≈ $33-55
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timezone

import anthropic
import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024
BATCH_SIZE = 10          # Commit every N updates
RATE_LIMIT_DELAY = 1.5   # Seconds between API calls (avoid rate limits)
MAX_RETRIES = 2           # Retries per facility on API error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("enrich_sqft.log")
    ]
)
logger = logging.getLogger("sqft_enrichment")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    """Get a PostgreSQL connection."""
    return psycopg2.connect(os.environ["DATABASE_URL"])


def get_facilities_to_enrich(conn, limit=100, provider=None, skip_existing=True):
    """Fetch facilities that need sqft enrichment."""
    sql = """
        SELECT id, name, provider, city, state, country, power_mw, address
        FROM facilities
        WHERE 1=1
    """
    params = []
    
    if skip_existing:
        sql += " AND (sqft IS NULL OR sqft = 0)"
    
    if provider:
        sql += " AND provider ILIKE %s"
        params.append(f"%{provider}%")
    
    # Prioritize larger / more well-known facilities (more likely to have public data)
    sql += " ORDER BY power_mw DESC NULLS LAST, name ASC"
    sql += " LIMIT %s"
    params.append(limit)
    
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(sql, params)
    return c.fetchall()


def update_sqft(conn, facility_id, sqft, source_note, confidence):
    """Update a facility's sqft in the database."""
    conn.cursor().execute("""
        UPDATE facilities
        SET sqft = %s,
            last_updated = %s
        WHERE id = %s AND (sqft IS NULL OR sqft = 0)
    """, (sqft, datetime.now(timezone.utc).strftime("%Y-%m-%d"), facility_id))


def update_operational_year(conn, facility_id, year):
    """Update a facility's operational_year if found."""
    conn.cursor().execute("""
        UPDATE facilities
        SET operational_year = %s,
            last_updated = %s
        WHERE id = %s AND operational_year IS NULL
    """, (year, datetime.now(timezone.utc).strftime("%Y-%m-%d"), facility_id))


# ---------------------------------------------------------------------------
# AI enrichment
# ---------------------------------------------------------------------------

def build_search_prompt(facility):
    """Build the prompt for Claude to search and extract sqft."""
    name = facility["name"]
    provider = facility.get("provider") or ""
    city = facility.get("city") or ""
    state = facility.get("state") or ""
    country = facility.get("country") or "US"
    power_mw = facility.get("power_mw") or ""
    address = facility.get("address") or ""
    
    location = ", ".join(filter(None, [city, state, country]))
    
    return f"""Find the square footage (sqft) for this data center facility. Also find the year it became operational if available.

Facility: {name}
Provider: {provider}
Location: {location}
Address: {address}
Power capacity: {power_mw} MW

Search for this specific facility's square footage. Check the provider's website, news articles, SEC filings, real estate listings, and data center directories.

Respond ONLY with a JSON object (no markdown, no backticks, no explanation):
{{
    "sqft": <integer or null if not found>,
    "sqft_source": "<brief source description or null>",
    "sqft_confidence": <float 0.0-1.0>,
    "operational_year": <4-digit integer or null if not found>,
    "year_source": "<brief source description or null>",
    "notes": "<any relevant context, e.g. if the number is for a campus vs single building>"
}}

Rules:
- Only return sqft you find from credible sources, never estimate or guess
- If you find a range, use the midpoint
- If the number is in square meters, convert to sqft (multiply by 10.764)
- sqft_confidence: 0.9+ = from official source/SEC filing, 0.7-0.9 = from news/directory, 0.5-0.7 = indirect/estimated
- If you cannot find sqft data, return null for sqft fields
- For operational_year, look for "opened in", "operational since", "built in", "commissioned" dates"""


def search_facility_sqft(client, facility, dry_run=False):
    """Use Claude with web search to find a facility's sqft."""
    prompt = build_search_prompt(facility)
    
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }],
                messages=[{"role": "user", "content": prompt}]
            )
            
            # Extract text from response
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            
            # Parse JSON from response
            text = text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            if text.startswith("json"):
                text = text[4:].strip()
            
            result = json.loads(text)
            
            # Validate
            sqft = result.get("sqft")
            if sqft is not None:
                sqft = int(sqft)
                if sqft < 500 or sqft > 50_000_000:  # Sanity check
                    logger.warning(f"  Unreasonable sqft {sqft} for {facility['name']}, skipping")
                    result["sqft"] = None
                    result["sqft_confidence"] = 0
            
            op_year = result.get("operational_year")
            if op_year is not None:
                op_year = int(op_year)
                if op_year < 1960 or op_year > 2030:
                    logger.warning(f"  Unreasonable year {op_year} for {facility['name']}, skipping")
                    result["operational_year"] = None
            
            return result
            
        except json.JSONDecodeError as e:
            logger.warning(f"  JSON parse error (attempt {attempt+1}): {e}")
            logger.warning(f"  Raw text: {text[:200]}")
            if attempt < MAX_RETRIES:
                time.sleep(2)
                continue
            return None
            
        except anthropic.RateLimitError:
            wait = 30 * (attempt + 1)
            logger.warning(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
            
        except anthropic.APIError as e:
            logger.error(f"  API error (attempt {attempt+1}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
            
        except Exception as e:
            logger.error(f"  Unexpected error: {e}")
            return None
    
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Enrich DC Hub facilities with sqft data")
    parser.add_argument("--limit", type=int, default=20, help="Max facilities to process")
    parser.add_argument("--provider", type=str, help="Filter by provider name")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--skip-existing", action="store_true", default=True, help="Skip facilities with sqft > 0")
    parser.add_argument("--delay", type=float, default=RATE_LIMIT_DELAY, help="Seconds between API calls")
    args = parser.parse_args()
    
    # Validate env
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)
    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL not set")
        sys.exit(1)
    
    client = anthropic.Anthropic()
    conn = get_conn()
    
    # Fetch facilities to enrich
    facilities = get_facilities_to_enrich(
        conn, 
        limit=args.limit, 
        provider=args.provider,
        skip_existing=args.skip_existing
    )
    
    logger.info(f"Found {len(facilities)} facilities to enrich")
    if args.dry_run:
        logger.info("DRY RUN — no database writes")
    
    # Stats
    total = len(facilities)
    found_sqft = 0
    found_year = 0
    errors = 0
    
    for i, facility in enumerate(facilities, 1):
        name = facility["name"]
        provider = facility.get("provider") or "Unknown"
        logger.info(f"[{i}/{total}] {name} ({provider})")
        
        result = search_facility_sqft(client, facility, dry_run=args.dry_run)
        
        if result is None:
            errors += 1
            logger.warning(f"  FAILED — no result")
            time.sleep(args.delay)
            continue
        
        sqft = result.get("sqft")
        confidence = result.get("sqft_confidence", 0)
        op_year = result.get("operational_year")
        source = result.get("sqft_source") or result.get("year_source") or ""
        notes = result.get("notes") or ""
        
        if sqft:
            found_sqft += 1
            logger.info(f"  SQFT: {sqft:,} (confidence: {confidence}, source: {source})")
            if not args.dry_run:
                update_sqft(conn, facility["id"], sqft, source, confidence)
        else:
            logger.info(f"  SQFT: not found")
        
        if op_year:
            found_year += 1
            logger.info(f"  YEAR: {op_year} (source: {result.get('year_source', '')})")
            if not args.dry_run:
                update_operational_year(conn, facility["id"], op_year)
        
        if notes:
            logger.info(f"  NOTE: {notes}")
        
        # Commit in batches
        if not args.dry_run and i % BATCH_SIZE == 0:
            conn.commit()
            logger.info(f"  Committed batch ({i} processed)")
        
        # Rate limit
        time.sleep(args.delay)
    
    # Final commit
    if not args.dry_run:
        conn.commit()
    
    conn.close()
    
    # Summary
    logger.info("=" * 60)
    logger.info(f"ENRICHMENT COMPLETE")
    logger.info(f"  Processed:  {total}")
    logger.info(f"  Sqft found: {found_sqft} ({found_sqft/max(total,1)*100:.1f}%)")
    logger.info(f"  Year found: {found_year} ({found_year/max(total,1)*100:.1f}%)")
    logger.info(f"  Errors:     {errors}")
    logger.info(f"  Mode:       {'DRY RUN' if args.dry_run else 'PRODUCTION'}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
