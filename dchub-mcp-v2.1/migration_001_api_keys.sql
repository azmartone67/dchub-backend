-- DC Hub — Migration 001 (Neon Postgres)
-- Adds api_keys (developer license records) and mcp_call_log (per-tool-call telemetry).
-- Idempotent: uses IF NOT EXISTS everywhere.
--
-- Usage:
--   psql "$NEON_DATABASE_URL" -f migration_001_api_keys.sql
--
-- Or from the Neon SQL Editor in the dashboard, paste this whole file and run.

BEGIN;

-- ── Developer license records ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    api_key       TEXT PRIMARY KEY,                          -- secret presented as X-API-Key
    developer_id  TEXT NOT NULL,                             -- stable id assigned at signup
    email         TEXT,
    tier          TEXT NOT NULL DEFAULT 'free'
                  CHECK (tier IN ('free','paid','enterprise')),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','revoked','pending')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ,
    metadata      JSONB
);

CREATE INDEX IF NOT EXISTS idx_api_keys_developer ON api_keys(developer_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_email     ON api_keys(email);
CREATE INDEX IF NOT EXISTS idx_api_keys_tier      ON api_keys(tier);
CREATE INDEX IF NOT EXISTS idx_api_keys_status    ON api_keys(status);

-- ── Per-tool-call telemetry ──────────────────────────────────────────────
-- This is what the patched server.mjs writes to via POST /api/v1/mcp/track.
CREATE TABLE IF NOT EXISTS mcp_call_log (
    id           BIGSERIAL PRIMARY KEY,
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool         TEXT        NOT NULL,
    params       JSONB,
    platform     TEXT,
    api_key      TEXT,
    tier         TEXT,
    session_id   TEXT,
    status       TEXT,                       -- ok | error | blocked_paid_only
    duration_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_mcp_log_ts        ON mcp_call_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_log_tool      ON mcp_call_log(tool);
CREATE INDEX IF NOT EXISTS idx_mcp_log_platform  ON mcp_call_log(platform);
CREATE INDEX IF NOT EXISTS idx_mcp_log_apikey    ON mcp_call_log(api_key);
CREATE INDEX IF NOT EXISTS idx_mcp_log_status    ON mcp_call_log(status);

-- Composite for the most common query (last 7d by tool, by platform):
CREATE INDEX IF NOT EXISTS idx_mcp_log_ts_tool_platform
    ON mcp_call_log(timestamp DESC, tool, platform);

-- ── Optional: helper view for the dashboard ──────────────────────────────
CREATE OR REPLACE VIEW v_mcp_stats_7d AS
SELECT
    tool,
    platform,
    tier,
    COUNT(*)                                              AS calls,
    AVG(duration_ms)::INT                                 AS avg_ms,
    SUM(CASE WHEN status='error'             THEN 1 ELSE 0 END) AS errors,
    SUM(CASE WHEN status='blocked_paid_only' THEN 1 ELSE 0 END) AS upgrade_blocks,
    COUNT(DISTINCT api_key)                               AS distinct_devs
FROM mcp_call_log
WHERE timestamp >= NOW() - INTERVAL '7 days'
GROUP BY tool, platform, tier;

COMMIT;

-- ── Verification query — run this after the migration ────────────────────
-- SELECT 'api_keys' AS tbl, COUNT(*) FROM api_keys
-- UNION ALL
-- SELECT 'mcp_call_log', COUNT(*) FROM mcp_call_log;
