-- ============================================================
-- DC Hub — Phase 2: Data Cleanup & Dedup
-- ============================================================
-- Run in Replit shell:  psql $DATABASE_URL -f cleanup.sql
-- Safe to re-run. All operations are idempotent.
-- ============================================================


-- =====================================================
-- STEP 1: PROVIDER NAME NORMALIZATION
-- Merge variant names into canonical names
-- =====================================================

-- AWS / Amazon Web Services → Amazon Web Services
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider = 'AWS';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider = 'Amazon.com, Inc.';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider = 'Amazon';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider LIKE 'Amazon Web Services%' AND provider != 'Amazon Web Services';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider LIKE 'Amazon SIN%';
UPDATE facilities SET provider = 'Amazon Web Services' WHERE provider LIKE 'Amazon IAD%';

-- Meta / Facebook → Meta Platforms
UPDATE facilities SET provider = 'Meta Platforms' WHERE provider = 'Meta';
UPDATE facilities SET provider = 'Meta Platforms' WHERE provider = 'Facebook';
UPDATE facilities SET provider = 'Meta Platforms' WHERE provider LIKE 'Meta Platforms%' AND provider != 'Meta Platforms';

-- Google variants → Google
UPDATE facilities SET provider = 'Google' WHERE provider = 'Google LLC';
UPDATE facilities SET provider = 'Google' WHERE provider = 'Google Cloud';
UPDATE facilities SET provider = 'Google' WHERE provider LIKE 'Google Singapore%';
UPDATE facilities SET provider = 'Google' WHERE provider LIKE 'Google台灣%';

-- Microsoft variants
UPDATE facilities SET provider = 'Microsoft' WHERE provider = 'Microsoft Corporation';
UPDATE facilities SET provider = 'Microsoft' WHERE provider = 'Microsoft Azure';

-- Oracle variants
UPDATE facilities SET provider = 'Oracle' WHERE provider = 'Oracle Corporation';
UPDATE facilities SET provider = 'Oracle' WHERE provider = 'Oracle Cloud';

-- CyrusOne variants
UPDATE facilities SET provider = 'CyrusOne' WHERE provider = 'CyrusOne Inc';
UPDATE facilities SET provider = 'CyrusOne' WHERE provider = 'CyrusOne LLC';
UPDATE facilities SET provider = 'CyrusOne' WHERE provider = 'CyrusOne, Inc.';
UPDATE facilities SET provider = 'CyrusOne' WHERE provider LIKE 'CyrusOne%' AND provider != 'CyrusOne';

-- NTT variants (RagingWire was acquired by NTT)
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider = 'NTT';
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider = 'NTT Ltd';
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider = 'NTT Communications';
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider = 'NTT Global';
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider = 'RagingWire';
UPDATE facilities SET provider = 'NTT Global Data Centers' WHERE provider LIKE 'RagingWire%';

-- Flexential (formerly Peak 10)
UPDATE facilities SET provider = 'Flexential' WHERE provider = 'Peak 10';
UPDATE facilities SET provider = 'Flexential' WHERE provider = 'Peak10';
UPDATE facilities SET provider = 'Flexential' WHERE provider LIKE 'Flexential%' AND provider != 'Flexential';

-- Digital Realty variants
UPDATE facilities SET provider = 'Digital Realty' WHERE provider = 'Digital Realty Trust';
UPDATE facilities SET provider = 'Digital Realty' WHERE provider = 'DigitalRealty';
UPDATE facilities SET provider = 'Digital Realty' WHERE provider LIKE 'Digital Realty%' AND provider != 'Digital Realty';

-- Equinix variants
UPDATE facilities SET provider = 'Equinix' WHERE provider = 'Equinix Inc';
UPDATE facilities SET provider = 'Equinix' WHERE provider = 'Equinix, Inc.';
UPDATE facilities SET provider = 'Equinix' WHERE provider LIKE 'Equinix%' AND provider != 'Equinix';

