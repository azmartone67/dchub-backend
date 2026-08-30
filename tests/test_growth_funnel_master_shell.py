"""Tests for the Growth Master Shell (#53).

The lanes are pure functions over DB rows, so every verdict is testable without
a database. What matters most is the HONESTY RULE: a lane that could not check
must read '?', never PASS — and a lane must not go green just because its data
happens to be empty.
"""

from routes import growth_funnel_master_shell as gs


def _by_id(checks):
    return {c["id"]: c for c in checks}


# ── lane verdicts obey the honesty rule ─────────────────────────────────

def test_no_db_reads_indeterminate_not_pass(monkeypatch):
    # Kills: a `return []` / truthy default that would render a confident PASS
    # on a lane that never ran.
    monkeypatch.setattr(gs, "_conn", lambda: None)
    for fn in (gs._lane_attribution, gs._lane_front_door, gs._lane_compounding):
        checks = fn()
        assert gs._lane_verdict(checks) == "?", fn.__name__
        assert all(c["pass"] is None for c in checks), fn.__name__


def test_crashed_lane_renders_indeterminate_not_500():
    def boom():
        raise RuntimeError("kaboom")
    checks = gs._safe_lane(boom)
    assert gs._lane_verdict(checks) == "?"
    assert "kaboom" in checks[0]["detail"]


# ── lane 1 · attribution ────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)
    def close(self): pass


def test_attribution_fails_when_generic_bucket_is_the_majority(monkeypatch):
    # The measured state on 2026-08-08: 226 of 278 unattributed.
    monkeypatch.setattr(gs, "_conn",
                        lambda: _Conn([("mcp", 226), ("claude", 16),
                                       ("connectors-manager", 20)]))
    c = _by_id(gs._lane_attribution())
    assert c["attr_cov"]["pass"] is False
    assert "36" in c["attr_cov"]["detail"] or "%" in c["attr_cov"]["detail"]


def test_attribution_passes_when_most_agents_are_named(monkeypatch):
    monkeypatch.setattr(gs, "_conn",
                        lambda: _Conn([("mcp", 10), ("claude", 60),
                                       ("cursor", 30)]))
    assert _by_id(gs._lane_attribution())["attr_cov"]["pass"] is True


def test_attribution_counts_null_and_unknown_as_generic(monkeypatch):
    # Kills: treating NULL/'unknown' as a named channel, which would flatter
    # coverage with exactly the rows that carry no signal.
    monkeypatch.setattr(gs, "_conn",
                        lambda: _Conn([(None, 40), ("unknown", 40),
                                       ("claude", 20)]))
    assert _by_id(gs._lane_attribution())["attr_cov"]["pass"] is False


def test_attribution_with_no_new_agents_is_indeterminate(monkeypatch):
    # Kills: 0/0 rendering as 0% and thus a confident FAIL, or as a PASS.
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([]))
    assert _by_id(gs._lane_attribution())["attr_cov"]["pass"] is None


# ── lane 2 · front door ─────────────────────────────────────────────────

def test_front_door_measures_CONVERSION_not_first_tool_rank(monkeypatch):
    # ★ v1 asked "does execute_plan rank top-5 as a FIRST tool" — unanswerable:
    #   an agent's first call cannot be execute_plan unless it read the
    #   instructions before calling anything, and the nudge is a SECOND-call
    #   mechanism. Permanently red for a reason no fix could address.
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([("search_facilities", 53)]))
    monkeypatch.setattr(gs, "_front_door_conversion", lambda: (264, 5))
    c = _by_id(gs._lane_front_door())
    assert c["fd_conv"]["pass"] is False        # 1.9% < 10% bar
    assert "1.9%" in c["fd_conv"]["detail"]
    assert "264" in c["fd_conv"]["detail"]


def test_front_door_passes_on_good_conversion(monkeypatch):
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([("search_facilities", 53)]))
    monkeypatch.setattr(gs, "_front_door_conversion", lambda: (200, 40))
    assert _by_id(gs._lane_front_door())["fd_conv"]["pass"] is True


def test_front_door_first_tool_is_CONTEXT_not_the_verdict(monkeypatch):
    # execute_plan absent from first-touch must NOT by itself fail the lane.
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([("search_facilities", 53)]))
    monkeypatch.setattr(gs, "_front_door_conversion", lambda: (100, 50))
    c = _by_id(gs._lane_front_door())
    assert c["fd_conv"]["pass"] is True
    assert "NOT the verdict" in c["fd_conv"]["detail"]


def test_front_door_unmeasurable_conversion_is_indeterminate(monkeypatch):
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([("search_facilities", 1)]))
    monkeypatch.setattr(gs, "_front_door_conversion", lambda: None)
    assert _by_id(gs._lane_front_door())["fd_conv"]["pass"] is None


def test_front_door_says_a_low_number_is_not_a_missing_nudge(monkeypatch):
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([("search_facilities", 53)]))
    monkeypatch.setattr(gs, "_front_door_conversion", lambda: (264, 5))
    d = _by_id(gs._lane_front_door())["fd_reading"]["detail"]
    assert "SEEN and DECLINED" in d


