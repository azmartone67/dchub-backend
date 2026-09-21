"""tests/test_loop_closure_master_shell.py — the guards that must be able to fail.

Every assertion targets a specific way this shell could go quietly inert, act on
the wrong population, leak customer data, or report a guess as a measurement.
Each is mutation-tested; the MUTATION line names what must turn it red.
"""
import ast
import inspect
import json
import pathlib
import re
import sys
import textwrap
import types

from routes import loop_closure_master_shell as lcs

REPO = pathlib.Path(__file__).resolve().parents[1]


# ── the sample floor ──────────────────────────────────────────────────
def test_rate_below_floor_is_none_not_zero():
    """0.0 over paid_total=2 reads as 'every attributable sale is lost'.

    MUTATION: return 0.0 instead of None below the floor.
    """
    rate, basis = lcs.rate_or_none(0, 2, floor=10)
    assert rate is None
    assert "below floor" in basis
    at_floor, _ = lcs.rate_or_none(1, 10, floor=10)
    assert at_floor == 10.0, "a denominator AT the floor must publish"


def test_rate_distinguishes_zero_numerator_from_no_reading():
    """MUTATION: `if not numerator` — folds a measured 0 into unmeasured."""
    real_zero, basis = lcs.rate_or_none(0, 200, floor=10)
    assert real_zero == 0.0 and basis == "0 of 200"
    assert lcs.rate_or_none(None, 200, floor=10)[0] is None


def test_zero_denominator_never_divides():
    rate, basis = lcs.rate_or_none(0, 0, floor=0)
    assert rate is None and "0" in basis


# ── selection: weakest ACTIONABLE lane ────────────────────────────────
def _L(name, score, actionable=False, owner="someone", why="owned elsewhere",
       error=None):
    return {"lane": name, "score": score, "actionable": actionable,
            "ready": False, "owner": owner, "why_not_actionable": why,
            "detail": {"error": error} if error else {}, "rate_basis": "b"}


def test_unmeasured_lane_is_excluded_not_treated_as_zero():
    """MUTATION: `score = lane.get("score") or 0`."""
    lanes = {"gradient": _L("gradient", None, error="no reading"),
             "activation": _L("activation", 40.0),
             "negative": _L("negative", 42.0),
             "objective": _L("objective", 56.0),
             "spec_debt": _L("spec_debt", 30.0, actionable=True)}
    weakest, excluded, _ = lcs.pick_weakest(lanes)
    assert weakest == "spec_debt"
    assert [e["lane"] for e in excluded] == ["gradient"]
    assert excluded[0]["why"] == "no reading"


def test_weakest_lane_whose_lever_is_elsewhere_is_skipped_not_selected(monkeypatch):
    """★ THE STARVATION GUARD. A lane whose own action cannot move its score
    would otherwise be weakest forever and the shell would go inert on tick 2.

    MUTATION: drop the actionability check from pick_weakest.
    """
    # gradient HAS an actuator registered here but nothing to do — the case
    # the actionability check exists for. Without it, gradient wins forever.
    monkeypatch.setattr(lcs, "ACTUATORS", {"gradient": lambda m, d: {},
                                           "spec_debt": lambda m, d: {}})
    lanes = {"gradient": _L("gradient", 0.5, owner="conversion volume"),
             "activation": _L("activation", 1.0, owner="human desk"),
             "negative": _L("negative", 42.0),
             "objective": _L("objective", 56.0),
             "spec_debt": _L("spec_debt", 38.0, actionable=True)}
    weakest, _, skipped = lcs.pick_weakest(lanes)
    assert weakest == "spec_debt"
    owners = {s["lane"]: s["owner"] for s in skipped}
    assert owners["gradient"] == "conversion volume"
    assert owners["activation"] == "human desk"


def test_actionable_flag_without_an_actuator_is_still_skipped():
    """A lane flipped to actionable with no registered actuator must not be
    dispatched into a KeyError.

    MUTATION: drop `name not in ACTUATORS` from the skip condition.
    """
    lanes = {n: _L(n, 90.0) for n in lcs.LANES}
    lanes["negative"] = _L("negative", 5.0, actionable=True)
    weakest, _, skipped = lcs.pick_weakest(lanes)
    assert weakest is None
    assert any(s["lane"] == "negative" for s in skipped)


