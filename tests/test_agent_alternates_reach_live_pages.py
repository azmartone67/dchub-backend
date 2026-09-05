"""2026-09-05 — the agent-alternate injector aimed two of five patterns at URLs
that do not serve HTML, and advertised a 404 as a machine-readable alternate.

MEASURED LIVE through the edge, by reading the `X-DC-Alternates-Injected`
header the module itself sets:

    /markets/dallas        200  injected: market      worked
    /hyperscaler-deals     200  injected: deals       worked
    /ai-capacity-index     200  injected: capacity    worked
    /facilities/<slug>     200  injected: NONE        <- ~20,500 pages
    /facility/<slug>       301                        <- what the pattern matched
    /grid/ercot            200  injected: NONE
    /grids/ercot           404                        <- what the pattern matched

A pattern aimed at a redirect or a 404 fails silently forever: neither carries
a `<head>`, so the hook returns early and sets no header — indistinguishable
from "not an SEO page". The two largest page families on the site had never
received an alternate link.

Separately, `/ai-capacity-index` DID inject and pointed every AI crawler at
`/ai-capacity-index/today.json`, which 404s. The live document is
`/api/v1/ai-capacity-index` (200, 14,172 b). All replacement URLs were fetched
before being written here:

    /api/v1/facility/<slug>              200   1,808 b
    /api/v1/markets/dallas               200   1,780 b
    /api/v1/grid-intelligence?iso=ercot  200   2,144 b
    /api/v1/deals?limit=50               200   2,111 b
    /api/v1/ai-capacity-index            200  14,172 b

These tests exercise the REGISTERED HOOK against a real Flask app, not the
regexes in isolation — the defect was that the hook never fired, and a test
that only matched patterns would have passed throughout.
"""
import re

import pytest

flask = pytest.importorskip("flask")

from routes.seo_agent_alternates import (  # noqa: E402
    _MCP_TOOLS,
    _SEO_PATTERNS,
    catalog_tool_names,
    register_alternate_hook,
)

# The paths that actually serve HTML, measured 2026-09-05. Left column is the
# live 200; right column is what the pattern used to match.
LIVE_HTML_PATHS = {
    "/facilities/equinix-nj-campus-26f01f95": "facility",
    "/markets/dallas": "market",
    "/grid/ercot": "grid",
    "/hyperscaler-deals": "deals",
    "/ai-capacity-index": "capacity",
}

# Measured 301 / 404 on the same sweep. Matching these is what the bug was.
RETIRED_PATHS = ("/facility/equinix-nj-campus-26f01f95", "/grids/ercot")

PAGE = ("<html><head><title>t</title></head><body><h1>x</h1></body></html>")


@pytest.fixture
def client():
    app = flask.Flask(__name__)

    @app.route("/<path:anything>")
    def any_page(anything):
        return flask.Response(PAGE, mimetype="text/html")

    @app.route("/hyperscaler-deals")
    @app.route("/ai-capacity-index")
    def flat():
        return flask.Response(PAGE, mimetype="text/html")

    register_alternate_hook(app)
    return app.test_client()


@pytest.mark.parametrize("path,kind", sorted(LIVE_HTML_PATHS.items()))
def test_every_live_html_page_gets_injected(client, path, kind):
    """The defect: the hook never fired on the two biggest page families."""
    r = client.get(path)
    assert r.headers.get("X-DC-Alternates-Injected") == kind, (
        f"{path} serves HTML in production but the hook did not fire — the "
        f"pattern is aimed at a URL that does not serve HTML")


@pytest.mark.parametrize("path", RETIRED_PATHS)
def test_the_retired_paths_are_not_what_we_target(client, path):
    """/facility/<slug> is a 301 and /grids/<iso> a 404. Targeting either is
    the bug returning."""
    assert client.get(path).headers.get("X-DC-Alternates-Injected") is None, (
        f"{path} does not serve HTML in production; a pattern matching it "
        f"injects into nothing and reports success by silence")


def test_no_alternate_points_at_the_retired_capacity_json():
    """We were advertising a 404 to every AI crawler."""
    for _pat, kind, tmpl in _SEO_PATTERNS:
        assert "today.json" not in tmpl, (
            f"{kind} advertises /ai-capacity-index/today.json, which 404s; "
            f"the live document is /api/v1/ai-capacity-index")


