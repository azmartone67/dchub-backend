-- 2026-07-01 r-reach-canonical-views: canonical agent-identity views + self-traffic IP exclusions
--
-- mcp_calls_identity is THE canonical definition of "one AI agent" (agent_id =
-- md5 of the first X-Forwarded-For token) and of "real external traffic"
-- (is_real_external). Every surface that reports agent counts (/api/v1/reach,
-- /api/v1/stats/live-proof, marketing_engine signals, wins_poster, the media
-- fact-check guard) reads these views. NEVER count session_id as agents — the
-- server mints a new session per MCP connection.
--
-- This revision adds IP-level self-traffic exclusions so PRE-2026-06-15 history
-- reads honestly (RFO 2026-07-01: the June "hundreds/wk" were DC Hub's own
-- failover harness + self-heal loops; PR#1137 stopped logging them 06-14, but
-- the rows already written still classified as real):
--   · 162.220.232.* / 152.55.176.* / 152.55.177.*  — Railway egress ranges
--     (our own backend calling itself)
--   · 50.18.85.20                                  — AWS failover-harness IP
--   · 54.* with user_agent exactly 'node'          — AWS failover-harness fleet
--     (22 observed IPs, all bursty single-day windows, all pre-06-15; the bare
--     'node' UA slips past the node-fetch/undici UA guard). Scoped to UA='node'
--     so a future legitimate AWS-hosted agent on 54.* still counts.
-- Verified before shipping: ZERO rows from these ranges after 2026-06-15, so
-- this changes history only.

