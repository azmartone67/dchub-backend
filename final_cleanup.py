"""
DC Hub - Final Cleanup
======================
Remove TBD deals and any remaining duplicates
"""

import sqlite3

DB_PATH = "dc_nexus.db"

def final_cleanup():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    c = conn.cursor()
    
    print("=" * 50)
    print("DC Hub - Final Cleanup")
    print("=" * 50)
    
    # Get current count
    c.execute("SELECT COUNT(*) FROM deals")
    before = c.fetchone()[0]
    print(f"\n📊 Deals before: {before}")
    
    # 1. Remove TBD/TBD deals
    print("\n🔍 Finding TBD deals...")
    c.execute("""
        SELECT id, buyer, seller, value FROM deals 
        WHERE LOWER(TRIM(buyer)) IN ('tbd', 'unknown', 'n/a', 'none', '')
           OR LOWER(TRIM(seller)) IN ('tbd', 'unknown', 'n/a', 'none', '')
    """)
    tbd_deals = c.fetchall()
    
    for row in tbd_deals:
        print(f"   ❌ TBD: {row[0]} ({row[1]} → {row[2]})")
    
    c.execute("""
        DELETE FROM deals 
        WHERE LOWER(TRIM(buyer)) IN ('tbd', 'unknown', 'n/a', 'none', '')
           OR LOWER(TRIM(seller)) IN ('tbd', 'unknown', 'n/a', 'none', '')
    """)
    tbd_deleted = c.rowcount
    print(f"   Removed {tbd_deleted} TBD deals")
    
    # 2. Remove buyer = seller deals
    print("\n🔍 Finding buyer=seller deals...")
    c.execute("""
        SELECT id, buyer, seller FROM deals 
        WHERE LOWER(TRIM(buyer)) = LOWER(TRIM(seller))
    """)
    same_deals = c.fetchall()
    
    for row in same_deals:
        print(f"   ❌ Same: {row[0]} ({row[1]} → {row[2]})")
    
    c.execute("DELETE FROM deals WHERE LOWER(TRIM(buyer)) = LOWER(TRIM(seller))")
    same_deleted = c.rowcount
    print(f"   Removed {same_deleted} buyer=seller deals")
    
    # 3. Find remaining SoftBank duplicates
    print("\n🔍 Finding SoftBank/DigitalBridge duplicates...")
    c.execute("""
        SELECT id, buyer, seller, value, date FROM deals 
        WHERE (LOWER(buyer) LIKE '%softbank%' AND LOWER(seller) LIKE '%digitalbridge%')
        ORDER BY date DESC
    """)
    softbank_deals = c.fetchall()
    
    if len(softbank_deals) > 1:
        print(f"   Found {len(softbank_deals)} SoftBank/DigitalBridge deals:")
        for row in softbank_deals:
            print(f"      • {row[0]}: {row[1]} → {row[2]} ({row[4]})")
        
        # Keep the first (most recent), delete the rest
        keep_id = softbank_deals[0][0]
        delete_ids = [d[0] for d in softbank_deals[1:]]
        
        print(f"   ✅ Keeping: {keep_id}")
        for del_id in delete_ids:
            print(f"   ❌ Deleting: {del_id}")
            c.execute("DELETE FROM deals WHERE id = %s", (del_id,))
    else:
        print("   No duplicates found")
    
    conn.commit()
    
    # Final count
    c.execute("SELECT COUNT(*) FROM deals")
    after = c.fetchone()[0]
    
    c.execute("SELECT ROUND(SUM(value)/1000, 1) FROM deals")
    total_value = c.fetchone()[0]
    
    print("\n" + "=" * 50)
    print(f"✅ Final count: {after} deals (removed {before - after})")
    print(f"💰 Total value: ${total_value}T")
    print("=" * 50)
    
    conn.close()

if __name__ == '__main__':
    final_cleanup()
