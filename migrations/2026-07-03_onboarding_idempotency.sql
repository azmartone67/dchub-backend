-- ============================================================================
-- 2026-07-03  Onboarding idempotency hardening  (r-onboarding-fix)
--
-- Companion migration to the code changes on branch
-- fix/onboarding-funnel-2026-07-03. REVIEW + RUN MANUALLY against the primary
-- Neon endpoint (NOT auto-run at boot — see the boot-DDL-storm history).
--
-- Fixes audit defects #4 (no idempotency) and #5 (duplicate api_keys via a
-- check-then-insert race). Founding customer #7 got TWO active api_keys rows
-- (dchub_cHcI5r / dchub_fgLdqa, 7ms apart) because two concurrent webhook
-- deliveries both passed the app-level "does an active key exist?" check before
-- either inserted.
-- ============================================================================

BEGIN;

-- 1) Stripe webhook idempotency ledger. The app also creates this lazily
--    (_stripe_event_already_processed), but codify it here as the canonical DDL.
CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT,
    processed_at TIMESTAMPTZ DEFAULT now()
);

-- 2) Deactivate DUPLICATE active api_keys, keeping the earliest-created active
--    key per user (non-destructive: we set is_active=0, we do not DELETE, so no
--    credential history is lost and nothing that referenced the row breaks).
UPDATE api_keys a
   SET is_active = 0
 WHERE COALESCE(a.is_active, 1) = 1
   AND EXISTS (
        SELECT 1 FROM api_keys b
         WHERE b.user_id = a.user_id
           AND COALESCE(b.is_active, 1) = 1
           AND ( b.created_at < a.created_at
                 OR (b.created_at = a.created_at AND b.id < a.id) )
   );

-- 3) Enforce "at most one ACTIVE api_keys row per user" at the DB level. After
--    this, a second concurrent INSERT that slips past the app guard raises a
--    unique violation instead of creating a duplicate; the caller's _pg_execute
--    swallows it, so the customer simply keeps their first key (desired).
CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_one_active_per_user
    ON api_keys (user_id)
    WHERE COALESCE(is_active, 1) = 1;

COMMIT;

-- ----------------------------------------------------------------------------
-- POST-RUN VERIFICATION (run separately; should each return 0 offending rows):
--   -- users with >1 active api_keys row (should be 0):
--   SELECT user_id, count(*) FROM api_keys WHERE COALESCE(is_active,1)=1
--     GROUP BY user_id HAVING count(*) > 1;
--   -- Roman specifically (77594c7a070f7304) — expect exactly 1 active:
--   SELECT id, key_prefix, is_active FROM api_keys WHERE user_id='77594c7a070f7304';
-- ----------------------------------------------------------------------------
