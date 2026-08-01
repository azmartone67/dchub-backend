"""Guards for the backfill tool's blocker scanner (backfill_facility_status_canon).

The scanner is the safety interlock: --apply refuses while a predicate that the
backfill would silently re-point is still in the tree. Its value is entirely in
being RIGHT about that set — a false positive is as costly as a false negative,
because an unclearable one leaves --force as the only path on a tool whose whole
purpose is refusing to run.

These tests pin the attribution rule, including the direction that must NOT be
traded away: a string that cannot be attributed to a table still blocks.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(ROOT, "backfill_facility_status_canon.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_PATH), reason="backfill tool not present in this tree")


def _mod():
    spec = importlib.util.spec_from_file_location("_bf_canon", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── attribution ────────────────────────────────────────────────────────────

def test_query_against_our_table_blocks():
    t = _mod()._targets_our_table
    assert t("SELECT COUNT(*) FROM discovered_facilities WHERE status = 'operational'")
    assert t("SELECT a FROM public.discovered_facilities d JOIN x ON x.id = d.id")


def test_query_against_the_legacy_facilities_table_does_not_block():
    """The api_server.py:1432 case. `facilities` keeps its own lowercase
    vocabulary and this backfill provably cannot move it."""
    t = _mod()._targets_our_table
    sql = ("SELECT SUM(CASE WHEN status = 'operational' THEN 1 ELSE 0 END) "
           "FROM facilities WHERE city LIKE ?")
    assert not t(sql)


def test_unattributable_string_still_blocks():
    """The conservative bias, pinned. A bare predicate fragment names no table,
    so it keeps the benefit of the doubt — under-reporting corrupts figures."""
    t = _mod()._targets_our_table
    assert t(" AND COALESCE(status,'') <> 'active'")
    assert t("status = 'operational'")


def test_a_query_touching_both_tables_still_blocks():
    t = _mod()._targets_our_table
    assert t("SELECT * FROM facilities f JOIN discovered_facilities d ON d.id = f.id")


# ── the scan as a whole ────────────────────────────────────────────────────

def test_scan_returns_well_formed_rows_and_only_known_blockers():
    m = _mod()
    rows = m.scan_blockers()
    known = {lit for lit, _why in m.BLOCKERS}
    for where, lit, why in rows:
        assert ":" in where, where
        assert lit in known, f"scanner invented a blocker literal: {lit}"
        assert why, "a blocker must explain itself"


def test_scanner_never_flags_the_legacy_facilities_reader():
    """Regression: api_server.py mentions discovered_facilities elsewhere, so
    file-level attribution could never clear it."""
    m = _mod()
    offenders = [w for w, _l, _y in m.scan_blockers() if w.startswith("api_server.py")]
    assert not offenders, (
        "api_server.py flagged again — its status query reads `facilities`, "
        f"not discovered_facilities: {offenders}")
