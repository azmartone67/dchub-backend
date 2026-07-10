-- 2026-07-10: UA-backstop hardening for mcp_calls_identity.is_real_external.
-- Brain-v2-headless (DC Hub QA) + render-verify probe UAs were passing the
-- real-external filter whenever client_name was set (the platform classifier
-- short-circuits on client_name, so the _SCRIPT_INTERNAL_UA regex is the only
-- backstop). GENERATED from mcp_calls_deloop.real_calls_predicate() after
-- adding brain-v2-headless|render-verify to _SCRIPT_INTERNAL_UA — do not
-- hand-edit; edit mcp_calls_deloop.py and re-render. Applied live 2026-07-10.

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
        END <> ALL (ARRAY['curl'::text, 'insomnia'::text, 'internal-dchub'::text, 'node-http-client'::text, 'node-script'::text, 'postman'::text, 'python-script'::text, 'verify'::text])) AND COALESCE(lower(platform), ''::text) !~~ '%dchub%'::text AND (COALESCE(lower(platform), ''::text) <> ALL (ARRAY['capwall2'::text, 'clawith'::text, 'dbg'::text, 'dchubhealer'::text, 'f5r'::text, 'final'::text, 'fix2-v2'::text, 'full'::text, 'fv'::text, 'mcp-probe'::text, 'mcp-vouch'::text, 'p'::text, 'pipeline_mcp'::text, 'probe'::text, 'qa'::text, 'qa-mozilla'::text, 'raw'::text, 'rev'::text, 't'::text, 'test'::text, 'v'::text, 'value-harness'::text, 'verify'::text, 'vinline'::text])) AND COALESCE(lower(platform), ''::text) !~~ '%verify%'::text AND COALESCE(lower(platform), ''::text) !~~ '%probe%'::text AND COALESCE(lower(platform), ''::text) !~~ '%audit%'::text AND COALESCE(lower(platform), ''::text) !~~ '%harness%'::text AND COALESCE(lower(platform), ''::text) !~~ '%test%'::text AND COALESCE(lower(platform), ''::text) !~~ '%check%'::text AND COALESCE(lower(platform), ''::text) !~~ '%diag%'::text AND COALESCE(lower(platform), ''::text) !~~ '%sweep%'::text AND (COALESCE(lower(platform), ''::text) = ''::text OR length(COALESCE(lower(platform), ''::text)) > 2) AND COALESCE(user_agent, ''::text) !~* '(python-httpx|python-urllib|urllib|curl/|wget|libwww|node-fetch|undici|axios|got/|go-http|okhttp|java/|requests/|aiohttp|scrapy|httpie|restsharp|dchub-|dchubhealer|self.?heal|value-harness|regression|brain-radar|brain-v2-headless|render-verify|uptimerobot)'::text AND TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) !~ '^(162\.220\.232\.|152\.55\.17[67]\.|50\.18\.85\.20$)'::text AND NOT (TRIM(BOTH FROM split_part(ip_address, ','::text, 1)) ~ '^54\.'::text AND COALESCE(user_agent, ''::text) = 'node'::text) AS is_real_external
   FROM mcp_tool_calls;;
