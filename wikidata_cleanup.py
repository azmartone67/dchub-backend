#!/usr/bin/env python3
"""
DC HUB - WIKIDATA CLEANUP & DATA QUALITY TOOL
==============================================
Clean up low-quality entries and improve data accuracy.

Features:
1. Stricter Wikidata validation (require positive keywords)
2. Remove generic/non-datacenter entries
3. Deduplicate across sources
4. Confidence scoring improvements
5. Statistics and reporting

Run: python wikidata_cleanup.py [--analyze|--clean|--strict-sync]
"""

import sqlite3
import json
import re
import os
from datetime import datetime
from typing import Dict, List, Tuple, Set
import argparse

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# =============================================================================
# ENHANCED VALIDATION RULES
# =============================================================================

# Strong positive indicators - MUST match for Wikidata entries
STRONG_POSITIVE_KEYWORDS = [
    'data center', 'datacenter', 'data centre', 'datacentre',
    'colocation', 'colo facility', 'colo site',
    'server farm', 'server facility',
    'internet exchange', 'internet data center',
    'cloud data center', 'cloud campus',
    'network hub', 'network facility',
    'carrier hotel', 'telecom hotel',
    'hosting facility', 'hosting center'
]

# Weak positive indicators - helpful but not definitive
WEAK_POSITIVE_KEYWORDS = [
    'cloud', 'hosting', 'server', 'network',
    'ix', 'noc', 'pop', 'edge', 'peering',
    'compute', 'hyperscale', 'campus'
]

# Major operators - presence confirms it's a DC
MAJOR_OPERATORS = [
    'equinix', 'digital realty', 'ntt', 'cyrusone', 'qts', 'coresite',
    'vantage', 'edgeconnex', 'databank', 'tierpoint', 'flexential',
    'switch', 'stream data', 'h5 data', 'prime data',
    'compass data', 'stack infrastructure', 'aligned', 'novva',
    'cloudhq', 'iron mountain', 'sabey', 'lincoln rackhouse',
    'interxion', 'telehouse', 'colt', 'centurylink', 'lumen',
    'cogent', 'zayo', 'de-cix', 'ams-ix', 'linx',
    'microsoft azure', 'amazon aws', 'google cloud', 'oracle cloud',
    'ibm cloud', 'alibaba cloud', 'tencent cloud'
]

# Strong negative keywords - definitely NOT data centers
# NOTE: Avoid generic words like "park" that appear in legitimate DC addresses (Science Park, Business Park)
STRONG_NEGATIVE_KEYWORDS = [
    # Transportation
    'railway', 'railroad', 'train station', 'metro station', 'bus station',
    'airport terminal', 'seaport', 'ferry terminal', 'subway station',
    
    # Education
    'school', 'university', 'college', 'academy', 'institute of technology',
    'elementary', 'high school', 'kindergarten', 'preschool',
    
    # Healthcare
    'hospital', 'clinic', 'medical center', 'health center', 'pharmacy',
    
    # Retail/Commercial - be specific
    'shopping mall', 'shopping center', 'supermarket', 'department store',
    'restaurant', 'cafe', 'hotel & casino', 'motel', 'resort', 'theater', 'cinema',
    
    # Industrial (non-DC) - be specific
    'warehouse district', 'factory', 'manufacturing plant', 'steel mill', 'oil refinery',
    'power station', 'power plant', 'electrical substation', 'wind farm', 'solar farm',
    
    # Residential
    'apartment complex', 'residential area', 'housing estate', 'condominium', 'villa',
    
    # Religious/Cultural
    'church', 'mosque', 'temple', 'synagogue', 'cathedral',
    'museum', 'public library', 'art gallery', 'monument',
    
    # Government (non-DC specific)
    'city hall', 'courthouse', 'prison', 'jail', 'police headquarters',
    'fire station', 'post office',
    
    # Sports/Recreation - be specific to avoid "Science Park", "Business Park"
    'football stadium', 'baseball stadium', 'sports arena', 'fitness center',
    'golf course', 'swimming pool', 'amusement park', 'theme park', 'national park',
    
    # Crypto (separate from DC)
    'bitcoin mine', 'crypto mine', 'mining farm', 'cryptocurrency',
    
    # Generic buildings - be more specific
    'office tower', 'corporate headquarters'
]

# Patterns that indicate generic/placeholder names
GENERIC_NAME_PATTERNS = [
    r'^Q\d+$',  # Wikidata ID without label
    r'^data center$',  # Just "data center" with no specifics
    r'^datacenter$',
    r'^\d+$',  # Just numbers
    r'^building \d+',  # Generic building numbers
    r'^facility \d+',
    r'^site \d+',
    r'^unknown',
    r'^unnamed',
    r'^untitled'
]


