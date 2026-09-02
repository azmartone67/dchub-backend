#!/usr/bin/env python3
"""tests/test_freshness_radar_declaration_audit.py — a registry must not declare
what it cannot measure, and must say so when it can't.

NO NETWORK, NO DB. The real functions run against a stub cursor.

WHAT WENT WRONG (2026-09-02). data_freshness_radar calls itself "the single
source of truth" for staleness and is driven by two crons
(evolve-cron.yml, sla-visitor.yml). Each domain declares a LIST of candidate
tables, and `_first_existing_table` returns the first one that exists — saying
nothing about the rest. Measured against information_schema on production:

  · the `transmission` domain declared transmission_lines, transmission_segments
    and `transmission`. THE LAST TWO DO NOT EXIST. The domain had been measuring
    transmission_lines alone while reading as a healthy three-table declaration.
  · the `transactions` domain declared announcements with candidate columns
    created_at / announced_date / date / updated_at. announcements has NONE of
    them (it has published_at, published_date, discovered_at, processed_at).
    It never mattered only because `deals` is tried first.

★ THE POINT IS NOT THE TWO BAD ROWS — it is that a declaration could be fiction
and nothing in the system said so. Same shape as the rest of this program: an
unmeasurable thing must not read as a fine thing.

Run standalone:   python3 tests/test_freshness_radar_declaration_audit.py
Run under pytest: pytest tests/test_freshness_radar_declaration_audit.py
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("flask")
from routes import data_freshness_radar as R  # noqa: E402


class _Cur:
    """Stub cursor. `existing` names the tables to_regclass will resolve."""

    def __init__(self, existing=(), raise_on=None):
        self.existing = set(existing)
        self.raise_on = raise_on
        self._result = None

    def execute(self, sql, params=None):
        if self.raise_on and self.raise_on in str(sql):
            raise RuntimeError("stub failure")
        if "to_regclass" in str(sql):
            name = (params or [""])[0].replace("public.", "")
            self._result = [name if name in self.existing else None]
        else:
            self._result = [0]

    def fetchone(self):
        return self._result


def test_missing_declared_tables_are_reported_in_order():
    missing = R._declaration_audit(
        _Cur(existing={"transmission_lines"}),
        ["transmission_lines", "transmission_segments", "transmission"])
    assert missing == ["transmission_segments", "transmission"]


def test_a_fully_satisfiable_declaration_reports_nothing():
    assert R._declaration_audit(_Cur(existing={"deals"}), ["deals"]) == []


def test_an_invalid_identifier_counts_as_missing():
    """A name the scanner refuses to query is not a name it verified."""
    assert R._declaration_audit(_Cur(existing=set()), ["bad-name; DROP"]) == ["bad-name; DROP"]


def test_the_audit_never_raises_inside_the_scan():
    """It runs inside scan_domains, which must not be breakable by it."""
    missing = R._declaration_audit(_Cur(existing={"deals"}, raise_on="to_regclass"), ["deals"])
    assert missing == ["deals"], "a cursor failure must degrade to 'unverified', not propagate"


def test_first_existing_table_still_picks_the_first_real_one():
    """The audit is additive — it must not change which table gets measured."""
    got = R._first_existing_table(
        _Cur(existing={"transmission_lines"}),
        ["transmission_lines", "transmission_segments"])
    assert got == "transmission_lines"


# ── the declarations themselves ────────────────────────────────────────────

def _domain(name):
    return next(d for d in R._DOMAINS if d[0] == name)


def test_transmission_no_longer_declares_the_two_phantom_tables():
    tables = _domain("transmission")[1]
    for phantom in ("transmission_segments", "transmission"):
        assert phantom not in tables, (
            f"{phantom!r} does not exist in production; re-declaring it puts the "
            f"domain back to reading as a healthy multi-table declaration while "
            f"measuring one table")


def test_transactions_declares_a_column_announcements_actually_has():
    """announcements has published_at / published_date / discovered_at /
    processed_at — and none of the four originally declared."""
    cols = _domain("transactions")[2]
    real = {"published_at", "published_date", "discovered_at", "processed_at"}
    assert real & set(cols), (
        "the transactions domain declares no column that announcements has; if "
        "`deals` ever moves, this domain goes unknown for a reason nobody can "
        "read off the declaration")


@pytest.mark.parametrize("entry", R._DOMAINS, ids=[d[0] for d in R._DOMAINS])
def test_every_declaration_is_well_formed(entry):
    domain, tables, ts_cols, sla = entry
    assert tables, f"{domain} declares no source table"
    assert ts_cols, f"{domain} declares no timestamp column"
    assert isinstance(sla, int) and sla > 0, f"{domain} has a nonsense SLA"
    for name in list(tables) + list(ts_cols):
        assert R._IDENT_RE.match(name), (
            f"{domain} declares {name!r}, which the scanner will refuse to "
            f"query — it would be silently skipped forever")


def test_the_domain_list_is_not_quietly_emptied():
    """A parametrized test over an empty list passes vacuously and forever."""
    assert len(R._DOMAINS) >= 11, "_DOMAINS shrank; a domain lost its coverage"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
