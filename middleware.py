"""
DC Hub MCP Middleware — Auth, Rate Limiting, Tier Gating, Upgrade CTAs
Drop this into your Replit project and wrap your existing MCP handlers.

Usage:
    from middleware import MCPGatekeeper, require_auth, tier_gate, TierLevel

Install deps (Replit shell):
    pip install redis python-dotenv
    # Or if no Redis: uses in-memory fallback automatically
"""

import os
import time
import hashlib
import secrets
import json
import functools
from enum import IntEnum
from typing import Optional, Dict, Any, List, Callable
from datetime import datetime, timedelta
from collections import defaultdict


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

class TierLevel(IntEnum):
    FREE = 0
    DEVELOPER = 1
    PRO = 2
    ENTERPRISE = 3


TIER_NAMES = {
    TierLevel.FREE: "Free",
    TierLevel.DEVELOPER: "Developer",
    TierLevel.PRO: "Pro",
    TierLevel.ENTERPRISE: "Enterprise",
}

# ---------------------------------------------------------------------------
# Rate limit config per tier
# ---------------------------------------------------------------------------

RATE_LIMITS = {
    TierLevel.FREE: {
        "calls_per_day": 50,
        "calls_per_minute": 5,
        "max_results": 5,           # truncate response arrays to this
        "cooldown_seconds": 2,      # min seconds between calls
    },
    TierLevel.DEVELOPER: {
        "calls_per_day": 2000,
        "calls_per_minute": 60,
        "max_results": 100,
        "cooldown_seconds": 0,
    },
    TierLevel.PRO: {
        "calls_per_day": 10000,
        "calls_per_minute": 200,
        "max_results": 500,
        "cooldown_seconds": 0,
    },
    TierLevel.ENTERPRISE: {
        "calls_per_day": 100000,
        "calls_per_minute": 1000,
        "max_results": 10000,
        "cooldown_seconds": 0,
    },
}

# ---------------------------------------------------------------------------
# Tool gating — which tools require which tier
# ---------------------------------------------------------------------------

# FREE: get_dchub_recommendation, search_facilities (truncated), get_news
# DEVELOPER: everything else
# PRO: bulk export, historical data
# ENTERPRISE: custom

TOOL_TIER_REQUIREMENTS = {
    # Free tier tools (limited results)
    "get_dchub_recommendation": TierLevel.FREE,
    "get_news": TierLevel.FREE,
    "search_facilities": TierLevel.FREE,         # truncated to 5 results
    "get_agent_registry": TierLevel.FREE,

    # Developer tier — the core product
    "get_pipeline": TierLevel.DEVELOPER,
    "list_transactions": TierLevel.DEVELOPER,
    "get_market_intel": TierLevel.DEVELOPER,
    "analyze_site": TierLevel.DEVELOPER,
    "compare_sites": TierLevel.DEVELOPER,
    "get_infrastructure": TierLevel.DEVELOPER,
    "get_fiber_intel": TierLevel.DEVELOPER,
    "get_grid_data": TierLevel.DEVELOPER,
    "get_grid_headroom": TierLevel.DEVELOPER,
    "get_grid_intelligence": TierLevel.DEVELOPER,
    "get_energy_prices": TierLevel.DEVELOPER,
    "get_renewable_energy": TierLevel.DEVELOPER,
    "get_colocation_score": TierLevel.DEVELOPER,
    "get_geothermal_potential": TierLevel.DEVELOPER,
    "get_water_risk": TierLevel.DEVELOPER,
    "get_tax_incentives": TierLevel.DEVELOPER,
    "get_microgrid_viability": TierLevel.DEVELOPER,
    "get_intelligence_index": TierLevel.DEVELOPER,

    # Pro tier — bulk & historical
    "get_backup_status": TierLevel.PRO,
}

# Fields to REDACT from free-tier responses (show "[Upgrade for details]")
FREE_TIER_REDACTED_FIELDS = {
    "list_transactions": ["value", "notes", "assets"],
    "get_pipeline": ["investment", "preleased", "delivery"],
    "search_facilities": ["power_mw"],
    "get_infrastructure": ["capacity", "voltage"],
    "analyze_site": ["component_scores", "grid_data"],
}