def is_valid_datacenter_strict(name: str, provider: str = "", source: str = "") -> Tuple[bool, str, float]:
    """
    Strict validation for data center entries.
    Returns: (is_valid, reason, confidence_score)
    """
    if not name:
        return False, "Empty name", 0.0
    
    name_lower = name.lower().strip()
    provider_lower = (provider or "").lower().strip()
    combined = f"{name_lower} {provider_lower}"
    
    # Check for generic/placeholder names
    for pattern in GENERIC_NAME_PATTERNS:
        if re.match(pattern, name_lower, re.IGNORECASE):
            return False, f"Generic name pattern: {pattern}", 0.0
    
    # Check for strong negative keywords
    for neg in STRONG_NEGATIVE_KEYWORDS:
        if neg in combined:
            return False, f"Negative keyword: {neg}", 0.0
    
    # Check for major operators (high confidence)
    for op in MAJOR_OPERATORS:
        if op in combined:
            return True, f"Major operator: {op}", 0.95
    
    # Check for strong positive keywords (high confidence)
    for pos in STRONG_POSITIVE_KEYWORDS:
        if pos in combined:
            return True, f"Strong positive: {pos}", 0.9
    
    # Check for weak positive keywords
    weak_matches = [p for p in WEAK_POSITIVE_KEYWORDS if p in combined]
    if len(weak_matches) >= 2:
        return True, f"Multiple weak positives: {weak_matches}", 0.7
    elif len(weak_matches) == 1:
        # Single weak positive - marginal
        if source == 'peeringdb':  # PeeringDB is trusted
            return True, f"Weak positive + trusted source", 0.75
        else:
            return False, f"Single weak positive insufficient: {weak_matches[0]}", 0.4
    
    # PeeringDB entries are generally trustworthy
    if source.lower() == 'peeringdb':
        return True, "Trusted source: PeeringDB", 0.8
    
    # OSM with telecom tags are usually valid
    if source.lower() in ['osm', 'openstreetmap']:
        return True, "OpenStreetMap entry", 0.7
    
    # For Wikidata without clear indicators, reject
    if source.lower() == 'wikidata':
        return False, "Wikidata entry without clear DC indicators", 0.3
    
    # Default: reject if no positive indicators
    return False, "No positive indicators found", 0.3


def analyze_database() -> Dict:
    """Analyze current database quality"""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    c = conn.cursor()
    
    stats = {
        'total': 0,
        'by_source': {},
        'invalid_entries': [],
        'low_confidence': [],
        'duplicates': [],
        'valid_count': 0,
        'invalid_count': 0
    }
    
    # Get all facilities
    c.execute("""
        SELECT id, name, provider, source, confidence, city, state, country
        FROM facilities
    """)
    
    rows = c.fetchall()
    stats['total'] = len(rows)
    
    # Analyze each entry
    for row in rows:
        source = row['source'] or 'unknown'
        stats['by_source'][source] = stats['by_source'].get(source, 0) + 1
        
        is_valid, reason, new_confidence = is_valid_datacenter_strict(
            row['name'], 
            row['provider'],
            source
        )
        
        if not is_valid:
            stats['invalid_entries'].append({
                'id': row['id'],
                'name': row['name'],
                'source': source,
                'reason': reason,
                'confidence': new_confidence
            })
            stats['invalid_count'] += 1
        elif new_confidence < 0.5:
            stats['low_confidence'].append({
                'id': row['id'],
                'name': row['name'],
                'source': source,
                'confidence': new_confidence
            })
        else:
            stats['valid_count'] += 1
    
    # Find duplicates
    c.execute("""
        SELECT name, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
        FROM facilities
        GROUP BY LOWER(TRIM(name))
        HAVING cnt > 1
    """)
    
    for row in c.fetchall():
        stats['duplicates'].append({
            'name': row['name'],
            'count': row['cnt'],
            'ids': row['ids'].split(',')
        })
    
    conn.close()
    return stats


