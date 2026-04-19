#!/usr/bin/env python3
"""
DC HUB - RAILWAY CLEANUP SCRIPT
===============================
Run this to remove all railway/metro entries from your database.

Usage:
  python cleanup_railways.py

This will DELETE entries matching railway patterns.
"""

import sqlite3
import re

DB_PATH = "dc_nexus.db"

# Patterns that indicate NON-datacenter entries
RAILWAY_PATTERNS = [
    # Exact company names
    'east japan railway',
    'west japan railway', 
    'central japan railway',
    'korea railroad',
    'korail',
    'seoul metro',
    'tokyo metro',
    'deutsche bahn',
    'db station',
    'db infrago',
    'db netz',
    'sncf',
    'société nationale des chemins de fer',
    'ns rail',
    'ns reizigers',
    'nederlandse spoorwegen',
    'trenitalia',
    'renfe',
    'nmbs',
    'sncb',
    'amtrak',
    'via rail',
    'eurostar',
    'thalys',
    'italo',
    
    # Generic patterns
    'railway',
    'railroad', 
    'rail company',
    'rail corp',
    'spoorwegen',
    'chemin de fer',
    'ferrocarril',
    'eisenbahn',
    'järnväg',
    'jernbane',
    'rautatie',
    
    # Metro/Transit
    'metro station',
    'subway',
    'underground station',
    'transit authority',
    'tramway',
    'tram depot',
    
    # Other non-DC
    'bus station',
    'bus terminal',
    'bus depot',
    'airport terminal',
    'airline',
    'hospital',
    'medical center',
    'university',
    'college campus',
    'school',
    'church',
    'mosque',
    'temple',
    'cathedral',
    'museum',
    'library',
    'theater',
    'theatre',
    'cinema',
    'hotel',
    'resort',
    'casino',
    'shopping mall',
    'retail',
    'supermarket',
    'bank branch',
    'post office',
    'fire station',
    'police station',
]

def cleanup_railways():
    print("🧹 DC Hub Railway Cleanup Script")
    print("=" * 50)
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    cursor = conn.cursor()
    
    # Get current count
    cursor.execute("SELECT COUNT(*) FROM facilities")
    before_count = cursor.fetchone()[0]
    print(f"📊 Facilities before cleanup: {before_count:,}")
    
    # Build deletion query
    total_deleted = 0
    
    for pattern in RAILWAY_PATTERNS:
        # Delete by name
        cursor.execute("""
            DELETE FROM facilities 
            WHERE LOWER(name) LIKE %s 
            OR LOWER(provider) LIKE %s
        """, (f'%{pattern}%', f'%{pattern}%'))
        
        deleted = cursor.rowcount
        if deleted > 0:
            total_deleted += deleted
            print(f"  ❌ Removed {deleted} entries matching '{pattern}'")
    
    conn.commit()
    
    # Get final count
    cursor.execute("SELECT COUNT(*) FROM facilities")
    after_count = cursor.fetchone()[0]
    
    print("=" * 50)
    print(f"📊 Facilities after cleanup: {after_count:,}")
    print(f"🗑️  Total removed: {total_deleted:,}")
    print(f"✅ Clean data centers: {after_count:,}")
    
    # Show top providers now
    print("\n📈 Top Providers (after cleanup):")
    cursor.execute("""
        SELECT provider, COUNT(*) as cnt 
        FROM facilities 
        WHERE provider != '' AND provider != 'Unknown'
        GROUP BY provider 
        ORDER BY cnt DESC 
        LIMIT 15
    """)
    
    for row in cursor.fetchall():
        print(f"  • {row[0]}: {row[1]}")
    
    conn.close()
    print("\n✅ Cleanup complete!")

if __name__ == "__main__":
    cleanup_railways()
