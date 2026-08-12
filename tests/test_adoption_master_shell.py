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

★ NEVER set DCHUB_ADMIN_KEY at module scope here. It leaked once: pytest
imports this file before tests/test_funnel_consistency.py, whose skipif reads
DCHUB_ADMIN_KEY AT IMPORT TIME — setting it un-skipped two live-network tests
that then 401'd against production. The key is monkeypatched per-test instead,
so it exists only inside the tests that need it.
"""
from __future__ import annotations

import pytest

from routes import adoption_master_shell as ams  # noqa: E402
from routes.brain_ascension_master_shell import _lane_verdict  # noqa: E402

_ADMIN_KEY = "test-admin-key"


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


def _by_id(checks, cid):
    """The check with this id, or None if the lane never emitted it. Absence
    is meaningful: a lane that short-circuits on a broken guard must NOT go on
    to publish the number that guard was protecting."""
    return next((k for k in checks if k["id"] == cid), None)


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


def test_tick_requires_an_admin_key_and_is_never_cached(monkeypatch):
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _ADMIN_KEY)
    # The citation lane's send-side probe is the only outbound call the board
    # makes. A unit suite must not depend on the live edge, so it is killed
    # here — the lane then renders UNMEASURED, which is the correct answer.
    monkeypatch.setenv("ADOPTION_CITATION_PROBE_DISABLE", "1")
    app = Flask(__name__)
    app.register_blueprint(ams.adoption_master_shell_bp)
    cl = app.test_client()
    assert cl.get("/api/v1/admin/adoption/master-tick").status_code == 401
    r = cl.get("/api/v1/admin/adoption/master-tick?admin_key=" + _ADMIN_KEY + "")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["window"]["kind"] == "rolling"
    assert set(body["red_by_design"]) == {
        "identity_durability", "conversion",
        "lookup_vs_workflow", "citation_survival"}
    # Measured state is DERIVED from the verdicts, never asserted alongside
    # them — the two can never disagree.
    assert body["red_now"] == [ln["id"] for ln in body["lanes"]
                               if ln["verdict"] == "FAIL"]
    assert body["unmeasured_now"] == [ln["id"] for ln in body["lanes"]
                                      if ln["verdict"] == "?"]
    assert set(ln["verdict"] for ln in body["lanes"]) <= {"PASS", "FAIL", "?"}


def test_kill_switch_returns_404_never_5xx(monkeypatch):
    """A 5xx here would make the CF worker fail the whole SITE over to the
    stale Render origin. Turning off a diagnostic board must not do that."""
    from flask import Flask
    monkeypatch.setenv("ADOPTION_SHELL_DISABLE", "1")
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _ADMIN_KEY)
    app = Flask(__name__)
    app.register_blueprint(ams.adoption_master_shell_bp)
    cl = app.test_client()
    assert cl.get(
        "/api/v1/admin/adoption/master-tick?admin_key=" + _ADMIN_KEY + ""
    ).status_code == 404
    assert cl.get("/admin/adoption?admin_key=" + _ADMIN_KEY + "").status_code == 404


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


# ══ lane 5 · lookup vs workflow ═══════════════════════════════════════
#
# ★ The mutation this lane exists to survive: make a WORKFLOW count as
# LOOKUPS and watch the board go red. If reclassifying the front door as a
# lookup left the verdict alone, the lane would not be measuring routing.

_LW_GUARD_SQL = "COUNT(*) FILTER (WHERE client_ip"
_LW_COUNTS_SQL = "GROUP BY 1"
_LW_LOOKUP_ONLY_SQL = "wf_agents AS (SELECT DISTINCT agent_id FROM pop"
_LW_AGENTS_SQL = "COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL)"


def _lw_conn(platform_rows, loopback=(780, 0), agents=(265, 11),
             lookup_only=(("search_facilities", 4264, 218),)):
    """A scripted population. platform_rows = (platform, wf, lk, nav, agents)."""
    return _Conn([
        (_LW_GUARD_SQL, [loopback]),
        (_LW_LOOKUP_ONLY_SQL, list(lookup_only)),
        (_LW_AGENTS_SQL, [agents]),
        (_LW_COUNTS_SQL, list(platform_rows)),
    ])


# The live 30d shape measured 2026-08-12: one integration runs the front door,
# every high-volume platform runs pure lookups.
_LIVE_SHAPE = [
    ("mcp-generic-client", 4, 9158, 4, 204),
    ("smithery connect", 0, 3260, 0, 1),
    ("datacolo", 0, 2560, 0, 2),
    ("anthropic/api", 0, 461, 0, 6),
    ("claude", 4, 142, 5, 13),
    ("connectors-manager", 27, 33, 6, 20),
]


def test_lookup_vs_workflow_is_red_on_the_measured_platform_shape():
    """1 of 6 measurable platforms clears the front-door floor. The lane is
    FAIL, and it is FAIL on the PER-PLATFORM shape, not on a global rate."""
    checks = ams._lane_lookup_vs_workflow(_lw_conn(_LIVE_SHAPE))
    assert _lane_verdict(checks) == "FAIL"
    maj = _by_id(checks, "lw_platform_majority")
    assert maj["pass"] is False
    assert maj["critical"] is True
    assert "connectors-manager" in maj["detail"]


def test_a_workflow_counted_as_a_lookup_turns_the_lane_red():
    """★ THE MUTATION THE TASK NAMES. Reclassify execute_plan as a lookup —
    the one platform that WAS clearing the floor must stop clearing it."""
    before = ams._lane_lookup_vs_workflow(_lw_conn(_LIVE_SHAPE))
    assert "clearing: connectors-manager" in _by_id(
        before, "lw_platform_majority")["detail"]

    orig = ams._WORKFLOW_TOOL
    ams._WORKFLOW_TOOL = "__not_a_tool__"
    try:
        # ★ Assert the mutation LANDED before believing the result.
        assert ams._WORKFLOW_TOOL != orig
        # Every workflow row now falls into the lookup bucket.
        mutated = [(p, 0, wf + lk, nav, ag)
                   for p, wf, lk, nav, ag in _LIVE_SHAPE]
        after = ams._lane_lookup_vs_workflow(_lw_conn(mutated))
        maj = _by_id(after, "lw_platform_majority")
        assert maj["pass"] is False
        assert "NONE clear it" in maj["detail"]
    finally:
        ams._WORKFLOW_TOOL = orig
    assert ams._WORKFLOW_TOOL == "execute_plan"


def test_subcall_guard_fails_when_sub_calls_leak_into_the_lookup_count():
    """If a workflow's own fan-out were counted as lookups, the ratio would
    measure ITSELF — running more workflows would raise the lookup side."""
    checks = ams._lane_lookup_vs_workflow(
        _lw_conn(_LIVE_SHAPE, loopback=(780, 780)))
    g = _by_id(checks, "lw_subcall_guard")
    assert g["pass"] is False
    assert g["critical"] is True
    assert _lane_verdict(checks) == "FAIL"
    # A broken guard must STOP the lane, not let it publish a ratio anyway.
    assert _by_id(checks, "lw_platform_majority") is None


def test_subcall_guard_cannot_pass_vacuously_on_an_empty_population():
    """★ A guard that goes green with nothing to check is worse than no guard.
    No loopback rows at all -> UNMEASURED, never PASS."""
    checks = ams._lane_lookup_vs_workflow(
        _lw_conn(_LIVE_SHAPE, loopback=(0, 0)))
    g = _by_id(checks, "lw_subcall_guard")
    assert g["pass"] is None
    assert "nothing to bite on" in g["detail"]


def test_thin_platform_samples_are_withheld_not_published():
    """A ratio over a handful of calls is a coincidence wearing a percentage
    sign. Every platform below the imported floor must be withheld, and the
    verdict must render UNMEASURED rather than a flattering 0%."""
    thin = [("cursor", 0, 3, 0, 2), ("gemini-cli", 1, 1, 0, 1)]
    checks = ams._lane_lookup_vs_workflow(_lw_conn(thin))
    maj = _by_id(checks, "lw_platform_majority")
    assert maj["pass"] is None
    assert maj["critical"] is True
    assert "never a 0% front-door share" in maj["detail"]


def test_withholding_floor_is_read_live_not_snapshotted():
    """The floor is IMPORTED and read at call time. A copy would rot on its
    own schedule and gate `measurable` at a floor its own withholding rule no
    longer used."""
    import routes.problems_solved as ps
    orig = ps._MIN_RUNS
    ps._MIN_RUNS = 1_000_000
    try:
        assert ams._lw_min_runs() == 1_000_000, "the floor was snapshotted"
        checks = ams._lane_lookup_vs_workflow(_lw_conn(_LIVE_SHAPE))
        assert _by_id(checks, "lw_platform_majority")["pass"] is None
    finally:
        ps._MIN_RUNS = orig
    assert ams._lw_min_runs() == orig


def test_window_mismatch_refuses_to_publish_any_ratio():
    """Both sides must be counted on ONE window. If the imported withheld-
    reason text names a different window, the ratios are refused rather than
    published with a false basis — the 2,300-vs-16 failure mode."""
    import routes.problems_solved as ps
    orig = ps._WINDOW_DAYS
    ps._WINDOW_DAYS = 7
    try:
        assert ams._lw_ps_window_days() == 7
        checks = ams._lane_lookup_vs_workflow(_lw_conn(_LIVE_SHAPE))
        w = _by_id(checks, "lw_withhold")
        assert w["pass"] is False
        assert "WINDOW MISMATCH" in w["detail"]
        assert _by_id(checks, "lw_counts") is None
    finally:
        ps._WINDOW_DAYS = orig


def test_classification_of_navigation_and_subcalls_is_published():
    """Requirement: a reader must be able to tell which side any row lands on
    WITHOUT reading the source."""
    block = ams._lookup_vs_workflow_block(
        ams._lane_lookup_vs_workflow(_lw_conn(_LIVE_SHAPE)))
    cls = block["classification"]
    assert set(cls) >= {"workflow", "lookup", "workflow_sub_calls",
                        "plan_query", "discover_tools"}
    assert "NEITHER" in cls["workflow_sub_calls"]
    assert block["window"]["same_window_both_sides"] is True


# ══ lane 6 · citation survival ════════════════════════════════════════

def test_citation_lane_can_never_render_pass(monkeypatch):
    """★ THE POINT OF THE LANE. Even with a PERFECT send side, citation
    survival stays unobservable — so PASS is unreachable by construction. A
    green lane here would claim something we cannot support."""
    monkeypatch.delenv("ADOPTION_CITATION_PROBE_DISABLE", raising=False)
    monkeypatch.setattr(ams, "_cs_probe", lambda base, t, a: {
        "tool": t, "envelope_citation": True, "envelope_provenance": True,
        "cite_as_anywhere": True, "error": None})
    checks = ams._lane_citation_survival(None)
    assert _by_id(checks, "cs_send_side")["pass"] is True
    assert _lane_verdict(checks) == "?"          # NOT "PASS"
    b = _by_id(checks, "cs_boundary")
    assert b["pass"] is None and b["critical"] is True


def test_citation_lane_is_red_when_the_send_side_is_missing(monkeypatch):
    """If we do not SEND the attribution it certainly never arrives. That is
    a real, fixable precondition and it must read FAIL, not '?'."""
    monkeypatch.delenv("ADOPTION_CITATION_PROBE_DISABLE", raising=False)
    monkeypatch.setattr(ams, "_cs_probe", lambda base, t, a: {
        "tool": t, "envelope_citation": False, "envelope_provenance": False,
        "cite_as_anywhere": False, "error": None})
    checks = ams._lane_citation_survival(None)
    assert _by_id(checks, "cs_send_side")["pass"] is False
    assert _lane_verdict(checks) == "FAIL"


def test_a_failed_probe_is_unmeasured_never_a_zero(monkeypatch):
    """★ The dangerous direction: unknown treated as success, or as a
    flattering zero. A transport failure is NOT evidence the envelope is
    empty."""
    monkeypatch.delenv("ADOPTION_CITATION_PROBE_DISABLE", raising=False)
    monkeypatch.setattr(ams, "_cs_probe", lambda base, t, a: {
        "tool": t, "error": "SimulatedTimeout"})
    checks = ams._lane_citation_survival(None)
    s = _by_id(checks, "cs_send_side")
    assert s["pass"] is None
    assert "NOT evidence the envelope is empty" in s["detail"]


def test_probe_kill_switch_renders_unmeasured_not_pass(monkeypatch):
    monkeypatch.setenv("ADOPTION_CITATION_PROBE_DISABLE", "1")
    called = []
    monkeypatch.setattr(ams, "_cs_probe",
                        lambda *a, **k: called.append(1) or {})
    checks = ams._lane_citation_survival(None)
    assert not called, "the kill switch did not stop the outbound probe"
    assert _by_id(checks, "cs_send_side")["pass"] is None


def test_lookalike_citation_tables_are_refused_as_the_answer(monkeypatch):
    """ai_citations / citation_probes measure whether a PUBLIC LLM answer
    mentioned dchub.cloud — a different population, about traffic that never
    touched MCP. Wiring one in as this lane's source must FAIL."""
    monkeypatch.setenv("ADOPTION_CITATION_PROBE_DISABLE", "1")
    clean = ams._lane_citation_survival(None)
    assert _by_id(clean, "cs_no_decoy")["pass"] is True

    monkeypatch.setattr(ams, "_CS_SOURCES",
                        ams._CS_SOURCES + ("ai_citations rollup",))
    assert any("ai_citations" in s for s in ams._CS_SOURCES)
    poisoned = ams._lane_citation_survival(None)
    d = _by_id(poisoned, "cs_no_decoy")
    assert d["pass"] is False
    assert "DECOY WIRED IN" in d["detail"]


def test_citation_block_names_the_instrument_without_building_it(monkeypatch):
    monkeypatch.setenv("ADOPTION_CITATION_PROBE_DISABLE", "1")
    block = ams._citation_survival_block(ams._lane_citation_survival(None))
    assert "PASS is unreachable" in block["verdict_ceiling"]
    assert "NOT BUILT" in block["instrumentation_needed"]
    assert set(block["sources_refused"]) == {
        "ai_citations", "citation_probes", "citation_scores"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
