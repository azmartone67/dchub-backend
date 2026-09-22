"""A trailing-slash spelling of a real page answers 301 to the page, not 404.

Measured through the edge 2026-09-22, browser UA, redirects not followed:

    404 /pricing/           application/json  x-dc-hub-served-by: railway-primary
    404 /connect/           application/json  (same)
    404 /facilities/in/nl/  text/html         (same)

routes/trailing_slash_redirect.py turns a final 404 on a GET/HEAD slash spelling
into a 301 to the stripped path. That happens only when routing matches the
stripped path to a rule other than the one that answered 404, and the hop's
rate-limit token is refunded.

Two layers, because two jobs run them:
  * a minimal Flask app using the REAL install() and the REAL rate_limiter
    hooks. It covers the decision table and the token arithmetic, and runs in
    unit-tests.
  * main.py's REAL app, booted in a subprocess by app_contract_gate.boot(). It
    covers the three measured URLs and the neighbours that must keep their
    answer (/upgrade/, /research/<x>/, /markets/<x>/). It skips on
    ModuleNotFoundError outside app-contract-gate, which runs this file with
    DCHUB_CONTRACT_GATE_STRICT=1.
"""
import functools
import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import flask
import pytest
from werkzeug.routing import BaseConverter

import rate_limiter
from routes import trailing_slash_redirect as tsr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# TEST-NET-3 (RFC 5737): not loopback, not Railway egress, not a partner egress,
# so rate_limit_before charges these requests like any visitor's.
_IP = "203.0.113.41"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def _app():
    """Rules shaped like the real ones this change is about.

    Registered with add_url_rule, not decorators: scripts/regression_lint.py
    counts a decorator's path literal as a second definition of the real route.
    """
    app = flask.Flask(__name__)
    app.before_request(rate_limiter.rate_limit_before)
    app.after_request(rate_limiter.rate_limit_after)
    tsr.install(app)
    rules = [
        ("/pricing", "GET", lambda: "pricing"),
        # render_facility_profile: a catch-all that answers its own 404.
        ("/facilities/<path:slug>", "GET", lambda slug: ("Facility not found", 404)),
        ("/facilities/in/<country>", "GET", lambda country: "facilities in " + country),
        # be#5223: /upgrade/ has its own rule and answers 302, while /upgrade
        # routes to a different view.
        ("/upgrade", "GET", lambda: flask.redirect("/pricing?utm_source=mcp_upgrade", 302)),
        ("/upgrade/", "GET", lambda: flask.redirect("https://checkout.example/direct", 302)),
        ("/news/<slug>", "GET", lambda slug: ("Article Not Found", 404)),
        ("/submit", "POST", lambda: "ok"),
        ("/api/v1/stats", "GET", lambda: flask.jsonify(ok=True)),
    ]
    for i, (rule, method, view) in enumerate(rules):
        app.add_url_rule(rule, endpoint="fixture_%d" % i, view_func=view,
                         methods=[method])
    return app


def _client(app=None):
    rate_limiter._buckets.clear()
    return (app or _app()).test_client()


def _open(client, path, method="GET", path_info=None):
    kwargs = {"method": method,
              "headers": {"User-Agent": _UA, "CF-Connecting-IP": _IP},
              "environ_base": {"REMOTE_ADDR": _IP}}
    if path_info is not None:
        # What gunicorn hands Flask for the raw request line. The test client
        # would parse "//host/x" as a URL with a host, and decode nothing.
        kwargs["environ_overrides"] = {"PATH_INFO": path_info}
    return client.open(path, **kwargs)


def _tokens():
    buckets = rate_limiter._buckets
    return (buckets[f"ip:{_IP}:min"]["tokens"], buckets[f"ip:{_IP}:hr"]["tokens"])


# ── what gets redirected ────────────────────────────────────────────────────

def test_a_routing_404_spelling_301s_to_the_page():
    """/pricing/ matches no rule; /pricing does."""
    r = _open(_client(), "/pricing/")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/pricing"
    assert r.headers[tsr.MARKER] == "trailing-slash"
    assert r.headers["Cache-Control"] == "public, max-age=86400"


def test_a_view_404_behind_a_catch_all_301s_to_the_specific_page():
    """/facilities/in/nl/ matches /facilities/<path:slug>, whose view answers
    404. /facilities/in/nl matches /facilities/in/<country>."""
    r = _open(_client(), "/facilities/in/nl/")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/facilities/in/nl"


def test_the_query_string_is_kept():
    r = _open(_client(), "/pricing/?utm_source=gsc&q=a%20b")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/pricing?utm_source=gsc&q=a%20b"