def clean_database(dry_run: bool = True) -> Dict:
    """Clean invalid entries from database"""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    c = conn.cursor()
    
    results = {
        'analyzed': 0,
        'removed': 0,
        'updated_confidence': 0,
        'removed_ids': [],
        'kept_ids': []
    }
    
    # Get all facilities
    c.execute("""
        SELECT id, name, provider, source, confidence
        FROM facilities
    """)
    
    rows = c.fetchall()
    results['analyzed'] = len(rows)
    
    to_remove = []
    to_update = []
    
    for row in rows:
        is_valid, reason, new_confidence = is_valid_datacenter_strict(
            row['name'],
            row['provider'],
            row['source']
        )
        
        if not is_valid:
            to_remove.append((row['id'], row['name'], row['source'], reason))
        elif new_confidence != row['confidence']:
            to_update.append((row['id'], new_confidence))
    
    results['to_remove_count'] = len(to_remove)
    results['to_update_count'] = len(to_update)
    
    if dry_run:
        print(f"\n🔍 DRY RUN - No changes made")
        print(f"Would remove: {len(to_remove)} entries")
        print(f"Would update confidence: {len(to_update)} entries")
        
        # Show sample removals
        print(f"\n📋 Sample entries to remove:")
        for id, name, source, reason in to_remove[:20]:
            print(f"  - [{source}] {name[:50]}... → {reason}")
        
        results['removed_ids'] = [r[0] for r in to_remove]
    else:
        # Actually perform cleanup
        for id, name, source, reason in to_remove:
            c.execute("DELETE FROM facilities WHERE id = %s", [id])
            results['removed'] += 1
            results['removed_ids'].append(id)
        
        for id, confidence in to_update:
            c.execute("UPDATE facilities SET confidence = %s WHERE id = %s", [confidence, id])
            results['updated_confidence'] += 1
        
        conn.commit()
        print(f"\n✅ CLEANUP COMPLETE")
        print(f"Removed: {results['removed']} entries")
        print(f"Updated confidence: {results['updated_confidence']} entries")
    
    conn.close()
    return results


def remove_duplicates(dry_run: bool = True) -> Dict:
    """Remove duplicate entries, keeping highest confidence"""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    c = conn.cursor()
    
    results = {
        'duplicates_found': 0,
        'removed': 0
    }
    
    # Find duplicates by normalized name
    c.execute("""
        SELECT LOWER(TRIM(name)) as norm_name, COUNT(*) as cnt
        FROM facilities
        GROUP BY norm_name
        HAVING cnt > 1
    """)
    
    duplicate_names = c.fetchall()
    results['duplicates_found'] = len(duplicate_names)
    
    to_remove = []
    
    for row in duplicate_names:
        norm_name = row['norm_name']
        
        # Get all entries with this name, ordered by confidence and source priority
        c.execute("""
            SELECT id, name, source, confidence
            FROM facilities
            WHERE LOWER(TRIM(name)) = %s
            ORDER BY 
                CASE source 
                    WHEN 'peeringdb' THEN 1 
                    WHEN 'osm' THEN 2 
                    WHEN 'provider' THEN 3
                    ELSE 4 
                END,
                confidence DESC
        """, [norm_name])
        
        entries = c.fetchall()
        
        # Keep first (highest priority), remove rest
        for entry in entries[1:]:
            to_remove.append((entry['id'], entry['name'], entry['source']))
    
    if dry_run:
        print(f"\n🔍 DRY RUN - No changes made")
        print(f"Duplicate groups found: {results['duplicates_found']}")
        print(f"Entries to remove: {len(to_remove)}")
        
        print(f"\n📋 Sample duplicates to remove:")
        for id, name, source in to_remove[:20]:
            print(f"  - [{source}] {name[:50]}...")
    else:
        for id, name, source in to_remove:
            c.execute("DELETE FROM facilities WHERE id = %s", [id])
            results['removed'] += 1
        
        conn.commit()
        print(f"\n✅ DEDUPLICATION COMPLETE")
        print(f"Removed: {results['removed']} duplicate entries")
    
    conn.close()
    return results


