"""
EIA Form 860 Bulk Loader — Loads power plant data into Neon PostgreSQL
=====================================================================
Downloads and imports EIA-860 Schedule 2 (Plant) data into the power_plants_eia table.

Usage:
  python eia860_bulk_loader.py                    # Load from CSV file
  python eia860_bulk_loader.py --file 2___Plant.csv  # Specify file

Table: power_plants_eia
Columns: plant_id, name, utility_name, state, city, county, zipcode,
         lat, lng, primary_fuel, nameplate_capacity_mw, source, 
         plant_name, capacity_mw, energy_source (aliases)
"""

import csv
import os
import sys
import psycopg2
from datetime import datetime

DATABASE_URL = os.environ.get('DATABASE_URL', '')

def load_eia860(csv_path):
    """Load EIA-860 plant data from CSV into power_plants_eia table."""
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set")
        return
    
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # Set statement timeout
    cur.execute("SET statement_timeout = 60000")  # 60s
    
    loaded = 0
    skipped = 0
    errors = 0
    
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            try:
                # Map EIA fields to our table columns
                plant_id = row.get('Plant Code', row.get('plant_code', ''))
                name = row.get('Plant Name', row.get('plant_name', ''))
                utility = row.get('Utility Name', row.get('utility_name', ''))
                state = row.get('State', row.get('state', ''))
                city = row.get('City', row.get('city', ''))
                county = row.get('County', row.get('county', ''))
                zipcode = row.get('Zip', row.get('zip', row.get('Zip Code', '')))
                lat = row.get('Latitude', row.get('latitude', row.get('lat', '')))
                lng = row.get('Longitude', row.get('longitude', row.get('lng', row.get('lon', ''))))
                primary_fuel = row.get('Primary Technology', row.get('primary_fuel', row.get('Energy Source 1', '')))
                capacity = row.get('Nameplate Capacity (MW)', row.get('nameplate_capacity_mw', row.get('Nameplate Capacity', '')))
                sector = row.get('Sector Name', row.get('sector', ''))
                
                # Clean values
                try:
                    lat = float(lat) if lat and lat.strip() else None
                except (ValueError, TypeError):
                    lat = None
                try:
                    lng = float(lng) if lng and lng.strip() else None
                except (ValueError, TypeError):
                    lng = None
                try:
                    capacity = float(capacity) if capacity and str(capacity).strip() else None
                except (ValueError, TypeError):
                    capacity = None
                
                if not plant_id or not name:
                    skipped += 1
                    continue
                
                cur.execute("""
                    INSERT INTO power_plants_eia 
                    (plant_id, name, utility_name, state, city, county, zipcode,
                     lat, lng, primary_fuel, nameplate_capacity_mw, sector, source,
                     plant_name, capacity_mw, energy_source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (plant_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        utility_name = EXCLUDED.utility_name,
                        lat = EXCLUDED.lat,
                        lng = EXCLUDED.lng,
                        primary_fuel = EXCLUDED.primary_fuel,
                        nameplate_capacity_mw = EXCLUDED.nameplate_capacity_mw,
                        plant_name = EXCLUDED.name,
                        capacity_mw = EXCLUDED.nameplate_capacity_mw,
                        energy_source = EXCLUDED.primary_fuel
                """, (
                    plant_id, name, utility, state, city, county, zipcode,
                    lat, lng, primary_fuel, capacity, sector, 'eia_860',
                    name, capacity, primary_fuel
                ))
                loaded += 1
                
                if loaded % 500 == 0:
                    conn.commit()
                    print(f"  Loaded {loaded} plants...")
                    
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  Error on row: {e}")
                conn.rollback()
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"\n=== EIA-860 Load Complete ===")
    print(f"  Loaded: {loaded}")
    print(f"  Skipped: {skipped}")
    print(f"  Errors: {errors}")
    return loaded


if __name__ == '__main__':
    csv_file = sys.argv[1] if len(sys.argv) > 1 else '2___Plant_Y2023.csv'
    if not os.path.exists(csv_file):
        # Try to find any EIA plant CSV
        import glob
        candidates = glob.glob('*Plant*.csv') + glob.glob('*plant*.csv')
        if candidates:
            csv_file = candidates[0]
            print(f"Found: {csv_file}")
        else:
            print(f"ERROR: {csv_file} not found. Download from https://www.eia.gov/electricity/data/eia860/")
            sys.exit(1)
    
    print(f"Loading {csv_file}...")
    load_eia860(csv_file)
