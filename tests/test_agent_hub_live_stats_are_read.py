"""agent_hub.get_live_stats must READ its figures, never invent them.

get_live_stats() ran four queries. The second and third could not execute
against the live Neon Postgres:

  · `FROM announcements WHERE timestamp > datetime("now", "-7 days")` —
    `announcements` has no `timestamp` column, and datetime() is SQLite.
    Note the DOUBLE quotes: db_utils.SQLITE_TO_PG_FUNC rewrites eight
    single-quoted datetime() spellings on the way to psycopg2, so most
    sites in this tree survive. This one was not in that dict and would
    have raised UndefinedColumn even if it had been.
  · `SELECT SUM(mw) FROM capacity_pipeline WHERE status != "cancelled"` —
    `capacity_pipeline` has no `mw` column (it is `capacity_mw`), and in
    Postgres "cancelled" is a quoted IDENTIFIER, not a string literal.

The first failure jumped to the handler, which returned

    {'facilities': 9603, 'recent_news': 0, 'pipeline_mw': 7194, 'deals': 100}

so EVERY call returned those constants. They reached prospects: the sales
agent injects them into its Claude prompt under the header "LIVE PLATFORM
DATA (use these real numbers)". Measured against the read replica on
2026-08-17 the true figures were 21,897 facilities, 550,081 MW of
CP_OK-publishable pipeline and 2,039 DEALS_OK-publishable deals — the
served numbers understated them by 2.3x, 76x and 20x. canonical_stats.py
had already named this helper's hardcoded 9,603 as a root cause of
published-figure drift; it was still being served.

WHAT THESE GUARDS PIN
  1. get_live_stats names only columns the live tables actually have.
  2. Its failure path returns no invented figures — a caller must be able
     to tell "unknown" from "9,603", which is exactly what the old
     handler destroyed.
  3. THE CLASS — capacity_pipeline and deals reads in this module go
     through the canonical CP_OK / DEALS_OK quarantine guards rather than
     a hand-rolled predicate, and no SQL in the module calls datetime().

The live column sets are PINNED constants measured against the Neon read
replica on 2026-08-17; CI runs with no DATABASE_URL and must not need
one. Every helper asserts it FOUND its target first — an empty scan
satisfies every "not in" below. Nothing runs at module scope.
"""

import ast
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_HUB = os.path.join(_REPO, "agent_hub.py")

# Measured on the Neon read replica, 2026-08-17. Adding a column here is a
# schema decision, not a refactor.
ANNOUNCEMENTS_COLS = {
    "id", "title", "summary", "content", "source", "source_url", "published_date",
    "announcement_type", "companies", "locations", "power_mw", "investment_usd",
    "sqft", "expected_completion", "confidence", "processed_at", "url",
    "discovered_at", "facility_processed", "facility_extracted_id", "category",
    "image_url", "categories", "published_at",
}
CAPACITY_PIPELINE_COLS = {
    "id", "operator", "market", "region", "capacity_mw", "phase", "status",
    "announcement_date", "completion_date", "source", "source_url", "notes",
    "created_at", "confidence_score", "confidence_label", "country",
    "source_announcement_id", "extraction_confidence", "extracted_via",
    "extracted_at", "data_flag",
}

# The exact constants the dead handler served.
INVENTED = ("9603", "7194")


def _source():
    with open(_AGENT_HUB, encoding="utf-8") as fh:
        return fh.read()


def _func(name):
    """Return the source segment of a top-level function, asserting it exists."""
    src = _source()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    pytest.fail(f"agent_hub.{name} not found — this guard scanned nothing")


