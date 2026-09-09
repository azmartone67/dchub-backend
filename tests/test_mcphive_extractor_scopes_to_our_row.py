r"""The MCPHive extractor must read OUR row, never the first row on the page.

WHY. The tracked URL for mcphive was `https://mcphive.com/` — the WRONG DOMAIN
(the live site is mcp-hive.com, hyphenated) and a bare homepage. It could never
carry a verdict, so the board showed a permanent red describing nothing, which
is exactly the "trains readers to ignore the colour" failure the four-state
verdict exists to prevent.

The corrected URL is a PROVIDER LISTING carrying many servers — measured
2026-09-09: 67 SoftwareApplication entries, DC Hub at index 41. The extractor
searched the whole page for `(\d+)\s*tools?` and took the first match, which
belonged to whichever server sorted first. It returned tools=3 (Weather API)
while our own row said 82.

★ A count scraped off someone else's row is WORSE than no count: it reads as
our drift and sends the board chasing a number we never published.

This is the same first-match defect the classifier already fixed once (mcp.so
carried "79 tools" x6 and "55 tools" x1, so the verdict depended on page order).
Fixing it there did not fix it here — one predicate, two consumers.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routes.mcp_presence_crawler import _extractor_mcphive  # noqa: E402

# The real page's shape: many rows, ours NOT first, each with its own count.
_LISTING = (
    '{"@type":"SoftwareApplication","name":"Weather API",'
    '"description":"Forecasts. 3 tools."}},'
    '{"@type":"ListItem","position":2,"item":'
    '{"@type":"SoftwareApplication","name":"Finance Server",'
    '"description":"Markets. 5 tools."}},'
    '{"@type":"ListItem","position":42,"item":'
    '{"@type":"SoftwareApplication",'
    '"name":"DC Hub — Data Center, Interconnection & Capacity Intelligence",'
    '"description":"The live data-center layer. 21,200+ facilities — 88 tools."}},'
    '{"@type":"ListItem","position":43,"item":'
    '{"@type":"SoftwareApplication","name":"AgentForge",'
    # ★ DELIBERATELY LARGER THAN OURS. With a neighbour at 9 the whole-page
    # max() coincides with our own 88, so a broken extractor that ignores
    # scoping entirely still returns the right answer and the guard passes by
    # luck. 150 makes first-match AND whole-page-max both wrong, so only real
    # scoping survives. Verified by mutation: replacing the scope with
    # `blob = html` now fails this file on the count assertion, not just on
    # the absent-row one.
    '"description":"Compare tools. 150 tools."}}'
)


def test_reads_our_row_not_the_first_row():
    got = _extractor_mcphive(_LISTING)
    assert got is not None, "our row is present; the extractor must find it"
    assert got["tools"] == 88, (
        "read %r — that is another server's count. The first row on this page "
        "declares 3 tools; ours declares 88." % (got["tools"],)
    )
    assert got["listing_title"].startswith("DC Hub"), got["listing_title"]


def test_absent_row_is_unverified_not_zero():
    """A page without us must be None. None is UNVERIFIED; 0 is a measurement."""
    got = _extractor_mcphive(
        '{"@type":"SoftwareApplication","name":"Weather API",'
        '"description":"Forecasts. 3 tools."}}'
    )
    assert got is None, (
        "returned %r for a page that does not list us — an absent listing and "
        "a listing we could not parse must never look like a measured result" % (got,)
    )


def test_a_count_on_a_neighbouring_row_cannot_leak_in():
    """The scope must END at our row, not run on into the next server."""
    got = _extractor_mcphive(_LISTING)
    assert got["tools"] != 9, "picked up the row AFTER ours"
    assert got["tools"] != 3, "picked up the row BEFORE ours"


def test_the_extractor_is_still_wired_to_mcphive():
    """Floor: a renamed extractor would make every assertion above vacuous."""
    from routes.mcp_presence_crawler import _EXTRACTORS
    assert _EXTRACTORS.get("mcphive") is _extractor_mcphive, (
        "mcphive is no longer wired to this extractor — these guards now "
        "protect a function nothing calls"
    )
