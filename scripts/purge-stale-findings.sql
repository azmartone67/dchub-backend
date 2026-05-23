-- ============================================================================
-- purge-stale-findings.sql
-- ============================================================================
-- Run against the Neon DATABASE_URL to clear historical brain findings that
-- accumulated as cumulative counts and are no longer actionable:
--
--   • enterprise_bot_present                — 22,677 cumulative count over
--     14d; almost all from Railway's own egress (162.220.232.*). The
--     2026-05-23 fix excludes the Railway range from new whales, but
--     existing history sits in the heal queue inflating the count.
--   • on_conflict_target_mismatch tags      — the brain dashboard's JS
--     matcher was tagging unrelated findings as this class (fixed in
--     commit bf308192). Stale tags persist in heal_findings cache.
--   • findings older than 30 days           — anything that hasn't fired
--     in a month is no longer signal. Trims the table.
--
-- Usage (from any host with Neon access):
--   psql "$DATABASE_URL" -f scripts/purge-stale-findings.sql
--
-- Or via the admin endpoint if Neon CLI isn't available:
--   curl -X POST https://dchub.cloud/api/v1/admin/heal/purge-stale \
--        -H "X-Admin-Key: $DCHUB_ADMIN_KEY"
--   (creates the endpoint server-side from this same SQL)
--
-- Idempotent. Safe to run repeatedly.
-- ============================================================================

BEGIN;

-- 1. Show current state (counts before purge)
SELECT 'before' AS phase, COUNT(*) AS total_findings,
       SUM(CASE WHEN issue = 'enterprise_bot_present' THEN 1 ELSE 0 END) AS bot_findings,
       SUM(CASE WHEN created_at < NOW() - INTERVAL '30 days' THEN 1 ELSE 0 END) AS over_30d_old,
       MIN(created_at) AS oldest,
       MAX(created_at) AS newest
  FROM heal_findings;

-- 2. Purge enterprise_bot_present findings that point at the Railway
--    egress range. Keep findings about real external whales.
DELETE FROM heal_findings
 WHERE issue = 'enterprise_bot_present'
   AND (
       url LIKE '%162.220.232.%'
    OR url LIKE '%162.220.233.%'
    OR url LIKE '%RLWY-METALGEN1%'
    OR url LIKE '%AS400940%'
   );

-- 3. Purge anything older than 30 days. Stale findings poison the
--    heatmap; if a finding hasn't refreshed in a month, the underlying
--    detector either stopped firing or the issue resolved.
DELETE FROM heal_findings
 WHERE created_at < NOW() - INTERVAL '30 days';

-- 4. Optional: purge any heal-cache entries the dashboard rendered as
--    "known: on_conflict_target_mismatch" but were actually
--    funnel_*, trial_*, or mcp_conversion_* — these were JS-matcher
--    false positives, not real ON CONFLICT errors.
--
-- (commented out by default — the new JS matcher in dchub-frontend/
--  brain.html re-renders these correctly on next page load. Only
--  uncomment if you want to force-rebuild the cache.)
--
-- DELETE FROM heal_findings
--  WHERE issue IN (
--    'trial_to_paid_stagnation',
--    'funnel_leak_critical',
--    'funnel_conversion_critical',
--    'mcp_conversion_stale_critical',
--    'mcp_funnel_concentration_top5'
--  )
--    AND created_at < NOW() - INTERVAL '24 hours';

-- 5. Show state after purge
SELECT 'after' AS phase, COUNT(*) AS total_findings,
       SUM(CASE WHEN issue = 'enterprise_bot_present' THEN 1 ELSE 0 END) AS bot_findings,
       SUM(CASE WHEN created_at < NOW() - INTERVAL '30 days' THEN 1 ELSE 0 END) AS over_30d_old,
       MIN(created_at) AS oldest,
       MAX(created_at) AS newest
  FROM heal_findings;

COMMIT;

-- Verify on /brain dashboard:
-- ▸ enterprise_bot_present count should drop drastically (was 22,677)
-- ▸ Total findings should shrink
-- ▸ Brain registry stays at 16 classes (purge doesn't touch the registry)
