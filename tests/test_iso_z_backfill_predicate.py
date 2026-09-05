"""The backfill's predicate must be idempotent and value-shaped, not name-shaped.

★ WHAT THIS GUARDS. migrations/2026-09-05_iso_z_backfill.sql appends `Z` to
bare ISO timestamps stored in TEXT columns. It runs against production rows, so
two properties have to hold and neither is visible in CI otherwise:

  1. IDEMPOTENT — a value already ending in `Z` (or an offset) must not match,
     or a second run produces `...ZZ`. Migrations get re-run.
  2. VALUE-SHAPED — it must never select a column by NAME. An earlier attempt
     filtered column names and matched `status`, `platform`, `category` and
     `raw_data`, because the substring "at" appears inside every one of them.
     Appending `Z` to a status string is silent, permanent corruption.

The regex is read OUT OF THE .sql FILE rather than restated here, so this
cannot certify a predicate the migration no longer uses.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = os.path.join(ROOT, "migrations", "2026-09-05_iso_z_backfill.sql")


def _predicate() -> str:
    src = open(SQL, encoding="utf-8").read()
    m = re.search(r"bare_iso\s+text\s*:=\s*'([^']+)'", src)
    assert m, f"could not find the bare_iso predicate in {SQL} — this test is blind"
    return m.group(1)


def test_the_migration_still_declares_a_predicate():
    assert _predicate(), "empty predicate"


@pytest.mark.parametrize("value,should_match,why", [
    ("2026-09-05T04:24:58.917155",       True,  "bare with micros — the target"),
    ("2026-09-05T04:24:58",              True,  "bare without micros — also the target"),
    ("2026-09-05T04:24:58.917155Z",      False, "already Z — IDEMPOTENCE"),
    ("2026-09-05T04:24:58.917155+00:00", False, "carries an offset"),
    ("2026-09-05 04:24:58",              False, "space separator, not ISO-T"),
    ("active",                           False, "a status string"),
    ("platform-x",                       False, "a platform string"),
    ('{"raw": 1}',                       False, "a JSON payload in a TEXT column"),
    ("",                                 False, "empty"),
])
def test_predicate_matches_only_bare_iso(value, should_match, why):
    assert bool(re.match(_predicate(), value)) is should_match, why


def test_the_migration_never_filters_columns_by_name():
    """The failure mode that would corrupt `status`/`category`/`raw_data`."""
    src = open(SQL, encoding="utf-8").read().lower()
    assert "column_name like" not in src, "column selected by NAME — see the docstring"
    assert "column_name ~" not in src, "column selected by NAME pattern"
    assert "data_type in ('text', 'character varying')" in src, \
        "must select candidate columns by TYPE, then filter rows by VALUE"


def test_the_migration_only_touches_base_tables():
    src = open(SQL, encoding="utf-8").read()
    assert "BASE TABLE" in src, "an UPDATE against a view is a different problem"
