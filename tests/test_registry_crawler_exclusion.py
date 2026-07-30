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


# ── LIKE ↔ regex-twin parity (2026-07-30) ──────────────────────────────────
# The regex twin MISSED the entire r-registry-crawlers block for two days:
# Round 12 added nine families to the LIKE form only, so every bound-params
# caller (high-intent claims, signal canonical — and now the Agent Success
# Report's episode queries) kept counting registry crawlers while the identity
# view excluded them. Exactly the drift the twin's docstring said could not
# happen, reached by adding families to a predicate BODY instead of the shared
# constant. Both forms now render from the same constants; these tests probe
# the parity per family, so extending one form and not the other fails here
# instead of drifting silently.

def _regex_twin_parts():
    sql = dl.internal_tag_regex_predicate("platform")
    fams = _re.search(r"!~\* '\(([^']+)\)'", sql).group(1)
    exact = _re.search(r"!~ '\^\(([^']+)\)\$'", sql).group(1)
    return fams, {e.lower() for e in exact.split("|")}


def _regex_excluded(name: str) -> bool:
    """Python emulation of the regex twin's verdict — parsed from the rendered
    SQL (never transcribed), same discipline as _FRAGMENTS above."""
    fams, exact = _regex_twin_parts()
    p = (name or "").lower()
    if _re.search(fams, p, _re.I):
        return True
    if p in exact:
        return True
    return p != "" and len(p) <= 2


def test_regex_twin_carries_every_like_family():
    fams, _ = _regex_twin_parts()
    twin = set(fams.split("|"))
    missing = set(_FRAGMENTS) - twin
    assert not missing, (
        f"families in the LIKE form but not the regex twin: {sorted(missing)} — "
        "bound-params callers would keep counting what the identity view excludes")


def test_like_form_carries_every_regex_family():
    """Drift is drift in either direction."""
    fams, _ = _regex_twin_parts()
    missing = set(fams.split("|")) - set(_FRAGMENTS)
    assert not missing, \
        f"families in the regex twin but not the LIKE form: {sorted(missing)}"


@pytest.mark.parametrize("frag", sorted(set(_FRAGMENTS)))
def test_family_probes_excluded_by_both_forms(frag):
    """A synthetic name of each family shape must fall to BOTH predicates —
    this is the mutation that reveals a family added to only one body."""
    probe = f"zz-{frag}-zz"
    assert _excluded(probe), f"LIKE form missed {probe}"
    assert _regex_excluded(probe), f"regex twin missed {probe}"


@pytest.mark.parametrize("name", OBSERVED_CRAWLERS)
def test_observed_crawlers_excluded_by_regex_twin_too(name):
    assert _regex_excluded(name), f"{name} passes the bound-params surfaces"


@pytest.mark.parametrize(
    "name", REAL_PLATFORMS + list(dl._AMBIGUOUS_NOT_EXCLUDED) + [""])
def test_real_ambiguous_and_anonymous_kept_by_regex_twin(name):
    """The expensive direction, twin edition: the regex form must not exclude
    a single name the LIKE form keeps."""
    assert not _regex_excluded(name), f"{name} wrongly excluded by the regex twin"
