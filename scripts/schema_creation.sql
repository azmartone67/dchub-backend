-- ============================================================
-- DC Hub Infrastructure Expansion — Schema Creation
-- Run against Railway Neon (ep-old-waterfall pooler)
-- ============================================================

-- 1. MIDSTREAM GAS INFRASTRUCTURE
CREATE TABLE IF NOT EXISTS gas_compressor_stations (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    state TEXT,
    county TEXT,
    capacity_hp DOUBLE PRECISION,
    pipeline_name TEXT,
    source TEXT DEFAULT 'HIFLD',
    source_id TEXT,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS gas_processing_plants (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    state TEXT,
    county TEXT,
    capacity_mmcfd DOUBLE PRECISION,
    plant_type TEXT,
    source TEXT DEFAULT 'HIFLD',
    source_id TEXT,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS lng_terminals (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    state TEXT,
    county TEXT,
    capacity_bcfd DOUBLE PRECISION,
    terminal_type TEXT,  -- import, export, both
    status TEXT,
    source TEXT DEFAULT 'HIFLD',
    source_id TEXT,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(source, source_id)
);

-- 2. EIA PRICING (extends existing eia_retail_rates)
CREATE TABLE IF NOT EXISTS eia_electricity_rates (
    id SERIAL PRIMARY KEY,
    state TEXT NOT NULL,
    sector TEXT NOT NULL,  -- COM, IND, RES, ALL
    price_cents_kwh DOUBLE PRECISION,
    period TEXT,  -- YYYY-MM format
    source TEXT DEFAULT 'EIA',
    retrieved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(state, sector, period)
);

CREATE TABLE IF NOT EXISTS eia_natural_gas_prices (
    id SERIAL PRIMARY KEY,
    state TEXT,
    series_id TEXT,
    price_dollars_mcf DOUBLE PRECISION,
    period TEXT,
    sector TEXT,  -- citygate, industrial, commercial, electric_power
    source TEXT DEFAULT 'EIA',
    retrieved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(state, sector, period)
);

CREATE TABLE IF NOT EXISTS eia_gas_storage_weekly (
    id SERIAL PRIMARY KEY,
    region TEXT NOT NULL,
    working_gas_bcf DOUBLE PRECISION,
    net_change_bcf DOUBLE PRECISION,
    period TEXT,
    source TEXT DEFAULT 'EIA',
    retrieved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(region, period)
);

-- 3. FIBER / CONNECTIVITY
CREATE TABLE IF NOT EXISTS fcc_fiber_availability (
    id SERIAL PRIMARY KEY,
    state TEXT,
    county_fips TEXT,
    county_name TEXT,
    technology_code INTEGER,  -- 50=fiber
    max_download_mbps DOUBLE PRECISION,
    max_upload_mbps DOUBLE PRECISION,
    provider_count INTEGER,
    residential_coverage_pct DOUBLE PRECISION,
    business_coverage_pct DOUBLE PRECISION,
    source TEXT DEFAULT 'FCC_BDC',
    retrieved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(county_fips, technology_code)
);

CREATE TABLE IF NOT EXISTS peeringdb_ix_facilities (
    id SERIAL PRIMARY KEY,
    ix_id INTEGER,
    name TEXT,
    city TEXT,
    country TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    participants INTEGER,
    speed_gbps DOUBLE PRECISION,
    website TEXT,
    source TEXT DEFAULT 'PeeringDB',
    retrieved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ix_id)
);

CREATE TABLE IF NOT EXISTS peeringdb_network_facilities (
    id SERIAL PRIMARY KEY,
    facility_id INTEGER,
    network_id INTEGER,
    network_name TEXT,
    facility_name TEXT,
    city TEXT,
    country TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    source TEXT DEFAULT 'PeeringDB',
    retrieved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(facility_id, network_id)
);

-- Indexes for spatial queries
CREATE INDEX IF NOT EXISTS idx_compressor_geo ON gas_compressor_stations(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_processing_geo ON gas_processing_plants(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_lng_geo ON lng_terminals(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_elec_rates_state ON eia_electricity_rates(state, sector);
CREATE INDEX IF NOT EXISTS idx_gas_prices_state ON eia_natural_gas_prices(state, sector);
CREATE INDEX IF NOT EXISTS idx_fcc_fiber_county ON fcc_fiber_availability(county_fips);
CREATE INDEX IF NOT EXISTS idx_peeringdb_ix_geo ON peeringdb_ix_facilities(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_peeringdb_net_geo ON peeringdb_network_facilities(latitude, longitude);
