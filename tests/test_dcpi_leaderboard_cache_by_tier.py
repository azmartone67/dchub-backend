"""/dcpi/leaderboard: the PAID render is the one that must never be shared-cached.

THE DEFECT THIS PINS
────────────────────
dcpi_leaderboard_page() masks the numeric scores (composite, excess-power,
constraint, quality, time-to-power) for non-paid callers and serves them in full
to paid ones. Its comment said the body is tier-varying and must never be
shared-cached "or a CDN could serve a paid table to anon", but the line under it
was inverted:

    "private, no-store" if not _lbp_paid else "public, max-age=600, must-revalidate"

The masked render was private and the UNMASKED paid render was public. And a
public directive on a /dcpi/* 200 is not left alone: add_security_headers()
restamps it from _HTML_CACHE_PATHS['/dcpi'], so the paid table actually shipped
as `public, max-age=60, s-maxage=300, stale-while-revalidate=86400`.

THE DIRECTIVES NOW
──────────────────
paid     -> `private, no-store, max-age=0` + `CDN-Cache-Control: no-store`, the
            same pair routes/pockets.py and the /dcpi index use for their
            per-caller renders.
non-paid -> public, via the shared /dcpi HTML policy. Unlike the /dcpi index,
            which serves anon 25 cards and free 50, this page has ONE non-paid
            body: _dcpi_is_paid() is its only tier input. That premise is what
            makes a shared copy safe, so it is asserted below, not assumed.

WHY IT RENDERS
──────────────
A source grep for "no-store" would be satisfied by the comment above the broken
line. So the real view is rendered through dcpi_bp with a stub cursor, and the
two main.py after_request hooks that own Cache-Control are lifted out by AST and
registered on the test app, in main.py's order, against main.py's own cache
tables. The headers asserted are the ones a caller receives. House rule: tests
never import main.py. A NameError from a hook means it grew a new free name;
add it to _hook_ns, don't catch it.
"""
from __future__ import annotations

import ast
import datetime
import logging
import os

import pytest
from flask import Flask, request

from routes import dcpi
from routes.dcpi import dcpi_bp

_MAIN = os.path.join(os.path.dirname(__file__), "..", "main.py")

NON_PAID = ("anonymous", "free", "identified")
PAID = ("starter", "developer", "pro", "enterprise")

# A score distinctive enough that finding it in a body means the row was
# rendered unmasked, and not finding it means it was masked.
_EXCESS = 71.37
_ROWS = (
    {"market_slug": "test-market", "market_name": "Test Market", "iso": "PJM",
     "state": "VA", "excess_power_score": _EXCESS, "constraint_score": 48.2,
     "quality_score": 0.9, "time_to_power_months": 30, "verdict": "BUILD",
     "computed_at": datetime.datetime(2026, 9, 23, 12, 0)},
)


def _main_tree():
    with open(_MAIN, encoding="utf-8") as fh:
        src = fh.read()
    return src, ast.parse(src)


def _hook_ns():
    """main.py's own cache tables and the two hooks that read them."""
    src, tree = _main_tree()
    ns = {"request": request, "ADMIN_ANALYTICS_AVAILABLE": False,
          "user_analytics": None, "logger": logging.getLogger(__name__)}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name in ("_CACHE_PATHS", "_HTML_CACHE_PATHS", "_AGENT_DOOR_PATHS"):
            ns[name] = ast.literal_eval(value)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name in ("_match_html_cache", "add_cache_headers",
                          "add_security_headers")]
    assert len(fns) == 3, "a hook or _match_html_cache was renamed in main.py"
    for fn in fns:
        fn.decorator_list = []
    exec(compile(ast.Module(fns, []), _MAIN, "exec"), ns)
    return ns


@pytest.fixture(scope="module")
def client():
    ns = _hook_ns()
    app = Flask(__name__)
    app.register_blueprint(dcpi_bp)
    # Registered in main.py's order. Flask runs after_request hooks in reverse,
    # so add_security_headers runs first, exactly as in production.
    app.after_request(ns["add_cache_headers"])
    app.after_request(ns["add_security_headers"])
    return app.test_client()


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        # Fresh dicts per call: the view nulls the masked fields in place.
        return [dict(r) for r in _ROWS]


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **kw):
        return _Cur()


@pytest.fixture
def render(client, monkeypatch):
    monkeypatch.setattr(dcpi, "_ensure_tables", lambda: None)
    monkeypatch.setattr(dcpi, "_conn", lambda: _Conn())

    def _go(plan):
        monkeypatch.setattr(dcpi, "_dcpi_caller_plan", lambda: plan)
        resp = client.get("/dcpi/leaderboard")
        assert resp.status_code == 200
        return resp
    return _go


def test_the_tier_lists_mean_what_the_view_means():
    # Anti-vacuity: every case below leans on these being classified this way.
    for plan in NON_PAID:
        assert not dcpi._dcpi_is_paid(plan), plan
    for plan in PAID:
        assert dcpi._dcpi_is_paid(plan), plan


@pytest.mark.parametrize("plan", PAID)
def test_the_paid_render_is_never_shared_cacheable(render, plan):
    resp = render(plan)
    body = resp.get_data(as_text=True)
    # It really is the unmasked table, or this proves nothing.
    assert str(_EXCESS) in body and "🔒" not in body
    assert resp.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert resp.headers.get("CDN-Cache-Control") == "no-store"
    for h in ("Cache-Control", "CDN-Cache-Control", "Surrogate-Control"):
        assert "public" not in (resp.headers.get(h) or ""), h
        assert "s-maxage" not in (resp.headers.get(h) or ""), h


@pytest.mark.parametrize("plan", NON_PAID)
def test_the_non_paid_render_is_shared_cacheable(render, plan):
    resp = render(plan)
    body = resp.get_data(as_text=True)
    assert str(_EXCESS) not in body and "🔒" in body
    cc = resp.headers["Cache-Control"]
    assert "public" in cc
    assert "private" not in cc and "no-store" not in cc
    assert "no-store" not in (resp.headers.get("CDN-Cache-Control") or "")


def test_every_non_paid_caller_gets_the_same_body(render):
    # The premise behind the public directive. If a later change makes free
    # differ from anon (as the /dcpi index does), a shared copy would serve one
    # caller's render to the other, and the non-paid render must go private.
    bodies = {plan: render(plan).get_data() for plan in NON_PAID}
    assert len(set(bodies.values())) == 1, sorted(bodies)
