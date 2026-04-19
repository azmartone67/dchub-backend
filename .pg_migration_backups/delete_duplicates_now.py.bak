"""
DC Hub - Hardcoded Duplicate Removal
====================================
Delete specific duplicate IDs
"""

import sqlite3

DB_PATH = "dc_nexus.db"

# EXACT IDs to DELETE
DELETE_IDS = [
    # SoftBank/DigitalBridge duplicates - keep deal_ba65641978b635d5
    'AUTO-20260104-c8f6ab',
    'AUTO-20260104-fa8712',
    'AUTO-20260104-f7a0b4',
    '2025-MA-002',
    
    # Goodman/CPP duplicates - keep 2026-JV-e1d521
    'deal_b7a6424a836be3d6',
    
    # Google/Intersect duplicates - keep deal_d7803437b146b7d5
    '2026-MA-3a6fae',
]

def cleanup():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()
    
    print("=" * 50)
    print("Hardcoded Duplicate Removal")
    print("=" * 50)
    
    c.execute("SELECT COUNT(*) FROM deals")
    before = c.fetchone()[0]
    print(f"\nBefore: {before} deals")
    
    deleted = 0
    for deal_id in DELETE_IDS:
        c.execute("SELECT id, buyer, seller FROM deals WHERE id = ?", (deal_id,))
        row = c.fetchone()
        if row:
            print(f"❌ Deleting: {row[0]} ({row[1]} → {row[2]})")
            c.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
            deleted += 1
    
    # Also delete ANY auto-discovered SoftBank/DigitalBridge from today
    c.execute("""
        DELETE FROM deals 
        WHERE id LIKE 'AUTO-20260104%' 
        AND LOWER(buyer) LIKE '%softbank%' 
        AND LOWER(seller) LIKE '%digitalbridge%'
    """)
    auto_deleted = c.rowcount
    if auto_deleted > 0:
        print(f"❌ Deleted {auto_deleted} more AUTO SoftBank deals from today")
        deleted += auto_deleted
    
    conn.commit()
    
    c.execute("SELECT COUNT(*) FROM deals")
    after = c.fetchone()[0]
    
    print(f"\nAfter: {after} deals (removed {deleted})")
    
    # Show 2026 deals
    print("\n2026 Deals:")
    c.execute("SELECT buyer, seller, value FROM deals WHERE year=2026 ORDER BY date DESC")
    for row in c.fetchall():
        print(f"  • {row[0]} → {row[1]} (${row[2]}M)")
    
    conn.close()

if __name__ == '__main__':
    cleanup()
