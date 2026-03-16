"""
news_facility_extractor.py — DC Hub News-Based Facility Discovery

Scans data center industry news sources for new facility announcements
and extracts structured metadata for insertion into discovered_facilities.

Usage:
    from news_facility_extractor import scan_news_sources, extract_facility_from_article

Scheduler integration (add to dchub-scheduler.py):
    @scheduler.task('cron', id='news_facility_extraction', hour=6, minute=0)
    def run_news_facility_extraction():
        from news_facility_extractor import scan_news_sources
        scan_news_sources()
"""

import re
import json
import logging
import traceback
from datetime import datetime

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# NEWS SOURCES — high-signal DC industry publications
# ─────────────────────────────────────────────────────────

NEWS_SOURCES = [
    # Tier 1: Industry-specific (highest signal for construction/expansion)
    {'name': 'DCD', 'rss': 'https://www.datacenterdynamics.com/en/rss/news/', 'category': 'construction'},
    {'name': 'DCF', 'url': 'https://www.datacenterfrontier.com/', 'category': 'construction'},
    {'name': 'DCP', 'url': 'https://datacenterpost.com/', 'category': 'construction'},
    {'name': 'DIN', 'url': 'https://digitalinfranetwork.com/news/', 'category': 'construction'},
    {'name': 'DCM', 'url': 'https://datacentremagazine.com/', 'category': 'construction'},

    # Tier 2: Press release wires (catch announcements early)
    {'name': 'PRNewswire', 'url': 'https://www.prnewswire.com/news-releases/technology-latest-news/', 'category': 'press'},
    {'name': 'BusinessWire', 'url': 'https://www.businesswire.com/portal/site/home/', 'category': 'press'},

    # Tier 3: Commercial real estate (catches land acquisitions)
    {'name': 'CPE', 'url': 'https://www.commercialsearch.com/news/', 'category': 'real_estate'},
    {'name': 'REBO', 'url': 'https://rebusinessonline.com/', 'category': 'real_estate'},
]

# ─────────────────────────────────────────────────────────
# FACILITY DETECTION PATTERNS
# ─────────────────────────────────────────────────────────

FACILITY_ANNOUNCEMENT_PATTERNS = [
    # Construction triggers
    r'(?:breaks? ground|broke ground|groundbreaking)\s+(?:on|at|for)',
    r'(?:begin|start|commence)s?\s+construction',
    r'under construction',

    # Announcement triggers
    r'(?:announce|unveil|reveal|plan)s?\s+(?:new|a|plans for)\s+.*?data cent(?:er|re)',
    r'(?:new|major)\s+data cent(?:er|re)\s+(?:campus|hub|facility|project)',

    # Acquisition/land triggers
    r'(?:acquire|purchase|secure)s?\s+(?:land|site|acres|property)\s+(?:for|to)',
    r'(?:\d+)[- ]acre',

    # Power triggers (strong signal for hyperscale)
    r'(\d+)\s*(?:MW|megawatt)',
    r'(?:secure|contract|agree)s?\s+(?:\d+)\s*MW',

    # Investment triggers
    r'\$[\d.]+\s*(?:billion|million|B|M)\s+(?:data cent|investment|campus)',
]

# ─────────────────────────────────────────────────────────
# STATUS CLASSIFICATION
# ─────────────────────────────────────────────────────────

STATUS_KEYWORDS = {
    'Operational': ['operational', 'online', 'live', 'launched', 'open for business', 'completed'],
    'Under Construction': ['construction', 'broke ground', 'breaks ground', 'groundbreaking',
                           'building', 'under development', 'site work underway'],
    'Announced': ['announced', 'unveiled', 'revealed', 'signed agreement', 'plans to develop',
                  'will develop', 'will build', 'has selected'],
    'Planned': ['planned', 'proposed', 'seeking approval', 'zoning', 'entitled', 'exploring'],
}


def classify_status(text):
    """Classify facility status from article text. Returns most advanced status found."""
    text_lower = text.lower()
    # Check in priority order (most advanced first)
    for status in ['Operational', 'Under Construction', 'Announced', 'Planned']:
        for kw in STATUS_KEYWORDS[status]:
            if kw in text_lower:
                return status
    return 'Announced'


# ─────────────────────────────────────────────────────────
# METADATA EXTRACTORS
# ─────────────────────────────────────────────────────────

def extract_power_mw(text):
    """Extract MW capacity from article text."""
    patterns = [
        r'(\d+(?:\.\d+)?)\s*(?:MW|megawatt)',
        r'(\d+(?:\.\d+)?)\s*(?:mw|Mw)',
    ]
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            values.append(float(match.group(1)))
    # Return the largest value found (usually the total campus capacity)
    return max(values) if values else None


