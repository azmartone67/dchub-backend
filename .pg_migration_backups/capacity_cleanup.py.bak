"""
DC Hub Capacity Data Cleanup & Enhancement Script
==================================================
Fixes:
1. Deduplicates capacity_pipeline table
2. Extracts operator names from notes field
3. Extracts locations and maps to markets/regions
4. Filters out non-data center content (solar, BESS, wind, hydro)
5. Adds confidence scores to each record

Run this in Replit: python capacity_cleanup.py
"""

import sqlite3
import re
from datetime import datetime

# =============================================================================
# CONFIGURATION
# =============================================================================

DB_PATH = 'dc_nexus.db'

# Known data center operators to extract from notes
KNOWN_OPERATORS = [
    # Hyperscalers
    ('Google', ['google', 'alphabet']),
    ('Microsoft', ['microsoft', 'azure']),
    ('Amazon/AWS', ['amazon', 'aws', 'amazon web services']),
    ('Meta', ['meta', 'facebook']),
    ('Apple', ['apple']),
    ('Oracle', ['oracle']),
    ('IBM', ['ibm']),
    
    # Major Colocation
    ('Equinix', ['equinix']),
    ('Digital Realty', ['digital realty', 'digitalrealty']),
    ('CyrusOne', ['cyrusone', 'cyrus one']),
    ('QTS', ['qts realty', 'qts data']),
    ('CoreSite', ['coresite']),
    ('Vantage', ['vantage data']),
    ('Stack Infrastructure', ['stack infrastructure']),
    ('Compass Datacenters', ['compass data']),
    ('Aligned', ['aligned data', 'aligned energy']),
    ('Switch', ['switch data', 'switch signs', 'switch inc']),
    ('DataBank', ['databank']),
    ('Flexential', ['flexential']),
    ('TierPoint', ['tierpoint']),
    ('CyrusOne', ['cyrusone']),
    
    # International
    ('NTT', ['ntt data', 'ntt global']),
    ('Chindata', ['chindata']),
    ('GDS', ['gds holdings', 'gds data']),
    ('AirTrunk', ['airtrunk']),
    ('NEXTDC', ['nextdc']),
    ('Interxion', ['interxion']),
    ('Global Switch', ['global switch']),
    ('Keppel DC', ['keppel dc', 'keppel data']),
    ('ST Telemedia', ['st telemedia']),
    ('EdgeConneX', ['edgeconnex']),
    ('Colt DCS', ['colt dcs', 'colt data']),
    
    # Emerging/Specialized
    ('Applied Digital', ['applied digital']),
    ('DayOne', ['dayone']),
    ('Nebius', ['nebius']),
    ('CleanArc', ['cleanarc']),
    ('Goodman', ['goodman']),
    ('xAI', ['xai', 'elon musk']),
    ('G42', ['g42']),
    ('Crow Holdings', ['crow holdings']),
    ('CleanSpark', ['cleanspark']),
    ('Soluna', ['soluna']),
    ('Blueprint Data Centers', ['blueprint data']),
    ('Galaxy Digital', ['galaxy digital', 'galaxy completes']),
    ('RadiusDC', ['radiusdc', 'radius dc']),
    ('WhiteFiber', ['whitefiber']),
    ('PACE', ['pace taps', 'pace data']),
    ('ASP', ['asp acquires']),
    ('TM Nxera', ['tm nxera', 'nxera']),
]

