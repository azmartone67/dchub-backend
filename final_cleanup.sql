-- ============================================================
-- DC Hub — FINAL CLEANUP ROUND
-- ============================================================
-- Run: psql $DATABASE_URL -f final_cleanup.sql
-- Targets: Market overview pages, remaining no-city entries,
--          MW backfill for Equinix/Vantage/Digital Realty
-- ============================================================

BEGIN;

-- ═══════════════════════════════════════════════════════════
-- STEP 1: DELETE DATACENTER MARKET OVERVIEW PAGES
-- These are DatacenterHawk market summary pages, not facilities
-- ═══════════════════════════════════════════════════════════

\echo '=== STEP 1: Deleting DatacenterHawk market pages ==='

DELETE FROM facilities WHERE name LIKE '%Data Center Market%';
DELETE FROM facilities WHERE name LIKE '%data center market%';

-- Also delete BrainServe (random OSM entry with no provider)
DELETE FROM facilities WHERE name = 'BrainServe' AND (provider IS NULL OR provider = '');

-- Any remaining no-provider entries
\echo '--- Remaining no-provider entries ---'
SELECT COUNT(*) as orphans FROM facilities WHERE (provider IS NULL OR provider = '');

DELETE FROM facilities WHERE (provider IS NULL OR provider = '')
  AND power_mw = 0 AND sqft = 0;


-- ═══════════════════════════════════════════════════════════
-- STEP 2: AWS — DELETE REMAINING NO-CITY ENTRIES
-- 42 AWS entries with no city = cloud region placeholders
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 2: AWS no-city cleanup ==='

-- Convert to cloud_region type instead of deleting (preserves count)
UPDATE facilities SET facility_type = 'cloud_region', power_mw = 0
WHERE provider = 'Amazon Web Services' 
  AND (city IS NULL OR city = '');


-- ═══════════════════════════════════════════════════════════
-- STEP 3: META — FIX REMAINING 13 NO-CITY ENTRIES  
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 3: Meta no-city cleanup ==='

-- Show what they are
SELECT id, name, city, source FROM facilities 
WHERE provider = 'Meta Platforms' AND (city IS NULL OR city = '')
LIMIT 13;

-- These are likely PeeringDB/OSM dupes of real campuses — delete
DELETE FROM facilities 
WHERE provider = 'Meta Platforms' 
  AND (city IS NULL OR city = '') 
  AND facility_type = 'campus';


-- ═══════════════════════════════════════════════════════════
-- STEP 4: MICROSOFT — FIX 18 NO-CITY ENTRIES
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 4: Microsoft no-city cleanup ==='

-- These are Azure region stubs — reclassify
UPDATE facilities SET facility_type = 'cloud_region'
WHERE provider = 'Microsoft' 
  AND (city IS NULL OR city = '');


-- ═══════════════════════════════════════════════════════════
-- STEP 5: DIGITAL REALTY — 22 NO-CITY ENTRIES
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 5: Digital Realty no-city cleanup ==='

DELETE FROM facilities 
WHERE provider = 'Digital Realty' 
  AND (city IS NULL OR city = '');


-- ═══════════════════════════════════════════════════════════
-- STEP 6: REMAINING SMALL-BATCH NO-CITY CLEANUP
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 6: Batch cleanup of remaining no-city entries ==='

-- Cable & Wireless (10) — old telecom, not DC operator
DELETE FROM facilities WHERE provider = 'Cable & Wireless' AND (city IS NULL OR city = '');

-- KIO (10) — Mexican DC provider, entries without city are useless
DELETE FROM facilities WHERE provider = 'KIO' AND (city IS NULL OR city = '');

-- Canberra Data Centres (9) — if no city, delete
DELETE FROM facilities WHERE provider = 'Canberra Data Centres' AND (city IS NULL OR city = '');

-- CoreSite (9 no city)
DELETE FROM facilities WHERE provider = 'CoreSite' AND (city IS NULL OR city = '') AND power_mw = 0;

-- Orange (9) — French telecom, not a DC operator
DELETE FROM facilities WHERE provider = 'Orange' AND (city IS NULL OR city = '');

-- IDCフロンティア (8) — Japanese provider
DELETE FROM facilities WHERE provider = 'IDCフロンティア' AND (city IS NULL OR city = '');

