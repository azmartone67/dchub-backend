-- 2026-07-01 r-platform-tag-excl: platform-column exclusions for internal tags
--
-- Follow-up to 2026-07-01_mcp_calls_identity_self_traffic_exclusions.sql.
-- value-harness (our own test harness, ~100 rotating cloud IPs/wk) was excluded
-- from is_real_external ONLY by its user-agent string ('dchub-value-harness/1.0')
-- — a UA change would leak it straight into every reach/agent count. This
-- revision pins the write-time `platform` column too, and drops the other
-- observed internal QA tags that WERE passing (dbg/pw/raw/full/paywall-test/
-- capcheck/mcp-vouch/qa-mozilla/clawith/cap-diag-fresh-*/fix2-v2/... — 1 IP
-- each, ~15 fake "agents" over 3 weeks). New platform rules: NOT LIKE %dchub%/
-- %harness%/%test%/%check%/%diag%/%sweep%, extended exact list, and length<=2
-- tags dropped (ad-hoc curl test params; NULL/empty platform still KEPT).
--
-- GENERATED from mcp_calls_deloop.real_calls_predicate() + the IP-level
-- self-traffic exclusions of the prior revision — do not hand-edit the
-- predicate here; edit mcp_calls_deloop.py and re-render.

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
    ((CASE
        WHEN NULLIF(LOWER(client_name), '') IS NOT NULL
             AND client_name !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN LOWER(client_name)
        WHEN user_agent ILIKE '%dchub-%' OR user_agent ILIKE '%dchubhealer%'
            OR user_agent ILIKE '%brain-v2-headless%' OR user_agent ILIKE '%brain-radar%'
            OR user_agent ILIKE '%uptimerobot%'
            THEN 'internal-dchub'
        WHEN user_agent ILIKE '%@modelcontextprotocol/sdk%'
            OR user_agent ILIKE '%modelcontextprotocol%'
            THEN 'mcp-sdk'
        WHEN user_agent ILIKE '%mcp-inspector%'
            THEN 'mcp-inspector'
        WHEN user_agent ILIKE '%n8n%'
            THEN 'n8n'
        WHEN user_agent ILIKE '%smithery%'
            THEN 'smithery'
        WHEN user_agent ILIKE '%chatgpt%' OR user_agent ILIKE '%openai%'
            THEN 'chatgpt'
        WHEN user_agent ILIKE '%claude%' OR user_agent ILIKE '%anthropic%'
            THEN 'claude'
        WHEN user_agent ILIKE '%perplexity%'
            THEN 'perplexity'
        WHEN user_agent ILIKE '%gemini%' OR user_agent ILIKE '%googleother%'
            THEN 'gemini'
        WHEN user_agent ILIKE '%groq%'
            THEN 'groq'
        WHEN user_agent ILIKE '%cursor%'
            THEN 'cursor'
        WHEN user_agent ILIKE '%windsurf%' OR user_agent ILIKE '%codeium%'
            THEN 'windsurf'
        WHEN user_agent ILIKE '%continue%'
            THEN 'continue.dev'
        WHEN user_agent ILIKE '%cody%' OR user_agent ILIKE '%sourcegraph%'
            THEN 'sourcegraph-cody'
        WHEN user_agent ILIKE '%copilot%'
            THEN 'github-copilot'
        WHEN user_agent ILIKE '%cline%'
            THEN 'cline'
        WHEN user_agent ILIKE '%phind%'
            THEN 'phind'
        WHEN user_agent ILIKE '%you.com%' OR user_agent ILIKE '%youbot%'
            THEN 'you.com'
        WHEN user_agent ILIKE '%meta-external%' OR user_agent ILIKE '%llama%'
            THEN 'meta-ai'
        WHEN user_agent ILIKE '%applebot-extended%'
            THEN 'apple-intelligence'
        WHEN user_agent ILIKE '%curl%'
            THEN 'curl'
        WHEN user_agent ILIKE '%python%' OR user_agent ILIKE '%requests%'
            THEN 'python-script'
        WHEN user_agent ILIKE '%node-fetch%' OR user_agent ILIKE '%undici%'
            OR user_agent ILIKE '%axios%' OR user_agent ILIKE '%got/%'
            THEN 'node-http-client'
        WHEN user_agent ILIKE '%node%'
            THEN 'node-script'
        WHEN user_agent ILIKE '%postman%'
            THEN 'postman'
        WHEN user_agent ILIKE '%insomnia%'
            THEN 'insomnia'
        ELSE 'unknown'
    END NOT IN ('curl','insomnia','internal-dchub','node-http-client','node-script','postman','python-script','verify')) AND (COALESCE(LOWER(platform), '') NOT LIKE '%dchub%' AND COALESCE(LOWER(platform), '') NOT IN ('capwall2','clawith','dbg','dchubhealer','f5r','final','fix2-v2','full','fv','mcp-probe','mcp-vouch','p','pipeline_mcp','probe','qa','qa-mozilla','raw','rev','t','test','v','value-harness','verify','vinline') AND COALESCE(LOWER(platform), '') NOT LIKE '%verify%' AND COALESCE(LOWER(platform), '') NOT LIKE '%probe%' AND COALESCE(LOWER(platform), '') NOT LIKE '%audit%' AND COALESCE(LOWER(platform), '') NOT LIKE '%harness%' AND COALESCE(LOWER(platform), '') NOT LIKE '%test%' AND COALESCE(LOWER(platform), '') NOT LIKE '%check%' AND COALESCE(LOWER(platform), '') NOT LIKE '%diag%' AND COALESCE(LOWER(platform), '') NOT LIKE '%sweep%' AND (COALESCE(LOWER(platform), '') = '' OR LENGTH(COALESCE(LOWER(platform), '')) > 2)) AND COALESCE(user_agent,'') !~* '(python-httpx|python-urllib|urllib|curl/|wget|libwww|node-fetch|undici|axios|got/|go-http|okhttp|java/|requests/|aiohttp|scrapy|httpie|restsharp|dchub-|dchubhealer|self.?heal|value-harness|regression|brain-radar|uptimerobot)')
    -- r-reach-canonical-views (2026-07-01): DC Hub self-traffic by SOURCE IP.
    -- Railway egress ranges + the AWS failover-harness fleet (bare 'node' UA).
    AND TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) !~ '^(162\.220\.232\.|152\.55\.17[67]\.|50\.18\.85\.20$)'::text
    AND NOT (TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) ~ '^54\.'::text AND COALESCE(user_agent, ''::text) = 'node'::text)
    ) AS is_real_external
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