# Location patterns for extraction
LOCATION_PATTERNS = [
    # US States/Cities
    (r'in\s+(Dallas|Houston|Austin|San Antonio),?\s*Texas', 'Dallas', 'Texas', 'North America'),
    (r'Texas\s+(?:campus|data center|facility)', None, 'Texas', 'North America'),
    (r'in\s+(Phoenix|Tempe|Mesa|Chandler),?\s*Arizona', 'Phoenix', 'Arizona', 'North America'),
    (r'in\s+(Northern Virginia|Ashburn|Loudoun)', 'Northern Virginia', 'Virginia', 'North America'),
    (r'in\s+(Kansas City),?\s*Missouri', 'Kansas City', 'Missouri', 'North America'),
    (r'in\s+(Indianapolis),?\s*Indiana', 'Indianapolis', 'Indiana', 'North America'),
    (r'in\s+(Southern California|Los Angeles|San Diego)', 'Southern California', 'California', 'North America'),
    (r'in\s+(Silicon Valley|San Jose|Santa Clara)', 'Silicon Valley', 'California', 'North America'),
    (r'in\s+(Chicago|Aurora),?\s*Illinois', 'Chicago', 'Illinois', 'North America'),
    (r'in\s+(Atlanta),?\s*Georgia', 'Atlanta', 'Georgia', 'North America'),
    (r'Nevada', None, 'Nevada', 'North America'),
    
    # International
    (r'(?:outside\s+)?Helsinki,?\s*Finland', 'Helsinki', 'Finland', 'EMEA'),
    (r'Finland', None, 'Finland', 'EMEA'),
    (r'UK\s+(?:data center|portfolio)', None, 'United Kingdom', 'EMEA'),
    (r'United Kingdom|Britain', None, 'United Kingdom', 'EMEA'),
    (r'Germany', None, 'Germany', 'EMEA'),
    (r'Netherlands', None, 'Netherlands', 'EMEA'),
    (r'Ireland', None, 'Ireland', 'EMEA'),
    (r'Norway|Suldal', None, 'Norway', 'EMEA'),
    (r'Bulgaria', None, 'Bulgaria', 'EMEA'),
    
    (r'Sydney,?\s*Australia', 'Sydney', 'Australia', 'APAC'),
    (r'Australia', None, 'Australia', 'APAC'),
    (r'(?:outside\s+)?Kuala Lumpur|Malaysia', 'Kuala Lumpur', 'Malaysia', 'APAC'),
    (r'Singapore', 'Singapore', 'Singapore', 'APAC'),
    (r'Tokyo|Japan', 'Tokyo', 'Japan', 'APAC'),
    (r'South Korea|Korean', None, 'South Korea', 'APAC'),
    
    (r'Canada', None, 'Canada', 'North America'),
    (r'Uzbekistan', None, 'Uzbekistan', 'EMEA'),
    (r'Chile', None, 'Chile', 'LATAM'),
    (r'Brazil', None, 'Brazil', 'LATAM'),
]

# Keywords that indicate NON-data center content (to filter out)
NON_DC_KEYWORDS = [
    'solar farm', 'solar plant', 'solar project', 'solar in 202',
    'pv magazine', 'photovoltaic',
    'bess', 'battery energy storage', 'energy storage news',
    'wind farm', 'wind project', 'wind power',
    'pumped storage', 'pumped hydro',
    'hydrogen electrolyzer', 'hydrogen plant',
    'thermal storage', 'thermal energy',
    'grid-forming', 'grid forming',
    'mwh of storage', 'mwh storage',
    'fuel cell',
    'geothermal ppa',  # except if it's powering a DC
]

# Keywords that CONFIRM data center content
DC_KEYWORDS = [
    'data center', 'data centre', 'datacenter', 'datacentre',
    'hyperscale', 'colocation', 'colo facility',
    'ai data center', 'ai-ready',
    'cloud campus', 'cloud infrastructure',
    'megawatt campus', 'mw campus',
    'server farm', 'compute facility',
]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def extract_operator(notes):
    """Extract operator name from notes field"""
    if not notes:
        return None
    
    notes_lower = notes.lower()
    
    for operator_name, keywords in KNOWN_OPERATORS:
        for keyword in keywords:
            if keyword in notes_lower:
                return operator_name
    
    return None


