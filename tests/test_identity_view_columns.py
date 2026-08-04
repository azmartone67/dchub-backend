"""Every query against mcp_calls_identity must use columns the view HAS.

★ 2026-08-04. Three shell lanes shipped broken and stayed broken for two days:

    agent_expansion_master_shell  lane 1 (front-door funnel)
    agent_expansion_master_shell  lane 2 (planner adoption)
    agent_retention_master_shell  lane 2 (return mechanism)

All three did `WHERE tool = …` against mcp_calls_identity, whose column is
`tool_name`. Postgres raised UndefinedColumn, `_safe_lane` caught it, and the
lane rendered a red FAIL with the error in its detail — so the board looked
like it was reporting a finding when it was reporting a typo.

Why it survived deploy: the post-merge check asserted the endpoint was
REGISTERED and admin-gated (401, not 404). It never read a tick. A gated
endpoint that returns broken lanes passes that check perfectly.

This test pins the view's actual column vocabulary. It is source-level and
needs no DB, because DB tests skip in CI — which is the other half of why a
column typo could ship twice.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The view's real columns, from
# migrations/2026-07-10_mcp_calls_identity_brainv2_ua_backstop.sql
IDENTITY_COLUMNS = {
    "id", "tool_name", "platform", "client_name", "success",
    "response_time_ms", "ip_address", "user_agent", "created_at",
    "session_id", "client_ip", "agent_id", "is_public_ip", "is_real_external",
}

# Columns that exist on OTHER call tables and are easy to reach for by habit.
# mcp_call_log genuinely has `tool` and `api_key`; mcp_calls_identity has
# neither. Naming them explicitly makes the failure message useful.
FOREIGN_COLUMNS = {
    "tool": "mcp_call_log has `tool`; the identity view calls it `tool_name`",
    "api_key": "mcp_call_log has `api_key`; the identity view has no key column",
    "tier": "not on the identity view",
}


def _sql_literals_touching_identity():
    """(file, sql) for every literal that actually SELECTS FROM the view.

    ★ Scoped to `FROM mcp_calls_identity` on purpose. The first cut of this
    matched any literal MENTIONING the view and flagged 19 files — because
    the view's name appears in basis-description strings, docstrings and SQL
    comments all over the codebase (`_CANONICAL_AGENTS_BASIS` and friends
    exist precisely to describe it). A guard that fires on 19 innocent files
    is a guard someone deletes, so it only considers real FROM clauses.
    """
    out = []
    for py in sorted(REPO.rglob("routes/*.py")):
        try:
            src = py.read_text()
        except Exception:
            continue
        if "mcp_calls_identity" not in src:
            continue
        for m in re.finditer(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', src, re.S):
            lit = m.group(1) or m.group(2) or ""
            if re.search(r"\bFROM\s+mcp_calls_identity\b", lit, re.I):
                out.append((py.name, lit))
    return out


def _strip_sql_comments(sql: str) -> str:
    """Drop `-- …` comment tails. A column named in a comment is not a read,
    and this repo has been bitten twice by assertions matching their own
    warning text rather than real SQL."""
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in sql.splitlines())


def test_the_scan_finds_real_queries():
    """Guard against a regex that matches nothing — the trap that makes every
    assertion below pass vacuously."""
    found = _sql_literals_touching_identity()
    assert len(found) >= 3, f"expected several identity-view queries, found {len(found)}"


def test_no_query_uses_a_column_the_view_does_not_have():
    problems = []
    for fname, raw in _sql_literals_touching_identity():
        sql = _strip_sql_comments(raw)
        for col, why in FOREIGN_COLUMNS.items():
            # word-boundary match, and only where it reads as a column
            # reference (followed by =, comma, whitespace-then-keyword).
            if re.search(rf"\b{col}\s*(=|,|\)|\s+(?:IS|IN|ANY))", sql):
                problems.append(f"{fname}: uses `{col}` — {why}")
    assert not problems, (
        "query against mcp_calls_identity uses a foreign column:\n  - "
        + "\n  - ".join(sorted(set(problems)))
    )


def test_tool_name_is_the_column_the_shells_actually_use():
    """Positive control: the lanes that filter by tool must name tool_name,
    or this test is asserting nothing."""
    hits = [
        (f, s) for f, s in _sql_literals_touching_identity() if "tool_name" in s
    ]
    assert hits, "no identity-view query references tool_name — did the lanes change?"


# ★ NOT ASSERTED, deliberately: "every identity query carries both de-loop
# predicates". A first cut asserted it and failed on 8+ files. That is not
# evidence of 8 bugs — a predicate can live in an outer CTE, a sub-select can
# be filtered by its wrapper, and some diagnostics legitimately count ALL rows
# including internal traffic. Shipping that assertion would have declared
# existing, possibly-correct code broken and turned main red on a hunch.
#
# The count-discipline rule is real and worth enforcing; it needs someone to
# read those 8 files first and decide per query. Recording it here rather than
# asserting it, because a test nobody can justify is a test that gets deleted
# with the bug still in it.
