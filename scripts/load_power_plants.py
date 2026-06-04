"""
load_power_plants.py — Load EIA Power Plants CSV into Neon
═══════════════════════════════════════════════════════════
Upload Power_Plants_in_the_US_*.csv to Railway workspace, then run:
  DATABASE_URL="postgresql://neondb_owner:...@ep-old-waterfall-aa2rwjzs-pooler.westus3.azure.neon.tech/neondb?sslmode=require" python load_power_plants.py

Creates power_plants_eia table with 13,400+ plants.
"""
import os, sys, csv, psycopg2

CSV_FILE = None
# Auto-find the CSV
for f in os.listdir('.'):
    if 'Power_Plants' in f and f.endswith('.csv'):
        CSV_FILE = f
        break
if not CSV_FILE:
    for f in os.listdir('/mnt/user-data/uploads/'):
        if 'Power_Plants' in f and f.endswith('.csv'):
            CSV_FILE = f'/mnt/user-data/uploads/{f}'
            break
if not CSV_FILE:
    print("❌ Power Plants CSV not found. Place it in current directory.")
    sys.exit(1)

print(f"📄 Using: {CSV_FILE}")

db_url = os.environ.get('DATABASE_URL', '')
if not db_url:
    print("❌ Set DATABASE_URL"); sys.exit(1)

conn = psycopg2.connect(db_url)
conn.autocommit = True
cur = conn.cursor()

# Create table
cur.execute("""
CREATE TABLE IF NOT EXISTS power_plants_eia (
    id SERIAL PRIMARY KEY,
    plant_id INTEGER,
    name VARCHAR(300),
    utility_name VARCHAR(300),
    sector VARCHAR(100),
    street VARCHAR(300),
    city VARCHAR(100),
    county VARCHAR(100),
    state VARCHAR(10),
    zipcode VARCHAR(20),
    primary_fuel VARCHAR(50),
    fuel_sources TEXT,
    technology VARCHAR(200),
    nameplate_capacity_mw REAL,
    max_output_mw REAL,
    battery_mw REAL,
    biomass_mw REAL,
    coal_mw REAL,
    geothermal_mw REAL,
    hydro_mw REAL,
    pumped_storage_mw REAL,
    natural_gas_mw REAL,
    nuclear_mw REAL,
    petroleum_mw REAL,
    solar_mw REAL,
    wind_mw REAL,
    other_mw REAL,
    source_survey VARCHAR(100),
    reporting_period VARCHAR(20),
    lat REAL,
    lng REAL,
    source VARCHAR(20) DEFAULT 'eia'
);
""")

# Create index
cur.execute("CREATE INDEX IF NOT EXISTS idx_power_plants_lat_lng ON power_plants_eia(lat, lng) WHERE lat IS NOT NULL;")
cur.execute("CREATE INDEX IF NOT EXISTS idx_power_plants_state ON power_plants_eia(state);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_power_plants_fuel ON power_plants_eia(primary_fuel);")

cur.execute("SELECT COUNT(*) FROM power_plants_eia")
before = cur.fetchone()[0]
print(f"📊 Before: {before}")

if before > 10000:
    print(f"⚠️ Already has {before} rows. Skipping. TRUNCATE first if you want to reload.")
    conn.close()
    sys.exit(0)

def safe_float(val):
    try:
        return float(val) if val and val.strip() else None
    except:
        return None

def safe_int(val):
    try:
        return int(float(val)) if val and val.strip() else None
    except:
        return None

inserted = 0
skipped = 0

with open(CSV_FILE, 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for row in reader:
        lat = safe_float(row.get('Latitude'))
        lng = safe_float(row.get('Longitude'))
        if not lat or not lng:
            skipped += 1
            continue
        try:
            cur.execute("""
                INSERT INTO power_plants_eia 
                (plant_id, name, utility_name, sector, street, city, county, state, zipcode,
                 primary_fuel, fuel_sources, technology, nameplate_capacity_mw, max_output_mw,
                 battery_mw, biomass_mw, coal_mw, geothermal_mw, hydro_mw, pumped_storage_mw,
                 natural_gas_mw, nuclear_mw, petroleum_mw, solar_mw, wind_mw, other_mw,
                 source_survey, reporting_period, lat, lng)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                safe_int(row.get('Plant identification number')),
                row.get('power Plant Name', '')[:300],
                row.get('Utility Name (day to day operations)', '')[:300],
                row.get('Plant-level sector name', '')[:100],
                row.get('Street address of power plant', '')[:300],
                row.get('City location of power plant', '')[:100],
                row.get('County location of power plant', '')[:100],
                row.get('State', '')[:10],
                row.get('Zip code of power plant', '')[:20],
                row.get('Primary Energy Source', '')[:50],
                row.get('Energy sources and corresponding net summer capacities for the power plant', '')[:500] if row.get('Energy sources and corresponding net summer capacities for the power plant') else None,
                row.get('Type(s) of technology (prime mover)', '')[:200],
                safe_float(row.get('Total combined generator nameplate capacity (installed)')),
                safe_float(row.get('Maximum output (MW)')),
                safe_float(row.get('Net summer capacity of battery powered electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of biomass electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of coal-fired electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of geothermal powered electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of hydroelectric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of pumped-storage hydroelectric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of natural gas fired electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of nuclear power electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of petroleum-fired electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of solar powered electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of wind turbine electric generators in megawatts (MW)')),
                safe_float(row.get('Net summer capacity of electric generators powered by other energy sources not specified in the other categories in megawatts (MW)')),
                row.get('The EIA source surveys for the power plants map data', '')[:100],
                row.get('The reporting period (currency) of the data (yyyymm)', '')[:20],
                lat, lng
            ))
            inserted += 1
            if inserted % 2000 == 0:
                print(f"  ... {inserted} inserted")
        except Exception as e:
            skipped += 1
            if skipped <= 3:
                print(f"  ERR: {e}")

cur.execute("SELECT COUNT(*) FROM power_plants_eia")
after = cur.fetchone()[0]

cur.execute("SELECT primary_fuel, COUNT(*), ROUND(SUM(COALESCE(nameplate_capacity_mw,0))::numeric) as total_mw FROM power_plants_eia GROUP BY primary_fuel ORDER BY count DESC LIMIT 10")
fuels = cur.fetchall()

cur.execute("SELECT COUNT(DISTINCT state) FROM power_plants_eia")
states = cur.fetchone()[0]

conn.close()

print(f"\n{'='*50}")
print(f"✅ Power Plants Load Complete!")
print(f"   Before: {before} | Inserted: {inserted} | Skipped: {skipped} | After: {after}")
print(f"   States: {states}")
print(f"\n   By fuel type:")
for fuel, count, mw in fuels:
    print(f"     {fuel}: {count} plants, {mw} MW")