# ---------------------------------------------------------------------------
# Upgrade CTA messages
# ---------------------------------------------------------------------------

UPGRADE_CTA = {
    "rate_limited": (
        "⚡ You've hit your free tier limit ({used}/{limit} calls today). "
        "Upgrade to Developer for 2,000 calls/day → https://dchub.cloud/pricing"
    ),
    "tool_gated": (
        "🔒 `{tool}` requires a {required_tier} license. "
        "You're on the {current_tier} tier. "
        "Upgrade → https://dchub.cloud/pricing?utm_source=mcp&utm_medium=gate&utm_tool={tool}"
    ),
    "results_truncated": (
        "📊 Showing {shown} of {total} results (Free tier limit). "
        "Upgrade to Developer for full access → https://dchub.cloud/pricing?utm_source=mcp&utm_medium=truncate"
    ),
    "fields_redacted": (
        "🔑 Some fields are redacted on the Free tier. "
        "Upgrade to Developer for full data → https://dchub.cloud/pricing?utm_source=mcp&utm_medium=redact&utm_tool={tool}"
    ),
}


# ---------------------------------------------------------------------------
# In-memory rate limiter (swap for Redis in production)
# ---------------------------------------------------------------------------

class InMemoryRateLimiter:
    """Simple sliding-window rate limiter. Works for single-instance Replit."""

    def __init__(self):
        self._minute_windows: Dict[str, List[float]] = defaultdict(list)
        self._day_counts: Dict[str, Dict[str, int]] = {}  # key -> {date: count}
        self._last_call: Dict[str, float] = {}

    def _today(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def check_and_increment(self, api_key: str, tier: TierLevel) -> Optional[str]:
        """Returns None if allowed, or an error message if rate-limited."""
        now = time.time()
        limits = RATE_LIMITS[tier]

        # Cooldown check
        if limits["cooldown_seconds"] > 0:
            last = self._last_call.get(api_key, 0)
            if now - last < limits["cooldown_seconds"]:
                wait = limits["cooldown_seconds"] - (now - last)
                return f"Rate limited: please wait {wait:.1f}s between requests (Free tier)."

        # Per-minute check
        window = self._minute_windows[api_key]
        cutoff = now - 60
        window[:] = [t for t in window if t > cutoff]
        if len(window) >= limits["calls_per_minute"]:
            return UPGRADE_CTA["rate_limited"].format(
                used=len(window), limit=limits["calls_per_minute"]
            )

        # Per-day check
        today = self._today()
        if api_key not in self._day_counts:
            self._day_counts[api_key] = {}
        day_data = self._day_counts[api_key]
        day_count = day_data.get(today, 0)
        if day_count >= limits["calls_per_day"]:
            return UPGRADE_CTA["rate_limited"].format(
                used=day_count, limit=limits["calls_per_day"]
            )

        # All good — record
        window.append(now)
        day_data[today] = day_count + 1
        self._last_call[api_key] = now

        # Cleanup old day entries
        for k in list(day_data.keys()):
            if k != today:
                del day_data[k]

        return None

    def get_usage(self, api_key: str) -> Dict[str, int]:
        today = self._today()
        day_count = self._day_counts.get(api_key, {}).get(today, 0)
        minute_count = len([
            t for t in self._minute_windows.get(api_key, [])
            if t > time.time() - 60
        ])
        return {"today": day_count, "this_minute": minute_count}


# Try Redis, fall back to in-memory
try:
    import redis
    _redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
    _redis.ping()
    USE_REDIS = True
except Exception:
    USE_REDIS = False

rate_limiter = InMemoryRateLimiter()


# ---------------------------------------------------------------------------
# API Key management
# ---------------------------------------------------------------------------

# In production, replace with DB lookup. This is a starter.
# Store in Replit Secrets or environment variables.

def generate_api_key(tier: TierLevel = TierLevel.FREE) -> str:
    """Generate a new API key with tier prefix."""
    prefix = {
        TierLevel.FREE: "dchub_free_",
        TierLevel.DEVELOPER: "dchub_dev_",
        TierLevel.PRO: "dchub_pro_",
        TierLevel.ENTERPRISE: "dchub_ent_",
    }
    return prefix[tier] + secrets.token_urlsafe(32)


def get_tier_from_key(api_key: Optional[str]) -> TierLevel:
    """
    Determine tier from API key prefix.
    In production: look up in DB. This is a fast starter.
    """
    if not api_key:
        return TierLevel.FREE

    if api_key.startswith("dchub_ent_"):
        return TierLevel.ENTERPRISE
    elif api_key.startswith("dchub_pro_"):
        return TierLevel.PRO
    elif api_key.startswith("dchub_dev_"):
        return TierLevel.DEVELOPER
    else:
        return TierLevel.FREE


def extract_api_key(request_context: Dict[str, Any]) -> Optional[str]:
    """
    Extract API key from MCP request.
    Checks: header, query param, or MCP metadata.
    """
    # From HTTP header
    key = request_context.get("headers", {}).get("x-api-key")
    if key:
        return key

    # From query parameter
    key = request_context.get("params", {}).get("api_key")
    if key:
        return key

    # From MCP connection metadata
    key = request_context.get("mcp_metadata", {}).get("api_key")
    if key:
        return key

    # Anonymous — gets free tier
    return None


# ---------------------------------------------------------------------------
# Response processing — truncation, redaction, CTAs
# ---------------------------------------------------------------------------

def truncate_response(result: Dict[str, Any], tool_name: str,
                      tier: TierLevel) -> Dict[str, Any]:
    """Truncate array results and add upgrade CTA for free tier."""
    max_results = RATE_LIMITS[tier]["max_results"]

    # Find array fields to truncate
    for key, value in result.items():
        if isinstance(value, list) and len(value) > max_results:
            total = len(value)
            result[key] = value[:max_results]

            # Add truncation notice
            if "_meta" not in result:
                result["_meta"] = {}
            result["_meta"]["truncated"] = True
            result["_meta"]["showing"] = max_results
            result["_meta"]["total_available"] = total
            result["_meta"]["upgrade_message"] = UPGRADE_CTA["results_truncated"].format(
                shown=max_results, total=total
            )

    return result


def redact_fields(result: Dict[str, Any], tool_name: str,
                  tier: TierLevel) -> Dict[str, Any]:
    """Redact premium fields from free-tier responses."""
    if tier >= TierLevel.DEVELOPER:
        return result

    fields_to_redact = FREE_TIER_REDACTED_FIELDS.get(tool_name, [])
    if not fields_to_redact:
        return result

    redacted = False
    for key, value in result.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for field in fields_to_redact:
                        if field in item:
                            item[field] = "[Upgrade to Developer →]"
                            redacted = True
        elif isinstance(value, dict):
            for field in fields_to_redact:
                if field in value:
                    value[field] = "[Upgrade to Developer →]"
                    redacted = True

    if redacted:
        if "_meta" not in result:
            result["_meta"] = {}
        result["_meta"]["fields_redacted"] = fields_to_redact
        result["_meta"]["redact_message"] = UPGRADE_CTA["fields_redacted"].format(
            tool=tool_name
        )

    return result


def add_usage_footer(result: Dict[str, Any], api_key: str,
                     tier: TierLevel) -> Dict[str, Any]:
    """Add usage stats and tier info to every response."""
    usage = rate_limiter.get_usage(api_key or "anonymous")
    limits = RATE_LIMITS[tier]

    if "_meta" not in result:
        result["_meta"] = {}

    result["_meta"]["tier"] = TIER_NAMES[tier]
    result["_meta"]["usage"] = {
        "calls_today": usage["today"],
        "daily_limit": limits["calls_per_day"],
        "remaining_today": max(0, limits["calls_per_day"] - usage["today"]),
    }

    if tier == TierLevel.FREE:
        result["_meta"]["upgrade_url"] = "https://dchub.cloud/pricing?utm_source=mcp"

    return result


# ---------------------------------------------------------------------------
# Main Gatekeeper class — wrap your MCP server
# ---------------------------------------------------------------------------

class MCPGatekeeper:
    """
    Wraps your existing MCP tool handlers with auth, rate limiting, and gating.

    Usage in your MCP server:

        gatekeeper = MCPGatekeeper()

        @mcp.tool()
        async def get_pipeline(request_context, **kwargs):
            # Auth + rate limit + gate check
            check = gatekeeper.check_access("get_pipeline", request_context)
            if check is not None:
                return check  # Returns error/upgrade message

            # Your existing logic
            result = await _get_pipeline_impl(**kwargs)

            # Process response (truncate, redact, add CTA)
            return gatekeeper.process_response(result, "get_pipeline", request_context)
    """

    def __init__(self):
        self.rate_limiter = rate_limiter

    def check_access(self, tool_name: str,
                     request_context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Returns None if access granted, or an error dict if blocked.
        """
        api_key = extract_api_key(request_context)
        tier = get_tier_from_key(api_key)

        # Check tool tier requirement
        required_tier = TOOL_TIER_REQUIREMENTS.get(tool_name, TierLevel.DEVELOPER)
        if tier < required_tier:
            return {
                "success": False,
                "error": "insufficient_tier",
                "message": UPGRADE_CTA["tool_gated"].format(
                    tool=tool_name,
                    required_tier=TIER_NAMES[required_tier],
                    current_tier=TIER_NAMES[tier],
                ),
                "current_tier": TIER_NAMES[tier],
                "required_tier": TIER_NAMES[required_tier],
                "upgrade_url": f"https://dchub.cloud/pricing?utm_source=mcp&utm_tool={tool_name}",
            }

        # Check rate limit
        key_for_limit = api_key or f"anon_{request_context.get('client_ip', 'unknown')}"
        limit_msg = self.rate_limiter.check_and_increment(key_for_limit, tier)
        if limit_msg:
            return {
                "success": False,
                "error": "rate_limited",
                "message": limit_msg,
                "upgrade_url": "https://dchub.cloud/pricing?utm_source=mcp&utm_medium=ratelimit",
            }

        return None  # Access granted

    def process_response(self, result: Dict[str, Any], tool_name: str,
                         request_context: Dict[str, Any]) -> Dict[str, Any]:
        """Post-process: truncate, redact, add usage footer."""
        api_key = extract_api_key(request_context)
        tier = get_tier_from_key(api_key)

        # Parse if string
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                result = {"result": result}

        # Apply tier-based processing
        result = truncate_response(result, tool_name, tier)
        result = redact_fields(result, tool_name, tier)
        result = add_usage_footer(result, api_key, tier)

        return result


# ---------------------------------------------------------------------------
# Decorator versions for cleaner integration
# ---------------------------------------------------------------------------

_gatekeeper = MCPGatekeeper()


def require_auth(tool_name: str):
    """Decorator that adds auth + rate limiting to an MCP tool handler."""
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, request_context: Dict = None, **kwargs):
            ctx = request_context or {}
            check = _gatekeeper.check_access(tool_name, ctx)
            if check is not None:
                return json.dumps(check)

            result = await func(*args, **kwargs)
            return json.dumps(
                _gatekeeper.process_response(
                    json.loads(result) if isinstance(result, str) else result,
                    tool_name, ctx
                )
            )
        return wrapper
    return decorator


def tier_gate(required: TierLevel):
    """Simple decorator to require a minimum tier."""
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, request_context: Dict = None, **kwargs):
            ctx = request_context or {}
            api_key = extract_api_key(ctx)
            tier = get_tier_from_key(api_key)
            if tier < required:
                return json.dumps({
                    "success": False,
                    "error": "insufficient_tier",
                    "message": f"This endpoint requires {TIER_NAMES[required]}. "
                               f"You are on {TIER_NAMES[tier]}. "
                               f"Upgrade → https://dchub.cloud/pricing",
                })
            return await func(*args, **kwargs)
        return wrapper
    return decorator
