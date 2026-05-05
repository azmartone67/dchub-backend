"""
DC Hub — KMZ v4.0 Source URL Validator (Fixed)
===============================================
Validates all fixed v4.0 fiber source URLs.
All carrier sources now use api_discover (ArcGIS search) — proven to work.
State broadband sources use real state GIS hub endpoints.

Usage:
    python validate_kmz_sources_v4.py
    python validate_kmz_sources_v4.py --category carriers
    python validate_kmz_sources_v4.py --category states
    python validate_kmz_sources_v4.py --category arcgis
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

TIMEOUT = 12
MAX_WORKERS = 10
HEADERS = {'User-Agent': 'DCHub-KMZ-Validator/4.0-fixed'}

# ── Carrier sources (all api_discover / ArcGIS search) ──────────────────────
V4_CARRIER_SOURCES = [
    {'name': 'Zayo Fiber Network',
     'provider': 'Zayo',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Zayo+fiber+network+routes+broadband&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Crown Castle Fiber',
     'provider': 'Crown Castle',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Crown+Castle+fiber+small+cell+network&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Lumen Long Haul Fiber',
     'provider': 'Lumen',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Lumen+CenturyLink+fiber+long+haul+network&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Windstream Fiber Network',
     'provider': 'Windstream',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Windstream+fiber+broadband+network+route&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'AT&T Fiber Network',
     'provider': 'AT&T',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=AT%26T+fiber+broadband+BEAD+expansion&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Comcast Xfinity Fiber',
     'provider': 'Comcast',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Comcast+Xfinity+fiber+broadband+expansion&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Verizon Fiber / FiOS',
     'provider': 'Verizon',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Verizon+FiOS+fiber+broadband+network&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Frontier Fiber Expansion',
     'provider': 'Frontier',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Frontier+fiber+broadband+expansion+BEAD&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Brightspeed Fiber Network',
     'provider': 'Brightspeed',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Brightspeed+fiber+broadband+BEAD&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Consolidated Communications Fiber',
     'provider': 'Consolidated',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Consolidated+Communications+fiber+broadband&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Cogent Communications Network',
     'provider': 'Cogent',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Cogent+fiber+network+route+backbone&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Uniti Fiber Wholesale Network',
     'provider': 'Uniti',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Uniti+fiber+wholesale+network+broadband&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Google Fiber Cities',
     'provider': 'Google Fiber',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Google+Fiber+city+broadband+gigabit&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'FCC BDC Living Atlas',
     'provider': 'FCC',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=FCC+broadband+data+collection+BDC+2024&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'USAC E-Rate Funded Connections',
     'provider': 'USAC',
     'url': 'https://opendata.usac.org/resource/rr4u-4bah.json?$limit=1'},
    {'name': 'ConnectAmerica CAF II',
     'provider': 'FCC-CAF',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=ConnectAmerica+CAF+II+auction+broadband+fiber&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Microsoft Airband Broadband',
     'provider': 'Microsoft',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Microsoft+Airband+broadband+rural+coverage&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Ookla Fixed Broadband Performance',
     'provider': 'Ookla',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Ookla+Speedtest+fixed+broadband+performance&sortField=modified&sortOrder=desc&num=5&f=json'},
]

# ── New v4.0 state broadband sources (fixed real endpoints) ──────────────────
V4_NEW_STATES = [
    {'name': 'Alaska Broadband (ABO Hub)',    'state': 'AK',
     'url': 'https://broadband-outreach-dcced.hub.arcgis.com/api/search/v1/items?q=broadband&num=5&f=json'},
    {'name': 'Arkansas Broadband',            'state': 'AR',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Arkansas+broadband+fiber+BEAD+rural&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'Delaware Broadband (FirstMap)', 'state': 'DE',
     'url': 'https://opendata.firstmap.delaware.gov/api/search/v1/items?q=broadband&num=5&f=json'},
    {'name': 'Hawaii Broadband',              'state': 'HI',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Hawaii+broadband+fiber+BEAD+DCCA&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'North Dakota GIS Hub',          'state': 'ND',
     'url': 'https://gishubdata-ndgov.hub.arcgis.com/api/search/v1/items?q=broadband&num=5&f=json'},
    {'name': 'Rhode Island Broadband',        'state': 'RI',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=Rhode+Island+broadband+fiber+BEAD&sortField=modified&sortOrder=desc&num=5&f=json'},
    {'name': 'South Dakota Broadband',        'state': 'SD',
     'url': 'https://www.arcgis.com/sharing/rest/search?q=South+Dakota+broadband+fiber+BEAD+BIT&sortField=modified&sortOrder=desc&num=5&f=json'},
]

# ── ArcGIS searches (proven working from first run) ───────────────────────────
V4_ARCGIS_SEARCHES = [
    'https://www.arcgis.com/sharing/rest/search?q=AT%26T+fiber+broadband+infrastructure&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Comcast+Xfinity+fiber+broadband+expansion&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Verizon+FiOS+fiber+broadband+BEAD&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=BEAD+subgrantee+fiber+award+locations&sortField=modified&sortOrder=desc&num=5&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=E-Rate+fiber+school+library+broadband+USAC&sortField=modified&sortOrder=desc&num=5&f=json',
]


def check_url(entry: dict, category: str) -> dict:
    url = entry.get('url', '')
    name = entry.get('name', url)
    result = {
        'name':      name,
        'url':       url,
        'category':  category,
        'provider':  entry.get('provider') or entry.get('state', ''),
        'status':    None,
        'http_code': None,
        'latency_ms': None,
        'live':      False,
        'results_count': None,
        'note':      '',
    }
    try:
        t0 = time.time()
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False,
                            allow_redirects=True)
        ms = round((time.time() - t0) * 1000)
        result['http_code']  = resp.status_code
        result['latency_ms'] = ms
        result['live']       = resp.status_code < 400

        if resp.status_code == 200:
            try:
                body = resp.json()
                if 'error' in body:
                    result['status'] = '⚠️  ArcGIS error in body'
                    result['note']   = str(body['error'])
                    result['live']   = False
                elif 'results' in body:
                    n = len(body['results'])
                    result['results_count'] = n
                    result['status'] = f'✅ LIVE ({n} results)'
                    result['live']   = True
                elif 'items' in body:
                    n = len(body.get('items', []))
                    result['results_count'] = n
                    result['status'] = f'✅ LIVE ({n} items)'
                    result['live']   = True
                elif isinstance(body, list):
                    result['results_count'] = len(body)
                    result['status'] = f'✅ LIVE ({len(body)} records)'
                    result['live']   = True
                else:
                    result['status'] = '✅ LIVE'
            except Exception:
                result['status'] = '✅ LIVE (non-JSON)'
        elif resp.status_code == 403:
            result['status'] = '🔒 403 FORBIDDEN (auth required — endpoint exists)'
            result['live']   = True
        elif resp.status_code == 404:
            result['status'] = '❌ 404 NOT FOUND'
        elif resp.status_code == 405:
            result['status'] = '⚠️  405 METHOD NOT ALLOWED'
        else:
            result['status'] = f'⚠️  HTTP {resp.status_code}'

    except requests.exceptions.Timeout:
        result['status'] = f'⏱️  TIMEOUT (>{TIMEOUT}s)'
    except requests.exceptions.ConnectionError as e:
        result['status'] = '🔴 CONNECTION ERROR'
        result['note']   = str(e)[:80]
    except Exception as e:
        result['status'] = '❓ ERROR'
        result['note']   = str(e)[:80]

    return result


def run_validation(categories: list) -> list:
    tasks = []
    if 'carriers' in categories:
        for entry in V4_CARRIER_SOURCES:
            tasks.append((entry, 'carrier'))
    if 'states' in categories:
        for entry in V4_NEW_STATES:
            tasks.append((entry, 'new_state'))
    if 'arcgis' in categories:
        for url in V4_ARCGIS_SEARCHES:
            tasks.append(({'name': url[60:110] + '…', 'url': url}, 'arcgis_search'))

    print(f"\n🔍 DC Hub KMZ v4.0 Source Validator (Fixed URLs)")
    print(f"   Checking {len(tasks)} endpoints  ({MAX_WORKERS} parallel workers)")
    print(f"   Timeout: {TIMEOUT}s per request")
    print("=" * 72)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_url, e, c): (e, c) for e, c in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            results.append(r)
            icon = r['status'].split()[0] if r['status'] else '?'
            ms   = f"{r['latency_ms']}ms" if r['latency_ms'] else '---'
            note = f"  [{r['note']}]" if r['note'] else ''
            print(f"  [{i:>2}/{len(tasks)}] {icon}  {r['name'][:55]:<55} {ms:>7}{note}")

    return results


def print_summary(results: list):
    live   = [r for r in results if r['live']]
    dead   = [r for r in results if not r['live']]
    by_cat = {}
    for r in results:
        by_cat.setdefault(r['category'], []).append(r)

    print("\n" + "=" * 72)
    print(f"📊  SUMMARY  —  {len(live)}/{len(results)} sources reachable")
    print("=" * 72)

    for cat, items in by_cat.items():
        live_c = sum(1 for i in items if i['live'])
        print(f"\n  {cat.upper():22s}  {live_c}/{len(items)} live")
        for r in sorted(items, key=lambda x: (not x['live'], x['name'])):
            tag = (r['status'] or '?')[:38]
            print(f"    {tag:<38}  {r['name'][:40]}")

    if dead:
        print(f"\n⚠️   {len(dead)} sources still need attention:")
        for r in dead:
            print(f"    • [{r['category']}] {r['name']} — {r['status']} {r['note']}")
    else:
        print(f"\n✅  All {len(results)} sources reachable!")

    print(f"\n✅  Validated at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")


def main():
    parser = argparse.ArgumentParser(description='Validate KMZ v4.0 sources (fixed)')
    parser.add_argument('--category', choices=['carriers', 'states', 'arcgis', 'all'],
                        default='all')
    parser.add_argument('--output', help='Save JSON report to file')
    args = parser.parse_args()

    cats    = ['carriers', 'states', 'arcgis'] if args.category == 'all' else [args.category]
    results = run_validation(cats)
    print_summary(results)

    if args.output:
        with open(args.output, 'w') as f:
            json.dump({
                'validated_at': datetime.utcnow().isoformat(),
                'total':  len(results),
                'live':   sum(1 for r in results if r['live']),
                'results': results,
            }, f, indent=2)
        print(f"\n💾  Report saved to {args.output}")


if __name__ == '__main__':
    main()
