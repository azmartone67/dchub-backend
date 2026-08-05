"""Shell queries must name tables and columns that actually exist.

★ 2026-08-04. Four separate name-guesses shipped in these two shells, each
one rendering a confident FAIL or UNMEASURED that was really a typo:

  tool                -> tool_name              (mcp_calls_identity)   3 lanes
  partner_keys        -> partner_keys_issued    (70 rows, 30 active!)  lane 4
  agents              -> distinct_external_ips  (reach_weekly)         lane 4
  created_at          -> timestamp              (mcp_call_log)         while fixing

Every one produced the same failure shape: Postgres raised, `_safe_lane`
caught it, and the board published the exception text as if it were a
finding. The partner-keys one is the worst — it told the operator for two
days that a program with 70 issued keys across 32 partners had never issued
its first.

This pins a VERIFIED SNAPSHOT of the names these shells depend on, taken
2026-08-04 against production via information_schema. It is not live
introspection — DB tests skip in CI, which is exactly why four typos shipped
— so treat it as a changelog: when the schema genuinely moves, update this
file IN THE SAME CHANGE and the diff records what moved and when.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHELLS = [
    REPO / "routes" / "agent_expansion_master_shell.py",
    REPO / "routes" / "agent_retention_master_shell.py",
]

# Verified 2026-08-04 against production (information_schema).
VERIFIED_TABLES = {
    "agent_expansion_doors", "linkedin_posts", "mcp_calls_identity",
    "partner_keys_issued", "ai_cumulative", "mcp_connections",
    "reach_weekly", "mcp_call_log",
}

# Names that LOOK right, do not exist, and have already cost us a red board.
KNOWN_WRONG = {
    "partner_keys": "the table is partner_keys_issued (70 rows, 30 active)",
    "mcp_calls": "the view is mcp_calls_identity",
}

# column -> (table it does NOT belong to, the right one)
KNOWN_WRONG_COLUMNS = {
    r"\bSELECT\s+agents\b|\bMAX\(agents\)": "reach_weekly has distinct_external_ips, not agents",
    r"\btool\s*=\s*(?:ANY|')": "mcp_calls_identity has tool_name; only mcp_call_log has tool",
}


def _sql_text(path: Path) -> str:
    """Source with comments stripped — assertions must not match the prose
    that explains the bug (three recorded instances of exactly that)."""
    src = path.read_text()
    src = "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in src.splitlines())


def test_the_shell_files_are_readable_and_query_something():
    """Vacuity guard: if this scan found nothing, every assertion below would
    pass on an empty string."""
    for p in SHELLS:
        assert p.exists(), p
        assert "FROM " in _sql_text(p), f"{p.name} has no queries?"


def test_no_shell_references_a_table_that_does_not_exist():
    problems = []
    for p in SHELLS:
        sql = _sql_text(p)
        for bad, why in KNOWN_WRONG.items():
            # ★ Must cover EVERY way a table name reaches Postgres here.
            # A first cut checked only FROM/JOIN and to_regclass(...) — and the
            # must-fail control PASSED, because the real bug was written as
            # `_table_exists(cur, "partner_keys")`, a helper the pattern could
            # not see. A guard blind to the exact shape of the bug it was
            # written for is worse than no guard: it certifies the defect.
            if (re.search(rf"\b(?:FROM|JOIN)\s+{bad}\b", sql)
                    or re.search(rf"to_regclass\(\s*['\"]{bad}['\"]", sql)
                    or re.search(rf"_table_exists\([^)]*['\"]{bad}['\"]", sql)
                    or re.search(rf"table_name\s*=\s*['\"]{bad}['\"]", sql)):
                problems.append(f"{p.name}: references `{bad}` — {why}")
    assert not problems, "shell queries a non-existent table:\n  - " + "\n  - ".join(problems)


def test_no_shell_uses_a_column_from_the_wrong_table():
    problems = []
    for p in SHELLS:
        sql = _sql_text(p)
        for pat, why in KNOWN_WRONG_COLUMNS.items():
            if re.search(pat, sql):
                problems.append(f"{p.name}: {why}")
    assert not problems, "wrong column for the table:\n  - " + "\n  - ".join(problems)


def test_every_table_named_is_one_we_verified():
    """Catches a NEW table name appearing without anyone checking it exists.
    If a shell legitimately needs a new table, verify it against
    information_schema and add it here — that is the whole point."""
    unknown = set()
    for p in SHELLS:
        # ★ Scan only SQL-looking literals. A first cut scanned the whole file
        # and flagged the table `the`, matched from docstring prose ("derived
        # FROM the ..."). Prose is not a query; requiring SELECT in the same
        # literal is what separates them.
        for lit in re.findall(r'"""(.*?)"""', _sql_text(p), re.S):
            if "SELECT" not in lit:
                continue
            for m in re.finditer(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", lit):
                t = m.group(1)
                # CTE aliases and information_schema are not base tables
                    # CTE names/aliases, not base tables. Explicit on purpose:
                # a real table typo must not be waved through by a loose rule,
                # so every entry is a CTE actually defined in these files.
                if t in {"firsts", "cur", "prev", "per", "information_schema",
                         "t", "u", "ours", "called"}:
                    continue
                if t not in VERIFIED_TABLES:
                    unknown.add(f"{p.name}: {t}")
    assert not unknown, (
        "unverified table name — check information_schema and add it to "
        "VERIFIED_TABLES in the same change:\n  - " + "\n  - ".join(sorted(unknown))
    )


def test_partner_lane_measures_activation_not_just_issuance():
    """The old lane asked "has a key been issued?" — answered yes, 70 times,
    since May. The useful question is whether they are USED."""
    sql = _sql_text(SHELLS[0])
    assert "partner_keys_issued" in sql
    assert "mcp_call_log" in sql, "activation needs the call join"
    assert "key_prefix" in sql, "key_prefix is the join handle"
    assert "l.timestamp" in sql, "mcp_call_log has `timestamp`, not created_at"


def test_crawler_silence_only_alarms_on_real_platforms():
    """It used a denylist and screamed about registry probes (glama,
    fabrique-noauth-probe, chiark-prober, yellowmcp-health) that were never AI
    platforms. A lane that always fails is a lane nobody reads."""
    sql = _sql_text(SHELLS[1])
    assert "AI_PLATFORMS" in sql, "filter to the curated roster, imported"
    assert "platform = ANY(%s)" in sql
    assert "unknown_ai','seo_bot'" not in sql, "the leaky denylist should be gone"
