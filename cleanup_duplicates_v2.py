"""
DC Hub Database Cleanup v2 - Fuzzy Duplicate Detection
=======================================================
Handles variations like "SoftBank" vs "SoftBank Group"
"""

import sqlite3
from datetime import datetime

DB_PATH = "dc_nexus.db"

# Known name variations to normalize
NAME_MAPPINGS = {
    'softbank': 'SoftBank',
    'softbank group': 'SoftBank',
    'soft bank': 'SoftBank',
    'digitalbridge': 'DigitalBridge',
    'digitalbridge group': 'DigitalBridge',
    'digital bridge': 'DigitalBridge',
    'google': 'Google',
    'alphabet': 'Google',
    'alphabet/google': 'Google',
    'goodman': 'Goodman Group',
    'goodman group': 'Goodman Group',
    'goodman group/cpp': 'Goodman Group',
    'cpp': 'CPP Investments',
    'cpp investments': 'CPP Investments',
    'cppib': 'CPP Investments',
    'blackstone': 'Blackstone',
    'blackstone/cppib': 'Blackstone',
    'blackstone/gip/mgx': 'Blackstone',
    'kkr': 'KKR',
    'kkr/gip': 'KKR',
    'intersect': 'Intersect Power',
    'intersect power': 'Intersect Power',
}

def normalize_name(name):
    """Normalize company name for comparison"""
    if not name:
        return ''
    lower = name.lower().strip()
    return NAME_MAPPINGS.get(lower, name)

def cleanup_duplicates():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()
    
    print("=" * 60)
    print("DC Hub Cleanup v2 - Fuzzy Duplicate Detection")
    print("=" * 60)
    
    # Get all deals
    c.execute("SELECT id, buyer, seller, value, date, mw FROM deals ORDER BY date DESC")
    all_deals = c.fetchall()
    
    print(f"\n📊 Total deals: {len(all_deals)}")
    
    # Group by normalized key
    groups = {}
    for deal in all_deals:
        deal_id, buyer, seller, value, date, mw = deal
        
        # Create normalized key
        norm_buyer = normalize_name(buyer)
        norm_seller = normalize_name(seller)
        year_month = date[:7] if date else ''
        
        key = (norm_buyer.lower(), norm_seller.lower(), value, year_month)
        
        if key not in groups:
            groups[key] = []
        groups[key].append({
            'id': deal_id,
            'buyer': buyer,
            'seller': seller,
            'value': value,
            'date': date,
            'mw': mw
        })
    
    # Find duplicates
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    
    print(f"\n🔍 Found {len(duplicates)} duplicate groups:")
    
    deleted = 0
    for key, deals in duplicates.items():
        print(f"\n   📌 {key[0]} / {key[1]} - ${key[2]}M ({key[3]})")
        
        # Sort: prefer non-AUTO IDs, then by MW (more data = better)
        def score(d):
            s = 0
            if not d['id'].startswith('AUTO-'):
                s += 100
            if d['mw'] and d['mw'] > 0:
                s += 10
            return s
        
        deals_sorted = sorted(deals, key=score, reverse=True)
        
        # Keep best, delete rest
        keep = deals_sorted[0]
        delete = deals_sorted[1:]
        
        print(f"      ✅ KEEP: {keep['id']} ({keep['buyer']} → {keep['seller']})")
        
        for d in delete:
            print(f"      ❌ DELETE: {d['id']} ({d['buyer']} → {d['seller']})")
            c.execute("DELETE FROM deals WHERE id = ?", (d['id'],))
            deleted += 1
    
    conn.commit()
    
    # Final count
    c.execute("SELECT COUNT(*), ROUND(SUM(value)/1000, 1) FROM deals")
    total, value = c.fetchone()
    
    c.execute("""
        SELECT substr(date,1,4), COUNT(*), ROUND(SUM(value)/1000,1) 
        FROM deals GROUP BY substr(date,1,4) ORDER BY 1 DESC LIMIT 5
    """)
    by_year = c.fetchall()
    
    conn.close()
    
    print("\n" + "=" * 60)
    print("Cleanup Complete!")
    print("=" * 60)
    print(f"\n🗑️  Deleted: {deleted} duplicate deals")
    print(f"📊 Final count: {total} deals")
    print(f"💰 Total value: ${value}T")
    
    print("\n📅 By Year:")
    for y, cnt, val in by_year:
        print(f"   {y}: {cnt} deals (${val}B)")

if __name__ == '__main__':
    cleanup_duplicates()
