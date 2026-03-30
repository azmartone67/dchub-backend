"""
DC Hub Multi-Source Facility Ingestion Script
==============================================
Pulls net-new facilities from multiple sources and inserts into Neon `facilities` table.
Deduplicates against existing records by name+city+country match.

Sources:
  1. PeeringDB API (delta sync - ~330 new)
  2. OpenStreetMap Overpass API (delta sync)
  3. Cloudscene web scrape (new source)
  4. Google News facility announcements (enhanced news extractor)

Usage:
  Set DATABASE_URL env var pointing to Neon, then:
    python3 facility_ingestion.py --source peeringdb
    python3 facility_ingestion.py --source osm
    python3 facility_ingestion.py --source all
    python3 facility_ingestion.py --source all --dry-run
"""

import os
import sys
import json
import time
import hashlib
import argparse
import urllib.request
import urllib.parse
from datetime import datetime

# --- Database connection ---

def get_db():
    import psycopg2
    url = os.environ.get('DATABASE_URL')
    if not url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)
    return psycopg2.connect(url)


def generate_facility_id(name, provider, city='', country=''):
    """Generate deterministic text ID matching DC Hub convention"""
    slug = f"{provider}-{name}-{city}-{country}".lower().strip()
    slug = slug.replace(' ', '-').replace('/', '-').replace(',', '')
    # Remove consecutive dashes
    while '--' in slug:
        slug = slug.replace('--', '-')
    hash_suffix = hashlib.md5(slug.encode()).hexdigest()[:8]
    # Truncate slug to reasonable length
    slug_part = slug[:80].rstrip('-')
    return f"{slug_part}-{hash_suffix}"


def get_existing_keys(conn):
    """Get set of (lower_name, lower_city, lower_country) for dedup"""
    cur = conn.cursor()
    cur.execute("""
        SELECT LOWER(TRIM(COALESCE(name,''))), 
               LOWER(TRIM(COALESCE(city,''))), 
               LOWER(TRIM(COALESCE(country,'')))
        FROM facilities
    """)
    keys = set()
    for row in cur.fetchall():
        keys.add((row[0], row[1], row[2]))
    cur.close()
    return keys


def insert_facilities(conn, facilities, source_name, dry_run=False):
    """Insert net-new facilities into facilities table"""
    existing = get_existing_keys(conn)
    cur = conn.cursor()
    inserted = 0
    skipped = 0

    for f in facilities:
        key = (
            (f.get('name') or '').lower().strip(),
            (f.get('city') or '').lower().strip(),
            (f.get('country') or '').lower().strip()
        )

        if key in existing or not key[0]:
            skipped += 1
            continue

        fac_id = generate_facility_id(
            f.get('name', ''),
            f.get('provider', source_name),
            f.get('city', ''),
            f.get('country', '')
        )

        if dry_run:
            print(f"  [DRY RUN] Would insert: {f.get('name')} | {f.get('city')}, {f.get('country')}")
            inserted += 1
            existing.add(key)  # prevent dup within same batch
            continue

        try:
            cur.execute("""
                INSERT INTO facilities (
                    id, name, provider, address, city, state, country, region,
                    latitude, longitude, power_mw, sqft, status,
                    source, source_url, source_id, confidence,
                    first_seen, last_updated
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (id) DO NOTHING
            """, (
                fac_id,
                f.get('name'),
                f.get('provider'),
                f.get('address'),
                f.get('city'),
                f.get('state'),
                f.get('country'),
                f.get('region'),
                f.get('latitude'),
                f.get('longitude'),
                f.get('power_mw'),
                f.get('sqft'),
                f.get('status', 'active'),
                source_name,
                f.get('source_url'),
                f.get('source_id'),
                f.get('confidence', 0.7),
                datetime.now().isoformat(),
                datetime.now().isoformat()
            ))
            inserted += 1
            existing.add(key)
        except Exception as e:
            print(f"  ERROR inserting {f.get('name')}: {e}")
            conn.rollback()
            # Re-get cursor after rollback
            cur = conn.cursor()

    if not dry_run:
        conn.commit()

    cur.close()
    return inserted, skipped


# ==============================================================
# SOURCE 1: PeeringDB
# ==============================================================

