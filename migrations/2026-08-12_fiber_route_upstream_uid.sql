-- 2026-08-12 — SH52-054: give fiber_routes an identity column that identifies
-- the ASSET instead of the crawl.
--
-- ★ WHAT WAS WRONG. fiber_routes carries UNIQUE(name, provider) and
-- UNIQUE(source, source_id). _save_route synthesized `name` from
-- owner/voltage/market, so distinct physical lines collapsed onto a few dozen
-- keys and ON CONFLICT DO NOTHING discarded the rest — the discovery lane sat
-- at ~154 rows against 55k of bulk carrier data.
--
-- The 2026-08-10 fingerprint fix (#2544) un-capped it: the hifld lane went
-- 109 -> 1,826 rows in three days. But it swapped one identity bug for a
-- smaller one. The fingerprint mixed in `start_lat/start_lng`, which
-- _sync_hifld_transmission_lines fills with the MARKET CENTROID (that call
-- passes return_geometry=False, so no line geometry exists). DC_MARKETS are
-- swept at a 50 km radius and those radii overlap, so the same physical line
-- reached from two markets got two fingerprints and two rows.
--
-- MEASURED ON THIS TABLE, 2026-08-12:
--     source='hifld':  1,826 rows  /  1,742 distinct upstream HIFLD line ids
--                      -> 84 physical lines held twice (4.8%), growing daily
--
-- ★ THE IDENTITY. HIFLD Electric_Power_Transmission_Lines carries `ID`,
-- populated on 52,244 of 52,244 features with zero empty values and zero
-- 'NOT AVAILABLE'/'UNKNOWN' sentinels (verified against the live FeatureServer
-- 2026-08-12). That is the asset id. REJECTED: OBJECTID (the row number of one
-- export — the exact mistake that holds substations.hifld_objectid hostage),
-- the market centroid (ours, not the asset's), and the synthesized name
-- (owner+voltage+market gives 14 distinct keys over 100 real lines in one
-- market).
--
-- ★★ NOTHING PUBLISHED IS REWRITTEN. `name`, `source_id`, `id` and every slug
-- derived from them are untouched. This migration only adds a column that did
-- not exist and fills it from the identity ALREADY EMBEDDED in each row's own
-- source_id. No row is inserted, deleted, renamed or redirected.
--
-- ★★★ WHY THE INDEX IS PARTIAL AND WHY THE BACKFILL PICKS ONE ROW PER GROUP.
-- 84 upstream ids are already held twice. Those rows are live and frozen, so a
-- unique index over all of them cannot be built. The backfill therefore stamps
-- upstream_uid on the LOWEST id of each group only; the twin keeps
-- upstream_uid NULL, stays published exactly as it is, and simply sits outside
-- the index. Duplicates are REPORTED (query at the bottom), never deleted —
-- pruning published rows is a separate, deliberate decision.
--
-- After this, _save_route writes upstream_uid and the bare
-- `ON CONFLICT DO NOTHING` — which arbitrates EVERY unique index on the table,
-- including this one — silently discards a line we already hold, no matter
-- which market we happened to see it from.

BEGIN;

ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS upstream_uid TEXT;

-- Recover each held row's upstream id from its own source_id.
--   'hifld_tl_141463_9f2a…'  -> split_part(…,'_',3) = '141463'
--   'osm_fiber_1234567_ab…'  -> '1234567'
--   'peeringdb_ix_42'        -> '42'
-- Pre-fix rows whose source_id is a bare 'hifld_tl_' yield '' and are excluded
-- by the uid <> '' test — they were never identified and this must not pretend
-- otherwise.
WITH ranked AS (
    SELECT id,
           source,
           split_part(source_id, '_', 3) AS uid,
           ROW_NUMBER() OVER (
               PARTITION BY source, split_part(source_id, '_', 3)
               ORDER BY id
           ) AS rn
    FROM fiber_routes
    WHERE source IN ('hifld', 'osm', 'peeringdb')
      AND source_id IS NOT NULL
      AND (source_id LIKE 'hifld\_tl\_%'
        OR source_id LIKE 'osm\_fiber\_%'
        OR source_id LIKE 'peeringdb\_ix\_%')
)
UPDATE fiber_routes f
   SET upstream_uid = r.uid
  FROM ranked r
 WHERE f.id = r.id
   AND r.rn = 1
   AND r.uid <> '';

-- The 'auto_discovery' (learned_fiber_*) lane is deliberately NOT backfilled.
-- Its held source_ids were minted from Python's per-process-salted hash(), so
-- there is no stable upstream id to recover from them — deriving one would
-- fabricate identity. Those rows keep upstream_uid NULL; new writes use the
-- deterministic md5 uid and are idempotent from here on.

CREATE UNIQUE INDEX IF NOT EXISTS fiber_routes_upstream_uid_uniq
    ON fiber_routes (source, upstream_uid)
    WHERE upstream_uid IS NOT NULL;

COMMIT;

-- ── VERIFY (run after; both are read-only) ──────────────────────────────────
-- Coverage: how many held rows now carry a real upstream identity.
--   SELECT source,
--          COUNT(*)                                        AS rows,
--          COUNT(upstream_uid)                             AS identified,
--          COUNT(*) - COUNT(upstream_uid)                  AS unidentified
--     FROM fiber_routes
--    GROUP BY source ORDER BY 2 DESC;
--
-- The frozen twins this migration reports rather than deletes:
--   SELECT source, split_part(source_id,'_',3) AS uid, COUNT(*) n,
--          array_agg(id ORDER BY id) AS row_ids
--     FROM fiber_routes
--    WHERE source IN ('hifld','osm','peeringdb')
--    GROUP BY 1,2 HAVING COUNT(*) > 1 ORDER BY 3 DESC;
