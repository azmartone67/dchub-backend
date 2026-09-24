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
import html
import json
import hashlib
import logging
import traceback
from datetime import datetime
from utc_clock import utc_now

try:
    from dchub_heartbeat import with_heartbeat
except ImportError:  # heartbeat client unavailable: the scan still runs, unreported
    def with_heartbeat(*_args, **_kwargs):
        return lambda fn: fn

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
        # `\d[\d,]*`, not `[\d,]+`: the restored `?` makes these match again,
        # and `[\d,]+` alone accepts a bare comma (", SF") -> int('') raises.
        r'(\d[\d,]*)\s*(?:square feet|sq\.?\s*ft|SF)',
        r'(\d[\d,]*)\s*(?:sqft|square-foot)',
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
    # The abbreviations are case-sensitive (`(?-i:...)`): the search below runs
    # re.IGNORECASE, and a bare `\bU\.?S\.?A?\b` would read the word "us" in
    # any quote as the United States — US is checked first, so it would win.
    country_patterns = {
        'US': [r'(?-i:\bU\.?S\.?A?\b)', r'\bUnited States\b', r'\bAmerica\b'],
        'IE': [r'\bIreland\b'],
        'ES': [r'\bSpain\b', r'\bMadrid\b'],
        'GB': [r'\bUnited Kingdom\b', r'(?-i:\bU\.?K\.?\b)', r'\bEngland\b', r'\bLondon\b'],
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
        'discovered_at': utc_now().strftime('%Y-%m-%d'),
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


# ────────────────────────────────────────────────────────
# ARTICLES — what one fetched feed or page is split into
# ────────────────────────────────────────────────────────
#
# r-news-scan-per-article (2026-09-13): scan_news_sources handed every
# candidate the FEED's URL as its source_url and the whole fetched page as its
# body. insert_discovered_facility dedups on source_url, so once a feed had one
# row every later candidate from it was dropped at DEBUG: one row per feed,
# ever (dchub-worker, 2026-09-13 07:01 UTC: 24 found, 0 inserted, 18 refused by
# the write gate, 6 dropped without a line). And a page-wide body read power_mw
# / investment / state off whichever article mentioned one, and made every
# title on a page "found" once anything on it said "under construction".

_FEED_ITEM_RE = re.compile(r'<item\b[^>]*>(.*?)</item\s*>', re.I | re.S)
_PAGE_HEADING_RE = re.compile(r'<(?:h[23]|title)[^>]*>([^<]+)</(?:h[23]|title)>')


def _feed_text(fragment):
    """Plain text of a feed field: CDATA unwrapped, entities decoded, tags
    dropped. Decoded on both sides of the tag strip, because feeds escape the
    HTML inside <description> (DCD sends `&lt;p&gt;`)."""
    text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', fragment or '', flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', html.unescape(text))
    return ' '.join(html.unescape(text).split())


def _feed_field(item, tag):
    m = re.search(r'<%s(?:\s[^>]*)?>(.*?)</%s\s*>' % (tag, tag), item, re.I | re.S)
    return _feed_text(m.group(1)) if m else ''


def _article_url(feed_url, title, link='', guid=''):
    """The source_url of ONE article: the key a re-scan must land on again.

    The item's <link>, else its <guid> when that is a URL. With neither, the
    feed URL plus a stable hash of the title as a fragment, so the column still
    holds a URL and the same article read tomorrow gets the same key."""
    for candidate in (link, guid):
        if candidate.startswith(('http://', 'https://')):
            return candidate
    digest = hashlib.sha256(title.encode('utf-8')).hexdigest()
    return f'{feed_url}#article-{digest[:16]}'


def _feed_articles(content, feed_url):
    """Yield (title, body, article_url) for each article in a fetched feed.

    An RSS feed yields one per <item>, from that item's own title, description
    and link. A page with no <item> keeps the heading scrape, where a heading
    is all the text an article has, so its body is empty rather than the page."""
    items = _FEED_ITEM_RE.findall(content)
    for item in items:
        title = _feed_field(item, 'title')
        body = ' '.join(filter(None, (_feed_field(item, 'description'),
                                      _feed_field(item, 'content:encoded'))))
        yield title, body, _article_url(feed_url, title,
                                        _feed_field(item, 'link'),
                                        _feed_field(item, 'guid'))
    if not items:
        for heading in _PAGE_HEADING_RE.findall(content):
            title = _feed_text(heading)
            yield title, '', _article_url(feed_url, title)


