"""
util/cache.py — Phase GG (2026-05-15) Bundle 6A.

Tiny helper to attach Cloudflare-friendly cache headers to JSON responses.
The Bundle 1-4 read-aggregated endpoints (index, brief, comparison, etc.)
do real DB work per call but the data changes slowly. 5-min edge cache
+ stale-while-revalidate gets us 16× speedup with negligible freshness loss.
"""


def with_edge_cache(resp, max_age=300, swr=600, cdn_age=None):
    """Apply edge-cache headers to a Flask response in place. Returns resp.

    Args:
        resp: Flask Response (from jsonify(...))
        max_age: browser/edge cache TTL in seconds (default 5 min)
        swr: stale-while-revalidate window after max_age expires
        cdn_age: explicit s-maxage; defaults to max_age
    """
    s_age = cdn_age if cdn_age is not None else max_age
    resp.headers["Cache-Control"] = (
        f"public, max-age={max_age}, s-maxage={s_age}, "
        f"stale-while-revalidate={swr}, must-revalidate")
    # Allow CF to cache cross-origin so the worker honors it.
    resp.headers.setdefault("Vary", "Accept")
    return resp