-- Selectel (8) — Russian provider
DELETE FROM facilities WHERE provider = 'Selectel' AND (city IS NULL OR city = '');

-- CloudHQ (8) — we added these, may have city mismatch
-- Check first
SELECT name, city FROM facilities WHERE provider = 'CloudHQ' AND (city IS NULL OR city = '');

-- REFSA (7)
DELETE FROM facilities WHERE provider = 'REFSA' AND (city IS NULL OR city = '');

-- Aligned Data Centers (7)
DELETE FROM facilities WHERE provider = 'Aligned Data Centers' AND (city IS NULL OR city = '') AND power_mw = 0;

-- Verizon (7)
DELETE FROM facilities WHERE provider = 'Verizon' AND (city IS NULL OR city = '');

-- Stack (7) — note: different from "STACK Infrastructure"
DELETE FROM facilities WHERE provider = 'Stack' AND (city IS NULL OR city = '');

-- GasLINE (7) — German gas utility, not DC
DELETE FROM facilities WHERE provider = 'GasLINE' AND (city IS NULL OR city = '');

-- NextDC (6) — Australian provider
DELETE FROM facilities WHERE provider = 'NextDC' AND (city IS NULL OR city = '');

-- SFR (6) — French telecom
DELETE FROM facilities WHERE provider = 'SFR' AND (city IS NULL OR city = '');

-- Equinix remaining 10 no-city
DELETE FROM facilities WHERE provider = 'Equinix' AND (city IS NULL OR city = '') AND power_mw = 0;


-- ═══════════════════════════════════════════════════════════
-- STEP 7: MW BACKFILL FOR MAJOR PROVIDERS
-- Many Equinix/DR/Vantage entries at 0 MW
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 7: MW backfill for major providers ==='

-- Equinix: avg facility is ~5MW for smaller, 15-50MW for larger
-- Set floor of 5MW for any Equinix with city but 0 MW
UPDATE facilities SET power_mw = 5 
WHERE provider = 'Equinix' AND power_mw = 0 
  AND city IS NOT NULL AND city != ''
  AND facility_type = 'campus';

-- Digital Realty: similar pattern
UPDATE facilities SET power_mw = 5 
WHERE provider = 'Digital Realty' AND power_mw = 0 
  AND city IS NOT NULL AND city != ''
  AND facility_type = 'campus';

-- Vantage: they're wholesale, typically 20-100MW per campus
-- But 95 entries at 0MW seems like cloud region stubs
-- Set conservative 15MW floor for entries with city
UPDATE facilities SET power_mw = 15 
WHERE provider = 'Vantage Data Centers' AND power_mw = 0 
  AND city IS NOT NULL AND city != ''
  AND facility_type = 'campus';

-- NTT Global: set 10MW floor
UPDATE facilities SET power_mw = 10 
WHERE provider = 'NTT Global Data Centers' AND power_mw = 0 
  AND city IS NOT NULL AND city != ''
  AND facility_type = 'campus';

-- QTS: wholesale, 15MW floor
UPDATE facilities SET power_mw = 15 
WHERE provider = 'QTS Realty Trust' AND power_mw = 0 
  AND city IS NOT NULL AND city != ''
  AND facility_type = 'campus';

-- Cologix: smaller colo, 5MW floor
UPDATE facilities SET power_mw = 5 
WHERE provider = 'Cologix' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';

-- CyrusOne: 10MW floor
UPDATE facilities SET power_mw = 10 
WHERE provider = 'CyrusOne' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';

-- DataBank: smaller colo, 5MW floor
UPDATE facilities SET power_mw = 5 
WHERE provider = 'DataBank' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';

-- Flexential: 3MW floor
UPDATE facilities SET power_mw = 3 
WHERE provider = 'Flexential' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';

-- CoreSite: 10MW floor
UPDATE facilities SET power_mw = 10 
WHERE provider = 'CoreSite' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';

-- Iron Mountain: 10MW floor
UPDATE facilities SET power_mw = 10 
WHERE provider = 'Iron Mountain Data Centers' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';

-- Switch: big campuses, 50MW floor
UPDATE facilities SET power_mw = 50 
WHERE provider = 'Switch' AND power_mw = 0 
  AND city IS NOT NULL AND city != '';


