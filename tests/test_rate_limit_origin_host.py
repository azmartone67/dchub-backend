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