-- QTS variants
UPDATE facilities SET provider = 'QTS Realty Trust' WHERE provider = 'QTS';
UPDATE facilities SET provider = 'QTS Realty Trust' WHERE provider = 'QTS Data Centers';
UPDATE facilities SET provider = 'QTS Realty Trust' WHERE provider LIKE 'QTS%' AND provider != 'QTS Realty Trust';

-- Vantage variants
UPDATE facilities SET provider = 'Vantage Data Centers' WHERE provider = 'Vantage';
UPDATE facilities SET provider = 'Vantage Data Centers' WHERE provider LIKE 'Vantage%' AND provider != 'Vantage Data Centers';

-- DataBank variants
UPDATE facilities SET provider = 'DataBank' WHERE provider = 'DataBank, Ltd.';
UPDATE facilities SET provider = 'DataBank' WHERE provider = 'DataBank, Ltd';
UPDATE facilities SET provider = 'DataBank' WHERE provider LIKE 'DataBank%' AND provider != 'DataBank';

-- EdgeConneX variants
UPDATE facilities SET provider = 'EdgeConneX' WHERE provider LIKE 'EdgeConneX%' AND provider != 'EdgeConneX';
UPDATE facilities SET provider = 'EdgeConneX' WHERE provider = 'Edge ConneX';

-- Iron Mountain
UPDATE facilities SET provider = 'Iron Mountain Data Centers' WHERE provider = 'Iron Mountain';
UPDATE facilities SET provider = 'Iron Mountain Data Centers' WHERE provider LIKE 'Iron Mountain%' AND provider != 'Iron Mountain Data Centers';

-- Switch variants
UPDATE facilities SET provider = 'Switch' WHERE provider = 'Switch, Ltd';
UPDATE facilities SET provider = 'Switch' WHERE provider = 'Switch Ltd';

-- Colt DCS
UPDATE facilities SET provider = 'Colt DCS' WHERE provider = 'Colt';
UPDATE facilities SET provider = 'Colt DCS' WHERE provider = 'Colt Data Centre Services';

-- CoreWeave xAI cleanup
UPDATE facilities SET provider = 'xAI' WHERE provider = 'xAI Colossus';


-- =====================================================
-- STEP 2: REMOVE FALSE POSITIVES
-- Metanet is NOT Meta/Facebook
-- =====================================================

-- Don't delete — just fix the provider so they don't pollute Meta searches
-- Metanet Communications is a separate Korean ISP/hosting company
UPDATE facilities SET provider = 'Metanet Communications' WHERE provider = 'Meta Platforms' AND name LIKE 'Metanet%';
UPDATE facilities SET provider = 'Metanet Communications' WHERE provider = 'Meta' AND name LIKE 'Metanet%';


-- =====================================================
-- STEP 3: FIX EMPTY CITY FIELDS
-- Fill in known cities for major facilities
-- =====================================================

-- Meta facilities with empty cities
UPDATE facilities SET city = 'Luleå', country = 'SE' WHERE name LIKE 'Meta%datacenter%Luleå%' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Altoona', state = 'IA', country = 'US' WHERE name LIKE '%Altoona%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Forest City', state = 'NC', country = 'US' WHERE name LIKE '%Forest City%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Fort Worth', state = 'TX', country = 'US' WHERE name LIKE '%Fort Worth%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Clonee', country = 'IE' WHERE name LIKE '%Clonee%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Odense', country = 'DK' WHERE name LIKE '%Odense%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'New Albany', state = 'OH', country = 'US' WHERE name LIKE '%New Albany%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Papillion', state = 'NE', country = 'US' WHERE name LIKE '%Sarpy%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Henrico', state = 'VA', country = 'US' WHERE name LIKE '%Henrico%' AND provider = 'Meta Platforms' AND (city IS NULL OR city = '');