def _sql_strings(func_src):
    """Every string constant in the function that looks like SQL."""
    tree = ast.parse(func_src)
    # Constants that belong to an f-string are reported again by ast.walk on
    # their own. Adjacent literals are merged by the parser, so the bare
    # fragment of "…WHERE {CP_OK} AND…" is the guard-defeating string
    # "…WHERE ". Collect the f-strings' own constants and skip them.
    inner = {id(p) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
             for p in ast.walk(n) if isinstance(p, ast.Constant)}

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in inner:
                continue
            if re.search(r"\bSELECT\b", node.value, re.IGNORECASE):
                out.append(node.value)
        # f-strings: the SQL is split across JoinedStr parts. Keep the
        # interpolated expression's SOURCE (e.g. "CP_OK") rather than dropping
        # it — dropping it turns "WHERE {CP_OK} AND ..." into "WHERE  AND ...",
        # which reads as an unguarded query that isn't one.
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for p in node.values:
                if isinstance(p, ast.Constant) and isinstance(p.value, str):
                    parts.append(p.value)
                elif isinstance(p, ast.FormattedValue):
                    parts.append(ast.unparse(p.value))
            joined = "".join(parts)
            if re.search(r"\bSELECT\b", joined, re.IGNORECASE):
                out.append(joined)
    return out