def test_nothing_actionable_selects_nothing():
    """MUTATION: default to LANES[0] when no lane qualifies."""
    lanes = {n: _L(n, 10.0) for n in lcs.LANES}
    weakest, _, skipped = lcs.pick_weakest(lanes)
    assert weakest is None and len(skipped) == len(lcs.LANES)


def test_tie_breaks_deterministically(monkeypatch):
    """MUTATION: sort on score alone."""
    # gradient precedes activation in LANES but FOLLOWS it alphabetically, so
    # a sort that falls through to the name picks the other one.
    monkeypatch.setattr(lcs, "ACTUATORS", {"gradient": lambda m, d: {},
                                           "activation": lambda m, d: {}})
    lanes = {n: _L(n, 42.0, actionable=n in ("gradient", "activation"))
             for n in lcs.LANES}
    picks = {lcs.pick_weakest(lanes)[0] for _ in range(8)}
    assert picks == {"gradient"}


def test_lane_kill_switch_excludes_with_a_reason(monkeypatch):
    """MUTATION: ignore _lane_off."""
    monkeypatch.setenv("LOOP_CLOSURE_LANE_SPEC_DEBT_OFF", "1")
    lanes = {n: _L(n, 50.0, actionable=(n == "spec_debt")) for n in lcs.LANES}
    weakest, excluded, _ = lcs.pick_weakest(lanes)
    assert weakest is None
    assert any(e["lane"] == "spec_debt" and "kill switch" in e["why"]
               for e in excluded)


# ── lane 1: the published attribution rate ────────────────────────────
def _funnel(paid, bridged, rate):
    return {"paid_signal_attribution_30d": {
        "paid_total": paid, "bridged_to_signal": bridged,
        "attribution_rate_pct": rate}}


def test_gradient_flags_a_rate_published_below_the_floor():
    """★ The regression this lane guards: a number over two sales.

    MUTATION: `published_rate_honest = True` unconditionally.
    """
    out = lcs.lane_gradient(funnel=_funnel(2, 0, 0.0))
    assert out["published_rate_honest"] is False
    assert "below floor" in out["detail"]["regression"]
    assert out["score"] is None


def test_gradient_accepts_a_withheld_rate():
    out = lcs.lane_gradient(funnel=_funnel(2, 0, None))
    assert out["published_rate_honest"] is True
    assert "regression" not in out["detail"]


def test_gradient_above_floor_measures():
    out = lcs.lane_gradient(funnel=_funnel(40, 10, 25.0))
    assert out["score"] == 25.0 and out["published_rate_honest"] is True


def test_gradient_unavailable_is_unmeasured(monkeypatch):
    monkeypatch.setattr(lcs, "_loopback_json", lambda *a, **k: None)
    out = lcs.lane_gradient()
    assert out["measured"] is False and out["score"] is None


def test_gradient_is_never_actionable():
    """No attribution change moves paid_total; the lane must not pretend."""
    assert lcs.lane_gradient(funnel=_funnel(2, 0, None))["actionable"] is False


# ── lane 2: activation — counts only, human-owned ─────────────────────
_DESK = {"ok": True, "open_total": 10, "never_contacted": 6,
         "by_segment": {"logged_in_never_called": 4, "never_logged_in": 6},
         "rows": [{"email": "buyer@example.com", "name": "A Buyer"},
                  {"email": "other@example.org", "name": "B Other"}],
         "basis": "users x api_keys"}


def test_activation_never_carries_customer_data():
    """★ The desk's rows hold emails; none may reach the snapshot.

    MUTATION: copy desk["rows"] into the lane output.
    """
    out = lcs.lane_activation(desk=_DESK)
    blob = json.dumps(out)
    assert "@" not in blob, "a customer email leaked into the lane output"
    assert "rows" not in out


def test_activation_reads_the_desk_and_names_the_human_owner():
    out = lcs.lane_activation(desk=_DESK)
    assert out["paid_never_called"] == 10 and out["never_contacted"] == 6
    assert out["score"] is None, "10 accounts is below the activation floor"
    assert "human" in out["owner"]


def test_activation_is_never_actionable():
    """The nudge already runs daily; a third sender adds nothing.

    MUTATION: mark the lane actionable.
    """
    assert lcs.lane_activation(desk=_DESK)["actionable"] is False


