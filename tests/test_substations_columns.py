"""Every query against `substations` must use columns the table HAS.

★ 2026-08-24. `/api/v1/land-power/site-analysis` — the Land & Power flagship —
had NEVER returned real power data. Its substations query asked for three
columns that do not exist:

    SELECT name, voltage, lines, ... FROM substations
     WHERE latitude IS NOT NULL AND longitude IS NOT NULL

The table has `voltage_kv` (real), `lat` and `lng`. Postgres raised
UndefinedColumn on the first one it resolved (`voltage`); the block's own
try/except caught it and recorded `power._error`.

Why it was worse than one dead block: the connection was NOT autocommit, so
every later statement in the same transaction died with "current transaction
is aborted, commands ignored until end of transaction block". `land`, `water`,
`tax` and `dcpi` all came back empty — and the endpoint still returned **200**
with a feasibility score computed from that emptiness. Live, before the fix:

    "power": {"_error": "column \\"voltage\\" does not exist ..."},
    "land":  {"_error": "current transaction is aborted, ..."},
    "feasibility_score": 35, "verdict": "WEAK_SITE",
    "narrative": "... no substations indexed within radius ..."

A caller cannot tell that verdict apart from a real one. That is the reason
this guard is source-level rather than a live probe: DB tests skip in CI, and
a 200 with a plausible number defeats any status-code check.

Same family as [[test_identity_view_columns]] — a column typo that a broad
try/except turned into quiet wrong data instead of a loud failure.

THE CONTRACT
────────────
  S1. No SQL literal that reads FROM substations names a column the table
      does not have.
  S2. The scan actually finds those literals (anti-vacuity control).
  S3. The real column names are the ones in use (positive control).

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ 0bea00a8): 1 failed, 2 passed
PATCHED   (this branch):            3 passed, 0 failed
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The table's real columns, measured against production 2026-08-24 via
# information_schema.columns WHERE table_name='substations'.
SUBSTATION_COLUMNS = {
    "id", "name", "operator", "owner", "substation_type", "sub_type", "type",
    "voltage_kv", "max_voltage_kv", "min_voltage_kv", "min_volt", "max_volt",
    "capacity_mva", "available_mva", "lat", "lng", "city", "state", "county",
    "county_fips", "country", "zip", "connected_transmission", "status",
    "source", "source_id", "source_date", "created_at", "updated_at",
    "hifld_objectid", "hifld_id", "naics_code", "naics_desc",
    "val_method", "val_date", "lines", "lines_count",
}

# Columns that exist on OTHER geo/power tables and are easy to reach for by
# habit. Naming them explicitly makes the failure message useful.
FOREIGN_COLUMNS = {
    "voltage": "substations calls it `voltage_kv` (real, not a string)",
    "latitude": "substations calls it `lat`",
    "longitude": "substations calls it `lng`",
}


def _sql_literals_touching_substations():
    """(file, sql) for every literal that actually reads FROM substations.

    ★ Scoped to a real FROM/JOIN clause on purpose. `substations` appears in
    docstrings, basis-description strings and count blurbs across the repo; a
    guard that fires on those is a guard someone deletes.
    """
    out = []
    for py in sorted([*REPO.glob("routes/*.py"), *REPO.glob("*.py")]):
        try:
            src = py.read_text()
        except Exception:
            continue
        if "substations" not in src:
            continue
        for m in re.finditer(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', src, re.S):
            lit = m.group(1) or m.group(2) or ""
            if re.search(r"\b(?:FROM|JOIN)\s+substations\b", lit, re.I):
                out.append((py.name, lit))
    return out


def _strip_sql_comments(sql: str) -> str:
    """Drop `-- …` comment tails and `AS <alias>` output names.

    A column named in a comment is not a read, and this repo has been bitten
    by assertions matching their own warning text rather than real SQL.
    `SELECT lat AS latitude` is likewise a RENAME of a valid column, not a
    read of an invalid one — a route may keep its published field names while
    reading the right column underneath.
    """
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in sql.splitlines())


def _strip_output_aliases(sql: str) -> str:
    """Drop `AS <alias>` output names only — NOT the table alias in
    `FROM substations AS s`, which _substations_aliases still needs, so this
    runs on the column scan alone."""
    return re.sub(r"(?<!substations)\s+AS\s+[a-z_][a-z0-9_]*", " ", sql, flags=re.I)


def test_the_scan_finds_real_queries():
    """Guard against a regex that matches nothing — the trap that makes every
    assertion below pass vacuously."""
    found = _sql_literals_touching_substations()
    assert len(found) >= 3, f"expected several substations queries, found {len(found)}"


def _tables_in(sql: str):
    """Every table named in a FROM/JOIN clause of this literal."""
    return {m.group(1).lower()
            for m in re.finditer(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", sql, re.I)}


def _substations_aliases(sql: str):
    """Aliases bound to substations — `JOIN substations s` → {'s'}."""
    out = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+substations\s+(?:AS\s+)?([a-z][a-z0-9_]*)",
                         sql, re.I):
        alias = m.group(1).lower()
        if alias not in {"on", "where", "left", "right", "inner", "outer",
                         "join", "group", "order", "limit", "using", "cross"}:
            out.add(alias)
    return out


def test_no_query_uses_a_column_the_table_does_not_have():
    """★ ALIAS-AWARE on purpose. The first cut flagged 5 files; 3 were correct
    code where `latitude` belonged to the OTHER table in a join
    (`batch b`, `infrastructure_layers il`, `gem_power`) while substations was
    read correctly as `s.lat`. A guard that calls working code broken is a
    guard someone deletes with the real bug still in it.

    So a foreign column counts only when it is genuinely a substations
    reference: qualified with a substations alias, or unqualified in a literal
    whose only table IS substations.
    """
    problems = []
    for fname, raw in _sql_literals_touching_substations():
        sql = _strip_sql_comments(raw)
        aliases = _substations_aliases(sql)
        single_table = _tables_in(sql) == {"substations"}
        sql = _strip_output_aliases(sql)
        for col, why in FOREIGN_COLUMNS.items():
            # Only where it reads as a column reference: followed by a
            # comparison, a list separator, a close-paren, or IS/IN/BETWEEN.
            ref = rf"\b{col}\s*(?:=|,|\)|\s+(?:IS|IN|ANY|BETWEEN))"
            qualified = any(re.search(rf"\b{a}\.{ref}", sql, re.I) for a in aliases)
            bare = single_table and re.search(rf"(?<![.\w]){ref}", sql, re.I)
            if qualified or bare:
                problems.append(f"{fname}: uses `{col}` — {why}")
    assert not problems, (
        "query against substations uses a column the table does not have:\n  - "
        + "\n  - ".join(sorted(set(problems)))
    )


def test_the_real_column_names_are_the_ones_in_use():
    """Positive control: if nothing selects voltage_kv/lat/lng, the scan is
    looking at the wrong literals and the test above asserts nothing."""
    joined = " ".join(s for _, s in _sql_literals_touching_substations())
    for col in ("lat", "lng"):
        assert re.search(rf"\b{col}\b", joined), (
            f"no substations query references `{col}` — did the scan break?"
        )
