#!/usr/bin/env python3
"""tests/test_sitemap_fetch_visible.py — a sitemap fetch must be recordable,
and must not land in the content bucket.

NO NETWORK, NO DB.

WHAT WAS INVISIBLE. We published /sitemap-ai.xml (#3909) and named it in
llms.txt (#3913) and AGENTS.md (#3927) so retrieval crawlers could find 15,950
facility URLs the ranking sitemap withholds. Then a fetch of that sitemap was
recorded by NOBODY: `^/sitemap` was absent from ai_tracking.AI_ENDPOINT_PATTERNS
AND from the edge beacon's ORGANIC_CONTENT_PREFIXES.

So on 2026-09-05 17:20–17:22, when ClaudeBot read /llms.txt and then
agent-card.json, mcp-server.json and /AGENTS.md, the one question the whole
change set exists to answer — did it go and fetch the sitemap those files name —
could not be answered from any table. The experiment could not observe its own
precondition.

★ THE LOAD-BEARING ASSERTION IS THE BUCKET, NOT THE ALLOWLIST. A sitemap is a
DISCOVERY artefact, like llms.txt. If these rows landed in `organic_content`
they would inflate precisely the number the AI sitemap exists to move — we would
publish a sitemap, count the crawler fetching the sitemap as a content crawl,
and report the change as its own success.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_a_sitemap_fetch_is_recordable_at_all():
    from ai_tracking import is_ai_endpoint
    for p in ("/sitemap.xml", "/sitemap-ai.xml",
              "/sitemap-ai-facilities-1.xml", "/sitemap-facilities-1.xml"):
        assert is_ai_endpoint(p), (
            "%s is not in AI_ENDPOINT_PATTERNS — a crawler following the "
            "pointer we publish would leave no trace" % p)


def test_sitemap_fetches_are_metadata_never_content():
    """★ The one that matters."""
    from crawler_externality import classify_path
    for p in ("/sitemap.xml", "/sitemap-ai.xml",
              "/sitemap-ai-facilities-3.xml"):
        got = classify_path(p)
        assert got == "instructed_metadata", (
            "%s classifies as %r — a sitemap fetch is a discovery artefact, "
            "and counting it as organic_content would inflate the exact number "
            "the AI sitemap was published to move" % (p, got))
    # the content bucket must still work for real content
    assert classify_path("/facilities/equinix-hk1-abc") == "organic_content"


def test_the_two_collectors_do_not_both_claim_sitemap():
    """No double-count. The edge beacon deliberately does NOT carry /sitemap;
    this repo's declared copy of its prefix list is the only thing here that
    can be checked, and it must stay clean."""
    from crawler_externality import WORKER_TRACKED_PREFIXES
    assert not any("sitemap" in p for p in WORKER_TRACKED_PREFIXES), (
        "the declared worker prefix list gained /sitemap — with the Flask hook "
        "now recording these, both collectors would write one fetch twice")


def test_the_caching_asymmetry_is_published_not_assumed():
    """/sitemap.xml is edge-cached and reaches the origin only on a MISS; the
    AI family answers DYNAMIC and is seen in full. A reader comparing the two
    counts would conclude the AI sitemap is more popular. Say so in the payload,
    not in a commit message nobody reads at query time."""
    from crawler_externality import collector_coverage
    cov = collector_coverage()
    assert "sitemap_fetches" in cov, (
        "collector_coverage does not mention sitemap instrumentation at all")
    txt = cov["sitemap_fetches"]
    assert "UNDER-COUNTED" in txt.upper(), (
        "the asymmetry is not stated — /sitemap.xml being cached makes its "
        "count a floor, and that must travel with the number")
    assert "not comparable" in txt.lower(), (
        "a reader must be told the two counts cannot be compared")
    assert "blind" in txt.lower(), (
        "the failure mode — a future cache rule silencing this instrument with "
        "no signal at the origin — must be named where it is read")


def test_the_pattern_is_anchored():
    """`^/sitemap` must anchor at the path root: an arbitrary path merely
    CONTAINING 'sitemap' is not ours to count."""
    from ai_tracking import is_ai_endpoint
    assert not is_ai_endpoint("/facilities/a-sitemap-of-texas")
    assert not is_ai_endpoint("/news/reading-a-sitemap")
