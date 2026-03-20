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
        print("[Redis Cache] ✅ Connected")
    except Exception as e:
        print(f"[Redis Cache] ⚠️ {e}")
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
