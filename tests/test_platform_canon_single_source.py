"""ai_platform_canon.KNOWN_AI_TOKENS must be the ONLY copy.

WHY (2026-09-05). ai_platform_canon's header said "Mirrors
flask_mcp_endpoints._LP_KNOWN_AI_TOKENS and agent_network_effect.
_KNOWN_AI_TOKENS — those now import from here" from 2026-07-27. It was not
true: both modules held byte-identical literal tuples. Nothing tested the
claim, so the claim was the enforcement, and the enforcement was a sentence.

The cost was not theoretical. `distinct_platforms` on /api/v1/stats/live-proof
derives from canon.count_platforms(); `platforms_30d` in the SAME payload is
filtered by flask's private copy. Adding one token to the canon therefore made
that payload contradict itself — the platform counted in the headline and
missing from the list beside it.
"""
import ast
import pathlib
import re

import pytest

import agent_network_effect
import ai_platform_canon

REPO = pathlib.Path(__file__).resolve().parent.parent
# The first three entries of the tuple, which no other construct in the repo
# shares. Matching on content (not on a variable name) is the point: a fourth
# copy under a NEW name is exactly the failure this catches.
FINGERPRINT = re.compile(r'"claude",\s*"anthropic",\s*"chatgpt"')


def _production_py():
    for f in REPO.rglob("*.py"):
        rel = f.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/", "node_modules/")):
            continue
        if "__pycache__" in rel:
            continue
        yield rel, f


def test_exactly_one_literal_definition_in_the_repo():
    hits = [rel for rel, f in _production_py()
            if FINGERPRINT.search(f.read_text(encoding="utf-8", errors="replace"))]
    assert hits == ["ai_platform_canon.py"], (
        "the recognised-platform token list must exist in exactly one place; "
        f"found literal copies in: {hits}. Import "
        "ai_platform_canon.KNOWN_AI_TOKENS instead of restating it.")


def test_agent_network_effect_aliases_the_canon():
    assert agent_network_effect._KNOWN_AI_TOKENS is ai_platform_canon.KNOWN_AI_TOKENS


def test_flask_endpoint_aliases_the_canon():
    """flask_mcp_endpoints needs NEON_DATABASE_URL to import, so this reads the
    binding out of the AST rather than skipping — a DB-gated import must not be
    the reason a single-source invariant goes unchecked in CI."""
    src = (REPO / "flask_mcp_endpoints.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    aliases = [
        a.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ai_platform_canon"
        for a in node.names
        if a.name == "KNOWN_AI_TOKENS"
    ]
    assert "_LP_KNOWN_AI_TOKENS" in aliases, (
        "_LP_KNOWN_AI_TOKENS is not bound to ai_platform_canon.KNOWN_AI_TOKENS")


@pytest.mark.parametrize("raw", ["codex", "chatgpt", "claude-code", "gemini-cli"])
def test_a_canon_token_reaches_every_consumer(raw):
    """The property the aliasing exists to give: one edit, all three agree."""
    assert ai_platform_canon.is_recognized(raw)
    assert agent_network_effect._is_recognized_platform(raw)