def test_head_is_redirected_like_get():
    r = _open(_client(), "/pricing/", method="HEAD")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/pricing"


def test_every_trailing_slash_goes_so_the_target_is_final():
    """A target still ending in "/" could be converted again: a chain."""
    r = _open(_client(), "/pricing///")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/pricing"


# ── what is left alone ──────────────────────────────────────────────────────

def test_a_slash_spelling_that_is_not_a_404_keeps_its_answer():
    """The /upgrade/ direct checkout is a 302 from its own rule, while /upgrade
    routes elsewhere. Deciding from routing alone would 301 it away."""
    r = _open(_client(), "/upgrade/")
    assert r.status_code == 302, r.status_code
    assert r.headers["Location"] == "https://checkout.example/direct"
    assert tsr.MARKER not in r.headers


def test_the_catch_alls_own_404_is_not_redirected_into_itself():
    """/facilities/zz/qq routes to the same catch-all that just said 404."""
    r = _open(_client(), "/facilities/zz/qq/")
    assert r.status_code == 404, r.status_code
    assert "Location" not in r.headers


def test_no_page_under_either_spelling_keeps_its_404():
    """The loop case: a target that routes nowhere is never offered."""
    r = _open(_client(), "/no-such-page/")
    assert r.status_code == 404, r.status_code
    assert "Location" not in r.headers


@pytest.mark.parametrize("path", ["/pricing/", "/submit/"])
def test_a_post_is_never_redirected(path):
    """A 301 turns a POST into a GET and drops its body. /submit takes POST."""
    r = _open(_client(), path, method="POST")
    assert r.status_code == 404, r.status_code
    assert "Location" not in r.headers


def test_a_get_is_not_sent_to_a_page_that_only_takes_post():
    r = _open(_client(), "/submit/")
    assert r.status_code == 404, r.status_code
    assert "Location" not in r.headers


def test_api_paths_keep_their_json_404():
    r = _open(_client(), "/api/v1/stats/")
    assert r.status_code == 404, r.status_code
    assert "Location" not in r.headers


# ── the Location stays on this site and means the same path ─────────────────

def test_a_decoded_path_cannot_become_a_query():
    """/news/a%3Fb/ reaches Flask as PATH_INFO /news/a?b/. Echoed raw, the
    Location would ask for /news/a with the query "b"."""
    r = _open(_client(), "/", path_info="/news/a?b/")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/news/a%3Fb"


class _AnyText(BaseConverter):
    """Any text not ending in "/", a leading "/" included. No real rule is this
    loose today, which is why these guards need a rule that is."""
    regex = r".*[^/]"
    part_isolating = False


def _loose_app():
    app = flask.Flask(__name__)
    app.url_map.converters["anytext"] = _AnyText
    app.url_map.merge_slashes = False
    tsr.install(app)

    @app.get("/<anytext:rest>")
    def everything(rest):
        return "page " + rest

    return app


def test_control_the_loose_rule_is_redirected_to():
    """Guard the guards below: without this, a loose rule that matched nothing
    would pass them for the wrong reason."""
    r = _open(_client(_loose_app()), "/ok/")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/ok"


def test_a_double_slash_request_is_redirected_on_this_site():
    """ "//evil.example" as a Location sends the browser to evil.example.
    Werkzeug builds request.path as "/" + PATH_INFO.lstrip("/"), so the hook
    sees "/evil.example/" and the Location stays on this site."""
    r = _open(_client(_loose_app()), "/", path_info="//evil.example/")
    location = r.headers.get("Location", "/")
    assert location.startswith("/") and not location.startswith("//"), location


def test_the_helper_refuses_a_network_path_reference():
    """The hook cannot reach this (see above); a direct caller can."""
    app = _loose_app()
    adapter = app.url_map.bind("dchub.cloud")
    assert tsr.stripped_target(adapter, "/ok/", "GET") == "/ok"
    assert tsr.stripped_target(adapter, "//evil.example/", "GET") is None


def test_a_backslash_cannot_turn_into_a_slash():
    """Browsers read "/\\evil.example" as "//evil.example"."""
    r = _open(_client(_loose_app()), "/", path_info="/\\evil.example/")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "/%5Cevil.example"


# ── the hop costs no rate-limit token ───────────────────────────────────────

def test_control_a_plain_404_is_charged():
    """Guard the guard: if these visitors were never charged, the tests below
    would pass with the refund deleted."""
    c = _client()
    _open(c, "/pricing")
    before = _tokens()
    assert _open(c, "/no-such-page/").status_code == 404
    assert _tokens() == (before[0] - 1, before[1] - 1)