def test_nudge_tool_list_mirrors_the_gateway():
    # A stale copy silently mis-sizes the denominator.
    assert "search_facilities" in gs._NUDGE_TOOLS
    assert "execute_plan" not in gs._NUDGE_TOOLS   # the destination, not a source
    assert len(gs._NUDGE_TOOLS) == 16


# ── lane 3 · distribution ───────────────────────────────────────────────

def _roster(monkeypatch, platforms):
    import routes.agent_onboarding_master_shell as ao
    monkeypatch.setattr(ao, "PLATFORMS", platforms, raising=False)


def test_distribution_fails_when_no_high_reach_channel_is_listed(monkeypatch):
    _roster(monkeypatch, [
        {"name": "Claude", "reach_weight": 1.0, "directory_listed": False},
        {"name": "Hugging Face", "reach_weight": 0.4, "directory_listed": True},
    ])
    c = _by_id(gs._lane_distribution())
    assert c["dist_high"]["pass"] is False
    # Kills: counting a LOW-reach listing as satisfying the high-reach check.
    assert "Claude" in c["dist_high"]["detail"]


def test_distribution_passes_once_a_high_reach_channel_lists(monkeypatch):
    _roster(monkeypatch, [
        {"name": "Claude", "reach_weight": 1.0, "directory_listed": True},
        {"name": "ChatGPT", "reach_weight": 1.0, "directory_listed": False},
    ])
    assert _by_id(gs._lane_distribution())["dist_high"]["pass"] is True


def test_distribution_treats_None_as_unmeasured_not_unlisted(monkeypatch):
    # None is "we never checked", which is a different work item from "no".
    _roster(monkeypatch, [
        {"name": "Grok", "reach_weight": 0.7, "directory_listed": None},
        {"name": "Claude", "reach_weight": 1.0, "directory_listed": True},
    ])
    c = _by_id(gs._lane_distribution())
    assert c["dist_unknown"]["pass"] is False
    assert "UNMEASURED" in c["dist_unknown"]["detail"]


def test_distribution_states_it_is_not_code_closeable(monkeypatch):
    _roster(monkeypatch, [{"name": "X", "reach_weight": 1.0,
                           "directory_listed": True}])
    assert "NOT CODE-CLOSEABLE" in _by_id(
        gs._lane_distribution())["dist_owner"]["detail"]


# ── lane 4 · compounding ────────────────────────────────────────────────

def _weeks(rows):
    return _Conn(rows)


def test_compounding_WITHHOLDS_a_verdict_on_the_real_2026_08_08_data(monkeypatch):
    # ★ THE CORRECTION THIS LANE EXISTS TO ENCODE. An earlier draft convicted
    #   here on "returning flat across a 2x swing". The real numbers do NOT
    #   meet that: returning GREW 0->6 and new swung only 1.8x — the eyeballed
    #   "flat 5-7" included cold-start weeks. With 4 complete weeks the honest
    #   verdict is '?', not FAIL. This test fails if anyone tunes the bar until
    #   it agrees with the hunch.
    monkeypatch.setattr(gs, "_conn", lambda: _weeks([
        ("2026-07-06", 43, 0), ("2026-07-13", 78, 3),
        ("2026-07-20", 55, 7), ("2026-07-27", 79, 6),
        ("2026-08-03", 26, 5),          # partial — must be dropped
    ]))
    c = _by_id(gs._lane_compounding())
    assert c["comp_flat"]["pass"] is None
    assert "complete week" in c["comp_flat"]["detail"]
    # The partial final week must NOT appear in the series text.
    assert "2026-08-03" not in c["comp_flat"]["detail"]
    # ...but the conversion rate IS reported even while the verdict is withheld.
    assert "conversion" in c["comp_flat"]["detail"]


def test_compounding_FAILS_on_zero_returning_against_real_supply(monkeypatch):
    # The one unambiguous signal — convictable at any history length.
    monkeypatch.setattr(gs, "_conn", lambda: _weeks([
        ("w1", 40, 2), ("w2", 90, 0), ("wPartial", 5, 0),
    ]))
    c = _by_id(gs._lane_compounding())
    assert c["comp_flat"]["pass"] is False
    assert "ZERO agents returned" in c["comp_flat"]["detail"]


def test_compounding_fails_when_pool_has_not_grown_over_enough_weeks(monkeypatch):
    # 6 complete weeks, returning ends no higher than it started -> convict.
    monkeypatch.setattr(gs, "_conn", lambda: _weeks([
        ("w1", 40, 6), ("w2", 80, 7), ("w3", 60, 5), ("w4", 90, 6),
        ("w5", 70, 5), ("w6", 85, 6), ("wPartial", 9, 1),
    ]))
    assert _by_id(gs._lane_compounding())["comp_flat"]["pass"] is False


