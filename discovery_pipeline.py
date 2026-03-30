import os
import re
import json
import hashlib
import time
from datetime import datetime, timedelta
from functools import wraps
from db_utils import get_db

DB_PATH = 'dc_nexus.db'

KNOWN_OPERATORS = [
    "Equinix", "Digital Realty", "NTT", "CyrusOne", "QTS", "Vantage",
    "CoreSite", "DataBank", "TierPoint", "Flexential", "Aligned",
    "CloudHQ", "Compass Datacenters", "DataGryd", "EdgeConneX",
    "H5 Data Centers", "Involta", "Lumen", "PointOne", "Prime Data Centers",
    "RagingWire", "Sabey", "Stack Infrastructure", "Stream Data Centers",
    "Switch", "T5 Data Centers", "US Signal", "Vapor IO", "Vertiv",
    "COPT", "American Tower", "Landmark Infrastructure",
    "AWS", "Amazon Web Services", "Microsoft", "Azure", "Google", "Google Cloud",
    "Meta", "Facebook", "Apple", "Oracle", "IBM",
    "Alibaba Cloud", "Tencent", "Baidu", "Huawei",
    "ChinData", "GDS Holdings", "VNET Group", "SUNeVision",
    "AirTrunk", "NEXTDC", "Canberra Data Centres", "Macquarie Data Centres",
    "Yondr", "AtlasEdge", "Ark Data Centres", "Echelon Data Centres",
    "Interxion", "Global Switch", "Cyxtera", "Cologix", "zColo",
    "Iron Mountain", "Evoque Data Center Solutions",
    "Scala Data Centers", "Ascenty", "ODATA", "Elea Digital",
    "Africa Data Centres", "Raxio", "Teraco",
    "Schneider Electric", "ABB", "Eaton", "Caterpillar",
    "Nautilus Data Technologies", "Lancium", "Crusoe Energy",
    "Applied Digital", "Core Scientific", "Riot Platforms",
    "CleanArc", "Novva Data Centers", "Chirisa Technology Parks",
    "Lincoln Rackhouse", "DC BLOX", "365 Data Centers",
    "Serverfarm", "PhoenixNAP", "HostDime", "Hivelocity",
    "DataSite", "Data Foundry", "FirstLight", "Green Mountain",
    "Baselayer", "Skybox Datacenters", "Evocative Data Centers",
    "IPG", "Kao Data", "DigiPlex", "Conapto",
    "MainOne", "Rack Centre", "iColo", "Liquid Intelligent Technologies",
    "Bridge Data Centres", "Telehouse", "Keppel Data Centres", "ST Telemedia",
    "PowerHouse Data Centers", "Nautilus", "EdgePresence",
    "AVAIO Digital", "AVAIO Digital Partners",
    "Yondr Group", "EdgeCore Digital Infrastructure", "EdgeCore",
    "Novva", "CloudHQ", "Prime Data Centers",
    "Scala Data Centers", "PowerHouse", "Nautilus Data Technologies",
    "Lincoln Rackhouse", "Corscale", "Stargate",
    "DataHouse", "Vantage Data Centers",
    "JLL", "CBRE", "Cushman & Wakefield",
    "DigitalBridge", "Brookfield", "KKR", "Blackstone", "GI Partners",
    "BlackRock", "Stonepeak", "IPI Partners", "Prologis",
    "Corscale", "Stargate", "xScale", "Cyrus Capital",
    "Lumen Technologies", "Zayo", "Crown Castle",
    "CoreWeave", "Lambda", "Cerebras", "Together AI",
    "Fidelity", "GLP", "ESR", "CapitaLand",
    "Princeton Digital Group", "SpaceDC", "Colt DCS",
    "Virtus Data Centres", "Vantage Data Centers", "Digital Edge",
    "Compass", "QTS Realty", "Digital Realty Trust", "DLR",
    "Iceotope", "Submer", "LiquidCool Solutions",
    "MCF Energy", "REV Renewables", "AES Corporation",
    "Renew Power", "CleanMax", "Hero Future Energies",
    "Microsoft Azure", "Google Cloud Platform", "GCP",
    "NTT Global Data Centers", "NTT Ltd",
    "China Mobile", "China Telecom", "China Unicom",
    "Ooredoo", "Etisalat", "STC", "Gulf Data Hub",
    "Khazna Data Centers", "Moro Hub", "G42",
]

HEADLINE_KEYWORDS = [
    'breaks ground', 'broke ground', 'groundbreaking',
    'under construction', 'construction begins', 'construction started',
    'new campus', 'new data center', 'new facility', 'new site',
    'data center planned', 'plans to build', 'plans data center',
    'mw facility', 'megawatt facility', 'gw facility',
    'hyperscale', 'hyperscaler',
    'expansion', 'expands', 'expanding',
    'announces site', 'announced site', 'site selection',
    'secures land', 'acquired land', 'land purchase', 'land deal',
    'power purchase agreement', 'ppa', 'power deal',
    'building permit', 'construction permit', 'permit approved',
    'certificate of occupancy', 'goes live', 'now operational',
    'data center development', 'campus development',
    'phase 1', 'phase 2', 'phase 3', 'phase one', 'phase two',
    'coming online', 'energized', 'delivered', 'leased up',
    'data center acquisition', 'acquires data center',
    'joint venture', 'data center joint venture',
    'build-to-suit', 'powered shell',
    'colocation', 'colo campus',
]