def test_every_advertised_tool_name_exists_in_the_catalog():
    """A misspelled tool name teaches an agent a call that errors.

    Checked against routes/tools_manifest.py `_TOOL_REST`, which this module
    does not own — so the assertion reads an independent source rather than
    restating its own list.
    """
    catalog = catalog_tool_names()
    unknown = {}
    for kind, (primary, _arg, rest) in _MCP_TOOLS.items():
        bad = [t for t in (primary,) + tuple(rest) if t not in catalog]
        if bad:
            unknown[kind] = bad
    assert not unknown, f"tool names absent from _TOOL_REST: {unknown}"


def test_each_kind_advertises_its_own_tools_not_the_market_set():
    """Was one hardcoded list for all five kinds: the deals page and the
    capacity page both advertised the facility/market tools."""
    primaries = {kind: _MCP_TOOLS[kind][0] for kind in _MCP_TOOLS}
    assert len(set(primaries.values())) == len(primaries), (
        f"two page kinds share a primary tool: {primaries}")
    assert primaries["deals"] == "hyperscaler_deals"
    assert primaries["capacity"] == "ai_capacity_index"


@pytest.mark.parametrize("path,kind", sorted(LIVE_HTML_PATHS.items()))
def test_the_body_carries_the_call_so_markdown_can_see_it(client, path, kind):
    """<head> is discarded by HTML->Markdown; the agent channel needs a body line."""
    body = client.get(path).get_data(as_text=True)
    after_head = body.split("</head>", 1)[-1]
    assert _MCP_TOOLS[kind][0] in after_head, (
        f"{path}: the primary tool is named only in <head>, which the markdown "
        f"representation served to agents discards entirely")


@pytest.mark.parametrize("path,kind", sorted(LIVE_HTML_PATHS.items()))
def test_no_tool_name_is_wrapped_in_em(client, path, kind):
    """#3948, same day: an <em> run eats the underscores inside its <code>,
    so get_market_dcpi_rank shipped to agents as getmarketdcpirank."""
    body = client.get(path).get_data(as_text=True)
    offenders = re.findall(
        r"<em>(?:(?!</em>).)*?<code>[^<]*_[^<]*</code>(?:(?!</em>).)*?</em>",
        body, re.S)
    assert not offenders, (
        "an <em> wraps a <code> identifier; in markdown the wrapper becomes "
        "_…_ and every underscore inside it is eaten, so the agent is handed a "
        "tool name that does not exist. Carry italics in CSS. " + str(offenders))


def test_a_page_that_already_has_a_handoff_is_left_alone(client):
    """/facilities/<slug> names get_facility in its own footer, and that one
    survives markdown intact — a second line would be noise."""
    app = flask.Flask(__name__)

    @app.route("/markets/<slug>")
    def already(slug):
        return flask.Response(
            "<html><head></head><body>see https://dchub.cloud/mcp</body></html>",
            mimetype="text/html")

    register_alternate_hook(app)
    r = app.test_client().get("/markets/dallas")
    assert r.headers.get("X-DC-Alternates-Injected") == "market", (
        "the head tags should still be injected")
    assert r.headers.get("X-DC-Agent-Handoff") is None, (
        "a page that already carries an MCP handoff got a duplicate one")


def test_the_argument_names_are_the_schema_names(client):
    """Verified against a live tools/list 2026-09-05. get_market_intel takes
    `market`, NOT `market_slug` — undeclared arguments are silently stripped,
    so the wrong name returns a contentless answer rather than an error."""
    body = client.get("/markets/dallas").get_data(as_text=True)
    assert 'get_market_intel market="dallas"' in body, (
        "market page must advertise the schema's `market` argument")
    assert "market_slug=" not in body, (
        "market_slug is the name of the VALUE, not the parameter; passing it "
        "is silently stripped and returns no market data")
    fac = client.get("/facilities/equinix-nj-campus-26f01f95").get_data(as_text=True)
    assert 'get_facility slug="equinix-nj-campus-26f01f95"' in fac
