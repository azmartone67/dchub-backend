"""
DC Hub - Deals Cleanup Script
Removes duplicates and invalid deals (buyer=seller)
"""

import sqlite3

DB_PATH = "dc_nexus.db"

def cleanup_deals():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()
    
    print("=" * 60)
    print("DC Hub Deals Cleanup")
    print("=" * 60)
    
    c.execute("SELECT COUNT(*) FROM deals")
    initial = c.fetchone()[0]
    print(f"\n📊 Initial: {initial} deals")
    
    # 1. Remove buyer = seller
    print("\n🔍 Removing invalid (buyer=seller)...")
    c.execute("SELECT id, buyer, seller FROM deals WHERE LOWER(TRIM(buyer)) = LOWER(TRIM(seller))")
    for d in c.fetchall():
        print(f"   ❌ {d[0]}: {d[1]} = {d[2]}")
    c.execute("DELETE FROM deals WHERE LOWER(TRIM(buyer)) = LOWER(TRIM(seller))")
    print(f"   Deleted: {c.rowcount}")
    
    # 2. Remove duplicates (keep oldest)
    print("\n🔍 Removing duplicates...")
    c.execute("""
        DELETE FROM deals WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM deals 
            GROUP BY LOWER(TRIM(buyer)), LOWER(TRIM(seller)), value
        )
    """)
    print(f"   Deleted: {c.rowcount}")
    
    conn.commit()
    
    c.execute("SELECT COUNT(*), ROUND(SUM(value)/1000,1) FROM deals")
    final, val = c.fetchone()
    
    print(f"\n✅ Final: {final} deals (${val}T)")
    print(f"🗑️  Removed: {initial - final} deals")
    
    c.execute("SELECT substr(date,1,4), COUNT(*) FROM deals GROUP BY 1 ORDER BY 1 DESC LIMIT 6")
    print("\n📅 By Year:")
    for y, cnt in c.fetchall():
        print(f"   {y}: {cnt}")
    
    conn.close()

if __name__ == '__main__':
    cleanup_deals()
