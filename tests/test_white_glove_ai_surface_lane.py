"""White-glove lane 6 (AI surface) + ai_surface_sentinel persistence.

NO network, NO DB, NO flask required. The lane is a pure function of what a
cursor returns, so we drive it with a fake cursor and assert the SEMANTICS —
above all the one that motivated the lane:

  ★NULL IS NOT ZERO. _one() returns None for a missing table or a failed query.
  If the lane coalesced that to 0 it would report "0 drifts — all clean" for a
  surface nobody has ever audited, which is the exact false-green this lane
  exists to catch. Both legs must say "never ran" instead, and must be RED.

★EVERY STATEMENT IS INSIDE A FUNCTION — a module-scope exit aborts collection
and takes the whole session with it (2026-07-28, twice).

Run:  python3 -m pytest tests/test_white_glove_ai_surface_lane.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _shell():
    """Import the shell module, shimming flask if it is absent."""
    import types
    if "flask" not in sys.modules:
        fake = types.ModuleType("flask")
        fake.Blueprint = lambda *a, **k: types.SimpleNamespace(
            route=lambda *a, **k: (lambda f: f))
        fake.Response = object
        fake.jsonify = lambda *a, **k: None
        fake.request = types.SimpleNamespace(headers={}, args={})
        sys.modules["flask"] = fake
    import routes.white_glove_loop_master_shell as m
    return m


class _Cur:
    """Fake cursor: maps a substring of the SQL to the scalar to return.

    Anything unmatched returns None — the same thing the real _one() yields for
    a missing table, which is precisely the case under test.
    """

    def __init__(self, mapping):
        self._map = mapping
        self._pending = None

    def execute(self, sql, args=None):
        # Longest needle wins. Every query in the lane contains
        # "FROM white_glove_runs", so a first-match-wins matcher would hand the
        # age query's value to the drifted/checked queries and quietly test
        # nothing. Specificity has to be explicit here.
        self._pending = None
        best = None
        for needle, val in self._map.items():
            if needle in sql and (best is None or len(needle) > len(best[0])):
                best = (needle, val)
        if best is not None:
            self._pending = best[1]

    def fetchone(self):
        return None if self._pending is None else (self._pending,)


def _by_id(checks):
    return {c["id"]: c for c in checks}


# ── The null-is-not-zero invariant ───────────────────────────────────
def test_never_audited_reads_as_never_ran_not_as_clean():
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({})))
    audited = checks["ai_surface_audited"]
    assert audited["pass"] is False
    assert audited["critical"] is True
    assert "NEVER" in audited["detail"]
    # The "agrees" check must NOT be emitted at all when nothing has ever run —
    # claiming "0 surfaces in drift" off a missing table is the false green.
    assert "ai_surface_agrees" not in checks


def test_never_propagated_reads_as_never_ran_not_as_clean():
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({})))
    told = checks["partners_told"]
    assert told["pass"] is False
    assert told["critical"] is True
    assert "NEVER" in told["detail"]
    assert "partner_listings_clean" not in checks


def test_empty_db_makes_the_whole_lane_red():
    m = _shell()
    checks = m._lane_ai_surface(_Cur({}))
    assert m._verdict(checks) is False
    assert all(c["pass"] is False for c in checks)


# ── Freshness ────────────────────────────────────────────────────────
def test_fresh_and_clean_audit_passes():
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({
        "FROM ai_surface_audits": 2.0,
        "major_drift FROM ai_surface_audits": 0,
        "total_drifts FROM ai_surface_audits": 0,
        "FROM white_glove_runs": 3.0,
        "drifted FROM white_glove_runs": 0,
        "checked FROM white_glove_runs": 9,
    })))
    assert checks["ai_surface_audited"]["pass"] is True
    assert checks["ai_surface_agrees"]["pass"] is True
    assert checks["partners_told"]["pass"] is True
    assert checks["partner_listings_clean"]["pass"] is True


def test_stale_audit_fails_even_when_it_was_clean():
    """A clean audit from a week ago is not evidence about today."""
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({
        "FROM ai_surface_audits": 200.0,
        "major_drift FROM ai_surface_audits": 0,
        "total_drifts FROM ai_surface_audits": 0,
    })))
    assert checks["ai_surface_audited"]["pass"] is False
    assert checks["ai_surface_agrees"]["pass"] is True


def test_drift_fails_even_when_the_audit_is_fresh():
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({
        "FROM ai_surface_audits": 1.0,
        "major_drift FROM ai_surface_audits": 3,
        "total_drifts FROM ai_surface_audits": 11,
    })))
    assert checks["ai_surface_audited"]["pass"] is True
    assert checks["ai_surface_agrees"]["pass"] is False
    assert checks["ai_surface_agrees"]["critical"] is True


def test_drifted_partner_listings_fail():
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({
        "FROM white_glove_runs": 1.0,
        "drifted FROM white_glove_runs": 4,
        "checked FROM white_glove_runs": 9,
    })))
    assert checks["partner_listings_clean"]["pass"] is False
    assert "4 of 9" in checks["partner_listings_clean"]["detail"]


# ── Lane 4: the check must be able to register success ───────────────
def test_brain_landing_can_actually_pass():
    """It could not, before 2026-08-06: the pass value was the literal False,
    so merged7 fed the message and nothing else. A critical check that can
    never go green pins the shell red forever and cannot tell you whether a
    fix worked."""
    m = _shell()
    checks = _by_id(m._lane_brain(_Cur({
        "FROM brain_findings": 5,
        "FROM brain_proposed_code_fixes": 4,
        "FROM brain_automerge_log": 12,
    })))
    assert checks["brain_landing"]["pass"] is True


def test_brain_landing_fails_at_zero_merges():
    m = _shell()
    checks = _by_id(m._lane_brain(_Cur({
        "FROM brain_findings": 5,
        "FROM brain_proposed_code_fixes": 4,
        "FROM brain_automerge_log": 0,
    })))
    assert checks["brain_landing"]["pass"] is False


def test_brain_landing_unreadable_table_is_failure_not_success():
    """A missing brain_automerge_log is itself a reason not to believe the
    brain is landing anything — it must not read as green."""
    m = _shell()
    checks = _by_id(m._lane_brain(_Cur({})))
    assert checks["brain_landing"]["pass"] is False


def test_lane_is_wired_into_the_tick():
    """A lane function nobody calls is the failure mode this whole shell is
    about. Assert it is actually in the assembled lane list."""
    import inspect
    m = _shell()
    src = inspect.getsource(m._run_tick)
    assert "_lane_ai_surface(cur)" in src


# ── Sentinel persistence ─────────────────────────────────────────────
def _sentinel():
    import types
    if "flask" not in sys.modules:
        fake = types.ModuleType("flask")
        fake.Blueprint = lambda *a, **k: types.SimpleNamespace(
            route=lambda *a, **k: (lambda f: f))
        fake.jsonify = lambda *a, **k: None
        fake.request = types.SimpleNamespace(headers={}, args={})
        sys.modules["flask"] = fake
    import ai_surface_sentinel as s
    return s


def test_persist_returns_false_without_a_db_and_never_raises(monkeypatch):
    """Losing the row must not lose the answer — and must not be reported as a
    successful write."""
    s = _sentinel()
    monkeypatch.setattr(s, "_audit_db_conn", lambda: None)
    assert s.persist_audit({"summary": {"clean": 1}, "total_drifts": 0}) is False


def test_persist_survives_a_malformed_result(monkeypatch):
    s = _sentinel()
    monkeypatch.setattr(s, "_audit_db_conn", lambda: None)
    for bad in ({}, {"summary": None}, {"summary": {"clean": "x"}}, None):
        assert s.persist_audit(bad) is False


def test_audit_table_ddl_avoids_the_non_immutable_index_trap():
    """Indexing `timestamptz::date` is non-IMMUTABLE and Postgres rejects it —
    this repo has an allowlisted `immutable_index` transform class because that
    trap has bitten it before."""
    import inspect
    s = _sentinel()
    ddl = inspect.getsource(s._ensure_audits_table)
    assert "::date" not in ddl
    assert "created_at DESC" in ddl
