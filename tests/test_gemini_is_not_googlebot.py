#!/usr/bin/env python3
"""tests/test_gemini_is_not_googlebot.py — Google Search's crawler is not an
AI platform, and the correction must not read as a collapse.

NO NETWORK, NO DB.

MEASURED 2026-09-05 over ai_requests, platform='gemini', 7 days:

    837  Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)
    837  TOTAL
      0  Google-Extended     <- the token Google DOCUMENTS for AI use
      0  GoogleOther
      0  any UA naming Gemini

100% of the published "Gemini" reach bar was our SEO crawl volume wearing an AI
platform's name and colour. Every Google Search crawl-rate change was reading as
AI demand moving.

★ THE NARROWING IS DELIBERATE AND BOUNDED. Google-Extended and GoogleOther stay:
they are Google's AI-side agents, and neither substring occurs in a plain
Googlebot UA, so real Gemini traffic is still attributable the moment it appears.
Only the Search crawler leaves.

★ COPILOT WAS NOT THE SAME CASE, AND THEN THE REQUIREMENT CHANGED (2026-09-08).
This file used to pin BingBot INSIDE copilot, on the reasoning that Copilot
crawls as Bingbot and has no other surface, so narrowing it would zero a real
channel rather than narrow a mislabelled one. That reasoning still holds on its
own terms and is why the split below is a RENAME of the only Copilot-adjacent
signal, not a discovery of a hidden one — Microsoft publishes no Bing-side AI
token, unlike Google-Extended.

What changed: the edge beacon now records Bingbot on CONTENT pages, thousands of
page fetches a day against the ~12/day of /api traffic the Flask hook saw.
Pouring a search-index crawl of that size into an assistant's bar is the gemini
failure at ~100x the volume, so measuring Bing and publishing "Copilot reach"
stopped being compatible. Bingbot now resolves to its own `bing` bucket;
`copilot` keeps its Copilot marker so a real Copilot UA still attributes.

★ THE ASYMMETRY IS STILL THE POINT, just resolved the other way: gemini's
narrowing REVEALED that a bar was mislabelled (837 Googlebot / 0
Google-Extended). This one does not reveal anything — it separates two uses of
one UA so a volume change in the crawl cannot be read as demand.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GOOGLEBOT = ("Mozilla/5.0 (compatible; Googlebot/2.1; "
             "+http://www.google.com/bot.html)")


def test_google_search_crawler_is_not_gemini():
    from ai_tracking import detect_platform
    got = detect_platform(GOOGLEBOT, "")
    assert got != "gemini", (
        "plain Googlebot attributes to gemini — 837 of 837 rows measured "
        "2026-09-05 were this UA, so the whole bar is Google Search")
    assert got == "seo_bot", (
        "Googlebot resolved to %r; it must reach the generic-bot branch so "
        "_log_ai_request drops it, not land in another named bucket" % got)


def test_googles_ai_side_agents_still_attribute():
    """The narrowing must not blind us to real Gemini traffic."""
    from ai_tracking import detect_platform
    for ua in ("Mozilla/5.0 (compatible; Google-Extended/1.0)",
               "Mozilla/5.0 (compatible; GoogleOther)",
               "Gemini/1.0"):
        assert detect_platform(ua, "") == "gemini", (
            "%r no longer attributes to gemini — the narrowing went too far "
            "and we can no longer see Google's AI agents at all" % ua)


def test_the_roster_entry_kept_the_ai_tokens():
    from ai_tracking import AI_PLATFORMS
    agents = [a.lower() for a in AI_PLATFORMS["gemini"]["agents"]]
    assert "googlebot" not in agents, (
        "Googlebot is back in gemini's agent list")
    for keep in ("google-extended", "googleother"):
        assert keep in agents, (
            "%s was removed too — Google's AI agents must stay attributable"
            % keep)


def test_bingbot_is_its_own_bucket_not_copilot():
    """★2026-09-08 — this assertion is the REVERSE of what stood here.

    It previously required `bingbot in copilot.agents`, with the reason quoted in
    the docstring above. The split is deliberate and its cost is published in
    main.py's reach_definition, the same standard the gemini narrowing was held
    to. Do not "restore symmetry" without reading both notes.
    """
    from ai_tracking import AI_PLATFORMS, detect_platform
    cop = [a.lower() for a in AI_PLATFORMS["copilot"]["agents"]]
    assert "bingbot" not in cop, (
        "BingBot is back inside copilot. Its content-page crawl is ~100x the "
        "/api volume, so it would publish a search-index crawl as assistant "
        "reach — the gemini failure at scale.")
    assert detect_platform("Mozilla/5.0 (compatible; bingbot/2.0)", "") == "bing"


def test_copilot_is_narrowed_not_deleted():
    """A real Copilot UA must still attribute, or this was a deletion."""
    from ai_tracking import AI_PLATFORMS, detect_platform
    assert "copilot" in [a.lower() for a in AI_PLATFORMS["copilot"]["agents"]], (
        "copilot lost its own marker — the bucket is now unreachable and the "
        "channel is deleted rather than narrowed")
    assert detect_platform("Mozilla/5.0 Copilot", "") == "copilot"


def test_bing_is_measured_but_not_counted_as_an_ai_platform():
    """The whole point of the separate id: measured, not promoted."""
    from ai_platform_canon import canonical_platform, count_platforms
    assert canonical_platform("bing") is None, (
        "bing became a canonical vendor — a search crawler would now inflate "
        "the published 'N AI platforms' count.")
    assert count_platforms(["bing", "copilot", "claude"]) == 2, (
        "count_platforms counts bing; it must drop it as unrecognized.")
    from ai_tracking import AI_PLATFORMS
    assert "bing" in AI_PLATFORMS, (
        "the bing bucket is gone, so Bingbot traffic falls somewhere unnamed")


def test_the_discontinuity_is_published_not_silent():
    """gemini's forward reach drops to ~0 while its history keeps the old
    basis. Unstated, that reads as Gemini abandoning us — this repo has
    already shipped a correction that got reported as a collapse."""
    # ★ AST, not a fixed slice. The sibling guard in
    # tests/test_reach_self_refresh_split.py used src[i:i+1800] and THIS commit
    # broke it — the text below added ~900 chars and pushed the pointer it
    # checks for past the window, failing a guard whose subject was still
    # correct. Same defect, so the same fix, in both places.
    from tests.test_reach_self_refresh_split import _reach_definition_text
    block = _reach_definition_text()
    assert "DISCONTINUITY" in block.upper(), (
        "reach_definition does not warn that gemini's drop is an attribution "
        "change")
    assert "NOT Gemini leaving" in block, (
        "the payload must say plainly what the drop is NOT")
    assert "OLD" in block and "basis" in block, (
        "a reader is not told the historical figures keep the old basis and "
        "are not revised")
