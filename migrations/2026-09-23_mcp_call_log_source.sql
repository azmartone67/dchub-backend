-- r-source-path (2026-09-23, issue #3778) - registry arrival attribution.
-- dchub-mcp-server#331 sends `source` (glama, smithery, pulsemcp, mcpso,
-- lobehub, toolplex, mcpmarket) on POST /api/v1/mcp/track. It had nowhere to
-- land and was silently dropped. NULL = arrived on canonical /mcp.
--
-- source is NOT platform: platform = which client, source = who sent them.
-- Caller-assertable (anyone can hit /mcp/glama), so a growth read only, never
-- identity or a payout basis. The writer (flask_mcp_endpoints.track_tool_call)
-- sanitises it to a lower-case slug of [a-z0-9._-], 64 chars max, else NULL.
--
-- Nullable, no default: metadata-only on Postgres, no table rewrite. The
-- writer also converges this column itself (catalog check, then ALTER under a
-- 2s lock_timeout), and names it in the INSERT only once it is confirmed, so
-- the call row is never lost to deploy skew.
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS source text;
COMMENT ON COLUMN mcp_call_log.source IS
  'Registry arrival path tag from the MCP server (glama, smithery, ...). NULL = canonical /mcp. Caller-assertable; not identity.';
