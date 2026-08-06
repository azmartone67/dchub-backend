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
        # Most-specific key wins, where a key may be a TUPLE of substrings that
        # must ALL appear; specificity is their combined length.
        #
        # Two separate traps forced this, and both silently made a test assert
        # nothing rather than fail:
        #  · every white_glove_runs query contains "FROM white_glove_runs", so
        #    first-match-wins handed the freshness value to the drifted/checked
        #    queries.
        #  · in _lane_agents the "returning" query is a strict SUPERSET of the
        #    "new" query — the whole of it plus an `ip_address IN (...)` clause
        #    — so longest-single-needle still resolved both to the same value,
        #    and a 10%-retention fixture quietly tested as 100%.
        # A tuple key says "this query and not its prefix-twin" directly.
        # Ranked by (number of parts, total length) — MORE CONSTRAINTS WINS
        # first, length only breaks ties. Ranking by length alone is not enough:
        # the single needle "COUNT(DISTINCT ip_address) FROM mcp_tool_calls" is
        # 45 chars and would still outscore the two-part key that actually
        # distinguishes the returning-query from the new-query, putting the
        # fixture straight back to silently testing nothing.
        self._pending = None
        best_score, best_val = None, None
        for needle, val in self._map.items():
            parts = needle if isinstance(needle, tuple) else (needle,)
            if all(p in sql for p in parts):
                score = (len(parts), sum(len(p) for p in parts))
                if best_score is None or score > best_score:
                    best_score, best_val = score, val
        if best_score is not None:
            self._pending = best_val

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
    # ★Must name the dry-run exclusion. With every white_glove_runs read now
    # filtered to non-dry, "no rows" and "only dry rows" collapse into this same
    # branch — so this is where a reader learns a dry probe does not count.
    assert "dry run" in told["detail"].lower()
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


def test_partners_told_sql_filters_dry_runs():
    """Pin the SQL. `IS NOT TRUE` rather than `= false` so a NULL dry_run — rows
    written before the column was populated — still counts as a real run instead
    of vanishing from the freshness window. All three white_glove_runs reads
    must filter, not just the freshness one."""
    import inspect
    m = _shell()
    src = inspect.getsource(m._lane_ai_surface)
    assert "dry_run = false" not in src.lower()
    assert src.count("dry_run IS NOT TRUE") >= 3