BODY_KEYWORDS = [
    'megawatt', 'megawatts', ' mw ', 'gigawatt', 'gigawatts', ' gw ',
    'it capacity', 'critical load', 'total capacity',
    'acre site', 'acres', 'square feet', 'sq ft',
    'fiber-connected', 'network-dense', 'carrier-neutral',
    'cooling', 'liquid cooling', 'immersion cooling',
    'power substation', 'utility power', 'redundant power',
    'tier iii', 'tier iv', 'tier 3', 'tier 4',
    'data hall', 'server hall', 'white space',
]

INDUSTRY_SOURCES = [
    'data center dynamics', 'dcd', 'datacenter dynamics',
    'data center knowledge', 'dck', 'datacenter knowledge',
    'datacenter frontier', 'data center frontier', 'dcf',
    'broadgroup', 'inside towers', 'bisnow',
    'hpcwire', 'datacenter hawk', 'datacenterhawk',
    'capacity media', 'light reading',
    'data centre magazine', 'dc byte',
]

GENERAL_TECH_SOURCES = [
    'techcrunch', 'the register', 'ars technica', 'zdnet',
    'the verge', 'wired', 'siliconangle', 'venturebeat',
    'the new stack', 'network world', 'serverwatch',
]

SKIP_PATTERNS = [
    r'stock price', r'share price', r'earnings report',
    r'q[1-4] results', r'quarterly results', r'annual report',
    r'analyst estimate', r'analyst says', r'analysts? predict',
    r'dividend', r'eps ', r'revenue guidance',
    r'opinion:', r'editorial:', r'commentary:',
    r'market cap', r'valuation', r'price target',
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
    'wisconsin': 'WI', 'wyoming': 'WY',
}
STATE_ABBREVS = {v: k.title() for k, v in US_STATES.items()}

STATUS_MAP = {
    'planned': ['planned', 'planning', 'proposed', 'plans to build', 'site selected', 'site selection'],
    'permitted': ['permitted', 'permit approved', 'building permit', 'received approval', 'green light', 'zoning approved'],
    'under_construction': ['under construction', 'broke ground', 'breaks ground', 'groundbreaking', 'construction begin', 'construction start', 'building a'],
    'operational': ['operational', 'now open', 'goes live', 'completed', 'energized', 'delivered', 'came online', 'coming online'],
    'announced': ['announced', 'will build', 'set to develop', 'data center project', 'new campus announced'],
    'expansion': ['expansion', 'expands', 'expanding', 'phase 2', 'phase 3', 'phase two', 'phase three', 'additional capacity'],
}




