"""Every column the DCPI alert query names must be declared in the table's DDL.

★ `SELECT score FROM market_power_scores` shipped and NEVER worked. That table
has 30 columns and not one is called `score`. psycopg2 raised UndefinedColumn,
a bare `except` swallowed it, the helper returned None, and the caller reported
"no_current_value" — so no dcpi_change alert has ever been able to fire, for as
long as the alert has existed.

It took FOUR fixes to become visible, each one revealing the next:

  #3980  the to_regclass probe raised KeyError(0)  -> "saved_lp_alerts_table_missing"
  #3989  the row was read positionally            -> "no_current_value"
  #4000  one string covered three conditions      -> "no_current_value"
  here   the column does not exist                -> "dcpi_lookup_failed:UndefinedColumn"

Only the third fix made the fourth findable. Every layer reported something
plausible and false.

★ THIS GUARD CHECKS AGAINST THE DDL, NOT A SNAPSHOT. routes/dcpi.py holds the
CREATE TABLE, so it is the in-repo authority and cannot rot the way a copied
column list would. Verified against the LIVE schema on 2026-09-06: 30 columns,
`excess_power_score` present, `score` absent.
"""
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _ddl_columns():
    """Column names declared in the market_power_scores CREATE TABLE."""
    src = open(os.path.join(_ROOT, "routes", "dcpi.py"), encoding="utf-8").read()
    i = src.index("CREATE TABLE IF NOT EXISTS market_power_scores")
    block = src[i:src.index(")", src.index("computed_at", i))]
    cols = set()
    for line in block.splitlines()[1:]:
        m = re.match(r"\s*([a-z_][a-z0-9_]*)\s+[A-Z]", line)
        if m:
            cols.add(m.group(1))
    return cols


def _alert_query():
    """The SQL string inside _current_dcpi_for_market."""
    src = open(os.path.join(_ROOT, "routes", "lp_alerts_cron.py"),
               encoding="utf-8").read()
    i = src.index("def _current_dcpi_for_market")
    j = src.index("FROM market_power_scores", i)
    return src[src.rindex("SELECT", i, j):src.index("LIMIT 1", j) + 7]


def test_the_ddl_parse_found_a_plausible_table():
    """A guard whose parse returns nothing passes everything."""
    cols = _ddl_columns()
    assert len(cols) >= 15, f"DDL parse produced only {len(cols)} columns: {cols}"
    for anchor in ("market_slug", "market_name", "computed_at", "excess_power_score"):
        assert anchor in cols, f"{anchor} missing — the DDL parse is wrong"


def test_score_is_not_a_column_and_never_was():
    """Pin the actual defect. If a `score` column is ever added, this test is
    what tells whoever adds it that an alert query is waiting on the name."""
    assert "score" not in _ddl_columns()


@pytest.mark.parametrize("col", ["excess_power_score", "market_name",
                                 "market_slug", "computed_at"])
def test_each_column_the_query_names_is_declared(col):
    q = _alert_query()
    assert col in q, f"the alert query no longer references {col}"
    assert col in _ddl_columns(), (
        f"the alert query selects {col}, which the market_power_scores DDL does "
        f"not declare — this raises UndefinedColumn and the alert dies silently")


def test_the_query_selects_no_undeclared_column():
    """★ THE GENERAL RULE, not just the one column that broke. Any identifier
    the query names must exist in the DDL."""
    q = _alert_query()
    ddl = _ddl_columns()
    # identifiers that look like columns, minus SQL keywords and the alias
    words = set(re.findall(r"\b[a-z_][a-z0-9_]{2,}\b", q))
    keywords = {"select", "from", "where", "lower", "order", "by", "desc",
                "limit", "market_power_scores", "as"}
    for w in words - keywords:
        assert w in ddl, (
            f"the alert query references {w!r}, which is not declared in the "
            f"market_power_scores DDL")


def test_the_query_still_targets_the_right_table():
    assert "FROM market_power_scores" in _alert_query()