def test_activation_unread_desk_is_unmeasured():
    out = lcs.lane_activation(desk={"ok": False, "note": "query failed"})
    assert out["measured"] is False and out["detail"]["error"] == "query failed"


# ── lane 3: negative — published counts + the ranker's own columns ────
_EFF = {"outcome_verification_30d": {"checks_performed": 210, "fix_failed": 114,
                                     "fix_succeeded": 85, "success_rate_pct": 42.7}}


def test_negative_rate_is_over_graded_outcomes_not_checks(monkeypatch):
    """★ Ungraded is not failed: 85 of 199 graded, not 85 of 210 checks.

    MUTATION: use checks_performed as the denominator.
    """
    monkeypatch.setattr(lcs, "_worst_class", lambda: {"available": False})
    out = lcs.lane_negative(effectiveness=_EFF)
    assert out["graded"] == 199 and out["ungraded"] == 11
    assert out["score"] == round(100 * 85 / 199, 2)


def test_negative_unavailable_is_unmeasured(monkeypatch):
    monkeypatch.setattr(lcs, "_loopback_json", lambda *a, **k: None)
    monkeypatch.setattr(lcs, "_worst_class", lambda: {"available": False})
    out = lcs.lane_negative()
    assert out["measured"] is False and out["score"] is None


