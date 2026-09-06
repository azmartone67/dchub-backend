#!/usr/bin/env python3
"""tests/test_dashboard_endpoint_perf.py — the two endpoints the /ai page waits
on, and the ways a speed fix quietly becomes a correctness change.

NO NETWORK, NO DB.

WHY. A cold load of /ai on 2026-09-05 rendered six panels in their
"unavailable" state because the fetches had not returned. Measured:

    /api/ai/tracking            1.9s   of which 0.83s was ONE query
    /api/v1/mcp/handoff-funnel  3.3s   57 sequential COUNT(DISTINCT) round trips

Both are polled by the dashboard every 30 seconds.

TWO FIXES, AND THE LINE BETWEEN THEM IS THE POINT:

  · tracking — push the roster filter INTO the query. 36,203 of 762,866 rows in
    the 7d window are rostered, so 95% of the scan was computed and discarded.
    0.83s -> 0.16s, results byte-identical.

  · funnel — a 30s result cache, TTL matched to the page's own refresh.

★ WHAT THIS FILE MOSTLY GUARDS IS WHAT WAS *NOT* DONE. Anchoring the
self-refresh LIKEs is faster still (0.09s) and CHANGES THE NUMBERS: `/health` is
a contains-pattern, and DC Hub has facilities named HealthPartners and Health
Dialog, so real crawls of those pages are excluded as our own polling. That is a
correctness bug with a judgment call in it and it ships separately, published as
a correction. A perf commit must never move a published figure — so this file
fails if the patterns are anchored HERE.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")
MCP = os.path.join(ROOT, "flask_mcp_endpoints.py")


def _self_refresh_query_src():
    """The execute() call that counts self-refresh rows, bounded by its AST node.

    ★ Located by LINE first. Walking every Call in main.py and calling
    ast.get_source_segment on each is O(nodes x file) and hung a 2-minute test
    run — main.py is ~40k lines. Find the statement's line by text, then take
    the one AST node that spans it: same exactness, no full-file segmentation.
    """
    src = open(MAIN, encoding="utf-8").read()
    marker = "SELECT platform, COUNT(*) FROM ai_requests "
    if marker not in src:
        return ""
    line = src[:src.index(marker)].count("\n") + 1
    best = None
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        lo = getattr(node, "lineno", None)
        hi = getattr(node, "end_lineno", None)
        if lo is None or hi is None or not (lo <= line <= hi):
            continue
        if best is None or (hi - lo) < (best.end_lineno - best.lineno):
            best = node          # innermost span containing the marker
    return ast.get_source_segment(src, best) or "" if best else ""


def test_the_scan_is_restricted_to_the_roster():
    q = _self_refresh_query_src()
    assert q, "the self-refresh query was not found"
    assert "platform = ANY(" in q, (
        "the query no longer restricts by platform — 95% of the 7d window is "
        "non-rostered and would be counted then discarded (0.83s vs 0.16s)")


def test_the_likes_are_anchored_prefixes():
    """★ FLIPPED 2026-09-05 by the correction that followed the perf change.

    This guard previously asserted the patterns were NOT anchored — its job was
    to stop a speed edit silently moving a published number. That correction has
    now shipped on its own, so the assertion inverts: leading-wildcard matching
    is the defect, because `/health` read as `%/health%` excluded real crawls of
    /facilities/healthpartners-data-center and
    /facilities/health-dialog-bedford-datacenter as our own polling."""
    q = _self_refresh_query_src()
    assert '_m + "%"' in q, (
        "the self-refresh patterns are not anchored — a contains-match on "
        "`/health` eats any path containing the word, and DC Hub has "
        "facilities operated by HealthPartners and Health Dialog")
    assert '"%" + _m' not in q, (
        "a leading wildcard is back on the self-refresh patterns")


def _funnel_src():
    src = open(MCP, encoding="utf-8").read()
    i = src.index("def handoff_funnel(")
    return src[i:src.index("\n@", i + 10)]


def test_the_funnel_publishes_its_cache_age():
    """★ Checked on the HIT BRANCH specifically. A first version asserted the
    token appeared anywhere in the function and stayed GREEN when age_s was
    deleted from the hit path — because the MISS path also sets it, where it is
    trivially 0.0 and tells a reader nothing. Found by mutation, not by reading:
    the age only matters on the branch that serves a stale value."""
    body = _funnel_src()
    i = body.index('_cached["cache"]')
    hit = body[i:body.index("return jsonify(_cached)", i)]
    assert '"age_s"' in hit and '"ttl_s"' in hit, (
        "the cache does not publish its age — a reader cannot tell a 29-second-"
        "old number from a live one, which turns a stale read into an "
        "unfalsifiable claim")
    assert '"hit"' in body, "the payload does not say whether it was a cache hit"


def test_a_failure_is_never_cached():
    """A transient DB error must not be served for 30s after it cleared, and an
    'unavailable' must always be a live verdict."""
    body = _funnel_src()
    m = re.search(r"if _ttl and out\.get\(\"ok\"\)", body)
    assert m, (
        "the cache stores unconditionally — an ok=False payload would be "
        "replayed for the whole TTL")
    # and the store must come after the ok flag is set by the except branch
    assert body.index('out["ok"] = False') < m.start(), (
        "the ok=False path runs AFTER the cache store, so a failure could still "
        "be written")


def test_the_cache_is_killable_and_ttl_is_30_by_default():
    src = open(MCP, encoding="utf-8").read()
    assert 'HANDOFF_FUNNEL_CACHE_TTL' in src, "no kill switch / override"
    assert '"HANDOFF_FUNNEL_CACHE_TTL", "30"' in src, (
        "default TTL is not 30s — it must match the /ai page's own refresh "
        "interval so a viewer never sees a figure older than one poll")


def test_ttl_zero_disables_the_cache():
    import importlib.util
    src = open(MCP, encoding="utf-8").read()
    i = src.index("def _funnel_ttl(")
    fn = src[i:src.index("\n\n", i)]
    ns = {"os": os}
    exec(fn, ns)
    prev = os.environ.get("HANDOFF_FUNNEL_CACHE_TTL")
    try:
        os.environ["HANDOFF_FUNNEL_CACHE_TTL"] = "0"
        assert ns["_funnel_ttl"]() == 0, "TTL=0 does not disable the cache"
        os.environ["HANDOFF_FUNNEL_CACHE_TTL"] = "not-a-number"
        assert ns["_funnel_ttl"]() == 30, "a malformed TTL must fall back to 30, not crash"
        del os.environ["HANDOFF_FUNNEL_CACHE_TTL"]
        assert ns["_funnel_ttl"]() == 30
    finally:
        if prev is None:
            os.environ.pop("HANDOFF_FUNNEL_CACHE_TTL", None)
        else:
            os.environ["HANDOFF_FUNNEL_CACHE_TTL"] = prev
