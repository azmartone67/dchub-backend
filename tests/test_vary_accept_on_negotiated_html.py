"""2026-09-05 — the HTML representation did not declare that it varies by Accept.

DC Hub's pages are CONTENT-NEGOTIATED. Cloudflare's "Markdown for Agents"
serves a converted `text/markdown` body when the request carries
`Accept: text/markdown`, and the HTML otherwise. Two representations, one URL.
RFC 9110 §12.5.5 obliges such a response to name the request header that
selected it. Measured live through the edge, every page class agreeing:

    Accept: text/html      -> content-type: text/html      vary: Accept-Encoding
    Accept: text/markdown  -> content-type: text/markdown  vary: Accept-Encoding, accept

The markdown variant carries `accept` (Cloudflare adds it). The HTML variant did
not, so it claimed to be THE representation of the URL when it is one of two.

★ WHAT THIS IS NOT, measured BEFORE the fix was written — both are corrections
to my own earlier guesses, and both are the reason this docstring exists:

1. NOT a Cloudflare-cache bug. Six alternating html/markdown request pairs
   against one un-cache-busted URL returned the correct content-type every
   single time, all `cf-cache-status: HIT`. CF keys on Accept internally no
   matter what we advertise. I had claimed this asymmetry was "plausibly" the
   cause of cross-serving; it is not.

2. NOT the dashboard's "97% of markdown requests fulfilled". Sweeping every page
   class, EVERY html page converts (/, /dcpi, /dcpi/<slug>, /markets,
   /markets/<slug>, /transactions, /connect, /ai, /agent, /grid/<iso>,
   /facilities/<slug>, /mcp). The unfulfilled remainder is markdown requests
   against resources that are not html — sitemap.xml (application/xml),
   openapi.json, .well-known/mcp.json (application/json), llms.txt and
   robots.txt (text/plain) — plus 301/308 redirects. None of those can be
   converted, and none is a defect.

★ WHO IT IS FOR: every cache that is NOT Cloudflare and does obey Vary — a
corporate proxy, another CDN in front of us, and the HTTP caches inside agent
frameworks. Those key on URL plus the headers Vary names, so without `Accept`
they may store the HTML and hand it to a later `Accept: text/markdown` request.
That is the failure this header prevents.

★ HTML ONLY, on purpose. JSON, XML and plain-text responses do not vary by
Accept; declaring that they do would fragment their cache keys for nothing.
"""
import re

import pytest

flask = pytest.importorskip("flask")

from api_response_enrichment import declare_accept_variance  # noqa: E402


def _app():
    """Register the real hook against a bare app.

    ★ Registers the SHIPPED helper, not a copy of it. An earlier draft of this
    file re-implemented the two-line body here; mutation testing showed the
    behavioural tests then stayed GREEN while main.py was broken, because they
    were exercising the copy. api_response_enrichment imports cleanly on its own
    (main.py does not — the house rule bans importing it), so the real function
    can be called directly.
    """
    app = flask.Flask(__name__)
    app.after_request(declare_accept_variance)

    @app.route("/html")
    def html():
        r = flask.Response("<html></html>", mimetype="text/html")
        r.headers["Vary"] = "Accept-Encoding"
        return r

    @app.route("/json")
    def json_():
        r = flask.Response("{}", mimetype="application/json")
        r.headers["Vary"] = "Accept-Encoding"
        return r

    @app.route("/plain")
    def plain():
        return flask.Response("hi", mimetype="text/plain")

    @app.route("/novary")
    def novary():
        return flask.Response("<html></html>", mimetype="text/html")

    return app.test_client()


@pytest.fixture(scope="module")
def client():
    return _app()


def test_html_declares_that_it_varies_by_accept(client):
    """The defect: the HTML claimed to be the only representation."""
    vary = client.get("/html").headers.get("Vary", "")
    assert "accept" in vary.lower().replace("accept-encoding", ""), (
        f"HTML response does not name Accept in Vary: {vary!r}")


def test_accept_encoding_is_not_clobbered(client):
    """Compression negotiation must survive — .add() appends, never replaces."""
    vary = client.get("/html").headers.get("Vary", "")
    assert "Accept-Encoding" in vary, f"lost Accept-Encoding: {vary!r}"
    assert "Accept" in vary


def test_it_is_idempotent(client):
    """Two requests, and a second pass over one response, must not duplicate."""
    for _ in range(3):
        vary = client.get("/html").headers.get("Vary", "")
        parts = [p.strip().lower() for p in vary.split(",") if p.strip()]
        assert len(parts) == len(set(parts)), f"duplicated token: {vary!r}"


def test_html_with_no_prior_vary_still_gets_one(client):
    vary = client.get("/novary").headers.get("Vary", "")
    assert "Accept" in vary, f"expected a Vary header to be created: {vary!r}"


@pytest.mark.parametrize("path", ["/json", "/plain"])
def test_non_html_is_left_alone(client, path):
    """Those representations do not vary by Accept; saying so would fragment
    their cache keys for nothing."""
    vary = client.get(path).headers.get("Vary", "")
    tokens = [p.strip().lower() for p in vary.split(",") if p.strip()]
    assert "accept" not in tokens, (
        f"{path} should not declare Vary: Accept — it has one representation "
        f"({vary!r})")


def test_main_py_actually_calls_the_shipped_helper():
    """Behaviour above proves the helper is right; this proves it RUNS.

    A helper nothing calls is dead code that tests green — the failure this
    pairing exists to prevent.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8")
    assert "declare_accept_variance(response)" in src, (
        "main.py no longer calls declare_accept_variance — the header this "
        "module adds would reach no response")
