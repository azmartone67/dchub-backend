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


# ── escalation-only patterns (action (None, None)) reach the gate too ─
# run_cycle's escalation branch ran BEFORE _rate_limit_check, so on
# 2026-09-24 inspector_l22_handoff (6 of 143) wrote "no autonomous action"
# rows and never a lesson_gate one.
class _Conn:
    """Answers the gate's count query with `counts`; every dedupe probe
    ("have we recorded this before?") with no row."""

    def __init__(self, counts):
        self.counts = counts

    def cursor(self, *a, **k):
        conn = self

        class _C(_Cur):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def fetchone(self):
                last = self.sql[-1] if self.sql else ""
                return conn.counts if "autopilot_outcomes" in last else None
        return _C(None)

    def rollback(self):
        pass

    def close(self):
        pass


def test_escalation_only_helper_benches_and_escalates(monkeypatch):
    escalated = _isolate(monkeypatch)
    why = ap._lesson_gate_for_escalation_only(_Conn((6, 137)), "inspector_l22_handoff")
    assert why and why.startswith("lesson_gate")
    assert escalated == [("inspector_l22_handoff", why)]
    assert ap._lesson_gate_for_escalation_only(_Conn((311, 1)), "x") is None


def _drive_cycle(monkeypatch, counts, issue="inspector_l22_handoff"):
    """Run the real autopilot_run route over one escalation-only finding."""
    import io, json
    from flask import Flask
    _isolate(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(ap, "_admin_key", lambda: None)
    monkeypatch.setattr(ap, "_is_disabled", lambda: False)
    monkeypatch.setattr(ap, "_conn", lambda: _Conn(counts))
    monkeypatch.setattr(ap, "_lookup_pattern",
                        lambda i: {"action": lambda f: (None, None)})
    payload = {"actionable_backend_issues": [{"issue": issue, "url": "u"}]}
    monkeypatch.setattr(ap.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    from routes import brain_consistency_radar as R
    monkeypatch.setattr(R, "scan_all", lambda *a, **k: [])
    recorded = []
    monkeypatch.setattr(ap, "_record_action",
                        lambda f, p, *a, **k: recorded.append((p, k)))
    app = Flask(__name__)
    with app.test_request_context("/api/v1/brain/autopilot/run", method="POST"):
        ap.autopilot_run()
    return recorded


def test_run_cycle_records_lesson_gate_for_a_failing_escalation_only_pattern(monkeypatch):
    """MUTATION: remove the gate call from the `action_path is None` branch →
    the row is outcome='escalated', error='no autonomous action …'."""
    rec = _drive_cycle(monkeypatch, (6, 137))
    assert len(rec) == 1
    p, k = rec[0]
    assert p == "inspector_l22_handoff" and k["outcome"] == "rate_limited"
    assert k["error"].startswith("lesson_gate")


def test_run_cycle_still_escalates_a_working_escalation_only_pattern(monkeypatch):
    rec = _drive_cycle(monkeypatch, (311, 1), issue="schema_org_coverage_low")
    assert [(p, k["outcome"], k["error"]) for p, k in rec] == [
        ("schema_org_coverage_low", "escalated", "no autonomous action for this pattern")]


# ── the radar detector that would have caught it ─────────────────────
def test_leak_detector_flags_a_benched_pattern_still_escalating():
    """MUTATION: return [] unconditionally, or drop the bench predicate."""
    from routes import brain_consistency_radar as R
    out = R.lesson_gate_leak_findings([
        ("inspector_l22_handoff", 6, 137, 2),       # benched, leaked
        ("schema_org_coverage_low", 311, 1, 9),     # working — never flagged
        ("render_pipeline_blocked", 5, 30, 0),      # benched, gated → quiet
    ])
    assert [f["issue"] for f in out] == ["brain_lesson_gate_leak"]
    assert out[0]["count"] == 2
    assert "inspector_l22_handoff" in out[0]["detail"]
    assert "schema_org" not in out[0]["detail"]


def test_leak_detector_quiet_when_unreadable_or_gate_killed(monkeypatch):
    from routes import brain_consistency_radar as R
    assert R.lesson_gate_leak_findings(None) == []
    monkeypatch.setenv("BRAIN_LESSON_GATE_DISABLED", "1")
    assert R.lesson_gate_leak_findings([("inspector_l22_handoff", 6, 137, 2)]) == []


def test_leak_detector_is_registered():
    import ast, inspect
    from routes import brain_consistency_radar as R
    names = {n.id for n in ast.walk(ast.parse(inspect.getsource(R.scan_all)))
             if isinstance(n, ast.Name)}
    assert "check_brain_lesson_gate_reaches_benched_patterns" in names
