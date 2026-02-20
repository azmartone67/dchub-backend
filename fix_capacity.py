"""
DC Hub Capacity Data Fix - Simple Version
==========================================
Run this in Replit: python fix_capacity.py

This will:
1. Delete duplicate records (keep only one per unique announcement)
2. Fix operator names extracted from notes
3. Fix market/region extraction
4. Remove non-data center content (solar, BESS, wind, hydro)
"""

import sqlite3
import re

DB_PATH = 'dc_nexus.db'

def fix_capacity_data():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    
    print("=" * 60)
    print("DC Hub Capacity Data Fix")
    print("=" * 60)
    
    # Step 1: Count current records
    c.execute('SELECT COUNT(*) FROM capacity_tracking')
    before_count = c.fetchone()[0]
    print(f"\n[1] Records before cleanup: {before_count}")
    
    # Step 2: Delete duplicates (keep lowest ID for each unique notes)
    print("\n[2] Removing duplicates...")
    c.execute('''
        DELETE FROM capacity_tracking 
        WHERE id NOT IN (
            SELECT MIN(id) 
            FROM capacity_tracking 
            GROUP BY notes
        )
    ''')
    dupes_removed = c.rowcount
    print(f"    Removed {dupes_removed} duplicate records")
    
    # Step 3: Delete non-data center content
    print("\n[3] Removing non-DC content (solar, BESS, wind, hydro)...")
    
    non_dc_patterns = [
        '%solar farm%', '%solar plant%', '%solar project%', '%solar in 202%',
        '%BESS%', '%battery energy storage%', '%grid-forming%',
        '%wind farm%', '%wind project%',
        '%pumped storage%', '%pumped hydro%',
        '%hydrogen electrolyzer%',
        '%thermal energy storage%',
        '%MWh of storage%', '%MWh storage%'
    ]
    
    for pattern in non_dc_patterns:
        c.execute(f"DELETE FROM capacity_tracking WHERE LOWER(notes) LIKE LOWER(?)", (pattern,))
    
    # Also remove by source
    c.execute("DELETE FROM capacity_tracking WHERE source = 'PV Magazine' AND notes NOT LIKE '%data center%' AND notes NOT LIKE '%data centre%'")
    c.execute("DELETE FROM capacity_tracking WHERE source = 'Energy Storage News' AND notes NOT LIKE '%data center%' AND notes NOT LIKE '%data centre%'")
    
    conn.commit()
    
    c.execute('SELECT COUNT(*) FROM capacity_tracking')
    after_filter = c.fetchone()[0]
    print(f"    Records after filtering: {after_filter}")
    
    # Step 4: Fix operator names
    print("\n[4] Fixing operator names...")
    
    operator_fixes = [
        # (pattern in notes, correct operator name)
        ('%Applied Digital%', 'Applied Digital'),
        ('%DayOne%', 'DayOne'),
        ('%Nebius%', 'Nebius'),
        ('%Google%', 'Google'),
        ('%Microsoft%', 'Microsoft'),
        ('%Amazon%', 'Amazon/AWS'),
        ('%AWS %', 'Amazon/AWS'),
        ('%Meta %', 'Meta'),
        ('%Facebook%', 'Meta'),
        ('%Equinix%', 'Equinix'),
        ('%Digital Realty%', 'Digital Realty'),
        ('%CyrusOne%', 'CyrusOne'),
        ('%QTS %', 'QTS'),
        ('%CoreSite%', 'CoreSite'),
        ('%Vantage%', 'Vantage'),
        ('%Switch %', 'Switch'),
        ('%NTT %', 'NTT'),
        ('%Chindata%', 'Chindata'),
        ('%GDS %', 'GDS'),
        ('%AirTrunk%', 'AirTrunk'),
        ('%NEXTDC%', 'NEXTDC'),
        ('%Interxion%', 'Interxion'),
        ('%Global Switch%', 'Global Switch'),
        ('%EdgeConneX%', 'EdgeConneX'),
        ('%CleanArc%', 'CleanArc'),
        ('%Crow Holdings%', 'Crow Holdings'),
        ('%Goodman%', 'Goodman'),
        ('%xAI%', 'xAI'),
        ('%Elon Musk%', 'xAI'),
        ('%G42 %', 'G42'),
        ('%CleanSpark%', 'CleanSpark'),
        ('%Soluna%', 'Soluna'),
        ('%Blueprint Data%', 'Blueprint Data Centers'),
        ('%Galaxy%Helios%', 'Galaxy Digital'),
        ('%Galaxy Completes%', 'Galaxy Digital'),
        ('%RadiusDC%', 'RadiusDC'),
        ('%WhiteFiber%', 'WhiteFiber'),
        ('%PACE %', 'PACE'),
        ('%ASP acquires%', 'ASP'),
        ('%TM Nxera%', 'TM Nxera'),
        ('%Compass Data%', 'Compass Datacenters'),
        ('%Stack Infrastructure%', 'Stack Infrastructure'),
        ('%Aligned%', 'Aligned'),
    ]
    
    for pattern, operator in operator_fixes:
        c.execute('''
            UPDATE capacity_tracking 
            SET operator = ? 
            WHERE LOWER(notes) LIKE LOWER(?) 
            AND (operator IS NULL OR operator = 'Unknown' OR operator NOT IN 
                ('Applied Digital', 'DayOne', 'Nebius', 'Google', 'Microsoft', 'Digital Realty', 
                 'CyrusOne', 'Equinix', 'QTS', 'Switch', 'Goodman', 'xAI', 'G42', 'CleanSpark',
                 'Soluna', 'Blueprint Data Centers', 'Galaxy Digital', 'WhiteFiber', 'CleanArc',
                 'Crow Holdings', 'PACE', 'ASP', 'TM Nxera', 'Compass Datacenters', 'Vantage',
                 'NTT', 'AirTrunk', 'NEXTDC', 'Interxion', 'EdgeConneX', 'RadiusDC'))
        ''', (operator, pattern))
    
    # Fix bad extractions (country names as operators)
    bad_operators = ['Helsinki', 'Canada', 'Austria', 'Norway', 'Uzbekistan', 'Australia', 
                     'Czechia', 'Australian', 'Former', 'Land Deal', 'Energy', 'Stargate',
                     'Weil Advises Crow Holdings', 'Consolidates Finland Expansion With',
                     'Soluna Expands Texas Campus With', 'Secures Landmark', 'Galaxy Completes',
                     'Tokyo Century', 'Rondo Energy', 'Masdar']
    
    for bad_op in bad_operators:
        c.execute("UPDATE capacity_tracking SET operator = NULL WHERE operator = ?", (bad_op,))
    
    conn.commit()
    
    # Step 5: Fix market/region
    print("\n[5] Fixing market and region...")
    
    market_fixes = [
        # (pattern in notes, market, region)
        ('%Dallas%Texas%', 'Dallas', 'North America'),
        ('%Texas%', 'Texas', 'North America'),
        ('%Austin%', 'Austin', 'North America'),
        ('%Houston%', 'Houston', 'North America'),
        ('%Phoenix%Arizona%', 'Phoenix', 'North America'),
        ('%Northern Virginia%', 'Northern Virginia', 'North America'),
        ('%Ashburn%', 'Northern Virginia', 'North America'),
        ('%Kansas City%', 'Kansas City', 'North America'),
        ('%Indianapolis%', 'Indianapolis', 'North America'),
        ('%Southern California%', 'Southern California', 'North America'),
        ('%Los Angeles%', 'Los Angeles', 'North America'),
        ('%Silicon Valley%', 'Silicon Valley', 'North America'),
        ('%Chicago%', 'Chicago', 'North America'),
        ('%Atlanta%', 'Atlanta', 'North America'),
        ('%Nevada%', 'Nevada', 'North America'),
        ('%Helsinki%Finland%', 'Helsinki', 'EMEA'),
        ('%Finland%', 'Finland', 'EMEA'),
        ('% UK %', 'United Kingdom', 'EMEA'),
        ('%United Kingdom%', 'United Kingdom', 'EMEA'),
        ('%Germany%', 'Germany', 'EMEA'),
        ('%Netherlands%', 'Netherlands', 'EMEA'),
        ('%Ireland%', 'Ireland', 'EMEA'),
        ('%Norway%', 'Norway', 'EMEA'),
        ('%Suldal%', 'Norway', 'EMEA'),
        ('%Sydney%Australia%', 'Sydney', 'APAC'),
        ('%Australia%', 'Australia', 'APAC'),
        ('%Kuala Lumpur%', 'Kuala Lumpur', 'APAC'),
        ('%Malaysia%', 'Malaysia', 'APAC'),
        ('%Singapore%', 'Singapore', 'APAC'),
        ('%Tokyo%Japan%', 'Tokyo', 'APAC'),
        ('%Japan%', 'Japan', 'APAC'),
        ('%South Korea%', 'South Korea', 'APAC'),
        ('%Canada%', 'Canada', 'North America'),
    ]
    
    for pattern, market, region in market_fixes:
        c.execute('''
            UPDATE capacity_tracking 
            SET market = ?, region = ?
            WHERE LOWER(notes) LIKE LOWER(?)
            AND (market IS NULL OR market = 'Unknown' OR market LIKE '%MW%' OR market LIKE '%Eyes%' OR market LIKE '%months%')
        ''', (market, region, pattern))
    
    # Clear bad market values
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%MW%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%Eyes%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%months%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%Flagship%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%secret%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market = 'AI'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%Helios%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%Expands%'")
    c.execute("UPDATE capacity_tracking SET market = NULL WHERE market LIKE '%five%'")
    
    conn.commit()
    
    # Step 6: Final stats
    print("\n" + "=" * 60)
    print("CLEANUP COMPLETE")
    print("=" * 60)
    
    c.execute('SELECT COUNT(*) FROM capacity_tracking')
    final_count = c.fetchone()[0]
    
    c.execute('SELECT SUM(capacity_mw) FROM capacity_tracking')
    total_mw = c.fetchone()[0] or 0
    
    c.execute('SELECT COUNT(DISTINCT operator) FROM capacity_tracking WHERE operator IS NOT NULL')
    operator_count = c.fetchone()[0]
    
    c.execute('SELECT COUNT(*) FROM capacity_tracking WHERE market IS NOT NULL AND market != "Unknown"')
    with_market = c.fetchone()[0]
    
    c.execute('SELECT COUNT(*) FROM capacity_tracking WHERE region IS NOT NULL AND region != "Unknown"')
    with_region = c.fetchone()[0]
    
    print(f"\nFinal record count: {final_count} (was {before_count})")
    print(f"Total pipeline MW: {total_mw:,.0f}")
    print(f"Records with operator: {operator_count}")
    print(f"Records with market: {with_market}")
    print(f"Records with region: {with_region}")
    
    # Show top operators
    print("\n" + "-" * 60)
    print("TOP OPERATORS BY MW:")
    print("-" * 60)
    
    c.execute('''
        SELECT operator, SUM(capacity_mw) as total_mw, COUNT(*) as count
        FROM capacity_tracking 
        WHERE operator IS NOT NULL
        GROUP BY operator 
        ORDER BY total_mw DESC 
        LIMIT 15
    ''')
    
    for row in c.fetchall():
        print(f"  {row[0]:25} | {row[1]:>8,.0f} MW | {row[2]} projects")
    
    # Show sample records
    print("\n" + "-" * 60)
    print("SAMPLE CLEANED RECORDS:")
    print("-" * 60)
    
    c.execute('''
        SELECT operator, capacity_mw, market, region, notes 
        FROM capacity_tracking 
        WHERE operator IS NOT NULL
        ORDER BY capacity_mw DESC 
        LIMIT 10
    ''')
    
    for row in c.fetchall():
        notes_short = (row[4] or '')[:50] + '...' if row[4] and len(row[4]) > 50 else row[4]
        print(f"  {row[0] or 'Unknown':20} | {row[1]:>6,.0f} MW | {row[2] or 'Unknown':15} | {row[3] or 'Unknown':12}")
    
    conn.close()
    
    return {
        'before': before_count,
        'after': final_count,
        'total_mw': total_mw,
        'operators': operator_count
    }


if __name__ == '__main__':
    print("\n🚀 Starting DC Hub Capacity Fix...\n")
    results = fix_capacity_data()
    print(f"\n✅ Done! Reduced from {results['before']} to {results['after']} records")
    print(f"   Total pipeline: {results['total_mw']:,.0f} MW across {results['operators']} operators")
