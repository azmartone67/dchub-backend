"""A route's own `no-store` on the force-public stats paths must survive.

get_stats() answers the boot window with a boot-degraded (or boot-stub) 200 and
sets `Cache-Control: no-store` so no edge keeps it. The after_request hook
add_security_headers() force-stamps /api/v1/stats, /api/v1/site/stats and
/api/v1/discovery/last-7d public, and that branch ran BEFORE the clause that
respects a view's private/no-store. So the boot payload shipped as
`public, max-age=120, s-maxage=600, stale-while-revalidate=86400`.

Measured 2026-09-24: api.dchub.cloud/api/v1/stats served a boot-degraded body
generated 01:12:24Z (cf-cache-status HIT, age climbing) until a zone-wide purge
at 01:31Z; URL purges did not evict it.

House rule: tests never import main.py, so the hook is lifted out by AST and run
against a bare Flask request context. A NameError here means the hook grew a new
free name; add it to _NS, don't catch it.
"""
import ast
import logging
import os

import pytest
from flask import Flask, jsonify, request

_MAIN = os.path.join(os.path.dirname(__file__), "..", "main.py")
_FORCE_PUBLIC = ("/api/v1/stats", "/api/v1/site/stats", "/api/v1/discovery/last-7d")


@pytest.fixture(scope="module")
def hook():
    src = open(_MAIN, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "add_security_headers")
    fn.decorator_list = []
    ns = {"request": request, "_CACHE_PATHS": {}, "_match_html_cache": lambda p: None,
          "ADMIN_ANALYTICS_AVAILABLE": False, "user_analytics": None,
          "logger": logging.getLogger(__name__)}
    exec(compile(ast.Module([fn], []), _MAIN, "exec"), ns)
    return ns["add_security_headers"]


def _run(hook, path, route_cc=None):
    with Flask(__name__).test_request_context(path, method="GET"):
        r = jsonify({"_cache": {"served_from": "boot-degraded"}})
        if route_cc is not None:
            r.headers["Cache-Control"] = route_cc
        return hook(r)


@pytest.mark.parametrize("path", _FORCE_PUBLIC)
def test_route_no_store_is_not_restamped_public(hook, path):
    out = _run(hook, path, "no-store")
    assert out.headers["Cache-Control"] == "no-store"
    assert out.headers.get("CDN-Cache-Control") is None


@pytest.mark.parametrize("path", _FORCE_PUBLIC)
def test_normal_stats_200_is_still_force_public(hook, path):
    # Control: the force-public branch must still fire for an ordinary 200
    # (the memo path sends `public, max-age=60`), or the fix above is vacuous.
    for route_cc in (None, "public, max-age=60"):
        out = _run(hook, path, route_cc)
        assert out.headers["Cache-Control"] == (
            "public, max-age=120, s-maxage=600, stale-while-revalidate=86400")
        assert out.headers["CDN-Cache-Control"] == "public, max-age=600"
