"""A @route decorator must sit directly on the view it names.

★ THE BUG (2026-09-12, PR #4482). A helper was inserted using
`def brain_public_page():` as its anchor, which placed it BETWEEN the two
route decorators and the view:

    @brain_v2_public_bp.route("/brain-live", methods=["GET"])
    @brain_v2_public_bp.route("/brain/public", methods=["GET"])
    def grade_score_text(weighted_score, esc=None) -> str:   # <- captured
        ...
    def brain_public_page():                                  # <- unrouted

Flask then called grade_score_text() with no arguments on every request.
/brain-live and /brain/public returned 500 in production and
brain_public_page became unreachable code. Unit tests passed — they called
grade_score_text directly and never asked what the ROUTE resolved to. CI
passed too: nothing smoke-probes these pages.

Checked with ast against executable text, so a route named in a comment
cannot satisfy it.
"""
import ast
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _route_bindings(rel_path):
    """[(rule, url_params, func_name, required_positional_args)] for every
    @<bp>.route(...)-decorated function in the file."""
    tree = ast.parse(open(os.path.join(REPO_ROOT, rel_path)).read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            if call is None or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in ("route", "get", "post", "put", "delete"):
                continue
            if not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            rule = str(call.args[0].value)
            params = set()
            for seg in rule.split("/"):
                if seg.startswith("<") and seg.endswith(">"):
                    params.add(seg[1:-1].split(":")[-1])
            required = [a.arg for a in node.args.args
                        if a.arg not in ("self", "cls")]
            n_defaults = len(node.args.defaults)
            if n_defaults:
                required = required[:-n_defaults]
            out.append((rule, params, node.name, required))
    return out


def test_the_brain_board_routes_bind_to_the_page_view():
    """The exact regression: these two must resolve to brain_public_page."""
    bound = {rule: fn for rule, _p, fn, _r
             in _route_bindings("routes/brain_v2_public.py")}
    assert bound.get("/brain-live") == "brain_public_page", bound.get("/brain-live")
    assert bound.get("/brain/public") == "brain_public_page", bound.get("/brain/public")


@pytest.mark.parametrize("rel", [
    "routes/brain_v2_public.py",
    "routes/brain_innovation_dashboard.py",
    "routes/brain_learning.py",
    "routes/claim_ledger.py",
])
def test_no_view_needs_an_argument_its_url_cannot_supply(rel):
    """The general class. Flask calls a view with ONLY its URL params, so a
    required positional arg that is not in the rule is a guaranteed 500 on
    the first request — which is what a helper captured by a stray decorator
    looks like."""
    bad = []
    for rule, params, fn, required in _route_bindings(rel):
        missing = [a for a in required if a not in params]
        if missing:
            bad.append(f"{rule} -> {fn}() needs {missing}, url supplies {sorted(params) or 'nothing'}")
    assert not bad, "route(s) Flask cannot call:\n  " + "\n  ".join(bad)


def test_the_real_url_map_resolves_the_board_to_its_view():
    """Independent of the ast check above: register the blueprint on a real
    Flask app and ask the url_map what /brain-live actually resolves to.
    This is the question the shipped bug answered wrongly."""
    flask = pytest.importorskip("flask")
    import sys
    sys.path.insert(0, REPO_ROOT)
    from routes.brain_v2_public import brain_v2_public_bp
    app = flask.Flask(__name__)
    app.register_blueprint(brain_v2_public_bp)
    m = {str(r.rule): r.endpoint for r in app.url_map.iter_rules()}
    assert m.get("/brain-live", "").endswith("brain_public_page"), m.get("/brain-live")
    assert m.get("/brain/public", "").endswith("brain_public_page"), m.get("/brain/public")
