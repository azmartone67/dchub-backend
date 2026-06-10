-- =============================================================================
-- NLR PARTNERSHIP — UPGRADE TO ENTERPRISE TIER (90% off NLR rate)
-- =============================================================================
-- Run in Neon SQL Console: console.neon.tech → dchub project → SQL Editor
--
-- Effect:
--   - Flips users.plan from 'free' → 'enterprise' for all 3 NLR contacts
--   - Flips api_keys.plan + rate_limit_tier → 'enterprise' for their active keys
--   - Preserves partner_keys_issued.label as the audit trail showing the
--     90%-off NLR Research Seed pricing
--   - Leaves existing key strings unchanged — they're already in NLR's hands
--     via the re-send emails. Rotating the keys would force a re-distribution.
--
-- Idempotent: re-running is safe (no-ops if already at enterprise).
-- =============================================================================

BEGIN;

-- 1. Upgrade users.plan
UPDATE users
SET
  plan = 'enterprise',
  role = COALESCE(NULLIF(role, ''), 'enterprise')
WHERE email IN (
  'gabriel.zuckerman@nlr.gov',
  'galen.maclaurin@nlr.gov',
  'ian.christie@nlr.gov'
);

-- 2. Upgrade api_keys.plan + rate_limit_tier for their ACTIVE keys
UPDATE api_keys
SET
  plan = 'enterprise',
  rate_limit_tier = 'enterprise'
WHERE user_id IN (
  SELECT id FROM users
  WHERE email IN (
    'gabriel.zuckerman@nlr.gov',
    'galen.maclaurin@nlr.gov',
    'ian.christie@nlr.gov'
  )
)
AND is_active = 1;

-- 3. Update partner_keys_issued to reflect enterprise tier (preserves the
--    "NLR Year-1 Research Seed (FY 2026, $3K)" label for the billing audit
--    trail — the label documents the 90% discount mechanism even though the
--    functional tier is now enterprise).
UPDATE partner_keys_issued
SET plan = 'enterprise'
WHERE partner_slug = 'reveal-nlr'
  AND revoked_at IS NULL
  AND user_email IN (
    'gabriel.zuckerman@nlr.gov',
    'galen.maclaurin@nlr.gov',
    'ian.christie@nlr.gov'
  );

COMMIT;

-- =============================================================================
-- VERIFICATION QUERY — confirm the upgrade landed
-- =============================================================================
SELECT
  u.email,
  u.plan AS user_plan,
  ak.plan AS api_key_plan,
  ak.rate_limit_tier,
  pki.plan AS partner_plan,
  pki.label AS partner_label,
  ak.key_prefix
FROM users u
JOIN api_keys ak ON ak.user_id = u.id AND ak.is_active = 1
LEFT JOIN partner_keys_issued pki ON pki.key_prefix = SUBSTRING(ak.key_hash, 1, 24)
  AND pki.partner_slug = 'reveal-nlr'
  AND pki.revoked_at IS NULL
WHERE u.email IN (
  'gabriel.zuckerman@nlr.gov',
  'galen.maclaurin@nlr.gov',
  'ian.christie@nlr.gov'
)
ORDER BY u.email;

-- Expected output (3 rows, all with user_plan = 'enterprise'):
--
--   email                          | user_plan  | api_key_plan | rate_limit_tier | partner_plan | partner_label                              | key_prefix
--   -------------------------------+------------+--------------+-----------------+--------------+--------------------------------------------+--------------------------
--   gabriel.zuckerman@nlr.gov      | enterprise | enterprise   | enterprise      | enterprise   | NLR Year-1 Research Seed (FY 2026, $3K)... | dchub_developer_jhfHONJx
--   galen.maclaurin@nlr.gov        | enterprise | enterprise   | enterprise      | enterprise   | NLR Year-1 Research Seed (FY 2026, $3K)... | dchub_developer_iWmhspMS
--   ian.christie@nlr.gov           | enterprise | enterprise   | enterprise      | enterprise   | NLR Year-1 Research Seed (FY 2026, $3K)... | dchub_developer_jZ6bKqlr
