"""The cache-lift hook never turns a per-caller answer into a public one.

WHY. main.py's add_cache_headers stamps `public, max-age=N, s-maxage=N` on
every 200 GET under _CACHE_PATHS, overriding whatever the view said. Measured
live 2026-09-22 after be#5179: an enterprise key's full answer on
/api/v1/dcpi/scores/<slug> came back from api.dchub.cloud as
`public, max-age=1800, s-maxage=1800` although util/numeric_tease marked it
`private, no-store`. The same override applied to the paid answers of
/api/v1/gas-pipelines (util/rest_tease) and to /api/v1/facility/<slug>/location,
whose module says its answers are never cacheable.

A view that says `private, no-store` answered one caller, and the hook now
leaves that answer alone. Every other answer on these paths is cached exactly
as before. The hook runs for real: pulled out of main.py with ast, as
tests/test_keyed_rest_walls_open.py does for main.py's handlers.
"""
import ast
import builtins
import copy
import functools
import pathlib

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _main_tree():
    return ast.parse((ROOT / "main.py").read_text())


def _node(name):
    for n in _main_tree().body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            node = copy.deepcopy(n)
            node.decorator_list = []
            return node
        if (isinstance(n, (ast.Assign, ast.AnnAssign))
                and name in {getattr(t, "id", None) for t in
                             (n.targets if isinstance(n, ast.Assign) else [n.target])}):
            return copy.deepcopy(n)
    raise AssertionError(f"{name} not found at main.py module level")


@pytest.fixture
def client():
    ns = {"request": flask.request}
    mod = ast.Module(body=[_node("_CACHE_PATHS"), _node("add_cache_headers")],
                     type_ignores=[])
    exec(compile(mod, "main.py", "exec"), ns)  # noqa: S102
    assert "/api/v1/dcpi/scores" in ns["_CACHE_PATHS"]
    app = flask.Flask("cache-lift")

    def view(slug):
        """Answers with whatever Cache-Control the test asks the view to set."""
        resp = flask.jsonify(slug=slug)
        cc = flask.request.args.get("cc")
        if cc:
            resp.headers["Cache-Control"] = cc
        return resp

    # add_url_rule, not @app.route: regression_lint reads a route decorator
    # in a test as a second registration of the production rule.
    app.add_url_rule("/api/v1/dcpi/scores/<slug>", "scores", view)
    app.add_url_rule("/api/v1/facility/<path:slug>", "facility", view)
    app.after_request(ns["add_cache_headers"])
    return app.test_client()


@pytest.mark.parametrize("path", ["/api/v1/dcpi/scores/ashburn",
                                  "/api/v1/facility/equinix-dc1/location"])
def test_a_per_caller_answer_stays_private(client, path):
    """FAILS before this change: the hook rewrote it to public, s-maxage."""
    r = client.get(path + "?cc=private, no-store, max-age=0")
    cc = r.headers["Cache-Control"]
    assert "no-store" in cc and "private" in cc and "public" not in cc, cc


def test_a_shared_answer_is_still_lifted(client):
    """The preview and every other shared answer keep the edge TTL."""
    r = client.get("/api/v1/dcpi/scores/ashburn")
    assert r.headers["Cache-Control"].startswith("public, max-age=1800, s-maxage=1800")
    r = client.get("/api/v1/dcpi/scores/ashburn?cc=public, max-age=120")
    assert r.headers["Cache-Control"].startswith("public, max-age=1800")


def test_no_store_alone_is_still_lifted(client):
    """Unchanged on purpose: views that say no-store without private (live
    status reads, one blueprint's blanket no-store) are shared answers the
    lift was added for."""
    r = client.get("/api/v1/facility/x?cc=no-store, no-cache, must-revalidate")
    assert r.headers["Cache-Control"].startswith("public, max-age=1800")
