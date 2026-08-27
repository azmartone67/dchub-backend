"""/redeem/<session_id> answered 200 for any string and echoed the token.

r-redeem-noindex (2026-08-27).

Measured against production: /redeem/xyz, /redeem/aaaa-1 and /redeem/zzzz-9
each returned HTTP 200 with NO canonical and NO robots meta, ~10 KB apiece,
differing in exactly four lines — and all four were the echoed token:

    <p class="note">… · Session <code>AAAA-1</code></p>
    var token = 'AAAA-1';

robots.txt carries no rule for /redeem either. So this was an unbounded family
of near-identical indexable URLs with no self-selected representative — the
"Duplicate without user-selected canonical" shape — reached by the same
prefix sweep that found /facilities/in (#3234).

★ IT IS ALSO THE WRONG PLACE FOR A SESSION TOKEN. A redemption code rendered
  into an indexable page can end up in a search result. noindex is the right
  answer here on hygiene grounds even before the duplicate-content one.

★★ A HOOK, NOT THREE EDITS. The blueprint returns HTML from three separate
   templates (FORM_HTML, SUCCESS_HTML, ERROR_HTML) across five Response()
   calls, and the next template added would not inherit a per-template meta
   tag. after_request covers every response the blueprint will ever make;
   the metas are the belt to its braces for anything reading the body rather
   than the header. Both are asserted below, separately, so removing either
   one goes red.
"""
import pytest

pytest.importorskip("flask")

# Members of the indexable space: strings that actually ROUTE to the page.
TOKENS = ["AAAA-1", "zzzz-9", "01HXYZ", "a" * 64, "9f2b8c1d-e3a4"]


def _client():
    from flask import Flask

    import routes.redeem_routes as rr

    app = Flask(__name__)
    app.register_blueprint(rr.redeem_bp)
    return app.test_client()


@pytest.mark.parametrize("token", TOKENS)
def test_the_header_is_stamped_for_any_token(token):
    """The unbounded space is the point — every member must be covered."""
    r = _client().get(f"/redeem/{token}")

    assert "noindex" in (r.headers.get("X-Robots-Tag") or "").lower(), (
        f"/redeem/{token} answered {r.status_code} with X-Robots-Tag "
        f"{r.headers.get('X-Robots-Tag')!r} — any string is an indexable page "
        "again, each echoing its own token")


@pytest.mark.parametrize("token", TOKENS[:3])
def test_the_body_carries_it_too(token):
    """The header alone is not enough if a proxy strips it."""
    body = _client().get(f"/redeem/{token}").data.decode("utf-8", "replace")

    assert 'content="noindex, follow"' in body, (
        f"/redeem/{token} rendered no robots meta")


def test_the_bare_page_is_covered():
    r = _client().get("/redeem")

    assert "noindex" in (r.headers.get("X-Robots-Tag") or "").lower()
    assert 'content="noindex, follow"' in r.data.decode("utf-8", "replace")


def test_the_error_template_is_covered_by_the_hook():
    """★ The branch a per-template edit is most likely to miss.

    A bad email drives ERROR_HTML on a 400. If the coverage ever regresses to
    a per-template meta and someone adds a fourth template, THIS is the shape
    that slips through — which is why the header comes from after_request.
    """
    r = _client().post("/redeem/AAAA-1", data={"email": "not-an-email"})

    assert r.status_code >= 400, (
        f"a bad email returned {r.status_code}; this test is not reaching the "
        "error template and proves nothing")
    assert "noindex" in (r.headers.get("X-Robots-Tag") or "").lower(), (
        f"the {r.status_code} error page carries "
        f"{r.headers.get('X-Robots-Tag')!r} — the hook no longer covers every "
        "response this blueprint makes")


def test_the_hook_does_not_clobber_an_explicit_header():
    """setdefault, not assignment — a route that sets its own must win."""
    import inspect

    import routes.redeem_routes as rr

    src = inspect.getsource(rr._never_index_a_redemption_page)
    assert "setdefault" in src, (
        "the hook assigns X-Robots-Tag instead of setdefault — a route that "
        "deliberately sets its own value would be silently overwritten")


def test_a_path_traversal_token_never_reaches_the_page_at_all():
    """★ Not a gap in the hook — a case that is not in the space.

    "../../etc/passwd" 404s in routing before the blueprint runs, so no
    after_request fires and none should: an unmatched URL is not a redemption
    page and a 404 needs no noindex. Written down because the first version of
    this file asserted the header on it and went red for the right reason.
    """
    r = _client().get("/redeem/../../etc/passwd")

    assert r.status_code == 404, (
        f"a traversal-shaped token answered {r.status_code} — it now routes to "
        "the redeem page, so it IS in the indexable space and belongs in TOKENS")

