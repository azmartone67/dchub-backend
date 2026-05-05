"""
DC Hub - Direct Duplicate Removal
=================================
Removes specific known duplicates by ID
"""

import sqlite3

DB_PATH = "dc_nexus.db"

# IDs to DELETE (keeping the better version of each)
DELETE_IDS = [
    # SoftBank/DigitalBridge - keep deal_ba65641978b635d5, delete others
    'AUTO-20260104-fa8712',  # SoftBank / DigitalBridge (duplicate, no MW)
    'AUTO-20260104-f7a0b4',  # If exists
    '2025-MA-002',           # SoftBank Group / DigitalBridge Group (older date)
    
    # Goodman/CPP - keep 2026-JV-e1d521 (has both parties named), delete other
    'deal_b7a6424a836be3d6', # Goodman Group/CPP / European DC Platform (bad seller name)
    
    # Google/Intersect - keep deal_d7803437b146b7d5 (has Alphabet/Google), delete other
    '2026-MA-3a6fae',        # Google / Intersect Power (duplicate)
]

def cleanup():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    c = conn.cursor()
    
    print("=" * 50)
    print("DC Hub - Direct Duplicate Removal")
    print("=" * 50)
    
    # Get current count
    c.execute("SELECT COUNT(*) FROM deals")
    before = c.fetchone()[0]
    print(f"\n📊 Deals before: {before}")
    
    deleted = 0
    for deal_id in DELETE_IDS:
        c.execute("SELECT id, buyer, seller, value FROM deals WHERE id = %s", (deal_id,))
        row = c.fetchone()
        if row:
            print(f"   ❌ Deleting: {row[0]} ({row[1]} → {row[2]}, ${row[3]}M)")
            c.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
            deleted += 1
        else:
            print(f"   ⏭️  Not found: {deal_id}")
    
    conn.commit()
    
    # Get final count
    c.execute("SELECT COUNT(*) FROM deals")
    after = c.fetchone()[0]
    
    # Show remaining 2026 deals
    print(f"\n📊 Deals after: {after} (removed {deleted})")
    
    print("\n📅 2026 Deals (should have no duplicates):")
    c.execute("SELECT id, buyer, seller, value FROM deals WHERE year = 2026 ORDER BY date DESC")
    for row in c.fetchall():
        print(f"   • {row[1]} → {row[2]} (${row[3]}M) [{row[0]}]")
    
    conn.close()
    print("\n✅ Done!")

if __name__ == '__main__':
    cleanup()
