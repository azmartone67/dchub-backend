#!/usr/bin/env python3
"""tests/test_reach_self_refresh_split.py — the published reach figure must
name the share of itself that is our own dashboards.

NO NETWORK, NO DB.

MEASURED 2026-09-05, /api/ai/tracking chart_data, 7d, all IPs:

    platform    requests_7d   self_refresh   % self
    chatgpt          19,052             14     0.1%
    perplexity        8,811              2     0.0%
    claude            5,352          1,318    24.6%     <-- a quarter
    you               1,350            223    16.5%
    copilot             160             19    11.9%
    gemini              843             90    10.7%

The claude rows are genuine `Claude/1.40609.1 ... Electron/42.7.0` — the Claude
DESKTOP app with a DC Hub dashboard open, polling
`/api/v1/mcp/handoff-funnel` 10,050 times from one machine. detect_platform()
is correct by its own rule; nothing is spoofed. It is simply not Claude-the-AI
reading DC Hub.

★ THE DEFECT WAS WIRING, NOT A MISSING RULE. FEED_SELF_REFRESH_ENDPOINTS has
named `/api/v1/mcp/` since 2026-08-06 and had exactly ONE call site — inside the
recent-activity FEED query. It cleaned the 20-row display while the headline
counted every poll. Same shape as the organic_content bucket (#3906).
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _tracking_route(strip_comments=True):
    """Body of ai_tracking_full(), comments stripped by default.

    The comments in that function quote the defect and the old wording; a
    guard matching prose would pass on a body whose CODE had been reverted."""
    s = open(MAIN, encoding="utf-8").read()
    i = s.index("def ai_tracking_full(")
    j = s.index("\n@app.route", i + 10)
    body = s[i:j]
    if strip_comments:
        body = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("#"))
    return body


def test_the_endpoint_list_is_imported_not_retyped():
    """★ A second hand-typed copy is how the two halves drift apart. The feed
    and the count must be filtered by the SAME list object."""
    body = _tracking_route()
    assert "from ai_tracking import FEED_SELF_REFRESH_ENDPOINTS" in body, (
        "the self-refresh split does not import FEED_SELF_REFRESH_ENDPOINTS — "
        "if it retypes the paths, the feed and the headline will disagree the "
        "first time either list is edited")
    # and no inline path list masquerading as one
    assert not re.search(r"\[\s*['\"]/api/v1/mcp/['\"]", body), (
        "an inline path list appears in the route — use the imported constant")


def test_the_list_still_carries_the_load_bearing_entry():
    """The whole finding rests on /api/v1/mcp/ — 10,050 of 10,816 rows."""
    from ai_tracking import FEED_SELF_REFRESH_ENDPOINTS as SRE
    assert "/api/v1/mcp/" in SRE, (
        "/api/v1/mcp/ left the list — handoff-funnel polling would be counted "
        "as platform reach again")
    assert len(SRE) >= 15, "the list shrank unexpectedly: %d entries" % len(SRE)


def test_requests_7d_is_never_overwritten():
    """★ requests_7d keeps its exact meaning and value. The split is ADDITIVE:
    published beside it, never subtracted silently, so a reader can audit or
    reject the subtraction. Same contract as mcp_calls_30d /
    mcp_calls_30d_including_self_traffic."""
    body = _tracking_route()
    i = body.index("self_refresh_7d")
    seg = body[max(0, i - 1500):i + 1500]
    assert not re.search(r'\["requests_7d"\]\s*=', seg), (
        "the split ASSIGNS to requests_7d — the published figure would change "
        "meaning without changing name, which is the failure this file exists "
        "to prevent")
    assert '"requests_7d_net_of_self_refresh"' in body, (
        "the net figure is not published — a reader cannot act on the split")
    assert '"self_refresh_7d"' in body, "the subtrahend is not published"


def test_a_failed_query_leaves_the_field_absent_not_zero():
    """A 0 would read as 'measured, none found'. The query did not run."""
    body = _tracking_route()
    i = body.index("self-refresh split skipped")
    seg = body[max(0, i - 900):i + 200]
    assert not re.search(r'self_refresh_7d"?\]?\s*=\s*0', seg), (
        "the failure path sets self_refresh_7d to 0 — absence and a measured "
        "zero are different claims")


def test_the_reach_definition_no_longer_says_it_cannot_be_subtracted():
    """The old definition said DC Hub's own fetches 'cannot be subtracted from
    it'. One large, named component can be, and now is."""
    s = open(MAIN, encoding="utf-8").read()
    i = s.index('"reach_definition"')
    block = s[i:i + 1800]
    assert "cannot be subtracted from it" not in block, (
        "reach_definition still claims the figure cannot be corrected, while "
        "the payload beside it now publishes the correction")
    assert "self_refresh_7d" in block, (
        "reach_definition does not point at the fields that carry the "
        "subtraction — the reader is told to trust rather than to check")
    assert "crawler_split_7d" in block, (
        "reach_definition dropped the pointer to the PATH split, which is what "
        "still answers the part self_refresh_7d does not")