@pytest.mark.parametrize("path", ["/pricing/", "/facilities/in/nl/"],
                         ids=["routing_404", "view_404"])
def test_the_hop_is_not_charged(path):
    """rate_limit_before charges every request before any 404 exists, so an
    unrefunded hop costs a token for the redirect and another for the page."""
    c = _client()
    _open(c, "/pricing")
    before = _tokens()
    r = _open(c, path)
    assert r.status_code == 301, r.status_code
    assert _tokens() == before


def test_one_visit_through_the_slash_spelling_costs_one_token():
    c = _client()
    _open(c, "/pricing")
    before = _tokens()
    hop = _open(c, "/pricing/")
    assert _open(c, hop.headers["Location"]).status_code == 200
    assert _tokens() == (before[0] - 1, before[1] - 1)


def test_a_client_out_of_tokens_gets_a_429_not_a_free_redirect():
    """The limiter answers before any view, so an exhausted visitor's slash
    request is a 429, and nothing is refunded."""
    c = _client()
    _open(c, "/pricing")
    rate_limiter._buckets[f"ip:{_IP}:min"]["tokens"] = 0
    assert _open(c, "/pricing/").status_code == 429
    assert rate_limiter._buckets[f"ip:{_IP}:min"]["tokens"] == 0


def test_a_refund_gives_back_one_charge_once():
    rate_limiter._buckets.clear()
    with flask.Flask(__name__).test_request_context(
            "/pricing/", headers={"User-Agent": _UA, "CF-Connecting-IP": _IP},
            environ_base={"REMOTE_ADDR": _IP}):
        assert rate_limiter.rate_limit_before() is None
        charged = _tokens()
        assert rate_limiter.refund_request() is True
        assert _tokens() == (charged[0] + 1, charged[1] + 1)
        assert rate_limiter.refund_request() is False
        assert _tokens() == (charged[0] + 1, charged[1] + 1)


def test_a_refund_is_a_no_op_for_an_uncharged_request():
    """Loopback is never charged, so a refund there must not mint a token."""
    rate_limiter._buckets.clear()
    with flask.Flask(__name__).test_request_context(
            "/pricing/", headers={"User-Agent": _UA},
            environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert rate_limiter.rate_limit_before() is None
        assert rate_limiter.refund_request() is False
        assert rate_limiter._buckets == {}


# ── main.py's real app ──────────────────────────────────────────────────────
#
# One visitor (IP) per sequence, so each sequence starts with fresh buckets. A
# warm-up request opens the visitor's buckets first, so every measured request
# reads a before and an after.
_REAL_CASES = [
    ("pricing", "GET", "/pricing", "203.0.113.60"),
    ("pricing_slash", "GET", "/pricing/", "203.0.113.60"),
    ("connect", "GET", "/connect", "203.0.113.61"),
    ("connect_slash", "GET", "/connect/", "203.0.113.61"),
    ("connect_again", "GET", "/connect", "203.0.113.61"),
    ("facilities_in_nl", "GET", "/facilities/in/nl", "203.0.113.62"),
    ("facilities_in_nl_slash", "GET", "/facilities/in/nl/", "203.0.113.62"),
    ("control", "GET", "/pricing", "203.0.113.63"),
    ("no_page_slash", "GET", "/no-such-page-zz9x/", "203.0.113.63"),
    ("pricing_slash_qs", "GET", "/pricing/?utm_source=gsc&a=1", "203.0.113.64"),
    ("pricing_slash_head", "HEAD", "/pricing/", "203.0.113.65"),
    ("connect_slash_post", "POST", "/connect/", "203.0.113.66"),
    ("upgrade_slash", "GET", "/upgrade/", "203.0.113.67"),
    ("research_slash", "GET", "/research/phoenix-az/", "203.0.113.68"),
    ("markets_slash", "GET", "/markets/atlanta/", "203.0.113.69"),
    ("api_slash", "GET", "/api/v1/stats/", "203.0.113.70"),
]

_CHILD = r'''
import json, sys
sys.path.insert(0, "scripts")
import app_contract_gate as g
app, _ = g.boot()
import rate_limiter
from routes.trailing_slash_redirect import MARKER
ua = sys.argv[3]

def tokens(ip):
    b = rate_limiter._buckets
    return [b.get("ip:%s:min" % ip, {}).get("tokens"),
            b.get("ip:%s:hr" % ip, {}).get("tokens")]

client = app.test_client()
out = {}
for name, method, path, ip in json.loads(sys.argv[2]):
    before = tokens(ip)
    r = client.open(path, method=method,
                    headers={"User-Agent": ua, "CF-Connecting-IP": ip},
                    environ_base={"REMOTE_ADDR": ip})
    out[name] = {"status": r.status_code, "location": r.headers.get("Location"),
                 "marker": r.headers.get(MARKER),
                 "before": before, "after": tokens(ip)}
json.dump(out, open(sys.argv[1], "w"))
import os; os._exit(0)
'''


@functools.lru_cache(maxsize=None)
def _real_app_run():
    """(results, skip_reason, failure) for _REAL_CASES, through main.py's app.

    Booted by scripts/app_contract_gate.boot() (DB stubbed, repo state files
    isolated), in a subprocess so main's import-time threads stay out of this
    one. Every before_request hook and error handler runs as in production.
    """
    out = os.path.join(tempfile.mkdtemp(), "slash.json")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, out, json.dumps(_REAL_CASES), _UA],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    if proc.returncode == 0 and os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            return json.load(fh), None, None
    # Same contract as tests/test_llms_cite_without_mcp.py: unit-tests installs
    # a light dep set, so a missing module THERE is a thin environment.
    # app-contract-gate installs requirements.txt and sets STRICT=1, so there a
    # missing module is a hard failure.
    if "ModuleNotFoundError" in proc.stderr and os.environ.get(
            "DCHUB_CONTRACT_GATE_STRICT") != "1":
        missing = next((line.strip() for line in proc.stderr.splitlines()
                        if "ModuleNotFoundError" in line), "a runtime dependency")
        return None, ("the real app cannot boot in this environment (%s); "
                      "app-contract-gate runs this file strictly" % missing), None
    tail = "\n".join(proc.stderr.splitlines()[-8:])
    return None, None, ("could not boot the real app (rc=%s). That is NOT a "
                        "pass: stderr tail:\n%s" % (proc.returncode, tail))


