"""
REST walls offer the plan that actually opens them (frontend#1534, 2026-09-21).

The owner's rule: never offer a rung that cannot open the thing it is shown on.
be#5091 made rest_wall_ladder(opens_on_rest, mcp_tool) honest; this sweep points
the remaining literal-/pricing REST walls at it, each with the CHEAPEST plan its
own gate admits:

  /api/deals ×2           caller_is_privileged('PRO')           → pro
  /api/ai/query           user_has_access(plan, 'pro')          → pro
  /api/site-score         plan in ('pro','enterprise','developer') → developer
  /api/v1/site-forecast   plan in ('pro','enterprise','developer') → developer
  capacity require_plan   its own min_plan                      → min_plan

site-score and site-forecast said "requires Pro" / "$99/mo" while their gates
admitted Developer; they now offer the Developer checkout and read the price.

★ WHY THE GATE IS READ, NOT RESTATED. The guard below takes each wall's plan
from the gate expression in the same function. Widen or narrow a gate without
moving its wall, or the reverse, and it fails. A list typed into this test would
pass that drift forever.
"""
import ast
import base64
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "rest-walls-test-key-not-a-secret"
RANK = {"free": 0, "identified": 1, "starter": 1, "developer": 2, "pro": 3,
        "founding": 3, "enterprise": 4}


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")[0]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)   # /go/c links are signed with it


# ── capacity require_plan, driven through the real decorator ─────────────

@pytest.mark.parametrize("min_plan", ["developer", "pro"])
def test_require_plan_wall_offers_the_plan_it_requires(monkeypatch, min_plan):
    flask = pytest.importorskip("flask")
    import api_tier_gating
    import capacity_headroom_api as cap
    monkeypatch.setattr(api_tier_gating, "validate_api_key", lambda k: (True, {"plan": "free"}))
    app = flask.Flask("rest-walls-test")

    @app.route("/gated-wall")
    @cap.require_plan(min_plan)
    def gated():
        return "opened"

    r = app.test_client().get("/gated-wall", headers={"X-API-Key": "k"})
    body = r.get_json()
    assert r.status_code == 403 and body["error"] == "plan_upgrade_required", body
    assert _plan_of(body["upgrade_url"]) == min_plan
    assert body["upgrade_options"][0]["plan"] == min_plan
    assert body["upgrade_options"][0]["opens"] == "rest"


def test_require_plan_still_opens_for_a_plan_that_qualifies(monkeypatch):
    flask = pytest.importorskip("flask")
    import api_tier_gating
    import capacity_headroom_api as cap
    monkeypatch.setattr(api_tier_gating, "validate_api_key", lambda k: (True, {"plan": "pro"}))
    app = flask.Flask("rest-walls-test-open")

    @app.route("/gated-open")
    @cap.require_plan("developer")
    def gated():
        return "opened"

    assert app.test_client().get("/gated-open", headers={"X-API-Key": "k"}).data == b"opened"


# ── main.py's helpers, run as written ────────────────────────────────────

def _main_helpers():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    fns = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)
           and n.name in ("_honest_rest_wall", "_plan_price_display")]
    assert len(fns) == 2
    ns = {}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def test_the_main_helper_offers_the_plan_it_is_given():
    ns = _main_helpers()
    for plan in ("developer", "pro"):
        wall = ns["_honest_rest_wall"](plan)
        assert _plan_of(wall["upgrade_url"]) == plan
        assert wall["upgrade_options"][0]["plan"] == plan
    import tier_registry
    assert ns["_plan_price_display"]("developer") == tier_registry.price_display("developer")


# ── each wall offers the cheapest plan its own gate admits ───────────────

def _fn(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _gate_plans(fn):
    """Plans the function's own gate admits: string tuples in `x in (...)` /
    `x not in (...)` comparisons that name a paid plan, and the plan argument of
    user_has_access(plan, '<p>') / caller_is_privileged('<P>')."""
    plans = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops):
            for c in n.comparators:
                if isinstance(c, ast.Tuple) and all(isinstance(e, ast.Constant) for e in c.elts):
                    vals = {str(e.value).lower() for e in c.elts}
                    if vals & {"pro", "developer"} and vals <= set(RANK):
                        plans |= vals
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id == "user_has_access" and len(n.args) == 2 and isinstance(n.args[1], ast.Constant):
                plans.add(str(n.args[1].value).lower())
            if n.func.id == "caller_is_privileged" and n.args and isinstance(n.args[0], ast.Constant):
                plans.add(str(n.args[0].value).lower())
    return plans


def _wall_plans(fn, helper):
    return {str(n.args[0].value) for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == helper
            and n.args and isinstance(n.args[0], ast.Constant)}


@pytest.mark.parametrize("name", ["api_site_score", "api_site_forecast", "ai_query"])
def test_the_main_wall_offers_the_cheapest_plan_its_gate_admits(name):
    fn = _fn("main.py", name)
    gate = _gate_plans(fn)
    assert gate, f"{name}: no gate found; the guard would pass vacuously"
    cheapest = min(gate, key=lambda p: RANK[p])
    assert _wall_plans(fn, "_honest_rest_wall") == {cheapest}, (name, gate)


def test_the_deals_walls_go_through_the_honest_ladder_at_its_gate():
    fn = _fn("routes/deals_routes.py", "get_deals")
    assert _gate_plans(fn) == {"pro"}
    assert any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_rest_wall"
               for n in ast.walk(fn)), "get_deals no longer reaches the ladder"
    # Every upgrade_url the handler writes is computed, never the literal page.
    urls = [v for d in ast.walk(fn) if isinstance(d, ast.Dict)
            for k, v in zip(d.keys, d.values)
            if isinstance(k, ast.Constant) and k.value == "upgrade_url"]
    assert len(urls) == 2, len(urls)
    for v in urls:
        assert not any(isinstance(c, ast.Constant) and "dchub.cloud/pricing" in str(c.value)
                       for c in ast.walk(v)), ast.unparse(v)
    helper = _fn("routes/deals_routes.py", "_rest_wall")
    kw = {k.arg: k.value.value for n in ast.walk(helper) if isinstance(n, ast.Call)
          and getattr(n.func, "id", "") == "rest_wall_ladder" for k in n.keywords}
    assert kw.get("opens_on_rest") == "pro"
