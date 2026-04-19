CREATE TABLE IF NOT EXISTS gas_compressor_stations (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    state TEXT,
    county TEXT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    naics_code TEXT,
    source_id TEXT,
    source TEXT DEFAULT 'hifld',
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_compressors_state ON gas_compressor_stations(state);
CREATE INDEX IF NOT EXISTS idx_compressors_coords ON gas_compressor_stations(latitude, longitude);

CREATE TABLE IF NOT EXISTS gas_processing_plants (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    state TEXT,
    county TEXT,
    city TEXT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    capacity_mmcfd DOUBLE PRECISION,
    naics_code TEXT,
    source_id TEXT,
    source TEXT DEFAULT 'hifld',
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_processing_state ON gas_processing_plants(state);
CREATE INDEX IF NOT EXISTS idx_processing_coords ON gas_processing_plants(latitude, longitude);

CREATE TABLE IF NOT EXISTS lng_terminals (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    terminal_type TEXT,
    state TEXT,
    county TEXT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    capacity_bcfd DOUBLE PRECISION,
    status TEXT DEFAULT 'active',
    country TEXT DEFAULT 'US',
    source_id TEXT,
    source TEXT DEFAULT 'hifld',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lng_state ON lng_terminals(state);
CREATE INDEX IF NOT EXISTS idx_lng_coords ON lng_terminals(latitude, longitude);

CREATE TABLE IF NOT EXISTS gas_storage_facilities (
    id SERIAL PRIMARY KEY,
    name TEXT,
    operator TEXT,
    state TEXT,
    field_type TEXT,
    working_gas_capacity_mcf BIGINT,
    total_capacity_mcf BIGINT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    source TEXT DEFAULT 'eia',
    source_id TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_storage_state ON gas_storage_facilities(state);

CREATE TABLE IF NOT EXISTS eia_electricity_rates (
    id SERIAL PRIMARY KEY,
    state TEXT NOT NULL,
    state_name TEXT,
    sector TEXT NOT NULL,
    period TEXT NOT NULL,
    rate_cents_kwh DOUBLE PRECISION,
    revenue_thousand_dollars DOUBLE PRECISION,
    sales_mwh DOUBLE PRECISION,
    customers INTEGER,
    source TEXT DEFAULT 'eia_api_v2',
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(state, sector, period)
);
CREATE INDEX IF NOT EXISTS idx_elec_rates_state ON eia_electricity_rates(state);
CREATE INDEX IF NOT EXISTS idx_elec_rates_period ON eia_electricity_rates(period);

CREATE TABLE IF NOT EXISTS eia_natural_gas_prices (
    id SERIAL PRIMARY KEY,
    state TEXT,
    price_type TEXT NOT NULL,
    period TEXT NOT NULL,
    price_per_mcf DOUBLE PRECISION,
    source TEXT DEFAULT 'eia_api_v2',
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(state, price_type, period)
);
CREATE INDEX IF NOT EXISTS idx_ng_prices_state ON eia_natural_gas_prices(state);
CREATE INDEX IF NOT EXISTS idx_ng_prices_period ON eia_natural_gas_prices(period);

CREATE TABLE IF NOT EXISTS eia_gas_storage_weekly (
    id SERIAL PRIMARY KEY,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    working_gas_bcf DOUBLE PRECISION,
    net_change_bcf DOUBLE PRECISION,
    source TEXT DEFAULT 'eia_api_v2',
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(region, period)
);
CREATE INDEX IF NOT EXISTS idx_gas_storage_period ON eia_gas_storage_weekly(period);

CREATE TABLE IF NOT EXISTS fcc_fiber_availability (
    id SERIAL PRIMARY KEY,
    state TEXT NOT NULL,
    county_fips TEXT,
    county_name TEXT,
    total_locations INTEGER,
    fiber_locations INTEGER,
    fiber_pct DOUBLE PRECISION,
    cable_locations INTEGER,
    dsl_locations INTEGER,
    fixed_wireless_locations INTEGER,
    bdc_period TEXT,
    source TEXT DEFAULT 'fcc_bdc',
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(county_fips, bdc_period)
);
CREATE INDEX IF NOT EXISTS idx_fiber_avail_state ON fcc_fiber_availability(state);
CREATE INDEX IF NOT EXISTS idx_fiber_avail_county ON fcc_fiber_availability(county_fips);

CREATE TABLE IF NOT EXISTS peeringdb_ix_facilities (
    id SERIAL PRIMARY KEY,
    ix_id INTEGER,
    ix_name TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    participants INTEGER,
    speed_gbps DOUBLE PRECISION,
    website TEXT,
    source TEXT DEFAULT 'peeringdb',
    source_id TEXT,
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ix_id)
);
CREATE INDEX IF NOT EXISTS idx_ix_state ON peeringdb_ix_facilities(state);
CREATE INDEX IF NOT EXISTS idx_ix_coords ON peeringdb_ix_facilities(latitude, longitude);

CREATE TABLE IF NOT EXISTS peeringdb_network_facilities (
    id SERIAL PRIMARY KEY,
    facility_name TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    network_count INTEGER,
    ix_count INTEGER,
    source TEXT DEFAULT 'peeringdb',
    pdb_fac_id INTEGER,
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(pdb_fac_id)
);
CREATE INDEX IF NOT EXISTS idx_netfac_state ON peeringdb_network_facilities(state);
CREATE INDEX IF NOT EXISTS idx_netfac_coords ON peeringdb_network_facilities(latitude, longitude);

INSERT INTO scheduled_refreshes (task_name, schedule, last_run, status, notes)
VALUES
    ('midstream_hifld_refresh', 'monthly', NULL, 'pending', 'HIFLD compressors + processing + LNG'),
    ('eia_pricing_refresh', 'weekly', NULL, 'pending', 'EIA electricity rates + NG prices + storage'),
    ('fcc_fiber_refresh', 'quarterly', NULL, 'pending', 'FCC BDC fiber availability by county'),
    ('peeringdb_refresh', 'weekly', NULL, 'pending', 'PeeringDB IX + network facility data')
ON CONFLICT DO NOTHING;
