-- ═══════════════════════════════════════════════════════════════
-- DC Hub Data Quality Fixes — Run against Neon
-- ═══════════════════════════════════════════════════════════════
-- Usage: psql $NEON_DATABASE_URL -f fix_data.sql
-- Or paste into Neon console

BEGIN;

-- ───────────────────────────────────────────────────────────────
-- 1. Flag cumulative capex announcements (not real M&A deals)
-- These inflate the "$324B+ tracked" headline
-- ───────────────────────────────────────────────────────────────
ALTER TABLE deals ADD COLUMN IF NOT EXISTS deal_category VARCHAR(30) DEFAULT 'transaction';
ALTER TABLE deals ADD COLUMN IF NOT EXISTS data_flag VARCHAR(50);

UPDATE deals SET deal_category = 'capex_announcement', data_flag = 'cumulative_capex'
WHERE value >= 50000
  AND (
    (buyer ILIKE '%Microsoft%' AND seller ILIKE '%OpenAI%')
    OR (buyer ILIKE '%Oracle%' AND seller ILIKE '%OpenAI%' AND value >= 100000)
    OR (buyer ILIKE '%Amazon%' AND seller ILIKE '%AWS%')
    OR (buyer ILIKE '%Google%' AND value >= 100000)
    OR (buyer ILIKE '%xAI%' AND seller ILIKE '%xAI%')
    OR (buyer ILIKE '%Alibaba%' AND value >= 50000)
  );

-- ───────────────────────────────────────────────────────────────
-- 2. Deduplicate transactions (keep the one with more data)
-- Meta Lebanon IN appears twice
-- ───────────────────────────────────────────────────────────────
-- Mark dupes by finding rows with same buyer + similar market + similar value
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY LOWER(buyer), LOWER(REGEXP_REPLACE(market, '[^a-zA-Z]', '', 'g')),
                            ROUND(COALESCE(value, 0) / GREATEST(COALESCE(value, 1) * 0.1, 1))
               ORDER BY
                   (CASE WHEN notes IS NOT NULL AND notes != '' THEN 1 ELSE 0 END
                    + CASE WHEN region IS NOT NULL AND region != '' THEN 1 ELSE 0 END
                    + CASE WHEN assets IS NOT NULL THEN 1 ELSE 0 END) DESC,
                   date DESC
           ) as rn
    FROM deals
    WHERE deal_category = 'transaction' OR deal_category IS NULL
)
DELETE FROM deals WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- ───────────────────────────────────────────────────────────────
-- 3. Fill missing regions based on market
-- ───────────────────────────────────────────────────────────────
UPDATE deals SET region = 'North America'
WHERE (region IS NULL OR region = '')
  AND (market ILIKE '%virginia%' OR market ILIKE '%texas%' OR market ILIKE '%ohio%'
       OR market ILIKE '%indiana%' OR market ILIKE '%california%' OR market ILIKE '%oregon%'
       OR market ILIKE '%chicago%' OR market ILIKE '%memphis%' OR market ILIKE '%denver%'
       OR market ILIKE '%atlanta%' OR market ILIKE '%new jersey%' OR market ILIKE '%wisconsin%'
       OR market ILIKE '%n. virginia%' OR market ILIKE '%ashburn%' OR market ILIKE '%dallas%'
       OR market ILIKE '%phoenix%' OR market ILIKE '%richmond%' OR market ILIKE '%west virginia%'
       OR market ILIKE '%mississippi%' OR market ILIKE '%pennsylvania%');

UPDATE deals SET region = 'EMEA'
WHERE (region IS NULL OR region = '')
  AND (market ILIKE '%sweden%' OR market ILIKE '%iceland%' OR market ILIKE '%nordics%'
       OR market ILIKE '%uk%' OR market ILIKE '%israel%' OR market ILIKE '%germany%');

UPDATE deals SET region = 'APAC'
WHERE (region IS NULL OR region = '')
  AND (market ILIKE '%tokyo%' OR market ILIKE '%singapore%' OR market ILIKE '%korea%'
       OR market ILIKE '%sydney%' OR market ILIKE '%india%');

UPDATE deals SET region = 'Global'
WHERE (region IS NULL OR region = '') AND market ILIKE '%global%';

