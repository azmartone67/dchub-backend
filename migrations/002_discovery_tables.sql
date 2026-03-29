-- ============================================================
-- DC Hub Discovery Platform — Neon Database Schema
-- Version: 1.0
-- Run this migration against your Neon database to create
-- all tables needed for the daily DC Hub data pipeline.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- 1. INTELLIGENCE INDEX
CREATE TABLE IF NOT EXISTS intelligence_index (
    id              SERIAL PRIMARY KEY,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pulse_score     NUMERIC(5,2),
    version         VARCHAR(10),
    agent_queries_24h INTEGER,
    active_integrations INTEGER,
    unique_facilities_queried_24h INTEGER,
    raw_json        JSONB,
    UNIQUE(fetched_at::date)
);
CREATE INDEX idx_intel_index_date ON intelligence_index (fetched_at DESC);

-- 2. NEWS ARTICLES
CREATE TABLE IF NOT EXISTS news_articles (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    source          VARCHAR(255),
    published_at    TIMESTAMPTZ,
    category        VARCHAR(100),
    summary         TEXT,
    url             TEXT,
    relevance_score NUMERIC(3,2),
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(title, published_at)
);
CREATE INDEX idx_news_published ON news_articles (published_at DESC);
CREATE INDEX idx_news_category ON news_articles (category);

-- 3. POWER INFRASTRUCTURE
CREATE TABLE IF NOT EXISTS infrastructure_power (
    id              SERIAL PRIMARY KEY,
    dchub_id        VARCHAR(255),
    type            VARCHAR(50) NOT NULL,
    name            TEXT,
    lat             NUMERIC(10,6),
    lon             NUMERIC(10,6),
    geom            GEOMETRY(Point, 4326),
    capacity_mw     NUMERIC(10,2),
    voltage_kv      NUMERIC(10,2),
    fuel_type       VARCHAR(100),
    operator        VARCHAR(255),
    source_market   VARCHAR(100),
    distance_km     NUMERIC(10,2),
    status          VARCHAR(50),
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(type, name, lat, lon)
);
CREATE INDEX idx_power_geom ON infrastructure_power USING GIST (geom);
CREATE INDEX idx_power_type ON infrastructure_power (type);
CREATE INDEX idx_power_market ON infrastructure_power (source_market);

CREATE OR REPLACE FUNCTION update_power_geom() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL THEN
        NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER trg_power_geom BEFORE INSERT OR UPDATE ON infrastructure_power FOR EACH ROW EXECUTE FUNCTION update_power_geom();

-- 4. GAS PIPELINES
CREATE TABLE IF NOT EXISTS infrastructure_gas (
    id              SERIAL PRIMARY KEY,
    dchub_id        VARCHAR(255),
    name            TEXT,
    operator        VARCHAR(255),
    lat             NUMERIC(10,6),
    lon             NUMERIC(10,6),
    geom            GEOMETRY(Point, 4326),
    diameter_inches NUMERIC(6,2),
    pressure_psi    NUMERIC(8,2),
    capacity        VARCHAR(100),
    source_market   VARCHAR(100),
    distance_km     NUMERIC(10,2),
    status          VARCHAR(50),
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, lat, lon)
);
CREATE INDEX idx_gas_geom ON infrastructure_gas USING GIST (geom);

CREATE OR REPLACE FUNCTION update_gas_geom() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL THEN
        NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER trg_gas_geom BEFORE INSERT OR UPDATE ON infrastructure_gas FOR EACH ROW EXECUTE FUNCTION update_gas_geom();

-- 5. FIBER ROUTES
CREATE TABLE IF NOT EXISTS infrastructure_fiber (
    id              SERIAL PRIMARY KEY,
    dchub_id        VARCHAR(255),
    carrier         VARCHAR(255),
    route_name      TEXT,
    route_type      VARCHAR(50),
    geojson         JSONB,
    geom            GEOMETRY(MultiLineString, 4326),
    distance_km     NUMERIC(10,2),
    endpoint_a      VARCHAR(255),
    endpoint_b      VARCHAR(255),
    lit_capacity    VARCHAR(100),
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(carrier, route_name)
);
CREATE INDEX idx_fiber_geom ON infrastructure_fiber USING GIST (geom);
CREATE INDEX idx_fiber_carrier ON infrastructure_fiber (carrier);

