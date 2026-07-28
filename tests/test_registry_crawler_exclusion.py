"""Guards for the registry-crawler exclusion (r-registry-crawlers, 2026-07-28).

A live audit of ~4.5h of gateway `[MCP] init` logs found the "real agent"
population dominated by MCP registry crawlers, indexers, scorers and health
checkers — every one of which passed `is_real_external`, because the synthetic
filter is a PREFIX match and none of them start with dchub-/qa-/probe-/monitor-
(`yellowmcp-health` CONTAINS "health" but does not START with "healthcheck").

This file is deliberately two-sided. Excluding traffic makes our own headline
numbers smaller, which is the safe direction; wrongly excluding a REAL platform
would silently delete a customer to make a metric prettier, which is not. So the
second half matters more than the first.

Pure functions: no DB, no network, never imports main.
"""
import pytest

dl = pytest.importorskip("mcp_calls_deloop")

# ★ DERIVED from the predicate, never transcribed. A hardcoded copy of these
# fragments drifts the moment someone adds a family — which happened within
# minutes of the first version of this file: `%validator%` was added to the
# module and every test here kept passing against the stale list. Read the
# contract, do not restate it.
import re as _re

_FRAGMENTS = tuple(_re.findall(r"NOT LIKE '%([a-z]+)%'",
                               dl.external_platform_predicate()))


def _excluded(name: str) -> bool:
    p = (name or "").lower()
    if any(f in p for f in _FRAGMENTS):
        return True
    if p in {v.lower() for v in dl.INTERNAL_PLATFORM_VALUES}:
        return True
    return p != "" and len(p) <= 2


OBSERVED_CRAWLERS = [
    "mcp-gateway-registry", "catalog-sync", "yellowmcp-health", "manifest-mirror",
    "watchdog", "agentpulse", "mcpscoringengine", "mcpqueen-grader",
    "mcpexplorerbot", "mcpindex-trust", "mcp-rugpull-research",
]

REAL_PLATFORMS = [
    "claude", "claude-code", "anthropic/claudeai", "chatgpt", "gemini",
    "perplexity", "grok", "mistral", "copilot", "cursor", "cline", "windsurf",
    "codex", "visualstudiocode", "deepseek", "kimi",
]


@pytest.mark.parametrize("name", OBSERVED_CRAWLERS)
def test_observed_crawlers_are_excluded(name):
    assert _excluded(name), f"{name} still counts as a real agent"


@pytest.mark.parametrize("name", REAL_PLATFORMS)
def test_real_platforms_are_never_excluded(name):
    """The expensive direction. A false exclusion deletes a real customer."""
    assert not _excluded(name), f"{name} was wrongly excluded — this deletes real agents"


@pytest.mark.parametrize("name", list(dl._AMBIGUOUS_NOT_EXCLUDED))
def test_ambiguous_registries_stay_counted(name):
    """smithery/glama proxy real end-user traffic; agent-toolscloud presented an
    API key. Ambiguity resolves toward KEEPING them: a slightly generous count we
    can see and name beats a silent deletion we cannot."""
    assert not _excluded(name), (
        f"{name} is documented as ambiguous-but-kept — excluding it needs "
        "evidence it carries no real users, not a tidier list"
    )


def test_anonymous_platform_is_kept():
    """Real agents frequently have no platform tag at all. Empty must survive —
    that escape is why the length<=2 rule cannot swallow it."""
    assert not _excluded("")


def test_predicate_sql_carries_the_new_families():
    sql = dl.external_platform_predicate()
    for frag in ("registry", "catalog", "mirror", "watchdog",
                 "health", "crawler", "scanner", "uptime"):
        assert f"'%{frag}%'" in sql, f"predicate lost the {frag} family"
    assert "= '' OR LENGTH" in sql, "the empty-platform escape was dropped"


def test_predicate_is_reusable_on_another_column():
    """Other tables reuse this verdict on their own tag column rather than
    keeping a second hand-maintained list — that reuse must keep working."""
    sql = dl.external_platform_predicate("mcp_client")
    assert "mcp_client" in sql and "registry" in sql


def test_validator_family_is_covered():
    """Found by re-reading the LIVE reach payload after shipping the first
    families: `mcp-server-validator` (3 agents / 156 calls) matched none of
    them. The list is derived from production, not from imagination."""
    sql = dl.external_platform_predicate()
    assert "'%validator%'" in sql
    assert _excluded("mcp-server-validator")
