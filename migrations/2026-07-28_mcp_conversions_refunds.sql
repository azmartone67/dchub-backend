-- 2026-07-28 — mcp_conversions never reversed refunds.
--
-- No Stripe refund event was handled anywhere in the codebase, so a refunded
-- sale stayed in the ledger as revenue forever. gabriel.zuckerman@nlr.gov was
-- double-billed $3,000/yr across two customer records; the duplicate was
-- refunded by hand in Stripe and the ledger never learned. Those two $3,000
-- rows then carried 77% of May and 96% of June reported MRR, manufacturing an
-- apparent 84% "collapse" into July that never happened.
--
-- Additive and reversible: the row is kept as the audit trail and merely
-- stamped. Read paths exclude `refunded_at IS NOT NULL` rather than deleting
-- history.

ALTER TABLE mcp_conversions ADD COLUMN IF NOT EXISTS refunded_at    TIMESTAMPTZ;
ALTER TABLE mcp_conversions ADD COLUMN IF NOT EXISTS refunded_cents BIGINT;

-- Partial index: every MRR read filters on `refunded_at IS NULL`, and the
-- refunded set is tiny, so index the exception rather than the whole table.
CREATE INDEX IF NOT EXISTS ix_mcp_conversions_refunded
    ON mcp_conversions (stripe_customer_id)
    WHERE refunded_at IS NOT NULL;
