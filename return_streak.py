"""
return_streak.py — progressive return-streak daily-cap boost (r-streak, 2026-07-18)
====================================================================================
The platform's #1 measured constraint is mature key-reuse (~4.9% vs an 8% floor):
agents mint a free key, use it once, and never come back. Both optimization
engines name "progressive unlocks: earn more by returning" as the actuator.

This module is that actuator. A key EARNS a higher daily cap by returning on
distinct days:

    distinct ACTIVE days in the trailing 14 (days with >=1 successful call):
        0-1 day   -> 1.0x base cap   (e.g. base 10 -> 10/day)
        2-3 days  -> 1.5x            (base 10 -> 15/day)
        4-6 days  -> 2.0x            (base 10 -> 20/day)
        7+  days  -> 3.0x            (base 10 -> 30/day)

Activity source: mcp_call_log(api_key, timestamp, status) — the canonical
per-key call telemetry every MCP surface already writes through
POST /api/v1/mcp/track. Only status='ok' (or legacy NULL) calls count, so
blocked/errored attempts don't build streak.

Consumers (all backend-enforced cap points):
    • routes/auto_trial.py  validate_trial_key — dch_trial_ per-key daily cap
      (base 15/day unbound, 50/day bound), relayed live to agents by the Node
      MCP server through POST /api/v1/keys/validate.
    • main.py _gate_mcp_result — Flask /mcp free-tier daily gate
      (base 25/day anon, 100/day identified).
    • mcp_gatekeeper.py _RateLimiter.check — MCP sidecar per-key daily caps.
    • flask_mcp_endpoints.py — SURFACING: /keys/validate, /keys/claim,
      /keys/identify responses carry the streak state + next unlock, because
      the incentive only works if agents KNOW about it.

Design rules:
    • FAIL-OPEN. Any error anywhere -> streak 0 -> base cap. This module must
      never block a call or raise into a caller.
    • CHEAP. Streak is cached in-process ~1h per key, so the cap check adds at
      most one indexed COUNT(DISTINCT day) per key per hour.
    • Caps only ever go UP from base — boosted_cap() never returns < base_cap.
"""

import os
import time
import logging
import threading

logger = logging.getLogger(__name__)

# Trailing window (days) in which distinct active days are counted.
STREAK_WINDOW_DAYS = 14

# (min distinct active days in window, cap multiplier) — checked high-to-low.
STREAK_LADDER = ((7, 3.0), (4, 2.0), (2, 1.5))

# Cache TTL (seconds). Env-tunable without a deploy.
STREAK_CACHE_TTL_S = int(os.environ.get("RETURN_STREAK_CACHE_TTL_S", "3600") or 3600)

# Kill switch: RETURN_STREAK_DISABLE=1 -> everything reports streak 0 (base cap).
_DISABLED = os.environ.get("RETURN_STREAK_DISABLE", "0") == "1"

_cache = {}  # api_key -> (streak_days: int, fetched_at: float)
_cache_lock = threading.Lock()
_CACHE_MAX = 5000


def streak_multiplier(streak_days) -> float:
    """Cap multiplier for a given distinct-active-day count. Fail-open 1.0."""
    try:
        d = int(streak_days or 0)
    except (TypeError, ValueError):
        return 1.0
    for min_days, mult in STREAK_LADDER:
        if d >= min_days:
            return mult
    return 1.0


def boosted_cap(base_cap, streak_days) -> int:
    """Base daily cap raised by the return-streak ladder.

    Never returns less than base_cap (boosts only go up; fail-open = base).
    At base 10 the ladder is exactly 10 / 15 / 20 / 30.
    """
    try:
        base = int(base_cap)
    except (TypeError, ValueError):
        return base_cap  # not a number — refuse to touch it
    if base <= 0:
        return base
    boosted = int(base * streak_multiplier(streak_days) + 0.5)
    return max(base, boosted)


def next_unlock(streak_days, base_cap=None):
    """The NEXT rung of the ladder, or None when already at the top (7+).

    Returns {"active_days": D, "multiplier": M[, "cap": C]} — cap included
    only when base_cap is a positive int.
    """
    try:
        d = int(streak_days or 0)
    except (TypeError, ValueError):
        d = 0
    for min_days, mult in sorted(STREAK_LADDER):  # low rung first
        if d < min_days:
            out = {"active_days": min_days, "multiplier": mult}
            if isinstance(base_cap, int) and base_cap > 0:
                out["cap"] = max(base_cap, int(base_cap * mult + 0.5))
            return out
    return None  # 7+ — max boost already active


