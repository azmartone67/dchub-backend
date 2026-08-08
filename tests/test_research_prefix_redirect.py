"""Guards the /research/* routing fix (2026-08-08).

For two months every /research/* path 302'd to /grid-intelligence, because
redirects_404_killer's prefix table was wired into a `before_request` hook.
before_request runs BEFORE the URL map, so the redirect fired on paths that
had a perfectly good handler: open_data_bp's /research/<slug> DCPI market
pages could never run, though research_market() renders a
"Cite as: https://dchub.cloud/research/<slug>" line intended for press.

Measured live 2026-08-08, before the fix:
    /research/phoenix-az        302 -> /grid-intelligence
    /research/northern-virginia 302 -> /grid-intelligence
    /research/dallas-tx         302 -> /grid-intelligence
    /research/atlanta-ga        302 -> /grid-intelligence

Per CLAUDE.md these tests never import main.py. The hook and the 404 handler
are pulled out of the source with `ast` and executed against a minimal Flask
app, so what is asserted is the shipped wiring rather than a paraphrase.
"""

import ast
import re
import pathlib

import pytest

flask = pytest.importorskip("flask")

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _main_src():
    return (ROOT / "main.py").read_text()


# ---------------------------------------------------------------- source facts


def test_before_request_hook_no_longer_fires_prefix_redirects():
    """The hook must not call maybe_prefix_redirect — that IS the bug.

    Asserted against the parsed function body, not a file-wide grep: the name
    still appears elsewhere in main.py (the import, and the 404 handler that
    now legitimately calls it), so a substring check would pass on the broken
    wiring.
    """
    tree = ast.parse(_main_src())
    hook = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_check_prefix_redirects"),
        None,
    )
    assert hook is not None, "_check_prefix_redirects vanished — re-point this test"

    called = {
        n.func.id
        for n in ast.walk(hook)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "maybe_prefix_redirect" not in called, (
        "before_request calls maybe_prefix_redirect again — this runs before the "
        "URL map, so every /research/<slug> market page is dead once more"
    )


def test_404_handler_consults_the_prefix_table():
    """The redirects have to live SOMEWHERE, or known-dead URLs start 404ing."""
    tree = ast.parse(_main_src())
    handler = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "smart_404"),
        None,
    )
    assert handler is not None, "smart_404 vanished — re-point this test"
    called = {
        n.func.id
        for n in ast.walk(handler)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "maybe_prefix_redirect" in called, (
        "the prefix redirects were removed from before_request but not added to "
        "the 404 handler — /research/grid-intelligence now dead-ends"
    )


def test_research_market_falls_back_to_the_redirect_table():
    """/research/grid-intelligence matches the DYNAMIC rule, so the 404 handler
    never sees it — an explicit 404 Response returned from a view bypasses
    errorhandler(404) entirely. The view has to consult the table itself.

    Guarded here because the behavioural harness below stands in for this view
    rather than executing it: without this assertion, deleting the fallback
    from the shipped source failed nothing (verified by mutation 2026-08-08).
    """
    src = (ROOT / "routes" / "open_data.py").read_text()
    fn = next(
        (n for n in ast.walk(ast.parse(src))
         if isinstance(n, ast.FunctionDef) and n.name == "research_market"),
        None,
    )
    assert fn is not None, "research_market vanished — re-point this test"
    called = {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "maybe_prefix_redirect" in called, (
        "research_market returns a bare 404 for non-market slugs again — "
        "/research/grid-intelligence dead-ends instead of reaching the index"
    )


# ------------------------------------------------------------------- behaviour


def _hook_source():
    """Extract the shipped before_request hook body as runnable source."""
    src = _main_src()
    tree = ast.parse(src)
    hook = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_check_prefix_redirects")
    lines = src.splitlines()[hook.lineno - 1:hook.end_lineno]
    return "\n".join(line[4:] if line.startswith("    ") else line for line in lines)


def _build_app():
    """A Flask app wired exactly like production: the real hook, the real
    redirect table, the real /research/<slug> rule, and a 404 handler that
    consults the table the way smart_404 now does."""
    from routes.redirects_404_killer import (
        redirects_404_killer_bp, maybe_prefix_redirect)

    app = flask.Flask(__name__)
    app.register_blueprint(redirects_404_killer_bp)

    # maybe_prefix_redirect is in scope here on purpose. The hook closes over it
    # in main.py, so a regression that re-adds the call must reproduce the real
    # production behaviour (a 302 off the market page) rather than dying on a
    # NameError — a test that fails for the wrong reason is not evidence.
    ns = {
        "flask": flask, "re": re,
        "redirect": flask.redirect, "request": flask.request,
        "maybe_prefix_redirect": maybe_prefix_redirect,
    }
    exec(_hook_source(), ns)                      # noqa: S102 — shipped source
    app.before_request(ns["_check_prefix_redirects"])

    # Stand-in for open_data_bp's /research/<slug>: same rule shape, same
    # not-found fallback the real view now uses.
    KNOWN_MARKETS = {"phoenix-az", "northern-virginia", "dallas-tx", "atlanta-ga"}

    @app.route("/research/<slug>", methods=["GET"])
    def research_market(slug):
        if slug not in KNOWN_MARKETS:
            r = maybe_prefix_redirect(flask.request.path or "")
            if r is not None:
                return r
            return flask.Response("<h1>Market not found</h1>", status=404)
        return flask.Response(f"<h1>{slug}</h1>", status=200)

    @app.errorhandler(404)
    def smart_404(e):
        if flask.request.method == "GET":
            r = maybe_prefix_redirect(flask.request.path or "")
            if r is not None:
                return r
        return flask.Response("not found", status=404)

    return app


@pytest.mark.parametrize("slug", ["phoenix-az", "northern-virginia", "dallas-tx", "atlanta-ga"])
def test_dcpi_market_pages_render(slug):
    """The four slugs measured 302ing in production must now render."""
    with _build_app().test_client() as c:
        resp = c.get(f"/research/{slug}")
    assert resp.status_code == 200, (
        f"/research/{slug} returned {resp.status_code} "
        f"(-> {resp.headers.get('Location')}) — the market page is dead again"
    )


def test_bare_research_grid_intelligence_still_reaches_the_canonical_index():
    """The retired static Briefs index must not become a 404.

    dchub-frontend #1139 deleted research/grid-intelligence/index.html because
    the backend-rendered /grid-intelligence IS that index, one generation on.
    This path matches the dynamic /research/<slug> rule, so the redirect has to
    come from the view's own fallback — the 404 handler never sees it.
    """
    with _build_app().test_client() as c:
        resp = c.get("/research/grid-intelligence")
    assert resp.status_code == 302, f"expected a redirect, got {resp.status_code}"
    assert resp.headers["Location"].endswith("/grid-intelligence")


def test_unmatched_research_subpath_still_redirects():
    """Multi-segment /research/* paths have no handler, so the 404 handler
    catches them — this is the path dchub-frontend serves statically, and it
    must still land somewhere useful if it ever reaches Flask."""
    with _build_app().test_client() as c:
        resp = c.get("/research/grid-intelligence/pjm/")
    assert resp.status_code == 302, f"expected a redirect, got {resp.status_code}"
    assert resp.headers["Location"].endswith("/grid-intelligence")


def test_non_research_404_is_still_a_404():
    """The move must not turn unrelated misses into redirects."""
    with _build_app().test_client() as c:
        resp = c.get("/no-such-page-anywhere")
    assert resp.status_code == 404, f"got {resp.status_code} -> {resp.headers.get('Location')}"
