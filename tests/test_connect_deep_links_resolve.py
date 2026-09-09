"""/connect/<tool> deep links must land on content that EXISTS.

MEASURED 2026-09-09, at the edge and on the Railway origin: /connect/{claude,
perplexity, copilot, grok, windsurf} returned 404 while nine siblings returned
200. They are now aliases onto instructions already published elsewhere.

★ THE FAILURE THIS GUARDS IS NOT THE 404 — it is the redirect into a dead end,
  which is strictly worse because every status-code check passes. _worker.js
  records the precedent in its own comments: GET /connect/mcp.html 301'd to
  /connect/mcp, which was itself a 404. So this file does not merely assert
  "302 with a Location header"; it asserts the TARGET exists.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONNECT_HTML = os.path.join(ROOT, "static", "connect.html")


@pytest.fixture(scope="module")
def aliases():
    from routes.mcp_connect import _CONNECT_ALIASES
    return _CONNECT_ALIASES


@pytest.fixture(scope="module")
def client():
    flask = pytest.importorskip("flask")
    from routes.mcp_connect import mcp_connect_bp
    app = flask.Flask(__name__)
    app.register_blueprint(mcp_connect_bp)
    return app.test_client()


def test_all_five_dead_paths_are_covered(aliases):
    """The floor: these are the paths measured as 404. Losing one silently
    re-opens a hole that reads green because the others still pass."""
    assert set(aliases) == {"claude", "perplexity", "copilot", "grok", "windsurf"}


def test_each_alias_redirects(client, aliases):
    for slug, target in aliases.items():
        r = client.get("/connect/" + slug)
        assert r.status_code == 302, f"/connect/{slug} -> {r.status_code}, expected 302"
        assert r.headers["Location"] == target, \
            f"/connect/{slug} -> {r.headers['Location']}, expected {target}"


def test_no_alias_is_permanent(client, aliases):
    """301 would outlive the decision in caches we cannot reach."""
    for slug in aliases:
        assert client.get("/connect/" + slug).status_code != 301


# ── the target must EXIST ───────────────────────────────────────────────────
def _connect_html():
    with open(CONNECT_HTML, encoding="utf-8") as fh:
        return fh.read()


def test_every_anchor_target_is_a_real_section(aliases):
    html = _connect_html()
    checked = 0
    for slug, target in aliases.items():
        if "#" not in target:
            continue
        path, _, anchor = target.partition("#")
        assert path == "/connect", f"{slug} points at {path}, not /connect"
        checked += 1
        assert f'id="{anchor}"' in html, (
            f"/connect/{slug} redirects to #{anchor}, which is not a section of "
            f"static/connect.html — a redirect into a dead end")
    assert checked >= 4, f"only {checked} anchor targets checked — the scan went blind"


def test_control_the_anchor_check_can_fail():
    """Without this, `id="x" in html` proves nothing when html is huge."""
    assert 'id="definitely-not-a-real-section"' not in _connect_html()


def test_non_anchor_target_is_a_path_we_publish(aliases):
    """Windsurf points at /install/windsurf. Assert the ROUTE family is real
    rather than the string being well-formed."""
    target = aliases["windsurf"]
    assert target == "/install/windsurf"
    html = _connect_html()
    assert 'href="/install/windsurf"' in html, \
        "/connect/windsurf points at /install/windsurf, which /connect does not link"


def test_the_existing_cards_still_answer(client):
    """Control against collateral damage: the alias loop registers rules on the
    same blueprint, and a clash would silently shadow a real card."""
    from routes.mcp_connect import _CLIENTS
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    for slug in _CLIENTS:
        assert "/connect/" + slug in rules, f"/connect/{slug} lost its rule"
    assert len(_CLIENTS) >= 9, f"only {len(_CLIENTS)} cards — the registry shrank"
