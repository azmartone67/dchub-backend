"""
DC Hub Redis Cache v1.0
========================
Simple cache wrapper for high-traffic API endpoints.
Caches JSON responses in Redis with configurable TTL.

Setup:
  1. Add Redis service in Railway (already done)
  2. REDIS_URL env var auto-linked to backend
  3. Import and use in routes:

     from redis_cache import cache_get, cache_set, cached_endpoint

     # Manual usage:
     data = cache_get("stats:global")
     if not data:
         data = expensive_query()
         cache_set("stats:global", data, ttl=300)

     # Decorator usage (for Flask routes):
     @app.route("/api/v1/stats")
     @cached_endpoint(ttl=300, key_prefix="stats")
     def get_stats():
         return expensive_query()

Requires: redis (pip install redis)
"""

import os
import json
import time
import logging
import hashlib
from functools import wraps

logger = logging.getLogger("dchub.cache")

# ═══════════════════════════════════════════════════════════
# REDIS CONNECTION
# ═══════════════════════════════════════════════════════════

_redis = None
_redis_available = False


def _get_redis():
    """Get or create Redis connection. Returns None if unavailable."""
    global _redis, _redis_available

    if _redis is not None:
        return _redis

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        logger.info("REDIS_URL not set — cache disabled")
        _redis_available = False
        return None

    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
        )
        # Test connection
        _redis.ping()
        _redis_available = True
        logger.info("✅ Redis cache connected")
        return _redis
    except ImportError:
        logger.warning("⚠️ redis package not installed — cache disabled (pip install redis)")
        _redis_available = False
        return None
    except Exception as e:
        logger.warning(f"⚠️ Redis connection failed: {e} — cache disabled")
        _redis_available = False
        _redis = None
        return None


# ═══════════════════════════════════════════════════════════
# CORE CACHE OPERATIONS
# ═══════════════════════════════════════════════════════════

def cache_get(key):
    """Get a cached value. Returns parsed JSON or None."""
    r = _get_redis()
    if not r:
        return None
    try:
        val = r.get(f"dchub:{key}")
        if val:
            return json.loads(val)
    except Exception as e:
        logger.debug(f"Cache get error ({key}): {e}")
    return None


def cache_set(key, data, ttl=300):
    """Cache a value as JSON with TTL in seconds. Default 5 minutes."""
    r = _get_redis()
    if not r:
        return False
    try:
        r.setex(f"dchub:{key}", ttl, json.dumps(data, default=str))
        return True
    except Exception as e:
        logger.debug(f"Cache set error ({key}): {e}")
        return False


def cache_delete(key):
    """Delete a cached value."""
    r = _get_redis()
    if not r:
        return False
    try:
        r.delete(f"dchub:{key}")
        return True
    except Exception as e:
        logger.debug(f"Cache delete error ({key}): {e}")
        return False


def cache_flush(prefix=""):
    """Flush all keys matching a prefix, or all dchub keys if no prefix."""
    r = _get_redis()
    if not r:
        return 0
    try:
        pattern = f"dchub:{prefix}*" if prefix else "dchub:*"
        keys = list(r.scan_iter(match=pattern, count=100))
        if keys:
            r.delete(*keys)
        return len(keys)
    except Exception as e:
        logger.debug(f"Cache flush error: {e}")
        return 0


# ═══════════════════════════════════════════════════════════
# DECORATOR — for Flask routes
# ═══════════════════════════════════════════════════════════

def cached_endpoint(ttl=300, key_prefix="api"):
    """Decorator that caches Flask route responses.
    
    Cache key = prefix + request path + sorted query params.
    Skips cache for authenticated requests (has Authorization header).
    
    Usage:
        @app.route("/api/v1/stats")
        @cached_endpoint(ttl=300, key_prefix="stats")
        def get_stats():
            return jsonify(expensive_query())
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                from flask import request, make_response

                # Don't cache authenticated requests
                if request.headers.get("Authorization") or request.headers.get("X-API-Key"):
                    return f(*args, **kwargs)

                # Build cache key from path + params
                param_str = "&".join(f"{k}={v}" for k, v in sorted(request.args.items()))
                raw_key = f"{request.path}?{param_str}" if param_str else request.path
                cache_key = f"{key_prefix}:{hashlib.md5(raw_key.encode()).hexdigest()}"

                # Try cache
                cached = cache_get(cache_key)
                if cached is not None:
                    resp = make_response(json.dumps(cached), 200)
                    resp.headers["Content-Type"] = "application/json"
                    resp.headers["X-Cache"] = "HIT"
                    return resp

                # Cache miss — call the actual function
                result = f(*args, **kwargs)

                # Cache the response if it's JSON
                try:
                    if hasattr(result, "get_json"):
                        data = result.get_json()
                    elif isinstance(result, tuple):
                        data = json.loads(result[0]) if isinstance(result[0], str) else result[0]
                    elif isinstance(result, str):
                        data = json.loads(result)
                    else:
                        data = result
                    cache_set(cache_key, data, ttl=ttl)
                except Exception:
                    pass  # Don't break the response if caching fails

                return result

            except Exception:
                # If anything goes wrong with caching, just call the function
                return f(*args, **kwargs)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════
# CACHE STATUS — for health/admin endpoints
# ═══════════════════════════════════════════════════════════

def cache_status():
    """Return cache health status for admin/health endpoints."""
    r = _get_redis()
    if not r:
        return {
            "available": False,
            "reason": "Redis not connected",
        }
    try:
        info = r.info("memory")
        key_count = r.dbsize()
        return {
            "available": True,
            "keys": key_count,
            "memory_used_mb": round(info.get("used_memory", 0) / (1024 * 1024), 2),
            "memory_peak_mb": round(info.get("used_memory_peak", 0) / (1024 * 1024), 2),
            "connected_clients": r.info("clients").get("connected_clients", 0),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# RECOMMENDED CACHE STRATEGY
# ═══════════════════════════════════════════════════════════
"""
Add these to main.py after importing redis_cache:

from redis_cache import cached_endpoint, cache_status

# High-traffic, slow-changing endpoints:
@app.route("/api/v1/stats")
@cached_endpoint(ttl=300, key_prefix="stats")        # 5 min
def get_stats(): ...

@app.route("/api/v1/search")
@cached_endpoint(ttl=60, key_prefix="search")         # 1 min
def search_facilities(): ...

@app.route("/api/news/live")
@cached_endpoint(ttl=120, key_prefix="news")           # 2 min
def get_news(): ...

@app.route("/api/v1/map")
@cached_endpoint(ttl=300, key_prefix="map")            # 5 min
def get_map_data(): ...

@app.route("/api/transactions")
@cached_endpoint(ttl=300, key_prefix="txns")           # 5 min
def get_transactions(): ...

# Cache health:
@app.route("/api/health/cache")
def cache_health():
    return jsonify(cache_status())

# Manual purge (admin):
@app.route("/api/admin/cache/flush", methods=["POST"])
def flush_cache():
    prefix = request.args.get("prefix", "")
    count = cache_flush(prefix)
    return jsonify({"flushed": count, "prefix": prefix or "all"})
"""
