-- ============================================================
-- DC Hub — Building-Level Granularity
-- ============================================================
-- Run: psql $DATABASE_URL -f buildings.sql
--
-- 1. Inserts individual buildings with facility_type='building'
-- 2. Updates parent campuses to facility_type='campus', power_mw=0
--    (MW now lives on buildings only — no double-count)
--
-- Query patterns:
--   Campus rollup:   WHERE facility_type = 'campus'
--   Building detail: WHERE facility_type = 'building'
--   Accurate MW:     SUM(power_mw) — no double-count since campus MW=0
-- ============================================================

-- META: PRINEVILLE, OR — 6 buildings (~60MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-pri-b1', 'Meta Prineville Building 1', 'Meta Platforms', 'Prineville', 'OR', 'US', 'NA', 10, 338000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-pri-b2', 'Meta Prineville Building 2', 'Meta Platforms', 'Prineville', 'OR', 'US', 'NA', 10, 338000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-pri-b3', 'Meta Prineville Building 3', 'Meta Platforms', 'Prineville', 'OR', 'US', 'NA', 10, 338000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-pri-b4', 'Meta Prineville Building 4', 'Meta Platforms', 'Prineville', 'OR', 'US', 'NA', 10, 338000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-pri-b5', 'Meta Prineville Building 5', 'Meta Platforms', 'Prineville', 'OR', 'US', 'NA', 10, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-pri-b6', 'Meta Prineville Building 6', 'Meta Platforms', 'Prineville', 'OR', 'US', 'NA', 10, 450000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: ALTOONA, IA — 7 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-alt-b1', 'Meta Altoona Building 1', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-alt-b2', 'Meta Altoona Building 2', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-alt-b3', 'Meta Altoona Building 3', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-alt-b4', 'Meta Altoona Building 4', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 25, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-alt-b5', 'Meta Altoona Building 5', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 25, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-alt-b6', 'Meta Altoona Building 6', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-alt-b7', 'Meta Altoona Building 7', 'Meta Platforms', 'Altoona', 'IA', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: FOREST CITY, NC — 3 buildings (~60MW, 1.3M sqft)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-fc-b1', 'Meta Forest City Building 1', 'Meta Platforms', 'Forest City', 'NC', 'US', 'NA', 20, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-fc-b2', 'Meta Forest City Building 2', 'Meta Platforms', 'Forest City', 'NC', 'US', 'NA', 20, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-fc-b3', 'Meta Forest City Building 3', 'Meta Platforms', 'Forest City', 'NC', 'US', 'NA', 20, 480000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: FORT WORTH, TX — 5 buildings (~150MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-fw-b1', 'Meta Fort Worth Building 1', 'Meta Platforms', 'Fort Worth', 'TX', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-fw-b2', 'Meta Fort Worth Building 2', 'Meta Platforms', 'Fort Worth', 'TX', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-fw-b3', 'Meta Fort Worth Building 3', 'Meta Platforms', 'Fort Worth', 'TX', 'US', 'NA', 30, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-fw-b4', 'Meta Fort Worth Building 4', 'Meta Platforms', 'Fort Worth', 'TX', 'US', 'NA', 30, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-fw-b5', 'Meta Fort Worth Building 5', 'Meta Platforms', 'Fort Worth', 'TX', 'US', 'NA', 30, 500000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: NEW ALBANY, OH — 5 buildings (~150MW, 2.5M sqft)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-na-b1', 'Meta New Albany Building 1', 'Meta Platforms', 'New Albany', 'OH', 'US', 'NA', 30, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-na-b2', 'Meta New Albany Building 2', 'Meta Platforms', 'New Albany', 'OH', 'US', 'NA', 30, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-na-b3', 'Meta New Albany Building 3', 'Meta Platforms', 'New Albany', 'OH', 'US', 'NA', 30, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-na-b4', 'Meta New Albany Building 4', 'Meta Platforms', 'New Albany', 'OH', 'US', 'NA', 30, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-na-b5', 'Meta New Albany Building 5', 'Meta Platforms', 'New Albany', 'OH', 'US', 'NA', 30, 450000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: PAPILLION, NE — 4 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-sar-b1', 'Meta Papillion Building 1', 'Meta Platforms', 'Papillion', 'NE', 'US', 'NA', 25, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-sar-b2', 'Meta Papillion Building 2', 'Meta Platforms', 'Papillion', 'NE', 'US', 'NA', 25, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-sar-b3', 'Meta Papillion Building 3', 'Meta Platforms', 'Papillion', 'NE', 'US', 'NA', 25, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-sar-b4', 'Meta Papillion Building 4', 'Meta Platforms', 'Papillion', 'NE', 'US', 'NA', 25, 476000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: HENRICO, VA — 3 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-hen-b1', 'Meta Henrico Building 1', 'Meta Platforms', 'Henrico', 'VA', 'US', 'NA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-hen-b2', 'Meta Henrico Building 2', 'Meta Platforms', 'Henrico', 'VA', 'US', 'NA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-hen-b3', 'Meta Henrico Building 3', 'Meta Platforms', 'Henrico', 'VA', 'US', 'NA', 34, 500000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: HUNTSVILLE, AL — 5 buildings (~150MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-hsv-b1', 'Meta Huntsville Building 1', 'Meta Platforms', 'Huntsville', 'AL', 'US', 'NA', 30, 485000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-hsv-b2', 'Meta Huntsville Building 2', 'Meta Platforms', 'Huntsville', 'AL', 'US', 'NA', 30, 485000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-hsv-b3', 'Meta Huntsville Building 3', 'Meta Platforms', 'Huntsville', 'AL', 'US', 'NA', 30, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-hsv-b4', 'Meta Huntsville Building 4', 'Meta Platforms', 'Huntsville', 'AL', 'US', 'NA', 30, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-hsv-b5', 'Meta Huntsville Building 5', 'Meta Platforms', 'Huntsville', 'AL', 'US', 'NA', 30, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: STANTON SPRINGS, GA — 5 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-ss-b1', 'Meta Stanton Springs Building 1', 'Meta Platforms', 'Social Circle', 'GA', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ss-b2', 'Meta Stanton Springs Building 2', 'Meta Platforms', 'Social Circle', 'GA', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ss-b3', 'Meta Stanton Springs Building 3', 'Meta Platforms', 'Social Circle', 'GA', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ss-b4', 'Meta Stanton Springs Building 4', 'Meta Platforms', 'Social Circle', 'GA', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ss-b5', 'Meta Stanton Springs Building 5', 'Meta Platforms', 'Social Circle', 'GA', 'US', 'NA', 40, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: LOS LUNAS, NM — 8 buildings (~140MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-ll-b1', 'Meta Los Lunas Building 1', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 20, 334000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b2', 'Meta Los Lunas Building 2', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 20, 334000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b3', 'Meta Los Lunas Building 3', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 20, 334000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b4', 'Meta Los Lunas Building 4', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 20, 334000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b5', 'Meta Los Lunas Building 5', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 15, 250000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b6', 'Meta Los Lunas Building 6', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 15, 250000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b7', 'Meta Los Lunas Building 7', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 15, 250000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ll-b8', 'Meta Los Lunas Building 8', 'Meta Platforms', 'Los Lunas', 'NM', 'US', 'NA', 15, 250000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: MESA, AZ — 5 buildings (~200MW, 2.5M sqft)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-mesa-b1', 'Meta Mesa Building 1', 'Meta Platforms', 'Mesa', 'AZ', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-mesa-b2', 'Meta Mesa Building 2', 'Meta Platforms', 'Mesa', 'AZ', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-mesa-b3', 'Meta Mesa Building 3', 'Meta Platforms', 'Mesa', 'AZ', 'US', 'NA', 40, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-mesa-b4', 'Meta Mesa Building 4', 'Meta Platforms', 'Mesa', 'AZ', 'US', 'NA', 40, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-mesa-b5', 'Meta Mesa Building 5', 'Meta Platforms', 'Mesa', 'AZ', 'US', 'NA', 40, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: GALLATIN, TN — 3 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-gal-b1', 'Meta Gallatin Building 1', 'Meta Platforms', 'Gallatin', 'TN', 'US', 'NA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-gal-b2', 'Meta Gallatin Building 2', 'Meta Platforms', 'Gallatin', 'TN', 'US', 'NA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-gal-b3', 'Meta Gallatin Building 3', 'Meta Platforms', 'Gallatin', 'TN', 'US', 'NA', 34, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: DEKALB, IL — 3 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-dk-b1', 'Meta DeKalb Building 1', 'Meta Platforms', 'DeKalb', 'IL', 'US', 'NA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-dk-b2', 'Meta DeKalb Building 2', 'Meta Platforms', 'DeKalb', 'IL', 'US', 'NA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-dk-b3', 'Meta DeKalb Building 3', 'Meta Platforms', 'DeKalb', 'IL', 'US', 'NA', 34, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: ODENSE, DK — 3 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-ode-b1', 'Meta Odense Building 1', 'Meta Platforms', 'Odense', NULL, 'DK', 'EMEA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ode-b2', 'Meta Odense Building 2', 'Meta Platforms', 'Odense', NULL, 'DK', 'EMEA', 33, 476000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-ode-b3', 'Meta Odense Building 3', 'Meta Platforms', 'Odense', NULL, 'DK', 'EMEA', 34, 476000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: LULEA, SE — 3 buildings (~120MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-lul-b1', 'Meta Lulea Building 1', 'Meta Platforms', 'Lulea', NULL, 'SE', 'EMEA', 40, 300000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-lul-b2', 'Meta Lulea Building 2', 'Meta Platforms', 'Lulea', NULL, 'SE', 'EMEA', 40, 300000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-lul-b3', 'Meta Lulea Building 3', 'Meta Platforms', 'Lulea', NULL, 'SE', 'EMEA', 40, 300000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: CLONEE, IE — 4 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-clo-b1', 'Meta Clonee Building 1', 'Meta Platforms', 'Clonee', NULL, 'IE', 'EMEA', 25, 310000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-clo-b2', 'Meta Clonee Building 2', 'Meta Platforms', 'Clonee', NULL, 'IE', 'EMEA', 25, 310000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-clo-b3', 'Meta Clonee Building 3', 'Meta Platforms', 'Clonee', NULL, 'IE', 'EMEA', 25, 310000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-clo-b4', 'Meta Clonee Building 4', 'Meta Platforms', 'Clonee', NULL, 'IE', 'EMEA', 25, 310000, 'active', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: EAGLE MOUNTAIN, UT — 2 buildings (~100MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-em-b1', 'Meta Eagle Mountain Building 1', 'Meta Platforms', 'Eagle Mountain', 'UT', 'US', 'NA', 50, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-em-b2', 'Meta Eagle Mountain Building 2', 'Meta Platforms', 'Eagle Mountain', 'UT', 'US', 'NA', 50, 500000, 'planned', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: KANSAS CITY, MO — 2 buildings (~150MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-kc-b1', 'Meta Kansas City Building 1', 'Meta Platforms', 'Kansas City', 'MO', 'US', 'NA', 75, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('meta-kc-b2', 'Meta Kansas City Building 2', 'Meta Platforms', 'Kansas City', 'MO', 'US', 'NA', 75, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: MONTGOMERY, AL — 2 buildings (~150MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-mont-b1', 'Meta Montgomery Building 1', 'Meta Platforms', 'Montgomery', 'AL', 'US', 'NA', 75, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-mont-b2', 'Meta Montgomery Building 2', 'Meta Platforms', 'Montgomery', 'AL', 'US', 'NA', 75, 500000, 'planned', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: HYPERION / RICHLAND PARISH, LA — 9 buildings (5GW, $10B)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-hyp-b1', 'Meta Hyperion Building 1', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b2', 'Meta Hyperion Building 2', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b3', 'Meta Hyperion Building 3', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b4', 'Meta Hyperion Building 4', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b5', 'Meta Hyperion Building 5', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b6', 'Meta Hyperion Building 6', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b7', 'Meta Hyperion Building 7', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b8', 'Meta Hyperion Building 8', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 555, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-hyp-b9', 'Meta Hyperion Building 9', 'Meta Platforms', 'Richland Parish', 'LA', 'US', 'NA', 560, 450000, 'planned', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: LEBANON, IN — 4 buildings (1GW, $10B)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-leb-b1', 'Meta Lebanon Building 1', 'Meta Platforms', 'Lebanon', 'IN', 'US', 'NA', 250, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-leb-b2', 'Meta Lebanon Building 2', 'Meta Platforms', 'Lebanon', 'IN', 'US', 'NA', 250, 500000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-leb-b3', 'Meta Lebanon Building 3', 'Meta Platforms', 'Lebanon', 'IN', 'US', 'NA', 250, 500000, 'planned', 'audit_bldg', '2026-02-22', 'building'),
('meta-leb-b4', 'Meta Lebanon Building 4', 'Meta Platforms', 'Lebanon', 'IN', 'US', 'NA', 250, 500000, 'planned', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- META: EL PASO, TX — 2 buildings (~150MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('meta-ep-b1', 'Meta El Paso Building 1', 'Meta Platforms', 'El Paso', 'TX', 'US', 'NA', 75, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building'),
('meta-ep-b2', 'Meta El Paso Building 2', 'Meta Platforms', 'El Paso', 'TX', 'US', 'NA', 75, 500000, 'planned', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;


-- GOOGLE: THE DALLES, OR — 4 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-td-b1', 'Google The Dalles Building 1', 'Google', 'The Dalles', 'OR', 'US', 'NA', 50, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-td-b2', 'Google The Dalles Building 2', 'Google', 'The Dalles', 'OR', 'US', 'NA', 50, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-td-b3', 'Google The Dalles Building 3', 'Google', 'The Dalles', 'OR', 'US', 'NA', 50, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-td-b4', 'Google The Dalles Building 4', 'Google', 'The Dalles', 'OR', 'US', 'NA', 50, 400000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: COUNCIL BLUFFS, IA — 5 buildings (~300MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-cb-b1', 'Google Council Bluffs Building 1', 'Google', 'Council Bluffs', 'IA', 'US', 'NA', 60, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-cb-b2', 'Google Council Bluffs Building 2', 'Google', 'Council Bluffs', 'IA', 'US', 'NA', 60, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-cb-b3', 'Google Council Bluffs Building 3', 'Google', 'Council Bluffs', 'IA', 'US', 'NA', 60, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-cb-b4', 'Google Council Bluffs Building 4', 'Google', 'Council Bluffs', 'IA', 'US', 'NA', 60, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-cb-b5', 'Google Council Bluffs Building 5', 'Google', 'Council Bluffs', 'IA', 'US', 'NA', 60, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: MIDLOTHIAN, TX — 3 buildings (~375MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-mid-b1', 'Google Midlothian Building 1', 'Google', 'Midlothian', 'TX', 'US', 'NA', 125, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-mid-b2', 'Google Midlothian Building 2', 'Google', 'Midlothian', 'TX', 'US', 'NA', 125, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-mid-b3', 'Google Midlothian Building 3', 'Google', 'Midlothian', 'TX', 'US', 'NA', 125, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: MESA, AZ — 3 buildings (~150MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-mesa-b1', 'Google Mesa Building 1', 'Google', 'Mesa', 'AZ', 'US', 'NA', 50, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-mesa-b2', 'Google Mesa Building 2', 'Google', 'Mesa', 'AZ', 'US', 'NA', 50, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-mesa-b3', 'Google Mesa Building 3', 'Google', 'Mesa', 'AZ', 'US', 'NA', 50, 400000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: HENDERSON, NV — 3 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-hend-b1', 'Google Henderson Building 1', 'Google', 'Henderson', 'NV', 'US', 'NA', 67, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-hend-b2', 'Google Henderson Building 2', 'Google', 'Henderson', 'NV', 'US', 'NA', 67, 450000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-hend-b3', 'Google Henderson Building 3', 'Google', 'Henderson', 'NV', 'US', 'NA', 66, 450000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: COLUMBUS, OH — 2 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-col-b1', 'Google Columbus Building 1', 'Google', 'Columbus', 'OH', 'US', 'NA', 100, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-col-b2', 'Google Columbus Building 2', 'Google', 'Columbus', 'OH', 'US', 'NA', 100, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: PAPILLION, NE — 2 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-pap-b1', 'Google Papillion Building 1', 'Google', 'Papillion', 'NE', 'US', 'NA', 100, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-pap-b2', 'Google Papillion Building 2', 'Google', 'Papillion', 'NE', 'US', 'NA', 100, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: MONCKS CORNER, SC — 3 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-mc-b1', 'Google Moncks Corner Building 1', 'Google', 'Moncks Corner', 'SC', 'US', 'NA', 67, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-mc-b2', 'Google Moncks Corner Building 2', 'Google', 'Moncks Corner', 'SC', 'US', 'NA', 67, 400000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-mc-b3', 'Google Moncks Corner Building 3', 'Google', 'Moncks Corner', 'SC', 'US', 'NA', 66, 400000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;

-- GOOGLE: KANSAS CITY, MO — 2 buildings (~200MW)
INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, sqft, status, source, last_updated, facility_type) VALUES
('goog-kc-b1', 'Google Kansas City Building 1', 'Google', 'Kansas City', 'MO', 'US', 'NA', 100, 500000, 'active', 'audit_bldg', '2026-02-22', 'building'),
('goog-kc-b2', 'Google Kansas City Building 2', 'Google', 'Kansas City', 'MO', 'US', 'NA', 100, 500000, 'construction', 'audit_bldg', '2026-02-22', 'building')
ON CONFLICT (name, city, country) DO NOTHING;


-- ═══════════════════════════════════════════════════════════
-- ZERO OUT CAMPUS MW & TAG facility_type
-- ═══════════════════════════════════════════════════════════

-- Tag all existing Meta/Google entries as campus
UPDATE facilities SET facility_type = 'campus'
WHERE provider IN ('Meta Platforms', 'Google') 
  AND facility_type IS DISTINCT FROM 'building';

-- Zero MW on Meta campuses that now have building detail
UPDATE facilities SET power_mw = 0
WHERE provider = 'Meta Platforms'
  AND facility_type = 'campus'
  AND city IN (
    'Prineville','Altoona','Forest City','Fort Worth','New Albany',
    'Papillion','Henrico','Huntsville','Social Circle','Los Lunas',
    'Mesa','Gallatin','DeKalb','Odense','Lulea','Clonee',
    'Eagle Mountain','Kansas City','Montgomery','Richland Parish',
    'Lebanon','El Paso'
  );

-- Zero MW on Google campuses that now have building detail
UPDATE facilities SET power_mw = 0
WHERE provider = 'Google'
  AND facility_type = 'campus'
  AND city IN (
    'The Dalles','Council Bluffs','Midlothian','Mesa','Henderson',
    'Columbus','Papillion','Moncks Corner','Kansas City'
  );


-- ═══════════════════════════════════════════════════════════
-- VERIFICATION
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '========================================='
\echo 'BUILDING-LEVEL GRANULARITY — RESULTS'
\echo '========================================='

\echo '--- Buildings inserted ---'
SELECT provider, COUNT(*) as buildings,
       ROUND(SUM(power_mw)::numeric) as mw,
       SUM(sqft) as sqft
FROM facilities WHERE source = 'audit_bldg'
GROUP BY provider ORDER BY buildings DESC;

\echo ''
\echo '--- Meta breakdown ---'
SELECT facility_type, COUNT(*) as n, ROUND(SUM(power_mw)::numeric) as mw
FROM facilities WHERE provider = 'Meta Platforms'
GROUP BY facility_type;

\echo ''
\echo '--- Google breakdown ---'
SELECT facility_type, COUNT(*) as n, ROUND(SUM(power_mw)::numeric) as mw
FROM facilities WHERE provider = 'Google'
GROUP BY facility_type;

\echo ''
\echo '--- Grand totals ---'
SELECT COUNT(*) as total,
       COUNT(*) FILTER (WHERE facility_type='building') as buildings,
       COUNT(*) FILTER (WHERE facility_type='campus') as campuses,
       ROUND(SUM(power_mw)::numeric) as total_mw
FROM facilities;
