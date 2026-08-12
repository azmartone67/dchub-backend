-- 2026-08-12 — SH52-056: give `substations` a column that holds the upstream
-- HIFLD asset id, so NEW ingestion has something real to key on.
--
-- ★ THE BLOCKER, RESTATED PRECISELY. The HIFLD slice is a one-shot snapshot:
-- 79,686 rows, every one created_at = 2026-03-17. Writes have been refused
-- since 2026-07-31 (routes/substation_ingest.py returns 409; land_power_crawler
-- raises LandPowerWriteBlocked) "pending an identity strategy". Neither
-- candidate key identifies anything:
--
--   · hifld_objectid holds an ArcGIS export ROW NUMBER. Verified on the live
--     table 2026-08-12 — and it is worse than "wrong", it is THREE id-spaces in
--     one INTEGER column:
--         78,356 rows  positional      (3 .. 79,687)
--          1,330 rows  real HIFLD ID   (107,655 .. 110,133)  <- 07-31 canary
--         47,190 rows  NULL
--     Re-export in a different order and OBJECTID 1 names a different asset.
--   · (name, lat, lng) is quantized: lat/lng are `real` (float4) on this table
--     while upstream ships double precision, so the triple collides or not on
--     a floating-point rounding coin flip (67.3% measured 2026-07-31).
--
-- ★★ THE IDENTITY: upstream `ID`. Measured against the live FeatureServer
-- (services5.arcgis.com/HDRa0B57OVrv2E1q/.../Electric_Substations/0) on
-- 2026-08-12 by paging all 38 pages:
--         75,328 features
--         75,328 with ID populated (0 empty, 0 sentinel)
--         75,327 DISTINCT ID values  -> exactly ONE duplicate pair in the layer
--         ID values are namespaced (107,655+), NOT 1..N
--         OBJECTID is 1..N and equals ID on 0 of 2,000 sampled rows
-- That is an asset id. OBJECTID is a row number. They are not the same field
-- and the March load stored the wrong one.
--
-- ★★★ WHY A NEW COLUMN INSTEAD OF REUSING hifld_objectid. Because that column
-- already mixes three id-spaces. Writing real IDs into it would make "is this
-- value an asset id or a row number?" unanswerable per row, forever — the
-- two-id-spaces trap this codebase has already been bitten by. hifld_objectid
-- is left exactly as it is; it stays readable as the historical artifact it is.
--
-- ★ NOTHING PUBLISHED IS REWRITTEN. No row is inserted, deleted, renamed or
-- redirected. `name`, `id`, `hifld_objectid`, lat/lng and every derived slug
-- are untouched. This adds a column that did not exist and seeds it ONLY where
-- the value is already unambiguously known.

BEGIN;

ALTER TABLE substations ADD COLUMN IF NOT EXISTS hifld_id TEXT;

-- Seed the ONLY rows whose real HIFLD ID is already known with certainty: the
-- 1,330 corrected by the 2026-07-31 canary, whose hifld_objectid is a real ID
-- rather than a row number. The two ranges cannot overlap (positional values
-- run 3..79,687; upstream IDs start at 107,655), so the >= 100000 test is exact
-- and not a heuristic.
--
-- NOTHING ELSE IS SEEDED HERE. The other 78,356 rows need a reconciliation
-- against upstream, which is a separate, measured, human-gated step — see the
-- block at the bottom. Guessing them here is how ~25,000 duplicate substations
-- would have shipped.
UPDATE substations
   SET hifld_id = hifld_objectid::text
 WHERE hifld_id IS NULL
   AND hifld_objectid IS NOT NULL
   AND hifld_objectid >= 100000;

-- PARTIAL: 126k rows have no upstream id and must not be forced to invent one.
-- NULL here means UNIDENTIFIED, and rows outside the index keep the table's
-- pre-existing constraints as their only arbiter — unchanged behaviour.
--
-- ★★★ THE NAME IS `substations_hifld_asset_id_uniq`, NOT `..._hifld_id_uniq`,
-- AND THAT IS NOT COSMETIC. `substations_hifld_id_uniq` ALREADY EXISTS on this
-- table and it is defined ON hifld_objectid — the stale name left behind when
-- that column was renamed. `CREATE UNIQUE INDEX IF NOT EXISTS` matches on NAME,
-- so the first cut of this migration SILENTLY DID NOTHING: it "succeeded",
-- `SELECT indexname ... WHERE indexname='substations_hifld_id_uniq'` returned a
-- row (the OLD index), and the check read as green. It was caught only by
-- executing the real ON CONFLICT against the table, which failed with
-- "there is no unique or exclusion constraint matching the ON CONFLICT
-- specification". Verify indexes by their DEFINITION, never by their name.
CREATE UNIQUE INDEX IF NOT EXISTS substations_hifld_asset_id_uniq
    ON substations (hifld_id)
    WHERE hifld_id IS NOT NULL;