def fetch_peeringdb():
    """Fetch all facilities from PeeringDB API"""
    print("\n=== PeeringDB Facility Sync ===")
    url = "https://www.peeringdb.com/api/fac?depth=0"

    print(f"  Fetching from {url}...")
    req = urllib.request.Request(url, headers={
        'User-Agent': 'DC-Hub-Ingestion/1.0 (dchub.cloud)'
    })

    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    raw_facilities = data.get('data', [])
    print(f"  PeeringDB returned {len(raw_facilities)} facilities")

    facilities = []
    for f in raw_facilities:
        if f.get('status') != 'ok':
            continue

        # Map country codes
        country = (f.get('country') or '').upper()

        # Determine region
        region = None
        if country in ('US', 'CA', 'MX'):
            region = 'North America'
        elif country in ('GB', 'DE', 'FR', 'NL', 'IE', 'SE', 'NO', 'DK', 'FI', 'CH', 'AT', 'BE', 'IT', 'ES', 'PT', 'PL', 'CZ', 'RO', 'BG', 'HR', 'HU', 'GR', 'LU', 'SK', 'SI', 'LT', 'LV', 'EE'):
            region = 'Europe'
        elif country in ('CN', 'JP', 'KR', 'SG', 'HK', 'TW', 'IN', 'AU', 'NZ', 'MY', 'TH', 'ID', 'PH', 'VN'):
            region = 'Asia Pacific'
        elif country in ('BR', 'AR', 'CL', 'CO', 'PE'):
            region = 'Latin America'
        elif country in ('AE', 'SA', 'IL', 'ZA', 'NG', 'KE', 'EG'):
            region = 'Middle East & Africa'

        facilities.append({
            'name': f.get('name'),
            'provider': f.get('org_name') or f.get('name'),
            'address': f.get('address1'),
            'city': f.get('city'),
            'state': f.get('state'),
            'country': country,
            'region': region,
            'latitude': f.get('latitude'),
            'longitude': f.get('longitude'),
            'power_mw': None,
            'sqft': None,
            'status': 'active',
            'source_url': f"https://www.peeringdb.com/fac/{f.get('id')}",
            'source_id': f"peeringdb_{f.get('id')}",
            'confidence': 0.9,
        })

    print(f"  Parsed {len(facilities)} active facilities")
    return facilities


# ==============================================================
# SOURCE 2: OpenStreetMap (Overpass API)
# ==============================================================

def fetch_osm():
    """Fetch data centers from OpenStreetMap Overpass API"""
    print("\n=== OpenStreetMap Facility Sync ===")

    # Overpass QL query for data centers worldwide
    query = """
[out:json][timeout:120];
(
  node["telecom"="data_center"];
  way["telecom"="data_center"];
  relation["telecom"="data_center"];
  node["building"="data_centre"];
  way["building"="data_centre"];
  node["building"="data_center"];
  way["building"="data_center"];
  node["man_made"="data_center"];
  way["man_made"="data_center"];
);
out center meta;
"""

    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({'data': query}).encode()

    print(f"  Querying Overpass API (this may take 30-60s)...")
    req = urllib.request.Request(url, data=data, headers={
        'User-Agent': 'DC-Hub-Ingestion/1.0 (dchub.cloud)',
        'Content-Type': 'application/x-www-form-urlencoded'
    })

    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode())

    elements = result.get('elements', [])
    print(f"  Overpass returned {len(elements)} elements")

    facilities = []
    for el in elements:
        tags = el.get('tags', {})

        # Get coordinates (center for ways/relations)
        lat = el.get('lat') or (el.get('center', {}) or {}).get('lat')
        lon = el.get('lon') or (el.get('center', {}) or {}).get('lon')

        name = (
            tags.get('name') or 
            tags.get('operator', 'Unknown') + ' Data Center'
        )

        operator = tags.get('operator') or tags.get('name', '')

        facilities.append({
            'name': name,
            'provider': operator,
            'address': tags.get('addr:street', tags.get('addr:full')),
            'city': tags.get('addr:city'),
            'state': tags.get('addr:state'),
            'country': (tags.get('addr:country') or '').upper(),
            'region': None,
            'latitude': lat,
            'longitude': lon,
            'power_mw': None,
            'sqft': None,
            'status': 'active',
            'source_url': f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
            'source_id': f"osm_{el.get('type')}_{el.get('id')}",
            'confidence': 0.7,
        })

    print(f"  Parsed {len(facilities)} facilities")
    return facilities


# ==============================================================
# SOURCE 3: Google News Facility Announcements
# ==============================================================

def fetch_news_facilities():
    """Scrape recent data center announcements from Google News RSS"""
    print("\n=== News Facility Extraction ===")

    search_queries = [
        "data center construction announced",
        "hyperscale data center new facility",
        "data center campus approved",
        "data center groundbreaking",
        "colocation facility expansion",
        "data center megawatt power",
    ]

    facilities = []

    for query in search_queries:
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'DC-Hub-Ingestion/1.0 (dchub.cloud)'
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode(errors='replace')

            # Simple XML parse for titles and links
            import re
            items = re.findall(r'<item>.*?</item>', content, re.DOTALL)

            for item in items[:5]:  # Top 5 per query
                title_match = re.search(r'<title>(.*?)</title>', item)
                link_match = re.search(r'<link/>(.*?)<', item)

                if title_match:
                    title = title_match.group(1)
                    link = link_match.group(1).strip() if link_match else ''

                    # Extract location hints from title
                    # This is basic - the real extraction would use NLP
                    facilities.append({
                        'name': title[:120],
                        'provider': '',
                        'address': None,
                        'city': None,
                        'state': None,
                        'country': 'US',
                        'region': 'North America',
                        'latitude': None,
                        'longitude': None,
                        'power_mw': None,
                        'sqft': None,
                        'status': 'announced',
                        'source_url': link,
                        'source_id': f"news_{hashlib.md5(title.encode()).hexdigest()[:12]}",
                        'confidence': 0.4,
                    })

            time.sleep(1)  # Rate limit

        except Exception as e:
            print(f"  Warning: Failed to fetch news for '{query}': {e}")

    print(f"  Extracted {len(facilities)} news facility mentions")
    print("  NOTE: News facilities have low confidence (0.4) and need manual review")
    return facilities


