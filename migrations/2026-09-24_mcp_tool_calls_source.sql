-- r-reach-source (2026-09-24, #3778 follow-up to #5409) - registry arrival
-- attribution on the table /api/v1/reach actually reads.
-- #5409 stored the MCP server's `source` tag (glama, smithery, ...) in
-- mcp_call_log.source. /api/v1/reach reads mcp_calls_identity, a view over
-- mcp_tool_calls, so it could not split by source. track_tool_call now writes
-- the same sanitised value here too, and reach joins mcp_calls_identity back
-- to mcp_tool_calls on id - the per-source numbers obey the same
-- is_real_external filter as the headline. The view itself is NOT changed.
--
-- Caller-assertable (anyone can hit /mcp/glama): attribution only, never
-- identity or a payout basis. NULL = arrived on canonical /mcp.
--
-- Nullable, no default: metadata-only on Postgres, no table rewrite. The
-- writer also converges this column itself (catalog check, then ALTER under a
-- 2s lock_timeout on the direct track connection), and names it in the INSERT
-- only once it is confirmed, so the call row is never lost to deploy skew.
ALTER TABLE mcp_tool_calls ADD COLUMN IF NOT EXISTS source text;
COMMENT ON COLUMN mcp_tool_calls.source IS
  'Registry arrival path tag from the MCP server (glama, smithery, ...). NULL = canonical /mcp. Caller-assertable; not identity.';
