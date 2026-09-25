-- r-anon-wall-keyed (2026-09-25) - keep keyed calls out of the anonymous
-- per-IP count.
-- routes/mcp_anon_usage.py counts an IP's calls today from mcp_tool_calls for
-- the anonymous daily cap and the hard wall (cap x 10). That table had no key
-- column, so calls made WITH an api_key were counted too: a keyed heavy user
-- used up the anonymous budget of everyone behind the same IP (an office NAT,
-- or the owner's laptop, where every keyed Claude session walled the
-- anonymous directory probe - measured 2026-09-25).
-- track_tool_call now writes keyed = the gateway sent an api_key on the call,
-- and the count skips keyed IS TRUE. NULL (rows written before this, or by a
-- writer that does not set it) still counts, as every row did before.
--
-- Nullable, no default: metadata-only on Postgres, no table rewrite. The
-- writer also converges this column itself (catalog check, then ALTER under a
-- 2s lock_timeout on the direct track connection), and names it in the INSERT
-- only once it is confirmed, so the call row is never lost to deploy skew.
ALTER TABLE mcp_tool_calls ADD COLUMN IF NOT EXISTS keyed boolean;
COMMENT ON COLUMN mcp_tool_calls.keyed IS
  'TRUE when the MCP gateway sent an api_key on this call. NULL = not recorded (counts as anonymous).';