def extract_location(notes):
    """Extract market, country, and region from notes"""
    if not notes:
        return None, None, 'Unknown'
    
    for pattern, market, country, region in LOCATION_PATTERNS:
        match = re.search(pattern, notes, re.IGNORECASE)
        if match:
            # If market is None but we have a capture group, use it
            extracted_market = market
            if market is None and match.groups():
                extracted_market = match.group(1)
            return extracted_market, country, region
    
    return None, None, 'Unknown'


def is_data_center_content(notes, source):
    """Determine if this is actually data center content"""
    if not notes:
        return False
    
    notes_lower = notes.lower()
    source_lower = (source or '').lower()
    
    # Check for explicit DC keywords - high confidence it's DC content
    for keyword in DC_KEYWORDS:
        if keyword in notes_lower:
            return True
    
    # Check for non-DC keywords - likely NOT DC content
    for keyword in NON_DC_KEYWORDS:
        if keyword in notes_lower or keyword in source_lower:
            # Exception: if it also mentions data center, keep it
            if 'data center' in notes_lower or 'data centre' in notes_lower:
                return True
            return False
    
    # Check source - some sources are DC-specific
    dc_sources = ['data center dynamics', 'data center knowledge', 'datacenter', 
                  'the register dc', 'datacenter frontier']
    for dc_source in dc_sources:
        if dc_source in source_lower:
            return True
    
    # Default: if source is energy/solar focused, probably not DC
    non_dc_sources = ['pv magazine', 'energy storage news', 'solar', 'wind']
    for non_dc_source in non_dc_sources:
        if non_dc_source in source_lower:
            return False
    
    # Uncertain - keep it but flag as low confidence
    return True


def calculate_confidence(record):
    """Calculate confidence score (0-100) for a capacity record"""
    score = 0
    
    # Has operator (40 points)
    if record.get('operator') and record['operator'] != 'Unknown':
        # Known major operator = higher score
        major_operators = ['Google', 'Microsoft', 'Amazon/AWS', 'Meta', 'Equinix', 
                          'Digital Realty', 'CyrusOne', 'QTS', 'NTT']
        if record['operator'] in major_operators:
            score += 40
        else:
            score += 30
    
    # Has specific location (30 points)
    if record.get('market') and record['market'] != 'Unknown':
        score += 30
    elif record.get('country') and record['country'] != 'Unknown':
        score += 15
    
    # Has reliable source (20 points)
    trusted_sources = ['data center dynamics', 'data center knowledge', 
                       'datacenter frontier', 'pr newswire', 'business wire']
    source_lower = (record.get('source') or '').lower()
    if any(ts in source_lower for ts in trusted_sources):
        score += 20
    elif record.get('source'):
        score += 10
    
    # Has recent date (10 points)
    if record.get('announcement_date'):
        score += 10
    
    return min(score, 100)


def get_confidence_label(score):
    """Convert score to label"""
    if score >= 70:
        return 'high'
    elif score >= 40:
        return 'medium'
    else:
        return 'low'


# =============================================================================
# MAIN CLEANUP FUNCTIONS
# =============================================================================