def extract_investment_usd(text):
    """Extract investment amount in USD from article text."""
    patterns = [
        (r'\$(\d+(?:\.\d+)?)\s*billion', 1_000_000_000),
        (r'\$(\d+(?:\.\d+)?)\s*B\b', 1_000_000_000),
        (r'\$(\d+(?:\.\d+)?)\s*million', 1_000_000),
        (r'\$(\d+(?:\.\d+)?)\s*M\b', 1_000_000),
        (r'€(\d+(?:\.\d+)?)\s*(?:billion|B)', 1_100_000_000),  # rough EUR→USD
        (r'€(\d+(?:\.\d+)?)\s*(?:million|M)', 1_100_000),
    ]
    for pattern, multiplier in patterns:
        match = re.search(pattern, text)
        if match:
            return int(float(match.group(1)) * multiplier)
    return None


def extract_acreage(text):
    """Extract acreage from article text."""
    match = re.search(r'(\d+(?:,\d+)?)[- ]acre', text)
    if match:
        return int(match.group(1).replace(',', ''))
    return None


def extract_sqft(text):
    """Extract square footage from article text."""
    patterns = [
        r'([\d,]+)\s*(?:square feet|sq\.?\s*ft|SF)',
        r'([\d,]+)\s*(?:sqft|square-foot)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1).replace(',', ''))
    return None


# ─────────────────────────────────────────────────────────
# US STATE DETECTION
# ─────────────────────────────────────────────────────────

US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY',
}

# Reverse map: abbreviation → full name
US_STATE_ABBREVS = {v: k.title() for k, v in US_STATES.items()}


def extract_state(text):
    """Extract US state from article text. Returns 2-letter abbreviation or None."""
    # Check for state abbreviations with context (e.g., "Ashburn, VA" or "in Virginia")
    for state_name, abbrev in US_STATES.items():
        if state_name in text.lower():
            return abbrev
    # Check 2-letter abbreviations preceded by comma+space or "in "
    match = re.search(r'(?:,\s*|\bin\s+)([A-Z]{2})\b', text)
    if match and match.group(1) in US_STATE_ABBREVS:
        return match.group(1)
    return None


def extract_country(text):
    """Extract country from article text. Returns ISO 2-letter code."""
    country_patterns = {
        'US': [r'\bU\.?S\.?A?\b', r'\bUnited States\b', r'\bAmerica\b'],
        'IE': [r'\bIreland\b'],
        'ES': [r'\bSpain\b', r'\bMadrid\b'],
        'GB': [r'\bUnited Kingdom\b', r'\bU\.?K\.?\b', r'\bEngland\b', r'\bLondon\b'],
        'DE': [r'\bGermany\b', r'\bFrankfurt\b'],
        'NL': [r'\bNetherlands\b', r'\bAmsterdam\b'],
        'SG': [r'\bSingapore\b'],
        'JP': [r'\bJapan\b', r'\bTokyo\b'],
        'AU': [r'\bAustralia\b', r'\bSydney\b', r'\bMelbourne\b'],
        'CA': [r'\bCanada\b', r'\bToronto\b', r'\bMontreal\b'],
        'FR': [r'\bFrance\b', r'\bParis\b', r'\bMarseille\b'],
        'IN': [r'\bIndia\b', r'\bMumbai\b', r'\bChennai\b'],
    }
    for country_code, patterns in country_patterns.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return country_code
    return 'US'  # default assumption for DC news


# ─────────────────────────────────────────────────────────
# CORE EXTRACTION
# ─────────────────────────────────────────────────────────

def is_facility_announcement(title, body):
    """Check if an article is about a new data center facility."""
    combined = f"{title} {body}"
    for pattern in FACILITY_ANNOUNCEMENT_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    return False


def extract_facility_from_article(title, body, source_url, source_name):
    """
    Extract facility metadata from a news article.
    Returns a dict ready for discovered_facilities INSERT, or None if not a facility announcement.
    """
    if not is_facility_announcement(title, body):
        return None

    combined = f"{title} {body}"
    country = extract_country(combined)
    state = extract_state(combined) if country == 'US' else None

    facility = {
        'name': title[:200],  # Use article title as facility name (to be refined)
        'provider': None,     # Requires NLP or LLM to extract reliably
        'city': None,         # Requires NLP or LLM to extract reliably
        'state': state,
        'country': country,
        'latitude': None,
        'longitude': None,
        'power_mw': extract_power_mw(combined),
        'sqft': extract_sqft(combined),
        'status': classify_status(combined),
        'source': 'news_extraction',
        'source_url': source_url,
        'confidence_score': 0.65,  # Lower confidence — needs manual review
        'discovered_at': datetime.utcnow().strftime('%Y-%m-%d'),
        'notes': f'Auto-extracted from {source_name}',
        'investment_usd': extract_investment_usd(combined),
        'acreage': extract_acreage(combined),
    }

    # Boost confidence if we extracted multiple strong signals
    signals = sum([
        facility['power_mw'] is not None,
        facility['investment_usd'] is not None,
        facility['acreage'] is not None,
        facility['state'] is not None,
    ])
    if signals >= 3:
        facility['confidence_score'] = 0.80
    elif signals >= 2:
        facility['confidence_score'] = 0.75

    return facility


