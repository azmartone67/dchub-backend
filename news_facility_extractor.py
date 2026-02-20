"""
DC Hub - News-to-Facility Extraction Engine
=============================================
Scans news articles for data center construction/expansion announcements
and automatically extracts facility data into discovered_facilities table.

Runs periodically after each news sync cycle.
"""

import sqlite3
import re
import json
import hashlib
import os
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'dc_nexus.db')

CONSTRUCTION_KEYWORDS = [
    'data center', 'datacenter', 'data centre',
    'hyperscale', 'colocation', 'colo facility',
    'cloud campus', 'ai campus', 'hpc facility',
    'server farm', 'compute campus'
]

ACTION_KEYWORDS = [
    'under construction', 'breaks ground', 'broke ground', 'groundbreaking',
    'construction begins', 'construction started', 'begins construction',
    'new facility', 'new campus', 'new data center', 'new datacenter',
    'plans to build', 'will build', 'announces plans', 'approved',
    'expansion', 'expands', 'expanding', 'new site',
    'megawatt', ' mw ', ' gw ', 'gigawatt',
    'under development', 'in development', 'proposed',
    'secures land', 'acquires land', 'purchases land', 'buys land',
    'signs lease', 'power agreement', 'grid connection',
    'construction permit', 'building permit', 'zoning approval',
    'phase 1', 'phase 2', 'phase one', 'phase two',
    'energization', 'energized', 'comes online', 'go live',
    'billion investment', 'million investment', '$1b', '$2b', '$5b', '$10b',
    'announces', 'unveiled', 'reveals plans', 'launches'
]

KNOWN_OPERATORS = [
    'equinix', 'digital realty', 'cyrusone', 'qts', 'coresite',
    'vantage', 'stack infrastructure', 'compass', 'flexential',
    'edgeconnex', 'aligned', 't5', 'prime', 'switch', 'sabey',
    'serverfarm', 'cologix', 'databank', 'tierpoint', 'zayo',
    'ntt', 'chindata', 'gds', 'airtrunk', 'stc', 'gulf data hub',
    'amazon', 'aws', 'google', 'microsoft', 'meta', 'facebook',
    'oracle', 'apple', 'ibm', 'alibaba', 'tencent', 'bytedance',
    'iren', 'terawulf', 'applied digital', 'cipher mining',
    'core scientific', 'hut 8', 'bitdeer', 'crusoe', 'lambda',
    'fluidstack', 'coreweave', 'vultr', 'novva',
    'anthropic', 'openai', 'stargate', 'xai',
    'princeton digital', 'yondr', 'datum', 'virtus',
    'cloudflare', 'akamai', 'fastly', 'lumen',
    'vnet', 'bridge data centres', 'st telemedia',
    'supernap', 'las vegas global', 'landmark',
    'stream data centers', 'skybox', 'nautilus',
    'echelon', 'scala data centers', 'ascenty'
]

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
    'wisconsin': 'WI', 'wyoming': 'WY'
}

STATE_ABBREVS = {v: v for v in US_STATES.values()}


def extract_power_mw(text):
    text_lower = text.lower()
    gw_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:gw|gigawatt)', text_lower)
    if gw_match:
        return float(gw_match.group(1)) * 1000

    mw_match = re.search(r'(\d+(?:,\d+)?(?:\.\d+)?)\s*(?:mw|megawatt)', text_lower)
    if mw_match:
        val = mw_match.group(1).replace(',', '')
        return float(val)
    return None


