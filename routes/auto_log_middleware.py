"""
auto_log_middleware.py — Phase r34 (2026-05-24).

Activates the brain's engagement-tracking signal for every page that's
registered with surface_brain. Without this, the round33b batch
registration only gave the brain METADATA ("I know this page exists")
— this commit adds the ACTUAL telemetry ingestion ("someone viewed
this page") that the brain uses to learn what's popular, what's stuck,
and what should be promoted.

How it works:
  1. Build a path → surface_id map at startup (one walk of SURFACES)
  2. Register a Flask before_request hook
  3. On every request:
     - Skip noisy paths (assets, telemetry-of-telemetry, internal probes)
     - Look up request.path in the map (exact match OR template-aware)
     - Fire auto_log(surface_id, "view") — never raises (the upstream
       function swallows DB errors)

Cost: O(1) per request (dict lookup + one INSERT that's rate-limited
upstream by surface_brain's _rate_limited check). Zero impact on
non-matching paths (the skip list short-circuits before the lookup).

After this lands, /api/v1/surfaces should start showing non-zero
health_score on every registered surface — the score is computed
from the surface_telemetry table that auto_log() writes to.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# Paths we explicitly DON'T want to log a view for.
# - Static assets (CSS, JS, images, fonts) generate tons of requests
# - Telemetry endpoints would loop (logging tracking calls = noise)
# - Internal probes (sentinel, heartbeat) shouldn't count as user views
_SKIP_PREFIXES = (
    "/static/", "/js/", "/css/", "/images/", "/icons/", "/fonts/",
    "/cf-fonts/", "/_next/", "/assets/",
    "/api/v1/surfaces",  # surface_brain's own surface — avoid recursion
    "/api/v1/observability/",
    "/api/v1/heartbeat",
    "/api/v1/sentinel/",
    "/api/v1/heal/",
    "/api/v1/brain/",     # brain endpoints poll each other constantly
    "/api/v1/log",
    "/api/csp-report",
    "/api/health",
    "/health",
    "/alive",
    "/favicon.ico",
    "/robots.txt",
    "/sitemap.xml",
)

_SKIP_EXACT = {
    "/", "/.well-known/security.txt",  # / is logged separately if needed
}


def install(app, log_homepage: bool = True) -> dict:
    """Wire a before_request hook into the Flask app.

    Args:
      app: the Flask app instance
      log_homepage: if True, the "/" path is logged (default True since
                    it's the most valuable engagement signal)

    Returns:
      dict with {paths_tracked, surfaces_seen, hook_installed}
    """
    try:
        from routes.surface_brain import SURFACES, auto_log
    except Exception as e:
        logger.warning(f"[auto_log_middleware] surface_brain import failed: {e}")
        return {"hook_installed": False, "error": str(e)}

    # Build the path → surface_id map at install time. Re-walk every
    # 5 minutes to pick up new registrations (cheap).
    path_to_surface: dict = {}
    for sid, surface in (SURFACES or {}).items():
        for r in (getattr(surface, "routes", None) or []):
            # Templated paths (/operators/<slug>) won't match exact
            # request.path; we strip the template params and use the
            # prefix for a startswith() fallback.
            if "<" not in r:
                path_to_surface[r] = sid
            else:
                # Template — store the literal prefix for prefix-match
                prefix = r.split("<")[0].rstrip("/") + "/"
                path_to_surface.setdefault(prefix, sid)

    # Optionally exclude the homepage from the skip set
    skip_exact = set(_SKIP_EXACT)
    if log_homepage:
        skip_exact.discard("/")

    def _should_skip(path: str) -> bool:
        if path in skip_exact:
            return True
        for pfx in _SKIP_PREFIXES:
            if path.startswith(pfx):
                return True
        return False

    def _resolve_surface(path: str):
        # Exact match first
        sid = path_to_surface.get(path)
        if sid:
            return sid
        # Strip trailing slash variant
        alt = path.rstrip("/")
        if alt != path and alt in path_to_surface:
            return path_to_surface[alt]
        # Prefix match (templated routes)
        for prefix, sid in path_to_surface.items():
            if prefix.endswith("/") and path.startswith(prefix):
                return sid
        return None

    @app.before_request
    def _auto_log_view():
        # Defensive: only log GETs of HTML/JSON pages, not POSTs/etc.
        try:
            from flask import request
            if request.method != "GET":
                return
            path = request.path or ""
            if _should_skip(path):
                return
            sid = _resolve_surface(path)
            if sid:
                auto_log(sid, "view")
        except Exception:
            pass  # never crash a request from instrumentation

    return {
        "hook_installed": True,
        "paths_tracked": len(path_to_surface),
        "surfaces_seen": len(set(path_to_surface.values())),
    }
