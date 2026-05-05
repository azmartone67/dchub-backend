"""
Update Permit Coordinates for DC Hub
=====================================
Run this script in your Replit shell:
  python update_permit_coords.py

This will add lat/lng coordinates to your existing permits.
"""

import sqlite3
import os

# Find the database
DB_PATH = 'dc_nexus.db'  # Main DC Hub database

# City coordinates lookup
CITY_COORDS = {
    'Temple': (31.0982, -97.3428),
    'Phoenix': (33.4484, -112.0740),
    'Columbus': (39.9612, -82.9988),
    'Manassas': (38.7509, -77.4753),
    'Goodyear': (33.4353, -112.3577),
    'Irving': (32.8140, -96.9489),
    'Hillsboro': (45.5229, -122.9898),
    'Leesburg': (39.1157, -77.5636),
    'Ashburn': (39.0438, -77.4874),
    'Dallas': (32.7767, -96.7970),
    'Mesa': (33.4152, -111.8315),
    'Chandler': (33.3062, -111.8413),
    'Atlanta': (33.7490, -84.3880),
    'Chicago': (41.8781, -87.6298),
    'Denver': (39.7392, -104.9903),
    'Las Vegas': (36.1699, -115.1398),
    'Salt Lake City': (40.7608, -111.8910),
    'San Jose': (37.3382, -121.8863),
    'Seattle': (47.6062, -122.3321),
    'Portland': (45.5152, -122.6784),
    'Sacramento': (38.5816, -121.4944),
    'San Antonio': (29.4241, -98.4936),
    'Austin': (30.2672, -97.7431),
    'Houston': (29.7604, -95.3698),
}

def update_coordinates():
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found: {DB_PATH}")
        print("   Checking for other .db files...")
        db_files = [f for f in os.listdir('.') if f.endswith('.db')]
        print(f"   Found: {db_files}")
        return
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # PRAGMA removed - not needed for PostgreSQL
    # PRAGMA removed - not needed for PostgreSQL
    cursor = conn.cursor()
    
    # Get all permits without coordinates
    cursor.execute("""
        SELECT id, project_name, city, state, lat, lng 
        FROM construction_permits 
        WHERE (lat IS NULL OR lng IS NULL) AND city IS NOT NULL
    """)
    
    permits = cursor.fetchall()
    print(f"📋 Found {len(permits)} permits without coordinates")
    
    updated = 0
    for permit in permits:
        id, name, city, state, lat, lng = permit
        
        if city in CITY_COORDS:
            new_lat, new_lng = CITY_COORDS[city]
            # Add slight randomness to prevent stacking
            import random
            new_lat += (random.random() - 0.5) * 0.02
            new_lng += (random.random() - 0.5) * 0.02
            
            cursor.execute("""
                UPDATE construction_permits 
                SET lat = %s, lng = %s 
                WHERE id = %s
            """, (new_lat, new_lng, id))
            
            print(f"✅ Updated: {name} ({city}, {state}) -> ({new_lat:.4f}, {new_lng:.4f})")
            updated += 1
        else:
            print(f"⚠️ No coords for: {name} ({city}, {state})")
    
    conn.commit()
    
    # Show results
    cursor.execute("""
        SELECT id, project_name, city, state, lat, lng, estimated_power_mw 
        FROM construction_permits 
        WHERE lat IS NOT NULL 
        ORDER BY estimated_power_mw DESC
    """)
    
    print(f"\n📊 Permits with coordinates:")
    print("-" * 80)
    for row in cursor.fetchall():
        id, name, city, state, lat, lng, mw = row
        print(f"  {name[:40]:<40} | {city}, {state} | {mw} MW | ({lat:.4f}, {lng:.4f})")
    
    conn.close()
    print(f"\n✅ Updated {updated} permits with coordinates")

if __name__ == '__main__':
    update_coordinates()
