-- r-aurora-canon (2026-08-02) — one-time, guarded, idempotent.
--
-- Splits the single mislabeled 'aurora' market_power_scores row into the two
-- real markets it was conflating. See routes/dcpi.py
-- _CITY_MARKET_DISAMBIGUATION and tests/test_market_canon_aurora.py.
--
-- Background: _load_markets_dynamic groups by (LOWER(city), city, state), so
-- Aurora IL (22 fac / 158 MW) and Aurora CO (12 fac / 51 MW) were BOTH emitted
-- under slug 'aurora'; the per-slug `UPDATE ... WHERE market_slug=<param>`
-- scoring loop kept only whichever was written LAST, and the loader's
-- `ORDER BY facility_count DESC` guarantees that is the SMALLER city. Hence one
-- row saying state=CO while 76% of the MW behind it is Illinois.
--
-- THIS SCRIPT IS OPTIONAL. The recompute self-heal in recompute_all_scores
-- performs step 1 automatically on the next DCPI run. Run this only to heal
-- immediately instead of waiting for the next cron tick. Step 1 deliberately
-- mirrors the self-heal's semantics EXACTLY (including the state qualifier),
-- so running both in either order converges on the same state.
--
-- Safe to re-run: every statement is a no-op once its precondition is gone.

BEGIN;

-- ── Step 1: give Colorado its own slug ────────────────────────────────────
-- RENAME when 'aurora-co' does not exist yet; DELETE the bare row when it
-- does (the loader would otherwise re-mint a duplicate). The `state = 'CO'`
-- qualifier is what makes this safe to re-run AFTER the loader has re-minted
-- bare 'aurora' as Illinois — the Illinois row is never matched.

UPDATE market_power_scores
   SET market_slug = 'aurora-co',
       market_name = 'Aurora, CO'
 WHERE market_slug = 'aurora'
   AND state       = 'CO'
   AND NOT EXISTS (SELECT 1 FROM market_power_scores
                    WHERE market_slug = 'aurora-co');

DELETE FROM market_power_scores
 WHERE market_slug = 'aurora'
   AND state       = 'CO'
   AND EXISTS (SELECT 1 FROM market_power_scores
                WHERE market_slug = 'aurora-co');

-- ── Step 2: drop the stale merged deep-dive brief ─────────────────────────
-- The live /markets/aurora brief was generated against the CONFLATED market:
-- "29 facilities, 209 MW" (the union of both Auroras) with CyrusOne — an
-- ILLINOIS operator — as top operator, under a Colorado label. Bare 'aurora'
-- now means Illinois only (22 fac / 158 MW), so those stats are wrong on a
-- live indexed page. Deleting lets cron_rotate regenerate it against the
-- corrected market; until it does, the page falls back to the guard-neutral
-- path (_brief_guard_reason) rather than serving wrong numbers.
--
-- Guarded on generated_at so a re-run never deletes a POST-fix regeneration.
-- OPTIONAL — skip this statement to leave the stale brief up until rotation
-- reaches it on its own.

DELETE FROM market_deep_dives
 WHERE market_slug  = 'aurora'
   AND generated_at < TIMESTAMPTZ '2026-08-02 06:35:00+00';

COMMIT;

-- ── Verify (expect: 'aurora' state=IL, 'aurora-co' state=CO after the next
--    recompute; before it, only 'aurora-co' exists) ───────────────────────
-- SELECT market_slug, market_name, state, computed_at
--   FROM market_power_scores
--  WHERE market_slug IN ('aurora', 'aurora-co') ORDER BY market_slug;