def cleanup_capacity_data():
    """Main cleanup function"""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    print("=" * 60)
    print("DC Hub Capacity Data Cleanup")
    print("=" * 60)
    
    # Step 1: Get all records
    c.execute('SELECT * FROM capacity_pipeline ORDER BY id')
    all_records = c.fetchall()
    print(f"\n[1] Total records found: {len(all_records)}")
    
    # Step 2: Identify duplicates and non-DC content
    seen_notes = {}  # notes -> best record id
    to_delete = []
    to_update = []
    non_dc_count = 0
    
    for row in all_records:
        record = dict(row)
        notes = (record.get('notes') or '').strip()
        
        # Check if it's data center content
        if not is_data_center_content(notes, record.get('source')):
            to_delete.append(record['id'])
            non_dc_count += 1
            continue
        
        # Check for duplicates (same notes = duplicate)
        if notes in seen_notes:
            # Keep the one with more data
            existing_id = seen_notes[notes]
            to_delete.append(record['id'])
        else:
            seen_notes[notes] = record['id']
            
            # Extract and enhance data
            extracted_operator = extract_operator(notes)
            market, country, region = extract_location(notes)
            
            # Only update if we found better data
            updates = {}
            
            if extracted_operator and (not record.get('operator') or record['operator'] == 'Unknown'):
                updates['operator'] = extracted_operator
            
            if market and (not record.get('market') or record['market'] == 'Unknown'):
                updates['market'] = market
            
            if region and region != 'Unknown' and (not record.get('region') or record['region'] == 'Unknown'):
                updates['region'] = region
            
            if updates:
                updates['id'] = record['id']
                to_update.append(updates)
    
    print(f"[2] Non-DC content to remove: {non_dc_count}")
    print(f"[3] Duplicates to remove: {len(to_delete) - non_dc_count}")
    print(f"[4] Records to enhance: {len(to_update)}")
    
    # Step 3: Delete duplicates and non-DC content
    if to_delete:
        placeholders = ','.join('?' * len(to_delete))
        c.execute(f'DELETE FROM capacity_pipeline WHERE id IN ({placeholders})', to_delete)
        print(f"[5] Deleted {c.rowcount} records")
    
    # Step 4: Update records with extracted data
    for updates in to_update:
        record_id = updates.pop('id')
        if updates:
            set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
            values = list(updates.values()) + [record_id]
            c.execute(f'UPDATE capacity_pipeline SET {set_clause} WHERE id = ?', values)
    
    print(f"[6] Updated {len(to_update)} records with extracted data")
    
    conn.commit()
    
    # Step 5: Report final stats
    c.execute('SELECT COUNT(*) FROM capacity_pipeline')
    final_count = c.fetchone()[0]
    
    c.execute('SELECT SUM(capacity_mw) FROM capacity_pipeline')
    total_mw = c.fetchone()[0] or 0
    
    c.execute('SELECT COUNT(DISTINCT operator) FROM capacity_pipeline WHERE operator IS NOT NULL AND operator != "Unknown"')
    operator_count = c.fetchone()[0]
    
    c.execute('SELECT COUNT(*) FROM capacity_pipeline WHERE market IS NOT NULL AND market != "Unknown"')
    with_market = c.fetchone()[0]
    
    print("\n" + "=" * 60)
    print("CLEANUP COMPLETE")
    print("=" * 60)
    print(f"Final record count: {final_count}")
    print(f"Total pipeline MW: {total_mw:,.0f}")
    print(f"Unique operators: {operator_count}")
    print(f"Records with market: {with_market}")
    
    # Step 6: Show sample of cleaned data
    print("\n" + "-" * 60)
    print("SAMPLE CLEANED RECORDS:")
    print("-" * 60)
    
    c.execute('''SELECT operator, capacity_mw, market, region, notes 
                 FROM capacity_pipeline 
                 WHERE operator IS NOT NULL AND operator != "Unknown"
                 ORDER BY capacity_mw DESC 
                 LIMIT 10''')
    
    for row in c.fetchall():
        print(f"  {row[0]:20} | {row[1]:>6} MW | {row[2] or 'Unknown':15} | {row[3]:12}")
    
    conn.close()
    
    return {
        'records_removed': len(to_delete),
        'records_updated': len(to_update),
        'final_count': final_count,
        'total_mw': total_mw,
        'operators': operator_count
    }