def extract_investment(text):
    patterns = [
        r'\$(\d+(?:\.\d+)?)\s*billion',
        r'\$(\d+(?:\.\d+)?)\s*B\b',
        r'\$(\d+(?:\.\d+)?)\s*million',
        r'\$(\d+(?:\.\d+)?)\s*M\b',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if 'million' in p.lower() or p.endswith("M\\b"):
                return f"${val}M"
            return f"${val}B"
    return None


def extract_operator(text):
    text_lower = text.lower()
    for op in KNOWN_OPERATORS:
        if op.lower() in text_lower:
            return op.title()
    return None


def extract_location(text):
    text_lower = text.lower()
    for state_name, abbrev in US_STATES.items():
        if state_name in text_lower:
            city = extract_city_near_state(text, state_name)
            return city, abbrev, 'US'

    for abbrev in STATE_ABBREVS:
        pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),?\s+' + abbrev + r'\b'
        m = re.search(pattern, text)
        if m:
            return m.group(1), abbrev, 'US'

    international = {
        'london': ('London', '', 'GB'), 'frankfurt': ('Frankfurt', '', 'DE'),
        'amsterdam': ('Amsterdam', '', 'NL'), 'paris': ('Paris', '', 'FR'),
        'singapore': ('Singapore', '', 'SG'), 'tokyo': ('Tokyo', '', 'JP'),
        'sydney': ('Sydney', 'NSW', 'AU'), 'mumbai': ('Mumbai', '', 'IN'),
        'dubai': ('Dubai', '', 'AE'), 'hong kong': ('Hong Kong', '', 'HK'),
        'toronto': ('Toronto', 'ON', 'CA'), 'jakarta': ('Jakarta', '', 'ID'),
        'seoul': ('Seoul', '', 'KR'), 'johor': ('Johor Bahru', '', 'MY'),
        'sao paulo': ('Sao Paulo', '', 'BR'), 'santiago': ('Santiago', '', 'CL'),
    }
    for city_key, loc in international.items():
        if city_key in text_lower:
            return loc

    return None, None, None


STOP_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'its', 'has', 'have',
    'will', 'new', 'one', 'two', 'three', 'four', 'five', 'six', 'data', 'center',
    'campus', 'project', 'facility', 'site', 'build', 'plans', 'announces',
    'construction', 'expansion', 'development', 'investment', 'billion', 'million',
    'power', 'energy', 'electric', 'grid', 'win', 'near', 'report', 'says',
    'could', 'would', 'should', 'may', 'can', 'first', 'second', 'third',
    'biggest', 'largest', 'major', 'massive', 'huge', 'giant', 'mega',
}

def extract_city_near_state(text, state_name):
    idx = text.lower().find(state_name)
    if idx < 0:
        return ''
    before = text[max(0, idx-60):idx]
    words = before.split()
    city_parts = []
    for w in reversed(words):
        clean = w.strip('.,;:()').strip()
        if clean and clean[0].isupper() and len(clean) > 2 and clean.lower() not in STOP_WORDS:
            city_parts.insert(0, clean)
            if len(city_parts) >= 2:
                break
        elif city_parts:
            break
    return ' '.join(city_parts) if city_parts else ''


def extract_status(text):
    text_lower = text.lower()
    status_map = [
        (['under construction', 'construction begins', 'broke ground', 'breaks ground',
          'groundbreaking', 'begins construction', 'construction started'], 'Under Construction'),
        (['comes online', 'go live', 'energized', 'operational', 'opened', 'opens'], 'active'),
        (['plans to build', 'proposed', 'announces plans', 'planning', 'in development',
          'will build', 'announces', 'unveiled', 'reveals plans'], 'Planning'),
        (['approved', 'permitted', 'zoning approval', 'building permit'], 'approved'),
        (['announced'], 'announced'),
    ]
    for keywords, status in status_map:
        for kw in keywords:
            if kw in text_lower:
                return status
    return 'announced'


def score_article(title, summary=''):
    combined = (title + ' ' + (summary or '')).lower()
    score = 0

    dc_match = any(kw in combined for kw in CONSTRUCTION_KEYWORDS)
    if not dc_match:
        return 0

    score += 10

    action_count = sum(1 for kw in ACTION_KEYWORDS if kw in combined)
    score += action_count * 5

    if extract_operator(combined):
        score += 15

    if extract_power_mw(combined):
        score += 20

    if extract_investment(combined):
        score += 15

    _, state, country = extract_location(title + ' ' + (summary or ''))
    if state or country:
        score += 10

    return score


