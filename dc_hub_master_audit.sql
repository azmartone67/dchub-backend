-- ============================================================
-- DC Hub — MASTER AUDIT FILE (All Phases Combined)
-- ============================================================
-- Run once:  psql $DATABASE_URL -f dc_hub_master_audit.sql
--
-- Phase 1: 68 missing hyperscaler + neocloud facilities
-- Phase 2: Provider normalization, false positive fixes, city backfill
-- Phase 3: 129 new operators (Compass, Stack, Yondr, AirTrunk, etc.)
--
-- Total: ~197 facility inserts + ~50 cleanup updates
-- All INSERTs use ON CONFLICT DO NOTHING — safe to re-run
-- All UPDATEs are idempotent
-- ============================================================


-- ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
-- PHASE 1: HYPERSCALER & NEOCLOUD GAP FILL (68 facilities)
-- ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░


-- ===================== META (17) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-prineville-or', 'Meta Prineville Data Center', 'Meta', 'Prineville', 'OR', 'US', 'NA', 60, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-huntsville-al', 'Meta Huntsville Data Center', 'Meta', 'Huntsville', 'AL', 'US', 'NA', 150, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-gallatin-tn', 'Meta Gallatin Data Center', 'Meta', 'Gallatin', 'TN', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-dekalb-il', 'Meta DeKalb Data Center', 'Meta', 'DeKalb', 'IL', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-temple-tx', 'Meta Temple Data Center', 'Meta', 'Temple', 'TX', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-kuna-id', 'Meta Kuna Data Center', 'Meta', 'Kuna', 'ID', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-cheyenne-wy', 'Meta Cheyenne Data Center', 'Meta', 'Cheyenne', 'WY', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-montgomery-al', 'Meta Montgomery Data Center', 'Meta', 'Montgomery', 'AL', 'US', 'NA', 150, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-jeffersonville-in', 'Meta Jeffersonville Data Center', 'Meta', 'Jeffersonville', 'IN', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-kc-mo', 'Meta Kansas City Data Center', 'Meta', 'Kansas City', 'MO', 'US', 'NA', 150, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-elpaso-tx', 'Meta El Paso Data Center', 'Meta', 'El Paso', 'TX', 'US', 'NA', 150, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-bowlinggreen-ky', 'Meta Bowling Green Data Center', 'Meta', 'Bowling Green', 'KY', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-beaverdam-wi', 'Meta Beaver Dam Data Center', 'Meta', 'Beaver Dam', 'WI', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-richland-la', 'Meta Richland Parish (Hyperion) Data Center', 'Meta', 'Richland Parish', 'LA', 'US', 'NA', 5000, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-lebanon-in', 'Meta Lebanon Data Center', 'Meta', 'Lebanon', 'IN', 'US', 'NA', 1000, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-rosemount-mn', 'Meta Rosemount Data Center', 'Meta', 'Rosemount', 'MN', 'US', 'NA', 100, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('meta-aiken-sc', 'Meta Aiken County Data Center', 'Meta', 'Aiken County', 'SC', 'US', 'NA', 100, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== GOOGLE (12) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-lenoir-nc', 'Google Lenoir Data Center', 'Google', 'Lenoir', 'NC', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-henderson-nv', 'Google Henderson Data Center', 'Google', 'Henderson', 'NV', 'US', 'NA', 200, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-papillion-ne', 'Google Papillion Data Center', 'Google', 'Papillion', 'NE', 'US', 'NA', 200, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-columbus-oh', 'Google Columbus Data Center', 'Google', 'Columbus', 'OH', 'US', 'NA', 200, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-kc-mo', 'Google Kansas City Data Center', 'Google', 'Kansas City', 'MO', 'US', 'NA', 200, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-kc2-mica-mo', 'Google Kansas City #2 (Project Mica)', 'Google', 'Kansas City', 'MO', 'US', 'NA', 400, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-sandsprings-ok', 'Google Sand Springs Data Center', 'Google', 'Sand Springs', 'OK', 'US', 'NA', 200, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-haskell-ok', 'Google Haskell County Data Center', 'Google', 'Haskell County', 'OK', 'US', 'NA', 300, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-eaglemtn-ut', 'Google Eagle Mountain Data Center', 'Google', 'Eagle Mountain', 'UT', 'US', 'NA', 200, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-stghislain-be', 'Google St. Ghislain Data Center', 'Google', 'Saint-Ghislain', NULL, 'BE', 'EMEA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-changhua-tw', 'Google Changhua Data Center', 'Google', 'Changhua County', NULL, 'TW', 'APAC', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('google-sydney-au', 'Google Sydney Data Center', 'Google', 'Sydney', NULL, 'AU', 'APAC', 50, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== NEBIUS (9) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-mantsala-fi', 'Nebius Mantsala Data Center', 'Nebius', 'Mantsala', NULL, 'FI', 'EMEA', 75, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-paris-fr', 'Nebius Paris (Equinix PA10)', 'Nebius', 'Paris', NULL, 'FR', 'EMEA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-kc-mo', 'Nebius Kansas City (Patmos)', 'Nebius', 'Kansas City', 'MO', 'US', 'NA', 40, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-keflavik-is', 'Nebius Keflavik Iceland', 'Nebius', 'Keflavik', NULL, 'IS', 'EMEA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-london-gb', 'Nebius London (Ark DC)', 'Nebius', 'London', NULL, 'GB', 'EMEA', 5, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-telaviv-il', 'Nebius Israel', 'Nebius', 'Tel Aviv', NULL, 'IL', 'EMEA', 5, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-vineland-nj', 'Nebius Vineland NJ (DataOne)', 'Nebius', 'Vineland', 'NJ', 'US', 'NA', 300, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-independence-mo', 'Nebius Independence MO (Eastgate)', 'Nebius', 'Independence', 'MO', 'US', 'NA', 800, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('nebius-bethune-fr', 'Nebius Bethune France', 'Nebius', 'Bethune', NULL, 'FR', 'EMEA', 240, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== TENSORWAVE (5) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('tw-tucson-az', 'TensorWave Tucson AZ (TECfusions)', 'TensorWave', 'Tucson', 'AZ', 'US', 'NA', 24.4, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('tw-pittston-pa', 'TensorWave Keystone Connect PA (TECfusions)', 'TensorWave', 'Pittston', 'PA', 'US', 'NA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('tw-clarksville-va', 'TensorWave Clarksville VA (TECfusions)', 'TensorWave', 'Clarksville', 'VA', 'US', 'NA', 5, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('tw-lasvegas-nv', 'TensorWave Las Vegas HQ', 'TensorWave', 'Las Vegas', 'NV', 'US', 'NA', 3, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('tw-jacksonville-fl', 'TensorWave Florida', 'TensorWave', 'Jacksonville', 'FL', 'US', 'NA', 5, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== CORE42 / G42 (6) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('core42-abudhabi-ae', 'Core42 Abu Dhabi HQ', 'Core42 (G42)', 'Abu Dhabi', NULL, 'AE', 'EMEA', 50, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('core42-barker-ny', 'Core42 Lake Mariner NY (TeraWulf)', 'Core42 (G42)', 'Barker', 'NY', 'US', 'NA', 70, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('core42-grenoble-fr', 'Core42 Grenoble France (DataOne)', 'Core42 (G42)', 'Grenoble', NULL, 'FR', 'EMEA', 0, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('core42-milan-it', 'Core42 Italy (Domyn)', 'Core42 (G42)', 'Milan', NULL, 'IT', 'EMEA', 0, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('core42-dublin-ie', 'Core42 Dublin European HQ', 'Core42 (G42)', 'Dublin', NULL, 'IE', 'EMEA', 0, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('core42-amman-jo', 'Core42 Jordan', 'Core42 (G42)', 'Amman', NULL, 'JO', 'EMEA', 0, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== LAMBDA (6) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('lambda-sf-ca', 'Lambda San Francisco (Colovore)', 'Lambda', 'San Francisco', 'CA', 'US', 'NA', 5, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('lambda-mv-ca', 'Lambda Mountain View (ECL)', 'Lambda', 'Mountain View', 'CA', 'US', 'NA', 5, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('lambda-allen-tx', 'Lambda Allen TX', 'Lambda', 'Allen', 'TX', 'US', 'NA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('lambda-atlanta-ga', 'Lambda Atlanta (EdgeConneX)', 'Lambda', 'Atlanta', 'GA', 'US', 'NA', 15, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('lambda-plano-tx', 'Lambda Plano TX (Aligned DFW-04)', 'Lambda', 'Plano', 'TX', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('lambda-chicago-il', 'Lambda Chicago (EdgeConneX)', 'Lambda', 'Chicago', 'IL', 'US', 'NA', 23, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== CRUSOE ENERGY (3) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('crusoe-midland-tx', 'Crusoe Energy Midland TX', 'Crusoe Energy', 'Midland', 'TX', 'US', 'NA', 20, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('crusoe-williston-nd', 'Crusoe Energy North Dakota', 'Crusoe Energy', 'Williston', 'ND', 'US', 'NA', 15, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('crusoe-rankin-tx', 'Crusoe Energy Upton County TX', 'Crusoe Energy', 'Rankin', 'TX', 'US', 'NA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== COREWEAVE (10 additional) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-edison-nj', 'CoreWeave Edison NJ', 'CoreWeave', 'Edison', 'NJ', 'US', 'NA', 20, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-secaucus-nj', 'CoreWeave Secaucus NJ', 'CoreWeave', 'Secaucus', 'NJ', 'US', 'NA', 15, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-ellendale-nd', 'CoreWeave Ellendale ND (Applied Digital)', 'CoreWeave', 'Ellendale', 'ND', 'US', 'NA', 250, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-denton-tx', 'CoreWeave Denton TX', 'CoreWeave', 'Denton', 'TX', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-quincy-wa', 'CoreWeave Quincy WA', 'CoreWeave', 'Quincy', 'WA', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-crawley-gb', 'CoreWeave Crawley UK (Digital Realty)', 'CoreWeave', 'Crawley', NULL, 'GB', 'EMEA', 20, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-london-gb', 'CoreWeave London Docklands (Global Switch)', 'CoreWeave', 'London', NULL, 'GB', 'EMEA', 15, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-barcelona-es', 'CoreWeave Barcelona Spain (Merlin)', 'CoreWeave', 'Barcelona', NULL, 'ES', 'EMEA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-stavanger-no', 'CoreWeave Stavanger Norway', 'CoreWeave', 'Stavanger', NULL, 'NO', 'EMEA', 50, 'planned', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('cw-dalton-ga', 'CoreWeave Dalton GA (Core Scientific)', 'CoreWeave', 'Dalton', 'GA', 'US', 'NA', 100, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;


-- ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
-- PHASE 2: PROVIDER NORMALIZATION & CLEANUP
-- ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░

-- AWS merges
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider = 'AWS';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider = 'Amazon.com, Inc.';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider = 'Amazon';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider LIKE 'Amazon Web Services%' AND provider != 'Amazon Web Services';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider LIKE 'Amazon SIN%';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider LIKE 'Amazon IAD%';

-- Meta merges
UPDATE facilities SET provider = 'Meta Platforms' WHERE provider = 'Meta';
UPDATE facilities SET provider = 'Meta Platforms' WHERE provider = 'Facebook';
UPDATE facilities SET provider = 'Meta Platforms' WHERE provider LIKE 'Meta Platforms%' AND provider != 'Meta Platforms';

-- Google merges
UPDATE facilities SET provider = 'Google' WHERE provider = 'Google LLC';
UPDATE facilities SET provider = 'Google' WHERE provider = 'Google Cloud';
UPDATE facilities SET provider = 'Google' WHERE provider LIKE 'Google Singapore%';
UPDATE facilities SET provider = 'Google' WHERE provider LIKE 'Google台灣%';

-- Microsoft
UPDATE facilities SET provider = 'Microsoft' WHERE provider = 'Microsoft Corporation';
UPDATE facilities SET provider = 'Microsoft' WHERE provider = 'Microsoft Azure';

-- Oracle
UPDATE facilities SET provider = 'Oracle' WHERE provider = 'Oracle Corporation';
UPDATE facilities SET provider = 'Oracle' WHERE provider = 'Oracle Cloud';

-- CyrusOne
UPDATE facilities SET provider = 'CyrusOne' WHERE provider LIKE 'CyrusOne%' AND provider != 'CyrusOne';

-- NTT / RagingWire
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider IN ('NTT', 'NTT Ltd', 'NTT Communications', 'NTT Global', 'RagingWire');
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider LIKE 'RagingWire%';

-- Flexential / Peak 10
UPDATE facilities SET provider = 'Flexential' WHERE provider IN ('Peak 10', 'Peak10');
UPDATE facilities SET provider = 'Flexential' WHERE provider LIKE 'Flexential%' AND provider != 'Flexential';

-- Digital Realty
UPDATE facilities SET provider = 'Digital Realty' WHERE provider IN ('Digital Realty Trust', 'DigitalRealty');
UPDATE facilities SET provider = 'Digital Realty' WHERE provider LIKE 'Digital Realty%' AND provider != 'Digital Realty';

-- Equinix
UPDATE facilities SET provider = 'Equinix' WHERE provider IN ('Equinix Inc', 'Equinix, Inc.');
UPDATE facilities SET provider = 'Equinix' WHERE provider LIKE 'Equinix%' AND provider != 'Equinix';

-- QTS
UPDATE facilities SET provider = 'QTS Realty Trust' WHERE provider IN ('QTS', 'QTS Data Centers');
UPDATE facilities SET provider = 'QTS Realty Trust' WHERE provider LIKE 'QTS%' AND provider != 'QTS Realty Trust';

-- Vantage
UPDATE facilities SET provider = 'Vantage Data Centers' WHERE provider = 'Vantage';
UPDATE facilities SET provider = 'Vantage Data Centers' WHERE provider LIKE 'Vantage%' AND provider != 'Vantage Data Centers';

-- DataBank
UPDATE facilities SET provider = 'DataBank' WHERE provider IN ('DataBank, Ltd.', 'DataBank, Ltd', 'Data Bank');
UPDATE facilities SET provider = 'DataBank' WHERE provider LIKE 'DataBank%' AND provider != 'DataBank';

-- EdgeConneX
UPDATE facilities SET provider = 'EdgeConneX' WHERE provider LIKE 'EdgeConneX%' AND provider != 'EdgeConneX';
UPDATE facilities SET provider = 'EdgeConneX' WHERE provider = 'Edge ConneX';

-- Iron Mountain
UPDATE facilities SET provider = 'Iron Mountain Data Centers' WHERE provider = 'Iron Mountain';
UPDATE facilities SET provider = 'Iron Mountain Data Centers' WHERE provider LIKE 'Iron Mountain%' AND provider != 'Iron Mountain Data Centers';

-- Switch
UPDATE facilities SET provider = 'Switch' WHERE provider IN ('Switch, Ltd', 'Switch Ltd');

-- Colt DCS
UPDATE facilities SET provider = 'Colt DCS' WHERE provider IN ('Colt', 'Colt Data Centre Services');

-- xAI cleanup
UPDATE facilities SET provider = 'xAI' WHERE provider = 'xAI Colossus';

-- Metanet false positive fix
UPDATE facilities SET provider = 'Metanet Communications' WHERE name LIKE 'Metanet%' AND provider IN ('Meta Platforms', 'Meta');

-- xAI MW fix
UPDATE facilities SET power_mw = 150 WHERE provider = 'xAI' AND city = 'Memphis' AND power_mw = 0;

-- City backfill for Meta
UPDATE facilities SET city = 'Luleå', country = 'SE' WHERE name LIKE 'Meta%datacenter%Luleå%' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Altoona', state = 'IA', country = 'US' WHERE name LIKE '%Altoona%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Forest City', state = 'NC', country = 'US' WHERE name LIKE '%Forest City%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Fort Worth', state = 'TX', country = 'US' WHERE name LIKE '%Fort Worth%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Clonee', country = 'IE' WHERE name LIKE '%Clonee%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Odense', country = 'DK' WHERE name LIKE '%Odense%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'New Albany', state = 'OH', country = 'US' WHERE name LIKE '%New Albany%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Papillion', state = 'NE', country = 'US' WHERE name LIKE '%Sarpy%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Henrico', state = 'VA', country = 'US' WHERE name LIKE '%Henrico%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');

-- City backfill for Google
UPDATE facilities SET city = 'The Dalles', state = 'OR', country = 'US' WHERE name LIKE '%Dalles%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Council Bluffs', state = 'IA', country = 'US' WHERE name LIKE '%Council Bluffs%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Moncks Corner', state = 'SC', country = 'US' WHERE name LIKE '%Berkeley County%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Midlothian', state = 'TX', country = 'US' WHERE name LIKE '%Douglas County%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Eemshaven', country = 'NL' WHERE name LIKE '%Eemshaven%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Middenmeer', country = 'NL' WHERE name LIKE '%Middenmeer%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Fredericia', country = 'DK' WHERE name LIKE '%Fredericia%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Hamina', country = 'FI' WHERE name LIKE '%Hamina%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Waltham Cross', country = 'GB' WHERE name LIKE '%Waltham Cross%' AND provider = 'Google' AND (city IS NULL OR city = '');

-- Status normalization
UPDATE facilities SET status = 'active' WHERE LOWER(status) IN ('operational');
UPDATE facilities SET status = 'planned' WHERE LOWER(status) IN ('planning', 'announced');
UPDATE facilities SET status = 'construction' WHERE LOWER(status) IN ('under construction');


-- ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
-- PHASE 3: NEW OPERATORS + INTERNATIONAL (129 facilities)
-- ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░


-- ===================== COMPASS DATACENTERS (17) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('compass-allen-tx', 'Compass Datacenters Allen TX', 'Compass Datacenters', 'Allen', 'TX', 'US', 'NA', 36, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-redoak-tx', 'Compass Datacenters Red Oak TX Campus', 'Compass Datacenters', 'Red Oak', 'TX', 'US', 'NA', 360, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-raleigh-nc', 'Compass Datacenters Raleigh NC', 'Compass Datacenters', 'Raleigh', 'NC', 'US', 'NA', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-loudoun-va', 'Compass Datacenters Loudoun County VA', 'Compass Datacenters', 'Ashburn', 'VA', 'US', 'NA', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-goodyear-az', 'Compass Datacenters Goodyear AZ Campus', 'Compass Datacenters', 'Goodyear', 'AZ', 'US', 'NA', 212, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-elmirage-az', 'Compass Datacenters El Mirage AZ', 'Compass Datacenters', 'El Mirage', 'AZ', 'US', 'NA', 108, 'construction', 'audit_2026_p3', '2026-02-22'),
('compass-tulsa-ok', 'Compass Datacenters Tulsa OK', 'Compass Datacenters', 'Tulsa', 'OK', 'US', 'NA', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-toronto-ca', 'Compass Datacenters Toronto', 'Compass Datacenters', 'Toronto', NULL, 'CA', 'NA', 50, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-montreal-ca', 'Compass Datacenters Montreal', 'Compass Datacenters', 'Montreal', NULL, 'CA', 'NA', 50, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-telaviv-il', 'Compass Datacenters Tel Aviv', 'Compass Datacenters', 'Tel Aviv', NULL, 'IL', 'EMEA', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-milan-it', 'Compass Datacenters Milan', 'Compass Datacenters', 'Milan', NULL, 'IT', 'EMEA', 50, 'construction', 'audit_2026_p3', '2026-02-22'),
('compass-meridian-ms', 'Compass Datacenters Meridian MS Campus', 'Compass Datacenters', 'Meridian', 'MS', 'US', 'NA', 500, 'construction', 'audit_2026_p3', '2026-02-22'),
('compass-hoffman-il', 'Compass Datacenters Hoffman Estates IL', 'Compass Datacenters', 'Hoffman Estates', 'IL', 'US', 'NA', 200, 'construction', 'audit_2026_p3', '2026-02-22'),
('compass-columbus-oh', 'Compass Datacenters Columbus OH', 'Compass Datacenters', 'Columbus', 'OH', 'US', 'NA', 50, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-boston-ma', 'Compass Datacenters Boston', 'Compass Datacenters', 'Boston', 'MA', 'US', 'NA', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('compass-statesville-nc', 'Compass Datacenters Statesville NC', 'Compass Datacenters', 'Statesville', 'NC', 'US', 'NA', 100, 'construction', 'audit_2026_p3', '2026-02-22'),
('compass-pwc-va', 'Compass Datacenters Prince William County VA', 'Compass Datacenters', 'Bristow', 'VA', 'US', 'NA', 300, 'construction', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== STACK INFRASTRUCTURE (23) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('stack-nva01-va', 'STACK NVA01 Campus', 'STACK Infrastructure', 'Manassas', 'VA', 'US', 'NA', 130, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-nva02-va', 'STACK NVA02 Campus', 'STACK Infrastructure', 'Manassas', 'VA', 'US', 'NA', 200, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-nva05-va', 'STACK NVA05 Campus', 'STACK Infrastructure', 'Manassas', 'VA', 'US', 'NA', 200, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-nva06-va', 'STACK NVA06 Leesburg', 'STACK Infrastructure', 'Leesburg', 'VA', 'US', 'NA', 100, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-stafford-va', 'STACK Stafford Technology Campus', 'STACK Infrastructure', 'Stafford', 'VA', 'US', 'NA', 1000, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-atl01-ga', 'STACK ATL01 Campus', 'STACK Infrastructure', 'Atlanta', 'GA', 'US', 'NA', 72, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-chi01-il', 'STACK CHI01 Campus', 'STACK Infrastructure', 'Chicago', 'IL', 'US', 'NA', 48, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-dfw01-tx', 'STACK DFW01 Campus', 'STACK Infrastructure', 'Dallas', 'TX', 'US', 'NA', 60, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-por01-or', 'STACK POR01 Campus', 'STACK Infrastructure', 'Hillsboro', 'OR', 'US', 'NA', 36, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-svy01-ca', 'STACK SVY01 Silicon Valley', 'STACK Infrastructure', 'San Jose', 'CA', 'US', 'NA', 36, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-svy03-ca', 'STACK SVY03 Hayward', 'STACK Infrastructure', 'Hayward', 'CA', 'US', 'NA', 77, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-santaclara-ca', 'STACK Santa Clara', 'STACK Infrastructure', 'Santa Clara', 'CA', 'US', 'NA', 50, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-newalbany-oh', 'STACK New Albany OH', 'STACK Infrastructure', 'New Albany', 'OH', 'US', 'NA', 100, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-phx-az', 'STACK Phoenix Campus', 'STACK Infrastructure', 'Phoenix', 'AZ', 'US', 'NA', 80, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-donaana-nm', 'STACK Dona Ana County NM (Stargate)', 'STACK Infrastructure', 'Las Cruces', 'NM', 'US', 'NA', 4500, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-mil01-it', 'STACK MIL01 Siziano', 'STACK Infrastructure', 'Siziano', NULL, 'IT', 'EMEA', 40, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-sto01-se', 'STACK STO01 Stockholm', 'STACK Infrastructure', 'Upplands Vasby', NULL, 'SE', 'EMEA', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-osl03-no', 'STACK OSL03 Oslo', 'STACK Infrastructure', 'Fetsund', NULL, 'NO', 'EMEA', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-tok01-jp', 'STACK TOK01 Tokyo', 'STACK Infrastructure', 'Inzai', NULL, 'JP', 'APAC', 36, 'active', 'audit_2026_p3', '2026-02-22'),
('stack-kix01-jp', 'STACK KIX01 Osaka', 'STACK Infrastructure', 'Osaka', NULL, 'JP', 'APAC', 30, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-mel-au', 'STACK Melbourne', 'STACK Infrastructure', 'Melbourne', NULL, 'AU', 'APAC', 50, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-jhr-my', 'STACK Johor Bahru', 'STACK Infrastructure', 'Johor Bahru', NULL, 'MY', 'APAC', 50, 'construction', 'audit_2026_p3', '2026-02-22'),
('stack-sel-kr', 'STACK Seoul', 'STACK Infrastructure', 'Seoul', NULL, 'KR', 'APAC', 40, 'construction', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== YONDR GROUP (8) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('yondr-arcola-va', 'Yondr Loudoun County VA Campus', 'Yondr Group', 'Arcola', 'VA', 'US', 'NA', 96, 'active', 'audit_2026_p3', '2026-02-22'),
('yondr-bristow-va', 'Yondr Bristow VA Campus', 'Yondr Group', 'Bristow', 'VA', 'US', 'NA', 60, 'construction', 'audit_2026_p3', '2026-02-22'),
('yondr-lancaster-tx', 'Yondr Lancaster TX Campus', 'Yondr Group', 'Lancaster', 'TX', 'US', 'NA', 550, 'construction', 'audit_2026_p3', '2026-02-22'),
('yondr-toronto-ca', 'Yondr Toronto', 'Yondr Group', 'Toronto', NULL, 'CA', 'NA', 27, 'active', 'audit_2026_p3', '2026-02-22'),
('yondr-slough-gb', 'Yondr London Slough Campus', 'Yondr Group', 'Slough', NULL, 'GB', 'EMEA', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('yondr-frankfurt-de', 'Yondr Frankfurt Bischofsheim', 'Yondr Group', 'Bischofsheim', NULL, 'DE', 'EMEA', 40, 'active', 'audit_2026_p3', '2026-02-22'),
('yondr-amsterdam-nl', 'Yondr Amsterdam Middenmeer', 'Yondr Group', 'Middenmeer', NULL, 'NL', 'EMEA', 30, 'construction', 'audit_2026_p3', '2026-02-22'),
('yondr-berlin-de', 'Yondr Berlin-Ragow', 'Yondr Group', 'Mittenwalde', NULL, 'DE', 'EMEA', 30, 'construction', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== APPLIED DIGITAL (3) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('appdig-ellendale-nd', 'Applied Digital Ellendale ND', 'Applied Digital', 'Ellendale', 'ND', 'US', 'NA', 400, 'active', 'audit_2026_p3', '2026-02-22'),
('appdig-jamestown-nd', 'Applied Digital Jamestown ND', 'Applied Digital', 'Jamestown', 'ND', 'US', 'NA', 200, 'active', 'audit_2026_p3', '2026-02-22'),
('appdig-garden-nd', 'Applied Digital Garden City KS', 'Applied Digital', 'Garden City', 'KS', 'US', 'NA', 100, 'construction', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== CLOUDHQ (5) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('cloudhq-ashburn-va', 'CloudHQ Ashburn VA', 'CloudHQ', 'Ashburn', 'VA', 'US', 'NA', 200, 'active', 'audit_2026_p3', '2026-02-22'),
('cloudhq-manassas-va', 'CloudHQ Manassas VA', 'CloudHQ', 'Manassas', 'VA', 'US', 'NA', 150, 'active', 'audit_2026_p3', '2026-02-22'),
('cloudhq-atlanta-ga', 'CloudHQ Atlanta GA', 'CloudHQ', 'Atlanta', 'GA', 'US', 'NA', 36, 'active', 'audit_2026_p3', '2026-02-22'),
('cloudhq-london-gb', 'CloudHQ London', 'CloudHQ', 'London', NULL, 'GB', 'EMEA', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('cloudhq-frankfurt-de', 'CloudHQ Frankfurt', 'CloudHQ', 'Frankfurt', NULL, 'DE', 'EMEA', 50, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== STREAM DATA CENTERS (4) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('stream-phoenix-az', 'Stream Data Centers Phoenix', 'Stream Data Centers', 'Phoenix', 'AZ', 'US', 'NA', 80, 'active', 'audit_2026_p3', '2026-02-22'),
('stream-dallas-tx', 'Stream Data Centers Dallas', 'Stream Data Centers', 'Dallas', 'TX', 'US', 'NA', 60, 'active', 'audit_2026_p3', '2026-02-22'),
('stream-chicago-il', 'Stream Data Centers Chicago', 'Stream Data Centers', 'Chicago', 'IL', 'US', 'NA', 40, 'active', 'audit_2026_p3', '2026-02-22'),
('stream-sanantonio-tx', 'Stream Data Centers San Antonio', 'Stream Data Centers', 'San Antonio', 'TX', 'US', 'NA', 30, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== AI CHIP COMPANIES (4) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('groq-santa-clara', 'Groq Santa Clara HQ', 'Groq', 'Santa Clara', 'CA', 'US', 'NA', 5, 'active', 'audit_2026_p3', '2026-02-22'),
('cerebras-santaclara', 'Cerebras Systems Santa Clara', 'Cerebras', 'Santa Clara', 'CA', 'US', 'NA', 5, 'active', 'audit_2026_p3', '2026-02-22'),
('sambanova-paloalto', 'SambaNova Systems Palo Alto', 'SambaNova', 'Palo Alto', 'CA', 'US', 'NA', 3, 'active', 'audit_2026_p3', '2026-02-22'),
('together-sf', 'Together AI San Francisco', 'Together AI', 'San Francisco', 'CA', 'US', 'NA', 5, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== AIRTRUNK — APAC (13) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('airtrunk-syd1-au', 'AirTrunk SYD1 Sydney', 'AirTrunk', 'Sydney', NULL, 'AU', 'APAC', 130, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-syd2-au', 'AirTrunk SYD2 Sydney', 'AirTrunk', 'Sydney', NULL, 'AU', 'APAC', 120, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-syd3-au', 'AirTrunk SYD3 Sydney', 'AirTrunk', 'Sydney', NULL, 'AU', 'APAC', 320, 'construction', 'audit_2026_p3', '2026-02-22'),
('airtrunk-mel1-au', 'AirTrunk MEL1 Melbourne', 'AirTrunk', 'Melbourne', NULL, 'AU', 'APAC', 185, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-mel2-au', 'AirTrunk MEL2 Melbourne', 'AirTrunk', 'Melbourne', NULL, 'AU', 'APAC', 354, 'construction', 'audit_2026_p3', '2026-02-22'),
('airtrunk-sgp1-sg', 'AirTrunk SGP1 Singapore', 'AirTrunk', 'Singapore', NULL, 'SG', 'APAC', 60, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-hkg1-hk', 'AirTrunk HKG1 Hong Kong', 'AirTrunk', 'Hong Kong', NULL, 'HK', 'APAC', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-tok1-jp', 'AirTrunk TOK1 Tokyo', 'AirTrunk', 'Inzai', NULL, 'JP', 'APAC', 300, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-osk1-jp', 'AirTrunk OSK1 Osaka', 'AirTrunk', 'Osaka', NULL, 'JP', 'APAC', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-osk2-jp', 'AirTrunk OSK2 Osaka', 'AirTrunk', 'Osaka', NULL, 'JP', 'APAC', 100, 'construction', 'audit_2026_p3', '2026-02-22'),
('airtrunk-jhb1-my', 'AirTrunk JHB1 Johor Bahru', 'AirTrunk', 'Johor Bahru', NULL, 'MY', 'APAC', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('airtrunk-ind-in', 'AirTrunk India', 'AirTrunk', 'Mumbai', NULL, 'IN', 'APAC', 50, 'construction', 'audit_2026_p3', '2026-02-22'),
('airtrunk-ksa-sa', 'AirTrunk Saudi Arabia', 'AirTrunk', 'Riyadh', NULL, 'SA', 'EMEA', 100, 'planned', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== CHINA & ASIA (Chindata, GDS, Bridge — 13) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('chindata-huailai-cn', 'Chindata Huailai Campus', 'Chindata Group', 'Huailai', NULL, 'CN', 'APAC', 150, 'active', 'audit_2026_p3', '2026-02-22'),
('chindata-datong-cn', 'Chindata Datong Campus', 'Chindata Group', 'Datong', NULL, 'CN', 'APAC', 200, 'active', 'audit_2026_p3', '2026-02-22'),
('chindata-hebei-cn', 'Chindata Hebei Campus', 'Chindata Group', 'Langfang', NULL, 'CN', 'APAC', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('chindata-jhr-my', 'Chindata Johor Malaysia', 'Chindata Group', 'Johor', NULL, 'MY', 'APAC', 50, 'active', 'audit_2026_p3', '2026-02-22'),
('gds-shanghai-cn', 'GDS Shanghai Campus', 'GDS Holdings', 'Shanghai', NULL, 'CN', 'APAC', 200, 'active', 'audit_2026_p3', '2026-02-22'),
('gds-beijing-cn', 'GDS Beijing Campus', 'GDS Holdings', 'Beijing', NULL, 'CN', 'APAC', 150, 'active', 'audit_2026_p3', '2026-02-22'),
('gds-shenzhen-cn', 'GDS Shenzhen Campus', 'GDS Holdings', 'Shenzhen', NULL, 'CN', 'APAC', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('gds-hk-hk', 'GDS Hong Kong', 'GDS Holdings', 'Hong Kong', NULL, 'HK', 'APAC', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('gds-jhr-my', 'GDS Johor Malaysia', 'GDS Holdings', 'Johor', NULL, 'MY', 'APAC', 60, 'active', 'audit_2026_p3', '2026-02-22'),
('bridge-mumbai-in', 'Bridge Data Centres Mumbai', 'Bridge Data Centres', 'Mumbai', NULL, 'IN', 'APAC', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('bridge-chennai-in', 'Bridge Data Centres Chennai', 'Bridge Data Centres', 'Chennai', NULL, 'IN', 'APAC', 15, 'active', 'audit_2026_p3', '2026-02-22'),
('bridge-kl-my', 'Bridge Data Centres Kuala Lumpur', 'Bridge Data Centres', 'Kuala Lumpur', NULL, 'MY', 'APAC', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('bridge-jakarta-id', 'Bridge Data Centres Jakarta', 'Bridge Data Centres', 'Jakarta', NULL, 'ID', 'APAC', 15, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== AFRICA (Teraco, Raxio, ADC — 14) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('teraco-jhb-za1', 'Teraco JB1 Johannesburg', 'Teraco Data Environments', 'Johannesburg', NULL, 'ZA', 'EMEA', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('teraco-jhb-za2', 'Teraco JB3 Johannesburg', 'Teraco Data Environments', 'Johannesburg', NULL, 'ZA', 'EMEA', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('teraco-cpt-za', 'Teraco CT1 Cape Town', 'Teraco Data Environments', 'Cape Town', NULL, 'ZA', 'EMEA', 15, 'active', 'audit_2026_p3', '2026-02-22'),
('teraco-dbn-za', 'Teraco DB1 Durban', 'Teraco Data Environments', 'Durban', NULL, 'ZA', 'EMEA', 5, 'active', 'audit_2026_p3', '2026-02-22'),
('raxio-kampala-ug', 'Raxio Kampala', 'Raxio', 'Kampala', NULL, 'UG', 'EMEA', 5, 'active', 'audit_2026_p3', '2026-02-22'),
('raxio-kinshasa-cd', 'Raxio Kinshasa', 'Raxio', 'Kinshasa', NULL, 'CD', 'EMEA', 3, 'active', 'audit_2026_p3', '2026-02-22'),
('raxio-addis-et', 'Raxio Addis Ababa', 'Raxio', 'Addis Ababa', NULL, 'ET', 'EMEA', 3, 'active', 'audit_2026_p3', '2026-02-22'),
('raxio-dar-tz', 'Raxio Dar es Salaam', 'Raxio', 'Dar es Salaam', NULL, 'TZ', 'EMEA', 3, 'active', 'audit_2026_p3', '2026-02-22'),
('raxio-maputo-mz', 'Raxio Maputo', 'Raxio', 'Maputo', NULL, 'MZ', 'EMEA', 2, 'active', 'audit_2026_p3', '2026-02-22'),
('adc-nairobi-ke', 'Africa Data Centres Nairobi', 'Africa Data Centres', 'Nairobi', NULL, 'KE', 'EMEA', 15, 'active', 'audit_2026_p3', '2026-02-22'),
('adc-jhb-za', 'Africa Data Centres Johannesburg', 'Africa Data Centres', 'Johannesburg', NULL, 'ZA', 'EMEA', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('adc-lagos-ng', 'Africa Data Centres Lagos', 'Africa Data Centres', 'Lagos', NULL, 'NG', 'EMEA', 10, 'active', 'audit_2026_p3', '2026-02-22'),
('adc-cairo-eg', 'Africa Data Centres Cairo', 'Africa Data Centres', 'Cairo', NULL, 'EG', 'EMEA', 5, 'active', 'audit_2026_p3', '2026-02-22'),
('adc-accra-gh', 'Africa Data Centres Accra', 'Africa Data Centres', 'Accra', NULL, 'GH', 'EMEA', 5, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== LATAM (Ascenty, ODATA, CIRION — 13) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('ascenty-sp1-br', 'Ascenty SP1 Sao Paulo', 'Ascenty', 'Sao Paulo', NULL, 'BR', 'LATAM', 40, 'active', 'audit_2026_p3', '2026-02-22'),
('ascenty-sp4-br', 'Ascenty SP4 Campinas', 'Ascenty', 'Campinas', NULL, 'BR', 'LATAM', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('ascenty-rj-br', 'Ascenty RJ1 Rio de Janeiro', 'Ascenty', 'Rio de Janeiro', NULL, 'BR', 'LATAM', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('ascenty-santiago-cl', 'Ascenty Santiago', 'Ascenty', 'Santiago', NULL, 'CL', 'LATAM', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('ascenty-queretaro-mx', 'Ascenty Queretaro', 'Ascenty', 'Queretaro', NULL, 'MX', 'LATAM', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('odata-sp-br', 'ODATA Sao Paulo', 'ODATA', 'Sao Paulo', NULL, 'BR', 'LATAM', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('odata-bogota-co', 'ODATA Bogota', 'ODATA', 'Bogota', NULL, 'CO', 'LATAM', 15, 'active', 'audit_2026_p3', '2026-02-22'),
('odata-santiago-cl', 'ODATA Santiago', 'ODATA', 'Santiago', NULL, 'CL', 'LATAM', 10, 'active', 'audit_2026_p3', '2026-02-22'),
('odata-mx-mx', 'ODATA Mexico City', 'ODATA', 'Mexico City', NULL, 'MX', 'LATAM', 10, 'active', 'audit_2026_p3', '2026-02-22'),
('cirion-sp-br', 'CIRION Technologies Sao Paulo', 'CIRION Technologies', 'Sao Paulo', NULL, 'BR', 'LATAM', 25, 'active', 'audit_2026_p3', '2026-02-22'),
('cirion-ba-ar', 'CIRION Technologies Buenos Aires', 'CIRION Technologies', 'Buenos Aires', NULL, 'AR', 'LATAM', 15, 'active', 'audit_2026_p3', '2026-02-22'),
('cirion-lima-pe', 'CIRION Technologies Lima', 'CIRION Technologies', 'Lima', NULL, 'PE', 'LATAM', 10, 'active', 'audit_2026_p3', '2026-02-22'),
('cirion-bogota-co', 'CIRION Technologies Bogota', 'CIRION Technologies', 'Bogota', NULL, 'CO', 'LATAM', 10, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ===================== EUROPE (DATA4, Aruba, Global Switch — 12) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES
('data4-paris-fr', 'DATA4 Paris Marcoussis', 'DATA4', 'Marcoussis', NULL, 'FR', 'EMEA', 62, 'active', 'audit_2026_p3', '2026-02-22'),
('data4-milan-it', 'DATA4 Milan Cornaredo', 'DATA4', 'Cornaredo', NULL, 'IT', 'EMEA', 32, 'active', 'audit_2026_p3', '2026-02-22'),
('data4-madrid-es', 'DATA4 Madrid Alcobendas', 'DATA4', 'Alcobendas', NULL, 'ES', 'EMEA', 20, 'active', 'audit_2026_p3', '2026-02-22'),
('aruba-it1-it', 'Aruba IT1 Arezzo', 'Aruba S.p.A.', 'Arezzo', NULL, 'IT', 'EMEA', 16, 'active', 'audit_2026_p3', '2026-02-22'),
('aruba-it3-it', 'Aruba IT3 Ponte San Pietro', 'Aruba S.p.A.', 'Ponte San Pietro', NULL, 'IT', 'EMEA', 90, 'active', 'audit_2026_p3', '2026-02-22'),
('aruba-dc-cz', 'Aruba Prague', 'Aruba S.p.A.', 'Prague', NULL, 'CZ', 'EMEA', 8, 'active', 'audit_2026_p3', '2026-02-22'),
('gs-london-gb', 'Global Switch London', 'Global Switch', 'London', NULL, 'GB', 'EMEA', 100, 'active', 'audit_2026_p3', '2026-02-22'),
('gs-amsterdam-nl', 'Global Switch Amsterdam', 'Global Switch', 'Amsterdam', NULL, 'NL', 'EMEA', 30, 'active', 'audit_2026_p3', '2026-02-22'),
('gs-frankfurt-de', 'Global Switch Frankfurt', 'Global Switch', 'Frankfurt', NULL, 'DE', 'EMEA', 22, 'active', 'audit_2026_p3', '2026-02-22'),
('gs-sydney-au', 'Global Switch Sydney', 'Global Switch', 'Sydney', NULL, 'AU', 'APAC', 60, 'active', 'audit_2026_p3', '2026-02-22'),
('gs-singapore-sg', 'Global Switch Singapore', 'Global Switch', 'Singapore', NULL, 'SG', 'APAC', 50, 'active', 'audit_2026_p3', '2026-02-22'),
('gs-hk-hk', 'Global Switch Hong Kong', 'Global Switch', 'Hong Kong', NULL, 'HK', 'APAC', 30, 'active', 'audit_2026_p3', '2026-02-22')
ON CONFLICT (name, city, country) DO NOTHING;


-- ============================================================
-- FINAL VERIFICATION
-- ============================================================

\echo ''
\echo '========================================='
\echo 'MASTER AUDIT — FINAL RESULTS'
\echo '========================================='
\echo ''

SELECT source, COUNT(*) as facilities_added
FROM facilities 
WHERE source IN ('audit_2026', 'audit_2026_p3')
GROUP BY source ORDER BY source;

\echo ''
\echo '=== ALL PROVIDERS FROM AUDIT ==='
\echo ''

SELECT provider, COUNT(*) as facilities, ROUND(SUM(COALESCE(power_mw,0))::numeric) as total_mw
FROM facilities 
WHERE source IN ('audit_2026', 'audit_2026_p3')
GROUP BY provider 
ORDER BY total_mw DESC;

\echo ''
\echo '=== GRAND TOTALS ==='
\echo ''

SELECT 
    COUNT(*) as total_facilities,
    COUNT(DISTINCT provider) as unique_providers,
    ROUND(SUM(COALESCE(power_mw,0))::numeric) as total_mw,
    COUNT(DISTINCT country) as countries
FROM facilities;


-- ============================================================
-- MASTER SUMMARY
-- ============================================================
-- Phase 1:  68 inserts (hyperscaler + neocloud gap fill)
-- Phase 2:  ~50 updates (provider normalization, city backfill)
-- Phase 3: 129 inserts (new operators + international)
-- --------------------------------------------------------
-- Total:   197 new facilities + 50 cleanup operations
--
-- New operators added (26 total):
--   Phase 1: Nebius, TensorWave, Core42, Crusoe Energy
--   Phase 3: Compass Datacenters, STACK Infrastructure,
--            Yondr Group, Applied Digital, CloudHQ,
--            Stream Data Centers, Groq, Cerebras, SambaNova,
--            Together AI, AirTrunk, Chindata Group,
--            GDS Holdings, Bridge Data Centres,
--            Teraco, Raxio, Africa Data Centres,
--            Ascenty, ODATA, CIRION Technologies,
--            DATA4, Aruba S.p.A., Global Switch
--
-- All operations safe to re-run (ON CONFLICT / idempotent)
-- ============================================================
