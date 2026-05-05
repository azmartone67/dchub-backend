#!/usr/bin/env bash
# neon_diagnostic.sh — Quick diagnostic against Neon Postgres.
# (Filename kept for compatibility; logic targets Neon, not Replit.)
#
# Usage:
#   export NEON_DATABASE_URL='postgres://…@…neon.tech/…'
#   ./replit_diagnostic.sh

set -u

if [ -z "${NEON_DATABASE_URL:-}" ]; then
  echo "ERROR: set NEON_DATABASE_URL first." >&2
  exit 2
fi

PSQL="psql ${NEON_DATABASE_URL}"

echo "── 1. Required tables present? ──────────────────────────────────────"
$PSQL -c "\dt api_keys mcp_call_log" 2>/dev/null \
  || echo "(missing — run migration_001_api_keys.sql)"
echo

echo "── 2. mcp_call_log timeline (last 14 days) ─────────────────────────"
$PSQL -c "SELECT date_trunc('day', timestamp)::date AS day,
                  COUNT(*)                          AS n,
                  COUNT(DISTINCT api_key)           AS keyed_devs,
                  COUNT(DISTINCT session_id)        AS sessions
           FROM mcp_call_log
           WHERE timestamp >= NOW() - INTERVAL '14 days'
           GROUP BY day ORDER BY day DESC;" 2>/dev/null \
  || echo "(table mcp_call_log doesn't exist or is empty)"
echo

echo "── 3. Tool-call mix (last 7 days) ──────────────────────────────────"
$PSQL -c "SELECT tool, COUNT(*) AS n,
                  COUNT(*) FILTER (WHERE status='blocked_paid_only') AS upgrade_blocks,
                  AVG(duration_ms)::int AS avg_ms
           FROM mcp_call_log
           WHERE timestamp >= NOW() - INTERVAL '7 days'
           GROUP BY tool ORDER BY n DESC;" 2>/dev/null
echo

echo "── 4. Active API keys by tier ──────────────────────────────────────"
$PSQL -c "SELECT tier, COUNT(*) AS n,
                  COUNT(*) FILTER (WHERE last_used_at IS NOT NULL) AS ever_used
           FROM api_keys WHERE status='active' GROUP BY tier ORDER BY tier;" 2>/dev/null
echo

echo "── 5. Most-recent paid-only blocks (upgrade signal) ────────────────"
$PSQL -c "SELECT timestamp, tool, platform, api_key
           FROM mcp_call_log
           WHERE status='blocked_paid_only'
           ORDER BY timestamp DESC LIMIT 10;" 2>/dev/null
echo

echo "Done."