def strict_wikidata_sync() -> Dict:
    """Re-sync Wikidata with much stricter validation"""
    import requests
    
    print("🔄 Re-syncing Wikidata with strict filtering...")
    
    # Stricter SPARQL query - require coordinates and more specific class
    query = """
    SELECT DISTINCT %sitem %sitemLabel %sitemDescription %scoords %scountry %scountryLabel %soperator %soperatorLabel WHERE {
      # Must be instance of data center (Q1066984) directly
      %sitem wdt:P31 wd:Q1066984 .
      
      # Must have coordinates
      %sitem wdt:P625 %scoords .
      
      # Get country
      OPTIONAL { %sitem wdt:P17 %scountry . }
      
      # Get operator
      OPTIONAL { %sitem wdt:P137 %soperator . }
      
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    LIMIT 10000
    """
    
    response = requests.get(
        "https://query.wikidata.org/sparql",
        headers={
            'Accept': 'application/sparql-results+json',
            'User-Agent': 'DCHub/3.0 (https://dchub.cloud)'
        },
        params={'query': query},
        timeout=60
    )
    
    if not response.ok:
        return {'error': f'SPARQL query failed: {response.status_code}'}
    
    data = response.json()
    results = data.get('results', {}).get('bindings', [])
    
    valid_entries = []
    invalid_entries = []
    
    for r in results:
        name = r.get('itemLabel', {}).get('value', '')
        desc = r.get('itemDescription', {}).get('value', '')
        operator = r.get('operatorLabel', {}).get('value', '')
        
        # Skip if no label or just Wikidata ID
        if not name or name.startswith('Q'):
            continue
        
        is_valid, reason, confidence = is_valid_datacenter_strict(
            name, operator, 'wikidata'
        )
        
        # Also check description
        if not is_valid and desc:
            combined = f"{name} {desc}"
            for pos in STRONG_POSITIVE_KEYWORDS:
                if pos in combined.lower():
                    is_valid = True
                    confidence = 0.85
                    reason = f"Description contains: {pos}"
                    break
        
        entry = {
            'name': name,
            'operator': operator,
            'country': r.get('countryLabel', {}).get('value', ''),
            'wikidata_id': r.get('item', {}).get('value', '').split('/')[-1],
            'confidence': confidence,
            'reason': reason
        }
        
        # Parse coordinates
        coords = r.get('coords', {}).get('value', '')
        if coords:
            match = re.search(r'Point\(([-\d.]+)\s+([-\d.]+)\)', coords)
            if match:
                entry['longitude'] = float(match.group(1))
                entry['latitude'] = float(match.group(2))
        
        if is_valid:
            valid_entries.append(entry)
        else:
            invalid_entries.append(entry)
    
    return {
        'total_fetched': len(results),
        'valid': len(valid_entries),
        'invalid': len(invalid_entries),
        'valid_entries': valid_entries,
        'sample_invalid': invalid_entries[:10]
    }


def print_analysis_report(stats: Dict):
    """Print detailed analysis report"""
    print("\n" + "="*60)
    print("📊 DC HUB DATABASE ANALYSIS REPORT")
    print("="*60)
    
    print(f"\n📈 OVERALL STATISTICS")
    print(f"  Total facilities: {stats['total']:,}")
    print(f"  Valid entries: {stats['valid_count']:,}")
    print(f"  Invalid entries: {stats['invalid_count']:,}")
    print(f"  Duplicate groups: {len(stats['duplicates'])}")
    
    print(f"\n📦 BY SOURCE")
    for source, count in sorted(stats['by_source'].items(), key=lambda x: -x[1]):
        pct = (count / stats['total'] * 100) if stats['total'] > 0 else 0
        print(f"  {source}: {count:,} ({pct:.1f}%)")
    
    print(f"\n❌ INVALID ENTRIES BY SOURCE")
    invalid_by_source = {}
    for entry in stats['invalid_entries']:
        src = entry['source']
        invalid_by_source[src] = invalid_by_source.get(src, 0) + 1
    
    for source, count in sorted(invalid_by_source.items(), key=lambda x: -x[1]):
        print(f"  {source}: {count:,}")
    
    print(f"\n📋 SAMPLE INVALID ENTRIES")
    for entry in stats['invalid_entries'][:15]:
        print(f"  [{entry['source']}] {entry['name'][:45]}...")
        print(f"      Reason: {entry['reason']}")
    
    print("\n" + "="*60)


def main():
    parser = argparse.ArgumentParser(description='DC Hub Wikidata Cleanup Tool')
    parser.add_argument('--analyze', action='store_true', help='Analyze database quality')
    parser.add_argument('--clean', action='store_true', help='Clean invalid entries')
    parser.add_argument('--dedup', action='store_true', help='Remove duplicates')
    parser.add_argument('--strict-sync', action='store_true', help='Re-sync Wikidata with strict rules')
    parser.add_argument('--dry-run', action='store_true', default=True, help='Dry run (no changes)')
    parser.add_argument('--execute', action='store_true', help='Actually execute changes')
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    if args.analyze or (not any([args.clean, args.dedup, args.strict_sync])):
        stats = analyze_database()
        print_analysis_report(stats)
    
    if args.clean:
        results = clean_database(dry_run=dry_run)
        print(f"\nCleanup results: {results}")
    
    if args.dedup:
        results = remove_duplicates(dry_run=dry_run)
        print(f"\nDeduplication results: {results}")
    
    if args.strict_sync:
        results = strict_wikidata_sync()
        print(f"\nStrict Wikidata sync:")
        print(f"  Total fetched: {results.get('total_fetched', 0)}")
        print(f"  Valid: {results.get('valid', 0)}")
        print(f"  Invalid: {results.get('invalid', 0)}")
        
        if results.get('sample_invalid'):
            print(f"\n  Sample invalid entries:")
            for e in results['sample_invalid']:
                print(f"    - {e['name'][:40]}... → {e['reason']}")


if __name__ == '__main__':
    main()
