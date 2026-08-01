"""Shell #44 lane 3 — agent-count parity, wired to the canonical field.

The lane's own history is the spec: its first cut invented proxy queries that
false-passed (261 == 261 while the dashboards said 118 vs 79), then it was
DECLARED UNMEASURABLE (hard FAIL with a narrative). Now that the canonical
definition exists (mcp_calls_deloop.canonical_external_activity_sql, #2038),
the lane must MEASURE: run the canonical query, fetch the live surfaces, and
judge each published value against the canonical count within
_PARITY_TOLERANCE.

Every failure direction gets a control here so the lane can never silently
degrade back into a narrative or a false green:
  · all-canonical           -> all checks PASS
  · field gone from payload -> critical FAIL (the revert case)
  · loose value published   -> FAIL (the 145-vs-95 drift case)
  · edge fetch fails        -> passed=None (UNMEASURED, never drift)
  · field present but null  -> passed=None (unmeasured at source)
  · canonical builder gone  -> single critical FAIL
  · canonical QUERY fails   -> passed=None (#1858: a swallowed DB error must
                               not render as a passing zero)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import mcp_calls_deloop  # noqa: E402
from routes import metric_integrity_master_shell as shell  # noqa: E402

CANON_AGENTS = 95
CANON_CALLS = 5584
RAW_IPS = 145


class FakeCursor:
    """Serves queued fetchall() results in order; None means 'raise'."""

    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append(sql)
        if self.results and self.results[0] is None:
            self.results.pop(0)
            raise RuntimeError("boom")

    def fetchall(self):
        return self.results.pop(0)


def _cur_ok():
    # first _q: canonical (agents, calls); second: raw-IP context scalar
    return FakeCursor([[(CANON_AGENTS, CANON_CALLS)], [(RAW_IPS,)]])


def _by_id(checks):
    return {c["id"]: c for c in checks}


def _fetches(monkeypatch, funnel, reach):
    def fake(path, timeout=15):
        if "funnel" in path:
            return funnel
        return reach
    monkeypatch.setattr(shell, "_fetch_json", fake)


def test_all_surfaces_canonical_pass(monkeypatch):
    _fetches(monkeypatch,
             {"real_external_agents_7d": CANON_AGENTS},
             {"real_agents_7d": CANON_AGENTS})
    checks = _by_id(shell._lane_agent_parity(_cur_ok()))
    assert checks["agent_parity_single_definition"]["pass"] is True
    assert str(CANON_AGENTS) in checks["agent_parity_single_definition"]["detail"]
    assert checks["agent_parity_funnel_canonical"]["pass"] is True
    assert checks["agent_parity_reach_canonical"]["pass"] is True
    assert shell._lane_verdict(list(checks.values())) is True


def test_within_tolerance_cache_lag_passes(monkeypatch):
    # reach serves a <=30-min cache; a small lag must read as noise.
    lagged = int(CANON_AGENTS * (1 + shell._PARITY_TOLERANCE) - 1)
    _fetches(monkeypatch,
             {"real_external_agents_7d": CANON_AGENTS},
             {"real_agents_7d": lagged})
    checks = _by_id(shell._lane_agent_parity(_cur_ok()))
    assert checks["agent_parity_reach_canonical"]["pass"] is True


def test_field_gone_is_critical_fail(monkeypatch):
    # The revert case: surface stopped publishing the canonical field.
    _fetches(monkeypatch,
             {"unique_ips_7d_real": RAW_IPS},  # canonical key absent
             {"real_agents_7d": CANON_AGENTS})
    checks = _by_id(shell._lane_agent_parity(_cur_ok()))
    c = checks["agent_parity_funnel_canonical"]
    assert c["pass"] is False and c["critical"] is True
    assert "GONE" in c["detail"]
    assert shell._lane_verdict(list(checks.values())) is False


def test_loose_value_is_drift_fail(monkeypatch):
    # The measured 2026-07-31 gap: loose 145 vs canonical 95 (~52% off).
    _fetches(monkeypatch,
             {"real_external_agents_7d": RAW_IPS},
             {"real_agents_7d": CANON_AGENTS})
    checks = _by_id(shell._lane_agent_parity(_cur_ok()))
    c = checks["agent_parity_funnel_canonical"]
    assert c["pass"] is False
    assert "145" in c["detail"] and "95" in c["detail"]


def test_fetch_failure_is_unmeasured_not_drift(monkeypatch):
    _fetches(monkeypatch, None, {"real_agents_7d": CANON_AGENTS})
    checks = _by_id(shell._lane_agent_parity(_cur_ok()))
    c = checks["agent_parity_funnel_canonical"]
    assert c["pass"] is None
    assert "UNMEASURED" in c["detail"]
    # undecided checks must not drag the lane verdict down
    assert shell._lane_verdict(list(checks.values())) is True


def test_null_field_is_unmeasured_at_source(monkeypatch):
    _fetches(monkeypatch,
             {"real_external_agents_7d": None,
              "real_external_agents_7d_error": "db timeout"},
             {"real_agents_7d": CANON_AGENTS})
    checks = _by_id(shell._lane_agent_parity(_cur_ok()))
    c = checks["agent_parity_funnel_canonical"]
    assert c["pass"] is None
    assert "db timeout" in c["detail"]


def test_canonical_builder_gone_is_single_critical_fail(monkeypatch):
    def broken(days=7):
        raise RuntimeError("builder deleted")
    monkeypatch.setattr(mcp_calls_deloop,
                        "canonical_external_activity_sql", broken)
    checks = shell._lane_agent_parity(_cur_ok())
    assert len(checks) == 1
    assert checks[0]["pass"] is False and checks[0]["critical"] is True
    assert "missing or failing" in checks[0]["detail"]


def test_canonical_query_db_failure_is_unmeasured_not_zero(monkeypatch):
    # #1858: a swallowed DB error must never render as a passing "0 agents".
    _fetches(monkeypatch,
             {"real_external_agents_7d": CANON_AGENTS},
             {"real_agents_7d": CANON_AGENTS})
    cur = FakeCursor([None])  # canonical query raises -> _q returns None
    checks = shell._lane_agent_parity(cur)
    assert len(checks) == 1
    assert checks[0]["pass"] is None
    assert "UNMEASURED" in checks[0]["detail"]


def test_genuine_zero_week_is_not_unmeasured(monkeypatch):
    # A real (0, 0) row is a dead week, not a failure: comparisons proceed.
    _fetches(monkeypatch,
             {"real_external_agents_7d": 0},
             {"real_agents_7d": 0})
    cur = FakeCursor([[(0, 0)], [(0,)]])
    checks = _by_id(shell._lane_agent_parity(cur))
    assert checks["agent_parity_single_definition"]["pass"] is True
    assert checks["agent_parity_funnel_canonical"]["pass"] is True
