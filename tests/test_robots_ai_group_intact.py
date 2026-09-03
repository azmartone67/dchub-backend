"""The named AI-crawler group in /robots.txt must be exactly ONE group.

WHY THIS IS A TEST AND NOT A COMMENT. Under RFC 9309 a group is a run of
User-agent lines followed by its directives, and the FIRST non-User-agent line
ends the run. So a directive — or a Content-Signal, or an Allow — placed
mid-list splits the block in two and every user-agent below it silently
inherits NOTHING: no /sites/ hygiene, no /admin block, no /api/ allowance.

The file itself carries that warning in prose. Prose is what this repo keeps
learning is not enough, and the failure here is invisible: robots.txt still
parses, still serves 200, and the orphaned partners just quietly get the
wildcard policy instead. Nobody would find it except by re-deriving the group
from the bytes — which is what this does.

It also pins the parity rule: an AI partner not named in the group falls
through to `User-agent: *`, which carries Disallow: /api/ — the one surface the
assistant crawlers actually fetch. Omission is therefore not neutral, it is a
STRICTER policy applied by accident. You.com was the measured case: 1.34K
reach/7d while unnamed.

Source-text test on purpose: tests/ never imports Flask, and the robots body
lives inside a route module that pulls the app. Reading the bytes is also
closer to what a crawler does than any import would be.
"""
import pathlib
import re

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "ai_discovery_routes.py"

# Partners with measured reach, or explicitly welcomed. Each MUST be inside the
# named group; each absence is an /api/ block applied by omission.
REQUIRED_PARTNERS = (
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",          # OpenAI
    "ClaudeBot", "Claude-User", "Claude-SearchBot",      # Anthropic
    "PerplexityBot", "Perplexity-User",                  # Perplexity
    "Googlebot", "GoogleOther", "Google-Extended",       # Google / Gemini
    "YouBot",                                            # You.com — 1.34K/7d
    "MistralAI-User", "DuckAssistBot", "cohere-ai",
    "Meta-ExternalFetcher", "meta-externalagent",
    "Applebot", "Applebot-Extended",
    "GrokBot", "xAI-Bot",                                # xAI, at their request
)


def _ai_group():
    """Re-derive the group from the bytes, the way a crawler parses it."""
    src = _SRC.read_text()
    start = src.index("User-agent: GPTBot")
    end = src.index("User-agent: Bingbot", start)      # the next, separate group
    lines = [l.strip() for l in src[start:end].split("\n")
             if l.strip() and not l.strip().startswith("#")]
    first_directive = next(
        (i for i, l in enumerate(lines) if not l.startswith("User-agent:")),
        len(lines),
    )
    uas = [l.split(":", 1)[1].strip() for l in lines[:first_directive]]
    rest = lines[first_directive:]
    return uas, rest


def test_the_group_is_not_split():
    """★ THE CONTROL. No user-agent may appear AFTER the first directive."""
    uas, rest = _ai_group()
    orphaned = [l for l in rest if l.startswith("User-agent:")]
    assert not orphaned, (
        "robots.txt AI group is SPLIT — these user-agents sit below a directive "
        "and inherit nothing (no /sites/ hygiene, no /admin block, no /api/ "
        f"allowance): {orphaned}"
    )
    assert len(uas) >= 20, f"group collapsed to {len(uas)} user-agents"


@pytest.mark.parametrize("partner", REQUIRED_PARTNERS)
def test_every_measured_partner_is_named(partner):
    """An unnamed AI partner falls through to `*`, which blocks /api/ — the one
    surface the assistant crawlers fetch. Omission is a stricter policy applied
    by accident, not a neutral default."""
    uas, _ = _ai_group()
    assert partner in uas, (
        f"{partner} is not in the named AI group, so it is served "
        "`User-agent: *` — including Disallow: /api/"
    )


def test_the_hygiene_rules_are_repeated_inside_the_group():
    """A named group inherits nothing. Measured 2026-07-28 with a bare
    `Allow: /` here: 20% of crawl budget went to /sites/*, and only 24% reached
    /facilities/*."""
    _, rest = _ai_group()
    for rule in ("Disallow: /sites/", "Disallow: /cdn-cgi/", "Disallow: /admin"):
        assert rule in rest, f"{rule!r} missing — void for every named partner"


def test_api_stays_open_to_the_assistant_crawlers():
    """Closed by a blanket Disallow on 2026-06-13, which silently cut the
    assistant crawlers off until 2026-06-28. It must not close again by
    accident."""
    _, rest = _ai_group()
    assert "Disallow: /api/" not in rest
    assert "Allow: /" in rest
