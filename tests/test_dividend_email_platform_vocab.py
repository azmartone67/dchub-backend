"""Guard for the MCP Dividend email's platform vocabulary (2026-08-11).

★ THE FAILURE THIS ENCODES

The email carried a hand-copied tuple of EXACT platform strings, matched with
`lower(platform)=ANY(...)` — a second vocabulary alongside
ai_platform_canon.KNOWN_AI_TOKENS.

On 2026-08-03 three commits FIXED mcp attribution (#2189/#2192/#2196: 61% of
traffic was unattributed because mcp_sessions was never created and the
session->platform persist was skipped). Platform labels became more accurate.
The next Monday the email reported:

    anthropicapi 40 -> 0     cursor 17 -> 0     copilot 3 -> 0

Three channels to EXACTLY zero in one week. Not decay — the filter silently
dropping rows whose labels it no longer recognised. An attribution FIX rendered
as a traffic collapse, and the founder read it as one.

★ TWO SETS, ON PURPOSE. The canon answers "how many AI VENDORS" and correctly
excludes Smithery and Glama — they are gateways proxying other vendors' agents,
and counting them there would inflate a published platform number. This email
asks "which CHANNELS sent sessions", where they are real and large (Smithery is
the biggest single caller by 30d tool calls). Different questions, different
sets, both named in the source rather than one silently copied from the other.
"""
import re

import pytest

WF = ".github/workflows/mcp-dividend-weekly.yml"
SRC = open(WF, encoding="utf-8").read()
canon = pytest.importorskip("ai_platform_canon")


def test_the_hand_copied_tuple_is_gone():
    """Its return is the bug returning."""
    assert "REAL=(" not in SRC
    assert "'anthropicapi','openai-mcp'" not in SRC
    assert "lower(platform)=ANY(%s)" not in SRC


def test_vocabulary_is_derived_from_the_canon():
    assert "from ai_platform_canon import KNOWN_AI_TOKENS" in SRC
    assert "_CHANNEL_TOKENS = tuple(KNOWN_AI_TOKENS) + _REGISTRY_TOKENS" in SRC


def test_the_job_can_actually_import_it():
    """The list was hand-copied because the job never checked out the repo —
    there was no way to reach the canon. Without this step the import raises
    and the email dies silently on a Monday.
    """
    assert "actions/checkout" in SRC
    assert SRC.index("actions/checkout") < SRC.index("actions/setup-python")


def test_matching_is_substring_not_exact():
    """Exact equality can never match `claude-code`, `anthropic/claudeai` or a
    versioned `Cursor/0.42` — which is precisely what post-fix attribution
    started emitting.
    """
    assert "any(t in lp for t in _CHANNEL_TOKENS)" in SRC


def test_registry_tokens_are_named_and_justified():
    assert '_REGISTRY_TOKENS = ("smithery", "glama")' in SRC
    block = SRC[SRC.index("# MCP registries/gateways"):SRC.index("_CHANNEL_TOKENS")]
    # Normalise: the justification is prose in wrapped YAML comments, so a
    # phrase can straddle a line break + "# " prefix. Assert on the meaning,
    # not on where the wrap happened to land.
    flat = " ".join(block.replace("#", " ").split())
    assert "how many AI VENDORS" in flat, "say why they are not in the canon"
    assert "biggest caller" in flat, "say why they belong here anyway"


def _known(label, tokens):
    lp = label.lower()
    return any(t in lp for t in tokens)


def test_no_channel_the_old_list_counted_is_lost():
    """A vocabulary fix that drops a live channel trades one silent zero for
    another. Smithery and Glama were in the old list and must survive.
    """
    old = ('claude', 'chatgpt', 'gemini', 'perplexity', 'grok', 'copilot', 'meta',
           'smitheryconnect', 'glama', 'cursor', 'opencode', 'deepseek',
           'anthropicapi', 'openai-mcp')
    tokens = tuple(canon.KNOWN_AI_TOKENS) + ("smithery", "glama")
    lost = [p for p in old if not _known(p, tokens)]
    assert lost == [], f"these channels would silently vanish: {lost}"


def test_the_labels_that_caused_the_incident_are_recovered():
    tokens = tuple(canon.KNOWN_AI_TOKENS) + ("smithery", "glama")
    for label in ("claude-code", "claude-desktop", "anthropic/claudeai",
                  "Cursor/0.42", "windsurf", "cline", "mistral"):
        assert _known(label, tokens), f"{label} still dropped"


def test_noise_is_still_ignored():
    """The canon is an allowlist for a reason — the audit/test long tail is
    unbounded and a denylist can never keep up.
    """
    tokens = tuple(canon.KNOWN_AI_TOKENS) + ("smithery", "glama")
    for junk in ("reviewer-sim", "Scraper-Block-Verify", "x", "qa-harness-7"):
        assert not _known(junk, tokens), f"{junk} should not count as a channel"


def test_unrecognised_labels_are_surfaced_not_swallowed():
    """THE anti-drift line. An unrecognised label is the only early warning
    that attribution changed shape. Without it, a relabelled platform is
    indistinguishable from a dead channel — which is exactly how a fix read as
    a collapse for a week.
    """
    assert "unknown" in SRC
    assert "unrecognised platform strings" in SRC
    assert "look here first" in SRC


def test_the_query_no_longer_filters_in_sql():
    """Classification moved to Python so the UNRECOGNISED rows still come back.
    Filtering in SQL is what made the dropped labels invisible.
    """
    assert "COALESCE(platform,'') <> ''" in SRC
    assert re.search(r"all_rows\s*=\s*cur\.fetchall\(\)", SRC)
