-- =============================================================================
-- Pre-generate password-reset tokens for the 3 NLR contacts
-- =============================================================================
-- Run in Neon SQL Console. Each contact gets a unique 72-hour reset link
-- they can click from the welcome email to set their first password.
--
-- Why this is needed: partner_key_issuer.py creates users with a placeholder
-- SHA256 password hash (64-char hex, no colon). The login endpoint expects
-- PBKDF2 format (salt:hash with a colon separator) and rejects anything
-- else as 'Invalid credentials'. NLR contacts couldn't log in without
-- triggering forgot-password first — bad UX.
--
-- This script pre-bakes the reset tokens so the welcome email can ship
-- direct click-to-set-password links instead.
--
-- Tokens expire 72 hours from creation. Idempotent: re-running generates
-- fresh tokens (old ones marked used).
-- =============================================================================

BEGIN;

-- Mark any existing unused tokens for these emails as used (clear the slate)
UPDATE password_reset_tokens
SET used = TRUE
WHERE user_email IN (
  'gabriel.zuckerman@nlr.gov',
  'galen.maclaurin@nlr.gov',
  'ian.christie@nlr.gov'
)
AND used = FALSE;

-- Generate a fresh reset token for each NLR contact, 72-hour TTL
INSERT INTO password_reset_tokens (user_email, token, expires_at)
VALUES
  ('gabriel.zuckerman@nlr.gov',
   encode(gen_random_bytes(32), 'base64'),
   (NOW() + INTERVAL '72 hours')::TEXT),
  ('galen.maclaurin@nlr.gov',
   encode(gen_random_bytes(32), 'base64'),
   (NOW() + INTERVAL '72 hours')::TEXT),
  ('ian.christie@nlr.gov',
   encode(gen_random_bytes(32), 'base64'),
   (NOW() + INTERVAL '72 hours')::TEXT);

COMMIT;

-- =============================================================================
-- Print the reset URLs to share with NLR contacts via email
-- =============================================================================
SELECT
  user_email,
  'https://dchub.cloud/reset-password.html?token=' ||
    REPLACE(REPLACE(token, '/', '_'), '+', '-') AS reset_url,
  expires_at
FROM password_reset_tokens
WHERE user_email IN (
  'gabriel.zuckerman@nlr.gov',
  'galen.maclaurin@nlr.gov',
  'ian.christie@nlr.gov'
)
AND used = FALSE
ORDER BY user_email;