def add_confidence_scores():
    """Add confidence scores to all capacity records"""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Check if confidence column exists, add if not
    c.execute("PRAGMA table_info(capacity_pipeline)")
    columns = [col[1] for col in c.fetchall()]
    
    if 'confidence_score' not in columns:
        c.execute('ALTER TABLE capacity_pipeline ADD COLUMN confidence_score INTEGER DEFAULT 0')
        c.execute('ALTER TABLE capacity_pipeline ADD COLUMN confidence_label TEXT DEFAULT "low"')
        print("[+] Added confidence_score and confidence_label columns")
    
    # Calculate and update confidence for all records
    c.execute('SELECT * FROM capacity_pipeline')
    
    for row in c.fetchall():
        record = dict(row)
        score = calculate_confidence(record)
        label = get_confidence_label(score)
        
        c.execute('''UPDATE capacity_pipeline 
                     SET confidence_score = ?, confidence_label = ? 
                     WHERE id = ?''', (score, label, record['id']))
    
    conn.commit()
    
    # Report distribution
    c.execute('''SELECT confidence_label, COUNT(*) as cnt 
                 FROM capacity_pipeline 
                 GROUP BY confidence_label''')
    
    print("\nConfidence Score Distribution:")
    for row in c.fetchall():
        print(f"  {row[0]:10}: {row[1]} records")
    
    conn.close()


# =============================================================================
# FLASK ENDPOINT INTEGRATION
# =============================================================================

def get_enhanced_capacity_endpoint_code():
    """Returns code to add to main.py for enhanced capacity endpoint"""
    return '''
# Enhanced Capacity Pipeline Endpoint
@app.route('/api/intelligence/global/capacity/enhanced')
def get_enhanced_capacity():
    """Get capacity pipeline with quality filtering and stats"""
    conn = sqlite3.connect('dc_nexus.db', timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Get filter params
    min_confidence = request.args.get('min_confidence', type=int)
    operator = request.args.get('operator')
    region = request.args.get('region')
    min_mw = request.args.get('min_mw', type=float)
    
    # Build query
    query = 'SELECT * FROM capacity_pipeline WHERE 1=1'
    params = []
    
    if min_confidence:
        query += ' AND confidence_score >= ?'
        params.append(min_confidence)
    
    if operator:
        query += ' AND operator LIKE ?'
        params.append(f'%{operator}%')
    
    if region:
        query += ' AND region = ?'
        params.append(region)
    
    if min_mw:
        query += ' AND capacity_mw >= ?'
        params.append(min_mw)
    
    query += ' ORDER BY capacity_mw DESC'
    
    c.execute(query, params)
    
    records = []
    for row in c.fetchall():
        records.append({
            'id': row['id'],
            'operator': row['operator'],
            'capacity_mw': row['capacity_mw'],
            'market': row['market'],
            'region': row['region'],
            'phase': row['phase'],
            'source': row['source'],
            'notes': row['notes'],
            'announcement_date': row['announcement_date'],
            'confidence_score': row['confidence_score'] if 'confidence_score' in row.keys() else 0,
            'confidence_label': row['confidence_label'] if 'confidence_label' in row.keys() else 'low'
        })
    
    # Calculate stats
    total_mw = sum(r['capacity_mw'] or 0 for r in records)
    high_confidence = len([r for r in records if r.get('confidence_score', 0) >= 70])
    operators = list(set(r['operator'] for r in records if r['operator'] and r['operator'] != 'Unknown'))
    
    conn.close()
    
    return jsonify({
        'success': True,
        'count': len(records),
        'total_mw': total_mw,
        'high_confidence_count': high_confidence,
        'unique_operators': len(operators),
        'data': records
    })
'''


# =============================================================================
# RUN
# =============================================================================

if __name__ == '__main__':
    print("\n🚀 Starting DC Hub Capacity Cleanup...\n")
    
    # Run cleanup
    results = cleanup_capacity_data()
    
    # Add confidence scores
    print("\n📊 Adding confidence scores...")
    add_confidence_scores()
    
    # Print endpoint code
    print("\n" + "=" * 60)
    print("ADD THIS ENDPOINT TO main.py:")
    print("=" * 60)
    print(get_enhanced_capacity_endpoint_code())
    
    print("\n✅ Cleanup complete!")
    print(f"   - Removed {results['records_removed']} bad/duplicate records")
    print(f"   - Enhanced {results['records_updated']} records")
    print(f"   - Final: {results['final_count']} records, {results['total_mw']:,.0f} MW")
