"""The audit must judge an ADD COLUMN on the COLUMN, not on its table.

The bug this closes
-------------------
`routes/ddl_audit.py` asked one question of production: does the table exist?
For `ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS publisher_url TEXT` —
dropped on the floor by the pooled cursor, like every frozen statement — the
answer was YES. news_articles has existed for a long time. The audit therefore
reported EXISTS, which was true and useless: the COLUMN was missing, every
INSERT naming it failed, and news intake ran at ~10 rows/day instead of ~157
for nine days (#4438).

The register was keyed one level coarser than the defect, so the defect was
invisible to it. These tests pin the finer key.

House rule: no test here imports main.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_ALTER = ("ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS "
              "publisher_url TEXT")


def _scanner():
    path = os.path.join(ROOT, "scripts", "check_ddl_through_pool.py")
    spec = importlib.util.spec_from_file_location("_ddl_guard_cols", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── extraction ────────────────────────────────────────────────────────────

def test_the_statement_that_hid_for_nine_days_yields_its_column():
    assert _scanner().target_column(NEWS_ALTER) == "publisher_url"


@pytest.mark.parametrize("stmt,why", [
    ("CREATE TABLE x (id int)", "a CREATE TABLE adds no column"),
    ("CREATE INDEX i ON x (y)", "an index is not a column"),
    ("ALTER TABLE x ADD CONSTRAINT c UNIQUE (y)", "a constraint is not a column"),
    ("ALTER TABLE x ADD PRIMARY KEY (y)", "a primary key is not a column"),
    ("ALTER TABLE x ALTER COLUMN y DROP DEFAULT", "this changes, not adds"),
])
def test_statements_with_no_added_column_report_none(stmt, why):
    assert _scanner().target_column(stmt) == "", why


def test_postgres_lets_you_omit_the_column_keyword():
    assert _scanner().target_column("ALTER TABLE x ADD foo TEXT") == "foo"


def test_the_column_survives_the_snippet_truncation():
    """Each offence stores `stmt.split("\\n")[0][:70]`. The column name is the
    LAST thing in an ADD COLUMN, so deriving it from the stored snippet
    instead of the full statement returns "" for anything long — and reports
    the statement as table-only, which is the blindness being removed."""
    g = _scanner()
    long_stmt = ("ALTER TABLE some_rather_long_table_name_here "
                 "ADD COLUMN IF NOT EXISTS a_column_well_past_char_seventy TEXT")
    snippet = long_stmt.split("\n")[0][:70]
    assert g.target_column(snippet) == "", (
        "the fixture no longer truncates the column away — it cannot show "
        "what it was written to show; lengthen it")
    assert g.target_column(long_stmt) == "a_column_well_past_char_seventy", (
        "the column must be read from the FULL statement, at capture time")


# ── the verdict ───────────────────────────────────────────────────────────

def _frozen(table, column="", path="m.py", fn="f"):
    return {"path": path, "function": fn, "table": table, "column": column,
            "line": 1, "sql": "x"}


def test_an_add_column_is_judged_on_the_column_not_the_table():
    """The exact shape of #4438: table present, column absent."""
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen("news_articles", "publisher_url")],
                    exists={"news_articles": True},
                    columns={("news_articles", "publisher_url"): False})
    assert rows[0]["verdict"] == "MISSING", (
        "a present table with an absent column was reported "
        f"{rows[0]['verdict']} — that verdict is what hid publisher_url")
    assert rows[0]["tables"][0]["table"] == "news_articles.publisher_url", (
        "the row does not name the column, so nobody reading the audit can "
        "tell which one is missing")


def test_a_present_column_is_exists():
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen("news_articles", "publisher_url")],
                    exists={"news_articles": True},
                    columns={("news_articles", "publisher_url"): True})
    assert rows[0]["verdict"] == "EXISTS"


def test_an_unaskable_column_is_unknown_not_exists():
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen("news_articles", "publisher_url")],
                    exists={"news_articles": True}, columns={})
    assert rows[0]["verdict"] == "UNKNOWN", (
        "a column we could not ask about must not be reported as present")


def test_a_create_table_is_still_judged_on_its_table():
    """The finer key must not break the coarser one."""
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen("submissions")], exists={"submissions": False},
                    columns={})
    assert rows[0]["verdict"] == "MISSING"
    assert rows[0]["tables"][0]["table"] == "submissions"


def test_one_function_with_a_live_table_and_a_dead_column_is_partial():
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen("announcements"),
                     _frozen("announcements", "category")],
                    exists={"announcements": True},
                    columns={("announcements", "category"): False})
    assert rows[0]["verdict"] == "PARTIAL", (
        "a function whose table exists but whose ADD COLUMN did not land is "
        "neither EXISTS nor MISSING")


# ── the floor ─────────────────────────────────────────────────────────────

MIN_ADD_COLUMNS = 4   # 5 today (api_server x2, discovery_pipeline x2,
                      # news_engine x1). A collapse detector, not an exact pin.


def test_the_scan_actually_finds_add_columns():
    """A scan that matches nothing reports the same green as a scan that
    matched everything and found it clean. If a refactor moves these
    statements out from under the extractor, this fails instead of quietly
    auditing an empty set."""
    g = _scanner()
    files, offences = g.scan_tree(ROOT)
    assert files >= g.MIN_FILES, (
        f"only {files} files scanned — the walk collapsed, so any column "
        "verdict below is vacuous")
    with_col = [o for o in offences if o.get("column")]
    assert len(with_col) >= MIN_ADD_COLUMNS, (
        f"only {len(with_col)} ADD COLUMN statements found on pooled cursors "
        f"(floor {MIN_ADD_COLUMNS}). Either they were genuinely fixed — then "
        "lower the floor deliberately — or the extractor stopped matching and "
        "this audit is now blind to the exact class that caused #4438")
    for o in with_col:
        assert o["table"], f"{o['path']}:{o['line']} has a column but no table"
