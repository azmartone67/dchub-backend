"""
ai_platform_canon.py — ONE definition of "an AI platform"
=========================================================
Data QA 2026-07-27.

THE BUG THIS FIXES
------------------
Two live endpoints disagreed about how many AI platforms query DC Hub:

    /api/v1/ai/reach        distinct_platforms = 15   (7-day rollup)
    /api/v1/stats/live-proof distinct_platforms = 10   (30-day window)

A 30-day window cannot contain FEWER platforms than a 7-day one, so one was
wrong. It was the 15: `ai_reach_rollup` counted `len(plats)` — every distinct
platform string, unfiltered — while `live-proof` counted only strings matching a
recognized-AI allowlist. The 15 for week 2026-07-20 was:

    mcp (1,931 reqs) · mcp-server-validator · smithery connect · unknown ·
    connectors-manager · reviewer-sim        <- six non-platforms
    anthropic/claudeai · claude-code · claude <- ONE vendor counted three times
    copilot · mistral · grok · cursor · gemini-cli · visual studio code

`mcp` is the protocol name and was the single largest bucket. `reviewer-sim` is
a test simulator. So the homepage's "15 AI platforms" counted our own test
harness and our own protocol as customers.

WHAT THIS MODULE DOES
---------------------
Two rules, applied by both endpoints so they can never diverge again:

  1. RECOGNITION — a platform string counts only if it matches a known-AI token.
     An allowlist, not a denylist: the audit/test long tail (reviewer-sim,
     Scraper-Block-Verify, single-char noise) is unbounded and a denylist can
     never keep up. A genuinely new platform is undercounted until it is added
     here, which is the safe direction to be wrong in.

  2. VENDOR COLLAPSE — `claude`, `claude-code`, `anthropic/claudeai` and
     `anthropicapi` are one vendor. Counting them separately inflates a
     "how many platforms" claim threefold for a single relationship.

Use `count_platforms()` for any published platform COUNT. Per-platform
breakdowns may keep the raw ids — the fragmentation only misleads when summed.
"""
from __future__ import annotations

# Recognized external AI platforms (substring match, lowercased).
#
# THE ONLY definition in the repo. flask_mcp_endpoints._LP_KNOWN_AI_TOKENS and
# agent_network_effect._KNOWN_AI_TOKENS are `import ... as` aliases of this
# tuple, enforced by tests/test_platform_canon_single_source.py.
#
# ★These three lines used to say "those now import from here" while both of
# them in fact held byte-identical literal copies (2026-07-27 -> 2026-09-05).
# The comment asserting the invariant WAS the whole enforcement, so the
# invariant quietly stopped holding and the file kept claiming it did. That is
# why there is now a test: a sentence cannot enforce anything.
KNOWN_AI_TOKENS = (
    "claude", "anthropic", "chatgpt", "openai", "gpt", "gemini", "bard",
    "copilot", "perplexity", "grok", "deepseek", "cursor", "cline",
    "windsurf", "mistral", "cohere", "llama", "meta", "nvidia", "groq",
    "huggingface", "phind", "you.com", "poe", "replit", "opencode",
    # ── 2026-09-05, measured, not guessed ──────────────────────────────────
    # `codex` appeared in /api/v1/ai/reach?period=30d per_platform (1 agent,
    # 5 requests) and canonical_platform() returned None for it: OpenAI's own
    # coding agent was calling the server and the published platform count
    # could not see it. It only stayed invisible because `chatgpt` happened to
    # be calling in the same window and already contributed the openai vendor
    # — so this was a live count that was RIGHT BY COINCIDENCE. The remaining
    # ids below are the other first-party agent clients that speak MCP and
    # would have landed in the same blind spot.
    "codex", "vscode", "visual studio code", "goose", "librechat",
    "openhands", "devin", "roocode", "kilocode",
)

# Vendor collapse. Order matters: the first matching token wins, so put the
# more specific token first where two could both match.
_VENDOR_ALIASES = (
    ("anthropic", "claude"),   # anthropic/claudeai, anthropicapi
    ("claude",    "claude"),   # claude, claude-code, claude-desktop
    ("chatgpt",   "openai"),
    ("openai",    "openai"),
    ("gpt",       "openai"),
    ("codex",     "openai"),   # OpenAI Codex collapses INTO openai, like claude-code
    ("visual studio code", "vscode"),   # before "vscode": both spellings, one vendor
    ("vscode",    "vscode"),
    ("goose",     "goose"),
    ("librechat", "librechat"),
    ("openhands", "openhands"),
    ("devin",     "devin"),
    ("roocode",   "roocode"),
    ("kilocode",  "kilocode"),
    ("bard",      "gemini"),
    ("gemini",    "gemini"),
    ("copilot",   "copilot"),
    ("perplexity", "perplexity"),
    ("grok",      "grok"),
    ("cursor",    "cursor"),
    ("cline",     "cline"),
    ("windsurf",  "windsurf"),
    ("mistral",   "mistral"),
    ("deepseek",  "deepseek"),
    ("cohere",    "cohere"),
    ("groq",      "groq"),
    ("llama",     "meta"),
    ("meta",      "meta"),
    ("nvidia",    "nvidia"),
    ("huggingface", "huggingface"),
    ("phind",     "phind"),
    ("you.com",   "you"),
    ("poe",       "poe"),
    ("replit",    "replit"),
    ("opencode",  "opencode"),
)