-- Fail loudly rather than leave a no-op behind. If the index above did not get
-- created on the intended COLUMN, everything downstream is broken in a way that
-- reads as healthy.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE tablename = 'substations'
           AND indexdef ILIKE '%(hifld_id)%'
           AND indexdef ILIKE '%UNIQUE%'
    ) THEN
        RAISE EXCEPTION
          'no UNIQUE index on substations(hifld_id) — the CREATE was a no-op '
          '(most likely an index of that NAME already exists on another column)';
    END IF;
END $$;

COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════
-- THE REMAINING STEP — DELIBERATELY NOT RUN HERE
-- ═══════════════════════════════════════════════════════════════════════════
-- Adding the key does NOT unblock the bulk refresh, and pretending otherwise
-- would be the whole failure again. With hifld_id NULL on 78,356 held rows, a
-- full upstream run keys every incoming record as new and inserts ~75,000
-- duplicates beside the rows they duplicate.
--
-- ★ WHAT THE RECOVERY ACTUALLY IS. Measured 2026-08-12 by fetching all 75,328
-- upstream records and matching them against the held HIFLD slice on
-- coordinate rounded to 4dp (~11 m) — NAME excluded from the match, because
-- 38,479 of 75,328 upstream names (51.1%) are `UNKNOWN<ID>` placeholders
-- literally built from the ID:
--
--     upstream distinct coordinate keys .................. 75,218
--     held     distinct coordinate keys .................. 79,558
--     upstream keys that MATCH a held row ................ 71,006
--     upstream keys with NO held row (genuinely new) .....  4,212
--     held keys upstream no longer lists .................  8,552
--     ambiguous keys (>1 asset at one point): up 110 / held 125
--
-- So the refresh is worth +4,212 substations (126,876 -> ~131,088) plus a
-- currency and enrichment pass over 71,006 — NOT the +75,328 the raw count
-- suggests, and NOT the ~25,000 duplicates the 07-31 canary extrapolated. That
-- extrapolation was right about the canary and wrong about the cause: the
-- canary matched on (name, lat, lng), and name is half placeholder upstream.
-- Drop name from the match and the duplicate risk collapses to the 235
-- ambiguous keys.
--
-- ★ A SAFE BACKFILL THEREFORE NEEDS, IN THIS ORDER:
--   1. This migration applied (hifld_id + partial unique index).
--   2. A LINK-ONLY pass: UPDATE substations SET hifld_id = <upstream ID> for
--      held rows matched 1:1 on rounded coordinate. It writes ONE column,
--      inserts nothing, deletes nothing, and must SKIP every coordinate key
--      that is ambiguous on either side (110 upstream / 125 held) — those are
--      genuinely co-located assets and need a human, not a tiebreak rule.
--      Run it in bounded batches with the row count asserted before COMMIT.
--   3. Re-measure: `SELECT COUNT(*) FROM substations WHERE source='HIFLD' AND
--      hifld_id IS NULL` must fall to roughly 8,552 + the ambiguous residue.
--      If it does not, STOP — the link pass did not do what it claims.
--   4. Only then clear SUBSTATION_WRITES_BLOCKED, and run the ingest with a
--      cap first (?cap=2000) and read rows_before/rows_after, not the
--      Python-side counter (which reported upserted=2,000 on 2026-08-02 while
--      the transaction rolled back and nothing was written).
--
-- Step 2 is NOT in this migration on purpose: it depends on a live upstream
-- fetch, it has an ambiguous residue that needs a decision, and an unbounded
-- write loop against this table is what disabled infra_sync in the scheduler.
-- `POST /api/v1/admin/ingest/substations?reconcile_report=1` re-measures every
-- number above on demand, so the decision is made against today's data rather
-- than this comment.
