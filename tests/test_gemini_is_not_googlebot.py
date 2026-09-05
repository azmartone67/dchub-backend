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

★ COPILOT IS NOT THE SAME CASE, and this file pins that too. Its bucket is
mostly BingBot — but Copilot crawls as Bingbot and has NO other surface;
robots.txt reopened /api/ to Bingbot on 2026-08-31 precisely because "Copilot is
the point". The same edit there would zero a real channel rather than narrow a
mislabelled one. Symmetry would be the wrong instinct.
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


def test_copilot_is_deliberately_left_alone():
    """★ Not an oversight. Removing BingBot would zero the channel."""
    from ai_tracking import AI_PLATFORMS, detect_platform
    agents = [a.lower() for a in AI_PLATFORMS["copilot"]["agents"]]
    assert "bingbot" in agents, (
        "BingBot was removed from copilot by symmetry with the gemini fix. "
        "Copilot has no other surface — robots.txt reopened /api/ to Bingbot "
        "on 2026-08-31 because Copilot is the point. That edit zeroes a real "
        "channel instead of narrowing a mislabelled one")
    assert detect_platform("Mozilla/5.0 (compatible; bingbot/2.0)", "") == "copilot"


def test_the_discontinuity_is_published_not_silent():
    """gemini's forward reach drops to ~0 while its history keeps the old
    basis. Unstated, that reads as Gemini abandoning us — this repo has
    already shipped a correction that got reported as a collapse."""
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = src.index('"reach_definition"')
    block = src[i:i + 3400]
    assert "DISCONTINUITY" in block.upper(), (
        "reach_definition does not warn that gemini's drop is an attribution "
        "change")
    assert "NOT Gemini leaving" in block, (
        "the payload must say plainly what the drop is NOT")
    assert "OLD" in block and "basis" in block, (
        "a reader is not told the historical figures keep the old basis and "
        "are not revised")
