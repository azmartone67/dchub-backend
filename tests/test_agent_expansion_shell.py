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
             "partner_keys", "enterprise_embedding"]
    positions = [src.index(f'"{lane}"') for lane in order]
    assert positions == sorted(positions)


def test_every_lane_fails_soft_without_db(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_conn", lambda: None)
    for lane_fn in (m._lane_front_door, m._lane_planner_adoption,
                    m._lane_platform_doors, m._lane_partner_keys,
                    m._lane_enterprise_embedding):
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
    assert sum("is_real_external" in s for s in sql) >= 4
