"""Response memo for the handful of MCP tools whose MEDIAN exceeds an agent's
patience.

WHY THIS EXISTS RATHER THAN redis_cache.cached_endpoint
-------------------------------------------------------
`redis_cache.cached_endpoint` returns early — uncached — for any request
carrying `Authorization` or `X-API-Key`:

    if request.headers.get("Authorization") or request.headers.get("X-API-Key"):
        return f(*args, **kwargs)

That guard is correct for endpoints whose BODY varies by caller. It makes the
decorator useless here, because the two slowest tools are `@require_pro`: every
real caller is authenticated, so every real caller would miss. Measured over the
14 days to 2026-08-31 (mcp_tool_calls):

    get_composite_site_score   n=41   p50 11,553 ms   p95 15,185 ms
    get_refined_queue          n=65   p50 10,991 ms   p95 12,854 ms

A p50 — not a tail — above 11 s is the problem. Most MCP clients time out
between 10 and 30 s, so roughly half of all callers were already at risk, and a
client that times out does not retry, it moves on. The reported error rate for
both tools is 0.0%, which is not evidence of health: a caller that gave up never
came back to be counted.

Both responses are a pure function of their INPUTS, not of the caller — a
composite score for (lat, lng, state) is the same score for every Pro key that
asks. So the cache key is built from the declared inputs and the auth header is
deliberately ignored. Nothing caller-specific may be added to a cached payload;
if that ever changes, this decorator is the wrong tool.

THREE GUARDS, because a cache that serves the wrong thing is worse than a slow
tool:
  1. Only HTTP 200 is stored, and only when the JSON body does not carry
     `success: false`. Errors, 4xx and 5xx stay uncached, so a transient failure
     cannot be pinned for the whole TTL. (`cached_endpoint` stores any JSON it
     can parse, including a 500.)
  2. Every key part is normalised through `_norm`, so `lat=33.45` and
     `lat=33.4500` share one entry, and floats round to `_COORD_DP` decimals
     (3 dp ≈ 110 m — finer than the source data resolves anyway).
  3. Best-effort throughout. A missing REDIS_URL, an unreachable Redis, an
     unserialisable body or any exception falls through to the live call. This
     can degrade to slow; it cannot degrade to wrong.

The response carries `X-Tool-Cache: HIT|MISS` so a hit rate is measurable from
outside rather than assumed.

Kill switch: `DCHUB_SLOW_TOOL_CACHE=0` disables every memo with no redeploy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import wraps

logger = logging.getLogger(__name__)

# Coordinate rounding for cache keys. 3 dp ≈ 110 m at the equator; the
# substation / fiber / hazard layers behind these tools do not resolve finer,
# so two calls this close are asking the same question.
_COORD_DP = 3


def _enabled() -> bool:
    """False only when explicitly switched off. Default ON."""
    return not str(os.environ.get("DCHUB_SLOW_TOOL_CACHE", "")).strip().lower() \
        in ("0", "false", "no", "off")


def _norm(v) -> str:
    """Normalise one key part so equivalent inputs collapse to one entry.

    Floats round to _COORD_DP; ints keep integer form; everything else is
    lower-cased and stripped. None becomes '' so a missing arg and an empty arg
    are the same key (they are the same request)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{round(v, _COORD_DP):.{_COORD_DP}f}"
    s = str(v).strip()
    # A numeric-looking string is rounded on the same rule, so '33.4500' and
    # '33.45' do not split the cache.
    try:
        return f"{round(float(s), _COORD_DP):.{_COORD_DP}f}"
    except (TypeError, ValueError):
        return s.lower()


def _request_parts(arg_names):
    """Pull the named args out of the live request, JSON body first then query
    string, and return them normalised in the caller's declared order.

    Order comes from `arg_names`, never from request order, so two callers
    sending the same values in a different order share one entry."""
    from flask import request as _rq

    body = {}
    try:
        body = _rq.get_json(silent=True) or {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    parts = []
    for name in arg_names:
        val = body.get(name)
        if val is None:
            val = _rq.args.get(name)
        if val is None:
            val = _rq.form.get(name) if _rq.form else None
        parts.append(f"{name}={_norm(val)}")
    return parts


def _payload_of(result):
    """Best-effort (json_dict, status_code) for whatever a Flask view returned.
    Returns (None, None) when the shape is not something we can safely store."""
    body, status = result, 200
    if isinstance(result, tuple):
        # (body, status) or (body, status, headers)
        if len(result) >= 2 and isinstance(result[1], int):
            status = result[1]
        body = result[0]
    try:
        if hasattr(body, "get_json"):
            # A Response object — status lives on it unless the tuple set one.
            if not isinstance(result, tuple) and hasattr(body, "status_code"):
                status = body.status_code
            return body.get_json(silent=True), status
        if isinstance(body, (dict, list)):
            return body, status
        if isinstance(body, (str, bytes)):
            return json.loads(body), status
    except Exception:
        return None, None
    return None, None


def cache_tool_response(ttl: int, prefix: str, arg_names, coord_args=()):
    """Memo a slow, caller-independent tool response in Redis.

    ttl        seconds to retain a successful response
    prefix     cache-key namespace, e.g. 'composite_score'
    arg_names  the declared inputs that define the answer. Anything NOT named
               here is excluded from the key — so an undeclared arg that
               actually changes the result would be served a stale answer.
               Name every arg the handler reads.
    coord_args reserved for callers that want a different rounding rule per
               argument; unused today, kept so the signature does not have to
               change when the first such caller appears.

    Deliberately ignores Authorization / X-API-Key: these payloads are a pure
    function of arg_names. Do not apply to anything caller-specific."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not _enabled():
                return f(*args, **kwargs)

            cache_key = None
            try:
                from redis_cache import cache_get

                raw = f"{prefix}|" + "|".join(_request_parts(arg_names))
                cache_key = f"tool:{prefix}:{hashlib.md5(raw.encode()).hexdigest()}"

                hit = cache_get(cache_key)
                if hit is not None:
                    from flask import make_response
                    resp = make_response(json.dumps(hit), 200)
                    resp.headers["Content-Type"] = "application/json"
                    resp.headers["X-Tool-Cache"] = "HIT"
                    return resp
            except Exception as e:  # noqa: BLE001 — a cache read must never fail a call
                logger.debug("slow-tool cache read miss (%s): %s", prefix, e)
                cache_key = cache_key  # fall through to the live call

            result = f(*args, **kwargs)

            # Store ONLY a clean 200. An error cached is an error served for the
            # whole TTL, which is the failure mode this guard exists to prevent.
            try:
                if cache_key:
                    data, status = _payload_of(result)
                    storable = (
                        status == 200
                        and isinstance(data, (dict, list))
                        and not (isinstance(data, dict) and data.get("success") is False)
                        and not (isinstance(data, dict) and data.get("error"))
                    )
                    if storable:
                        from redis_cache import cache_set
                        cache_set(cache_key, data, ttl=ttl)
            except Exception as e:  # noqa: BLE001
                logger.debug("slow-tool cache write skipped (%s): %s", prefix, e)

            # Mark the miss when we can do it without rebuilding the response.
            try:
                if hasattr(result, "headers"):
                    result.headers["X-Tool-Cache"] = "MISS"
            except Exception:
                pass
            return result
        return wrapper
    return decorator
