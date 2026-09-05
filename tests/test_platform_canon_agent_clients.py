"""The platform canon must see first-party agent clients, and must still
refuse registries, routers and our own harnesses.

WHY THIS EXISTS (2026-09-05). A live read of /api/v1/ai/reach?period=30d
returned `codex` in per_platform — OpenAI's coding agent, 1 agent / 5 requests
— and `canonical_platform("codex")` returned None. The published
`distinct_platforms` was still 3 and still CORRECT, but only because `chatgpt`
was calling in the same window and already contributed the `openai` vendor. A
count that is right by coincidence is the defect: had ChatGPT gone quiet,
OpenAI would have been reported as absent while its agent was calling.

The canon's stated design bias is to UNDER-count ("a genuinely new platform is
undercounted until it is added here, which is the safe direction to be wrong
in"). That bias is only safe if somebody periodically closes the gap, and only
honest if the exclusions stay exclusions. Both halves are asserted here.
"""
import pytest

import ai_platform_canon as canon


# Every id here was OBSERVED in per_platform, or is a first-party agent client
# that speaks MCP. Each must resolve to the vendor named beside it.
RECOGNISED = {
    "codex": "openai",                 # ← the measured miss this test exists for
    "chatgpt": "openai",
    "anthropic/claudeai": "claude",
    "claude-code": "claude",
    "claude-ai": "claude",
    "anthropic/api": "claude",
    "anthropic/toolbox": "claude",
    "hub-grok-bot": "grok",
    "gemini-cli": "gemini",
    "vscode": "vscode",
    "visual studio code": "vscode",
    "goose": "goose",
    "librechat": "librechat",
    "openhands": "openhands",
    "devin": "devin",
    "roocode": "roocode",
    "kilocode": "kilocode",
}

# Observed in per_platform and deliberately NOT platforms. Three classes:
# the protocol itself, MCP registries/routers (real distribution, but they are
# not the AI), and our own test harnesses. Admitting any of these is the exact
# "15 AI platforms" inflation the canon was written to kill.
NOT_PLATFORMS = [
    "mcp", "mcp-generic-client", "mcp-server-validator", "mcp-spec-study",
    "connectors-manager", "smithery connect", "mcphub", "toolrouter",
    "glama-user-sim", "reviewer-sim", "skeptic-verifier", "robinsaige-verifier",
    "chain-hire", "actionist-apps-verification", "vouch-census",
    "fabrique-c3-idempotency", "northwind-agent", "datacolo", "unknown", "",
    None,
]


@pytest.mark.parametrize("raw,vendor", sorted(RECOGNISED.items()))
def test_recognised_client_maps_to_its_vendor(raw, vendor):
    assert canon.is_recognized(raw), f"{raw!r} is not recognised by the canon"
    assert canon.canonical_platform(raw) == vendor, (
        f"{raw!r} collapsed to {canon.canonical_platform(raw)!r}, expected {vendor!r}")


@pytest.mark.parametrize("raw", NOT_PLATFORMS)
def test_non_platform_is_still_refused(raw):
    assert canon.canonical_platform(raw) is None, (
        f"{raw!r} is now counted as an AI platform — a registry, router, "
        f"protocol name or test harness must never reach a published count")


def test_codex_does_not_inflate_the_count_it_de_risks_it():
    """Codex collapses INTO openai, exactly as claude-code collapses into claude.

    Stated so nobody 'fixes' this by giving Codex its own vendor to make the
    headline bigger: adding it must leave a window that already has ChatGPT
    UNCHANGED, and must rescue a window that has Codex alone.
    """
    with_both = canon.count_platforms(["chatgpt", "codex", "claude"])
    with_chatgpt_only = canon.count_platforms(["chatgpt", "claude"])
    assert with_both == with_chatgpt_only == 2

    # The case the miss would have broken: OpenAI present, ChatGPT silent.
    assert canon.count_platforms(["codex", "claude"]) == 2


def test_no_new_token_admits_an_observed_non_platform():
    """A substring allowlist can admit junk by accident — assert it did not.

    Short tokens ('zed', 'roo', 'warp') were deliberately NOT added for this
    reason; this asserts the ones that WERE added stay clean against the real
    unrecognised population.
    """
    admitted = [p for p in NOT_PLATFORMS if p and canon.is_recognized(p)]
    assert admitted == [], f"new tokens admitted non-platforms: {admitted}"
