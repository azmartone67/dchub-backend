-- ============================================================
-- DC Hub — Gap Fill: 68 Missing Facilities
-- ============================================================
-- Generated: 2026-02-21
-- Source: Compared DC Hub Neon PostgreSQL vs. industry research
-- Schema: facilities (id, name, provider, city, state, country, 
--         region, power_mw, status, source, last_updated)
-- 
-- USAGE: Run in Replit shell:
--   psql $DATABASE_URL -f insert_missing.sql
--
-- Or in Python:
--   import os, psycopg2
--   db = psycopg2.connect(os.environ['DATABASE_URL'])
--   cur = db.cursor()
--   cur.execute(open('insert_missing.sql').read())
--   db.commit()
--
-- Uses ON CONFLICT DO NOTHING so it's safe to run multiple times.
-- UNIQUE constraint is (name, city, country).
-- ============================================================

-- ===================== META (17 missing) =====================

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

-- ===================== GOOGLE (12 missing) =====================

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

-- ===================== NEBIUS (9 missing — new operator) =====================

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

-- ===================== TENSORWAVE (5 missing — new operator) =====================

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

-- ===================== CORE42 / G42 (6 missing — new operator) =====================

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

-- ===================== LAMBDA (6 missing — KC already exists) =====================

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

-- ===================== CRUSOE ENERGY (3 missing — new operator) =====================

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('crusoe-midland-tx', 'Crusoe Energy Midland TX', 'Crusoe Energy', 'Midland', 'TX', 'US', 'NA', 20, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('crusoe-williston-nd', 'Crusoe Energy North Dakota', 'Crusoe Energy', 'Williston', 'ND', 'US', 'NA', 15, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

INSERT INTO facilities (id, name, provider, city, state, country, region, power_mw, status, source, last_updated)
VALUES ('crusoe-rankin-tx', 'Crusoe Energy Upton County TX', 'Crusoe Energy', 'Rankin', 'TX', 'US', 'NA', 10, 'active', 'audit_2026', '2026-02-21')
ON CONFLICT (name, city, country) DO NOTHING;

-- ===================== COREWEAVE (10 missing — 26 already exist) =====================

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

-- ============================================================
-- SUMMARY: 68 INSERT statements
--   Meta:       17
--   Google:     12
--   Nebius:      9
--   CoreWeave:  10
--   Lambda:      6
--   Core42:      6
--   TensorWave:  5
--   Crusoe:      3
-- 
-- Total new MW added: ~11,780 MW
-- New operators added: Nebius, TensorWave, Core42, Crusoe Energy
-- Safe to re-run (ON CONFLICT DO NOTHING)
-- ============================================================
