"""Monthly per-key MCP call quota — counting rail + the phase-2 enforcement branch.

Phase 1 of moving billing from per-day caps to per-month quotas
(starter "200/day" -> 6,000/month). Verified 2026-07-30: the advertised
per-day caps are NOT enforced for paid keys on the /mcp path (edge worker
passes through, server.mjs gates content depth only, the backend daily
gate is free-tier-only) — so before any monthly quota can BLOCK, we need
a week of real per-key monthly counts to see who would newly hit a wall.

This module:
  * derives each tier's would-be monthly quota from the canonical
    TIER_LIMITS (mcp_daily x 30 — same arithmetic the pricing page
    implies, so display and quota can never disagree),
  * maintains the per-key month rollup in mcp_monthly_usage, written
    from the /api/v1/mcp/track hot path on the same autocommit
    connection as the mcp_call_log insert, and
  * (phase 2) decides whether a call is over quota — quota_decision() —
    behind MONTHLY_QUOTA_ENFORCE, which defaults OFF.

★ MONTHLY_QUOTA_ENFORCE was a NO-OP before phase 2: the flag was named in
this docstring and read by nothing, so "just enable it" was never a
deploy step that could do anything. Flipping it is now a real behaviour
change and must not happen until the log-only window opened 2026-07-30
has been reviewed against live mcp_monthly_usage.

★ THE TIER TRAP (why resolve_quota_tier exists). The tier string that
reaches this module comes from /api/v1/keys/validate, whose entire output
vocabulary is the NODE gate's: enterprise | paid | identified | starter |
developer | free (flask_mcp_endpoints._node_tier_max). 'paid' is what
founding, pro, team and metered all collapse to — and there is no 'paid'
key in TIER_LIMITS. So monthly_quota_for('paid') hits the free fallback
and returns 300/month, which would cap every founding/pro/team customer
at 1/200th of their real quota (a real founding customer did 1,201 calls
in July: fine against pro's 60,000, blocked at 300). Enforcement
therefore MUST route the tier through resolve_quota_tier(), which maps
the Node vocabulary onto real TIER_LIMITS keys and returns None (→ fail
open, logged loudly) rather than silently degrading a tier it does not
recognise to free.

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

import logging
import os
from datetime import date, datetime, timezone

from tier_registry import MCP_DAYS_PER_MONTH, TIER_LIMITS

_LOG = logging.getLogger(__name__)

# One month = 30 x the canonical per-day display number. Kept as a
# function of TIER_LIMITS (not a copied table) so a repriced tier can
# never drift from its monthly quota — and the multiplier itself now
# lives in tier_registry, so the pricing copy that quotes a monthly
# figure and the gate that enforces it cannot disagree.
_DAYS_PER_MONTH = MCP_DAYS_PER_MONTH
_FALLBACK_TIER = "free"

# ── Node-gate vocabulary → real TIER_LIMITS keys ──────────────────────
# LEFT side covers every value flask_mcp_endpoints._node_tier_max can
# emit, plus the granular registry names that may arrive pre-resolved.
# RIGHT side must be a TIER_LIMITS key.
# tests/test_monthly_quota.py asserts this stays a superset of
# _node_tier_max's output vocabulary, so a new Node tier cannot land
# unmapped and quietly inherit the free quota.
_QUOTA_TIER_BY_NAME = {
    # Node gate vocabulary (the only thing validate_key emits)
    "enterprise":    "enterprise",
    "paid":          "pro",        # ← founding / pro / team / metered
    "developer":     "developer",
    "starter":       "starter",
    "identified":    "identified",
    "free":          "free",
    # Registry names, for callers that resolved the granular plan already
    "anonymous":     "anonymous",
    "anon":          "anonymous",
    "pro":           "pro",
    "founding":      "pro",        # founding == pro (tier_registry rule)
    "team":          "pro",
    "metered":       "pro",
    "research_seed": "enterprise",
    "admin":         "admin",
}

# Tiers whose quota must never be reached through the free fallback. A
# paid tier landing on 300/month is the phase-2 landmine, not a config
# nuance — the resolver has its own test for exactly this set.
PAID_QUOTA_TIERS = frozenset({"starter", "developer", "pro", "enterprise", "admin"})

# The upsell offered at the wall, per resolved quota tier. URLs come from
# routes/_stripe_links.py — never hardcode a Stripe link here.
_UPSELL_TIER = {
    "anonymous":  "starter",
    "free":       "starter",
    "identified": "starter",
    "starter":    "developer",
    "developer":  "pro",
    "pro":        "enterprise",
    "enterprise": "enterprise",
    "admin":      "enterprise",
}

PRICING_URL = "https://dchub.cloud/pricing"

# ── Exemptions (the absent-key landmine) ──────────────────────────────
# Not every well-formed key is in mcp_dev_keys: edge-minted dchub_ keys,
# enterprise comp keys and the QA canary live outside it, so all three
# tier sources miss and such a key would otherwise resolve to free/300.
# Those must not be blocked. Overridable with MONTHLY_QUOTA_EXEMPT
# (comma-separated key PREFIXES). Unset → these defaults; set-but-empty →
# no exemptions, so an operator can switch the allowlist off on purpose.
DEFAULT_EXEMPT_PREFIXES = ("dchub_live_", "dchub_enterpri", "dchub_qamcp")

# Statuses on /api/v1/mcp/track that burn quota. Design decision (b):
# only a successful call counts, matching /api/v1/mcp/usage-today, which
# has always filtered status='ok'. A blocked, errored, preview or
# status-less row must not consume a paying customer's month.
_COUNTED_STATUSES = frozenset({"ok"})


def monthly_quota_for(tier):
    """Would-be monthly call quota for a tier (falls back to free).

    DISPLAY/LOG helper — deliberately permissive, and kept that way so the
    log-only snapshot never 500s on a novel tier name. Enforcement must go
    through quota_decision(), which refuses to fall back at all (see the
    tier trap in this module's docstring).
    """
    t = (tier or "").strip().lower()
    limits = TIER_LIMITS.get(t) or TIER_LIMITS[_FALLBACK_TIER]
    return int(limits["mcp_daily"]) * _DAYS_PER_MONTH


def resolve_quota_tier(tier):
    """Map an inbound tier name onto a TIER_LIMITS key, or None.

    None means "we do not know what this caller is entitled to", and the
    caller MUST fail open. Returning free for an unknown name is the exact
    bug this function exists to prevent.
    """
    t = (tier or "").strip().lower()
    if not t:
        return None
    mapped = _QUOTA_TIER_BY_NAME.get(t)
    if mapped:
        return mapped
    if t in TIER_LIMITS:
        return t
    # A tier nobody taught us. Do not guess — an unmapped name that
    # silently became 'free' would cap a paying customer at 300/month.
    _LOG.warning(
        "[monthly_quota] UNMAPPED tier %r — failing open, no quota applied. "
        "Add it to _QUOTA_TIER_BY_NAME before it can be enforced.", t)
    return None


def enforcement_enabled():
    """True only when MONTHLY_QUOTA_ENFORCE is explicitly '1'. Default OFF."""
    return (os.environ.get("MONTHLY_QUOTA_ENFORCE") or "0").strip() == "1"


def exempt_prefixes():
    """Key prefixes that are never blocked (see the exemptions note above)."""
    raw = os.environ.get("MONTHLY_QUOTA_EXEMPT")
    if raw is None:
        return DEFAULT_EXEMPT_PREFIXES
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def is_exempt(api_key):
    prefixes = exempt_prefixes()
    if not api_key or not prefixes:
        return False
    return str(api_key).startswith(prefixes)


def counts_toward_quota(status):
    """Does a tracked call with this status burn a month's quota?

    Design decision (b): only successful calls. The July baseline was
    measured WITH failures included, so post-change usage is <= baseline
    for every key — the blast radius can only shrink, never grow.
    """
    return (status or "").strip().lower() in _COUNTED_STATUSES


def upgrade_url_for(quota_tier):
    """Canonical Stripe Payment Link for the tier above `quota_tier`.

    Lazy import so monthly_quota never hard-depends on routes/ being on
    the path (same convention as tier_registry._stripe_link). Falls back
    to the pricing page — never to a hardcoded Stripe URL.
    """
    try:
        from routes._stripe_links import STRIPE_LINKS
        link = STRIPE_LINKS.get(_UPSELL_TIER.get(quota_tier, "developer"))
        return link or PRICING_URL
    except Exception:
        return PRICING_URL


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
    """View of a key's month: used / quota / remaining.

    `enforce` now reports the REAL MONTHLY_QUOTA_ENFORCE flag (phase 1
    hard-coded it False, which meant a consumer could not tell an
    enforcing deploy from a counting one). It still only describes the
    switch — the block itself is quota_decision(), which resolves the tier
    properly instead of using monthly_quota_for's free fallback.
    """
    used = month_usage(cur, api_key, ts)
    quota = monthly_quota_for(tier)
    return {
        "month": month_bucket(ts).isoformat(),
        "used": used,
        "quota": quota,
        "remaining": max(0, quota - used),
        "tier": (tier or _FALLBACK_TIER),
        "enforce": enforcement_enabled(),
    }


def _wall_message(quota_tier, used, quota, upgrade_url):
    """Copy served at the monthly wall.

    Design decision (a): the wall is a conversion moment, so this mirrors
    the sibling gate (mcp_upgrade_gate.gate_tool_call → {allowed, tier,
    message, upgrade_url}) rather than returning a bare 429. On the MCP
    path content is delivered through the error channel, so the message
    has to carry the offer itself — the agent never sees a status code.
    """
    return (
        f"🤖 Monthly quota reached: {used:,} of {quota:,} DC Hub MCP calls used "
        f"this month on the {quota_tier} tier. The quota resets on the 1st (UTC).\n\n"
        f"Need more now? Upgrade in one step → {upgrade_url}\n"
        f"Compare plans → {PRICING_URL}\n\n"
        "PAUSE and ask your human before purchasing. Nothing else about this key "
        "has changed — every call you have already made is still yours."
    )


def quota_decision(cur, api_key, tier, ts=None):
    """Should this call be allowed under the monthly quota?

    Returns the sibling gate's shape ({allowed, tier, message,
    upgrade_url}) plus the counters, so a caller can render the wall
    without a second lookup.

    FAIL OPEN on every uncertainty — design decision (c). Concretely:
      * enforcement switch off              → allowed (reason enforcement_off)
      * exempt key prefix                   → allowed (reason exempt)
      * tier we cannot map                  → allowed (reason unresolved_tier)
      * DB/table error reading the rollup    → allowed (reason db_error)
    Only a resolved tier whose month rollup is at or over its quota blocks.
    """
    out = {
        "allowed": True,
        "blocked": False,
        "enforce": enforcement_enabled(),
        "reason": "under_quota",
        "month": month_bucket(ts).isoformat(),
        "used": 0,
        "quota": None,
        "remaining": None,
        "tier": (tier or ""),
        "quota_tier": None,
    }

    if not out["enforce"]:
        out["reason"] = "enforcement_off"

    if is_exempt(api_key):
        out["reason"] = "exempt"
        return out

    quota_tier = resolve_quota_tier(tier)
    out["quota_tier"] = quota_tier
    if quota_tier is None:
        # Unknown but well-formed key / unmapped tier: log-only, never a block.
        out["reason"] = "unresolved_tier"
        return out

    quota = int(TIER_LIMITS[quota_tier]["mcp_daily"]) * _DAYS_PER_MONTH
    out["quota"] = quota

    try:
        used = month_usage(cur, api_key, ts)
    except Exception as e:
        # Missing table, DB blip, dead pool — never block on infrastructure.
        _LOG.warning("[monthly_quota] usage read failed, failing open: %s", e)
        out["reason"] = "db_error"
        out["error"] = str(e)[:160]
        return out

    out["used"] = used
    out["remaining"] = max(0, quota - used)

    if used < quota:
        return out

    # At/over quota. Still only a real block when the switch is on; while
    # it is off this reports the wall the key WOULD have hit, which is
    # what the log-only review window is reading.
    out["would_block"] = True
    if not out["enforce"]:
        out["reason"] = "over_quota_log_only"
        return out

    url = upgrade_url_for(quota_tier)
    out.update({
        "allowed": False,
        "blocked": True,
        "reason": "over_monthly_quota",
        "upgrade_url": url,
        "pricing_url": PRICING_URL,
        "message": _wall_message(quota_tier, used, quota, url),
    })
    return out


# ── Wall-activity rollup ──────────────────────────────────────────────
# The wall firing is the conversion event MONTHLY_QUOTA_ENFORCE was
# flipped for, and until this rollup existed nothing recorded it — the
# funnel dashboard could not show when the wall started serving
# allowed=false, to whom, or at which quota. One row per (api_key,
# month), written where the decision is computed, so the funnel can
# report: decisions served at the wall, distinct keys, and the tier/
# quota each key hit. `hits` counts every at-quota decision (including
# log-only ones while the switch is off) and `blocked_hits` counts the
# subset actually served allowed=false, so the metric stays meaningful
# across a flag flip instead of going silently dark.

_WALL_HITS_DDL = """CREATE TABLE IF NOT EXISTS mcp_quota_wall_hits (
      api_key      text NOT NULL,
      month        date NOT NULL,
      quota_tier   text,
      quota        integer,
      hits         integer NOT NULL DEFAULT 0,
      blocked_hits integer NOT NULL DEFAULT 0,
      first_hit_at timestamptz NOT NULL,
      last_hit_at  timestamptz NOT NULL,
      PRIMARY KEY (api_key, month)
    )"""


def ensure_wall_hits_table(cur):
    """Lazy-create the rollup (same to_regclass pattern as the funnel's
    mcp_funnel_real view). Cheap enough per hit: wall hits are rare by
    construction, and the gateway caches decisions 10s–5min per key."""
    cur.execute("SELECT to_regclass('public.mcp_quota_wall_hits')")
    row = cur.fetchone()
    if not (row and row[0]):
        cur.execute(_WALL_HITS_DDL)


def record_wall_hit(cur, api_key, decision, ts=None):
    """Roll up one at-quota decision. No-op unless the decision actually
    reached the wall (would_block), so callers can pass every decision
    through. Caller owns the cursor and the fail-soft guard — a metrics
    write must never change or break the decision itself."""
    if not (decision or {}).get("would_block"):
        return False
    if ts is None:
        ts = datetime.now(timezone.utc)
    ensure_wall_hits_table(cur)
    blocked = 1 if decision.get("blocked") else 0
    # first_hit_at is insert-only (never touched on conflict): it answers
    # "when did this key FIRST hit the wall this month".
    cur.execute(
        """INSERT INTO mcp_quota_wall_hits
               (api_key, month, quota_tier, quota, hits, blocked_hits,
                first_hit_at, last_hit_at)
           VALUES (%s, %s, %s, %s, 1, %s, %s, %s)
           ON CONFLICT (api_key, month)
           DO UPDATE SET hits = mcp_quota_wall_hits.hits + 1,
                         blocked_hits = mcp_quota_wall_hits.blocked_hits
                                        + EXCLUDED.blocked_hits,
                         quota_tier   = EXCLUDED.quota_tier,
                         quota        = EXCLUDED.quota,
                         last_hit_at  = EXCLUDED.last_hit_at""",
        (api_key, month_bucket(ts), decision.get("quota_tier"),
         decision.get("quota"), blocked, ts, ts),
    )
    return True


def wall_stats(cur, ts=None):
    """Wall activity for the funnel dashboard: current-month totals plus
    the by-tier breakdown and the 7d first-hit count (new keys reaching
    the wall this week — the leading edge of the conversion event).

    Missing table reads as zeros, which is accurate — no hit has ever
    been recorded — so the dashboard shows an explicit 0 before the
    first wall hit instead of a hole."""
    out = {
        "enforce": enforcement_enabled(),
        "month": month_bucket(ts).isoformat(),
        "hits_month": 0,
        "blocked_month": 0,
        "keys_month": 0,
        "keys_first_hit_7d": 0,
        "last_hit_at": None,
        "by_tier_month": [],
        "table_exists": True,
    }
    cur.execute("SELECT to_regclass('public.mcp_quota_wall_hits')")
    row = cur.fetchone()
    if not (row and row[0]):
        out["table_exists"] = False
        return out
    bucket = month_bucket(ts)
    cur.execute(
        """SELECT COALESCE(SUM(hits), 0), COALESCE(SUM(blocked_hits), 0),
                  COUNT(*), MAX(last_hit_at)
           FROM mcp_quota_wall_hits WHERE month = %s""",
        (bucket,),
    )
    tot = cur.fetchone() or (0, 0, 0, None)
    # SUM() comes back Decimal — cast before it reaches JSON.
    out["hits_month"] = int(tot[0] or 0)
    out["blocked_month"] = int(tot[1] or 0)
    out["keys_month"] = int(tot[2] or 0)
    if tot[3] is not None:
        out["last_hit_at"] = (
            tot[3].isoformat() if hasattr(tot[3], "isoformat") else str(tot[3]))
    cur.execute(
        "SELECT COUNT(*) FROM mcp_quota_wall_hits "
        "WHERE first_hit_at >= NOW() - INTERVAL '7 days'"
    )
    out["keys_first_hit_7d"] = int((cur.fetchone() or [0])[0])
    cur.execute(
        """SELECT COALESCE(quota_tier, 'unknown'), quota, COUNT(*),
                  COALESCE(SUM(hits), 0), COALESCE(SUM(blocked_hits), 0)
           FROM mcp_quota_wall_hits WHERE month = %s
           GROUP BY 1, 2 ORDER BY 4 DESC""",
        (bucket,),
    )
    out["by_tier_month"] = [
        {"tier": r[0], "quota": (int(r[1]) if r[1] is not None else None),
         "keys": int(r[2] or 0), "hits": int(r[3] or 0),
         "blocked": int(r[4] or 0)}
        for r in (cur.fetchall() or [])
    ]
    return out
