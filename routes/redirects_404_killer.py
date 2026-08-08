"""redirects_404_killer.py — r79 (2026-06-03)

Catch-all redirect blueprint for known-dead URLs that were biting users
(including during live demos) before we shipped the link-check CI gate.

Each entry below was identified during the 2026-06-03 audit of dchub.cloud.
The URL on the left 404'd; the URL on the right is the closest working
canonical destination. All redirects use HTTP 302 (temporary) so we can
delete entries here once the actual destination route is built without
poisoning SEO.

Rule of thumb: a URL ends up here when (a) it 404'd in production, and
(b) it was referenced from a nav element, footer, press release, or any
file in dchub-frontend/. Don't add a redirect for a URL that nothing
references — that's just dead code.

How to add a new redirect: append to _REDIRECTS dict, restart. No new
routes needed — the path catcher at the bottom resolves any unmatched
path. Add to _PREFIX_REDIRECTS for "/foo/*" patterns where the whole
sub-tree should go to one place.
"""

from __future__ import annotations

from flask import Blueprint, redirect

redirects_404_killer_bp = Blueprint("redirects_404_killer", __name__)


# Exact-path redirects. Left = dead URL we saw in production; right =
# closest working destination.
_REDIRECTS: dict[str, str] = {
    # /team didn't exist but was linked from footer in a few places
    "/team":                  "/about",

    # /dcpi/* phantoms that nothing on the live page actually links but
    # an agent or stale press release might guess at
    "/dcpi/about":            "/dcpi",
    "/dcpi/freshness":        "/dcpi",
    "/dcpi/explainer":        "/dcpi/methodology/",
    "/dcpi/faq":              "/dcpi",
    "/dcpi/markets":          "/dcpi",

    # Lone-word redirects users / search engines try
    "/methodology":           "/dcpi/methodology/",
    "/dcpi-methodology":      "/dcpi/methodology/",
    "/data-quality":          "/dcpi/methodology/",
    "/data-sources":          "/dcpi/methodology/",

    # /transactions/* sub-paths nothing links but easy to guess
    "/transactions/list":     "/transactions",
    "/transactions/recent":   "/transactions",

    # /research bare path. The original note here — "out-of-repo CF Pages
    # config makes /research/* unreachable, until that's fixed in the CF
    # Dashboard" — was true when written (2026-06-03) and is NOT true any
    # more, so do not reason from it. The unreachability was in-repo all
    # along: dchub-frontend's _worker.js forwarded /research/* to Railway
    # without asking the Pages ASSETS binding, and this table then redirected
    # everything away. Fixed in dchub-frontend #1139 (ASSETS-first guard), so
    # the real research pages now serve. Kept because /research bare is still
    # owned by main.py's curated page, not by anything here.
    "/research":              "/grid-intelligence",
    "/research/dcpi":         "/dcpi",
    "/research/methodology":  "/dcpi/methodology/",
}

# Prefix redirects: any path starting with key → value. Order matters
# (longer keys first to avoid greedy match).
#
# ★ These are a LAST RESORT, not a routing layer. Until 2026-08-08 they were
# wired into a before_request hook in main.py, which runs BEFORE the URL map —
# so they fired on paths that had working handlers and made this file the de
# facto owner of everything under /research/. That killed open_data_bp's
# /research/<slug> DCPI market pages outright for two months. They now run from
# the 404 handler: real routes win, and only genuinely unmatched paths land
# here. Keep it that way — a redirect table that outranks the router is not a
# redirect table.
_PREFIX_REDIRECTS: list[tuple[str, str]] = [
    # /research/grid-intelligence/<region> → /grid-intelligence. Now a true
    # fallback: dchub-frontend serves the real per-ISO briefs as Pages assets
    # (#1139), so these only fire if someone reaches Railway directly or asks
    # for a region that does not exist.
    ("/research/grid-intelligence/", "/grid-intelligence"),
    # The bare path is load-bearing: the static Briefs index that used to own
    # it was retired in #1139 because the backend-rendered /grid-intelligence
    # IS that index, one generation on. Note this one is ALSO reached from
    # research_market()'s not-found fallback, because /research/<slug> matches
    # it dynamically and a 404 returned from a view never sees errorhandler(404).
    ("/research/grid-intelligence",  "/grid-intelligence"),

    # Catch-all for /research/* — any sub-path lands on /grid-intelligence
    # (the most likely intent of someone clicking a /research/ link).
    ("/research/",                    "/grid-intelligence"),
]


def _register_exact(path: str, dest: str) -> None:
    """Register a single exact-path 302 redirect on the blueprint.

    Uses a closure to bind dest into a unique view function — Flask
    requires unique endpoint names per blueprint."""
    endpoint = "redir_" + path.strip("/").replace("/", "_").replace("-", "_") or "redir_root"

    def view_fn():
        return redirect(dest, code=302)

    view_fn.__name__ = endpoint
    redirects_404_killer_bp.add_url_rule(
        path, endpoint=endpoint, view_func=view_fn, methods=["GET"]
    )


# Wire up all exact redirects at import time
for _src, _dst in _REDIRECTS.items():
    _register_exact(_src, _dst)


# Prefix redirects: register a catch-all handler that checks _PREFIX_REDIRECTS.
# This must be added to main.py separately because Flask blueprints can't
# capture arbitrary paths. We expose a helper function for main.py to call:

def maybe_prefix_redirect(path: str):
    """Return a Flask redirect response if `path` matches a prefix in
    _PREFIX_REDIRECTS, else None. Called from main.py's catch-all 404 handler
    (or registered as a before_request hook by app factory)."""
    for src_prefix, dest in _PREFIX_REDIRECTS:
        if path.startswith(src_prefix):
            return redirect(dest, code=302)
    return None
