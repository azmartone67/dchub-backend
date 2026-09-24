"""The autopilot lesson gate: bench a pattern whose verified effect rate is at
or below the lessons compiler's "fails" line. Each test names its mutation."""
import pytest

from routes import brain_autopilot as ap
from routes import brain_lessons as bl


# ── the pure predicate, on the 2026-09-24 live numbers (90d lessons) ──
@pytest.mark.parametrize("ok,bad,benched", [
    (6, 137, True),      # inspector_l22_handoff
    (5, 35, True),       # competitor_announcement
    (23, 84, True),      # render_pipeline_blocked, 21%
    (311, 1, False),     # schema_org_coverage_low — must never be benched
    (42, 1, False),      # autonomy_proactive
    (8, 12, False),      # 40% — above the line
])
def test_bench_matches_the_lessons_fails_line(ok, bad, benched):
    assert (ap.lesson_bench_reason(ok, bad) is not None) is benched


def test_the_line_is_the_lessons_own_constant(monkeypatch):
    """MUTATION: hardcode 0.30 → a change to the lessons' definition of
    'fails' would silently stop applying to the autopilot."""
    monkeypatch.setattr(bl, "FAILS_AT", 0.10)
    assert ap.lesson_bench_reason(5, 35) is None          # 12.5% > 10%
    assert ap.lesson_bench_reason(2, 38) is not None      # 5%


def test_thin_samples_never_bench():
    """MUTATION: drop the minimum → one failed run benches a pattern."""
    assert ap.lesson_bench_reason(0, 19) is None
    assert ap.lesson_bench_reason(0, 20) is not None


def test_unknown_counts_never_bench():
    """A failed read is UNKNOWN, never 0 (the file's flattering-zero rule)."""
    assert ap.lesson_bench_reason(None, 50) is None
    assert ap.lesson_bench_reason(5, None) is None


def test_reason_carries_the_evidence():
    r = ap.lesson_bench_reason(6, 137)
    assert "6 of 143" in r and "escalated" in r


# ── wiring: the gate runs inside _rate_limit_check and escalates ─────
class _Cur:
    def __init__(self, row):
        self.row, self.sql = row, []
        self.connection = self

    def execute(self, sql, args=None):
        self.sql.append(sql)

    def fetchone(self):
        return self.row

    def rollback(self):
        pass


def _isolate(monkeypatch):
    monkeypatch.setattr(ap, "_quarantined_patterns", lambda: frozenset())
    monkeypatch.setattr(ap, "_recidivism_check", lambda cur, p: None)
    escalated = []
    monkeypatch.setattr(ap, "_escalate_recidivism_once",
                        lambda cur, p, r, n=None: escalated.append((p, r)))
    return escalated


def test_rate_limit_check_refuses_and_escalates_a_failing_pattern(monkeypatch):
    """MUTATION: remove the gate from _rate_limit_check → allowed=True."""
    escalated = _isolate(monkeypatch)
    allowed, why = ap._rate_limit_check(_Cur((6, 137)), "inspector_l22_handoff", None)
    assert allowed is False and why.startswith("lesson_gate")
    assert escalated and escalated[0][0] == "inspector_l22_handoff"


def test_kill_switch_lets_it_through_to_the_next_gate(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("BRAIN_LESSON_GATE_DISABLED", "1")
    assert ap._lesson_gate_check(_Cur((6, 137)), "inspector_l22_handoff") is None


def test_a_working_pattern_is_not_stopped_by_the_gate(monkeypatch):
    _isolate(monkeypatch)
    assert ap._lesson_gate_check(_Cur((311, 1)), "schema_org_coverage_low") is None