# ── CLIENT CLASS (2026-09-07) ────────────────────────────────────────────
# `canonical_platform` answers "which VENDOR is this" and returns None for
# everything else. Measured live on 2026-09-07, that None bucket was 345 of 577
# MCP requests in the window — 60% — published as a single undifferentiated
# `unrecognised_requests` count. A reader could not tell whether that was
# hidden adoption or expected tooling, and those imply opposite next moves.
#
# ★ THIS DOES NOT ASSIGN VENDORS, AND MUST NOT. Nothing here may be added to
# _VENDOR_ALIASES: that tuple feeds the published "N platforms calling MCP
# tools" headline, and promoting a verification harness or a generic client id
# into it would inflate the one number the site asks people to trust. The two
# classifications answer different questions and stay separate.
#
# ★ NAME-BASED, THEREFORE A HINT. A client id is self-declared — the caller
# chooses it — so this is weaker evidence than behaviour. What the DB actually
# showed for these ids on 2026-09-07 over 30d:
#
#   chain-hire                    search x1473, everything else x2   ONE tool
#   actionist-apps-verification   ~15 tools at 5-15 calls each, flat  SWEEP
#   connectors-manager            execute_plan 15, why_dchub 13, then
#                                 site_selection_canvas / rank_markets /
#                                 analyze_site — a VARIED real workload
#   mcp                           get_grid_scoreboard 6615 + a wide tail — BOTH
#
# So the names are not reliable: `connectors-manager` reads like plumbing and
# behaves like an agent doing work. That is why UNKNOWN is a real class here
# and not a synonym for "tooling" — treating unknown as tooling is how a
# genuine caller stops being counted. Anything not matched stays UNKNOWN, and
# UNKNOWN is the bucket that deserves investigation, never dismissal.
CLASS_ASSISTANT = "assistant"          # resolves to a vendor via canonical_platform
CLASS_VERIFIER  = "verifier_or_probe"  # conformance harness / listing checker
CLASS_REGISTRY  = "registry_or_tooling"  # a directory or SDK, not an end agent
CLASS_UNKNOWN   = "unknown"            # not classifiable — NOT a synonym for tooling

# Substring -> class. First match wins; keep the more specific token first.
_CLIENT_CLASS_TOKENS = (
    ("verification", CLASS_VERIFIER),
    ("verifier",     CLASS_VERIFIER),
    ("-sim",         CLASS_VERIFIER),   # reviewer-sim, registry-sim
    ("spec-study",   CLASS_VERIFIER),
    ("conformance",  CLASS_VERIFIER),
    ("smithery",     CLASS_REGISTRY),
    ("glama",        CLASS_REGISTRY),
    ("pulsemcp",     CLASS_REGISTRY),
    ("mcp.so",       CLASS_REGISTRY),
    ("toolplex",     CLASS_REGISTRY),
)


def client_class(platform: str | None) -> str:
    """Classify a raw client id: assistant / verifier / registry / unknown.

    Orthogonal to canonical_platform, and deliberately conservative — an id we
    cannot place is UNKNOWN, never assumed to be tooling. See the note above
    for why the name alone is only a hint.
    """
    if canonical_platform(platform):
        return CLASS_ASSISTANT
    if not platform:
        return CLASS_UNKNOWN
    p = platform.lower()
    for tok, cls in _CLIENT_CLASS_TOKENS:
        if tok in p:
            return cls
    return CLASS_UNKNOWN


def classify_clients(platforms) -> dict:
    """{class: count of DISTINCT client ids} over an iterable of raw ids."""
    seen, out = set(), {}
    for p in platforms:
        key = (p or "").lower()
        if key in seen:
            continue
        seen.add(key)
        c = client_class(p)
        out[c] = out.get(c, 0) + 1
    return out


def is_recognized(platform: str | None) -> bool:
    """True if `platform` names a known external AI platform."""
    if not platform:
        return False
    p = platform.lower()
    return any(tok in p for tok in KNOWN_AI_TOKENS)


def canonical_platform(platform: str | None) -> str | None:
    """Collapse a raw platform id to its vendor, or None if unrecognized.

    'anthropic/claudeai' -> 'claude'   'claude-code' -> 'claude'
    'gemini-cli'         -> 'gemini'   'mcp'         -> None
    'reviewer-sim'       -> None       'unknown'     -> None
    """
    if not platform:
        return None
    p = platform.lower()
    for tok, vendor in _VENDOR_ALIASES:
        if tok in p:
            return vendor
    return None


def count_platforms(platforms) -> int:
    """Distinct recognized VENDORS in an iterable of raw platform ids.

    This is the only correct way to produce a published "N AI platforms"
    number. Counting raw distinct strings both admits non-platforms and
    multiple-counts a single vendor.
    """
    return len({c for c in (canonical_platform(p) for p in platforms) if c})
