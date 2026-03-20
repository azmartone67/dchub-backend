"""
DC Hub Redis Cache v2 — Bulletproof inline caching.
Falls back gracefully if Redis is unavailable.
"""
import os
import json
import hashlib

_redis_client = None
_redis_checked = False

def _get_redis():
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    url = os.environ.get('REDIS_URL', '')
    if not url:
        print("[Redis Cache] No REDIS_URL — caching disabled")
        return None
    try:
        import redis
        _redis_client = redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        _redis_client.ping()
        print(f"[Redis Cache] ✅ Connected to {url[:30]}...")
    except Exception as e:
        print(f"[Redis Cache] ⚠️ FAILED: {e} (URL prefix: {url[:40]})")
        _redis_client = None
    return _redis_client

def cache_get(key):
    try:
        r = _get_redis()
        if not r: return None
        val = r.get(f"dchub:{key}")
        return json.loads(val) if val else None
    except: return None

def cache_set(key, value, ttl=300):
    try:
        r = _get_redis()
        if not r: return
        r.setex(f"dchub:{key}", ttl, json.dumps(value, default=str))
    except: pass

def cache_clear():
    try:
        r = _get_redis()
        if not r: return 0
        keys = r.keys("dchub:*")
        if keys: r.delete(*keys)
        return len(keys)
    except: return 0

def cache_stats():
    try:
        r = _get_redis()
        if not r: return {"status": "disconnected"}
        info = r.info('memory')
        return {
            "status": "connected",
            "keys": len(r.keys("dchub:*")),
            "memory": info.get('used_memory_human', '?'),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

def register_cache_routes(app):
    """Register cache management routes."""
    from flask import request, jsonify
    import os

    @app.route('/api/cache/redis/stats', methods=['GET'])
    def redis_cache_stats_route():
        admin_key = request.headers.get('X-Admin-Key')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY'):
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(cache_stats())

    @app.route('/api/cache/redis/purge', methods=['POST'])
    def redis_cache_purge_route():
        admin_key = request.headers.get('X-Admin-Key')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY'):
            return jsonify({"error": "unauthorized"}), 401
        count = cache_clear()
        return jsonify({"purged": count})

def cached_response(ttl=300, key_prefix="api"):
    """No-op decorator when Redis isn't wired yet."""
    import functools
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)
        return wrapper
    return decorator

def debug_redis_env():
    """Debug helper — check what REDIS_URL looks like in the deploy."""
    import os
    url = os.environ.get('REDIS_URL', 'NOT SET')
    masked = url[:20] + '***' if url != 'NOT SET' else url
    result = {"redis_url_prefix": masked, "redis_url_length": len(url)}
    try:
        import redis as _r
        result["redis_package"] = _r.__version__
    except ImportError:
        result["redis_package"] = "NOT INSTALLED"
    try:
        r = _get_redis()
        if r:
            result["ping"] = r.ping()
        else:
            result["ping"] = "client is None"
    except Exception as e:
        result["ping"] = str(e)
    return result