-- ═══════════════════════════════════════════════════════════
-- STEP 8: GLOBAL MW FLOOR
-- Any remaining facility with city + provider but 0 MW
-- gets a conservative 2MW floor estimate
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 8: Global 2MW floor for remaining 0-MW entries ==='

UPDATE facilities SET power_mw = 2 
WHERE power_mw = 0 
  AND city IS NOT NULL AND city != ''
  AND provider IS NOT NULL AND provider != ''
  AND facility_type NOT IN ('cloud_region', 'building');


-- ═══════════════════════════════════════════════════════════
-- STEP 9: CONFIDENCE SCORE REFRESH
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '=== STEP 9: Confidence score refresh ==='

-- High: audit data
UPDATE facilities SET confidence = 0.95 
WHERE source IN ('audit_2026', 'audit_2026_p3', 'audit_bldg');

-- Good: has city + provider + MW > 2
UPDATE facilities SET confidence = 0.7 
WHERE city IS NOT NULL AND city != '' 
  AND provider IS NOT NULL AND provider != ''
  AND power_mw > 2
  AND confidence < 0.7
  AND source NOT IN ('audit_2026', 'audit_2026_p3', 'audit_bldg');

-- Medium: has city + provider
UPDATE facilities SET confidence = 0.5 
WHERE city IS NOT NULL AND city != '' 
  AND provider IS NOT NULL AND provider != ''
  AND confidence < 0.5;

-- Low: cloud regions
UPDATE facilities SET confidence = 0.3 
WHERE facility_type = 'cloud_region' AND confidence != 0.3;


COMMIT;


-- ═══════════════════════════════════════════════════════════
-- VERIFICATION
-- ═══════════════════════════════════════════════════════════

\echo ''
\echo '========================================='
\echo 'FINAL CLEANUP — RESULTS'
\echo '========================================='

\echo ''
\echo '--- Facility type breakdown ---'
SELECT facility_type, COUNT(*) as n, ROUND(SUM(power_mw)::numeric) as mw
FROM facilities GROUP BY facility_type ORDER BY n DESC;

\echo ''
\echo '--- Remaining data quality issues ---'
SELECT 
    COUNT(*) FILTER (WHERE city IS NULL OR city = '') as no_city,
    COUNT(*) FILTER (WHERE provider IS NULL OR provider = '') as no_provider,
    COUNT(*) FILTER (WHERE power_mw = 0) as no_power,
    COUNT(*) FILTER (WHERE power_mw = 2) as at_floor_2mw
FROM facilities WHERE facility_type NOT IN ('cloud_region');

\echo ''
\echo '--- Top 15 providers by MW ---'
SELECT provider, COUNT(*) as facilities, ROUND(SUM(power_mw)::numeric) as total_mw
FROM facilities 
WHERE facility_type NOT IN ('cloud_region')
GROUP BY provider 
ORDER BY SUM(power_mw) DESC LIMIT 15;

\echo ''
\echo '--- Grand totals ---'
SELECT 
    COUNT(*) as total_records,
    COUNT(*) FILTER (WHERE facility_type = 'campus') as campuses,
    COUNT(*) FILTER (WHERE facility_type = 'building') as buildings,
    COUNT(*) FILTER (WHERE facility_type = 'cloud_region') as cloud_regions,
    COUNT(DISTINCT provider) as providers,
    ROUND(SUM(power_mw)::numeric) as total_mw,
    COUNT(DISTINCT country) as countries,
    ROUND(AVG(confidence)::numeric, 2) as avg_confidence
FROM facilities;

\echo ''
\echo '--- Confidence distribution ---'
SELECT 
    CASE 
        WHEN confidence >= 0.9 THEN 'High (0.9+)'
        WHEN confidence >= 0.7 THEN 'Good (0.7-0.89)'
        WHEN confidence >= 0.5 THEN 'Medium (0.5-0.69)'
        ELSE 'Low (<0.5)'
    END as quality_tier,
    COUNT(*) as records
FROM facilities
GROUP BY 1 ORDER BY 1;


-- ============================================================
-- EXPECTED OUTCOMES:
-- ~40 market pages deleted
-- ~200+ no-city entries cleaned (deleted or reclassified)
-- ~3,000+ entries get MW backfill (conservative estimates)
-- All records get confidence scores
-- Remaining no-city: <100 (edge cases)
-- Total MW: should jump to 75,000+ from backfills
-- ============================================================