-- Google facilities with empty cities
UPDATE facilities SET city = 'The Dalles', state = 'OR', country = 'US' WHERE name LIKE '%Dalles%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Council Bluffs', state = 'IA', country = 'US' WHERE name LIKE '%Council Bluffs%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Moncks Corner', state = 'SC', country = 'US' WHERE name LIKE '%Berkeley County%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Midlothian', state = 'TX', country = 'US' WHERE name LIKE '%Douglas County%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Eemshaven', country = 'NL' WHERE name LIKE '%Eemshaven%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Middenmeer', country = 'NL' WHERE name LIKE '%Middenmeer%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Fredericia', country = 'DK' WHERE name LIKE '%Fredericia%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Hamina', country = 'FI' WHERE name LIKE '%Hamina%' AND provider = 'Google' AND (city IS NULL OR city = '');
UPDATE facilities SET city = 'Waltham Cross', country = 'GB' WHERE name LIKE '%Waltham Cross%' AND provider = 'Google' AND (city IS NULL OR city = '');


-- =====================================================
-- STEP 4: STATUS NORMALIZATION
-- Consolidate status values
-- =====================================================

UPDATE facilities SET status = 'active' WHERE LOWER(status) IN ('operational', 'active', 'operational');
UPDATE facilities SET status = 'active' WHERE LOWER(status) = 'active';
UPDATE facilities SET status = 'planned' WHERE LOWER(status) IN ('planned', 'planning', 'announced');
UPDATE facilities SET status = 'construction' WHERE LOWER(status) IN ('under construction', 'construction');


-- =====================================================
-- STEP 5: DUPLICATE DETECTION REPORT
-- This SELECT shows duplicates — review before deleting
-- =====================================================

-- Show duplicate name+city+country groups
-- Run this SELECT to review, then delete manually if confirmed
\echo ''
\echo '=== DUPLICATE REPORT ==='
\echo 'Groups with 2+ entries sharing same name+city+country:'
\echo ''

SELECT name, city, country, COUNT(*) as copies, 
       STRING_AGG(id, ', ') as ids,
       STRING_AGG(DISTINCT source, ', ') as sources
FROM facilities 
GROUP BY name, city, country 
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC
LIMIT 30;


-- =====================================================
-- STEP 6: VERIFY RESULTS
-- =====================================================

\echo ''
\echo '=== POST-CLEANUP PROVIDER COUNTS ==='
\echo ''

SELECT provider, COUNT(*) as facilities, ROUND(SUM(COALESCE(power_mw,0))::numeric) as total_mw
FROM facilities 
WHERE provider IN (
    'Meta Platforms', 'Google', 'Oracle', 'Amazon Web Services', 'Microsoft',
    'CoreWeave', 'Nebius', 'Lambda', 'TensorWave', 'Core42 (G42)', 
    'Crusoe Energy', 'xAI',
    'Equinix', 'Digital Realty', 'CyrusOne', 'Flexential', 'DataBank',
    'Vantage Data Centers', 'EdgeConneX', 'QTS Realty Trust',
    'NTT Global Data Centers', 'Iron Mountain Data Centers', 'Switch'
)
GROUP BY provider ORDER BY facilities DESC;

\echo ''
\echo '=== TOTAL STATS ==='
\echo ''

SELECT 
    COUNT(*) as total_facilities,
    COUNT(DISTINCT provider) as unique_providers,
    ROUND(SUM(COALESCE(power_mw,0))::numeric) as total_mw,
    COUNT(DISTINCT country) as countries,
    COUNT(*) FILTER (WHERE city IS NOT NULL AND city != '') as has_city,
    COUNT(*) FILTER (WHERE city IS NULL OR city = '') as missing_city
FROM facilities;


-- ============================================================
-- SUMMARY
-- Step 1: Provider normalization (~20 merge operations)
-- Step 2: Metanet false positive fix
-- Step 3: Empty city backfill for Meta + Google facilities  
-- Step 4: Status value normalization
-- Step 5: Duplicate detection (report only — review before delete)
-- Step 6: Verification queries
--
-- DOES NOT DELETE anything. Safe to run.
-- After reviewing Step 5 duplicates, delete with:
--   DELETE FROM facilities WHERE id IN ('id1', 'id2', ...);
-- ============================================================
