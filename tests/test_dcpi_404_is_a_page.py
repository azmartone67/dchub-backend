"""An unknown /dcpi/<slug> answers a real 404 page that says noindex.

Measured live 2026-09-21: `/dcpi/zz-no-such-market-xyz` answered 404 with a
48-byte body, `<h1>Market not found: zz-no-such-market-xyz</h1>`: no <title>,
no robots meta, no X-Robots-Tag, and the slug rendered unescaped
(`/dcpi/a%3Cb%3Ec` came back as `a<b>c`).

The helper is EXECUTED here; the wiring check reads the route's AST, because
driving the route itself needs a live market_power_scores table.
"""
import ast
import os

from routes.dcpi import _dcpi_not_found_response

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page(slug):
    r = _dcpi_not_found_response(slug)
    return r, r.get_data(as_text=True)


def test_unknown_market_is_a_404_page_not_a_headless_line():
    r, body = _page("zz-no-such-market-xyz")
    assert r.status_code == 404
    assert body.startswith("<!doctype html>")
    assert "<title>" in body and "</head>" in body
    assert "zz-no-such-market-xyz" in body


def test_noindex_rides_in_both_the_meta_and_the_header():
    r, body = _page("zz-no-such-market-xyz")
    assert '<meta name="robots" content="noindex, follow">' in body
    assert r.headers.get("X-Robots-Tag") == "noindex"


def test_csp_still_ships_on_the_404():
    r, _ = _page("x")
    assert r.headers.get("Content-Security-Policy"), "phase 284: a 404 ships the CSP too"


def test_the_slug_is_escaped_not_rendered():
    _, body = _page('a<b>c"d\'e&f')
    assert "<b>" not in body
    assert "a&lt;b&gt;c&quot;d&#x27;e&amp;f" in body


def test_the_route_returns_the_helper_on_the_404_path():
    tree = ast.parse(open(os.path.join(ROOT, "routes", "dcpi.py")).read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "public_market_page")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
             and getattr(n.value.func, "id", None) == "_dcpi_not_found_response"]
    assert calls, "public_market_page no longer returns _dcpi_not_found_response(slug)"
    src = ast.get_source_segment(open(os.path.join(ROOT, "routes", "dcpi.py")).read(), fn)
    assert "<h1>Market not found: {slug}</h1>" not in src, "the headless unescaped body is back"