-- 6. GRID INTELLIGENCE
CREATE TABLE IF NOT EXISTS grid_intelligence (
    id              SERIAL PRIMARY KEY,
    region_id       VARCHAR(50) NOT NULL,
    corridors       JSONB,
    queue_congestion JSONB,
    energy_rates    JSONB,
    tax_incentives  JSONB,
    facility_count  INTEGER,
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(region_id, fetched_at::date)
);
CREATE INDEX idx_grid_region ON grid_intelligence (region_id);

-- 7. ENERGY PRICES
CREATE TABLE IF NOT EXISTS energy_prices (
    id              SERIAL PRIMARY KEY,
    state           VARCHAR(50),
    sector          VARCHAR(50),
    rate_cents_kwh  NUMERIC(8,4),
    data_type       VARCHAR(50),
    iso             VARCHAR(20),
    data_source     VARCHAR(255),
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_energy_state ON energy_prices (state);

-- 8. FACILITIES
CREATE TABLE IF NOT EXISTS facilities (
    id              SERIAL PRIMARY KEY,
    dchub_id        INTEGER UNIQUE,
    name            TEXT NOT NULL,
    provider        VARCHAR(255),
    city            VARCHAR(255),
    state           VARCHAR(50),
    country         VARCHAR(10),
    lat             NUMERIC(10,6),
    lon             NUMERIC(10,6),
    geom            GEOMETRY(Point, 4326),
    status          VARCHAR(50),
    capacity_mw     NUMERIC(10,2),
    pue             NUMERIC(4,2),
    tier_level      INTEGER,
    floor_space_sqft INTEGER,
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_facilities_geom ON facilities USING GIST (geom);
CREATE INDEX idx_facilities_state ON facilities (state);

CREATE OR REPLACE FUNCTION update_facility_geom() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL THEN
        NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER trg_facility_geom BEFORE INSERT OR UPDATE ON facilities FOR EACH ROW EXECUTE FUNCTION update_facility_geom();

-- 9. TRANSACTIONS
CREATE TABLE IF NOT EXISTS transactions (
    id              SERIAL PRIMARY KEY,
    buyer           VARCHAR(255),
    seller          VARCHAR(255),
    deal_date       DATE,
    deal_type       VARCHAR(100),
    value_usd       NUMERIC(15,2),
    region          VARCHAR(100),
    market          VARCHAR(255),
    assets          TEXT,
    raw_json        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(buyer, seller, deal_date)
);
CREATE INDEX idx_transactions_date ON transactions (deal_date DESC);

-- 10. DISCOVERY RUNS (audit log)
CREATE TABLE IF NOT EXISTS discovery_runs (
    id              SERIAL PRIMARY KEY,
    run_date        DATE NOT NULL UNIQUE,
    started_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ,
    status          VARCHAR(20) DEFAULT 'running',
    records_inserted JSONB,
    errors          JSONB,
    summary         TEXT
);

-- GEOJSON VIEWS FOR MAP API
CREATE OR REPLACE VIEW v_power_geojson AS
SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(json_build_object('type','Feature','geometry',ST_AsGeoJSON(geom)::json,'properties',json_build_object('id',id,'type',type,'name',name,'capacity_mw',capacity_mw,'voltage_kv',voltage_kv,'fuel_type',fuel_type,'operator',operator,'source_market',source_market,'status',status))),'[]'::json)) AS geojson FROM infrastructure_power WHERE geom IS NOT NULL;

CREATE OR REPLACE VIEW v_gas_geojson AS
SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(json_build_object('type','Feature','geometry',ST_AsGeoJSON(geom)::json,'properties',json_build_object('id',id,'name',name,'operator',operator,'diameter_inches',diameter_inches,'source_market',source_market,'status',status))),'[]'::json)) AS geojson FROM infrastructure_gas WHERE geom IS NOT NULL;

CREATE OR REPLACE VIEW v_fiber_geojson AS
SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(json_build_object('type','Feature','geometry',COALESCE(geojson->'geometry',ST_AsGeoJSON(geom)::jsonb),'properties',json_build_object('id',id,'carrier',carrier,'route_name',route_name,'route_type',route_type,'distance_km',distance_km,'endpoint_a',endpoint_a,'endpoint_b',endpoint_b))),'[]'::json)) AS geojson FROM infrastructure_fiber;

CREATE OR REPLACE VIEW v_facilities_geojson AS
SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(json_build_object('type','Feature','geometry',ST_AsGeoJSON(geom)::json,'properties',json_build_object('id',id,'dchub_id',dchub_id,'name',name,'provider',provider,'city',city,'state',state,'status',status,'capacity_mw',capacity_mw))),'[]'::json)) AS geojson FROM facilities WHERE geom IS NOT NULL;
