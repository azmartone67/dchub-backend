"""tests/test_adoption_master_shell.py — the adoption shell's own mutation test.

★ WHY THIS FILE EXISTS. A board that cannot render FAIL is decoration, and this
repo has shipped one: a shell compared lane verdicts against invented
"RED"/"GREEN" literals that could never match the "FAIL"/"?"/"PASS" the shared
_lane_verdict actually returns, so every lane read green forever.

So every test here is a MUTATION on the REAL run path: it feeds the real lane
functions a scripted database, flips ONE input, and asserts the verdict moves.
A lane that stays the same colour under a mutation is a lane that cannot fail.

The scripted connection is deliberately dumb — it matches on SQL substrings and
returns rows. It is NOT a database emulator; it exists so the lane logic, the
imported _check/_lane_verdict primitives and the three-valued rendering are all
exercised for real.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DCHUB_ADMIN_KEY", "test-admin-key")

from routes import adoption_master_shell as ams  # noqa: E402
from routes.brain_ascension_master_shell import _lane_verdict  # noqa: E402


# ── scripted connection ───────────────────────────────────────────────

class _Cur:
    def __init__(self, rules):
        self._rules = rules
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        for frag, rows in self._rules:
            if frag in " ".join(sql.split()):
                self._rows = list(rows)
                return
        raise AssertionError("unscripted SQL: " + " ".join(sql.split())[:160])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n=1000):
        return self._rows[:n]

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rules):
        self._rules = rules

    def cursor(self):
        return _Cur(self._rules)

    def rollback(self):
        pass

    def close(self):
        pass


def _oauth(mature, returned):
    return ("dch_oauth_", [(mature, returned)])


def _free(mature, returned):
    return ("dch_live_", [(mature, returned)])


_TRIAL_BOUND = ("operator_email IS NOT NULL AND operator_email <> ''",
                [(0, 0)])
_TRIAL_ONLY = ("operator_email IS NULL OR operator_email = ''", [(0, 0)])


# ══ lane 1 · identity durability ══════════════════════════════════════

def test_identity_lane_is_red_at_the_measured_40x_gap():
    """The live shape: OAuth 4/7 return, free 6/462. Durable identity is a
    MINORITY of returners (4 of 10) — the lane must be FAIL."""
    c = _Conn([_oauth(7, 4), _free(462, 6), _TRIAL_BOUND, _TRIAL_ONLY])
    checks = ams._lane_identity_durability(c)
    assert _lane_verdict(checks) == "FAIL"
    gate = next(k for k in checks if k["id"] == "id_durable_majority")
    assert gate["pass"] is False
    assert gate["critical"] is True


def test_identity_lane_goes_green_only_when_durable_is_the_majority():
    """MUTATION: same free cohort, OAuth returns now outnumber key-only ones.
    If this does not flip to PASS the gate is unreachable and the lane is a
    decoration."""
    c = _Conn([_oauth(60, 40), _free(462, 6), _TRIAL_BOUND, _TRIAL_ONLY])
    checks = ams._lane_identity_durability(c)
    assert _lane_verdict(checks) == "PASS"
    assert next(k for k in checks
                if k["id"] == "id_durable_majority")["pass"] is True


def test_identity_lane_cannot_pass_vacuously_on_zero_returners():
    """A cohort with NO returners must render '?', never PASS. A flattering
    zero denominator is the exact bug this board exists to catch."""
    c = _Conn([_oauth(7, 0), _free(462, 0), _TRIAL_BOUND, _TRIAL_ONLY])
    checks = ams._lane_identity_durability(c)
    assert _lane_verdict(checks) == "?"
    assert next(k for k in checks
                if k["id"] == "id_durable_majority")["pass"] is None


def test_identity_lane_reports_unreadable_as_indeterminate_not_zero():
    """A failed read must NOT be rendered as 'nobody returned'."""
    class _Boom(_Conn):
        def cursor(self):
            raise RuntimeError("connection reset")

    checks = ams._lane_identity_durability(_Boom([]))
    assert _lane_verdict(checks) == "?"
    assert all(k["pass"] is None for k in checks)


# ══ lane 2 · activation ═══════════════════════════════════════════════

_NO_STAMP = ("information_schema.columns", [(0,)])
_HAS_STAMP = ("information_schema.columns", [(1,)])
_MINTED = ("FROM mcp_dev_keys WHERE created_at", [(748, 309)])
_MINTED_TRIAL = ("FROM auto_trial_keys WHERE minted_at", [(0, 0)])


def test_activation_is_unmeasured_without_a_first_call_stamp():
    c = _Conn([_NO_STAMP, _MINTED, _MINTED_TRIAL])
    checks = ams._lane_activation(c)
    assert _lane_verdict(checks) == "?"
    cliff = next(k for k in checks if k["id"] == "act_cliff")
    assert cliff["pass"] is None and cliff["critical"] is True
    # ...and the honest, answerable part is still reported with its basis.
    ever = next(k for k in checks if k["id"] == "act_ever")
    assert "309 of 748" in ever["detail"]
    assert "NOT the cliff" in ever["detail"]


def test_activation_stays_unmeasured_even_once_the_column_exists():
    """MUTATION: the instrumentation lands. The lane must STILL not claim a
    measured cliff — this shell has not been taught to read the column, and a
    column no board reads is not a measurement."""
    c = _Conn([_HAS_STAMP, _MINTED, _MINTED_TRIAL])
    checks = ams._lane_activation(c)
    cliff = next(k for k in checks if k["id"] == "act_cliff")
    assert cliff["pass"] is None
    assert "INSTRUMENTATION PRESENT" in cliff["detail"]
    assert _lane_verdict(checks) == "?"


def test_activation_never_reports_a_zero_percent_on_an_empty_window():
    c = _Conn([_NO_STAMP, ("FROM mcp_dev_keys WHERE created_at", [(0, 0)]),
               _MINTED_TRIAL])
    ever = next(k for k in ams._lane_activation(c) if k["id"] == "act_ever")
    assert ever["pass"] is None
    assert "UNMEASURED, not 0%" in ever["detail"]


# ══ lane 3 · conversion ═══════════════════════════════════════════════

# ORDER MATTERS: the human-paid query also reads mcp_high_intent_sessions, so
# its distinctive fragment must be matched FIRST or the gate rule swallows it.
_NO_HUMAN_PAID = ("COUNT(DISTINCT s.id)", [(0,)])
_HUMAN_PAID = ("COUNT(DISTINCT s.id)", [(2,)])
_GATE = ("FROM mcp_high_intent_sessions", [(273, 118, 0, 3, 118)])
_RAIL_PRESENT = ("to_regclass('public.x402_unlocks')", [(True,)])
_RAIL_ABSENT = ("to_regclass('public.x402_unlocks')", [(False,)])
_NO_MACHINE_PAID = ("FROM x402_unlocks", [(0, 0, 0)])
_MACHINE_PAID = ("FROM x402_unlocks", [(9, 3, 45.0)])


def test_conversion_is_red_when_neither_path_pays():
    c = _Conn([_NO_HUMAN_PAID, _GATE, _RAIL_PRESENT, _NO_MACHINE_PAID])
    checks = ams._lane_conversion(c)
    assert _lane_verdict(checks) == "FAIL"
    assert next(k for k in checks if k["id"] == "cv_human_paid")["pass"] is False
    assert next(k for k in checks
                if k["id"] == "cv_machine_paid")["pass"] is False


def test_conversion_gates_the_two_paths_separately():
    """MUTATION: the MACHINE path starts paying while the human path stays at
    zero. The machine check must flip and the human check must NOT — a single
    combined number would have hidden exactly this."""
    c = _Conn([_NO_HUMAN_PAID, _GATE, _RAIL_PRESENT, _MACHINE_PAID])
    checks = ams._lane_conversion(c)
    assert next(k for k in checks if k["id"] == "cv_machine_paid")["pass"] is True
    assert next(k for k in checks if k["id"] == "cv_human_paid")["pass"] is False
    assert _lane_verdict(checks) == "FAIL"   # human path still red

    c2 = _Conn([_HUMAN_PAID, _GATE, _RAIL_PRESENT, _MACHINE_PAID])
    checks2 = ams._lane_conversion(c2)
    assert _lane_verdict(checks2) == "PASS"


def test_conversion_separates_unmeasurable_machine_path_from_measured_zero():
    """An absent ledger is NOT a measured zero — they must render differently."""
    c = _Conn([_NO_HUMAN_PAID, _GATE, _RAIL_ABSENT])
    checks = ams._lane_conversion(c)
    mp = next(k for k in checks if k["id"] == "cv_machine_paid")
    assert mp["pass"] is None
    assert "DIFFERENT finding from 'measured zero'" in mp["detail"]


def test_conversion_reports_the_gate_funnel_with_its_basis():
    c = _Conn([_NO_HUMAN_PAID, _GATE, _RAIL_PRESENT, _NO_MACHINE_PAID])
    gate = next(k for k in ams._lane_conversion(c) if k["id"] == "cv_gate")
    assert "273" in gate["detail"] and "118" in gate["detail"]
    assert "rolling" in gate["detail"]


# ══ lane 4 · questions retired ════════════════════════════════════════

_RE_PRESENT = ("to_regclass('public.recipe_executions')", [(True,)])


def _exec_rows(rows):
    return ("FROM recipe_executions", rows)


# (intent_class, executed+gated, failed, skipped, not_run, outcome, age_min)
def _run(cls, tools, failed=0, skipped=0, not_run=0,
         outcome="completed", age=99.0):
    return (cls, tools, failed, skipped, not_run, outcome, age)


def test_questions_retired_is_red_when_workflows_only_partially_close():
    c = _Conn([_RE_PRESENT, _exec_rows([
        _run("market_ranking", 4, skipped=1),
        _run("market_ranking", 3, failed=1),
        _run("market_comparison", 5, not_run=2),
    ])])
    checks = ams._lane_questions_retired(c)
    assert _lane_verdict(checks) == "FAIL"
    roll = next(k for k in checks if k["id"] == "qr_rollup")
    assert roll["pass"] is False


def test_questions_retired_goes_green_when_one_workflow_actually_closes():
    """MUTATION: the SAME workflows, now with nothing deferred and nothing
    failed. If this does not flip the lane, closure is unmeasurable."""
    c = _Conn([_RE_PRESENT, _exec_rows([
        _run("market_ranking", 4),
        _run("market_ranking", 3),
        _run("market_comparison", 5),
    ])])
    checks = ams._lane_questions_retired(c)
    assert _lane_verdict(checks) == "PASS"
    mk = next(k for k in checks if k["id"] == "qr_market_selection")
    assert mk["pass"] is True
    assert "median tools used 3.5" in mk["detail"]


def test_a_problem_with_no_runs_is_unmeasured_not_zero_percent():
    c = _Conn([_RE_PRESENT, _exec_rows([_run("market_ranking", 4)])])
    checks = ams._lane_questions_retired(c)
    fib = next(k for k in checks if k["id"] == "qr_fiber_power_pairing")
    assert fib["pass"] is None
    assert "UNMEASURED" in fib["detail"]
    assert "NOT a closure rate of 0" in fib["detail"]


def test_in_flight_workflows_are_excluded_not_counted_as_failures():
    """A workflow younger than the canonical abandonment threshold has not
    failed — counting it would manufacture a red."""
    c = _Conn([_RE_PRESENT, _exec_rows([
        _run("market_ranking", 4),
        _run("market_ranking", 0, outcome=None, age=1.0),
    ])])
    mk = next(k for k in ams._lane_questions_retired(c)
              if k["id"] == "qr_market_selection")
    assert "of 1 run(s)" in mk["detail"]
    assert "1 in flight, excluded" in mk["detail"]


def test_no_runs_at_all_renders_unmeasured_never_pass():
    c = _Conn([_RE_PRESENT, _exec_rows([])])
    checks = ams._lane_questions_retired(c)
    assert _lane_verdict(checks) == "?"
    assert next(k for k in checks if k["id"] == "qr_rollup")["pass"] is None


def test_contract_drift_guard_fails_on_an_unmapped_published_problem(
        monkeypatch):
    """MUTATION on the guard itself: publish a new canonical problem with no
    measurement mapping. The lane must go RED rather than quietly measure the
    problems it happens to know about."""
    import routes.anchor_intents as ai
    monkeypatch.setattr(ai, "ANCHORS", ai.ANCHORS + (
        {"recipe": "brand_new_problem", "intent": "something new"},))
    c = _Conn([_RE_PRESENT, _exec_rows([_run("market_ranking", 4)])])
    checks = ams._lane_questions_retired(c)
    guard = next(k for k in checks if k["id"] == "qr_contract")
    assert guard["pass"] is False
    assert "brand_new_problem" in guard["detail"]
    assert _lane_verdict(checks) == "FAIL"


def test_contract_guard_is_green_on_the_shipped_contract():
    """The other half of the mutation: unmutated, the guard must PASS — a
    guard that is always red is as useless as one that is always green."""
    c = _Conn([_RE_PRESENT, _exec_rows([_run("market_ranking", 4)])])
    guard = next(k for k in ams._lane_questions_retired(c)
                 if k["id"] == "qr_contract")
    assert guard["pass"] is True


# ══ board wiring ══════════════════════════════════════════════════════

def test_lane_verdicts_use_the_shared_vocabulary_not_invented_literals():
    """The #24 regression, pinned: verdicts are FAIL/?/PASS. Any comparison
    against RED/GREEN in this shell would be dead code."""
    src = open(ams.__file__, encoding="utf-8").read()
    # No comparison against a verdict literal ANY value can never take.
    for dead in ('== "RED"', '== "GREEN"', "== 'RED'", "== 'GREEN'",
                 'in ("RED"', 'in ("GREEN"'):
        assert dead not in src, f"dead comparison in shell: {dead}"
    # And the verdicts this shell renders come from the shared helper, so the
    # three literals it can produce are exactly these.
    assert _lane_verdict([ams._check("x", "x", False, "d")]) == "FAIL"
    assert _lane_verdict([ams._check("x", "x", None, "d", True)]) == "?"
    assert _lane_verdict([ams._check("x", "x", True, "d")]) == "PASS"


def test_check_dict_is_keyed_pass():
    k = ams._check("id", "name", True, "detail", critical=True)
    assert k["pass"] is True and k["critical"] is True


def test_tick_requires_an_admin_key_and_is_never_cached():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ams.adoption_master_shell_bp)
    cl = app.test_client()
    assert cl.get("/api/v1/admin/adoption/master-tick").status_code == 401
    r = cl.get("/api/v1/admin/adoption/master-tick?admin_key=test-admin-key")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["window"]["kind"] == "rolling"
    assert set(body["born_red"]) == {"identity_durability", "conversion",
                                     "questions_retired"}
    assert set(ln["verdict"] for ln in body["lanes"]) <= {"PASS", "FAIL", "?"}


def test_kill_switch_returns_404_never_5xx(monkeypatch):
    """A 5xx here would make the CF worker fail the whole SITE over to the
    stale Render origin. Turning off a diagnostic board must not do that."""
    from flask import Flask
    monkeypatch.setenv("ADOPTION_SHELL_DISABLE", "1")
    app = Flask(__name__)
    app.register_blueprint(ams.adoption_master_shell_bp)
    cl = app.test_client()
    assert cl.get(
        "/api/v1/admin/adoption/master-tick?admin_key=test-admin-key"
    ).status_code == 404
    assert cl.get("/admin/adoption?admin_key=test-admin-key").status_code == 404


def test_main_registers_the_blueprint():
    """L1-adjacent: the shell exists in main.py's registration chain. An
    unregistered blueprint is unreachable code that still passes every unit
    test in this file."""
    import pathlib
    src = pathlib.Path(ams.__file__).parent.parent / "main.py"
    txt = src.read_text(encoding="utf-8")
    assert "from routes.adoption_master_shell import adoption_master_shell_bp" \
        in txt
    assert "app.register_blueprint(adoption_master_shell_bp)" in txt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
