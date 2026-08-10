"""Guard: the flywheel reach→usage lane's WoW gate must be BURST-ROBUST.

WHY (measured 2026-08-10)
────────────────────────
The reach_usage lane fired 0/3 with real tool calls "8,504 → 2,617 (-69% WoW)".
Traced live: the prior 7d window was inflated by a single-day "datacolo" burst —
IP 90.0.172.134 made 1,482 real calls across just 2 sessions (~741/session) on
08-01 and never again. A single runaway session (a loop/batch) was dominating a
7-DAY sum and flipping the lane. Meanwhile the real driver (Smithery Connect
going dark 08-07..09) is a genuine MULTI-day decline that MUST still register.

CONTRACT
────────
  B1. A one-off single-session burst in the prior window does NOT flip the lane:
      when the burst-adjusted (per-session-day-capped) WoW is within floor, the
      usage/conversion checks PASS even though the RAW WoW is a steep drop.
  B2. A genuine multi-day decline (burst-adjusted WoW still below floor) STILL
      fails — the fix must not blind the lane.
  B3. If the winsorized probe is unavailable, the gate falls back to RAW (never
      lose the check on a query slip).
  B4. Source wiring: the per-(session,day) winsorization query exists and the
      usage gate is computed on the burst-adjusted basis, while the RAW sum is
      still displayed (funnel-identical level).

Run: python3 -m pytest tests/test_flywheel_reach_usage_burst_robust.py -v
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "flywheel_master_shell.py")


# ── stub cursor: answers each probe by SQL substring ─────────────────────────
class _Cur:
    def __init__(self, plan):
        self.plan = plan
        self._res = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "ai_daily_stats" in s:
            self._res = [self.plan["reach"]]
        elif "WITH sd AS" in s:                       # winsorized usage
            self._res = [self.plan["usage_wins"]] if self.plan.get("usage_wins") is not None else []
        elif "FROM mcp_tool_calls" in s:              # raw usage
            self._res = [self.plan["usage_raw"]]
        elif "mcp_calls_identity" in s:               # agents WoW
            self._res = [self.plan.get("agents", (10, 10))]
        else:
            self._res = []

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class _Conn:
    def __init__(self, plan):
        self._plan = plan

    def cursor(self):
        return _Cur(self._plan)

    def rollback(self):
        pass


def _lane(plan):
    fsf = pytest.importorskip("routes.flywheel_master_shell")
    checks = fsf._lane_reach_usage(_Conn(plan))
    return {ch["id"]: ch for ch in checks}


def test_b1_single_session_burst_does_not_flip_the_lane():
    # RAW prior inflated by a burst (1000 -> 3000, -67%); burst-adjusted flat.
    ch = _lane({"reach": (2000, 2000), "usage_raw": (1000, 3000),
                "usage_wins": (1000, 1050)})
    assert ch["ru_usage_wow"]["pass"] is True, ch["ru_usage_wow"]["detail"]
    assert ch["ru_conversion"]["pass"] is True, ch["ru_conversion"]["detail"]


def test_b2_real_multiday_decline_still_fails():
    # burst-adjusted WoW is still a steep drop -> must stay red.
    ch = _lane({"reach": (2000, 2000), "usage_raw": (1000, 3000),
                "usage_wins": (1000, 2900)})
    assert ch["ru_usage_wow"]["pass"] is False, ch["ru_usage_wow"]["detail"]


def test_b3_falls_back_to_raw_when_winsorized_unavailable():
    ch = _lane({"reach": (2000, 2000), "usage_raw": (1000, 3000),
                "usage_wins": None})
    assert ch["ru_usage_wow"]["pass"] is False, "lost the check when winsorized probe empty"


def test_b4_source_wires_winsorization_and_keeps_raw_display():
    src = open(SRC).read()
    assert "WITH sd AS" in src, "per-(session,day) winsorization query is gone"
    assert "LEAST(COUNT(*)," in src, "the per-session-day cap is gone"
    # gate on the burst-adjusted basis, not the raw sum
    assert re.search(r"_wow_pass\(\s*u7\s*,\s*up\s*\)", src), \
        "ru_usage_wow no longer gates on the burst-adjusted (u7/up) basis"
    # raw sum still shown (funnel-identical level)
    assert "{use_7d} real calls/7d" in src, "raw usage level no longer displayed"


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
