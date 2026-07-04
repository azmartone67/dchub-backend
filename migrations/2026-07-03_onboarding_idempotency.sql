-- ============================================================================
-- 2026-07-03  Onboarding idempotency hardening  (r-onboarding-fix)
--
-- Companion migration to the code changes on branch
-- fix/onboarding-funnel-2026-07-03. REVIEW + RUN MANUALLY against the primary
-- Neon endpoint (NOT auto-run at boot — see the boot-DDL-storm history).
--
-- Fixes audit defect #4 (no idempotency) and cleans up the duplicate-key
-- symptom from defect #5. Founding customer #7 got TWO active api_keys rows
-- (dchub_cHcI5r / dchub_fgLdqa, 7ms apart) because two concurrent webhook
-- deliveries both passed the app-level "does an active key exist?" check before
-- either inserted.
--
-- IMPORTANT — why there is NO `UNIQUE(user_id)` constraint here:
--   An earlier draft added a partial UNIQUE index enforcing "one active key per
--   user". That is WRONG for this schema: internal/owner accounts (e.g.
--   user_id='admin001') legitimately hold many active keys — including the
--   platform's own live keys (DCHUB_API_KEY, DCHUB_ENT_KEY). A blanket
--   constraint deactivates those. The correct race protection is the
--   APPLICATION-level idempotency gate (_stripe_event_already_processed, which
--   dedupes the same Stripe event across retries/concurrent deliveries — the
--   actual cause of the dup), NOT a DB uniqueness rule. So this migration only
--   creates the ledger and cleans UNUSED duplicate keys.
-- ============================================================================

BEGIN;

-- 1) Stripe webhook idempotency ledger. The app also creates this lazily
--    (_stripe_event_already_processed), but codify it here as the canonical DDL.
CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT,
    processed_at TIMESTAMPTZ DEFAULT now()
);

-- 2) Deactivate ONLY UNUSED duplicate active keys (zero usage on every counter),
--    keeping the earliest active key per user. This clears webhook-race dups
--    like Roman's id 94 WITHOUT touching keys that have ever served traffic and
--    WITHOUT collapsing accounts that intentionally hold multiple keys (their
--    used keys all survive). Non-destructive: is_active=0, never DELETE.
UPDATE api_keys a
   SET is_active = 0
 WHERE COALESCE(a.is_active, 1) = 1
   AND COALESCE(a.calls_total, 0) = 0
   AND COALESCE(a.usage_count, 0) = 0
   AND COALESCE(a.calls_today, 0) = 0
   AND EXISTS (
        SELECT 1 FROM api_keys b
         WHERE b.user_id = a.user_id
           AND COALESCE(b.is_active, 1) = 1
           AND ( b.created_at < a.created_at
                 OR (b.created_at = a.created_at AND b.id < a.id) )
   );

COMMIT;

-- ----------------------------------------------------------------------------
-- POST-RUN VERIFICATION:
--   -- Roman (77594c7a070f7304): expect id 93 active, id 94 inactive:
--   SELECT id, key_prefix, is_active FROM api_keys WHERE user_id='77594c7a070f7304';
--   -- No USED key should have been deactivated (should return 0 rows):
--   SELECT id, user_id, key_prefix, calls_total FROM api_keys
--    WHERE COALESCE(is_active,1)=0
--      AND (COALESCE(calls_total,0)>0 OR COALESCE(usage_count,0)>0);
-- ----------------------------------------------------------------------------