def _sql_constants(fn):
    """String constants in a function that look like SQL — docstrings and
    comments excluded, so a guard cannot be satisfied by its own prose."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "SELECT" in node.value.upper() and "FROM" in node.value.upper():
            out.append(node.value)
    return out


def test_worst_class_reads_the_rankers_columns():
    """★ brain_fix_outcomes has klass/resolved/verified_at. `pattern` and
    `succeeded` are autopilot_outcomes' columns; querying them here fails on
    the first live tick.

    MUTATION: query `succeeded` / `pattern` on brain_fix_outcomes.
    """
    sql = " ".join(_sql_constants(lcs._worst_class))
    assert sql, "floor: the scan must find the SQL it is checking"
    assert "klass" in sql and "resolved" in sql and "brain_fix_outcomes" in sql
    assert "succeeded" not in sql and "pattern" not in sql


class _Cur:
    def __init__(self, row=None, raise_on_execute=None):
        self._row, self._raise = row, raise_on_execute

    def execute(self, *a, **k):
        if self._raise:
            raise self._raise

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def close(self):
        return None


def test_neutral_weight_means_the_negative_never_arrived(monkeypatch):
    """A class failing 9 of 10 at NEUTRAL 1.0 never reached the ranker.

    MUTATION: `applied <= 1.0` — reports a never-arrived negative as delivered.
    """
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur(("dead_route", 10, 9))))
    monkeypatch.setattr(lcs, "applied_weight_for", lambda k: 1.0)
    w = lcs._worst_class()
    assert w["available"] and w["negative_reached_ranker"] is False


def test_down_weighted_class_counts_as_reached(monkeypatch):
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur(("dead_route", 10, 9))))
    monkeypatch.setattr(lcs, "applied_weight_for", lambda k: 0.4)
    assert lcs._worst_class()["negative_reached_ranker"] is True


def test_unreadable_weight_is_none_not_false(monkeypatch):
    """MUTATION: `bool(applied and applied < 1.0)` — turns None into False."""
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur(("dead_route", 10, 9))))
    monkeypatch.setattr(lcs, "applied_weight_for", lambda k: None)
    assert lcs._worst_class()["negative_reached_ranker"] is None


def test_worst_class_read_failure_is_reported_not_raised(monkeypatch):
    boom = Exception('column "klass" does not exist')
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur(raise_on_execute=boom)))
    w = lcs._worst_class()
    assert w["available"] is False and "klass" in w["why"]


# ── lane 4: objective — merged PRs are output ─────────────────────────
_SHIPPED = {"shipped_30d": {"conversions": 3, "code_fixes": 109,
                            "autopilot_actions": 62, "press_releases": 54,
                            "linkedin_posts": 27, "outreach_pitches": 10,
                            "facilities_discovered": 0},
            "weights": {"conversions": 25, "code_fixes": 8,
                        "autopilot_actions": 5, "press_releases": 1,
                        "linkedin_posts": 1, "outreach_pitches": 1,
                        "facilities_discovered": 0.2},
            "verdict": "high_output"}


def test_merged_prs_are_discounted_by_verified_success():
    """★ A merged PR is output until an outcome says it worked.

    MUTATION: count code_fixes at full weight (verified would read 1257).
    """
    verified, total = lcs.verified_value_score(
        _SHIPPED["shipped_30d"], _SHIPPED["weights"], 0.427)
    assert total == 1348.0, "must reproduce the published total exactly"
    assert verified == round(75 + 310 + 872 * 0.427, 1)


def test_media_is_never_verified_effect():
    """MUTATION: add press_releases to the verified sum."""
    v, _ = lcs.verified_value_score({"press_releases": 100},
                                    {"press_releases": 1}, 1.0)
    assert v == 0.0


def test_an_uncomputable_discount_is_unmeasured_not_one():
    """MUTATION: treat a missing success rate as 1.0."""
    v, total = lcs.verified_value_score(_SHIPPED["shipped_30d"],
                                        _SHIPPED["weights"], None)
    assert v is None and total == 1348.0
    out = lcs.lane_objective(shipped=_SHIPPED, success_rate=None)
    assert out["measured"] is False and out["score"] is None


def test_objective_unavailable_payload_scores_none(monkeypatch):
    monkeypatch.setattr(lcs, "_loopback_json", lambda *a, **k: None)
    out = lcs.lane_objective(success_rate=0.5)
    assert out["score"] is None


# ── lane 5: spec debt — the one lever this shell pulls ────────────────
def _impl(armed=True, disabled=False, acted=True):
    calls = []
    mod = types.ModuleType("routes.brain_spec_implementer")
    mod._armed = lambda: armed
    mod._disabled = lambda: disabled

    def implement_spec(spec_name, kind="spec", item_id=0, apply=False):
        calls.append({"doc": spec_name, "apply": apply})
        return {"ok": True, "acted": acted and apply,
                "pr": {"pr_url": "https://example.invalid/pull/1"} if apply else {},
                "note": "dry run" if not apply else "opened"}

    mod.implement_spec = implement_spec
    return mod, calls


_SCAN = {"state": "MEASURED",
         "counts": {"open": 2, "closed": 1, "unknown": 7, "total_docs": 10},
         "open_obligations": [
             {"doc": "old.md", "title": "oldest", "age_days": 40.0,
              "unchecked_items": 3},
             {"doc": "newer.md", "title": "newer", "age_days": 5.0,
              "unchecked_items": 1}]}


def test_spec_debt_unmeasured_corpus_is_not_zero_debt():
    """MUTATION: drop the MEASURED guard."""
    # counts PRESENT on purpose: the state flag is the contract, and a scan
    # that reports UNMEASURED must not be scored even if it carries numbers.
    out = lcs.lane_spec_debt(scan={"state": "UNMEASURED",
                                   "counts": {"open": 0, "closed": 5,
                                              "unknown": 0, "total_docs": 5}},
                             attempted=set())
    assert out["measured"] is False and out["score"] is None


def test_spec_debt_unknown_is_not_folded_into_closed(monkeypatch):
    """MUTATION: total = open + closed."""
    mod, _ = _impl()
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    out = lcs.lane_spec_debt(scan=_SCAN, attempted=set())
    assert out["total"] == 10 and out["score"] == 10.0


def test_spec_debt_skips_recently_driven_docs(monkeypatch):
    """MUTATION: ignore `attempted` — the same spec re-driven every tick."""
    mod, _ = _impl()
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    out = lcs.lane_spec_debt(scan=_SCAN, attempted={"old.md"})
    assert out["target"]["doc"] == "newer.md"


def test_spec_debt_needs_the_implementers_own_arm(monkeypatch):
    """★ Two arms. Actionable without SPEC_IMPLEMENTER_ARM, never READY.

    MUTATION: `ready = True` regardless of the implementer's arm.
    """
    mod, _ = _impl(armed=False)
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    out = lcs.lane_spec_debt(scan=_SCAN, attempted=set())
    assert out["actionable"] is True and out["ready"] is False
    assert "SPEC_IMPLEMENTER_ARM" in out["why_not_ready"]


def test_spec_debt_ready_when_the_implementer_is_armed(monkeypatch):
    mod, _ = _impl(armed=True)
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    assert lcs.lane_spec_debt(scan=_SCAN, attempted=set())["ready"] is True


def test_disabled_implementer_is_not_actionable(monkeypatch):
    mod, _ = _impl(disabled=True)
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    out = lcs.lane_spec_debt(scan=_SCAN, attempted=set())
    assert out["actionable"] is False and "DISABLE" in out["why_not_actionable"]


def test_dry_act_passes_apply_false_and_records_nothing(monkeypatch):
    """★ A dry 'attempt' recorded would make the first real tick skip the very
    spec it previewed.

    MUTATION: record the attempt on a dry tick, or pass apply=True while dry.
    """
    mod, calls = _impl()
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)

    def _no_record(*a, **k):
        raise AssertionError("a dry tick recorded an attempt")

    monkeypatch.setattr(lcs, "_record_attempt", _no_record)
    res = lcs.act_spec_debt({"target": {"doc": "old.md"}}, dry=True)
    assert calls == [{"doc": "old.md", "apply": False}]
    assert res["acted"] is False


def test_armed_act_applies_and_records(monkeypatch):
    mod, calls = _impl(acted=True)
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    seen = []
    monkeypatch.setattr(lcs, "_record_attempt",
                        lambda doc, acted, note: seen.append((doc, acted)))
    res = lcs.act_spec_debt({"target": {"doc": "old.md"}}, dry=False)
    assert calls == [{"doc": "old.md", "apply": True}]
    assert res["acted"] is True and seen == [("old.md", True)]
    assert res["pr"] == "https://example.invalid/pull/1"


# ── the inert check ───────────────────────────────────────────────────
def test_inert_fires_on_a_full_window_of_ready_unacted_ticks(monkeypatch):
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur((5, 5))))
    out = lcs.inert_check(limit=5)
    assert out["fired"] is True and "zero actions" in out["detail"]


def test_inert_does_not_fire_before_the_window_fills(monkeypatch):
    """MUTATION: drop the `armed_ticks < n` guard."""
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur((2, 2))))
    out = lcs.inert_check(limit=5)
    assert out["fired"] is False and "not yet measurable" in out["reason"]


def test_inert_does_not_fire_when_a_tick_acted_or_was_idle(monkeypatch):
    """An idle tick (nothing READY) is honest, not inert.

    MUTATION: fire on `ready_unacted_ticks > 0`.
    """
    monkeypatch.setattr(lcs, "_conn", lambda: _Conn(_Cur((5, 4))))
    assert lcs.inert_check(limit=5)["fired"] is False


def test_inert_unmeasurable_without_a_db(monkeypatch):
    monkeypatch.setattr(lcs, "_conn", lambda: None)
    out = lcs.inert_check()
    assert out["measured"] is False and out["fired"] is False


# ── the tick, end to end (dry) ────────────────────────────────────────
def test_dry_tick_selects_the_actionable_lane_and_leaks_nothing(monkeypatch):
    mod, calls = _impl(armed=True)
    monkeypatch.setitem(sys.modules, "routes.brain_spec_implementer", mod)
    monkeypatch.delenv("LOOP_CLOSURE_ARM", raising=False)
    monkeypatch.setattr(lcs, "lane_gradient",
                        lambda: _L("gradient", None, error="stub"))
    monkeypatch.setattr(lcs, "lane_activation",
                        lambda: _L("activation", None, owner="human"))
    monkeypatch.setattr(lcs, "lane_negative",
                        lambda: {**_L("negative", 42.7), "success_rate": 0.427})
    monkeypatch.setattr(lcs, "lane_objective",
                        lambda success_rate=None: _L("objective", 56.2))
    monkeypatch.setattr(lcs, "lane_spec_debt",
                        lambda: {**_L("spec_debt", 10.0, actionable=True),
                                 "ready": True, "target": {"doc": "old.md"}})
    monkeypatch.setattr(lcs, "inert_check", lambda: {"fired": False})
    persisted = []
    monkeypatch.setattr(lcs, "_persist", persisted.append)
    snap = lcs.run_tick()
    assert snap["armed"] is False
    assert snap["weakest_actionable_lane"] == "spec_debt"
    assert snap["acted"] is False, "a dry tick must never act"
    assert calls == [{"doc": "old.md", "apply": False}]
    assert snap["ready_lanes"] == 1
    assert persisted and "@" not in json.dumps(persisted[0], default=str)


# ── structural ────────────────────────────────────────────────────────
def test_every_actuator_has_a_dry_gate_and_reports_acted():
    for name, act in lcs.ACTUATORS.items():
        assert name in lcs.LANES
        assert "dry" in inspect.signature(act).parameters
        assert "acted" in inspect.getsource(act)


def test_every_written_table_is_read_back():
    """★ An actuator whose output nothing reads is not actuation.

    MUTATION: drop the read of a table this shell creates.
    """
    src = inspect.getsource(lcs)
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", src))
    assert created, "floor: the scan must find the tables it checks"
    selected = set(re.findall(r"FROM (\w+)", src))
    assert not (created - selected), sorted(created - selected)


def test_arm_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LOOP_CLOSURE_ARM", raising=False)
    assert lcs._armed() is False


# ── item 1, at the surface: flask_mcp_endpoints withholds the rate ────
def _psa_nodes():
    tree = ast.parse((REPO / "flask_mcp_endpoints.py").read_text())
    dicts = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)
             and any(isinstance(k, ast.Constant)
                     and k.value == "attribution_rate_withheld_reason"
                     for k in n.keys)]
    assigns = {t.id: n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)
               and t.id in ("_psa_withhold", "_psa_floor")}
    return dicts, assigns


def test_published_rate_is_withheld_below_the_floor():
    """★ The item-1 fix on the published surface, checked on the AST so no
    comment can satisfy it.

    MUTATION: publish _psa_rate unconditionally; flip `<`; floor 0.
    """
    dicts, assigns = _psa_nodes()
    assert len(dicts) == 1, "exactly one paid_signal_attribution block"
    block = dicts[0]
    value = next(v for k, v in zip(block.keys, block.values)
                 if isinstance(k, ast.Constant) and k.value == "attribution_rate_pct")
    assert isinstance(value, ast.IfExp)
    assert isinstance(value.test, ast.Name) and value.test.id == "_psa_withhold"
    assert isinstance(value.body, ast.Constant) and value.body.value is None
    cmp_ = assigns["_psa_withhold"]
    assert isinstance(cmp_, ast.Compare) and isinstance(cmp_.ops[0], ast.Lt)
    assert cmp_.left.id == "_paid_total" and cmp_.comparators[0].id == "_psa_floor"
    floor = assigns["_psa_floor"]
    assert isinstance(floor, ast.Constant) and floor.value >= 5, \
        "a floor below 5 sales is no floor"


# ── the snapshot must persist at ANY size ─────────────────────────────
def test_oversized_snapshot_detail_is_still_valid_json():
    """★ `json.dumps(x)[:60000]` is invalid JSON past 60k chars; Postgres rejects
    it for jsonb, the fail-soft handler swallows it, and the snapshot — which
    inert_check reads — is silently never written.

    MUTATION: bind a sliced json.dumps() again.
    """
    snap = {"generated_at": "2026-09-21T07:17:00+00:00", "armed": True,
            "loop_score": 41.2, "weakest_actionable_lane": "spec_debt",
            "ready_lanes": 1, "acted": True,
            "lanes": {"spec_debt": {"blob": "x" * 200000}}}
    captured = []

    class _C:
        def execute(self, sql, params=None):
            captured.append(params)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _K:
        def cursor(self, *a, **k):
            return _C()

        def commit(self):
            return None

        def close(self):
            return None

    import pytest as _pt
    mp = _pt.MonkeyPatch()
    try:
        mp.setattr(lcs, "_ensure_tables", lambda: True)
        mp.setattr(lcs, "_conn", lambda: _K())
        lcs._persist(snap)
    finally:
        mp.undo()
    params = next(p for p in captured if p)
    detail = json.loads(params[-1])          # must PARSE, whatever its size
    # Non-vacuity: this payload is ~200k chars, so the oversize branch MUST
    # have run — a test whose input fits would prove nothing about size.
    assert detail.get("_truncated") is True
    # json_for_column keeps identifying scalars as STRINGS (str(v)[:300]).
    # inert_check reads the table's typed armed/acted columns, never this JSON.
    assert detail.get("armed") == "True"
    assert detail.get("generated_at") == snap["generated_at"]
    assert len(params[-1]) <= 60000
