"""
DC Hub Redis Cache — Drop-in caching for slow API endpoints.
Usage:
    from redis_cache import cache_get, cache_set, cached_response

    # Manual get/set
    data = cache_get("stats")
    if not data:
        data = expensive_query()
        cache_set("stats", data, ttl=300)

    # Decorator for Flask routes
    @app.route('/api/v1/stats')
    @cached_response(ttl=300, key_prefix="stats")
    def get_stats():
        return expensive_query()
"""
import os
import json
import hashlib
import functools
import time
import redis
from flask import request, jsonify

_redis = None
CACHE_ENABLED = True

def _get_redis():
    global _redis
    if _redis is None:
        url = os.environ.get('REDIS_URL')
        if not url:
            return None
        try:
            _redis = redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
            _redis.ping()
            print("[Redis Cache] Connected")
        except Exception as e:
            print(f"[Redis Cache] Connection failed: {e}")
            _redis = None
    return _redis


def cache_get(key):
    """Get value from Redis cache. Returns None on miss or error."""
    if not CACHE_ENABLED:
        return None
    try:
        r = _get_redis()
        if not r:
            return None
        val = r.get(f"dchub:{key}")
        if val:
            return json.loads(val)
    except Exception:
        pass
    return None


def cache_set(key, value, ttl=300):
    """Set value in Redis cache with TTL (seconds). Default 5 min."""
    if not CACHE_ENABLED:
        return
    try:
        r = _get_redis()
        if not r:
            return
        r.setex(f"dchub:{key}", ttl, json.dumps(value, default=str))
    except Exception:
        pass


def cache_delete(key):
    """Delete a cache key."""
    try:
        r = _get_redis()
        if r:
            r.delete(f"dchub:{key}")
    except Exception:
        pass


def cache_clear(prefix="dchub:"):
    """Clear all DC Hub cache keys."""
    try:
        r = _get_redis()
        if not r:
            return 0
        keys = r.keys(f"{prefix}*")
        if keys:
            r.delete(*keys)
        return len(keys)
    except Exception:
        return 0


def _make_cache_key(prefix, request):
    """Generate cache key from route + query params."""
    args = dict(sorted(request.args.items()))
    raw = f"{prefix}:{request.path}:{json.dumps(args)}"
    return hashlib.md5(raw.encode()).hexdigest()


def cached_response(ttl=300, key_prefix="api"):
    """Flask route decorator that caches JSON responses in Redis."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            # Skip cache for authenticated/admin requests
            if request.headers.get('Authorization') or request.headers.get('X-Admin-Key'):
                return f(*args, **kwargs)

            cache_key = _make_cache_key(key_prefix, request)
            
            # Try cache
            cached = cache_get(cache_key)
            if cached is not None:
                resp = jsonify(cached)
                resp.headers['X-Cache'] = 'HIT'
                resp.headers['X-Cache-TTL'] = str(ttl)
                return resp

            # Cache miss — run the actual function
            result = f(*args, **kwargs)

            # Cache the response if it's JSON-serializable
            try:
                if hasattr(result, 'get_json'):
                    data = result.get_json()
                elif isinstance(result, dict):
                    data = result
                elif isinstance(result, tuple):
                    data = result[0] if isinstance(result[0], dict) else None
                else:
                    data = None

                if data:
                    cache_set(cache_key, data, ttl)
                    if hasattr(result, 'headers'):
                        result.headers['X-Cache'] = 'MISS'
            except Exception:
                pass

            return result
        return wrapper
    return decorator


# Cache purge endpoint (add to your app)
def register_cache_routes(app):
    """Register cache management routes."""
    @app.route('/api/cache/redis/stats', methods=['GET'])
    def redis_cache_stats():
        admin_key = request.headers.get('X-Admin-Key')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY'):
            return jsonify({"error": "unauthorized"}), 401
        try:
            r = _get_redis()
            if not r:
                return jsonify({"status": "disconnected"})
            info = r.info('memory')
            keys = r.keys("dchub:*")
            return jsonify({
                "status": "connected",
                "keys": len(keys),
                "memory_used": info.get('used_memory_human', '?'),
                "uptime_seconds": r.info('server').get('uptime_in_seconds', 0),
            })
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})

    @app.route('/api/cache/redis/purge', methods=['POST'])
    def redis_cache_purge():
        admin_key = request.headers.get('X-Admin-Key')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY'):
            return jsonify({"error": "unauthorized"}), 401
        count = cache_clear()
        return jsonify({"purged": count})
