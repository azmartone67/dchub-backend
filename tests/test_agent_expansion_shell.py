"""Agent Expansion Master Shell #45 — contract pins.

The shell is a read-only board over the five expansion levers. These tests pin
the properties that make it trustworthy: honest degradation without a DB,
five lanes in the ranked order, the deliberate ON CONFLICT DO NOTHING on
human-owned door statuses, and the pure-DB invariant (the module never makes
HTTP calls — the 2026-07-06 flywheel-outage rule).
"""
import inspect


def _mod():
    from routes import agent_expansion_master_shell as m
    return m


def test_five_lanes_in_ranked_order():
    m = _mod()
    src = inspect.getsource(m._run_tick)
    order = ["front_door_funnel", "planner_adoption", "platform_doors",
             "partner_keys", "enterprise_embedding", "story_shipped",
             "data_decides"]
    positions = [src.index(f'"{lane}"') for lane in order]
    assert positions == sorted(positions)


def test_every_lane_fails_soft_without_db(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_conn", lambda: None)
    for lane_fn in (m._lane_front_door, m._lane_planner_adoption,
                    m._lane_platform_doors, m._lane_partner_keys,
                    m._lane_enterprise_embedding, m._lane_story_shipped,
                    m._lane_data_decides):
        checks = lane_fn()
        assert isinstance(checks, list) and checks
        assert not any(c.get("passed") for c in checks), lane_fn.__name__


def test_doors_seed_do_nothing_is_deliberate_and_documented():
    m = _mod()
    src = inspect.getsource(m._ensure_doors)
    assert "ON CONFLICT (door) DO NOTHING" in src
    # the choice must carry its justification in-code — ownership decides
    assert "HUMAN-OWNED" in src


def test_module_never_makes_http_calls():
    m = _mod()
    src = inspect.getsource(m)
    for banned in ("requests.", "urllib.request", "http.client", "httpx"):
        assert banned not in src, banned


def test_canonical_basis_only():
    # The property that matters: no QUERY counts on session_id, and the
    # real-external identity predicate backs every count. Assert over the
    # module's actual SQL string literals via ast — prose (docstrings and
    # comments that WARN about session_id) must never satisfy or fail this
    # (the self-matching-test trap, third documented hit in this repo).
    import ast
    m = _mod()
    tree = ast.parse(inspect.getsource(m))
    sql = [n.value for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "SELECT" in n.value and "mcp_calls" in n.value]
    assert sql, "no SQL literals found — extraction broke, not the module"
    assert all("session_id" not in s for s in sql)
    # ★2026-08-05: this was `sum(...) >= 4` — a COUNT of inline queries, which
    # made "delete an inline query and import the canonical one instead" fail
    # the fence, i.e. it penalised the fix. The property is per-query, not a
    # census: EVERY identity-view query this module still runs must carry the
    # real-external predicate. Deleting one in favour of an import is fine;
    # adding an unguarded one is not.
    unguarded = [s for s in sql if "is_real_external" not in s]
    assert not unguarded, (
        f"identity-view SQL without is_real_external: {unguarded}")


def test_wave2_rows_never_touch_the_doors_aggregate():
    # Wave-2 human rows share the doors table; lane 3's SELECT must be scoped
    # to its own door names or a posted story greens the doors lane.
    m = _mod()
    src = inspect.getsource(m._lane_platform_doors)
    assert "door = ANY(%s)" in src
    door_names = {d[0] for d in m._DOORS_SEED}
    accel_names = {d[0] for d in m._ACCEL_SEED}
    assert not door_names & accel_names
    assert accel_names == {"story_posted", "post_gate_decision"}


def test_wave2_seed_shares_the_do_nothing_ownership_rule():
    m = _mod()
    src = inspect.getsource(m._ensure_doors)
    assert "_DOORS_SEED + _ACCEL_SEED" in src
