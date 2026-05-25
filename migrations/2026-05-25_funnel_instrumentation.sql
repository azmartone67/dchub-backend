-- Phase ZZZZZ-round40 (2026-05-25) — Funnel instrumentation.
-- Diagnosis: 9,885 paywall hits / 0 dev-key activations in 14d.
-- Root cause invisibility: no event taxonomy + no attribution.

-- Item 1: activation event types in mcp_call_log
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS event_type text;
COMMENT ON COLUMN mcp_call_log.event_type IS
  'Funnel event: key_issued | key_first_use | key_first_paid_tool | tool_call | paywall_block';
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_event_type_ts
  ON mcp_call_log (event_type, timestamp DESC)
  WHERE event_type IS NOT NULL;

-- Item 8: referrer / UA attribution on paywall blocks
ALTER TABLE mcp_call_log
  ADD COLUMN IF NOT EXISTS referrer text,
  ADD COLUMN IF NOT EXISTS user_agent text;
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_paywall_ref
  ON mcp_call_log (referrer) WHERE status = 'blocked_paid_only';

-- Activation funnel view (per dev key)
CREATE OR REPLACE VIEW v_activation_funnel AS
SELECT
  k.api_key, k.email, k.tier, k.created_at AS key_issued_at,
  MIN(m.timestamp) FILTER (WHERE m.status='ok')                                AS first_use_at,
  MIN(m.timestamp) FILTER (WHERE m.status='ok'
       AND m.tool IN ('get_intelligence_index','compare_sites','analyze_site',
                      'get_infrastructure','get_fiber_intel','get_grid_intelligence')) AS first_paid_tool_at,
  COUNT(*) FILTER (WHERE m.status='ok') AS total_ok_calls
FROM mcp_dev_keys k
LEFT JOIN mcp_call_log m ON m.api_key = k.api_key
WHERE k.status='active'
GROUP BY k.api_key, k.email, k.tier, k.created_at;

-- Paywall attribution view (by inferred source)
CREATE OR REPLACE VIEW v_paywall_attribution AS
SELECT
  date_trunc('day', timestamp) AS day,
  COALESCE(NULLIF(referrer,''), 'direct') AS referrer_clean,
  CASE
    WHEN user_agent ILIKE '%claudebot%'                          THEN 'Claude (anthropic)'
    WHEN user_agent ILIKE '%gptbot%' OR user_agent ILIKE '%chatgpt%' THEN 'ChatGPT (openai)'
    WHEN user_agent ILIKE '%perplexity%'                         THEN 'Perplexity'
    WHEN user_agent ILIKE '%cursor%'                             THEN 'Cursor'
    WHEN user_agent ILIKE '%cline%'                              THEN 'Cline'
    WHEN user_agent ILIKE '%mcp-remote%'                         THEN 'Claude Desktop'
    WHEN user_agent ILIKE '%mozilla%'                            THEN 'Browser'
    ELSE 'Other/Unknown'
  END AS source,
  COUNT(*) AS blocks,
  COUNT(DISTINCT COALESCE(api_key, 'anon')) AS unique_visitors
FROM mcp_call_log
WHERE status='blocked_paid_only' AND timestamp >= NOW() - INTERVAL '90 days'
GROUP BY day, referrer_clean, source
ORDER BY day DESC, blocks DESC;

-- Quick conversion-rate read
CREATE OR REPLACE VIEW v_conversion_summary AS
SELECT
  COUNT(*) FILTER (WHERE key_issued_at IS NOT NULL)              AS keys_issued,
  COUNT(*) FILTER (WHERE first_use_at IS NOT NULL)               AS keys_used_once,
  COUNT(*) FILTER (WHERE first_paid_tool_at IS NOT NULL)         AS keys_used_paid_tool,
  ROUND(100.0 * COUNT(*) FILTER (WHERE first_use_at IS NOT NULL)
              / NULLIF(COUNT(*) FILTER (WHERE key_issued_at IS NOT NULL),0), 1) AS pct_activated,
  ROUND(100.0 * COUNT(*) FILTER (WHERE first_paid_tool_at IS NOT NULL)
              / NULLIF(COUNT(*) FILTER (WHERE first_use_at IS NOT NULL),0), 1) AS pct_used_paid_tool
FROM v_activation_funnel;
