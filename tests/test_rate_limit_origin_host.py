"""SH52-126: the rate-limiter's same-origin bypass must be an EXACT-HOST
allowlist, not a substring test.

The old guard was `'dchub.cloud' in origin` — any client could satisfy it by
sending the header, and it also accepted hostile lookalike hosts such as
`dchub.cloud.evil.com`. `_origin_host_is_trusted` now parses the Origin/Referer
header and compares the HOST.

Design constraints: no import of main.py, no DB, no network — rate_limiter is a
leaf module (only internal_auth + railway_egress helpers).
"""
import rate_limiter


def test_exact_host_bypasses():
    assert rate_limiter._origin_host_is_trusted("https://dchub.cloud") is True
    assert rate_limiter._origin_host_is_trusted("https://dchub.cloud/map?z=8") is True
    assert rate_limiter._origin_host_is_trusted("http://dchub.cloud") is True


def test_subdomains_bypass():
    assert rate_limiter._origin_host_is_trusted("https://www.dchub.cloud") is True
    assert rate_limiter._origin_host_is_trusted("https://api.dchub.cloud/x") is True


def test_lookalike_hosts_do_not_bypass():
    # The exact class the substring test let through.
    assert rate_limiter._origin_host_is_trusted("https://dchub.cloud.evil.com") is False
    assert rate_limiter._origin_host_is_trusted("https://evildchub.cloud.attacker.net") is False
    # Suffix-without-dot must not match `.dchub.cloud`.
    assert rate_limiter._origin_host_is_trusted("https://xdchub.cloud") is False
    # Bare token in a path, no host.
    assert rate_limiter._origin_host_is_trusted("dchub.cloud") is False


def test_empty_and_garbage_do_not_bypass():
    assert rate_limiter._origin_host_is_trusted("") is False
    assert rate_limiter._origin_host_is_trusted(None) is False
    assert rate_limiter._origin_host_is_trusted("not a url") is False


# ---------------------------------------------------------------------------
# ★2026-09-02 — BEHAVIOURAL TESTS FOR rate_limit_before() ITSELF.
#
# Everything above this line tests the HELPER in isolation, and every one of
# those assertions passed continuously while rate_limit_before() still carried a
# SECOND, unmigrated bypass:
#
#     origin = request.headers.get('Origin', '') or request.headers.get('Referer', '')
#     if 'dchub.cloud' in origin:
#         return None
#
# The host-exact gate returns first, so that block was only ever reached once the
# host gate had ALREADY rejected the origin — it did nothing except re-admit
# exactly the lookalike hosts `test_lookalike_hosts_do_not_bypass` asserts
# against. A unit test on a helper cannot see an unmigrated call site; only a
# test that drives the real entry point can. These do.
#
# Observable: `g._rl_limit` is stashed by rate_limit_before ONLY after it has
# passed every bypass and engaged the limiter. Present => limiter engaged.
# Absent  => the request was bypassed. Return value alone cannot tell the two
# apart, because an allowed request and a bypassed one both return None.
# ---------------------------------------------------------------------------
import flask
import pytest

# A path that is neither in SKIP_PATHS nor under SKIP_PREFIXES, so the origin
# gate is genuinely the thing under test.
_LIMITED_PATH = "/api/v1/facilities/search"
# TEST-NET-3 (RFC 5737): not loopback, not Railway egress, so neither of those
# bypasses fires and the test cannot pass for the wrong reason.
_PUBLIC_IP = "203.0.113.7"


def _limiter_engaged(origin_header, path=_LIMITED_PATH, header="Origin"):
    """Run the real rate_limit_before() and report whether it reached the
    limiter (True) or short-circuited on a bypass (False)."""
    app = flask.Flask(__name__)
    rate_limiter._buckets.clear()
    with app.test_request_context(
        path,
        headers={header: origin_header,
                 "User-Agent": "Mozilla/5.0 (behavioural test)",
                 "CF-Connecting-IP": _PUBLIC_IP},
        environ_base={"REMOTE_ADDR": _PUBLIC_IP},
    ):
        rate_limiter.rate_limit_before()
        return hasattr(flask.g, "_rl_limit")


def test_control_the_path_under_test_is_actually_limited():
    """Guard-the-guard: if this path were skipped or the IP were treated as
    internal, every assertion below would pass vacuously."""
    assert _limiter_engaged("https://unrelated.example.com") is True


@pytest.mark.parametrize("origin", [
    "https://dchub.cloud.evil.com",
    "https://evildchub.cloud.attacker.net",
    "https://xdchub.cloud",
    "https://evil.example.com/?redirect=https://dchub.cloud",
    "https://evil.example.com/#dchub.cloud",
])
def test_lookalike_origin_does_not_bypass_the_limiter(origin):
    """THE REGRESSION. Every one of these satisfied `'dchub.cloud' in origin`
    and skipped rate limiting entirely."""
    assert _limiter_engaged(origin) is True, (
        "%s bypassed the rate limiter — the substring bypass is back" % origin)


@pytest.mark.parametrize("origin", [
    "https://dchub.cloud",
    "https://dchub.cloud/map?z=8",
    "https://www.dchub.cloud",
    "https://api.dchub.cloud/x",
])
def test_real_frontend_origin_still_bypasses(origin):
    """The bypass exists for a reason — the map's pan/zoom storm. Removing the
    substring block must not cost the legitimate case."""
    assert _limiter_engaged(origin) is False, (
        "%s should still bypass — the map would start seeing 429s" % origin)


def test_referer_is_honoured_the_same_way_as_origin():
    assert _limiter_engaged("https://dchub.cloud/map", header="Referer") is False
    assert _limiter_engaged("https://dchub.cloud.evil.com", header="Referer") is True


def test_no_substring_origin_comparison_survives_in_the_module():
    """AST, not grep — the prose in this file and in rate_limiter.py both
    contain the literal `'dchub.cloud' in origin` on purpose, to explain the
    bug. A text search cannot tell an explanation from an exploit."""
    import ast as _ast
    import inspect
    tree = _ast.parse(inspect.getsource(rate_limiter))
    offenders = [
        n.lineno for n in _ast.walk(tree)
        if isinstance(n, _ast.Compare)
        and any(isinstance(o, _ast.In) for o in n.ops)
        and isinstance(n.left, _ast.Constant)
        and isinstance(n.left.value, str)
        and "dchub.cloud" in n.left.value
    ]
    assert offenders == [], (
        "substring-origin comparison(s) back at line(s) %s" % offenders)