def _docstring_ids(tree):
    """ids of string Constants that are docstrings, not code.

    Needed because this guard's own prose names the very SQL it forbids; a
    line-level regex flags the explanation as if it were the offence.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _executed_sql(tree):
    """Every string constant that is SQL and is NOT a docstring or comment."""
    skip = _docstring_ids(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip \
                and re.search(r"\bSELECT\b", node.value, re.IGNORECASE):
            out.append((getattr(node, "lineno", 0), node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for p in node.values:
                if isinstance(p, ast.Constant) and isinstance(p.value, str):
                    parts.append(p.value)
                elif isinstance(p, ast.FormattedValue):
                    parts.append(ast.unparse(p.value))
            joined = "".join(parts)
            if re.search(r"\bSELECT\b", joined, re.IGNORECASE):
                out.append((getattr(node, "lineno", 0), joined))
    return out


def _code_numbers(node):
    """Numeric literals reachable in CODE — comments and docstrings excluded.

    Keying on the AST rather than on the text matters: this file's own
    explanatory comments name 9603 and 7194, and a substring scan flags them.
    """
    nums = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, (int, float)):
            nums.add(str(sub.value))
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            nums.update(re.findall(r"\d+", sub.value))
    return nums


def test_get_live_stats_names_only_real_columns():
    """The two queries that could never execute must name live columns."""
    sql = _sql_strings(_func("get_live_stats"))
    assert sql, "found no SELECT in get_live_stats — guard scanned nothing"

    ann = [s for s in sql if re.search(r"\bFROM\s+announcements\b", s, re.IGNORECASE)]
    cap = [s for s in sql if re.search(r"\bFROM\s+capacity_pipeline\b", s, re.IGNORECASE)]
    assert ann, "no announcements read in get_live_stats — guard scanned nothing"
    assert cap, "no capacity_pipeline read in get_live_stats — guard scanned nothing"

    for stmt in ann:
        for col in re.findall(r"\b(\w+)\s*::timestamptz", stmt):
            assert col in ANNOUNCEMENTS_COLS, (
                f"get_live_stats reads announcements.{col}, which the live table "
                f"does not have: {stmt!r}")
        assert not re.search(r"\btimestamp\s*[><=]", stmt), (
            "announcements has no `timestamp` column — this is the original bug")

    for stmt in cap:
        assert not re.search(r"\bSUM\(\s*mw\s*\)", stmt, re.IGNORECASE), (
            "capacity_pipeline has no `mw` column; it is capacity_mw")
        for col in re.findall(r"\bSUM\(\s*(\w+)\s*\)", stmt, re.IGNORECASE):
            assert col in CAPACITY_PIPELINE_COLS, (
                f"get_live_stats sums capacity_pipeline.{col}, which does not exist")


def test_get_live_stats_failure_path_invents_nothing():
    """A failed read must be reported as unknown, not as a plausible number."""
    src = _func("get_live_stats")
    handlers = [h for h in ast.walk(ast.parse(src)) if isinstance(h, ast.ExceptHandler)]
    assert handlers, "get_live_stats has no except handler — guard scanned nothing"

    for h in handlers:
        served = _code_numbers(h)
        leaked = served.intersection(INVENTED)
        assert not leaked, (
            f"get_live_stats's failure path still returns the invented figure(s) "
            f"{sorted(leaked)}. Callers cannot distinguish those from a real read, "
            f"which is how 9,603 facilities reached the sales prompt.")


def test_no_invented_platform_figures_anywhere_in_module():
    """The same constants were also .get() defaults in the sales prompt.

    `live_stats.get('facilities', 9603)` re-invented the number at the point
    of use, so fixing only the helper would have left the prompt unchanged.
    """
    tree = ast.parse(_source())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # x.get(key, DEFAULT) — the shape that re-introduced the constants
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
            continue
        for arg in node.args[1:]:
            if isinstance(arg, ast.Constant) and str(arg.value) in INVENTED:
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert not offenders, (
        "agent_hub still re-invents platform figures as .get() defaults:\n  "
        + "\n  ".join(offenders))


def test_module_runs_no_sqlite_datetime():
    """agent_hub talks to Postgres; datetime() is SQLite and raises there."""
    sql = _executed_sql(ast.parse(_source()))
    assert sql, "found no SQL in agent_hub — guard scanned nothing"
    offenders = [f"line {ln}: {s.strip()}" for ln, s in sql
                 if re.search(r"datetime\(\s*[\"']now", s)]
    assert not offenders, (
        "SQLite datetime() reached a Postgres cursor in agent_hub:\n  "
        + "\n  ".join(offenders))


def test_no_reads_of_the_empty_capacity_tracking_table():
    """`capacity_tracking` has never held a row; SUM() over it returns NULL.

    get_live_dchub_config summed it and, on the NULL, served `pipeline_gw =
    13.0` — a literal, not a fallback, because the read could not succeed.
    Its only writer (deep_learning_engine._save_capacity_update) is being
    removed as dead. capacity_pipeline is the live table.
    """
    sql = _executed_sql(ast.parse(_source()))
    assert sql, "found no SQL in agent_hub — guard scanned nothing"
    offenders = [f"line {ln}: {s.strip()}" for ln, s in sql
                 if re.search(r"\bFROM\s+capacity_tracking\b", s, re.IGNORECASE)]
    assert not offenders, (
        "agent_hub reads capacity_tracking, which holds zero rows and has no "
        "surviving writer:\n  " + "\n  ".join(offenders))


def test_pipeline_gw_is_never_a_local_literal():
    """The GW figure must come from a read or from canonical_stats."""
    src = _source()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        # pipeline_gw = <number>
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "pipeline_gw" in names and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, (int, float)):
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
        # "pipeline_gw": <number>
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "pipeline_gw"
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, (int, float))):
                    offenders.append(f"line {k.lineno}: pipeline_gw: {v.value}")
    assert not offenders, (
        "agent_hub hardcodes a construction-pipeline GW figure; it must read "
        "capacity_pipeline or defer to canonical_stats:\n  " + "\n  ".join(offenders))


def test_quarantine_guards_are_the_canonical_ones():
    """capacity_pipeline / deals reads must import CP_OK and DEALS_OK."""
    src = _source()
    assert "from util.capacity_pipeline import CP_OK" in src, (
        "agent_hub reads capacity_pipeline; it must use the canonical CP_OK "
        "guard rather than a hand-rolled predicate")
    assert "from util.deals import DEALS_OK" in src

    stats = _func("get_live_stats")
    cap = [s for s in _sql_strings(stats)
           if re.search(r"\bFROM\s+capacity_pipeline\b", s, re.IGNORECASE)]
    assert cap, "no capacity_pipeline read found — guard scanned nothing"
    for stmt in cap:
        assert "COALESCE(data_flag" in stmt or "CP_OK" in stmt, (
            f"unguarded capacity_pipeline read publishes quarantined rows: {stmt!r}")