CREATE OR REPLACE VIEW mcp_calls_identity AS
 SELECT id,
    tool_name,
    platform,
    client_name,
    success,
    response_time_ms,
    ip_address,
    user_agent,
    created_at,
    session_id,
    TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) AS client_ip,
    md5(TRIM(BOTH FROM split_part(ip_address, ','::text, 1))) AS agent_id,
    ip_address IS NOT NULL AND ip_address <> ''::text AND TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) !~ '^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|::1|fc|fd|0\.0\.0\.0|$)'::text AS is_public_ip,
    (
        CASE
            WHEN NULLIF(lower(client_name), ''::text) IS NOT NULL AND client_name !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN lower(client_name)
            WHEN user_agent ~~* '%dchub-%'::text OR user_agent ~~* '%dchubhealer%'::text OR user_agent ~~* '%brain-v2-headless%'::text OR user_agent ~~* '%brain-radar%'::text OR user_agent ~~* '%uptimerobot%'::text THEN 'internal-dchub'::text
            WHEN user_agent ~~* '%@modelcontextprotocol/sdk%'::text OR user_agent ~~* '%modelcontextprotocol%'::text THEN 'mcp-sdk'::text
            WHEN user_agent ~~* '%mcp-inspector%'::text THEN 'mcp-inspector'::text
            WHEN user_agent ~~* '%n8n%'::text THEN 'n8n'::text
            WHEN user_agent ~~* '%smithery%'::text THEN 'smithery'::text
            WHEN user_agent ~~* '%chatgpt%'::text OR user_agent ~~* '%openai%'::text THEN 'chatgpt'::text
            WHEN user_agent ~~* '%claude%'::text OR user_agent ~~* '%anthropic%'::text THEN 'claude'::text
            WHEN user_agent ~~* '%perplexity%'::text THEN 'perplexity'::text
            WHEN user_agent ~~* '%gemini%'::text OR user_agent ~~* '%googleother%'::text THEN 'gemini'::text
            WHEN user_agent ~~* '%groq%'::text THEN 'groq'::text
            WHEN user_agent ~~* '%cursor%'::text THEN 'cursor'::text
            WHEN user_agent ~~* '%windsurf%'::text OR user_agent ~~* '%codeium%'::text THEN 'windsurf'::text
            WHEN user_agent ~~* '%continue%'::text THEN 'continue.dev'::text
            WHEN user_agent ~~* '%cody%'::text OR user_agent ~~* '%sourcegraph%'::text THEN 'sourcegraph-cody'::text
            WHEN user_agent ~~* '%copilot%'::text THEN 'github-copilot'::text
            WHEN user_agent ~~* '%cline%'::text THEN 'cline'::text
            WHEN user_agent ~~* '%phind%'::text THEN 'phind'::text
            WHEN user_agent ~~* '%you.com%'::text OR user_agent ~~* '%youbot%'::text THEN 'you.com'::text
            WHEN user_agent ~~* '%meta-external%'::text OR user_agent ~~* '%llama%'::text THEN 'meta-ai'::text
            WHEN user_agent ~~* '%applebot-extended%'::text THEN 'apple-intelligence'::text
            WHEN user_agent ~~* '%curl%'::text THEN 'curl'::text
            WHEN user_agent ~~* '%python%'::text OR user_agent ~~* '%requests%'::text THEN 'python-script'::text
            WHEN user_agent ~~* '%node-fetch%'::text OR user_agent ~~* '%undici%'::text OR user_agent ~~* '%axios%'::text OR user_agent ~~* '%got/%'::text THEN 'node-http-client'::text
            WHEN user_agent ~~* '%node%'::text THEN 'node-script'::text
            WHEN user_agent ~~* '%postman%'::text THEN 'postman'::text
            WHEN user_agent ~~* '%insomnia%'::text THEN 'insomnia'::text
            ELSE 'unknown'::text
        END <> ALL (ARRAY['curl'::text, 'insomnia'::text, 'internal-dchub'::text, 'node-http-client'::text, 'node-script'::text, 'postman'::text, 'python-script'::text, 'verify'::text]))
    AND COALESCE(lower(platform), ''::text) !~~ 'dchub-%'::text
    AND (COALESCE(lower(platform), ''::text) <> ALL (ARRAY['capwall2'::text, 'dchubhealer'::text, 'fv'::text, 'mcp-probe'::text, 'p'::text, 'pipeline_mcp'::text, 'probe'::text, 't'::text, 'test'::text, 'v'::text, 'verify'::text]))
    AND COALESCE(lower(platform), ''::text) !~~ '%verify%'::text
    AND COALESCE(lower(platform), ''::text) !~~ '%probe%'::text
    AND COALESCE(lower(platform), ''::text) !~~ '%audit%'::text
    AND COALESCE(user_agent, ''::text) !~* '(python-httpx|python-urllib|urllib|curl/|wget|libwww|node-fetch|undici|axios|got/|go-http|okhttp|java/|requests/|aiohttp|scrapy|httpie|restsharp|dchub-|dchubhealer|self.?heal|value-harness|regression|brain-radar|uptimerobot)'::text
    -- r-reach-canonical-views (2026-07-01): DC Hub self-traffic by SOURCE IP.
    -- Railway egress ranges + the AWS failover-harness fleet (bare 'node' UA).
    AND TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) !~ '^(162\.220\.232\.|152\.55\.17[67]\.|50\.18\.85\.20$)'::text
    AND NOT (TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) ~ '^54\.'::text AND COALESCE(user_agent, ''::text) = 'node'::text)
    AS is_real_external
   FROM mcp_tool_calls;

-- Depends on mcp_calls_identity; re-stated here so the pair is versioned together.
CREATE OR REPLACE VIEW mcp_agent_retention_30d AS
 SELECT agent_id,
    min(client_ip) AS client_ip,
    min(created_at) AS first_call,
    max(created_at) AS last_call,
    count(*) AS calls,
    count(DISTINCT created_at::date) AS active_days,
    count(DISTINCT session_id) AS sessions,
    count(DISTINCT created_at::date) >= 2 AS returned_2nd_day
   FROM mcp_calls_identity
  WHERE created_at >= (now() - '30 days'::interval) AND is_public_ip AND is_real_external
  GROUP BY agent_id;
