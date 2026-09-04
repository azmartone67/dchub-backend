"""r-poe-canon — /poe/query must answer out of the CANONICAL, PUBLISHED row.

The DCPI branch of handle_poe_query looked a market up by name and answered
straight from whatever row matched, with no `published` predicate. The retired
alias-twins r-twin-unpublish keeps so direct links still resolve carry the
LONGER names, so `ORDER BY LENGTH(market_name) DESC` did not merely fail to
exclude them — it PREFERRED them. Measured live before the fix, five of six
ordinary phrasings answered out of rows frozen at the 2026-07-19 retirement,
under the words "Full daily-recomputed breakdown", and two of the verdicts
disagreed with the live market.

The fix is NOT simply to filter the lookup. Narrowing the name match to
published rows would stop recognising the names people type — "northern
virginia" is not a substring of "Ashburn" — turning a wrong answer into no
answer. So the match stays wide and the ANSWER is resolved through
util.market_aliases.canonical_slug, which is how /dcpi/<twin-slug> already
behaves.

That split is what these tests pin, because it is what a later reader is most
likely to "tidy up" in either direction:

  * a query that SELECTS SCORES must be published-filtered
  * a query that selects only identifying columns may be wide — that is the
    lookup, and it is harmless precisely because nothing can be answered from it

These tests read the shipped source rather than importing main.py, per the
house rule.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "ai_interconnection.py"
TABLE = "market_power_scores"
PREDICATE_NAME = "PUBLISHED_ONLY"
RESOLVER = "canonical_slug"
HANDLER = "handle_poe_query"

#: Columns that constitute an ANSWER about a market. A query selecting any of
#: these is serving DCPI figures and must be pinned to the published universe.
SCORE_COLUMNS = ("verdict", "excess_power_score", "constraint_score",
                 "time_to_power_months")


@pytest.fixture(scope="module")
def tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _sql_texts(tree):
    """(lineno, SQL) for every SELECT over the scores table.

    Reconstructed from the string node with each f-string slot rendered as the
    SOURCE of the expression it interpolates, so `f"... AND {PUBLISHED_ONLY}"`
    yields the literal text `AND PUBLISHED_ONLY`.

    ★ Two traps, both of which produced a silently green check in the sibling
    fence before they were caught by mutation:

    1. Do NOT unparse the enclosing statement instead — that wraps the SQL back
       inside its own call, and any paren-depth reasoning about the query then
       measures the call, not the query.
    2. ast.walk descends INTO an f-string, so each literal chunk is also visited
       as a bare Constant — the same query again, minus its interpolated slot.
       Left in, every f-string query reports itself as unpredicated.
    """
    inside_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                      for v in n.values}
    out = []
    for node in ast.walk(tree):
        if id(node) in inside_fstring:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else ast.unparse(v.value)
                for v in node.values
                if isinstance(v, (ast.Constant, ast.FormattedValue)))
        else:
            continue
        if f"FROM {TABLE}" in " ".join(text.split()):
            out.append((getattr(node, "lineno", 0), text))
    return out


def _select_list(sql):
    """The column list between SELECT and FROM, lowercased."""
    flat = " ".join(sql.split()).lower()
    return flat[flat.index("select") + 6:flat.index("from ")] if "select" in flat else ""


def test_the_handler_and_its_queries_are_found(tree):
    """A renamed handler or a moved query must not pass by vacancy."""
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert HANDLER in names, f"{HANDLER} is gone; move this fence with it"
    assert _sql_texts(tree), f"no SELECT over {TABLE} left in {SRC.name}"


def test_every_query_that_serves_scores_is_published_only(tree):
    """The defect: answering a market question out of a retired row."""
    offenders = []
    for ln, sql in _sql_texts(tree):
        cols = _select_list(sql)
        if any(c in cols for c in SCORE_COLUMNS) and PREDICATE_NAME not in sql:
            offenders.append(ln)
    assert not offenders, (
        f"{SRC.name} line(s) {offenders}: a query selects DCPI scores without "
        f"{PREDICATE_NAME}. The retired alias-twins are still in this table and "
        f"carry the longer names, so an unfiltered lookup does not merely risk "
        f"them — ORDER BY LENGTH(market_name) prefers them.")


def test_the_wide_lookup_selects_no_scores(tree):
    """The other half of the split, so 'fixing' it either way is caught.

    A wide (unfiltered) query is allowed ONLY while it cannot answer anything —
    identifying columns, nothing more. The moment one selects a score it stops
    being a lookup and becomes an answer.
    """
    offenders = []
    for ln, sql in _sql_texts(tree):
        if PREDICATE_NAME in sql:
            continue
        cols = _select_list(sql)
        leaked = [c for c in SCORE_COLUMNS if c in cols]
        if leaked:
            offenders.append(f"line {ln}: {leaked}")
    assert not offenders, (
        "an unfiltered query over the scores table selects score columns:\n  "
        + "\n  ".join(offenders))


def test_the_answer_is_resolved_through_the_alias_table(tree):
    """Filtering alone would silently stop answering, which is its own failure.

    'northern virginia' is not a substring of 'Ashburn'. Without the resolver
    the published-only lookup simply misses, and the endpoint quietly degrades
    to a generic list for every aliased market — a regression that no count
    would show.
    """
    handler = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == HANDLER), None)
    assert handler is not None, f"{HANDLER} not found"
    called = {ast.unparse(n.func) for n in ast.walk(handler) if isinstance(n, ast.Call)}
    assert RESOLVER in called, (
        f"{HANDLER} never calls {RESOLVER}(). A published-only lookup that does "
        f"not resolve aliases stops recognising the names users type.")
