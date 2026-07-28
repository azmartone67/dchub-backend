"""SLO rollback sentinel — the probe that moved off GitHub cron to the worker.

Unit-level (no main import, no DB, no network). What is asserted here is the
decision contract, because this is the thing allowed to roll production back:
how many samples it needs, that a cooldown really suppresses, that it stays
disarmed without an explicit arm, and that a read failure never actuates.
"""

import pathlib

import pytest

from routes import slo_rollback_sentinel as sent

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Fresh sentinel state and a disarmed, tokenless environment per test."""
    sent._STATE.update({
        "samples": [], "last_verdict": None, "last_checked": None,
        "last_action": None, "last_action_at": 0.0, "actions": 0, "errors": 0,
    })
    monkeypatch.delenv("SLO_SENTINEL_ROLLBACK", raising=False)
    monkeypatch.delenv("RAILWAY_TOKEN", raising=False)
    monkeypatch.setattr(sent, "_file_finding", lambda *a, **k: None)
    yield


def feed(monkeypatch, verdicts, n5xx=150, pattern="/api/v1/thing"):
    """Drive check_once() once per verdict, with compute_budget stubbed."""
    seq = list(verdicts)

    def fake_budget():
        v = seq.pop(0)
        return {
            "verdict": v,
            "top_5xx_paths": [{"pattern": pattern, "n5xx": n5xx}] if v != "within_budget" else [],
        }, (503 if v == "hard_burn" else 200)

    import routes.slo_error_budget as budget_mod
    monkeypatch.setattr(budget_mod, "compute_budget", fake_budget)
    return [sent.check_once() for _ in range(len(verdicts))]


class TestTriggerContract:
    def test_three_of_five_hard_burn_triggers(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sent, "_rollback", lambda: (calls.append(1), ("rolled-back", "ok"))[1])
        monkeypatch.setattr(sent, "_armed", lambda: True)
        feed(monkeypatch, ["hard_burn"] * 3)
        assert sent._STATE["last_action"]["action"] == "rolled-back"
        assert len(calls) == 1

    def test_two_hard_burns_do_not_trigger(self, monkeypatch):
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: pytest.fail("must not roll back on 2/5"))
        feed(monkeypatch, ["hard_burn", "within_budget", "hard_burn"])
        assert sent._STATE["last_action"] is None

    def test_soft_burn_never_triggers(self, monkeypatch):
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: pytest.fail("soft_burn must not roll back"))
        feed(monkeypatch, ["soft_burn"] * 5)
        assert sent._STATE["last_action"] is None

    def test_window_is_rolling_not_cumulative(self, monkeypatch):
        """3 hard burns spread beyond the 5-sample window must not accumulate."""
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: pytest.fail("stale samples must age out"))
        feed(monkeypatch, [
            "hard_burn",
            "within_budget", "within_budget", "within_budget", "within_budget",
            "hard_burn",
            "within_budget", "within_budget", "within_budget", "within_budget",
            "hard_burn",
        ])
        assert sent._STATE["last_action"] is None
        assert len(sent._STATE["samples"]) <= sent.WINDOW_N


class TestCooldown:
    def test_cooldown_suppresses_a_second_rollback(self, monkeypatch):
        n = []
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: (n.append(1), ("rolled-back", "ok"))[1])
        feed(monkeypatch, ["hard_burn"] * 3)
        assert len(n) == 1
        # brain_http_errors still holds the pre-rollback 5xx for its 5-min
        # window, so the next samples are still hard_burn. Must not re-fire.
        feed(monkeypatch, ["hard_burn"] * 3)
        assert len(n) == 1, "cooldown must suppress a repeat rollback"

    def test_cooldown_expiry_allows_acting_again(self, monkeypatch):
        n = []
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: (n.append(1), ("rolled-back", "ok"))[1])
        feed(monkeypatch, ["hard_burn"] * 3)
        sent._STATE["last_action_at"] -= (sent.COOLDOWN_S + 1)
        feed(monkeypatch, ["hard_burn"] * 3)
        assert len(n) == 2

    def test_window_cleared_after_acting(self, monkeypatch):
        """The next decision needs fresh evidence, not the samples that fired."""
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: ("rolled-back", "ok"))
        feed(monkeypatch, ["hard_burn"] * 3)
        assert sent._STATE["samples"] == []


class TestDisarmedByDefault:
    def test_disarmed_detects_but_does_not_roll_back(self, monkeypatch):
        monkeypatch.setattr(sent, "_rollback", lambda: pytest.fail("must not actuate while disarmed"))
        feed(monkeypatch, ["hard_burn"] * 3)
        assert sent._STATE["last_action"]["action"] == "detected-disarmed"

    def test_arm_needs_both_flag_and_token(self, monkeypatch):
        assert sent._armed() is False
        monkeypatch.setenv("SLO_SENTINEL_ROLLBACK", "1")
        assert sent._armed() is False, "flag alone must not arm"
        monkeypatch.setenv("RAILWAY_TOKEN", "tok")
        assert sent._armed() is True

    def test_token_alone_does_not_arm(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_TOKEN", "tok")
        assert sent._armed() is False, "a token present for other jobs must not arm rollback"

    @pytest.mark.parametrize("flag,expected", [
        ("1", True), ("true", True), ("YES", True),
        ("0", False), ("false", False), ("", False), ("maybe", False),
    ])
    def test_arm_flag_parsing(self, monkeypatch, flag, expected):
        monkeypatch.setenv("RAILWAY_TOKEN", "tok")
        monkeypatch.setenv("SLO_SENTINEL_ROLLBACK", flag)
        assert sent._armed() is expected


class TestFailureModes:
    def test_unreadable_budget_never_actuates(self, monkeypatch):
        """A DB error must not look like a burn."""
        import routes.slo_error_budget as budget_mod
        monkeypatch.setattr(budget_mod, "compute_budget", lambda: ({"ok": False, "reason": "no_db"}, 503))
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: pytest.fail("must not act on an unreadable budget"))
        for _ in range(5):
            assert sent.check_once() is None
        assert sent._STATE["errors"] == 5
        assert sent._STATE["samples"] == []

    def test_rollback_failure_is_recorded_not_raised(self, monkeypatch):
        monkeypatch.setattr(sent, "_armed", lambda: True)
        monkeypatch.setattr(sent, "_rollback", lambda: ("rollback-failed", "Railway API error: boom"))
        feed(monkeypatch, ["hard_burn"] * 3)
        assert sent._STATE["last_action"]["action"] == "rollback-failed"
        # cooldown still engages, so a broken Railway API cannot spin
        assert sent._STATE["last_action_at"] > 0


class TestNoDriftWithTheEndpoint:
    def test_sentinel_uses_the_endpoints_own_verdict_function(self):
        """Two copies of the threshold logic would be a drift class.

        The endpoint and the thing that rolls production back must never
        disagree about what hard_burn means.
        """
        src = (ROOT / "routes" / "slo_rollback_sentinel.py").read_text()
        assert "from routes.slo_error_budget import compute_budget" in src
        for lit in ("PER_PATH_5MIN_HARD", "worst_path_n5xx >="):
            assert lit not in src, (
                f"sentinel reimplements threshold logic ({lit}); it must call "
                "compute_budget() instead"
            )

    def test_endpoint_still_serves_through_compute_budget(self):
        src = (ROOT / "routes" / "slo_error_budget.py").read_text()
        assert "def compute_budget(" in src
        assert "payload, status = compute_budget()" in src, (
            "the route must delegate to compute_budget so both paths grade identically"
        )

    def test_rollback_logic_is_reused_not_reimplemented(self):
        """The tested Railway contract lives in scripts/railway_rollback.py."""
        src = (ROOT / "routes" / "slo_rollback_sentinel.py").read_text()
        assert "railway_rollback.py" in src
        assert "deploymentRollback(id:" not in src, (
            "sentinel must not carry its own copy of the GraphQL mutation"
        )


class TestWorkerOnly:
    def test_main_gates_the_loop_on_the_worker_role(self):
        """A web-role replica must not run the loop.

        dchub-worker is a separate Railway service, so it keeps grading while
        the web service is 5xxing — that isolation is the point of the move.
        """
        src = (ROOT / "main.py").read_text()
        i = src.find("slo_rollback_sentinel")
        assert i > 0, "sentinel is not registered in main.py"
        block = src[i:i + 900]
        assert "_ROLE_RUNS_BG" in block, "sentinel loop must be role-gated to the worker"
        assert "_slo_sentinel_start()" in block
