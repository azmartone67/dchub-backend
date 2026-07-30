"""Monthly per-key MCP call quota — counting rail only (nothing enforces yet).

Phase 1 of moving billing from per-day caps to per-month quotas
(starter "200/day" -> 6,000/month). Verified 2026-07-30: the advertised
per-day caps are NOT enforced for paid keys on the /mcp path (edge worker
passes through, server.mjs gates content depth only, the backend daily
gate is free-tier-only) — so before any monthly quota can BLOCK, we need
a week of real per-key monthly counts to see who would newly hit a wall.

This module therefore only:
  * derives each tier's would-be monthly quota from the canonical
    TIER_LIMITS (mcp_daily x 30 — same arithmetic the pricing page
    implies, so display and quota can never disagree), and
  * maintains the per-key month rollup in mcp_monthly_usage, written
    from the /api/v1/mcp/track hot path on the same autocommit
    connection as the mcp_call_log insert.

Enforcement (a 429/paywall branch reading these counts) is phase 2,
gated behind MONTHLY_QUOTA_ENFORCE, and must not ship until the log-only
window has been reviewed.

Table (created out-of-band; every caller here is fail-soft if absent):

    CREATE TABLE IF NOT EXISTS mcp_monthly_usage (
      api_key      text NOT NULL,
      month        date NOT NULL,          -- first day of the month, UTC
      calls        integer NOT NULL DEFAULT 0,
      last_call_at timestamptz,
      PRIMARY KEY (api_key, month)
    );

Plain composite PK on purpose: ON CONFLICT names the constraint columns
directly (no partial-index conflict-target trap).
"""

from datetime import date, datetime, timezone

from tier_registry import TIER_LIMITS

# One month = 30 x the canonical per-day display number. Kept as a
# function of TIER_LIMITS (not a copied table) so a repriced tier can
# never drift from its monthly quota.
_DAYS_PER_MONTH = 30
_FALLBACK_TIER = "free"


def monthly_quota_for(tier):
    """Would-be monthly call quota for a tier (falls back to free)."""
    t = (tier or "").strip().lower()
    limits = TIER_LIMITS.get(t) or TIER_LIMITS[_FALLBACK_TIER]
    return int(limits["mcp_daily"]) * _DAYS_PER_MONTH


def month_bucket(ts=None):
    """First day of ts's month in UTC (the rollup key)."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    return date(ts.year, ts.month, 1)


def record_monthly_call(cur, api_key, ts=None):
    """Increment the caller's month bucket. Caller owns the cursor
    (track_tool_call passes its autocommit track connection) and wraps
    this in its own guard — a rollup failure must never fail tracking."""
    cur.execute(
        """INSERT INTO mcp_monthly_usage (api_key, month, calls, last_call_at)
               VALUES (%s, %s, 1, %s)
           ON CONFLICT (api_key, month)
           DO UPDATE SET calls = mcp_monthly_usage.calls + 1,
                         last_call_at = EXCLUDED.last_call_at""",
        (api_key, month_bucket(ts), ts or datetime.now(timezone.utc)),
    )


def month_usage(cur, api_key, ts=None):
    """Calls recorded for this key in ts's month (0 if none)."""
    cur.execute(
        "SELECT calls FROM mcp_monthly_usage WHERE api_key = %s AND month = %s",
        (api_key, month_bucket(ts)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def quota_snapshot(cur, api_key, tier, ts=None):
    """Log-only view of a key's month: used / quota / remaining.

    enforce is hard-False in phase 1 — consumers must not build a block
    on this field until the enforcement phase flips it deliberately.
    """
    used = month_usage(cur, api_key, ts)
    quota = monthly_quota_for(tier)
    return {
        "month": month_bucket(ts).isoformat(),
        "used": used,
        "quota": quota,
        "remaining": max(0, quota - used),
        "tier": (tier or _FALLBACK_TIER),
        "enforce": False,
    }