# ─────────────────────────────────────────────────────────
# DATABASE INSERT HELPER
# ─────────────────────────────────────────────────────────

def insert_discovered_facility(conn, facility):
    """
    Insert an extracted facility into discovered_facilities.
    Returns the new row ID, or None if duplicate/error.
    """
    try:
        cur = conn.cursor()

        # Dedup check: same source_url already exists?
        cur.execute(
            "SELECT id FROM discovered_facilities WHERE source_url = %s LIMIT 1",
            (facility['source_url'],)
        )
        if cur.fetchone():
            logger.debug(f"Skipping duplicate source_url: {facility['source_url']}")
            return None

        cur.execute("""
            INSERT INTO discovered_facilities
                (name, provider, city, state, country, latitude, longitude,
                 power_mw, sqft, status, source, source_url,
                 confidence_score, discovered_at, notes,
                 investment_usd, acreage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            facility['name'], facility['provider'], facility['city'],
            facility['state'], facility['country'],
            facility['latitude'], facility['longitude'],
            facility['power_mw'], facility['sqft'],
            facility['status'], facility['source'], facility['source_url'],
            facility['confidence_score'], facility['discovered_at'],
            facility['notes'],
            facility.get('investment_usd'), facility.get('acreage'),
        ))
        new_id = cur.fetchone()[0]
        conn.commit()
        logger.info(f"Inserted discovered facility {new_id}: {facility['name']}")
        return new_id

    except Exception as e:
        conn.rollback()
        logger.error(f"Error inserting facility: {e}\n{traceback.format_exc()}")
        return None


# ─────────────────────────────────────────────────────────
# MAIN SCAN FUNCTION
# ─────────────────────────────────────────────────────────

def scan_news_sources(conn=None):
    """
    Scan all configured news sources for new facility announcements.
    Extracts metadata and inserts into discovered_facilities.

    Args:
        conn: PostgreSQL connection (if None, will attempt to get one from app context)

    Returns:
        dict with scan results: articles_scanned, facilities_found, facilities_inserted
    """
    import requests

    results = {
        'articles_scanned': 0,
        'facilities_found': 0,
        'facilities_inserted': 0,
        'errors': [],
    }

    # Get DB connection if not provided
    if conn is None:
        try:
            from main import get_read_db
            conn = get_read_db()
        except Exception as e:
            logger.error(f"Could not get DB connection: {e}")
            results['errors'].append(str(e))
            return results

    for source in NEWS_SOURCES:
        try:
            url = source.get('rss') or source.get('url')
            if not url:
                continue

            logger.info(f"Scanning {source['name']}: {url}")

            # Fetch the page/RSS
            resp = requests.get(url, timeout=30, headers={
                'User-Agent': 'DCHub-NewsExtractor/1.0 (dchub.cloud)'
            })
            if resp.status_code != 200:
                logger.warning(f"{source['name']} returned {resp.status_code}")
                continue

            content = resp.text

            # Simple extraction: find article-like blocks
            # For RSS feeds, parse XML; for HTML, use regex on <article> or <h2>/<h3> tags
            # This is a basic implementation — enhance with proper RSS/HTML parsing

            # Extract title-like strings (h2/h3 tags or RSS <title> tags)
            titles = re.findall(r'<(?:h[23]|title)[^>]*>([^<]+)</(?:h[23]|title)>', content)

            for title in titles:
                title = title.strip()
                if not title or len(title) < 20:
                    continue

                results['articles_scanned'] += 1

                # Use title as both title and body for now
                # TODO: Fetch individual article pages for full body text
                facility = extract_facility_from_article(title, content, url, source['name'])
                if facility:
                    results['facilities_found'] += 1
                    new_id = insert_discovered_facility(conn, facility)
                    if new_id:
                        results['facilities_inserted'] += 1

        except Exception as e:
            error_msg = f"Error scanning {source['name']}: {e}"
            logger.error(error_msg)
            results['errors'].append(error_msg)

    logger.info(f"News scan complete: {results}")
    return results


# ─────────────────────────────────────────────────────────
# CLI ENTRY POINT (for manual runs)
# ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print("DC Hub News Facility Extractor")
    print("=" * 40)

    # Test extraction on a sample headline
    test_title = "AVAIO Digital Announces New Large-Scale AI-Ready Data Center and Power Campus in Little Rock, Arkansas"
    test_body = """AVAIO Digital Partners announced today a major new data center hub near Little Rock 
    in Pulaski County, Arkansas. The campus will be built out in multiple phases with an initial 
    $6 billion combined investment. AVAIO is currently contracted with Entergy Arkansas for 150 MW 
    of power. Construction of the first phase is expected to start in Q1 2026. The 760-acre campus 
    was chosen for its robust connectivity and rapid power delivery."""

    result = extract_facility_from_article(test_title, test_body, 'https://example.com/test', 'Test')
    if result:
        print("\nExtracted facility:")
        for k, v in result.items():
            if v is not None:
                print(f"  {k}: {v}")
    else:
        print("No facility detected (check patterns)")