def _dsn():
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL") or "")


def _query_streak_days(api_key: str) -> int:
    """DB hit: distinct UTC days with >=1 successful call in the window.

    Raises on failure — get_streak_days() is the fail-open wrapper.
    """
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("no database url configured")
    import psycopg
    with psycopg.connect(dsn, connect_timeout=4, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(DISTINCT (timestamp AT TIME ZONE 'utc')::date)
                     FROM mcp_call_log
                    WHERE api_key = %s
                      AND timestamp >= NOW() - make_interval(days => %s)
                      AND COALESCE(status, 'ok') = 'ok'""",
                (api_key, STREAK_WINDOW_DAYS),
            )
            row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def get_streak_days(api_key) -> int:
    """Distinct active days for this key in the trailing window.

    Cached ~1h per key so hot cap checks stay cheap. FAIL-OPEN: any error
    (no DSN, DB blip, bad key) -> 0, which maps to the base cap.
    """
    if _DISABLED or not api_key or not isinstance(api_key, str):
        return 0
    now = time.time()
    with _cache_lock:
        hit = _cache.get(api_key)
        if hit and (now - hit[1]) < STREAK_CACHE_TTL_S:
            return hit[0]
    try:
        # NOW()-14d spans up to 15 calendar dates — clamp so surfaced copy
        # never claims more active days than the window it cites.
        days = min(_query_streak_days(api_key), STREAK_WINDOW_DAYS)
    except Exception as e:  # fail-open — never let streak math block a call
        logger.debug("return_streak: streak lookup failed (fail-open to base): %s", e)
        return 0
    with _cache_lock:
        if len(_cache) > _CACHE_MAX:
            _cache.clear()
        _cache[api_key] = (days, now)
    return days


# ── Surfacing — the incentive only works if agents KNOW ─────────────────────

_LADDER_TEXT = (
    "Progressive unlock — your daily cap grows with your return streak "
    "(distinct days with >=1 successful call in the trailing 14): "
    "2+ days = 1.5x, 4+ days = 2x, 7+ days = 3x the base cap. "
    "Keep this key and return tomorrow to climb."
)


def ladder_text() -> str:
    """Static one-liner describing the ladder (no DB hit)."""
    return _LADDER_TEXT


def streak_line(streak_days, base_cap=None) -> str:
    """One human/agent-readable line about the current streak + next rung."""
    try:
        d = int(streak_days or 0)
    except (TypeError, ValueError):
        d = 0
    nxt = next_unlock(d, base_cap)
    if isinstance(base_cap, int) and base_cap > 0:
        cap_now = boosted_cap(base_cap, d)
        line = (f"Return streak: {d} active day(s) in the last {STREAK_WINDOW_DAYS} "
                f"— today's cap {cap_now}/day")
        if d >= 2:
            line += f" (boosted from {base_cap})"
        line += "."
        if nxt:
            line += (f" Active on {nxt['active_days']}+ distinct days unlocks "
                     f"{nxt['cap']}/day — return tomorrow to climb.")
        else:
            line += " Max streak boost (3x) active — keep returning to keep it."
        return line
    line = (f"Return streak: {d} active day(s) in the last {STREAK_WINDOW_DAYS}. "
            f"Daily caps grow with your streak: 2+ days = 1.5x, 4+ = 2x, 7+ = 3x.")
    if nxt:
        line += (f" Next unlock at {nxt['active_days']}+ active days "
                 f"({nxt['multiplier']}x) — return tomorrow to climb.")
    else:
        line += " Max streak boost (3x) active — keep returning to keep it."
    return line


def streak_snapshot(api_key, base_cap=None) -> dict:
    """Machine-readable streak block for response payloads.

    ALWAYS returns a dict (fail-open inside). With a positive int base_cap the
    block carries absolute cap numbers; without, it speaks in multipliers so
    it stays honest on surfaces where the base differs per key state.
    """
    try:
        d = get_streak_days(api_key)
    except Exception:
        d = 0
    out = {
        "streak_days": d,
        "window_days": STREAK_WINDOW_DAYS,
        "multiplier": streak_multiplier(d),
        "how_it_works": _LADDER_TEXT,
        "message": streak_line(d, base_cap),
    }
    if isinstance(base_cap, int) and base_cap > 0:
        out["base_cap"] = base_cap
        out["cap_today"] = boosted_cap(base_cap, d)
    nxt = next_unlock(d, base_cap)
    if nxt:
        out["next_unlock"] = nxt
    else:
        out["max_boost_active"] = True
    return out
