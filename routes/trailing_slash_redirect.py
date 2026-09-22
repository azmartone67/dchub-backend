"""routes/trailing_slash_redirect.py — a trailing-slash spelling of a real page
answers 301 to the page instead of 404.

Flask rules are strict_slashes, so /pricing/ never matched the /pricing rule.
Measured through the edge 2026-09-22, browser UA, redirects not followed:

    404 /pricing/           application/json  x-dc-hub-served-by: railway-primary
    404 /connect/           application/json  (same)
    404 /facilities/in/nl/  text/html         (same)
    301 /markets/atlanta/ -> /markets/atlanta   (main._check_prefix_redirects, r73)

Any external link carrying the slash landed in GSC's 404 bucket (3,763 URLs on
2026-09-21), and /pricing and /connect are money doors.

THE RULE. A GET or HEAD whose response is a 404, on a path ending in "/" (not
"/" itself), answers 301 to the path with its trailing slashes stripped, query
string kept. Only when routing matches the stripped path, for the same method,
to a rule OTHER than the one that just answered 404.

★ ONLY A 404 RESPONSE IS CONVERTED, never a route. There are two kinds of 404,
  and a hook that decides before the view runs can only see the first:
    routing  /pricing/ matches no rule at all (the slash spelling of 2,472 GET
             rules in the booted app, 2026-09-22)
    view     /facilities/in/nl/ matches /facilities/<path:slug>, whose view
             looks up slug "in" and answers its own HTML 404 (67 GET rules sit
             behind a catch-all that way)
  Deciding from routing alone would be wrong for the second kind. /upgrade/ has
  its own rule (the direct-checkout 302 to Stripe, be#5223) while /upgrade
  routes to pair_code, and /api/v1/facilities/8484/ answers 200 from the
  <path:slug> handler. So the hook only notes a candidate before the view runs,
  and the conversion happens in after_this_request. That runs before every
  app-level after_request hook and sees the final response. The answers some
  hooks already give on routing-404 paths are not 404s and stay as they are:
  smart_404's /research/ prefix 302, and the r73 /markets/<x>/ 301.

★ THE REDIRECT IS NOT CHARGED. rate_limit_before has already charged the request
  by the time any 404 exists: it is a before_request hook, and a routing 404 is
  raised only after those run. A visitor following /connect/ would pay twice,
  once for the hop and once for the page. The conversion gives the hop's token
  back (rate_limiter.refund_request), so one page view costs one token.

★ NO LOOP. The target never ends in "/", so it can never be converted again. A
  stripped path that routing matches nothing for is never a target, so a path
  with no page under either spelling keeps its 404.

★ /api/ IS OUT OF SCOPE. Its 404 is a JSON contract: smart_404's suggestions,
  built for agents. robots.txt disallows /api/ for crawlers, so GSC does not
  report it. And /api/ has three more per-request meters a hop would charge:
  enforce_tier_rate_limits, the paid-key usage counter, and auto-issued trial
  keys.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from flask import after_this_request, redirect, request
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.routing import RoutingException

logger = logging.getLogger(__name__)

# Its own header: dchub-frontend's worker overwrites x-dc-hub-source on every
# response it proxies (worker-phase282-failover), so a value there never
# reaches the caller.
MARKER = "X-DC-Slash-Redirect"

_METHODS = ("GET", "HEAD")
# RFC 3986 pchar, plus "/" between segments. quote() never escapes letters,
# digits or "-._~" and escapes everything not listed: "?", "#", "\", CR/LF,
# spaces, non-ASCII. So a decoded path cannot become a query, a backslash cannot
# become a slash, and nothing can break the header.
_PATH_SAFE = "/:@!$&'()*+,;="
# The query string arrives already percent-encoded: keep "%" so its escapes
# survive, and "?" and "/", which are legal in a query.
_QUERY_SAFE = _PATH_SAFE + "?%"


def stripped_target(adapter, path, method, served_rule=None):
    """The path a trailing-slash spelling should 301 to, or None.

    `adapter` is a bound werkzeug MapAdapter. `served_rule` is the rule that
    matched `path` (request.url_rule), None when routing matched nothing. It
    only consults routing and never runs a view.
    """
    if method not in _METHODS or path == "/" or not path.endswith("/"):
        return None
    if path.startswith("/api/"):
        return None
    target = path.rstrip("/")
    # "//host" is a network-path reference: as a Location it leaves the site.
    # ("/\host", which browsers read the same way, cannot get out: _location
    # escapes the backslash to %5C.)
    if not target or target.startswith("//"):
        return None
    try:
        rule, _args = adapter.match(target, method=method, return_rule=True)
    except (HTTPException, RoutingException):
        # NotFound: no page under either spelling. MethodNotAllowed: a GET
        # would land on a 405. RequestRedirect: routing wants yet another URL.
        return None
    if rule is served_rule:
        # The rule that answered 404 would answer the stripped path too.
        return None
    return target


def _location(target):
    location = quote(request.script_root + target, safe=_PATH_SAFE)
    if request.query_string:
        location += "?" + quote(request.query_string, safe=_QUERY_SAFE)
    return location


def _redirect_to(location):
    response = redirect(location, code=301)
    response.headers[MARKER] = "trailing-slash"
    # A day, like the facility-slug 301s. A browser or edge cache holding this
    # 301 cannot hide a slash rule added later for longer than that.
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


def _refund():
    try:
        from rate_limiter import refund_request
        refund_request()
    except Exception:
        logger.warning("trailing-slash 301: rate-limit refund failed", exc_info=True)


def install(app):
    """Register the candidate hook on `app` and return it.

    Where it sits in the before_request chain does not decide the result. It
    never answers a request itself, and a request some earlier hook answers
    was never a 404 to convert.
    """

    def _note_trailing_slash_candidate():
        path = request.path or ""
        if request.method not in _METHODS or path == "/" or not path.endswith("/"):
            return None
        routing_error = request.routing_exception
        if routing_error is not None and not isinstance(routing_error, NotFound):
            return None
        try:
            target = stripped_target(app.create_url_adapter(request), path,
                                     request.method, request.url_rule)
        except Exception:
            logger.warning("trailing-slash 301: routing the stripped path failed",
                           exc_info=True)
            return None
        if target is None:
            return None
        location = _location(target)

        @after_this_request
        def _convert_404(response):
            if response.status_code != 404:
                return response
            _refund()
            return _redirect_to(location)

        return None

    app.before_request(_note_trailing_slash_candidate)
    return _note_trailing_slash_candidate
