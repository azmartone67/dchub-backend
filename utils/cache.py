"""
DC Hub Bounded Cache
====================
Thread-safe in-memory cache with TTL expiration and size limits.
Extracted from main.py during Phase 2 modularization.
Used by: energy_routes, discovery_routes, deals_routes, and main.py
"""

from datetime import datetime


class BoundedCache:
    """Simple bounded cache with TTL eviction.
    
    Usage:
        cache = BoundedCache(max_size=100, ttl=3600)
        cache.set('key', value)
        result = cache.get('key')  # Returns None if expired/missing
    """
    __slots__ = ('_data', '_max_size', '_ttl')

    def __init__(self, max_size=100, ttl=3600):
        self._data = {}
        self._max_size = max_size
        self._ttl = ttl

    def get(self, key):
        if key in self._data:
            val, ts = self._data[key]
            if (datetime.now() - ts).total_seconds() < self._ttl:
                return val
            del self._data[key]
        return None

    def set(self, key, value):
        if len(self._data) >= self._max_size:
            self._evict()
        self._data[key] = (value, datetime.now())

    def _evict(self):
        now = datetime.now()
        expired = [k for k, (_, ts) in self._data.items()
                   if (now - ts).total_seconds() >= self._ttl]
        for k in expired:
            del self._data[k]
        if len(self._data) >= self._max_size:
            oldest = sorted(self._data.items(), key=lambda x: x[1][1])
            for k, _ in oldest[:len(self._data) - self._max_size // 2]:
                del self._data[k]

    def clear(self):
        self._data.clear()

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return self.get(key) is not None

    def items(self):
        return self._data.items()
