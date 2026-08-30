#!/usr/bin/env python3
"""The field `news_articles` must count the table `news_articles`.

NO NETWORK, NO DB.

This field has been wrong twice in one day, in opposite directions:

  before #3381   main.py counted `announcements`      -> published 15,254
  #3381          main.py counted `news`               -> published  3,503
  #3384-follow   both surfaces count `news_articles`  -> publishes 13,086

#3381 correctly spotted that two citable surfaces disagreed under one field
name, and agreed them — on the wrong table. Agreement is not the invariant.
The invariant is that a field an agent can cite counts the table it names.

★ AND `news` IS NOT ABANDONED, whatever the r-newsdead docstring says
(tests/test_news_freshness_watches_live_table.py, #2631 2026-08-12: "its ONLY
writer is news_aggregator.py, which no workflow or cron invokes"). Measured
live 2026-08-30:

    news            3,503 rows,   313 published in the last 14 days
    news_articles  13,086 rows, 1,903 published in the last 14 days

`news` gains ~35 rows/day — crawler_scheduler._run_news_crawler() calls
news_aggregator.run_aggregator() as its PRIMARY path (auto_sync, which writes
news_articles, is only its fallback). So these are TWO LIVE FEEDS, not a live
one and a dead one, which is exactly why the field cannot be allowed to drift
between them: both counts look plausible.

Run standalone:   python3 tests/test_news_articles_field_counts_its_own_table.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# (file, the assignment that publishes the field, human name of the surface)
PUBLISHERS = [
    ("main.py",
     '_live_counts["news_articles"]',
     "the agent manifest (/.well-known + agent front door)"),
    ("routes/facilities_by_dims.py",
     'stats["news_articles"]',
     "/api/v1/stats/canonical"),
]

COUNT_FROM = re.compile(r"COUNT\(\*\)\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _lines(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def _table_feeding(rel, assignment):
    """The table counted by the execute() immediately above `assignment`."""
    lines = _lines(rel)
    idx = [i for i, l in enumerate(lines) if assignment in l and "=" in l]
    assert idx, f"{rel}: no assignment to {assignment} — did the field move?"
    assert len(idx) == 1, f"{rel}: {len(idx)} assignments to {assignment}; expected 1"
    # walk back to the nearest COUNT(*) FROM <table>, skipping comments
    for j in range(idx[0] - 1, max(idx[0] - 12, -1), -1):
        m = COUNT_FROM.search(lines[j])
        if m and not lines[j].lstrip().startswith("#"):
            return m.group(1)
    raise AssertionError(f"{rel}: no COUNT(*) FROM above {assignment}")


def test_every_citable_news_articles_field_counts_the_news_articles_table():
    wrong = []
    for rel, assignment, surface in PUBLISHERS:
        table = _table_feeding(rel, assignment)
        if table != "news_articles":
            wrong.append(f"{rel} ({surface}) publishes `news_articles` "
                         f"from table `{table}`")
    assert not wrong, (
        "a citable `news_articles` field counts a table it is not named "
        "after — an agent quoting it gets a number for something else:\n  "
        + "\n  ".join(wrong))


def test_the_two_citable_surfaces_agree():
    """#3381's actual concern, kept: one field name, one number."""
    tables = {rel: _table_feeding(rel, a) for rel, a, _ in PUBLISHERS}
    assert len(set(tables.values())) == 1, (
        f"the two citable surfaces count DIFFERENT tables under one field "
        f"name: {tables}")


if __name__ == "__main__":
    test_every_citable_news_articles_field_counts_the_news_articles_table()
    test_the_two_citable_surfaces_agree()
    print("ok")
