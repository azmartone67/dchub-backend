"""Qualifying a directory must not answer questions it cannot see.

Companion to the reachability gate (#3896) and verified auto-paths (#3944).
This module decides whether to put a directory on a human's worklist, so a
wrong "already listed" silently REMOVES work that needs doing.

★ THE BUG THIS FILE WAS BORN FROM. The first matcher looked for "dchub"
anywhere in the page body and reported mcp.so as ALREADY LISTED. The listing
URL is `?q=dchub` and the page echoes the query back — so searching for
ourselves always found ourselves, and the one aggregator we are genuinely
absent from would have dropped straight off the worklist. Verified by hand the
same day in a browser: mcp.so returns "No servers match" for dchub.
A real listing is a LINK to a server page; an echoed query is not.
"""
import pytest

from routes.mcp_onboarding import (
    _slug_for,
    build_worklist,
    qualify,
    tag_availability,
)

CTRL = "playwright github notion "          # satisfies the positive control
BIG = "x" * 2000                            # clears MIN_LISTING_BYTES


def _get(body, status=200):
    return lambda url: (body, status)


def _post(status):
    return lambda url: ("", status)


# ── the regression that started it ───────────────────────────────────────
def test_an_echoed_search_query_is_not_a_listing():
    """?q=dchub echoed into the page must NOT read as 'already listed'."""
    body = CTRL + BIG + '<input value="dchub"> <h1>Search results for "dchub"</h1>'
    r = qualify({"host": "mcp.so", "kind": "aggregator",
                 "listing_url": "https://mcp.so/search?q=dchub"}, fetch=_get(body))
    assert r["lists_us"] == "no", r
    assert r["barrier"] == "submission required"


def test_a_real_link_is_a_listing():
    body = CTRL + BIG + '<a href="/servers/dchub-mcp-server">DC Hub</a>'
    r = qualify({"host": "x.test", "kind": "aggregator",
                 "listing_url": "https://x.test/"}, fetch=_get(body))
    assert r["lists_us"] == "yes"
    assert r["barrier"] == "already listed"


def test_explicit_no_results_is_believed():
    body = CTRL + BIG + "No servers match your search"
    r = qualify({"host": "x.test", "kind": "aggregator",
                 "listing_url": "https://x.test/"}, fetch=_get(body))
    assert r["lists_us"] == "no"


# ── never answer from a page we could not read ───────────────────────────
@pytest.mark.parametrize("body,status", [
    (None, None),                                    # transport failure
    ("", 200),                                       # empty 200
    ("<title>Vercel Security Checkpoint</title>" + BIG, 429),
    ("nope" + BIG, 404),
])
def test_unreadable_pages_are_unknown_not_absent(body, status):
    r = qualify({"host": "x.test", "kind": "client",
                 "listing_url": "https://x.test/"}, fetch=_get(body, status))
    assert r["lists_us"] == "unknown", r
    assert r["directory"] == "unknown"
    assert "unreadable" in r["barrier"]


def test_a_page_with_no_known_server_cannot_be_judged():
    """claude.ai/directory is a JS app: a script sees no server names at all.
    Reporting 'not listed' from that page would be a measurement of nothing."""
    r = qualify({"host": "claude.ai", "kind": "client",
                 "listing_url": "https://claude.ai/directory"},
                fetch=_get("<div id=root></div>" + BIG))
    assert r["lists_us"] == "unknown"
    assert "control failed" in r["barrier"]


# ── the tag must exist before the listing does ───────────────────────────
@pytest.mark.parametrize("status,state", [
    (200, "already_serving"),
    (404, "available"),
    (405, "available"),
    (503, "unknown"),
])
def test_tag_availability_states(status, state):
    assert tag_availability("mcp.so", fetch=_post(status))["state"] == state


@pytest.mark.parametrize("host,slug", [
    ("mcp.so", "mcp"), ("code.visualstudio.com", "codevisualstudio"),
    ("www.klavis.ai", "klavis"), ("cursor.directory", "cursor"),
])
def test_slugs_are_path_safe(host, slug):
    s = _slug_for(host)
    assert s == slug and s.isalnum()


# ── ordering is a decision, not cosmetics ────────────────────────────────
def test_client_directories_outrank_aggregators():
    """Measured 2026-09-05: #11,607 of ~21,970 on PulseMCP, while identifiable
    MCP clients are 3.2% of /mcp traffic. Another aggregator row is not what
    moves adoption, so a client directory must sort first."""
    cands = [{"host": "agg.test", "kind": "aggregator", "listing_url": "https://agg.test/"},
             {"host": "cli.test", "kind": "client", "listing_url": "https://cli.test/"}]
    rows = build_worklist(cands, fetch_get=_get(CTRL + BIG), fetch_post=_post(404))
    assert rows[0]["host"] == "cli.test", [r["host"] for r in rows]