-- ───────────────────────────────────────────────────────────────
-- 4. Generate slugs for facilities missing them
-- ───────────────────────────────────────────────────────────────
UPDATE discovered_facilities
SET slug = LOWER(REGEXP_REPLACE(REGEXP_REPLACE(name, '[^a-zA-Z0-9 ]', '', 'g'), '\s+', '-', 'g'))
WHERE (slug IS NULL OR slug = '')
  AND name IS NOT NULL AND name != '';

-- ───────────────────────────────────────────────────────────────
-- 5. Infer provider from facility name where missing
-- ───────────────────────────────────────────────────────────────
UPDATE discovered_facilities SET provider = 'Meta' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%meta%';
UPDATE discovered_facilities SET provider = 'Google' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%google%';
UPDATE discovered_facilities SET provider = 'Amazon Web Services' WHERE (provider IS NULL OR provider = '') AND (name ILIKE '%aws%' OR name ILIKE '%amazon%');
UPDATE discovered_facilities SET provider = 'Microsoft' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%microsoft%';
UPDATE discovered_facilities SET provider = 'Oracle' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%oracle%';
UPDATE discovered_facilities SET provider = 'Equinix' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%equinix%';
UPDATE discovered_facilities SET provider = 'Digital Realty' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%digital realty%';
UPDATE discovered_facilities SET provider = 'CoreWeave' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%coreweave%';
UPDATE discovered_facilities SET provider = 'xAI' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%xai%';
UPDATE discovered_facilities SET provider = 'Aligned' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%aligned%';
UPDATE discovered_facilities SET provider = 'STACK Infrastructure' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%stack%';
UPDATE discovered_facilities SET provider = 'Vantage Data Centers' WHERE (provider IS NULL OR provider = '') AND name ILIKE '%vantage%';

-- ───────────────────────────────────────────────────────────────
-- 6. Create api_keys table for gatekeeper
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    api_key VARCHAR(128) UNIQUE NOT NULL,
    tier VARCHAR(20) NOT NULL DEFAULT 'free',
    email VARCHAR(255),
    name VARCHAR(255),
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP,
    daily_calls INTEGER DEFAULT 0,
    total_calls INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    stripe_customer_id VARCHAR(128),
    notes TEXT
);

-- Add indexes only if columns exist (safe for any schema)
DO $$
BEGIN
    -- Try adding indexes — silently skip if column doesn't exist
    BEGIN
        CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(api_key);
    EXCEPTION WHEN undefined_column THEN NULL;
    END;
    BEGIN
        CREATE INDEX IF NOT EXISTS idx_api_keys_tier ON api_keys(tier);
    EXCEPTION WHEN undefined_column THEN NULL;
    END;
    BEGIN
        CREATE INDEX IF NOT EXISTS idx_api_keys_email ON api_keys(email);
    EXCEPTION WHEN undefined_column THEN NULL;
    END;
END $$;

-- ───────────────────────────────────────────────────────────────
-- 7. Summary report
-- ───────────────────────────────────────────────────────────────
DO $$
DECLARE
    txn_count INTEGER;
    capex_count INTEGER;
    fac_count INTEGER;
    null_slugs INTEGER;
    null_providers INTEGER;
BEGIN
    SELECT COUNT(*) INTO txn_count FROM deals WHERE deal_category = 'transaction' OR deal_category IS NULL;
    SELECT COUNT(*) INTO capex_count FROM deals WHERE deal_category = 'capex_announcement';
    SELECT COUNT(*) INTO fac_count FROM discovered_facilities;
    SELECT COUNT(*) INTO null_slugs FROM discovered_facilities WHERE slug IS NULL OR slug = '';
    SELECT COUNT(*) INTO null_providers FROM discovered_facilities WHERE provider IS NULL OR provider = '';

    RAISE NOTICE '';
    RAISE NOTICE '══════════════════════════════════════════';
    RAISE NOTICE '  DC Hub Data Quality Fix — Complete';
    RAISE NOTICE '══════════════════════════════════════════';
    RAISE NOTICE '  Transactions: %', txn_count;
    RAISE NOTICE '  Capex announcements flagged: %', capex_count;
    RAISE NOTICE '  Facilities: %', fac_count;
    RAISE NOTICE '  Remaining null slugs: %', null_slugs;
    RAISE NOTICE '  Remaining null providers: %', null_providers;
    RAISE NOTICE '  api_keys table: ready';
    RAISE NOTICE '══════════════════════════════════════════';
END $$;

COMMIT;
