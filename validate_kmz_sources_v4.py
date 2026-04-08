"""
DC Hub — KMZ v4.0 Source URL Validator
======================================
Checks every new v4.0 fiber source URL (carriers, states, ArcGIS searches)
and prints a report of which endpoints are live vs. unreachable.

Usage:
    python validate_kmz_sources_v4.py

    # Only check fiber carrier sources:
    python validate_kmz_sources_v4.py --category carriers

    # Save report to file:
    python validate_kmz_sources_v4.py --output report.json

Requirements:
    pip install requests
"""

import argparse
import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    import requests
    requests.packages.urllib3.disable_warnings()
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

TIMEOUT = 10
MAX_WORKERS = 10

# ── New v4.0 carrier / ISP sources ──────────────────────────────────────────
V4_CARRIER_SOURCES = [
    {'name': 'AT&T Fiber BEAD Expansion Zones',         'provider': 'AT&T',
     'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/ATT_BEAD_Fiber_Expansion/FeatureServer/0?f=json'},
    {'name': 'Comcast Xfinity Fiber Footprint',          'provider': 'Comcast',
     'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Comcast_Fiber_Footprint/FeatureServer/0?f=json'},
    {'name': 'Verizon Fios / Fiber Network',             'provider': 'Verizon',
     'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Verizon_Fiber_Network/FeatureServer/0?f=json'},
    {'name': 'Frontier Fiber Expansion Network',         'provider': 'Frontier',
     'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Frontier_Fiber_Expansion/FeatureServer/0?f=json'},
    {'name': 'Brightspeed Fiber Network',                'provider': 'Brightspeed',
     'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Brightspeed_Fiber_Network/FeatureServer/0?f=json'},
    {'name': 'Consolidated Communications Fiber',        'provider': 'Consolidated',
     'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Consolidated_Fiber/FeatureServer/0?f=json'},
    {'name': 'Cogent Communications Network',            'provider': 'Cogent',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Cogent_Fiber_Network/FeatureServer/0?f=json'},
    {'name': 'Uniti Fiber Wholesale Network',            'provider': 'Uniti',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Uniti_Fiber/FeatureServer/0?f=json'},
    {'name': 'Google Fiber Cities GIS',                  'provider': 'Google Fiber',
     'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Google_Fiber_Cities/FeatureServer/0?f=json'},
    {'name': 'ConnectAmerica Fund (CAF) Fiber Builds',   'provider': 'FCC-CAF',
     'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/CAF_II_Auction_Winners/FeatureServer/0?f=json'},
    {'name': 'USAC E-Rate Fiber Recipients',             'provider': 'USAC',
     'url': 'https://opendata.usac.org/api/views/rr4u-4bah/rows.json?accessType=DOWNLOAD&$limit=1'},
    {'name': 'FCC Broadband Fabric',                     'provider': 'FCC',
     'url': 'https://broadbandmap.fcc.gov/api/public/map/listAvailability'},
    # Existing carriers (sanity check)
    {'name': 'Zayo Fiber Network',                       'provider': 'Zayo',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Zayo_Network/FeatureServer/0?f=json'},
    {'name': 'Crown Castle Fiber',                       'provider': 'Crown Castle',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Crown_Castle_Fiber/FeatureServer/0?f=json'},
    {'name': 'Lumen Long Haul Fiber',                    'provider': 'Lumen',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Lumen_Fiber/FeatureServer/0?f=json'},
    {'name': 'Windstream Fiber Network',                 'provider': 'Windstream',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Windstream_Fiber/FeatureServer/0?f=json'},
]

# ── New v4.0 state broadband sources ─────────────────────────────────────────
V4_NEW_STATES = [
    {'name': 'Alaska Broadband',      'state': 'AK',
     'url': 'https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services/Alaska_Broadband?f=json'},
    {'name': 'Arkansas Broadband',    'state': 'AR',
     'url': 'https://services.arcgis.com/6bMRakJlLJLYR9rZ/arcgis/rest/services/Arkansas_Broadband?f=json'},
    {'name': 'Delaware Broadband',    'state': 'DE',
     'url': 'https://services1.arcgis.com/FjPcSmEFuDYlIdKC/arcgis/rest/services/Delaware_Broadband?f=json'},
    {'name': 'Hawaii Broadband',      'state': 'HI',
     'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Hawaii_Broadband?f=json'},
    {'name': 'North Dakota Broadband','state': 'ND',
     'url': 'https://services.arcgis.com/PX1yVoqIVMefKX8j/arcgis/rest/services/NorthDakota_Broadband?f=json'},
    {'name': 'Rhode Island Broadband','state': 'RI',
     'url': 'https://services2.arcgis.com/XVOqAjTOJ5P2QRIS/arcgis/rest/services/RhodeIsland_Broadband?f=json'},
    {'name': 'South Dakota Broadband','state': 'SD',
     'url': 'https://services.arcgis.com/qnjIJp7UJr6nLJwU/arcgis/rest/services/SouthDakota_Broadband?f=json'},
]

# ── Sample ArcGIS search queries (v4.0 additions) ─────────────────────────────
V4_ARCGIS_SEARCHES = [
    'https://www.arcgis.com/sharing/rest/search?q=AT%26T+fiber+broadband+infrastructure&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Comcast+Xfinity+fiber+broadband+expansion&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Verizon+FiOS+fiber+broadband+BEAD&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=BEAD+subgrantee+fiber+award+locations&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=E-Rate+fiber+school+library+broadband+USAC&sortField=modified&sortOrder=desc&num=5&f=json',
]

HEADERS = {'User-Agent': 'DCHub-KMZ-Validator/4.0'}


def check_url(entry: dict, category: str) -> dict:
    url = entry.get('url', '')
    name = entry.get('name', url)
    result = {
        'name':     name,
        'url':      url,
        'category': category,
        'provider': entry.get('provider') or entry.get('state', ''),
        'status':   None,
        'http_code': None,
        'latency_ms': None,
        'live':     False,
        'note':     '',
    }
    try:
        t0 = time.time()
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False,
                            allow_redirects=True)
        ms = round((time.time() - t0) * 1000)
        result['http_code']   = resp.status_code
        result['latency_ms']  = ms
        result['live']        = resp.status_code < 400

        if resp.status_code == 200:
            result['status'] = '✅ LIVE'
            # Check for ArcGIS error in JSON body
            try:
                body = resp.json()
                if 'error' in body:
                    result['status'] = '⚠️  HTTP 200 but ArcGIS error'
                    result['note']   = str(body['error'])
                    result['live']   = False
                elif category == 'arcgis_search' and 'results' in body:
                    result['note'] = f"{len(body['results'])} results returned"
            except Exception:
                pass
        elif resp.status_code == 404:
            result['status'] = '❌ 404 NOT FOUND'
        elif resp.status_code == 403:
            result['status'] = '🔒 403 FORBIDDEN (may need auth)'
            result['live']   = True   # endpoint exists, just gated
        else:
            result['status'] = f'⚠️  HTTP {resp.status_code}'

    except requests.exceptions.Timeout:
        result['status'] = '⏱️  TIMEOUT'
        result['note']   = f'>{TIMEOUT}s'
    except requests.exceptions.ConnectionError as e:
        result['status'] = '🔴 CONNECTION ERROR'
        result['note']   = str(e)[:80]
    except Exception as e:
        result['status'] = '❓ ERROR'
        result['note']   = str(e)[:80]

    return result


def run_validation(categories: list) -> dict:
    tasks = []

    if 'carriers' in categories:
        for entry in V4_CARRIER_SOURCES:
            tasks.append((entry, 'carrier'))

    if 'states' in categories:
        for entry in V4_NEW_STATES:
            tasks.append((entry, 'new_state'))

    if 'arcgis' in categories:
        for url in V4_ARCGIS_SEARCHES:
            tasks.append(({'name': url[:80], 'url': url}, 'arcgis_search'))

    print(f"\n🔍 DC Hub KMZ v4.0 Source Validator")
    print(f"   Checking {len(tasks)} endpoints  ({MAX_WORKERS} parallel workers)")
    print(f"   Timeout: {TIMEOUT}s per request")
    print("=" * 70)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_url, entry, cat): (entry, cat) for entry, cat in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            results.append(r)
            icon = r['status'].split()[0] if r['status'] else '?'
            ms   = f"{r['latency_ms']}ms" if r['latency_ms'] else '---'
            note = f"  [{r['note']}]" if r['note'] else ''
            print(f"  [{i:>2}/{len(tasks)}] {icon}  {r['name'][:55]:<55} {ms:>7}{note}")

    return results


def print_summary(results: list):
    live    = [r for r in results if r['live']]
    dead    = [r for r in results if not r['live']]
    by_cat  = {}
    for r in results:
        by_cat.setdefault(r['category'], []).append(r)

    print("\n" + "=" * 70)
    print(f"📊  SUMMARY  —  {len(live)}/{len(results)} sources reachable")
    print("=" * 70)

    for cat, items in by_cat.items():
        live_c = sum(1 for i in items if i['live'])
        print(f"\n  {cat.upper():20s}  {live_c}/{len(items)} live")
        for r in sorted(items, key=lambda x: x['live'], reverse=True):
            tag = r['status'] or '?'
            print(f"    {tag:35s}  {r['name'][:50]}")

    if dead:
        print(f"\n⚠️   {len(dead)} sources need attention:")
        for r in dead:
            print(f"    • {r['name']} — {r['status']} {r['note']}")

    print(f"\n✅  Validated at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")


def main():
    parser = argparse.ArgumentParser(description='Validate KMZ v4.0 sources')
    parser.add_argument('--category', choices=['carriers', 'states', 'arcgis', 'all'],
                        default='all', help='Which category to check')
    parser.add_argument('--output', help='Save JSON report to file')
    args = parser.parse_args()

    cats = ['carriers', 'states', 'arcgis'] if args.category == 'all' else [args.category]
    results = run_validation(cats)
    print_summary(results)

    if args.output:
        with open(args.output, 'w') as f:
            json.dump({
                'validated_at': datetime.utcnow().isoformat(),
                'total': len(results),
                'live':  sum(1 for r in results if r['live']),
                'results': results,
            }, f, indent=2)
        print(f"\n💾  Report saved to {args.output}")


if __name__ == '__main__':
    main()