# ─────────────────────────────────────────────────────────
# DATABASE INSERT HELPER
# ─────────────────────────────────────────────────────────

def _facility_already_staged(conn, cur, facility) -> bool:
    """True when discovered_facilities already holds THIS facility.

    Keyed on the canonical slug, because the slug is what decides whether two
    rows are one page: it hashes provider|name, so rows composing the same slug
    share a URL whatever else differs. Two spellings of the question, because a
    row inserted moments ago has no stored canonical_slug yet (the freeze
    backfills it later) — so a run's own siblings are only visible through
    provider+name, and rows frozen long ago are only visible through the stored
    column.

    ★ FAILS OPEN. A probe that cannot run — an older schema with no
    canonical_slug column, a lost connection — must never stop ingestion, so
    anything unexpected here answers "not staged" and the insert proceeds, the
    behaviour that shipped before this guard existed. The failed probe poisons
    the transaction, so it is rolled back and the caller's cursor is re-armed.
    """
    try:
        from routes.facility_slug_freeze import build_canonical_slug
        slug = build_canonical_slug(facility.get('provider'),
                                    facility.get('name'))
    except Exception:
        return False
    if not slug:
        # A name that folds to nothing composes no URL, so there is no page to
        # collide with; the name-sanity gate above owns that rejection.
        return False
    try:
        cur.execute(
            "SELECT id FROM discovered_facilities "
            " WHERE canonical_slug = %s "
            "    OR (LOWER(TRIM(COALESCE(provider, ''))) "
            "          = LOWER(TRIM(COALESCE(%s, ''))) "
            "        AND LOWER(TRIM(COALESCE(name, ''))) "
            "          = LOWER(TRIM(COALESCE(%s, '')))) "
            " LIMIT 1",
            (slug, facility.get('provider'), facility.get('name')))
        row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    if not row:
        return False
    logger.info("Skipping facility already staged as %s (row %s): %r",
                slug, row[0], (facility.get('name') or '')[:120])
    return True