def init_pipeline_tables():
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_facilities (
                id SERIAL PRIMARY KEY,
                operator TEXT,
                operator_known BOOLEAN DEFAULT false,
                location_text TEXT,
                city TEXT,
                state TEXT,
                country TEXT,
                lat REAL,
                lng REAL,
                capacity_mw REAL,
                capacity_qualifier TEXT,
                status TEXT,
                timeline TEXT,
                investment_usd REAL,
                confidence_score INTEGER DEFAULT 0,
                confidence_tier TEXT,
                classification TEXT,
                source_url TEXT,
                source_name TEXT,
                source_article_title TEXT,
                source_published_date TEXT,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed BOOLEAN DEFAULT 0,
                review_action TEXT,
                reviewed_at TIMESTAMP,
                matched_facility_id TEXT,
                match_type TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS facility_sources (
                id SERIAL PRIMARY KEY,
                facility_id TEXT,
                pending_facility_id INTEGER,
                article_url TEXT,
                article_title TEXT,
                source_name TEXT,
                published_date TEXT,
                extracted_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_metrics (
                id SERIAL PRIMARY KEY,
                metric_date TEXT,
                articles_processed INTEGER DEFAULT 0,
                articles_classified INTEGER DEFAULT 0,
                facilities_extracted INTEGER DEFAULT 0,
                auto_verified INTEGER DEFAULT 0,
                pending_review INTEGER DEFAULT 0,
                discarded INTEGER DEFAULT 0,
                avg_confidence REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_confidence ON pending_facilities(confidence_score DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_reviewed ON pending_facilities(reviewed, confidence_tier)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sources_facility ON facility_sources(facility_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sources_pending ON facility_sources(pending_facility_id)")
        except:
            pass

        try:
            c.execute("SELECT facility_processed FROM announcements LIMIT 1")
        except:
            try:
                c.execute("ALTER TABLE announcements ADD COLUMN facility_processed BOOLEAN DEFAULT false")
            except:
                pass

        try:
            c.execute("SELECT facility_extracted_id FROM announcements LIMIT 1")
        except:
            try:
                c.execute("ALTER TABLE announcements ADD COLUMN facility_extracted_id INTEGER")
            except:
                pass

        conn.commit()
    finally:
        conn.close()
    print("✅ Discovery pipeline tables initialized")


def classify_article(article):
    title = (article.get('title') or '').lower()
    summary = (article.get('summary') or article.get('content') or '').lower()
    source = (article.get('source') or article.get('source_name') or '').lower()
    text = f"{title} {summary}"

    for pattern in SKIP_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return {
                'article_id': article.get('id'),
                'passed_filter': False,
                'classification': None,
                'classification_confidence': 0,
                'reason': 'skip_pattern_match'
            }

    headline_hits = sum(1 for kw in HEADLINE_KEYWORDS if kw in title)
    body_hits = sum(1 for kw in BODY_KEYWORDS if kw in text)

    has_mw = bool(re.search(r'\d+\s*(?:MW|GW|megawatt|gigawatt)', text, re.IGNORECASE))
    has_operator = any(op.lower() in text for op in KNOWN_OPERATORS[:100])

    signal_count = 0
    if headline_hits >= 1:
        signal_count += 1
    if body_hits >= 1:
        signal_count += 1
    if has_mw:
        signal_count += 1
    if has_operator:
        signal_count += 1

    if signal_count < 2:
        return {
            'article_id': article.get('id'),
            'passed_filter': False,
            'classification': None,
            'classification_confidence': 0,
            'reason': f'insufficient_signals ({signal_count}/2)'
        }

    classification = 'new_facility'
    if any(kw in text for kw in ['acquisition', 'acquires', 'acquire', 'merger', 'purchase']):
        classification = 'acquisition'
    elif any(kw in text for kw in ['expansion', 'expands', 'phase 2', 'phase 3', 'additional capacity']):
        classification = 'expansion'
    elif any(kw in text for kw in ['power purchase', 'ppa', 'power deal', 'energy contract']):
        classification = 'power_deal'

    confidence = min(30 + (headline_hits * 10) + (body_hits * 5), 95)
    if has_mw:
        confidence = min(confidence + 10, 95)
    if has_operator:
        confidence = min(confidence + 10, 95)

    is_industry = any(s in source for s in INDUSTRY_SOURCES)
    if is_industry:
        confidence = min(confidence + 15, 95)

    return {
        'article_id': article.get('id'),
        'article_url': article.get('source_url') or article.get('url'),
        'article_title': article.get('title'),
        'source_name': article.get('source') or article.get('source_name'),
        'published_date': article.get('published_date'),
        'classification': classification,
        'classification_confidence': confidence,
        'passed_filter': True,
    }


def extract_facility_data(article):
    title = article.get('title') or ''
    summary = article.get('summary') or article.get('content') or ''
    source = article.get('source') or article.get('source_name') or ''
    text = f"{title} {summary}"
    text_lower = text.lower()

    operator = None
    operator_known = False
    operator_confidence = 0

    for op in KNOWN_OPERATORS:
        if op.lower() in text_lower:
            operator = op
            operator_known = True
            operator_confidence = 95
            break

    if not operator:
        op_patterns = [
            r'(%s:by|from|operator|developer|built by|owned by|managed by)\s+([A-Z][A-Za-z\s&]+%s)(%s:\s+(%s:is|has|will|plans|broke|announced|data center|facility))',
            r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s+(?:breaks ground|broke ground|announces|plans|will build|to build|developing)',
        ]
        for pat in op_patterns:
            m = re.search(pat, text)
            if m:
                candidate = m.group(1).strip()
                if len(candidate) > 2 and len(candidate) < 50:
                    operator = candidate
                    operator_confidence = 40
                    break

    city = None
    state = None
    country = None
    location_text = None
    location_confidence = 0

    city_state_pattern = r'(?:in|at|near|outside|located in)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|[A-Z]{2})'
    m = re.search(city_state_pattern, text)
    if m:
        city = m.group(1).strip()
        raw_state = m.group(2).strip()
        if raw_state.upper() in STATE_ABBREVS:
            state = raw_state.upper()
        elif raw_state.lower() in US_STATES:
            state = US_STATES[raw_state.lower()]
        else:
            state = raw_state
        country = 'US' if state and len(state) == 2 else None
        location_text = f"{city}, {state}"
        location_confidence = 85

    if not city:
        intl_pattern = r'(?:in|at|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})'
        m = re.search(intl_pattern, text)
        if m:
            city = m.group(1).strip()
            country = m.group(2).strip()
            location_text = f"{city}, {country}"
            location_confidence = 70

    if not city:
        region_keywords = {
            'Northern Virginia': ('Ashburn', 'VA', 'US'),
            'NoVA': ('Ashburn', 'VA', 'US'),
            'Silicon Valley': ('Santa Clara', 'CA', 'US'),
            'Dallas-Fort Worth': ('Dallas', 'TX', 'US'),
            'DFW': ('Dallas', 'TX', 'US'),
            'Phoenix Metro': ('Phoenix', 'AZ', 'US'),
            'Loudoun County': ('Ashburn', 'VA', 'US'),
            'Prince William County': ('Manassas', 'VA', 'US'),
        }
        for region, (c, s, co) in region_keywords.items():
            if region.lower() in text_lower:
                city, state, country = c, s, co
                location_text = f"{city}, {state}"
                location_confidence = 75
                break

    capacity_mw = None
    capacity_qualifier = 'confirmed'
    capacity_confidence = 0

    mw_patterns = [
        (r'(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*(?:MW|megawatt)s?\s+(?:facility|campus|data center|site|project)', 'confirmed', 90),
        (r'(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*(?:MW|megawatt)s?\s+(?:of\s+)?(?:IT\s+)?capacity', 'confirmed', 85),
        (r'(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*-?\s*(?:MW|megawatt)s?', 'confirmed', 75),
        (r'(\d{1,2}(?:\.\d+)?)\s*(?:GW|gigawatt)s?', 'confirmed', 80),
    ]

    skip_mw_patterns = [
        r'(?:could reach|analyst estimates?|across the portfolio|portfolio.wide)',
    ]

    for pattern, qualifier, conf in mw_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace(',', ''))
            if 'gw' in pattern.lower() or 'gigawatt' in pattern.lower():
                val *= 1000

            if val < 1 or val > 10000:
                continue

            context_start = max(0, m.start() - 60)
            context = text[context_start:m.end() + 60].lower()

            is_risky = False
            for skip in skip_mw_patterns:
                if re.search(skip, context, re.IGNORECASE):
                    is_risky = True
                    break

            if 'up to' in context:
                qualifier = 'maximum'
                conf -= 10
            elif 'could reach' in context or 'potential' in context:
                qualifier = 'potential'
                conf -= 20
            elif is_risky:
                continue

            capacity_mw = val
            capacity_qualifier = qualifier
            capacity_confidence = conf
            break

    status = None
    status_confidence = 0
    for status_key, patterns in STATUS_MAP.items():
        for pat in patterns:
            if pat in text_lower:
                status = status_key
                status_confidence = 85
                break
        if status:
            break

    timeline = None
    timeline_patterns = [
        r'(?:expected|completion|complete|operational|online)\s+(?:in|by)?\s*(Q[1-4]\s*\d{4})',
        r'(?:expected|completion|complete|operational|online)\s+(?:in|by)?\s*(\d{4})',
        r'(?:by|in)\s+(Q[1-4]\s*\d{4})',
        r'(Q[1-4]\s*\d{4})\s+(?:completion|target|delivery)',
    ]
    for pat in timeline_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            timeline = m.group(1).strip()
            break

    investment_usd = None
    invest_patterns = [
        r'\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:billion|B)\b',
        r'\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:million|M)\b',
    ]
    for pat in invest_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace(',', ''))
            if 'billion' in pat.lower() or 'B' in pat:
                val *= 1_000_000_000
            else:
                val *= 1_000_000
            investment_usd = val
            break

    if not operator and not capacity_mw and not city:
        return None

    return {
        'operator': operator,
        'operator_known': operator_known,
        'operator_confidence': operator_confidence,
        'location_text': location_text,
        'city': city,
        'state': state,
        'country': country,
        'lat': None,
        'lng': None,
        'location_confidence': location_confidence,
        'capacity_mw': capacity_mw,
        'capacity_qualifier': capacity_qualifier,
        'capacity_confidence': capacity_confidence,
        'status': status or 'announced',
        'status_confidence': status_confidence,
        'timeline': timeline,
        'investment_usd': investment_usd,
        'source_url': article.get('source_url') or article.get('url'),
        'source_name': article.get('source') or article.get('source_name'),
        'source_article_title': article.get('title'),
        'source_published_date': article.get('published_date'),
        'extracted_at': datetime.utcnow().isoformat(),
    }


def calculate_confidence(extracted, classification=None):
    score = 0

    if extracted.get('operator'):
        if extracted.get('operator_known'):
            score += 20
        else:
            score += 10

    if extracted.get('city') and extracted.get('state'):
        score += 20
    elif extracted.get('city') or extracted.get('country'):
        score += 10

    if extracted.get('capacity_mw'):
        if extracted.get('capacity_qualifier') == 'confirmed':
            score += 15
        else:
            score += 5

    if extracted.get('status'):
        score += 10

    source = (extracted.get('source_name') or '').lower()
    if any(s in source for s in INDUSTRY_SOURCES):
        score += 15
    elif any(s in source for s in GENERAL_TECH_SOURCES):
        score += 5

    if extracted.get('lat') and extracted.get('lng'):
        score += 10

    if extracted.get('timeline'):
        score += 5

    if extracted.get('investment_usd'):
        score += 5

    score = min(score, 100)

    if score >= 80:
        tier = 'high'
    elif score >= 50:
        tier = 'medium'
    elif score >= 25:
        tier = 'low'
    else:
        tier = 'discard'

    extracted['confidence_score'] = score
    extracted['confidence_tier'] = tier
    return extracted


def find_matching_facility(extracted):
    conn = get_db()
    try:
        c = conn.cursor()

        try:
            if extracted.get('operator') and extracted.get('city'):
                op = extracted['operator']
                city = extracted['city']
                c.execute("""
                    SELECT id, name, provider, city, state, country, power_mw, status
                    FROM facilities
                    WHERE (provider LIKE %s OR name LIKE %s)
                    AND city = %s
                    LIMIT 1
                """, (f"%{op}%", f"%{op}%", city))
                match = c.fetchone()
                if match:
    finally:
        conn.close()
                return {'action': 'update', 'facility_id': match['id'], 'match_type': 'exact', 'matched': dict(match)}

        if extracted.get('operator') and extracted.get('state'):
            op = extracted['operator']
            state = extracted['state']
            c.execute("""
                SELECT id, name, provider, city, state, country, power_mw, status
                FROM facilities
                WHERE (provider LIKE %s OR name LIKE %s)
                AND state = %s
                LIMIT 5
            """, (f"%{op}%", f"%{op}%", state))
            matches = c.fetchall()
            if matches:
                conn.close()
                return {'action': 'review', 'possible_match': matches[0]['id'], 'match_type': 'operator_state', 'matched': dict(matches[0])}

        c.execute("""
            SELECT id, operator, operator AS provider, city, state, source_article_title
            FROM pending_facilities
            WHERE operator = %s AND city = %s
            AND reviewed = false
            LIMIT 1
        """, (extracted.get('operator'), extracted.get('city')))
        pending_match = c.fetchone()
        if pending_match:
            conn.close()
            return {'action': 'duplicate_pending', 'pending_id': pending_match['id'], 'match_type': 'pending'}

    except Exception as e:
        print(f"   ⚠️ Matching error: {e}")

    conn.close()
    return {'action': 'create_new', 'match_type': 'none'}


def add_to_pending(extracted, match_result):
    conn = get_db()
    try:
        c = conn.cursor()

        try:
            c.execute("""
                INSERT INTO pending_facilities (
                    operator, operator_known, location_text, city, state, country,
                    lat, lng, capacity_mw, capacity_qualifier, status, timeline,
                    investment_usd, confidence_score, confidence_tier, classification,
                    source_url, source_name, source_article_title, source_published_date,
                    extracted_at, matched_facility_id, match_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                extracted.get('operator'),
                True if extracted.get('operator_known') else False,
                extracted.get('location_text'),
                extracted.get('city'),
                extracted.get('state'),
                extracted.get('country'),
                extracted.get('lat'),
                extracted.get('lng'),
                extracted.get('capacity_mw'),
                extracted.get('capacity_qualifier'),
                extracted.get('status'),
                extracted.get('timeline'),
                extracted.get('investment_usd'),
                extracted.get('confidence_score', 0),
                extracted.get('confidence_tier', 'low'),
                extracted.get('classification'),
                extracted.get('source_url'),
                extracted.get('source_name'),
                extracted.get('source_article_title'),
                extracted.get('source_published_date'),
                extracted.get('extracted_at'),
                match_result.get('facility_id') or match_result.get('possible_match'),
                match_result.get('match_type'),
            ))
            pending_id = c.lastrowid

            c.execute("""
                INSERT INTO facility_sources (
                    pending_facility_id, article_url, article_title,
                    source_name, published_date, extracted_data
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                pending_id,
                extracted.get('source_url'),
                extracted.get('source_article_title'),
                extracted.get('source_name'),
                extracted.get('source_published_date'),
                json.dumps(extracted, default=str),
            ))

            conn.commit()
    finally:
        conn.close()
        return pending_id
    except Exception as e:
        conn.close()
        print(f"   ⚠️ Pending insert error: {e}")
        return None


def auto_create_facility(extracted):
    conn = get_db()
    try:
        c = conn.cursor()

        try:
            fac_id = f"news_{hashlib.md5((extracted.get('operator', '') + (extracted.get('city', '') or '') + str(extracted.get('capacity_mw', ''))).encode()).hexdigest()[:12]}"

            c.execute("SELECT id FROM facilities WHERE id = %s", (fac_id,))
            if c.fetchone():
    finally:
        conn.close()
            return fac_id

        c.execute("""
            INSERT INTO facilities (
                id, name, provider, city, state, country,
                latitude, longitude, power_mw, status,
                source, source_url, source_id,
                first_seen, last_updated
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            fac_id,
            f"{extracted.get('operator', 'Unknown')} - {extracted.get('city', 'Unknown')}",
            extracted.get('operator'),
            extracted.get('city'),
            extracted.get('state'),
            extracted.get('country', 'US'),
            extracted.get('lat'),
            extracted.get('lng'),
            extracted.get('capacity_mw'),
            extracted.get('status', 'announced'),
            'news_pipeline',
            extracted.get('source_url'),
            fac_id,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
        ))

        c.execute("""
            INSERT INTO facility_sources (
                facility_id, article_url, article_title,
                source_name, published_date, extracted_data
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            fac_id,
            extracted.get('source_url'),
            extracted.get('source_article_title'),
            extracted.get('source_name'),
            extracted.get('source_published_date'),
            json.dumps(extracted, default=str),
        ))

        conn.commit()
        conn.close()
        return fac_id
    except Exception as e:
        conn.close()
        print(f"   ⚠️ Auto-create error: {e}")
        return None


def run_pipeline(limit=50):
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("""
            SELECT id, title, summary, source, source_url, published_date, url
            FROM announcements
            WHERE (facility_processed IS NULL OR facility_processed = false)
            AND title IS NOT NULL
            ORDER BY discovered_at DESC
            LIMIT %s
        """, (limit,))
        articles = [dict(row) for row in c.fetchall()]
    finally:
        conn.close()

    if not articles:
        return {
            'articles_processed': 0,
            'articles_classified': 0,
            'facilities_extracted': 0,
            'auto_verified': 0,
            'pending_review': 0,
            'discarded': 0,
        }

    stats = {
        'articles_processed': len(articles),
        'articles_classified': 0,
        'facilities_extracted': 0,
        'auto_verified': 0,
        'pending_review': 0,
        'discarded': 0,
        'errors': 0,
    }

    for article in articles:
        try:
            classification = classify_article(article)

            if not classification['passed_filter']:
                mark_processed(article['id'])
                continue

            stats['articles_classified'] += 1

            extracted = extract_facility_data(article)
            if not extracted:
                mark_processed(article['id'])
                continue

            extracted['classification'] = classification.get('classification')
            stats['facilities_extracted'] += 1

            scored = calculate_confidence(extracted, classification)

            match_result = find_matching_facility(scored)

            if match_result['action'] == 'duplicate_pending':
                mark_processed(article['id'])
                continue

            if scored['confidence_score'] >= 80 and match_result['action'] == 'create_new':
                fac_id = auto_create_facility(scored)
                if fac_id:
                    stats['auto_verified'] += 1
                    mark_processed(article['id'], fac_id)
                else:
                    add_to_pending(scored, match_result)
                    stats['pending_review'] += 1
                    mark_processed(article['id'])
            elif scored['confidence_score'] >= 25:
                add_to_pending(scored, match_result)
                stats['pending_review'] += 1
                mark_processed(article['id'])
            else:
                stats['discarded'] += 1
                mark_processed(article['id'])

        except Exception as e:
            stats['errors'] += 1
            mark_processed(article['id'])

    return stats


def mark_processed(article_id, facility_id=None):
    try:
        conn = get_db()
        try:
            c = conn.cursor()
            if facility_id:
                c.execute("UPDATE announcements SET facility_processed = true, facility_extracted_id = %s WHERE id = %s", (facility_id, article_id))
            else:
                c.execute("UPDATE announcements SET facility_processed = true WHERE id = %s", (article_id,))
            conn.commit()
        finally:
            conn.close()
    except:
        pass


def get_pending_facilities(tier=None, reviewed=False, sort='confidence_desc', limit=50, offset=0):
    conn = get_db()
    try:
        c = conn.cursor()

        query = "SELECT * FROM pending_facilities WHERE reviewed = %s"
        params = [1 if reviewed else 0]

        if tier:
            query += " AND confidence_tier = %s"
            params.append(tier)

        if sort == 'confidence_desc':
            query += " ORDER BY confidence_score DESC"
        elif sort == 'newest':
            query += " ORDER BY created_at DESC"
        else:
            query += " ORDER BY confidence_score DESC"

        query += " LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        c.execute(query, params)
        results = [dict(row) for row in c.fetchall()]
    finally:
        conn.close()
    return results


def get_pending_stats():
    conn = get_db()
    try:
        c = conn.cursor()

        stats = {}
        c.execute("SELECT COUNT(*) FROM pending_facilities WHERE reviewed = false")
        stats['total_pending'] = c.fetchone()[0]

        c.execute("SELECT confidence_tier, COUNT(*) FROM pending_facilities WHERE reviewed = false GROUP BY confidence_tier")
        tier_counts = {row[0]: row[1] for row in c.fetchall()}
        stats['high'] = tier_counts.get('high', 0)
        stats['medium'] = tier_counts.get('medium', 0)
        stats['low'] = tier_counts.get('low', 0)

        c.execute("SELECT COUNT(*) FROM pending_facilities WHERE reviewed = true AND review_action = 'approved' AND DATE(reviewed_at) = DATE('now')")
        stats['approved_today'] = c.fetchone()[0]

        try:
            c.execute("SELECT COUNT(*) FROM facilities WHERE source = 'news_pipeline' AND DATE(last_updated) = DATE('now')")
            stats['auto_verified_today'] = c.fetchone()[0]
        except:
            stats['auto_verified_today'] = 0

        try:
            c.execute("SELECT COUNT(*) FROM facilities WHERE source = 'news_pipeline'")
            stats['total_auto_verified'] = c.fetchone()[0]
        except:
            stats['total_auto_verified'] = 0

        c.execute("SELECT COUNT(*) FROM pending_facilities WHERE reviewed = true")
        stats['total_reviewed'] = c.fetchone()[0]

        c.execute("""
            SELECT source_name, COUNT(*) as cnt
            FROM pending_facilities WHERE reviewed = false
            GROUP BY source_name ORDER BY cnt DESC LIMIT 10
        """)
        stats['top_sources'] = [{'name': row[0], 'count': row[1]} for row in c.fetchall()]

        c.execute("""
            SELECT COUNT(*) FROM announcements
            WHERE facility_processed = true
            AND discovered_at >= datetime('now', '-24 hours')
        """)
        stats['processed_24h'] = c.fetchone()[0]

    finally:
        conn.close()
    return stats


def approve_pending(pending_id):
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("SELECT * FROM pending_facilities WHERE id = %s", (pending_id,))
        pending = c.fetchone()
        if not pending:
    finally:
        conn.close()
        return {'success': False, 'error': 'Not found'}

    pending = dict(pending)
    fac_id = f"approved_{pending_id}_{hashlib.md5(str(pending_id).encode()).hexdigest()[:8]}"

    try:
        c.execute("""
            INSERT INTO facilities (
                id, name, provider, city, state, country,
                latitude, longitude, power_mw, status,
                source, source_url, source_id,
                first_seen, last_updated
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            fac_id,
            f"{pending.get('operator', 'Unknown')} - {pending.get('city', 'Unknown')}",
            pending.get('operator'),
            pending.get('city'),
            pending.get('state'),
            pending.get('country', 'US'),
            pending.get('lat'),
            pending.get('lng'),
            pending.get('capacity_mw'),
            pending.get('status', 'announced'),
            'news_pipeline',
            pending.get('source_url'),
            fac_id,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
        ))

        c.execute("""
            UPDATE pending_facilities
            SET reviewed = true, review_action = 'approved', reviewed_at = %s,
                matched_facility_id = %s
            WHERE id = %s
        """, (datetime.utcnow().isoformat(), fac_id, pending_id))

        c.execute("""
            UPDATE facility_sources SET facility_id = %s
            WHERE pending_facility_id = %s
        """, (fac_id, pending_id))

        conn.commit()
        conn.close()
        return {'success': True, 'facility_id': fac_id}
    except Exception as e:
        conn.close()
        return {'success': False, 'error': str(e)}


def reject_pending(pending_id, notes=None):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE pending_facilities
            SET reviewed = true, review_action = 'rejected', reviewed_at = %s, notes = %s
            WHERE id = %s
        """, (datetime.utcnow().isoformat(), notes, pending_id))
        conn.commit()
    finally:
        conn.close()
    return {'success': True}


def merge_pending(pending_id, facility_id):
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("SELECT * FROM pending_facilities WHERE id = %s", (pending_id,))
        pending = c.fetchone()
        if not pending:
    finally:
        conn.close()
        return {'success': False, 'error': 'Pending not found'}

    pending = dict(pending)

    c.execute("SELECT * FROM facilities WHERE id = %s", (facility_id,))
    facility = c.fetchone()
    if not facility:
        conn.close()
        return {'success': False, 'error': 'Facility not found'}

    facility = dict(facility)

    updates = []
    params = []
    if pending.get('capacity_mw') and (not facility.get('power_mw') or pending['capacity_mw'] > facility['power_mw']):
        updates.append("power_mw = %s")
        params.append(pending['capacity_mw'])

    if pending.get('status') and pending['status'] != facility.get('status'):
        status_order = ['announced', 'planned', 'permitted', 'under_construction', 'operational']
        old_idx = status_order.index(facility.get('status', 'announced')) if facility.get('status') in status_order else -1
        new_idx = status_order.index(pending['status']) if pending['status'] in status_order else -1
        if new_idx > old_idx:
            updates.append("status = %s")
            params.append(pending['status'])

    if updates:
        updates.append("last_updated = %s")
        params.append(datetime.utcnow().isoformat())
        params.append(facility_id)
        c.execute(f"UPDATE facilities SET {', '.join(updates)} WHERE id = %s", params)

    c.execute("""
        UPDATE pending_facilities
        SET reviewed = true, review_action = 'merged', reviewed_at = %s,
            matched_facility_id = %s
        WHERE id = %s
    """, (datetime.utcnow().isoformat(), facility_id, pending_id))

    c.execute("""
        UPDATE facility_sources SET facility_id = %s
        WHERE pending_facility_id = %s
    """, (facility_id, pending_id))

    conn.commit()
    conn.close()
    return {'success': True, 'facility_id': facility_id, 'updates_applied': len(updates) - 1}


def edit_and_approve_pending(pending_id, edits):
    conn = get_db()
    try:
        c = conn.cursor()

        allowed_fields = ['operator', 'city', 'state', 'country', 'capacity_mw', 'status', 'timeline', 'investment_usd', 'location_text', 'lat', 'lng']
        updates = []
        params = []
        for field, value in edits.items():
            if field in allowed_fields:
                updates.append(f"{field} = %s")
                params.append(value)

        if updates:
            params.append(pending_id)
            c.execute(f"UPDATE pending_facilities SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()

    finally:
        conn.close()
    return approve_pending(pending_id)


def register_pipeline_routes(app):
    init_pipeline_tables()

    def require_admin(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            api_key = request.headers.get('X-Admin-Key', '') or request.headers.get('X-API-Key', '')
            admin_key = os.environ.get('DCHUB_ADMIN_KEY', '')
            if not admin_key or api_key != admin_key:
                return jsonify({'error': 'Admin access required'}), 403
            return f(*args, **kwargs)
        return decorated

    from flask import request, jsonify, send_from_directory

    @app.route('/admin/discovery')
    def admin_discovery_page():
        return send_from_directory('static', 'admin-discovery.html')

    @app.route('/api/admin/pending-facilities', methods=['GET'])
    @require_admin
    def api_pending_facilities():
        tier = request.args.get('tier')
        reviewed = request.args.get('reviewed', 'false').lower() == 'true'
        sort = request.args.get('sort', 'confidence_desc')
        limit = min(int(request.args.get('limit', 50)), 200)
        offset = int(request.args.get('offset', 0))

        facilities = get_pending_facilities(tier=tier, reviewed=reviewed, sort=sort, limit=limit, offset=offset)
        return jsonify({'success': True, 'facilities': facilities, 'count': len(facilities)})

    @app.route('/api/admin/pending-facilities/stats', methods=['GET'])
    @require_admin
    def api_pending_stats():
        stats = get_pending_stats()
        return jsonify({'success': True, 'stats': stats})

    @app.route('/api/admin/pending-facilities/<int:pid>/approve', methods=['POST'])
    @require_admin
    def api_approve(pid):
        result = approve_pending(pid)
        return jsonify(result), 200 if result['success'] else 400

    @app.route('/api/admin/pending-facilities/<int:pid>/reject', methods=['POST'])
    @require_admin
    def api_reject(pid):
        notes = request.json.get('notes') if request.is_json else None
        result = reject_pending(pid, notes)
        return jsonify(result)

    @app.route('/api/admin/pending-facilities/<int:pid>/merge', methods=['POST'])
    @require_admin
    def api_merge(pid):
        if not request.is_json or 'facility_id' not in request.json:
            return jsonify({'error': 'facility_id required'}), 400
        result = merge_pending(pid, request.json['facility_id'])
        return jsonify(result), 200 if result['success'] else 400

    @app.route('/api/admin/pending-facilities/<int:pid>/edit', methods=['POST'])
    @require_admin
    def api_edit_approve(pid):
        if not request.is_json:
            return jsonify({'error': 'JSON body required'}), 400
        result = edit_and_approve_pending(pid, request.json)
        return jsonify(result), 200 if result['success'] else 400

    @app.route('/api/admin/pending-facilities/bulk-approve', methods=['POST'])
    @require_admin
    def api_bulk_approve():
        tier = request.args.get('tier', 'high')
        pending = get_pending_facilities(tier=tier, reviewed=False, limit=100)
        approved = 0
        for p in pending:
            result = approve_pending(p['id'])
            if result.get('success'):
                approved += 1
        return jsonify({'success': True, 'approved': approved, 'total': len(pending)})

    @app.route('/api/admin/discovery-pipeline/run', methods=['POST'])
    @require_admin
    def api_run_pipeline():
        limit = int(request.args.get('limit', 50))
        stats = run_pipeline(limit=limit)
        return jsonify({'success': True, 'stats': stats})

    @app.route('/api/admin/discovery-pipeline/stats', methods=['GET'])
    @require_admin
    def api_pipeline_stats():
        stats = get_pending_stats()
        return jsonify({'success': True, 'discovery_pipeline': stats})

    print("✅ Discovery Pipeline registered")
    print("   📋 GET  /api/admin/pending-facilities")
    print("   📊 GET  /api/admin/pending-facilities/stats")
    print("   ✅ POST /api/admin/pending-facilities/:id/approve")
    print("   ❌ POST /api/admin/pending-facilities/:id/reject")
    print("   🔗 POST /api/admin/pending-facilities/:id/merge")
    print("   ✏️  POST /api/admin/pending-facilities/:id/edit")
    print("   🚀 POST /api/admin/pending-facilities/bulk-approve")
    print("   🔄 POST /api/admin/discovery-pipeline/run")
    print("   📊 GET  /api/admin/discovery-pipeline/stats")