def _real(name):
    results, skip, failure = _real_app_run()
    if skip:
        pytest.skip(skip)
    assert failure is None, failure
    return results[name]


@pytest.mark.parametrize("name,location", [
    ("pricing_slash", "/pricing"),
    ("connect_slash", "/connect"),
    ("facilities_in_nl_slash", "/facilities/in/nl"),
])
def test_real_app_the_measured_404s_301_to_their_page(name, location):
    r = _real(name)
    assert (r["status"], r["location"], r["marker"]) == (301, location, "trailing-slash"), r


@pytest.mark.parametrize("name", ["pricing_slash", "connect_slash",
                                  "facilities_in_nl_slash"])
def test_real_app_the_hop_is_not_charged(name):
    r = _real(name)
    assert r["before"][0] is not None, "the warm-up did not open the bucket: %r" % r
    assert r["after"] == r["before"], r


def test_real_app_control_a_plain_404_is_charged():
    r = _real("no_page_slash")
    assert (r["status"], r["location"]) == (404, None), r
    assert r["after"] == [r["before"][0] - 1, r["before"][1] - 1], r


def test_real_app_one_visit_through_the_slash_costs_one_token():
    hop, page = _real("connect_slash"), _real("connect_again")
    assert page["status"] == 200, page
    assert page["after"] == [hop["before"][0] - 1, hop["before"][1] - 1], (hop, page)


def test_real_app_keeps_the_query_string_and_serves_head():
    assert _real("pricing_slash_qs")["location"] == "/pricing?utm_source=gsc&a=1"
    head = _real("pricing_slash_head")
    assert (head["status"], head["location"]) == (301, "/pricing"), head


def test_real_app_a_post_is_not_redirected():
    r = _real("connect_slash_post")
    assert (r["status"], r["location"]) == (404, None), r


def test_real_app_answers_that_were_not_404s_are_untouched():
    upgrade = _real("upgrade_slash")
    assert upgrade["status"] == 302, upgrade
    assert urlsplit(upgrade["location"]).netloc == "buy.stripe.com", upgrade
    research = _real("research_slash")
    assert (research["status"], research["location"]) == (302, "/grid-intelligence"), research
    markets = _real("markets_slash")
    assert (markets["status"], markets["location"]) == (301, "/markets/atlanta"), markets
    assert upgrade["marker"] is None and research["marker"] is None


def test_real_app_api_keeps_its_404():
    r = _real("api_slash")
    assert (r["status"], r["location"]) == (404, None), r


@pytest.mark.parametrize("name,slash", [
    ("pricing", "/pricing/"), ("connect", "/connect/"),
    ("facilities_in_nl", "/facilities/in/nl/")])
def test_real_app_the_target_never_sends_the_caller_back(name, slash):
    """The one loop this could make: the page itself redirecting to the slash."""
    r = _real(name)
    assert r["location"] is None or urlsplit(r["location"]).path != slash, r
