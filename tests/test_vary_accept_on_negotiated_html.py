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

★ NO FLASK APP AND NO ROUTES HERE, and that is not stylistic. An earlier draft
registered /html, /json, /plain and /novary on a throwaway app; CI's route-table
coherence checker scans tests/ for Flask routes and reported four "NEW uncovered
routes" that ought to be in dchub-frontend/_routes.json — a file capped at 98
rules and sitting at 97. A fixture app is indistinguishable from production
registration to that scanner. declare_accept_variance() is a pure function over
a Response, so it needs neither.
"""
import pytest

flask = pytest.importorskip("flask")

from api_response_enrichment import declare_accept_variance  # noqa: E402


def _resp(mimetype, vary=None):
    """A response shaped like the one the after_request hook receives."""
    r = flask.Response("body", mimetype=mimetype)
    if vary is not None:
        r.headers["Vary"] = vary
    return r


def _vary_of(mimetype, vary=None, passes=1):
    r = _resp(mimetype, vary)
    for _ in range(passes):
        declare_accept_variance(r)
    return r.headers.get("Vary", "")


def test_html_declares_that_it_varies_by_accept():
    """The defect: the HTML claimed to be the only representation."""
    vary = _vary_of("text/html", "Accept-Encoding")
    tokens = [p.strip().lower() for p in vary.split(",")]
    assert "accept" in tokens, f"HTML does not name Accept in Vary: {vary!r}"


def test_accept_encoding_is_not_clobbered():
    """Compression negotiation must survive — .add() appends, never replaces."""
    vary = _vary_of("text/html", "Accept-Encoding")
    tokens = [p.strip().lower() for p in vary.split(",")]
    assert "accept-encoding" in tokens, f"lost Accept-Encoding: {vary!r}"


def test_it_is_idempotent():
    """The hook can run more than once over one response."""
    vary = _vary_of("text/html", "Accept-Encoding", passes=3)
    tokens = [p.strip().lower() for p in vary.split(",") if p.strip()]
    assert len(tokens) == len(set(tokens)), f"duplicated token: {vary!r}"


def test_html_with_no_prior_vary_still_gets_one():
    vary = _vary_of("text/html")
    assert "accept" in vary.lower(), f"expected a Vary header: {vary!r}"


@pytest.mark.parametrize("mimetype", ["application/json", "text/plain",
                                      "application/xml"])
def test_non_html_is_left_alone(mimetype):
    """Those have one representation; saying otherwise fragments cache keys."""
    vary = _vary_of(mimetype, "Accept-Encoding")
    tokens = [p.strip().lower() for p in vary.split(",")]
    assert "accept" not in tokens, (
        f"{mimetype} should not declare Vary: Accept ({vary!r})")


def test_a_response_that_cannot_carry_headers_does_not_raise():
    """Fail-open by contract — this runs on EVERY response."""
    class Hostile:
        headers = {"Content-Type": "text/html"}

        @property
        def vary(self):
            raise RuntimeError("no vary here")

    declare_accept_variance(Hostile())  # must not raise


def test_main_py_actually_calls_the_shipped_helper():
    """Behaviour above proves the helper is right; this proves it RUNS.

    A helper nothing calls is dead code that tests green.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8")
    assert "declare_accept_variance(response)" in src, (
        "main.py no longer calls declare_accept_variance — the header this "
        "module adds would reach no response")