def insert_discovered_facility(conn, facility, failures=None):
    """
    Insert an extracted facility into discovered_facilities.
    Returns the new row ID, or None if duplicate/error.

    None means BOTH "skipped" (rejected name, already known) and "the write
    failed", so a caller that counts outcomes passes `failures`, a list: a
    failed write appends its error there, a skip appends nothing.
    """
    # r-headline-reject (2026-08-09): the choke point every news path funnels
    # through — this module's own scan, routes/news_entity_extraction's NER
    # promotion, and routes/competitor_gap_crawler. Until now `name` was the
    # raw article title (see extract_facility_from_article: "Use article title
    # as facility name"), so headlines like "Stack breaks ground on second
    # Tokyo data center" and bare NER spans like "Copilot"/"FERC" became live
    # indexable /facilities/<slug> pages. Reject at the WRITE, not in the
    # sitemap: the slugs carry no structural marker, and slugs are frozen so
    # a bad row can never be renamed away. See util/facility_name_sanity.py.
    try:
        from util.facility_name_sanity import facility_reject_reason
        _reject = facility_reject_reason(facility)
    except Exception:  # predicate must never break ingestion
        _reject = None
    if _reject:
        logger.info(
            "Rejected news-derived facility candidate (%s): %r",
            _reject, (facility.get('name') or '')[:120])
        return None

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

        # r-one-url-many-rows (2026-09-12): source_url was the ONLY write-time
        # dedup, and it is narrower than the identity it was protecting. One
        # building reachable at five URLs in a single competitor-gap sweep
        # became five rows at consecutive ids, all wearing ONE canonical_slug,
        # none pointing at another, none flagged — measured live on
        # `south-reach-networks-fort-pierce-d6d47cf4` (ids 12901642-46), where
        # the 5-result free search preview returned that one building five
        # times. The name+city probe that should have caught it
        # (competitor_gap_crawler._is_existing) runs BEFORE any of the run's own
        # inserts, so siblings in one sweep cannot see each other.
        #
        # The slug IS the identity: it hashes provider|name, so two rows
        # composing the same slug are ONE /facilities/<slug> page. The second
        # row can never have a page of its own — it can only inflate every
        # count and every search result that reads rows.
        if _facility_already_staged(conn, cur, facility):
            return None

        cur.execute("""
            INSERT INTO discovered_facilities
                (name, provider, city, state, country, latitude, longitude,
                 power_mw, sqft, status, source, source_url,
                 confidence_score, discovered_at, notes,
                 investment_usd, acreage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
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
        if failures is not None:
            failures.append(f"{type(e).__name__}: {e}")
        conn.rollback()
        logger.error(f"Error inserting facility: {e}\n{traceback.format_exc()}")
        return None


# ─────────────────────────────────────────────────────────
# MAIN SCAN FUNCTION
# ─────────────────────────────────────────────────────────

# The source-registry heartbeat fires from this scan: rows = facilities inserted,
# and a failure when no connection was obtained, no source could be read, or a
# write failed.
@with_heartbeat("backend-news-facility-extractor", rows_key="facilities_inserted")
def scan_news_sources(conn=None):
    """
    Scan all configured news sources for new facility announcements.
    Extracts metadata and inserts into discovered_facilities.

    Args:
        conn: PostgreSQL connection (if None, will attempt to get one from app context)

    Returns:
        dict with scan results: articles_scanned, facilities_found,
        facilities_inserted, facilities_insert_failed, sources_read, errors, and
        success — False when no connection was obtained, no source answered, or
        a write failed, so a scan that could not look (or could not write) is
        not reported as a scan that found nothing.
    """
    import requests

    results = {
        'articles_scanned': 0,
        'facilities_found': 0,
        'facilities_inserted': 0,
        # Counted apart from "not inserted": insert_discovered_facility returns
        # None for a skip AND for a failed write, so without this a run whose
        # every INSERT was refused reported 0 new facilities and nothing else.
        'facilities_insert_failed': 0,
        'sources_read': 0,
        'errors': [],
        'success': False,
    }

    # This scan WRITES. With no connection given it takes one from the primary
    # pool (main.get_db, as the other insert_discovered_facility callers do),
    # never main.get_read_db, which hands out the read-replica pool whenever
    # DATABASE_READ_URL / NEON_REPLICA_URL is set; dchub-worker, where the
    # scheduler calls this, sets one. A connection the scan took it gives
    # back; a caller's stays open.
    owned = conn is None
    if owned:
        try:
            from main import get_db
            conn = get_db()
        except Exception as e:
            logger.error(f"Could not get DB connection: {e}")
            results['errors'].append(str(e))
            results['success'] = False
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
            results['sources_read'] += 1

            content = resp.text

            # One candidate per ARTICLE: its own link is its source_url (the key
            # insert_discovered_facility dedups on) and its own text is what it
            # is judged and measured on; never the feed's URL or the whole page.
            for title, body, article_url in _feed_articles(content, url):
                if not title or len(title) < 20:
                    continue

                results['articles_scanned'] += 1

                # TODO: Fetch individual article pages for full body text
                facility = extract_facility_from_article(
                    title, body, article_url, source['name'])
                if facility:
                    results['facilities_found'] += 1
                    failed = []
                    new_id = insert_discovered_facility(conn, facility, failures=failed)
                    if new_id:
                        results['facilities_inserted'] += 1
                    elif failed:
                        results['facilities_insert_failed'] += 1
                        if results['facilities_insert_failed'] == 1:
                            # One message, not one per candidate: a refused
                            # write refuses every insert the same way.
                            results['errors'].append(
                                f"discovered_facilities write failed: {failed[0][:200]}")

        except Exception as e:
            error_msg = f"Error scanning {source['name']}: {e}"
            logger.error(error_msg)
            results['errors'].append(error_msg)

    if owned:
        # Each source's work sits inside its own try, so no Exception from the
        # loop can skip this.
        try:
            conn.close()
        except Exception:
            pass

    results['success'] = (results['sources_read'] > 0
                          and results['facilities_insert_failed'] == 0)
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
