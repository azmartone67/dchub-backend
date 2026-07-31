"""Regression guards: the /grid pages' loopback self-call must clear the
metered map-session gate.

routes/grid_public_routes.py::_fetch_live loopback-fetches
/api/v1/grid/intelligence/<iso> to render the public /grid pages. That
prefix is METERED (free_tier_gate.METERED_MAP_PREFIXES), and the gate runs
at before_request — before any route-level UA / X-DC-Probe bypass —
privileging only X-Internal-Key / X-Admin-Key / paid keys / loopback
remote_addr. Self-identifying headers alone are NOT enough: the identical
loopback in routes/radar.py was 402'd by our own paywall and pinned /radar
to 07-16 baselines for 15 days (PR #2018, tests/test_radar_freshness.py).

These tests pin the fix's three legs:
  1. _fetch_live sends X-Internal-Key sourced from DCHUB_INTERNAL_KEY
     (DCHUB_SYNC_KEY fallback);
  2. a non-200 from the loopback is loud (log.warning carrying the HTTP
     status), never silently swallowed into the stale-cache fallback;
  3. /api/v1/grid/intelligence is still in METERED_MAP_PREFIXES — the
     reason leg 1 exists. If it ever leaves the metered set on purpose,
     retire these guards with it rather than cargo-culting the key.

House rules: static AST extraction, no import of routes.grid_public_routes
(it imports flask at module scope), nothing executes at module scope here.
"""
import ast
import os

_GRID = os.path.join(os.path.dirname(__file__), "..", "routes",
                     "grid_public_routes.py")
_GATE = os.path.join(os.path.dirname(__file__), "..", "free_tier_gate.py")


def _tree(path: str, min_body: int) -> ast.Module:
    with open(os.path.abspath(path), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: an empty/degenerate parse would vacuously pass every
    # search below (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > min_body, (
        f"{path} parsed to a degenerate module — extraction harness is not "
        "looking at the real file")
    return tree


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"routes/grid_public_routes.py no longer defines {name}() — these "
        "self-call guards need updating, not deleting")


def _code_consts(fn: ast.FunctionDef) -> set:
    """String constants in the function BODY, docstring excluded — a guard a
    docstring can satisfy guards nothing (the 07-19 'comments satisfy grep'
    lesson, applied to AST)."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return {n.value for stmt in body for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# ── 1. the loopback must clear the before_request gates ─────────────────────

def test_fetch_live_sends_internal_key():
    consts = _code_consts(_fn(_tree(_GRID, 10), "_fetch_live"))
    assert "X-Internal-Key" in consts, (
        "_fetch_live() no longer sends X-Internal-Key — loopback calls to "
        "/api/v1/grid/intelligence/* get 402'd by the metered map-session "
        "gate (free_tier_gate, before_request; UA/X-DC-Probe never "
        "privilege it) and every /grid page silently falls back to stale "
        "cache / 0 MW — the radar re-stamp class, PR #2018")
    assert "DCHUB_INTERNAL_KEY" in consts, (
        "_fetch_live() must source the key from DCHUB_INTERNAL_KEY (set on "
        "the Railway web service; DCHUB_SYNC_KEY is the accepted fallback)")
    assert "DCHUB_SYNC_KEY" in consts, (
        "_fetch_live() lost the DCHUB_SYNC_KEY fallback — the two envs are "
        "equal on the web service today, but only by convention")


# ── 2. a non-200 from our own paywall must be loud ──────────────────────────

def test_fetch_live_non200_is_loud():
    fn = _fn(_tree(_GRID, 10), "_fetch_live")
    warning_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "warning"
    ]
    assert warning_calls, (
        "_fetch_live() swallows a non-200 from its own loopback silently "
        "again — the miss must log.warning, or the next gate regression "
        "pins /grid to stale cache invisibly for weeks (radar did exactly "
        "this, 07-16 → 07-31)")
    assert any(
        isinstance(a, ast.Attribute) and a.attr == "status_code"
        for call in warning_calls for arg in call.args for a in ast.walk(arg)
    ), (
        "_fetch_live()'s loopback warning no longer carries the HTTP status "
        "— a 402 (own paywall) must be distinguishable from a 500 in logs")


# ── 3. the reason leg 1 exists: the prefix is still metered ─────────────────

def test_grid_intelligence_still_metered():
    tree = _tree(_GATE, 10)
    prefixes = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "METERED_MAP_PREFIXES"
                for t in node.targets):
            assert isinstance(node.value, ast.List), (
                "METERED_MAP_PREFIXES is no longer a literal list — update "
                "this extraction")
            prefixes = {e.value for e in node.value.elts
                        if isinstance(e, ast.Constant)}
    assert prefixes, (
        "free_tier_gate.py no longer defines METERED_MAP_PREFIXES — the "
        "metered gate moved; re-point these guards at its new home")
    assert "/api/v1/grid/intelligence" in prefixes, (
        "/api/v1/grid/intelligence left METERED_MAP_PREFIXES — if that is "
        "deliberate, the X-Internal-Key on _fetch_live's loopback is no "
        "longer load-bearing and this guard file can be retired with it")