def test_drifted_partner_listings_fail():
    m = _shell()
    checks = _by_id(m._lane_ai_surface(_Cur({
        "FROM white_glove_runs\n                             WHERE dry_run": 1.0,
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


# ══════════════════════════════════════════════════════════════════════
# Lanes 1, 2 and 5 — measurement validity
# ══════════════════════════════════════════════════════════════════════
class _RowCur(_Cur):
    """_Cur plus fetchall(), for the lanes that read row sets."""

    def __init__(self, mapping, rows=None, rowsets=None):
        super().__init__(mapping)
        self._rows = rows or []
        self._rowsets = rowsets or {}
        self._pending_rows = None

    def execute(self, sql, args=None):
        super().execute(sql, args)
        self._pending_rows = self._rows
        for needle, rs in self._rowsets.items():
            if needle in sql:
                self._pending_rows = rs

    def fetchall(self):
        return self._pending_rows


# ── Lane 2 · the two retention bases must stay separate ──────────────
def test_keyed_retention_is_a_separate_check_not_a_replacement():
    """Swapping the IP number for the api_key number would make the lane
    greener without anything improving. Both must be reported, and the IP one
    stays the critical=True verdict."""
    m = _shell()
    checks = _by_id(m._lane_agents(_RowCur({
        "COUNT(DISTINCT ip_address) FROM mcp_tool_calls": 100,
        ("COUNT(DISTINCT ip_address)", "ip_address IN ("): 10,
        "COUNT(DISTINCT api_key) FROM mcp_call_log": 20,
        ("COUNT(DISTINCT api_key)", "api_key IN ("): 12,
    })))
    assert "agent_retention" in checks and "agent_retention_keyed" in checks
    # IP basis: 10/100 = 10% -> below the 25% floor -> fails, and is critical.
    assert checks["agent_retention"]["pass"] is False
    assert checks["agent_retention"]["critical"] is True
    # Keyed basis: 12/20 = 60% -> passes, but must NOT be critical, because it
    # speaks for a strict subset (keyless callers have no api_key).
    assert checks["agent_retention_keyed"]["pass"] is True
    assert checks["agent_retention_keyed"]["critical"] is False


def test_keyed_retention_names_its_basis_and_population():
    m = _shell()
    checks = _by_id(m._lane_agents(_RowCur({
        "COUNT(DISTINCT ip_address) FROM mcp_tool_calls": 10,
        ("COUNT(DISTINCT ip_address)", "ip_address IN ("): 1,
        "COUNT(DISTINCT api_key) FROM mcp_call_log": 4,
        ("COUNT(DISTINCT api_key)", "api_key IN ("): 3,
    })))
    assert "api_key" in checks["agent_retention_keyed"]["detail"]
    assert "IP basis" in checks["agent_retention"]["name"]


def test_zero_keyed_callers_is_unmeasured_not_zero_percent():
    """0 returning of 0 keyed callers is 0%, which would render as a false red
    for a population that does not exist."""
    m = _shell()
    checks = _by_id(m._lane_agents(_RowCur({
        "COUNT(DISTINCT ip_address) FROM mcp_tool_calls": 10,
        ("COUNT(DISTINCT ip_address)", "ip_address IN ("): 5,
        "COUNT(DISTINCT api_key) FROM mcp_call_log": 0,
        ("COUNT(DISTINCT api_key)", "api_key IN ("): 0,
    })))
    assert checks["agent_retention_keyed"]["pass"] is None
    assert "UNMEASURED" in checks["agent_retention_keyed"]["detail"]


def test_unreadable_call_log_does_not_promote_the_ip_figure():
    m = _shell()
    checks = _by_id(m._lane_agents(_RowCur({
        "COUNT(DISTINCT ip_address) FROM mcp_tool_calls": 10,
        ("COUNT(DISTINCT ip_address)", "ip_address IN ("): 1,
    })))
    assert checks["agent_retention_keyed"]["pass"] is None
    assert checks["agent_retention"]["pass"] is False


# ── Lane 5 · reach cannot be judged on an unfetched column ───────────
def _media_rows(n, impressions=16):
    return [("auto_dcpi", f"line {i}\nbody", impressions) for i in range(n)]


def test_reach_is_unmeasured_when_engagement_was_never_fetched():
    """★The lane-5 finding. impressions is filled in later by
    fetch_linkedin_impressions(), which gives up on 401/403 when the token
    lacks r_organization_social — and its daily workflow pipes the response to
    `head` without checking status, so a permanently no-opping sync stays
    green. A median computed off that column measures the sync, not the
    audience."""
    m = _shell()
    checks = _by_id(m._lane_media(_RowCur(
        {"engagement_fetched_at IS NOT NULL": 0},
        rows=_media_rows(70))))
    reach = checks["media_reach"]
    assert reach["pass"] is None
    assert "UNMEASURED" in reach["detail"]
    assert "FIX THE SYNC BEFORE JUDGING REACH" in reach["detail"]


def test_reach_is_judged_normally_once_engagement_is_fetched():
    m = _shell()
    checks = _by_id(m._lane_media(_RowCur(
        {"engagement_fetched_at IS NOT NULL": 70},
        rows=_media_rows(70, impressions=16))))
    # Fetched, and genuinely below the floor -> a real red, not UNMEASURED.
    assert checks["media_reach"]["pass"] is False
    assert "UNMEASURED" not in checks["media_reach"]["detail"]


def test_reach_passes_when_fetched_and_above_the_floor():
    m = _shell()
    checks = _by_id(m._lane_media(_RowCur(
        {"engagement_fetched_at IS NOT NULL": 70},
        rows=_media_rows(70, impressions=500))))
    assert checks["media_reach"]["pass"] is True


# ── Lane 1 · a percentage nobody can act on ──────────────────────────
def test_dormant_partners_are_named_not_just_counted():
    m = _shell()
    checks = _by_id(m._lane_partners(_RowCur(
        {"COUNT(*) FROM partner_keys_issued": 8,
         "length(key_prefix) >= 12": 8,
         "COUNT(DISTINCT p.key_prefix)": 2},
        rowsets={"NOT EXISTS": [
            ("reveal", "gabriel@reveal.example", "dchub_developer_Aa", 41),
            ("acme", "ops@acme.example", "dchub_developer_Bb", 12),
        ]})))
    detail = checks["partner_activated"]["detail"]
    assert "DORMANT:" in detail
    assert "reveal" in detail and "41d" in detail


def test_dormant_query_failure_is_not_reported_as_none_dormant():
    """A failed lookup must not read as 'nobody is dormant' — the same
    unmeasured-as-zero mistake in miniature."""
    m = _shell()

    class _Boom(_RowCur):
        def execute(self, sql, args=None):
            if "NOT EXISTS" in sql:
                raise RuntimeError("relation missing")
            super().execute(sql, args)

    checks = _by_id(m._lane_partners(_Boom(
        {"COUNT(*) FROM partner_keys_issued": 8,
         "length(key_prefix) >= 12": 8,
         "COUNT(DISTINCT p.key_prefix)": 2})))
    assert "UNAVAILABLE" in checks["partner_activated"]["detail"]