def process_news_for_facilities():
    """Scan recent news articles and extract potential new facility announcements"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""
            SELECT id, title, summary, url, published_at, source
            FROM news
            WHERE id NOT IN (
                SELECT COALESCE(source_id, '') FROM discovered_facilities WHERE source = 'news_extractor'
            )
            ORDER BY published_at DESC
            LIMIT 500
        """)
        articles = c.fetchall()

        extracted = 0
        for article in articles:
            title = article['title'] or ''
            summary = article['summary'] or ''
            combined = title + ' ' + summary

            relevance = score_article(title, summary)
            if relevance < 30:
                continue

            operator = extract_operator(combined)
            power_mw = extract_power_mw(combined)
            investment = extract_investment(combined)
            city, state, country = extract_location(combined)
            status = extract_status(combined)

            if not operator and not power_mw:
                continue

            if not city and not state and not country:
                continue

            facility_name = f"{operator or 'Unknown'} {city or ''} {state or ''} Data Center".strip()
            source_id = str(article['id'])

            raw_data = json.dumps({
                'article_title': title,
                'article_url': article['url'],
                'article_source': article['source'],
                'published_date': article['published_at'],
                'investment': investment,
                'relevance_score': relevance,
                'extraction_method': 'news_facility_extractor'
            })

            try:
                c.execute("""
                    INSERT OR IGNORE INTO discovered_facilities
                    (source, source_id, name, provider, city, state, country,
                     power_mw, status, source_url, raw_data, discovered_at, confidence_score)
                    VALUES ('news_extractor', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    source_id, facility_name, operator or 'Unknown',
                    city or '', state or '', country or 'US',
                    power_mw or 0, status,
                    article['url'] or '', raw_data,
                    datetime.now().isoformat(),
                    min(relevance / 100, 0.95)
                ))
                if c.rowcount > 0:
                    extracted += 1
            except Exception:
                pass

        conn.commit()
        conn.close()

        if extracted > 0:
            print(f"✅ News Extractor: Found {extracted} potential new DC projects from {len(articles)} articles")
        return extracted

    except Exception as e:
        print(f"⚠️ News Extractor error: {e}")
        return 0


def promote_high_confidence_discoveries():
    """Move high-confidence discovered facilities into the main facilities table"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""
            SELECT * FROM discovered_facilities
            WHERE confidence_score >= 0.6
            AND merged_at IS NULL
            AND is_duplicate = 0
            AND source = 'news_extractor'
            AND provider != 'Unknown'
            AND (city != '' OR state != '')
        """)
        candidates = c.fetchall()

        promoted = 0
        for df in candidates:
            existing = c.execute("""
                SELECT id FROM facilities 
                WHERE (LOWER(name) = LOWER(?) OR 
                       (LOWER(provider) = LOWER(?) AND LOWER(city) = LOWER(?) AND LOWER(state) = LOWER(?)))
            """, (df['name'], df['provider'] or '', df['city'] or '', df['state'] or '')).fetchone()

            if existing:
                c.execute("UPDATE discovered_facilities SET is_duplicate = 1 WHERE id = ?", (df['id'],))
                continue

            fac_id = 'news_' + hashlib.sha256(df['name'].encode()).hexdigest()[:12]
            try:
                c.execute("""
                    INSERT OR IGNORE INTO facilities
                    (id, name, provider, city, state, country, power_mw, status,
                     source, source_id, source_url, raw_data, first_seen, last_updated, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'news_pipeline', ?, ?, ?, ?, ?, ?)
                """, (
                    fac_id, df['name'], df['provider'], df['city'], df['state'],
                    df['country'] or 'US', df['power_mw'] or 0, df['status'] or 'announced',
                    str(df['id']), df['source_url'] or '', df['raw_data'] or '{}',
                    datetime.now().isoformat(), datetime.now().isoformat(),
                    df['confidence_score'] or 0.6
                ))
                if c.rowcount > 0:
                    c.execute("UPDATE discovered_facilities SET merged_at = ?, merged_facility_id = ? WHERE id = ?",
                              (datetime.now().isoformat(), fac_id, df['id']))
                    promoted += 1
            except Exception:
                pass

        conn.commit()
        conn.close()

        if promoted > 0:
            print(f"✅ News Extractor: Promoted {promoted} discoveries to main facilities table")
        return promoted

    except Exception as e:
        print(f"⚠️ News Extractor promotion error: {e}")
        return 0


def run_extraction():
    """Full extraction cycle: scan news → extract → promote"""
    extracted = process_news_for_facilities()
    promoted = promote_high_confidence_discoveries()
    return extracted, promoted


if __name__ == '__main__':
    extracted, promoted = run_extraction()
    print(f"\nResults: {extracted} extracted, {promoted} promoted to facilities")