def test_compounding_passes_when_the_pool_actually_grows(monkeypatch):
    monkeypatch.setattr(gs, "_conn", lambda: _weeks([
        ("w1", 40, 5), ("w2", 80, 12), ("w3", 60, 19), ("w4", 90, 28),
        ("w5", 70, 35), ("w6", 85, 44), ("wPartial", 20, 9),
    ]))
    assert _by_id(gs._lane_compounding())["comp_flat"]["pass"] is True


def test_compounding_reports_conversion_rate_not_just_counts(monkeypatch):
    # 10 of week-1's 40 new came back in week 2 = 25%.
    monkeypatch.setattr(gs, "_conn", lambda: _weeks([
        ("w1", 40, 0), ("w2", 50, 10), ("wPartial", 5, 1)]))
    assert "25%" in _by_id(gs._lane_compounding())["comp_flat"]["detail"]


def test_compounding_names_what_a_flat_pool_does_not_mean(monkeypatch):
    monkeypatch.setattr(gs, "_conn", lambda: _weeks([
        ("w1", 40, 1), ("w2", 90, 2), ("w3", 45, 3), ("w4", 80, 2), ("w5", 5, 1),
    ]))
    d = _by_id(gs._lane_compounding())["comp_reading"]["detail"]
    assert "NOT evidence the return MECHANISM is broken" in d


# ── tick shape ──────────────────────────────────────────────────────────

def test_tick_reports_fail_when_any_lane_fails(monkeypatch):
    monkeypatch.setattr(gs, "_conn", lambda: _Conn([("mcp", 100)]))
    t = gs._run_tick()
    assert t["lanes_total"] == 4
    assert t["verdict"] in ("FAIL", "?")
    assert "generated_at" in t


def test_tick_does_not_beat_unless_asked(monkeypatch):
    called = []
    monkeypatch.setattr(gs, "_beat_ledger", lambda note, failing=False: called.append(note))
    monkeypatch.setattr(gs, "_conn", lambda: None)
    gs._run_tick(beat=False)
    assert called == []
    gs._run_tick(beat=True)
    assert len(called) == 1


# ── the collision that made this shell dead code on its ship day ────────

def test_blueprint_NAME_is_unique_not_just_the_variable():
    # ★ Shipped as Blueprint("growth_master_shell", ...) while
    #   routes/growth_master_shell.py already owned that name. Flask refused
    #   the registration, main.py's fail-soft try/except swallowed it, and
    #   every route 404'd with CI fully green. Renaming the *_bp VARIABLE does
    #   not rename the blueprint.
    assert gs.growth_funnel_master_shell_bp.name == "growth_funnel_master_shell"


def test_no_route_collides_with_the_existing_growth_shell():
    import routes.growth_master_shell as other
    mine = {r for r in _declared_routes(gs)}
    theirs = {r for r in _declared_routes(other)}
    assert mine and theirs
    assert not (mine & theirs), f"colliding routes: {mine & theirs}"


def _declared_routes(mod):
    import inspect, re
    src = inspect.getsource(mod)
    return set(re.findall(r'\.route\(\s*"([^"]+)"', src))


def test_both_blueprints_register_on_one_flask_app():
    # The real failure mode, reproduced: two blueprints on ONE app. This raises
    # ValueError if either name or the object collides.
    import routes.growth_master_shell as other
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(other.growth_master_shell_bp)
    app.register_blueprint(gs.growth_funnel_master_shell_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/admin/growth-funnel/master-tick" in rules
    assert "/api/v1/admin/growth/master-tick" in rules


def test_distribution_imports_the_REAL_roster_symbol():
    # ★ The first live tick returned '?' because this lane imported _PLATFORMS
    #   (guessed) instead of PLATFORMS (actual). Assert the symbol exists so a
    #   rename upstream fails here, not silently on the board.
    from routes.agent_onboarding_master_shell import PLATFORMS
    assert isinstance(PLATFORMS, list) and PLATFORMS
    assert all("reach_weight" in p for p in PLATFORMS)


def test_distribution_reads_the_live_roster_without_monkeypatching():
    # End-to-end against the real constant: must produce a real verdict, not '?'.
    checks = _by_id(gs._lane_distribution())
    assert checks["dist_high"]["pass"] in (True, False)
    assert "could not import" not in checks["dist_high"]["detail"]


def test_renamed_generic_bucket_still_counts_as_UNattributed(monkeypatch):
    # ★ mcp-server #156 renamed the unnameable bucket 'mcp' ->
    #   'mcp-generic-client'. If this shell does not know the new name, the
    #   rename alone walks lane 1 to ~100% and flips it to a false PASS — the
    #   same agents, better-labelled, counted as attributed.
    monkeypatch.setattr(gs, "_conn",
                        lambda: _Conn([("mcp-generic-client", 226),
                                       ("claude", 16)]))
    c = _by_id(gs._lane_attribution())
    assert c["attr_cov"]["pass"] is False
    assert "6.6%" in c["attr_cov"]["detail"]


def test_generic_list_covers_both_bucket_names_during_the_migration():
    # Old rows keep 'mcp'; new rows get 'mcp-generic-client'. Both are absences.
    for name in ("mcp", "mcp-generic-client"):
        assert name in gs._GENERIC_PLATFORMS