# ==============================================================
# SOURCE 4: Cloudscene Directory
# ==============================================================

def fetch_cloudscene():
    """Fetch from Cloudscene data center directory"""
    print("\n=== Cloudscene Directory Sync ===")
    print("  NOTE: Cloudscene requires web scraping - this fetches their public listings")

    # Cloudscene has a public directory but no public API
    # We can scrape their country listing pages
    countries = [
        ('united-states', 'US'),
        ('united-kingdom', 'GB'),
        ('germany', 'DE'),
        ('netherlands', 'NL'),
        ('france', 'FR'),
        ('singapore', 'SG'),
        ('japan', 'JP'),
        ('australia', 'AU'),
        ('canada', 'CA'),
        ('india', 'IN'),
        ('brazil', 'BR'),
        ('ireland', 'IE'),
    ]

    facilities = []

    for country_slug, country_code in countries:
        url = f"https://cloudscene.com/market/data-centers-in-{country_slug}/all"

        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; DC-Hub-Bot/1.0)',
                'Accept': 'text/html'
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode(errors='replace')

            # Extract facility names from listing page
            import re
            # Look for facility card patterns
            names = re.findall(r'<h[23][^>]*class="[^"]*card[^"]*"[^>]*>(.*?)</h[23]>', html, re.DOTALL)
            if not names:
                names = re.findall(r'data-center-name["\s]*[=>]([^<"]+)', html)

            for name in names:
                name = name.strip()
                if name and len(name) > 3:
                    facilities.append({
                        'name': name,
                        'provider': '',
                        'address': None,
                        'city': None,
                        'state': None,
                        'country': country_code,
                        'region': None,
                        'latitude': None,
                        'longitude': None,
                        'power_mw': None,
                        'sqft': None,
                        'status': 'active',
                        'source_url': url,
                        'source_id': f"cloudscene_{hashlib.md5(name.encode()).hexdigest()[:12]}",
                        'confidence': 0.6,
                    })

            print(f"  {country_slug}: found {len(names)} listings")
            time.sleep(2)  # Be respectful

        except Exception as e:
            print(f"  Warning: Failed {country_slug}: {e}")

    print(f"  Total Cloudscene facilities: {len(facilities)}")
    return facilities


# ==============================================================
# Main
# ==============================================================

def main():
    parser = argparse.ArgumentParser(description='DC Hub Multi-Source Facility Ingestion')
    parser.add_argument('--source', choices=['peeringdb', 'osm', 'news', 'cloudscene', 'all'],
                        default='all', help='Which source to pull from')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be inserted without writing to DB')
    args = parser.parse_args()

    conn = get_db()

    # Get baseline
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM facilities")
    baseline = cur.fetchone()[0]
    cur.close()
    print(f"\nBaseline facility count: {baseline}")

    total_inserted = 0
    total_skipped = 0

    sources = {
        'peeringdb': ('PeeringDB', fetch_peeringdb),
        'osm': ('OpenStreetMap', fetch_osm),
        'news': ('news_pipeline', fetch_news_facilities),
        'cloudscene': ('Cloudscene', fetch_cloudscene),
    }

    run_sources = sources.keys() if args.source == 'all' else [args.source]

    for src_key in run_sources:
        source_name, fetch_fn = sources[src_key]
        try:
            facilities = fetch_fn()
            inserted, skipped = insert_facilities(conn, facilities, source_name, args.dry_run)
            print(f"  Result: {inserted} inserted, {skipped} skipped (duplicates)")
            total_inserted += inserted
            total_skipped += skipped
        except Exception as e:
            print(f"  ERROR in {src_key}: {e}")
            import traceback
            traceback.print_exc()

    # Final count
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM facilities")
    final = cur.fetchone()[0]
    cur.close()

    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    print(f"  Baseline:  {baseline}")
    print(f"  Inserted:  {total_inserted}")
    print(f"  Skipped:   {total_skipped}")
    print(f"  Final:     {final}")
    print(f"  Net new:   {final - baseline}")
    if args.dry_run:
        print(f"  (DRY RUN - no records were written)")

    conn.close()


if __name__ == '__main__':
    main()